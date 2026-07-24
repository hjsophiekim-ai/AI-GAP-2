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

from app.trading.macd2 import config, ledger, order_executor, risk_exit
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState, RuntimeStatus, SignalState
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_macd_crossover,
    is_tradeable_completed_bar,
    make_signal_id,
    resample_completed_3m,
    signed_b_condition,
)
from app.trading.trading_cost_engine import TradeCostEngine

KST = config.KST

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


def _pending_primary_signal(
    bars_3m,
    *,
    last_evaluated_bar_ts: Optional[str],
    last_detected_direction: Optional[Direction],
    now: datetime,
) -> tuple[Direction, Optional[Any], Optional[Direction]]:
    detected = last_detected_direction
    for i in range(config.EMA_SLOW, len(bars_3m) + 1):
        snap = calculate_macd(bars_3m.iloc[:i])
        if snap is None:
            continue
        if last_evaluated_bar_ts and snap.bar_dt.isoformat() <= last_evaluated_bar_ts:
            continue
        if not is_tradeable_completed_bar(snap.bar_dt, now):
            continue
        pattern = evaluate_macd_crossover(snap, detected)
        if pattern != Direction.HOLD:
            return pattern, snap, pattern
    return Direction.HOLD, None, detected


def _expire_pending_if_needed(state: RuntimeState, macd_snap, now: datetime) -> bool:
    pending = state.pending_signal
    if not pending:
        return False
    pending_dir = Direction(pending.get("direction")) if pending.get("direction") in {d.value for d in Direction} else None
    age = _pending_age_sec(pending, now)
    if not _pending_direction_still_active(pending_dir, macd_snap) or (age is not None and age > config.PENDING_SIGNAL_RETRY_SEC):
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
):
    order_started = time.monotonic()
    result.signal_dispatch_trace = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": signal_type,
        "completed_bar_at": macd_snap.bar_dt.isoformat(),
        "position_reconcile_result": None,
        "quote_status": None,
        "target_quote_valid": False,
        "order_executor_called": False,
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

    quote_status, quotes = _quote_status_for_order(
        market_data, (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL)
    )
    target = order_executor.target_symbol_for_direction(direction)
    result.signal_dispatch_trace["quote_status"] = quote_status
    result.signal_dispatch_trace["target_quote_valid"] = bool(target and target in quotes and quotes[target] > 0)
    if quote_status != "READY":
        state.order_block_reason = quote_status
        result.signal_dispatch_trace["final_block_reason"] = quote_status
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=quote_status,
        )
        result.skipped = quote_status
        result.timing["order_execution"] = time.monotonic() - order_started
        return None

    result.signal_dispatch_trace["order_executor_called"] = True
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id, quotes=quotes,
        position=position, budget=state.budget,
        processed_signal_ids=frozenset(state.processed_signal_ids),
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

    bar_ts_str = macd_snap.bar_dt.isoformat()
    is_new_bar = bar_ts_str != state.last_evaluated_bar_ts
    tradeable_bar = is_tradeable_completed_bar(macd_snap.bar_dt, now)

    before_open = now.time() < config.SESSION_OPEN
    entry_cutoff_passed = now.time() >= config.NEW_ENTRY_CUTOFF
    force_liquidate_time = now.time() >= config.FORCE_LIQUIDATE_AT
    entry_window_open = (not before_open) and (not entry_cutoff_passed)
    t0 = time.monotonic()
    current_condition = (
        evaluate_macd_crossover(macd_snap, state.last_detected_direction)
        if tradeable_bar and entry_window_open else Direction.HOLD
    )
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
                )
                if outcome is not None:
                    _apply_switch_outcome(state, outcome, pending_dir)
                    result.actions.append(f"OPPOSITE_SIGNAL:{pending_dir.value}")
                    state.last_evaluated_bar_ts = bar_ts_str
                    return result

        if is_new_bar and entry_window_open and tradeable_bar:
            pattern, signal_snap, detected = _pending_primary_signal(
                bars_3m,
                last_evaluated_bar_ts=state.last_evaluated_bar_ts,
                last_detected_direction=state.last_detected_direction,
                now=now,
            )
            if pattern == Direction.HOLD and signal_snap is None and current_condition != Direction.HOLD:
                pattern = current_condition
                signal_snap = macd_snap
                detected = pattern
            if detected is not None:
                state.last_detected_direction = detected
            if pattern != Direction.HOLD:
                state.current_episode_direction = pattern
                state.latest_primary_flag = pattern
                macd_signal_snap = signal_snap or macd_snap
                target = order_executor.target_symbol_for_direction(pattern)
                if target != pos.symbol:
                    signal_id = make_signal_id(macd_signal_snap.bar_dt, pattern)
                    state.latest_primary_signal_id = signal_id
                    signal_detected_at = datetime.now(KST)
                    result.signal_detected_at = signal_detected_at.isoformat()
                    outcome = _execute_or_wait(
                        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_signal_snap,
                        direction=pattern, signal_id=signal_id, signal_type="REVERSAL", position=pos, result=result,
                    )
                    _record_signal_ledger(state, macd_signal_snap, pattern, "REVERSAL", signal_id, signal_detected_at, outcome)
                    if outcome is not None:
                        _apply_switch_outcome(state, outcome, pattern)
                        result.actions.append(f"OPPOSITE_SIGNAL:{pattern.value}")
                        state.last_evaluated_bar_ts = macd_signal_snap.bar_dt.isoformat()
                        return result
                    state.last_evaluated_bar_ts = macd_signal_snap.bar_dt.isoformat()
                    return result

        if profit_lock_should_exit:
            outcome = order_executor.execute_exit(
                broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                exit_reason=config.EXIT_PROFIT_LOCK, entry_price=pos.avg_price,
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
            )
            if outcome is not None:
                _apply_switch_outcome(state, outcome, pending_dir)
                result.actions.append(f"ENTRY:{pending_dir.value}")
                state.last_evaluated_bar_ts = bar_ts_str
                return result

    if is_new_bar and entry_window_open and tradeable_bar:
        pattern, signal_snap, detected = _pending_primary_signal(
            bars_3m,
            last_evaluated_bar_ts=state.last_evaluated_bar_ts,
            last_detected_direction=state.last_detected_direction,
            now=now,
        )
        if pattern == Direction.HOLD and signal_snap is None and current_condition != Direction.HOLD:
            pattern = current_condition
            signal_snap = macd_snap
            detected = pattern
        if detected is not None:
            state.last_detected_direction = detected
        if pattern != Direction.HOLD:
            state.current_episode_direction = pattern
            state.latest_primary_flag = pattern
            macd_signal_snap = signal_snap or macd_snap
            signal_id = make_signal_id(macd_signal_snap.bar_dt, pattern)
            state.latest_primary_signal_id = signal_id
            signal_detected_at = datetime.now(KST)
            result.signal_detected_at = signal_detected_at.isoformat()
            outcome = _execute_or_wait(
                broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_signal_snap,
                direction=pattern, signal_id=signal_id, signal_type="INITIAL", position=None, result=result,
            )
            _record_signal_ledger(state, macd_signal_snap, pattern, "INITIAL", signal_id, signal_detected_at, outcome)
            if outcome is not None:
                _apply_switch_outcome(state, outcome, pattern)
                result.actions.append(f"ENTRY:{pattern.value}")
            state.last_evaluated_bar_ts = macd_signal_snap.bar_dt.isoformat()
            return result

    state.last_evaluated_bar_ts = bar_ts_str
    return result


def _apply_exit_outcome(state: RuntimeState, outcome) -> None:
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


def _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, detected_at, outcome) -> None:
    order_result = outcome.final_state.value if outcome is not None else SignalState.WAITING.value
    block_reason = outcome.block_reason or "" if outcome is not None else (state.order_block_reason or "WAITING")
    trading_date = macd_snap.bar_dt.astimezone(KST).strftime("%Y%m%d")
    ledger.append_signal({
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
    })


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
