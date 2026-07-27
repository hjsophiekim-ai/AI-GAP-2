"""MACD2 worker — single 5-second tick loop (docs §11/§13).

``run_once()`` is one tick, fully testable without a background thread.
``start()``/``stop()`` wrap it in exactly one daemon thread. Never calls KIS
directly, and never triggers MarketDataService's own incremental merge
either — MarketDataService's own history-updater/quote-updater background
threads refresh those caches; this module only reads them via
``get_history_df()``/``get_quote()`` (docs §8/§11).
Never renders UI, never re-walks full history, never reloads modules, never
uses a pending-signal timer or a signal queue, never runs more than one
Worker thread, never reuses a stopped thread object.

Every tick also reconciles the real account position against
``state.position`` (one ``broker.get_positions()`` call) before evaluating
any signal — a mismatch blocks every order this tick (entry/switch/exit)
until it clears (docs: 실제 계좌와 state는 항상 reconcile). A new trading
date resets only the session-scoped runtime fields (last_signal_direction,
last_evaluated_bar_ts, today's Profit Lock/processed_signal_ids) — the
permanent signal ledger (ledger.append_signal, dedup by signal_id) is never
cleared.

Priority order for a held position, per docs §10 (this is docs' own stated
order, not a re-derivation of MACD v1's runtime behavior — docs is the sole
source of truth per the 2026-07-23 design decision):
  1) 15:00 FORCED_LIQUIDATION
  2) STOP_LOSS
  3) OPPOSITE_SIGNAL (a new, confirmed opposite signed-B direction)
  4) PROFIT_LOCK
  5) HOLD
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from app.trading.macd2 import config, ledger, order_executor, risk_exit
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState, RuntimeStatus, SignalState
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_macd_crossover,
    evaluate_primary_forming_crossover,
    forming_bar_window,
    make_provisional_signal_id,
    make_signal_id,
    resample_completed_3m,
    signed_b_condition,
)
from app.trading.trading_cost_engine import TradeCostEngine

KST = config.KST

# 주문 성공 응답만으로 체결로 간주하지 않고 실제 체결/잔고 재조회로 확인하는
# 최대 대기시간(docs 2026-07-27 체결확인 fix) — order_executor의 reconcile
# 재시도 파라미터로 Worker의 모든 프로덕션 호출에 적용된다.
ORDER_FILL_RECONCILE_RETRIES = max(1, int(config.ORDER_FILL_POLL_MAX_SEC / config.ORDER_FILL_POLL_INTERVAL_SEC))
ORDER_FILL_RECONCILE_DELAY_SEC = config.ORDER_FILL_POLL_INTERVAL_SEC

POSITION_MISMATCH = "POSITION_MISMATCH"
POSITION_DATA_ERROR = "POSITION_DATA_ERROR"
QUOTE_STALE = "QUOTE_STALE"
MATCH_FLAT = "MATCH_FLAT"
MATCH_POSITION = "MATCH_POSITION"
RECOVERED_FROM_BROKER = "RECOVERED_FROM_BROKER"
RECOVERED_TO_FLAT = "RECOVERED_TO_FLAT"
SIGNAL_NOT_DISPATCHED = "SIGNAL_NOT_DISPATCHED"
TEMPORARY_BLOCK_REASONS = {
    QUOTE_STALE,
    order_executor.BLOCK_ORDER_DATA_INVALID,
    POSITION_DATA_ERROR,
}


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip()
    except Exception:
        return ""


@dataclass
class TickResult:
    ok: bool = True
    actions: list[str] = field(default_factory=list)
    error: Optional[str] = None
    skipped: Optional[str] = None
    signal_detected_at: Optional[str] = None
    order_requested_at: Optional[str] = None
    signal_dispatch_trace: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, float] = field(default_factory=dict)


def _net_return_pct(symbol: str, entry_price: float, current_price: float, quantity: int) -> float:
    if entry_price <= 0 or quantity <= 0 or current_price <= 0:
        return 0.0
    cost = TradeCostEngine().compute_net_pnl(
        symbol, entry_price, current_price, quantity, buy_order_type="market", sell_order_type="market",
    )
    return float(cost["net_pnl"]) / (entry_price * quantity) * 100.0


def _fresh_quote_prices(market_data: MarketDataService, symbols: tuple[str, ...]) -> dict[str, float]:
    """Only symbols whose cached quote age <= QUOTE_MAX_AGE_SEC are considered
    valid for order sizing/exit decisions (docs §12) — stale/missing quotes
    are simply absent from the returned dict, letting order_executor's own
    ORDER_DATA_INVALID gate fire naturally.
    """
    prices: dict[str, float] = {}
    for symbol in symbols:
        snap = market_data.get_quote(symbol)
        if snap is None or snap.error or snap.price <= 0:
            continue
        if snap.age_sec is not None and snap.age_sec > config.QUOTE_MAX_AGE_SEC:
            continue
        prices[symbol] = snap.price
    return prices


def _apply_day_rollover(state: RuntimeState, now: datetime) -> None:
    """New trading date -> reset only session-scoped runtime fields (docs:
    거래일 변경 초기화). The permanent signal ledger (ledger.append_signal's
    CSV, deduped by signal_id) is untouched here — ``processed_signal_ids``
    is only the in-state, same-day dedup list, safe to clear on rollover."""
    today_str = now.strftime("%Y%m%d")
    if state.session_date is None:
        # First tick ever for this state (e.g. brand-new RuntimeState) — there
        # is nothing to roll over yet, so just record today without wiping
        # fields a caller may have already set for the current session.
        state.session_date = today_str
        return
    if state.session_date == today_str:
        return
    state.session_date = today_str
    state.last_signal_direction = None
    state.last_detected_direction = None
    state.last_executed_direction = None
    state.current_episode_direction = None
    state.last_evaluated_bar_ts = None
    state.last_confirmed_bar_ts = None
    state.processed_signal_ids = []
    state.pending_signal = None
    state.peak_net_return = 0.0
    state.profit_lock_active = False


def _relation_from_diff(diff: Optional[float]) -> str:
    if diff is None:
        return "EQUAL"
    if diff > 0:
        return "ABOVE"
    if diff < 0:
        return "BELOW"
    return "EQUAL"


def initialize_strategy_session(
    state: RuntimeState,
    market_data: MarketDataService,
    *,
    now: Optional[datetime] = None,
    worker_instance_id: Optional[str] = None,
) -> RuntimeState:
    now = now or datetime.now(KST)
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.session_started_at = now.isoformat()
    state.worker_instance_id = worker_instance_id
    state.pending_signal = None
    state.last_detected_direction = None
    state.last_executed_direction = None
    state.current_episode_direction = None
    state.processed_signal_ids = []

    df_1m = market_data.get_history_df()
    macd_snap = calculate_macd(resample_completed_3m(df_1m, now=now))
    if macd_snap is not None:
        state.session_baseline_bar_ts = macd_snap.bar_dt.isoformat()
        state.last_evaluated_bar_ts = macd_snap.bar_dt.isoformat()
        # Marks THIS bar as already baseline'd so run_once's Primary gate
        # (_advance_confirmed_primary) treats the next NEW completed bar as a
        # genuine same-day continuation (mid-session Worker (re)start with
        # plenty of already-completed today bars) rather than mistakenly
        # re-treating it as "the very first bar ever evaluated" — while a
        # real overnight gap (this baseline bar dated yesterday, the next one
        # dated today) is still correctly caught by that gate's own date
        # comparison.
        state.last_confirmed_bar_ts = macd_snap.bar_dt.isoformat()
        state.baseline_relation = macd_snap.relation or _relation_from_diff(macd_snap.current_diff)
        state.primary_previous_diff = macd_snap.previous_diff
        state.primary_current_diff = macd_snap.current_diff
        state.primary_relation = state.baseline_relation
        state.signed_b_shadow_direction = signed_b_condition(macd_snap)
        state.signed_b_shadow_hist_last3 = macd_snap.hist_last3
    else:
        state.session_baseline_bar_ts = None
        state.last_evaluated_bar_ts = None
        state.baseline_relation = None
    return state


def _normalize_broker_positions(raw_positions) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    broker_positions: dict[str, dict[str, Any]] = {}
    all_positions: list[dict[str, Any]] = []
    for p in raw_positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip()
        try:
            qty = int(float(getattr(p, "quantity", 0) or 0))
        except (TypeError, ValueError):
            qty = 0
        try:
            avg_price = float(getattr(p, "avg_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            avg_price = 0.0
        row = {"symbol": symbol, "qty": qty, "avg_price": avg_price}
        all_positions.append(row)
        if symbol in config.TRADE_SYMBOLS and qty > 0:
            broker_positions[symbol] = row
    return broker_positions, all_positions


def _runtime_position_dict(state: RuntimeState) -> dict[str, Any]:
    pos = state.position
    if pos is None or not pos.symbol or int(pos.quantity or 0) <= 0:
        return {"symbol": None, "qty": 0, "avg_price": 0.0}
    return {"symbol": pos.symbol, "qty": int(pos.quantity), "avg_price": float(pos.avg_price or 0.0)}


def _should_reconcile_position(state: RuntimeState, now: datetime, *, force: bool = False) -> bool:
    if force or state.position is not None:
        return True
    if not state.last_position_reconcile_at:
        return True
    try:
        last = datetime.fromisoformat(state.last_position_reconcile_at)
    except ValueError:
        return True
    return (now - last).total_seconds() >= config.FLAT_POSITION_RECONCILE_INTERVAL_SEC


def reconcile_position_state(broker, state: RuntimeState, now: datetime, *, force: bool = False) -> str:
    if not _should_reconcile_position(state, now, force=force):
        return str((state.position_reconcile_diag or {}).get("comparison_result") or MATCH_FLAT)
    try:
        broker_positions, all_positions = _normalize_broker_positions(broker.get_positions())
        broker_error = None
    except Exception as exc:
        broker_positions, all_positions = {}, []
        broker_error = repr(exc)

    runtime = _runtime_position_dict(state)
    diag = {
        "runtime_position": runtime,
        "broker_positions": all_positions,
        f"{config.LONG_SYMBOL}_broker_qty": int((broker_positions.get(config.LONG_SYMBOL) or {}).get("qty") or 0),
        f"{config.INVERSE_SYMBOL}_broker_qty": int((broker_positions.get(config.INVERSE_SYMBOL) or {}).get("qty") or 0),
        "reconciled_at": now.isoformat(),
        "broker_response_error": broker_error,
    }

    if broker_error:
        diag.update({"comparison_result": POSITION_DATA_ERROR, "mismatch_reason": broker_error})
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return POSITION_DATA_ERROR

    broker_owned = [row for row in broker_positions.values() if int(row["qty"]) > 0]
    if runtime["qty"] <= 0 and not broker_owned:
        diag.update({"comparison_result": MATCH_FLAT, "mismatch_reason": ""})
        state.position = None
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return MATCH_FLAT

    if runtime["qty"] > 0:
        broker_row = broker_positions.get(str(runtime["symbol"]))
        if broker_row and int(broker_row["qty"]) == int(runtime["qty"]):
            diag.update({"comparison_result": MATCH_POSITION, "mismatch_reason": ""})
            state.position_reconcile_diag = diag
            state.last_position_reconcile_at = now.isoformat()
            return MATCH_POSITION
        if not broker_owned:
            state.position = None
            state.peak_net_return = 0.0
            state.profit_lock_active = False
            diag.update({"comparison_result": RECOVERED_TO_FLAT, "mismatch_reason": "runtime_position_broker_flat"})
            state.position_reconcile_diag = diag
            state.last_position_reconcile_at = now.isoformat()
            return RECOVERED_TO_FLAT

    if runtime["qty"] <= 0 and broker_owned:
        recovered = broker_owned[0]
        state.position = PositionSnapshot(
            symbol=recovered["symbol"], quantity=int(recovered["qty"]),
            avg_price=float(recovered["avg_price"] or 0.0), entry_at=now,
        )
        diag.update({"comparison_result": RECOVERED_FROM_BROKER, "mismatch_reason": "runtime_flat_broker_position"})
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return RECOVERED_FROM_BROKER

    diag.update({"comparison_result": POSITION_MISMATCH, "mismatch_reason": "runtime_broker_position_diff"})
    state.position_reconcile_diag = diag
    state.last_position_reconcile_at = now.isoformat()
    return POSITION_MISMATCH


def _quote_status_for_order(market_data: MarketDataService, symbols: tuple[str, ...]) -> tuple[str, dict[str, float]]:
    statuses = market_data.quote_statuses(symbols)
    valid_prices = _fresh_quote_prices(market_data, symbols)
    vals = set(statuses.values())
    if vals == {"VALID"}:
        return "READY", valid_prices
    if "STALE" in vals:
        return QUOTE_STALE, valid_prices
    return order_executor.BLOCK_ORDER_DATA_INVALID, valid_prices


def _required_quote_symbols(direction: Direction, position: Optional[PositionSnapshot]) -> tuple[str, ...]:
    symbols = [config.WATCH_SYMBOL]
    if position is not None and position.quantity > 0 and position.symbol:
        symbols.append(position.symbol)
    target = order_executor.target_symbol_for_direction(direction)
    if target:
        symbols.append(target)
    return tuple(dict.fromkeys(symbols))


def _quote_ages(market_data: MarketDataService, symbols: tuple[str, ...]) -> dict[str, Optional[float]]:
    ages: dict[str, Optional[float]] = {}
    for symbol in symbols:
        snap = market_data.get_quote(symbol)
        ages[symbol] = snap.age_sec if snap is not None else None
    return ages


def _pending_detected_at(pending: dict[str, Any], now: datetime) -> datetime:
    """The ORIGINAL detection time of a retried pending signal — the
    QUOTE_STALE 15s window (config.QUOTE_STALE_MAX_WAIT_SEC) is anchored to
    when the signal was first confirmed, not to this retry tick's ``now``."""
    raw = pending.get("detected_at")
    if raw:
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            pass
    return now


def _pending_age_sec(pending: dict[str, Any], now: datetime) -> Optional[float]:
    raw = pending.get("detected_at")
    if not raw:
        return None
    try:
        return (now - datetime.fromisoformat(str(raw))).total_seconds()
    except ValueError:
        return None


def _pending_direction_still_active(pending_dir: Optional[Direction], macd_snap) -> bool:
    if pending_dir == Direction.UP_RED:
        return (macd_snap.current_diff if macd_snap.current_diff is not None else macd_snap.macd - macd_snap.signal) > 0
    if pending_dir == Direction.DOWN_BLUE:
        return (macd_snap.current_diff if macd_snap.current_diff is not None else macd_snap.macd - macd_snap.signal) < 0
    return False


def _quote_valid_for_provisional(market_data: MarketDataService, symbol: str) -> bool:
    snap = market_data.get_quote(symbol)
    return bool(
        snap is not None
        and not snap.error
        and snap.price > 0
        and (snap.age_sec is None or snap.age_sec <= config.QUOTE_MAX_AGE_SEC)
    )


def _update_provisional_diagnostics(state: RuntimeState, macd_snap) -> None:
    """Raw, unconfirmed live diff/MACD/signal display — updated every tick
    regardless of candidate/confirmation status (diagnostic only, never order
    authority)."""
    state.provisional_bar_start = macd_snap.bar_dt.astimezone(KST).isoformat()
    state.provisional_bar_end = (macd_snap.bar_dt + timedelta(minutes=3)).astimezone(KST).isoformat()
    state.provisional_macd = macd_snap.macd
    state.provisional_signal = macd_snap.signal
    state.provisional_diff = macd_snap.current_diff


def _update_provisional_shadow_flag(state: RuntimeState, macd_snap, pattern: Direction, signal_id: Optional[str]) -> None:
    """Shadow/candidate display only (2026-07-27 KIS-parity fix) — the
    forming/provisional bar never carries order, stats, or last_direction
    authority any more. See _advance_confirmed_primary() for the actual
    order-authoritative Primary flag (completed 3m bars only)."""
    _update_provisional_diagnostics(state, macd_snap)
    state.provisional_flag = pattern if pattern != Direction.HOLD else None
    state.provisional_signal_id = signal_id


def _reset_candidate(state: RuntimeState) -> None:
    state.candidate_flag = None
    state.candidate_bar_ts = None
    state.candidate_first_seen_at = None
    state.candidate_first_diff = None


def _advance_provisional_candidate(
    state: RuntimeState,
    provisional_snap,
    provisional_condition: Direction,
    now: datetime,
    *,
    today_has_completed_bar: bool,
) -> tuple[Optional[Any], Direction]:
    """Two-tick shadow/candidate gate (2026-07-27 momentary-crossing fix,
    demoted to display-only by the 2026-07-27 KIS-parity fix — this NEVER
    drives orders/stats/last_direction any more; see
    _advance_confirmed_primary() for that).

    A single-tick provisional forming-bar crossing from
    evaluate_primary_forming_crossover() only becomes a "confirmed candidate"
    (for UI display) once the SAME direction is still present on a LATER,
    fresh quote tick at least config.PROVISIONAL_CONFIRM_MIN_GAP_SEC apart.
    Any other outcome this tick — HOLD, the opposite direction, or the
    forming bar rolling over — cancels the candidate immediately; a fresh
    candidate may start right after.

    ``today_has_completed_bar`` suppresses the very first forming bar of a
    new trading day the same way _advance_confirmed_primary() suppresses the
    first completed bar — previous_diff there still refers to yesterday.
    """
    if provisional_snap is None or provisional_condition == Direction.HOLD or not today_has_completed_bar:
        _reset_candidate(state)
        return None, Direction.HOLD

    bar_key = provisional_snap.bar_dt.isoformat()
    same_candidate = state.candidate_flag == provisional_condition and state.candidate_bar_ts == bar_key
    if not same_candidate:
        # First sighting of this direction on this forming bar — arm the
        # candidate only, never a dispatch signal.
        state.candidate_flag = provisional_condition
        state.candidate_bar_ts = bar_key
        state.candidate_first_seen_at = now.isoformat()
        state.candidate_first_diff = provisional_snap.current_diff
        if config.PROVISIONAL_CONFIRM_MIN_GAP_SEC <= 0:
            state.candidate_confirmed_at = now.isoformat()
            state.candidate_confirmed_diff = provisional_snap.current_diff
            return provisional_snap, provisional_condition
        return None, Direction.HOLD

    gap_sec = None
    if state.candidate_first_seen_at:
        try:
            gap_sec = (now - datetime.fromisoformat(state.candidate_first_seen_at)).total_seconds()
        except ValueError:
            gap_sec = None
    if gap_sec is None or gap_sec < config.PROVISIONAL_CONFIRM_MIN_GAP_SEC:
        return None, Direction.HOLD

    state.candidate_confirmed_at = now.isoformat()
    state.candidate_confirmed_diff = provisional_snap.current_diff
    return provisional_snap, provisional_condition


def _last_1m_diag(df_1m) -> tuple[Optional[str], Optional[float]]:
    if df_1m is None or getattr(df_1m, "empty", True) or "datetime" not in df_1m.columns or "close" not in df_1m.columns:
        return None, None
    work = df_1m.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    closes = pd.to_numeric(work["close"], errors="coerce")
    work = work.loc[work["datetime"].notna() & closes.notna()].copy()
    if work.empty:
        return None, None
    row = work.sort_values("datetime").iloc[-1]
    return pd.Timestamp(row["datetime"]).to_pydatetime().isoformat(), float(row["close"])


def _update_forming_input_diag(
    state: RuntimeState,
    *,
    now: datetime,
    df_1m,
    watch_price: Optional[float],
    market_data: MarketDataService,
) -> None:
    forming_start, forming_end = forming_bar_window(now)
    last_1m_at, last_1m_close = _last_1m_diag(df_1m)
    state.provisional_bar_start = forming_start.isoformat()
    state.provisional_bar_end = forming_end.isoformat()
    state.provisional_evaluated_at = datetime.now(KST).isoformat()
    state.provisional_input_now = now.astimezone(KST).isoformat()
    state.provisional_quote_price = watch_price
    state.provisional_last_1m_at = last_1m_at
    state.provisional_last_1m_close = last_1m_close
    diag = market_data.quote_normalization_diag() if hasattr(market_data, "quote_normalization_diag") else {}
    note = str(diag.get("reason") or "")
    if diag:
        note = note or "NO_SCALE_CHANGE"
    state.provisional_price_scale_note = note


def _update_history_freshness_diag(
    state: RuntimeState, *, df_1m, macd_snap, watch_price: Optional[float], now: datetime,
) -> None:
    """docs 2026-07-27 §1: 당일 추가 1분봉 수/history newest/마지막 완성 3분봉
    시각을 runtime/UI에 표시하고, KIS 1분봉(history)과 실시간 quote의 단위·
    시각이 설명되지 않게 어긋나면 주문을 차단한다 (quote_history_mismatch_reason).
    market_data._normalize_quote_price()가 이미 10배/0.1배 스케일 오차는
    보정하므로, 여기서는 그 보정 이후에도 남는 큰 괴리(단위 불일치)와 1분봉
    history 자체가 갱신되지 않는 시각 불일치만 잡아낸다.
    """
    today_str = now.astimezone(KST).strftime("%Y%m%d")
    today_count = 0
    newest_at: Optional[str] = None
    newest_close: Optional[float] = None
    newest_dt: Optional[datetime] = None
    if df_1m is not None and not df_1m.empty and "datetime" in df_1m.columns:
        dates = df_1m["datetime"].dt.strftime("%Y%m%d")
        today_count = int((dates == today_str).sum())
        last_row = df_1m.sort_values("datetime").iloc[-1]
        newest_dt = pd.Timestamp(last_row["datetime"]).to_pydatetime()
        newest_at = newest_dt.isoformat()
        if "close" in df_1m.columns:
            try:
                newest_close = float(last_row["close"])
            except (TypeError, ValueError):
                newest_close = None
    state.today_1m_bar_count = today_count
    state.history_newest_at = newest_at
    state.last_completed_3m_bar_at = macd_snap.bar_dt.isoformat() if macd_snap is not None else None

    reason: Optional[str] = None
    if now.time() >= config.SESSION_OPEN and not _within_open_grace_window(now):
        if newest_dt is None:
            reason = "HISTORY_EMPTY"
        elif (now - newest_dt).total_seconds() > config.HISTORY_STALE_MAX_SEC:
            reason = "HISTORY_STALE"
    if reason is None and watch_price is not None and newest_close is not None and newest_close > 0:
        ratio = float(watch_price) / float(newest_close)
        if not (config.QUOTE_HISTORY_PRICE_RATIO_MIN <= ratio <= config.QUOTE_HISTORY_PRICE_RATIO_MAX):
            reason = "QUOTE_HISTORY_PRICE_MISMATCH"
    state.quote_history_mismatch_reason = reason


def _within_open_grace_window(now: datetime, *, grace_sec: float = 60.0) -> bool:
    """A brief grace window right at SESSION_OPEN — the history-updater
    thread may not have pulled today's very first 1m bar yet even though the
    session clock has already ticked over; avoid a false HISTORY_STALE/EMPTY
    block in that narrow window."""
    open_dt = now.astimezone(KST).replace(
        hour=config.SESSION_OPEN.hour, minute=config.SESSION_OPEN.minute, second=0, microsecond=0,
    )
    return bool(now.astimezone(KST) < open_dt + timedelta(seconds=grace_sec))


def _expire_pending_if_needed(state: RuntimeState, macd_snap, now: datetime) -> bool:
    pending = state.pending_signal
    if not pending:
        return False
    pending_dir = Direction(pending.get("direction")) if pending.get("direction") in {d.value for d in Direction} else None
    age = _pending_age_sec(pending, now)
    inactive = macd_snap is not None and not _pending_direction_still_active(pending_dir, macd_snap)
    if inactive or (age is not None and age > config.PENDING_SIGNAL_RETRY_SEC):
        pending["status"] = SignalState.EXPIRED.value
        state.pending_signal = None
        return True
    return False


def _set_pending_signal(
    state: RuntimeState,
    *,
    signal_id: str,
    direction: Direction,
    signal_type: str,
    macd_snap,
    detected_at: datetime,
    reason: str,
) -> None:
    existing = state.pending_signal if state.pending_signal and state.pending_signal.get("signal_id") == signal_id else {}
    state.pending_signal = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": signal_type,
        "bar_ts": macd_snap.bar_dt.isoformat(),
        "detected_at": existing.get("detected_at") or detected_at.isoformat(),
        "status": SignalState.WAITING.value,
        "reason": reason,
        "order_requested": False,
    }


def _has_order_request(outcome) -> bool:
    return bool(outcome.timestamps.get("buy_requested_at") or outcome.timestamps.get("sell_requested_at"))


def _mark_processed_after_request(state: RuntimeState, outcome) -> None:
    if _has_order_request(outcome) and outcome.signal_id and outcome.signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [outcome.signal_id]


def _execute_or_wait(
    *,
    broker,
    market_data: MarketDataService,
    state: RuntimeState,
    now: datetime,
    macd_snap,
    direction: Direction,
    signal_id: str,
    signal_type: str,
    position: Optional[PositionSnapshot],
    result: TickResult,
    signal_detected_at: Optional[datetime] = None,
):
    order_started = time.monotonic()
    result.signal_dispatch_trace = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": signal_type,
        "completed_bar_at": macd_snap.bar_dt.isoformat(),
        "forming_bar_start": "",
        "forming_bar_end": "",
        "position_reconcile_result": None,
        "quote_status": None,
        "required_quote_symbols": [],
        "quote_ages": {},
        "target_quote_valid": False,
        "order_executor_called": False,
        "executor_called_at": None,
        "broker_called": False,
        "broker_order_id": "",
        "broker_raw": {},
        "final_block_reason": None,
    }
    reconcile = reconcile_position_state(broker, state, now, force=True)
    result.signal_dispatch_trace["position_reconcile_result"] = reconcile
    if reconcile == RECOVERED_FROM_BROKER:
        state.order_block_reason = RECOVERED_FROM_BROKER
        result.signal_dispatch_trace["final_block_reason"] = RECOVERED_FROM_BROKER
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=RECOVERED_FROM_BROKER,
        )
        result.skipped = RECOVERED_FROM_BROKER
        result.timing["order_execution"] = time.monotonic() - order_started
        return None
    if reconcile in (POSITION_DATA_ERROR, POSITION_MISMATCH):
        state.order_block_reason = reconcile
        result.signal_dispatch_trace["final_block_reason"] = reconcile
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=reconcile,
        )
        result.skipped = reconcile
        result.timing["order_execution"] = time.monotonic() - order_started
        return None

    required_symbols = _required_quote_symbols(direction, position)
    quote_status, quotes = _quote_status_for_order(market_data, required_symbols)
    target = order_executor.target_symbol_for_direction(direction)
    detected_at = signal_detected_at or now
    quote_ages_at_detection = _quote_ages(market_data, required_symbols)
    result.signal_dispatch_trace["required_quote_symbols"] = list(required_symbols)
    result.signal_dispatch_trace["quote_ages"] = quote_ages_at_detection
    result.signal_dispatch_trace["quote_status"] = quote_status
    result.signal_dispatch_trace["target_quote_valid"] = bool(target and target in quotes and quotes[target] > 0)
    state.last_quote_stale_signal_id = signal_id
    state.last_quote_stale_quote_ages = str(quote_ages_at_detection)

    retry_count = 0
    while quote_status != "READY":
        elapsed = (datetime.now(KST) - detected_at).total_seconds()
        if elapsed >= config.QUOTE_STALE_MAX_WAIT_SEC or retry_count >= config.QUOTE_STALE_RETRY_MAX_ATTEMPTS:
            break
        market_data.refresh_quotes(symbols=required_symbols)
        time.sleep(config.QUOTE_STALE_RETRY_INTERVAL_SEC)
        retry_count += 1
        quote_status, quotes = _quote_status_for_order(market_data, required_symbols)

    result.signal_dispatch_trace["quote_stale_retry_count"] = retry_count
    state.last_quote_stale_retry_count = retry_count

    if quote_status != "READY":
        state.order_block_reason = config.MISSED_SIGNAL_QUOTE_STALE
        state.last_quote_stale_result = config.MISSED_SIGNAL_QUOTE_STALE
        result.signal_dispatch_trace["final_block_reason"] = config.MISSED_SIGNAL_QUOTE_STALE
        result.signal_dispatch_trace["quote_ages"] = _quote_ages(market_data, required_symbols)
        # Resolved synchronously within this one call/tick — never left as a
        # cross-tick pending retry (docs 2026-07-27 QUOTE_STALE fix), so no
        # later tick can mistakenly dispatch this signal_id late.
        state.pending_signal = None
        result.skipped = config.MISSED_SIGNAL_QUOTE_STALE
        result.timing["order_execution"] = time.monotonic() - order_started
        return None

    state.last_quote_stale_result = "RECOVERED" if retry_count > 0 else None
    result.signal_dispatch_trace["quote_ages"] = _quote_ages(market_data, required_symbols)

    result.signal_dispatch_trace["order_executor_called"] = True
    result.signal_dispatch_trace["executor_called_at"] = datetime.now(KST).isoformat()
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id, quotes=quotes,
        position=position, budget=state.budget,
        processed_signal_ids=frozenset(state.processed_signal_ids),
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
    )
    if outcome is None:
        state.order_block_reason = SIGNAL_NOT_DISPATCHED
        result.skipped = SIGNAL_NOT_DISPATCHED
        result.signal_dispatch_trace["final_block_reason"] = SIGNAL_NOT_DISPATCHED
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=SIGNAL_NOT_DISPATCHED,
        )
        result.timing["order_execution"] = time.monotonic() - order_started
        return None
    result.order_requested_at = outcome.timestamps.get("sell_requested_at") or outcome.timestamps.get("buy_requested_at")
    result.signal_dispatch_trace["order_requested_at"] = result.order_requested_at or ""
    if outcome.orderable_cash_at_sizing is not None:
        state.last_order_orderable_cash = outcome.orderable_cash_at_sizing
        state.last_order_nrcvb_buy_amt = outcome.nrcvb_buy_amt
        state.last_order_nrcvb_buy_qty = outcome.nrcvb_buy_qty
        state.last_order_psbl_qty_calc_unpr = outcome.psbl_qty_calc_unpr
        state.last_order_budget_qty = outcome.budget_qty
        state.last_order_final_qty = outcome.final_qty
        state.last_order_sizing_rt_cd = outcome.sizing_rt_cd
        state.last_order_sizing_msg_cd = outcome.sizing_msg_cd
        state.last_order_sizing_msg1 = outcome.sizing_msg1
        state.last_order_sizing_price = outcome.sizing_price
        state.last_order_requested_qty = outcome.buy_result.requested_qty if outcome.buy_result else outcome.quantity
        state.last_order_expected_amount = outcome.expected_amount
    state.last_order_failure_stage = outcome.order_failure_stage
    state.last_order_filled_qty = outcome.filled_qty
    state.last_order_fill_poll_result = outcome.fill_poll_result
    state.last_order_balance_qty = outcome.balance_qty
    _record_broker_order_result(state, outcome)
    broker_result = outcome.buy_result or outcome.sell_result
    if broker_result is not None:
        result.signal_dispatch_trace["broker_called"] = True
        result.signal_dispatch_trace["broker_order_id"] = broker_result.order_id
        result.signal_dispatch_trace["broker_raw"] = dict(broker_result.raw or {})
    result.signal_dispatch_trace["orderable_cash"] = outcome.orderable_cash_at_sizing
    result.signal_dispatch_trace["nrcvb_buy_amt"] = outcome.nrcvb_buy_amt
    result.signal_dispatch_trace["nrcvb_buy_qty"] = outcome.nrcvb_buy_qty
    result.signal_dispatch_trace["psbl_qty_calc_unpr"] = outcome.psbl_qty_calc_unpr
    result.signal_dispatch_trace["budget_qty"] = outcome.budget_qty
    result.signal_dispatch_trace["final_qty"] = outcome.final_qty
    result.signal_dispatch_trace["sizing_price"] = outcome.sizing_price
    result.signal_dispatch_trace["requested_qty"] = outcome.buy_result.requested_qty if outcome.buy_result else outcome.quantity
    result.signal_dispatch_trace["expected_amount"] = outcome.expected_amount
    result.signal_dispatch_trace["sizing_rt_cd"] = outcome.sizing_rt_cd
    result.signal_dispatch_trace["sizing_msg_cd"] = outcome.sizing_msg_cd
    result.signal_dispatch_trace["sizing_msg1"] = outcome.sizing_msg1
    result.signal_dispatch_trace["filled_qty"] = outcome.filled_qty
    result.signal_dispatch_trace["fill_poll_result"] = outcome.fill_poll_result
    result.signal_dispatch_trace["balance_qty"] = outcome.balance_qty
    result.signal_dispatch_trace["failure_stage"] = outcome.order_failure_stage or ""
    if _has_order_request(outcome):
        if state.pending_signal and state.pending_signal.get("signal_id") == signal_id:
            state.pending_signal["status"] = SignalState.ORDER_REQUESTED.value
            state.pending_signal["order_requested"] = True
        _mark_processed_after_request(state, outcome)
    if outcome.final_state == SignalState.BLOCKED and outcome.block_reason in TEMPORARY_BLOCK_REASONS:
        state.order_block_reason = outcome.block_reason
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=outcome.block_reason or "BLOCKED",
        )
    else:
        state.pending_signal = None
    result.signal_dispatch_trace["final_block_reason"] = outcome.block_reason or ""
    result.timing["order_execution"] = time.monotonic() - order_started
    return outcome


def _advance_confirmed_primary(state: RuntimeState, macd_snap) -> Direction:
    """Primary (order-authoritative) crossover — completed 3m bars ONLY
    (docs 2026-07-27 KIS-parity fix): previous_diff/current_diff come solely
    from calculate_macd(bars_3m), the same confirmed MACD(12,26,9) KIS itself
    charts a flag on for a completed bar. Evaluated exactly once per new
    completed-bar timestamp — a repeat tick against the same bar_dt is
    always HOLD here, regardless of direction.

    The very first completed bar this state has ever evaluated (or the
    first one on a NEW calendar date relative to the previously evaluated
    bar) sets direction baseline only, never dispatches: previous_diff there
    can span across a trading-day gap (yesterday's last bar vs. today's
    first), so any zero-crossing is an overnight-gap artifact, not an
    intraday reversal. It is also never counted toward the repeat-direction
    suppression state, so a genuine later same-direction crossing still
    fires normally.
    """
    bar_key = macd_snap.bar_dt.isoformat()
    if state.last_confirmed_bar_ts == bar_key:
        return Direction.HOLD
    prior_bar_ts = state.last_confirmed_bar_ts
    state.last_confirmed_bar_ts = bar_key
    is_first_of_day = True
    if prior_bar_ts:
        try:
            prior_date = datetime.fromisoformat(prior_bar_ts).astimezone(KST).date()
            is_first_of_day = prior_date != macd_snap.bar_dt.astimezone(KST).date()
        except ValueError:
            is_first_of_day = True
    if is_first_of_day:
        return Direction.HOLD
    direction = evaluate_macd_crossover(macd_snap, state.last_detected_direction)
    if direction != Direction.HOLD:
        state.last_detected_direction = direction
        state.latest_primary_flag = direction
        state.latest_primary_signal_id = make_signal_id(macd_snap.bar_dt, direction)
    return direction


def _dispatch_confirmed_signal(
    *,
    broker,
    market_data: MarketDataService,
    state: RuntimeState,
    now: datetime,
    macd_snap,
    direction: Direction,
    signal_type: str,
    position: Optional[PositionSnapshot],
    result: TickResult,
    signal_id_override: Optional[str] = None,
):
    signal_id = signal_id_override or make_signal_id(macd_snap.bar_dt, direction)
    if signal_id in state.processed_signal_ids:
        state.order_block_reason = order_executor.BLOCK_DUPLICATE_SIGNAL
        return None
    if state.pending_signal and state.pending_signal.get("signal_id") == signal_id:
        return None

    state.current_episode_direction = direction
    signal_detected_at = datetime.now(KST)
    result.signal_detected_at = signal_detected_at.isoformat()
    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        direction=direction, signal_id=signal_id, signal_type=signal_type, position=position, result=result,
        signal_detected_at=signal_detected_at,
    )
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace)
    return outcome


def _dispatch_candidate_signal(
    *,
    broker,
    market_data: MarketDataService,
    state: RuntimeState,
    now: datetime,
    candidate_snap,
    direction: Direction,
    signal_type: str,
    position: Optional[PositionSnapshot],
    result: TickResult,
):
    signal_id = make_signal_id(candidate_snap.bar_dt, direction)
    return _dispatch_confirmed_signal(
        broker=broker, market_data=market_data, state=state, now=now,
        macd_snap=candidate_snap, direction=direction, signal_type=signal_type,
        position=position, result=result, signal_id_override=signal_id,
    )


def _record_confirmed_blocked_signal(
    *,
    state: RuntimeState,
    macd_snap,
    direction: Direction,
    signal_type: str,
    reason: str,
    result: TickResult,
) -> None:
    signal_id = make_signal_id(macd_snap.bar_dt, direction)
    if signal_id in state.processed_signal_ids:
        return
    state.order_block_reason = reason
    signal_detected_at = datetime.now(KST)
    result.signal_detected_at = signal_detected_at.isoformat()
    result.signal_dispatch_trace = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": signal_type,
        "completed_bar_at": macd_snap.bar_dt.isoformat(),
        "order_executor_called": False,
        "broker_called": False,
        "final_block_reason": reason,
    }
    outcome = order_executor.ExecutionOutcome(
        signal_id=signal_id,
        direction=direction,
        target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED,
        block_reason=reason,
    )
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace)


def run_once(
    *,
    broker,
    market_data: MarketDataService,
    state: RuntimeState,
    now: Optional[datetime] = None,
) -> TickResult:
    """One Worker cycle — no pending timers, no queues: same-tick signal->order."""
    now = now or datetime.now(KST)
    result = TickResult()
    tick_started = time.monotonic()
    result.timing["state_load"] = 0.0

    if not state.auto_trade_on:
        result.skipped = "auto_trade_off"
        result.timing["total"] = time.monotonic() - tick_started
        return result

    _apply_day_rollover(state, now)
    if state.strategy_version != config.STRATEGY_VERSION or state.signal_rule != config.SIGNAL_RULE:
        state.strategy_name = config.STRATEGY_NAME
        state.strategy_version = config.STRATEGY_VERSION
        state.signal_rule = config.SIGNAL_RULE
        state.pending_signal = None
        state.last_detected_direction = None
        state.last_evaluated_bar_ts = None
        state.last_confirmed_bar_ts = None

    t0 = time.monotonic()
    reconcile = reconcile_position_state(broker, state, now)
    result.timing["position_reconcile"] = time.monotonic() - t0
    if reconcile in (POSITION_DATA_ERROR, POSITION_MISMATCH, RECOVERED_FROM_BROKER, RECOVERED_TO_FLAT):
        state.order_block_reason = reconcile
        result.skipped = reconcile
        result.timing["total"] = time.monotonic() - tick_started
        return result

    t0 = time.monotonic()
    quotes = _fresh_quote_prices(market_data, (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL))
    result.timing["quote_cache_read"] = time.monotonic() - t0

    # Worker never calls KIS itself and never triggers the incremental merge —
    # MarketDataService's own history-updater thread refreshes this cache in
    # the background (docs §8/§11); this only reads the cached snapshot.
    t0 = time.monotonic()
    df_1m = market_data.get_history_df()
    result.timing["history_cache_read"] = time.monotonic() - t0
    t0 = time.monotonic()
    bars_3m = resample_completed_3m(df_1m, now=now)
    macd_snap = calculate_macd(bars_3m)
    result.timing["macd_calculation"] = time.monotonic() - t0
    if macd_snap is None:
        state.warmup_ready = False
        result.skipped = "NOT_READY"
        result.timing["total"] = time.monotonic() - tick_started
        return result
    state.warmup_ready = True
    state.primary_previous_diff = macd_snap.previous_diff
    state.primary_current_diff = macd_snap.current_diff
    state.primary_relation = macd_snap.relation or _relation_from_diff(macd_snap.current_diff)
    state.signed_b_shadow_direction = signed_b_condition(macd_snap)
    state.signed_b_shadow_hist_last3 = macd_snap.hist_last3

    # ── Shadow/candidate only: forming-bar provisional + Signed-B NEVER carry
    # order/stat/last_direction authority (docs 2026-07-27 KIS-parity fix) ──
    raw_provisional_snap = None
    raw_provisional_condition = Direction.HOLD
    watch_quote_ready = _quote_valid_for_provisional(market_data, config.WATCH_SYMBOL)
    watch_price = quotes.get(config.WATCH_SYMBOL)
    _update_forming_input_diag(
        state, now=now, df_1m=df_1m, watch_price=watch_price, market_data=market_data,
    )
    _update_history_freshness_diag(state, df_1m=df_1m, macd_snap=macd_snap, watch_price=watch_price, now=now)
    if watch_quote_ready and watch_price is not None:
        primary_result = evaluate_primary_forming_crossover(
            bars_3m, df_1m, now=now, current_price=watch_price,
            previous_direction=state.last_detected_direction,
        )
        raw_provisional_snap = primary_result.snapshot
        raw_provisional_condition = primary_result.direction

    if raw_provisional_snap is not None:
        _update_provisional_diagnostics(state, raw_provisional_snap)
    else:
        state.provisional_macd = None
        state.provisional_signal = None
        state.provisional_diff = None

    today_str = now.astimezone(KST).strftime("%Y%m%d")
    today_has_completed_bar = bool(
        not bars_3m.empty and (bars_3m["datetime"].dt.strftime("%Y%m%d") == today_str).any()
    )
    provisional_ready = today_has_completed_bar and bool(state.last_confirmed_bar_ts)
    candidate_snap, candidate_condition = _advance_provisional_candidate(
        state, raw_provisional_snap, raw_provisional_condition, now,
        today_has_completed_bar=provisional_ready,
    )
    if candidate_snap is not None:
        candidate_signal_id = make_provisional_signal_id(candidate_snap.bar_dt, candidate_condition)
        _update_provisional_shadow_flag(state, candidate_snap, candidate_condition, candidate_signal_id)
    else:
        state.provisional_flag = None
        state.provisional_signal_id = None

    # ── Primary (order-authoritative): completed 3m bars ONLY — same
    # confirmed MACD(12,26,9) KIS itself charts a flag on (docs 2026-07-27
    # KIS-parity fix). Evaluated exactly once per new completed-bar
    # timestamp; the first completed bar this state has ever evaluated (or
    # the first on a new calendar date) sets baseline only.
    confirmed_direction = _advance_confirmed_primary(state, macd_snap)

    bar_ts_str = macd_snap.bar_dt.isoformat()

    before_open = now.time() < config.SESSION_OPEN
    entry_cutoff_passed = now.time() >= config.NEW_ENTRY_CUTOFF
    force_liquidate_time = now.time() >= config.FORCE_LIQUIDATE_AT
    entry_window_open = (not before_open) and (not entry_cutoff_passed) and not state.quote_history_mismatch_reason
    t0 = time.monotonic()
    # A pending (blocked-on-retry) signal is always confirmed-bar-sourced now
    # — its own direction stays "active" as long as macd_snap (the completed
    # bar) hasn't rolled over to a new one yet.
    _expire_pending_if_needed(state, macd_snap, now)
    result.timing["signal_evaluation"] = time.monotonic() - t0

    pos = state.position

    # ── Held position: priority chain (docs §10) ───────────────────────
    if pos is not None and pos.quantity > 0:
        current_price = quotes.get(pos.symbol)
        profit_lock_should_exit = False

        if force_liquidate_time:
            outcome = order_executor.execute_exit(
                broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                exit_reason=config.EXIT_FORCED_LIQUIDATION, entry_price=pos.avg_price,
                reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
            )
            _apply_exit_outcome(state, outcome)
            result.actions.append(f"FORCED_LIQUIDATION:{pos.symbol}")
            return result

        if current_price is not None:
            net_return = _net_return_pct(pos.symbol, pos.avg_price, current_price, pos.quantity)
            exits = risk_exit.evaluate_position_exits(
                current_net_return=net_return, peak_net_return=state.peak_net_return,
                profit_lock_active=state.profit_lock_active,
            )
            # Bookkeeping (peak/active) updates every tick regardless of which
            # exit (if any) actually fires this tick.
            state.peak_net_return = exits.peak_net_return
            state.profit_lock_active = exits.profit_lock_active

            if exits.exit_reason == config.EXIT_STOP_LOSS:
                outcome = order_executor.execute_exit(
                    broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                    exit_reason=config.EXIT_STOP_LOSS, entry_price=pos.avg_price,
                    reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                )
                _apply_exit_outcome(state, outcome)
                result.actions.append(f"STOP_LOSS:{pos.symbol}")
                return result

            # Opposite-signal check (priority 3, below) gets first refusal —
            # Profit Lock's own exit (priority 4) only fires afterward if the
            # opposite-signal branch does not switch this tick.
            profit_lock_should_exit = exits.exit_reason == config.EXIT_PROFIT_LOCK

        if state.pending_signal and not state.pending_signal.get("order_requested"):
            pending_dir = Direction(state.pending_signal["direction"])
            if _pending_direction_still_active(pending_dir, macd_snap):
                outcome = _execute_or_wait(
                    broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                    direction=pending_dir, signal_id=str(state.pending_signal["signal_id"]),
                    signal_type=str(state.pending_signal.get("signal_type") or "REVERSAL"), position=pos, result=result,
                    signal_detected_at=_pending_detected_at(state.pending_signal, now),
                )
                if outcome is not None:
                    _apply_switch_outcome(state, outcome, pending_dir)
                    result.actions.append(f"OPPOSITE_SIGNAL:{pending_dir.value}")
                    state.last_evaluated_bar_ts = bar_ts_str
                    return result

        candidate_switch_direction = candidate_condition if candidate_snap is not None else Direction.HOLD
        if entry_window_open and candidate_switch_direction != Direction.HOLD:
            target = order_executor.target_symbol_for_direction(candidate_switch_direction)
            if target != pos.symbol:
                outcome = _dispatch_candidate_signal(
                    broker=broker, market_data=market_data, state=state, now=now,
                    candidate_snap=candidate_snap, direction=candidate_switch_direction,
                    signal_type="REVERSAL_PROVISIONAL", position=pos, result=result,
                )
                if outcome is not None:
                    _apply_switch_outcome(state, outcome, candidate_switch_direction)
                    result.actions.append(f"OPPOSITE_SIGNAL:{candidate_switch_direction.value}")
                    return result
            else:
                _record_confirmed_blocked_signal(
                    state=state, macd_snap=candidate_snap, direction=candidate_switch_direction,
                    signal_type="HELD_SAME_PROVISIONAL",
                    reason=order_executor.BLOCK_ALREADY_HOLDING,
                    result=result,
                )

        if entry_window_open and confirmed_direction != Direction.HOLD:
            target = order_executor.target_symbol_for_direction(confirmed_direction)
            if target != pos.symbol:
                outcome = _dispatch_confirmed_signal(
                    broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                    direction=confirmed_direction, signal_type="REVERSAL", position=pos, result=result,
                )
                if outcome is not None:
                    _apply_switch_outcome(state, outcome, confirmed_direction)
                    result.actions.append(f"OPPOSITE_SIGNAL:{confirmed_direction.value}")
                    return result
            else:
                _record_confirmed_blocked_signal(
                    state=state, macd_snap=macd_snap, direction=confirmed_direction,
                    signal_type="HELD_SAME",
                    reason=order_executor.BLOCK_ALREADY_HOLDING,
                    result=result,
                )

        if profit_lock_should_exit:
            outcome = order_executor.execute_exit(
                broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                exit_reason=config.EXIT_PROFIT_LOCK, entry_price=pos.avg_price,
                reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
            )
            _apply_exit_outcome(state, outcome)
            result.actions.append(f"PROFIT_LOCK:{pos.symbol}")
            return result

        state.last_evaluated_bar_ts = bar_ts_str
        return result

    # ── Flat: new-entry evaluation ──────────────────────────────────────
    if state.pending_signal and not state.pending_signal.get("order_requested"):
        pending_dir = Direction(state.pending_signal["direction"])
        if _pending_direction_still_active(pending_dir, macd_snap):
            outcome = _execute_or_wait(
                broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                direction=pending_dir, signal_id=str(state.pending_signal["signal_id"]),
                signal_type=str(state.pending_signal.get("signal_type") or "INITIAL"), position=None, result=result,
                signal_detected_at=_pending_detected_at(state.pending_signal, now),
            )
            if outcome is not None:
                _apply_switch_outcome(state, outcome, pending_dir)
                result.actions.append(f"ENTRY:{pending_dir.value}")
                state.last_evaluated_bar_ts = bar_ts_str
                return result

    candidate_entry_direction = candidate_condition if candidate_snap is not None else Direction.HOLD
    if entry_window_open and candidate_entry_direction != Direction.HOLD:
        outcome = _dispatch_candidate_signal(
            broker=broker, market_data=market_data, state=state, now=now,
            candidate_snap=candidate_snap, direction=candidate_entry_direction,
            signal_type="INITIAL_PROVISIONAL", position=None, result=result,
        )
        if outcome is not None:
            _apply_switch_outcome(state, outcome, candidate_entry_direction)
            result.actions.append(f"ENTRY:{candidate_entry_direction.value}")
            return result

    if entry_window_open and confirmed_direction != Direction.HOLD:
        outcome = _dispatch_confirmed_signal(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            direction=confirmed_direction, signal_type="INITIAL", position=None, result=result,
        )
        if outcome is not None:
            _apply_switch_outcome(state, outcome, confirmed_direction)
            result.actions.append(f"ENTRY:{confirmed_direction.value}")
            return result

    state.last_evaluated_bar_ts = bar_ts_str
    return result


def _record_broker_order_result(state: RuntimeState, outcome) -> None:
    """Most recent broker call result (any leg: entry/switch/exit), so the UI
    can show it independent of the ephemeral per-tick TickResult."""
    result = outcome.buy_result or outcome.sell_result
    if result is None:
        return
    state.last_broker_order_id = result.order_id
    if result.success:
        state.last_broker_order_result = "OK"
    else:
        state.last_broker_order_result = outcome.order_failure_stage or outcome.block_reason or "ORDER_FAILED"
    state.last_broker_order_symbol = result.symbol
    state.last_broker_order_side = result.side
    state.last_broker_order_at = datetime.now(KST).isoformat()


def _apply_exit_outcome(state: RuntimeState, outcome) -> None:
    _record_broker_order_result(state, outcome)
    if outcome.final_state == SignalState.EXECUTED:
        state.position = None
        state.peak_net_return = 0.0
        state.profit_lock_active = False
    state.order_block_reason = outcome.block_reason


def _apply_switch_outcome(state: RuntimeState, outcome, pattern: Direction) -> None:
    """Retry policy (docs §2): every signal_id is single-shot regardless of
    outcome — success, failure, or block — so it is never automatically
    retried; a later, genuinely new signal_id (a different bar) is still
    free to fire. A switch whose SELL leg cleared to 0 but whose BUY leg then
    failed/was blocked leaves the account really flat, so state.position must
    reflect that immediately rather than keep pointing at the already-sold
    symbol (docs: 스위칭 부분실패 상태 처리) — this also prevents a duplicate
    SELL next tick, since the held-position branch will no longer see a
    stale position for that symbol.
    """
    if outcome.final_state == SignalState.EXECUTED:
        state.position = PositionSnapshot(
            symbol=outcome.target_symbol, quantity=outcome.quantity,
            avg_price=(outcome.filled_avg_price or (outcome.buy_result.executed_price if outcome.buy_result else 0.0)),
            entry_at=datetime.now(KST),
        )
        state.last_signal_direction = pattern
        state.last_executed_direction = pattern
        state.last_signal_bar_ts = outcome.timestamps.get("evaluated_at")
        state.peak_net_return = 0.0
        state.profit_lock_active = False
    elif outcome.sell_result is not None and outcome.sell_result.success and outcome.sell_qty_after == 0:
        state.position = None
        state.peak_net_return = 0.0
        state.profit_lock_active = False
    if _has_order_request(outcome) and outcome.signal_id and outcome.signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [outcome.signal_id]
    state.order_block_reason = outcome.block_reason


def _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, detected_at, outcome, dispatch_trace=None) -> None:
    order_result = outcome.final_state.value if outcome is not None else SignalState.WAITING.value
    block_reason = outcome.block_reason or "" if outcome is not None else (state.order_block_reason or "WAITING")
    trading_date = macd_snap.bar_dt.astimezone(KST).strftime("%Y%m%d")
    trace = dict(dispatch_trace or {})
    raw = dict(trace.get("broker_raw") or {})
    # All ledger-recorded signals are confirmed (completed-bar) since the
    # 2026-07-27 KIS-parity fix — the forming/provisional candidate never
    # reaches this function any more (shadow display only).
    written = ledger.append_signal({
        "trading_date": trading_date,
        "completed_bar_at": macd_snap.bar_dt.astimezone(KST).strftime("%H%M%S"),
        "signal_id": signal_id,
        "signal_type": signal_type,
        "direction": direction.value,
        "macd": macd_snap.macd,
        "signal": macd_snap.signal,
        "hist_last3": str(macd_snap.hist_last3),
        "detected_at": detected_at.isoformat(),
        "order_requested_at": (
            outcome.timestamps.get("buy_requested_at") or outcome.timestamps.get("sell_requested_at") or ""
            if outcome is not None else ""
        ),
        "order_result": order_result,
        "block_reason": block_reason,
        "signal_bar_at": macd_snap.bar_dt.astimezone(KST).isoformat(),
        "signal_confirmed_at": (macd_snap.bar_dt + timedelta(minutes=3)).astimezone(KST).isoformat(),
        "baseline_completed_bar_at": state.session_baseline_bar_ts or "",
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "worker_code_sha": _git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        "forming_bar_start": trace.get("forming_bar_start") or "",
        "forming_bar_end": trace.get("forming_bar_end") or "",
        "previous_macd": macd_snap.previous_macd if macd_snap.previous_macd is not None else "",
        "previous_signal": macd_snap.previous_signal if macd_snap.previous_signal is not None else "",
        "previous_diff": macd_snap.previous_diff if macd_snap.previous_diff is not None else "",
        "provisional_macd": "",
        "provisional_signal": "",
        "provisional_diff": "",
        "confirmed_macd": macd_snap.macd,
        "confirmed_signal": macd_snap.signal,
        "confirmed_diff": macd_snap.current_diff,
        "provisional_direction": "",
        "confirmed_direction": direction.value,
        "quote_ages": str(trace.get("quote_ages") or {}),
        "position_reconcile": trace.get("position_reconcile_result") or "",
        "executor_called": trace.get("order_executor_called"),
        "order_requested_at_trace": trace.get("order_requested_at") or "",
        "broker_called": trace.get("broker_called"),
        "broker_order_id": trace.get("broker_order_id") or "",
        "broker_rt_cd": raw.get("rt_cd") or "",
        "broker_msg_cd": raw.get("msg_cd") or "",
        "broker_msg1": raw.get("msg1") or "",
        "orderable_cash": trace.get("orderable_cash") if trace.get("orderable_cash") is not None else "",
        "nrcvb_buy_amt": trace.get("nrcvb_buy_amt") if trace.get("nrcvb_buy_amt") is not None else "",
        "nrcvb_buy_qty": trace.get("nrcvb_buy_qty") if trace.get("nrcvb_buy_qty") is not None else "",
        "psbl_qty_calc_unpr": trace.get("psbl_qty_calc_unpr") if trace.get("psbl_qty_calc_unpr") is not None else "",
        "budget_qty": trace.get("budget_qty") if trace.get("budget_qty") is not None else "",
        "final_qty": trace.get("final_qty") if trace.get("final_qty") is not None else "",
        "sizing_price": trace.get("sizing_price") if trace.get("sizing_price") is not None else "",
        "requested_qty": trace.get("requested_qty") if trace.get("requested_qty") is not None else "",
        "expected_amount": trace.get("expected_amount") if trace.get("expected_amount") is not None else "",
        "sizing_rt_cd": trace.get("sizing_rt_cd") or "",
        "sizing_msg_cd": trace.get("sizing_msg_cd") or "",
        "sizing_msg1": trace.get("sizing_msg1") or "",
        "filled_qty": trace.get("filled_qty") if trace.get("filled_qty") is not None else "",
        "fill_poll_result": trace.get("fill_poll_result") or "",
        "balance_qty": trace.get("balance_qty") if trace.get("balance_qty") is not None else "",
        "failure_stage": trace.get("failure_stage") or "",
        "final_result": order_result if not block_reason else f"{order_result}:{block_reason}",
    })
    state.last_duplicate_signal_id = None if written else signal_id


class Macd2Worker:
    """Owns exactly one background tick thread (docs §13 single-Worker principle)."""

    def __init__(
        self, *, broker, market_data: MarketDataService, get_state, save_state,
        tick_interval_sec: float = config.WORKER_INTERVAL_SEC,
    ) -> None:
        self._broker = broker
        self._market_data = market_data
        self._get_state = get_state
        self._save_state = save_state
        self._tick_interval_sec = tick_interval_sec
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._tick_intervals: list[float] = []
        self._tick_n = 0
        self._last_tick_at: Optional[datetime] = None
        self._last_exception: Optional[str] = None
        self._last_stage_timing: dict[str, float] = {}
        self._lock = threading.RLock()
        self._instance_id = uuid.uuid4().hex[:12]
        self._started_at: Optional[datetime] = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def tick_stats(self) -> dict[str, Any]:
        with self._lock:
            intervals = list(self._tick_intervals[-20:])
            mean = sum(intervals) / len(intervals) if intervals else None
            p95 = sorted(intervals)[int(len(intervals) * 0.95) - 1] if intervals else None
            age = (datetime.now(KST) - self._last_tick_at).total_seconds() if self._last_tick_at else None
            next_tick_at = (
                (self._last_tick_at + timedelta(seconds=self._tick_interval_sec)).isoformat()
                if self._last_tick_at else None
            )
            return {
                "tick_n": self._tick_n, "mean_interval_sec": mean, "p95_interval_sec": p95,
                "max_interval_sec": max(intervals) if intervals else None,
                "last_tick_age_sec": age, "last_exception": self._last_exception,
                "stalled": bool(age is not None and age > config.WORKER_STALL_AGE_SEC),
                "instance_id": self._instance_id,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
                "next_tick_at": next_tick_at,
                "recent_tick_sample_count": len(self._tick_intervals),
                "stage_timing_sec": dict(self._last_stage_timing),
            }

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            stage_timing: dict[str, float] = {}
            try:
                t_stage = time.monotonic()
                state = self._get_state()
                state.worker_instance_id = self._instance_id
                stage_timing["state_load"] = time.monotonic() - t_stage
                tick_result = run_once(broker=self._broker, market_data=self._market_data, state=state, now=datetime.now(KST))
                stage_timing.update(tick_result.timing)
                t_stage = time.monotonic()
                self._save_state(state)
                stage_timing["state_save"] = time.monotonic() - t_stage
                with self._lock:
                    self._last_exception = None
            except Exception as exc:
                with self._lock:
                    self._last_exception = f"{exc}\n{traceback.format_exc()}"
            elapsed = time.monotonic() - t0
            with self._lock:
                self._tick_n += 1
                self._last_tick_at = datetime.now(KST)
                self._tick_intervals.append(elapsed)
                self._tick_intervals = self._tick_intervals[-50:]
                stage_timing["total"] = elapsed
                self._last_stage_timing = stage_timing
            self._stop_event.wait(max(0.0, self._tick_interval_sec - elapsed))

    def start(self) -> None:
        if self.is_alive():
            return  # never spawn a second Worker thread
        self._stop_event.clear()
        self._started_at = datetime.now(KST)
        self._thread = threading.Thread(target=self._run_loop, name="macd2-worker", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        self._thread = None  # never reused — start() always creates a fresh Thread object
