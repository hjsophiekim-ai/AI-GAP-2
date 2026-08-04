"""TSLA_AUTO worker — single tick loop (docs §7/§8/§12/§14).

``run_once()`` is one tick, fully testable without a background thread.
Structure mirrors app/trading/macd2/worker.py (docs TSLA_AUTO_COPY_MAP.md —
COPY_WITH_US_MARKET_CHANGE + new stop-loss-cooldown/NORMAL-CHOP/15:45-cutoff
logic), re-implemented here independently — never imports
app.trading.macd2.worker or any other app.trading.macd2.* module.

Order authority rests EXCLUSIVELY with the confirmed, completed-3m-bar
Primary crossover (_advance_confirmed_primary). The forming-bar/candidate
path is shadow-display only and is designed from the start so it can never
gain order authority the way MACD2's did in a 2026-07-31 regression — there
is no candidate-dispatch code path here at all.

Priority order for a held position, matched to MACD2:
  1) Forced liquidation (market-close relative cutoff)
  2) Stop Loss
  3) Approved opposite (confirmed) flag switch
  4) Profit Lock
  5) Hold
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from app.trading.tsla_auto import config, ledger, market_session, order_executor, risk_exit, strong_flag_filter
from app.trading.tsla_auto.market_data import MarketDataService, filter_complete_3m_bars
from app.trading.tsla_auto.models import Direction, PositionSnapshot, RuntimeState, RuntimeStatus, SignalState
from app.trading.tsla_auto.signal_engine import (
    calculate_macd,
    evaluate_confirmed_macd_flag,
    evaluate_macd_crossover,
    evaluate_primary_forming_crossover,
    make_signal_id,
    resample_completed_3m,
)
from app.trading.tsla_auto.cost_engine import OverseasTradeCostEngine

ET = config.ET

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
TEMPORARY_BLOCK_REASONS = {QUOTE_STALE, order_executor.BLOCK_ORDER_DATA_INVALID, POSITION_DATA_ERROR}


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip()
    except Exception:
        return ""


def git_sha() -> str:
    """Public wrapper — same short SHA written into every signal-ledger
    row's worker_code_sha column."""
    return _git_sha()


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


def _net_return_pct(entry_price: float, current_price: float, quantity: int) -> float:
    if entry_price <= 0 or quantity <= 0 or current_price <= 0:
        return 0.0
    risk_cost_engine = OverseasTradeCostEngine(cost_config={
        "overseas_buy_fee_rate": 0.0,
        "overseas_sell_fee_rate": 0.0,
        "fx_conversion_fee_rate": 0.0,
        "fx_spread_rate": 0.0,
        "slippage_rate_limit_order": 0.0001,
    })
    cost = risk_cost_engine.compute_net_pnl_usd(entry_price, current_price, quantity, "limit", "limit")
    return float(cost["net_pnl_usd"]) / (entry_price * quantity) * 100.0


def _update_quick_profit_minute_high(state: RuntimeState, symbol: str, current_price: float, now: datetime) -> float:
    """Approximate "1분봉 고가" for the Quick-Profit take-profit check (MACD2
    parity, 2026-08-04) — mirrors app/trading/macd2/worker.py's function of
    the same name exactly, only KST -> ET. Resets to ``current_price``
    whenever the held symbol changes or a new calendar minute (:00) starts,
    then tracks the running max of the already-polled live quote within that
    minute. Never called when ``state.quick_profit_enabled`` is False."""
    bucket = now.astimezone(ET).replace(second=0, microsecond=0).isoformat()
    if state.quick_profit_minute_symbol != symbol or state.quick_profit_minute_bucket != bucket:
        state.quick_profit_minute_symbol = symbol
        state.quick_profit_minute_bucket = bucket
        state.quick_profit_minute_high = current_price
    else:
        state.quick_profit_minute_high = max(float(state.quick_profit_minute_high or 0.0), current_price)
    return float(state.quick_profit_minute_high)


def _fresh_quote_prices(market_data: MarketDataService, symbols: tuple[str, ...]) -> dict[str, float]:
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
    """New ET trading date -> reset only session-scoped runtime fields. The
    permanent signal ledger is never cleared here."""
    today_str = now.astimezone(ET).strftime("%Y%m%d")
    if state.session_date is None:
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
    state.daily_entry_count = 0
    state.last_entry_at = None
    state.stop_loss_reentry_override_used_today = False
    state.last_stop_loss_exit_at = None
    state.stop_loss_cooldown_direction = None


def initialize_strategy_session(
    state: RuntimeState, market_data: MarketDataService, *, now: Optional[datetime] = None,
    worker_instance_id: Optional[str] = None,
) -> RuntimeState:
    now = now or datetime.now(ET)
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.worker_code_sha = git_sha()
    state.session_started_at = now.isoformat()
    state.worker_instance_id = worker_instance_id
    state.pending_signal = None
    state.last_detected_direction = None
    state.last_executed_direction = None
    state.current_episode_direction = None
    state.processed_signal_ids = []

    df_1m = market_data.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    bars_3m, _dropped = filter_complete_3m_bars(bars_3m, df_1m)
    macd_snap = calculate_macd(bars_3m)
    if macd_snap is not None:
        state.session_baseline_bar_ts = macd_snap.bar_dt.isoformat()
        state.last_evaluated_bar_ts = macd_snap.bar_dt.isoformat()
        # Marks THIS bar as already baseline'd -> the next NEW completed bar
        # is a genuine continuation, and no bar completed before Worker
        # start is ever retroactively ordered (docs §7 Worker 재시작).
        state.last_confirmed_bar_ts = macd_snap.bar_dt.isoformat()
        state.primary_previous_diff = macd_snap.previous_diff
        state.primary_current_diff = macd_snap.current_diff
    else:
        state.session_baseline_bar_ts = None
        state.last_evaluated_bar_ts = None
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
        # docs §4: 전략 보유수량과 계좌 전체 보유수량을 분리한다 — TSLL/TSLZ가
        # 아닌 심볼이나 개인이 보유한 비전략 잔고는 전략 포지션으로 인식하지
        # 않는다.
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
        "runtime_position": runtime, "broker_positions": all_positions,
        f"{config.LONG_SYMBOL}_broker_qty": int((broker_positions.get(config.LONG_SYMBOL) or {}).get("qty") or 0),
        f"{config.INVERSE_SYMBOL}_broker_qty": int((broker_positions.get(config.INVERSE_SYMBOL) or {}).get("qty") or 0),
        "strategy_owned_qty": int(state.strategy_owned_qty or 0),
        "broker_error": broker_error,
    }
    state.last_position_reconcile_at = now.isoformat()

    if broker_error is not None:
        diag["comparison_result"] = POSITION_DATA_ERROR
        state.position_reconcile_diag = diag
        return POSITION_DATA_ERROR

    if runtime["symbol"] is None:
        if not broker_positions:
            state.account_holding_qty = 0
            state.strategy_owned_qty = 0
            diag["comparison_result"] = MATCH_FLAT
            state.position_reconcile_diag = diag
            return MATCH_FLAT
        diag["comparison_result"] = order_executor.STRATEGY_OWNERSHIP_MISMATCH
        state.position_reconcile_diag = diag
        return order_executor.STRATEGY_OWNERSHIP_MISMATCH

    broker_qty = int((broker_positions.get(runtime["symbol"]) or {}).get("qty") or 0)
    state.account_holding_qty = broker_qty
    strategy_qty = int(state.strategy_owned_qty or 0)
    if strategy_qty <= 0:
        strategy_qty = int(runtime["qty"])
        state.strategy_owned_qty = strategy_qty
        state.strategy_average_price = float(runtime["avg_price"] or 0.0)
    if broker_qty == int(runtime["qty"]) == strategy_qty:
        diag["comparison_result"] = MATCH_POSITION
        state.position_reconcile_diag = diag
        return MATCH_POSITION
    if broker_qty == 0:
        state.position = None
        state.account_holding_qty = 0
        state.strategy_owned_qty = 0
        state.strategy_average_price = 0.0
        diag["comparison_result"] = RECOVERED_TO_FLAT
        state.position_reconcile_diag = diag
        return RECOVERED_TO_FLAT
    if broker_qty != strategy_qty:
        diag["comparison_result"] = order_executor.STRATEGY_OWNERSHIP_MISMATCH
        state.position_reconcile_diag = diag
        return order_executor.STRATEGY_OWNERSHIP_MISMATCH
    diag["comparison_result"] = POSITION_MISMATCH
    state.position_reconcile_diag = diag
    return POSITION_MISMATCH


def _direction_for_symbol(symbol: Optional[str]) -> Optional[Direction]:
    if not symbol:
        return None
    if symbol == config.LONG_SYMBOL:
        return Direction.UP_RED
    if symbol == config.INVERSE_SYMBOL:
        return Direction.DOWN_BLUE
    return None


def _position_direction(position: Optional[PositionSnapshot]) -> Optional[Direction]:
    if position is None or int(position.quantity or 0) <= 0:
        return None
    return _direction_for_symbol(position.symbol)


def _parse_iso_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _advance_confirmed_primary(state: RuntimeState, macd_snap) -> Direction:
    """Primary (order-authoritative) crossover — completed 3m bars ONLY.
    Evaluated exactly once per new completed-bar timestamp."""
    bar_key = macd_snap.bar_dt.isoformat()
    if state.last_confirmed_bar_ts == bar_key:
        return Direction.HOLD
    state.last_confirmed_bar_ts = bar_key
    flag = evaluate_confirmed_macd_flag(macd_snap, state.last_detected_direction)
    direction = flag.confirmed_flag
    if direction != Direction.HOLD:
        state.last_detected_direction = direction
        state.latest_primary_flag = direction
        state.latest_primary_signal_id = flag.published_signal_id
    return direction


def _compute_regime(bars_3m: pd.DataFrame) -> str:
    """docs §10 — 매 tick 재계산되는 시장 상태(NORMAL/CHOP/UNKNOWN), 방향과
    무관한 변동성 지표만 사용한다(§strong_flag_filter.classify_regime)."""
    work = strong_flag_filter._prepare_bars(bars_3m)
    if work is None:
        return "UNKNOWN"
    _scores, metrics, err = strong_flag_filter.compute_component_scores(work)
    if err or metrics is None:
        return "UNKNOWN"
    return strong_flag_filter.classify_regime(metrics)


def _update_history_diagnostics(state: RuntimeState, df_1m: pd.DataFrame, bars_3m: pd.DataFrame, now: datetime) -> None:
    if df_1m is None or df_1m.empty or "datetime" not in df_1m.columns:
        state.today_1m_bar_count = 0
        state.history_newest_at = None
        state.last_completed_3m_bar_at = None
        return
    work = df_1m.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    if work["datetime"].dt.tz is None:
        state.today_1m_bar_count = 0
        state.history_newest_at = None
        state.last_completed_3m_bar_at = None
        return
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    today = now.astimezone(ET).strftime("%Y%m%d")
    state.today_1m_bar_count = int((work["datetime"].dt.tz_convert(ET).dt.strftime("%Y%m%d") == today).sum())
    state.history_newest_at = work["datetime"].iloc[-1].isoformat() if not work.empty else None
    if bars_3m is not None and not bars_3m.empty:
        state.last_completed_3m_bar_at = pd.Timestamp(bars_3m["datetime"].iloc[-1]).isoformat()
    else:
        state.last_completed_3m_bar_at = None


def _judge_strong_flag(*, state: RuntimeState, bars_3m, direction: Direction, position, now: datetime, signal_id: str):
    position_direction = _position_direction(position)
    last_entry_at = _parse_iso_dt(state.last_entry_at)
    last_exit_at = _parse_iso_dt(state.last_exit_at) if state.last_exit_direction == direction else None
    daily_count = int(state.daily_entry_count or 0)
    decision = strong_flag_filter.evaluate_strong_flag(bars_3m, direction, position_direction, last_entry_at, daily_count, now)
    decision = strong_flag_filter.apply_trade_gates(
        decision, flag_direction=direction, position_direction=position_direction, last_entry_at=last_entry_at,
        last_same_direction_exit_at=last_exit_at, daily_entry_count=daily_count, now=now,
        daily_max_entries=strong_flag_filter.daily_max_entries_for(state.market_regime),
    )
    state.strong_filter_version = config.STRONG_FILTER_VERSION
    state.last_score = float(decision.score)
    state.last_required_score = float(decision.required_score)
    state.last_approved = bool(decision.approved)
    state.last_decision = decision.decision
    state.last_block_reason = decision.block_reason
    state.last_is_reversal = bool(decision.is_reversal)
    state.last_fast_reversal = bool(decision.fast_reversal)
    state.last_component_scores = dict(decision.component_scores or {})
    state.last_metrics = dict(decision.metrics or {})
    state.last_signal_id = signal_id
    return decision


def _check_stop_loss_cooldown(state: RuntimeState, direction: Direction, now: datetime, bars_3m) -> tuple[bool, Optional[float]]:
    """docs §12 (신규): 손절 후 같은 방향 15분 재진입 금지. 그 이후 새
    LIVE_CONFIRMED 플래그이며 score>=max(85, 그 시점 문턱)이면 하루 1회만
    같은 방향 재진입 허용. Returns (blocked, achieved_score_or_None)."""
    if state.stop_loss_cooldown_direction != direction or not state.last_stop_loss_exit_at:
        return False, None
    exit_at = _parse_iso_dt(state.last_stop_loss_exit_at)
    if exit_at is None:
        return False, None
    elapsed_min = (now - exit_at).total_seconds() / 60.0
    if elapsed_min >= config.STOP_LOSS_REENTRY_COOLDOWN_MIN:
        return False, None
    # 쿨다운 구간 내 — 하루 1회 예외를 이미 썼으면 그냥 차단.
    if state.stop_loss_reentry_override_used_today:
        return True, None
    work = strong_flag_filter._prepare_bars(bars_3m)
    if work is None:
        return True, None
    scores_t, metrics_t, err = strong_flag_filter.compute_component_scores(work)
    if err or scores_t is None or metrics_t is None:
        return True, None
    scores, _metrics = strong_flag_filter.score_for_direction(scores_t, metrics_t, direction)
    total = float(sum(scores.values()))
    regime = strong_flag_filter.classify_regime(_metrics)
    thresholds = strong_flag_filter.required_scores_for(now_et=now.astimezone(ET), regime=regime, daily_filled_entry_count=int(state.daily_entry_count or 0))
    override_floor = max(config.STOP_LOSS_REENTRY_OVERRIDE_SCORE_FLOOR, thresholds["entry"])
    if total >= override_floor:
        return False, total  # allowed via override this one time (caller marks it used)
    return True, total


def _quote_ages(market_data: MarketDataService, symbols: tuple[str, ...]) -> dict[str, Optional[float]]:
    ages: dict[str, Optional[float]] = {}
    for symbol in symbols:
        snap = market_data.get_quote(symbol)
        ages[symbol] = snap.age_sec if snap is not None else None
    return ages


def _quote_status_for_order(market_data: MarketDataService, symbols: tuple[str, ...]) -> tuple[str, dict[str, float]]:
    prices = _fresh_quote_prices(market_data, symbols)
    if all(s in prices for s in symbols):
        return "READY", prices
    return "NOT_READY", prices


def _required_quote_symbols(direction: Direction, position: Optional[PositionSnapshot]) -> tuple[str, ...]:
    target = order_executor.target_symbol_for_direction(direction)
    symbols = {config.SIGNAL_SYMBOL}
    if target:
        symbols.add(target)
    if position is not None and position.symbol:
        symbols.add(position.symbol)
    return tuple(sorted(symbols))


def _has_order_request(outcome) -> bool:
    return bool(outcome.timestamps.get("buy_requested_at") or outcome.timestamps.get("sell_requested_at"))


def _execute_or_wait(
    *, broker, market_data: MarketDataService, state: RuntimeState, now: datetime, macd_snap, direction: Direction,
    signal_id: str, signal_type: str, position: Optional[PositionSnapshot], result: TickResult,
    signal_detected_at: Optional[datetime] = None,
):
    order_started = time.monotonic()
    result.signal_dispatch_trace = {
        "signal_id": signal_id, "direction": direction.value, "signal_type": signal_type,
        "completed_bar_at": macd_snap.bar_dt.isoformat(), "order_executor_called": False, "broker_called": False,
        "display_at": macd_snap.bar_dt.isoformat(), "order_due_at": (macd_snap.bar_dt + timedelta(minutes=3)).isoformat(),
        "final_block_reason": None,
    }
    reconcile = reconcile_position_state(broker, state, now, force=True)
    result.signal_dispatch_trace["position_reconcile_result"] = reconcile
    if reconcile in (RECOVERED_FROM_BROKER, POSITION_DATA_ERROR, POSITION_MISMATCH, order_executor.STRATEGY_OWNERSHIP_MISMATCH):
        state.order_block_reason = reconcile
        result.signal_dispatch_trace["final_block_reason"] = reconcile
        result.skipped = reconcile
        result.timing["order_execution"] = time.monotonic() - order_started
        return None

    required_symbols = _required_quote_symbols(direction, position)
    quote_status, quotes = _quote_status_for_order(market_data, required_symbols)
    detected_at = signal_detected_at or now
    result.signal_dispatch_trace["quote_ages"] = _quote_ages(market_data, required_symbols)
    result.signal_dispatch_trace["quote_status"] = quote_status
    if direction == Direction.DOWN_BLUE and config.INVERSE_SYMBOL not in quotes:
        state.order_block_reason = config.TSLZ_EXCHANGE_UNRESOLVED
        result.signal_dispatch_trace["final_block_reason"] = config.TSLZ_EXCHANGE_UNRESOLVED
        state.pending_signal = None
        result.skipped = config.TSLZ_EXCHANGE_UNRESOLVED
        result.timing["order_execution"] = time.monotonic() - order_started
        return None

    retry_count = 0
    while quote_status != "READY":
        elapsed = (datetime.now(ET) - detected_at).total_seconds()
        if elapsed >= config.QUOTE_STALE_MAX_WAIT_SEC or retry_count >= config.QUOTE_STALE_RETRY_MAX_ATTEMPTS:
            break
        market_data.refresh_quotes(symbols=required_symbols)
        time.sleep(config.QUOTE_STALE_RETRY_INTERVAL_SEC)
        retry_count += 1
        quote_status, quotes = _quote_status_for_order(market_data, required_symbols)

    state.last_quote_stale_retry_count = retry_count
    if quote_status != "READY":
        state.order_block_reason = config.MISSED_SIGNAL_QUOTE_STALE
        state.last_quote_stale_result = config.MISSED_SIGNAL_QUOTE_STALE
        result.signal_dispatch_trace["final_block_reason"] = config.MISSED_SIGNAL_QUOTE_STALE
        state.pending_signal = None
        result.skipped = config.MISSED_SIGNAL_QUOTE_STALE
        result.timing["order_execution"] = time.monotonic() - order_started
        return None
    state.last_quote_stale_result = "RECOVERED" if retry_count > 0 else None

    result.signal_dispatch_trace["order_executor_called"] = True
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id, quotes=quotes, position=position,
        budget_usd=state.budget_usd, processed_signal_ids=frozenset(state.processed_signal_ids),
        strategy_owned_qty=state.strategy_owned_qty if position is not None else None,
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
        market_state=market_session.get_us_market_state(now),
    )
    if outcome is None:
        state.order_block_reason = SIGNAL_NOT_DISPATCHED
        result.skipped = SIGNAL_NOT_DISPATCHED
        result.timing["order_execution"] = time.monotonic() - order_started
        return None
    result.order_requested_at = outcome.timestamps.get("sell_requested_at") or outcome.timestamps.get("buy_requested_at")
    result.signal_dispatch_trace["order_requested_at"] = result.order_requested_at or ""
    result.signal_dispatch_trace["broker_called"] = outcome.broker_called
    result.signal_dispatch_trace["final_block_reason"] = outcome.block_reason
    if _has_order_request(outcome) and outcome.signal_id and outcome.signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [outcome.signal_id]
    state.order_block_reason = outcome.block_reason
    result.timing["order_execution"] = time.monotonic() - order_started
    return outcome


def _record_major_filtered_signal(*, state, macd_snap, direction, signal_type, signal_id, decision, detected_at, result):
    block_reason = decision.block_reason or decision.decision or config.FILTERED_OUT
    state.order_block_reason = block_reason
    if signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
    result.signal_dispatch_trace = {
        "signal_id": signal_id, "direction": direction.value, "signal_type": signal_type,
        "order_executor_called": False, "broker_called": False, "final_block_reason": block_reason,
        "order_result_override": config.FILTERED_OUT,
    }
    outcome = order_executor.ExecutionOutcome(
        signal_id=signal_id, direction=direction, target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED, block_reason=block_reason,
    )
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, detected_at, outcome, result.signal_dispatch_trace, strong_decision=decision)
    return outcome


def _record_blocked_signal(*, state, macd_snap, direction, signal_type, reason, result, extra_trace=None, now=None):
    signal_id = make_signal_id(macd_snap.bar_dt, direction)
    if signal_id in state.processed_signal_ids:
        return
    state.order_block_reason = reason
    detected_at = (now or datetime.now(ET)).astimezone(ET)
    result.signal_detected_at = detected_at.isoformat()
    trace = {"signal_id": signal_id, "direction": direction.value, "signal_type": signal_type, "final_block_reason": reason}
    if extra_trace:
        trace.update(extra_trace)
    result.signal_dispatch_trace = trace
    outcome = order_executor.ExecutionOutcome(
        signal_id=signal_id, direction=direction, target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED, block_reason=reason,
    )
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, detected_at, outcome, trace)


def _dispatch_confirmed_signal(
    *, broker, market_data, state, now, macd_snap, direction, signal_type, position, result, bars_3m=None,
):
    bar_end = macd_snap.bar_dt + timedelta(minutes=3)
    if now.astimezone(ET) < bar_end.astimezone(ET):
        state.order_block_reason = "WAITING_FOR_3M_CONFIRMATION"
        return None

    signal_id = make_signal_id(macd_snap.bar_dt, direction)
    if signal_id in state.processed_signal_ids:
        state.order_block_reason = order_executor.BLOCK_DUPLICATE_SIGNAL
        return None
    if state.pending_signal and state.pending_signal.get("signal_id") == signal_id:
        return None

    state.current_episode_direction = direction
    signal_detected_at = now.astimezone(ET)
    result.signal_detected_at = signal_detected_at.isoformat()

    # (신규) 손절 재진입 쿨다운 — 2026-08-04 MACD2 parity 수정: MACD2에는 이런
    # 손절 재진입 쿨다운이 전혀 없다(필터가 꺼져 있으면 확정 플래그가 곧바로
    # 매매로 이어짐). strong_filter_enabled가 꺼져 있는 기본 상태에서는 이
    # 쿨다운도 함께 꺼서 손절 규칙이 MACD2와 완전히 동일하게 동작하도록 하고,
    # strong_filter_enabled를 다시 켠 사용자에게는 기존의 안전장치(쿨다운
    # 포함)를 그대로 유지한다.
    used_override = False
    decision = None
    if state.strong_filter_enabled:
        cooldown_blocked, achieved_score = _check_stop_loss_cooldown(state, direction, now, bars_3m)
        if cooldown_blocked:
            last_stop_loss_at = _parse_iso_dt(state.last_stop_loss_exit_at)
            cooldown_end_at = (
                last_stop_loss_at + timedelta(minutes=config.STOP_LOSS_REENTRY_COOLDOWN_MIN)
                if last_stop_loss_at is not None else None
            )
            elapsed_min = (
                (now - last_stop_loss_at).total_seconds() / 60.0
                if last_stop_loss_at is not None else None
            )
            reason = (
                config.STOP_LOSS_REENTRY_OVERRIDE_USED_TODAY
                if state.stop_loss_reentry_override_used_today
                else config.STOP_LOSS_REENTRY_COOLDOWN_BLOCK
            )
            return _record_blocked_signal(
                state=state, macd_snap=macd_snap, direction=direction, signal_type=signal_type, reason=reason, result=result,
                extra_trace={
                    "stop_loss_reentry_cooldown_active": True,
                    "last_stop_loss_at": last_stop_loss_at.isoformat() if last_stop_loss_at is not None else "",
                    "cooldown_end_at": cooldown_end_at.isoformat() if cooldown_end_at is not None else "",
                    "elapsed_minutes_after_stop_loss": round(float(elapsed_min), 6) if elapsed_min is not None else "",
                },
                now=now,
            )
        used_override = achieved_score is not None

        decision = _judge_strong_flag(state=state, bars_3m=bars_3m, direction=direction, position=position, now=now, signal_id=signal_id)
        if not decision.approved:
            return _record_major_filtered_signal(
                state=state, macd_snap=macd_snap, direction=direction, signal_type=signal_type, signal_id=signal_id,
                decision=decision, detected_at=signal_detected_at, result=result,
            )

    if used_override:
        state.stop_loss_reentry_override_used_today = True

    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap, direction=direction,
        signal_id=signal_id, signal_type=signal_type, position=position, result=result, signal_detected_at=signal_detected_at,
    )
    if outcome is not None:
        result.signal_dispatch_trace["stop_loss_reentry_override_used"] = used_override
        _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace, strong_decision=decision)
    return outcome


def _is_filtered(outcome) -> bool:
    return bool(outcome is not None and outcome.block_reason in (
        config.STRONG_SCORE_BELOW_THRESHOLD, config.STRONG_PRICE_CONFIRMATION_FAILED, config.STRONG_SIDEWAYS_BLOCK,
        config.STRONG_PROFILE_FAILED, config.STRONG_SAME_DIRECTION_COOLDOWN, config.STRONG_MIN_HOLD_BLOCK, "DAILY_ENTRY_LIMIT",
        config.SAME_DIRECTION_POSITION_HELD,
    ) and outcome.final_state == SignalState.BLOCKED and not outcome.broker_called)


def _apply_exit_outcome(state: RuntimeState, outcome, *, exit_reason: str, now: Optional[datetime] = None) -> None:
    _record_broker_order_result(state, outcome)
    if outcome.final_state == SignalState.EXECUTED:
        exited_symbol = outcome.target_symbol or (outcome.sell_result.symbol if outcome.sell_result else None)
        exited_direction = _direction_for_symbol(exited_symbol)
        state.position = None
        state.account_holding_qty = 0
        state.strategy_owned_qty = 0
        state.strategy_average_price = 0.0
        state.peak_net_return = 0.0
        state.profit_lock_active = False
        state.quick_profit_minute_symbol = None
        state.quick_profit_minute_bucket = None
        state.quick_profit_minute_high = None
        event_at = (now or datetime.now(ET)).astimezone(ET)
        state.last_exit_at = event_at.isoformat()
        state.last_exit_direction = exited_direction
        if exit_reason == config.EXIT_STOP_LOSS and exited_direction is not None:
            state.last_stop_loss_exit_at = event_at.isoformat()
            state.stop_loss_cooldown_direction = exited_direction
            state.stop_loss_reentry_override_used_today = False
    state.order_block_reason = outcome.block_reason


def _apply_switch_outcome(state: RuntimeState, outcome, pattern: Direction, *, now: Optional[datetime] = None) -> None:
    event_at = (now or datetime.now(ET)).astimezone(ET)
    if outcome.final_state == SignalState.EXECUTED:
        state.position = PositionSnapshot(
            symbol=outcome.target_symbol, quantity=outcome.quantity,
            avg_price=(outcome.filled_avg_price or (outcome.buy_result.executed_price if outcome.buy_result else 0.0)),
            entry_at=event_at,
        )
        state.account_holding_qty = int(outcome.quantity or 0)
        state.strategy_owned_qty = int(outcome.quantity or 0)
        state.strategy_average_price = float(state.position.avg_price or 0.0)
        if outcome.buy_result and outcome.buy_result.order_id:
            state.strategy_order_ids = list(state.strategy_order_ids or []) + [outcome.buy_result.order_id]
        state.last_signal_direction = pattern
        state.last_executed_direction = pattern
        state.peak_net_return = 0.0
        state.profit_lock_active = False
        state.quick_profit_minute_symbol = None
        state.quick_profit_minute_bucket = None
        state.quick_profit_minute_high = None
        state.last_entry_at = event_at.isoformat()
        # docs §10: daily_entry_count는 실제 filled_qty>0인 신규 매수 체결
        # 횟수만 — 반대 전환 후 신규 BUY도 1회로 포함, 주문거절·미체결은
        # 증가하지 않는다.
        filled_qty = int(outcome.quantity or 0) or int((outcome.buy_result.executed_qty if outcome.buy_result else 0) or 0)
        if filled_qty > 0:
            state.daily_entry_count = int(state.daily_entry_count or 0) + 1
    elif outcome.sell_result is not None and outcome.sell_result.success and outcome.sell_qty_after == 0:
        exited_symbol = outcome.sell_result.symbol
        state.position = None
        state.account_holding_qty = 0
        state.strategy_owned_qty = 0
        state.strategy_average_price = 0.0
        state.peak_net_return = 0.0
        state.profit_lock_active = False
        state.last_exit_at = event_at.isoformat()
        state.last_exit_direction = _direction_for_symbol(exited_symbol)
    if _has_order_request(outcome) and outcome.signal_id and outcome.signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [outcome.signal_id]
    state.order_block_reason = outcome.block_reason


def _record_broker_order_result(state: RuntimeState, outcome) -> None:
    result = outcome.buy_result or outcome.sell_result
    if result is None:
        return
    state.last_broker_order_id = result.order_id
    state.last_broker_order_result = "OK" if result.success else (outcome.order_failure_stage or outcome.block_reason or "ORDER_FAILED")
    state.last_broker_order_symbol = result.symbol
    state.last_broker_order_side = result.side
    state.last_broker_order_at = datetime.now(ET).isoformat()


def _cancel_open_buy_orders_if_supported(broker, state: RuntimeState) -> None:
    get_open_orders = getattr(broker, "get_open_orders", None)
    cancel_order = getattr(broker, "cancel_order", None)
    if get_open_orders is None or cancel_order is None:
        return
    for order in get_open_orders() or []:
        side = str(getattr(order, "side", "") or getattr(order, "ord_dvsn", "") or "").upper()
        symbol = str(getattr(order, "symbol", "") or "")
        order_id = str(getattr(order, "order_id", "") or getattr(order, "odno", "") or "")
        if side == "BUY" and symbol in config.TRADE_SYMBOLS and order_id:
            cancel_order(order_id, symbol)
            state.liquidation_status.setdefault("cancelled_buy_orders", []).append({"symbol": symbol, "order_id": order_id})


def _force_liquidate_managed_positions(*, broker, state: RuntimeState, now: datetime, result: TickResult) -> TickResult:
    session_key = now.astimezone(ET).strftime("%Y%m%d")
    status = dict(state.liquidation_status or {})
    if status.get("session_date") != session_key:
        status = {"session_date": session_key, "symbols": {}, "complete": False}
    state.liquidation_status = status
    _cancel_open_buy_orders_if_supported(broker, state)
    raw_positions = broker.get_positions() if hasattr(broker, "get_positions") else []
    targets = []
    for p in raw_positions or []:
        symbol = str(getattr(p, "symbol", "") or "")
        qty = int(float(getattr(p, "quantity", 0) or 0))
        avg = float(getattr(p, "avg_price", 0.0) or 0.0)
        if symbol in config.TRADE_SYMBOLS and qty > 0:
            targets.append((symbol, qty, avg))
    if not targets:
        status["complete"] = True
        status["message"] = "강제청산 완료 - 보유수량 0"
        state.position = None
        state.account_holding_qty = 0
        state.strategy_owned_qty = 0
        result.actions.append("FORCE_LIQUIDATION_COMPLETE:FLAT")
        return result
    for symbol, qty, avg in targets:
        symbol_status = status.setdefault("symbols", {}).setdefault(symbol, {})
        if symbol_status.get("state") == "FLAT":
            continue
        symbol_status["state"] = "SELL_SUBMITTED"
        outcome = order_executor.execute_exit(
            broker=broker, symbol=symbol, quantity=qty, exit_reason=config.EXIT_FORCED_LIQUIDATION,
            entry_price=avg, strategy_owned_qty=qty,
            reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
        )
        _record_broker_order_result(state, outcome)
        remaining = broker.reconcile_position(symbol) if hasattr(broker, "reconcile_position") else outcome.sell_qty_after
        symbol_status["remaining_qty"] = int(remaining or 0)
        symbol_status["state"] = "FLAT" if int(remaining or 0) == 0 else "FAILED"
        symbol_status["last_reason"] = outcome.block_reason or outcome.final_state.value
        result.actions.append(f"FORCED_LIQUIDATION:{symbol}")
    if all((v or {}).get("state") == "FLAT" for v in status.get("symbols", {}).values()):
        status["complete"] = True
        status["message"] = "강제청산 완료 - 보유수량 0"
        state.position = None
        state.account_holding_qty = 0
        state.strategy_owned_qty = 0
        state.strategy_average_price = 0.0
    return result


def _managed_liquidation_symbols() -> set[str]:
    return {str(s).upper() for s in getattr(config, "MANAGED_LIQUIDATION_SYMBOLS", config.TRADE_SYMBOLS)}


def _cancel_open_buy_orders_if_supported(broker, state: RuntimeState, managed_symbols: set[str]) -> tuple[bool, str]:  # type: ignore[no-redef]
    get_open_orders = getattr(broker, "get_open_orders", None)
    cancel_order = getattr(broker, "cancel_order", None)
    if get_open_orders is None or cancel_order is None:
        return False, "OPEN_ORDER_CANCEL_UNSUPPORTED"
    state.liquidation_status.setdefault("cancelled_buy_orders", [])
    state.liquidation_status["cancel_state"] = "CANCELING_ORDERS"
    try:
        open_orders = get_open_orders() or []
    except Exception as exc:
        state.liquidation_status["cancel_state"] = "FAILED"
        state.liquidation_status["failure_reason"] = f"OPEN_ORDER_QUERY_FAILED:{exc}"
        return False, state.liquidation_status["failure_reason"]
    for order in open_orders:
        side = str(getattr(order, "side", "") or getattr(order, "ord_dvsn", "") or "").upper()
        symbol = str(getattr(order, "symbol", "") or "").upper()
        order_id = str(getattr(order, "order_id", "") or getattr(order, "odno", "") or "")
        if side == "BUY" and symbol in managed_symbols and order_id:
            try:
                cancel_result = cancel_order(order_id, symbol)
            except Exception as exc:
                state.liquidation_status["cancel_state"] = "FAILED"
                state.liquidation_status["failure_reason"] = f"OPEN_BUY_CANCEL_FAILED:{symbol}:{order_id}:{exc}"
                return False, state.liquidation_status["failure_reason"]
            if not getattr(cancel_result, "success", False):
                state.liquidation_status["cancel_state"] = "FAILED"
                state.liquidation_status["failure_reason"] = (
                    f"OPEN_BUY_CANCEL_FAILED:{symbol}:{order_id}:{getattr(cancel_result, 'message', '')}"
                )
                return False, state.liquidation_status["failure_reason"]
            state.liquidation_status["cancelled_buy_orders"].append({"symbol": symbol, "order_id": order_id})
    state.liquidation_status["cancel_state"] = "DONE"
    return True, ""


def _force_liquidate_managed_positions(*, broker, state: RuntimeState, now: datetime, result: TickResult) -> TickResult:  # type: ignore[no-redef]
    session_key = now.astimezone(ET).strftime("%Y%m%d")
    status = dict(state.liquidation_status or {})
    if status.get("session_date") != session_key:
        status = {"session_date": session_key, "symbols": {}, "complete": False}
    state.liquidation_status = status
    managed_symbols = _managed_liquidation_symbols()
    account_scope = str(getattr(broker, "account_id", "") or getattr(broker, "mode", "") or "unknown")
    cancel_ok, cancel_reason = _cancel_open_buy_orders_if_supported(broker, state, managed_symbols)
    if not cancel_ok:
        status["complete"] = False
        status["failure_reason"] = cancel_reason
        result.actions.append(f"FORCE_LIQUIDATION_FAILED:{cancel_reason}")
        return result

    raw_positions = broker.get_positions() if hasattr(broker, "get_positions") else []
    targets: list[tuple[str, int, float]] = []
    for p in raw_positions or []:
        symbol = str(getattr(p, "symbol", "") or "").upper()
        qty = int(float(getattr(p, "quantity", 0) or 0))
        avg = float(getattr(p, "avg_price", 0.0) or 0.0)
        if symbol in managed_symbols and qty > 0:
            targets.append((symbol, qty, avg))
            status.setdefault("symbols", {}).setdefault(symbol, {}).setdefault("state", "READY")
    status["target_count"] = len(targets)
    if not targets:
        status["complete"] = True
        status["completed_count"] = len([v for v in status.get("symbols", {}).values() if (v or {}).get("state") == "FLAT"])
        status["remaining_symbols"] = []
        status["message"] = "강제청산 완료 - 보유수량 0"
        state.position = None
        state.account_holding_qty = 0
        state.strategy_owned_qty = 0
        result.actions.append("FORCE_LIQUIDATION_COMPLETE:FLAT")
        return result

    for symbol, qty, _avg in targets:
        symbol_status = status.setdefault("symbols", {}).setdefault(symbol, {})
        if symbol_status.get("state") == "FLAT":
            continue
        remaining = int((broker.reconcile_position(symbol) if hasattr(broker, "reconcile_position") else qty) or 0)
        if remaining <= 0:
            symbol_status["state"] = "FLAT"
            symbol_status["remaining_qty"] = 0
            continue
        idempotency_key = f"{session_key}:{account_scope}:{symbol}:FORCE_LIQUIDATION"
        symbol_status["idempotency_key"] = idempotency_key
        attempts = int(symbol_status.get("attempts") or 0)
        max_retries = int(config.US_LIQUIDATION_MAX_RETRIES)
        while remaining > 0 and attempts < max_retries:
            if symbol_status.get("state") == "SELL_SUBMITTED" and int(symbol_status.get("inflight_qty") or 0) == remaining:
                symbol_status["remaining_qty"] = remaining
                symbol_status["last_reason"] = "DUPLICATE_INFLIGHT_SKIPPED"
                break
            attempts += 1
            symbol_status["attempts"] = attempts
            symbol_status["state"] = "SELL_SUBMITTED"
            symbol_status["inflight_qty"] = remaining
            outcome = order_executor.execute_exit(
                broker=broker, symbol=symbol, quantity=remaining, exit_reason=config.EXIT_FORCED_LIQUIDATION,
                entry_price=_avg, strategy_owned_qty=None,
                reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
            )
            _record_broker_order_result(state, outcome)
            sell_result = outcome.sell_result
            result.actions.append(f"FORCED_LIQUIDATION:{symbol}")
            if sell_result is None or not sell_result.success or outcome.final_state != SignalState.EXECUTED:
                symbol_status["last_reason"] = outcome.block_reason or (sell_result.message if sell_result else "SELL_FAILED")
                break
            remaining = int(outcome.sell_qty_after if outcome.sell_qty_after is not None else 0)
            symbol_status["remaining_qty"] = remaining
            symbol_status["last_executed_qty"] = int(sell_result.executed_qty or 0)
            symbol_status["last_order_id"] = sell_result.order_id
            if remaining <= 0:
                symbol_status["state"] = "FLAT"
                symbol_status["inflight_qty"] = 0
                break
            symbol_status["state"] = "PARTIAL"
            symbol_status["last_reason"] = "PARTIAL_FILL"
        if remaining > 0 and symbol_status.get("state") != "SELL_SUBMITTED":
            symbol_status["state"] = "FAILED"
            symbol_status["remaining_qty"] = remaining
            symbol_status["last_reason"] = symbol_status.get("last_reason") or "MAX_RETRIES_EXCEEDED"

    symbols_state = status.get("symbols", {})
    completed = [sym for sym, meta in symbols_state.items() if (meta or {}).get("state") == "FLAT"]
    remaining_symbols = [sym for sym, meta in symbols_state.items() if int((meta or {}).get("remaining_qty") or 0) > 0]
    status["completed_count"] = len(completed)
    status["remaining_symbols"] = remaining_symbols
    if targets and len(completed) >= len(targets) and not remaining_symbols:
        status["complete"] = True
        status["message"] = "강제청산 완료 - 보유수량 0"
        state.position = None
        state.account_holding_qty = 0
        state.strategy_owned_qty = 0
        state.strategy_average_price = 0.0
    else:
        status["complete"] = False
    return result


def _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, detected_at, outcome, dispatch_trace=None, *, strong_decision=None) -> None:
    order_result = outcome.final_state.value if outcome is not None else SignalState.WAITING.value
    block_reason = outcome.block_reason or "" if outcome is not None else (state.order_block_reason or "WAITING")
    trading_date = macd_snap.bar_dt.astimezone(ET).strftime("%Y%m%d")
    trace = dict(dispatch_trace or {})
    order_result = str(trace.get("order_result_override") or order_result)
    bar_start = macd_snap.bar_dt
    bar_end = bar_start + timedelta(minutes=3)
    bar_start_tz = market_session.dual_timezone_iso(bar_start)
    bar_end_tz = market_session.dual_timezone_iso(bar_end)
    detected_tz = market_session.dual_timezone_iso(detected_at)
    order_requested_raw = outcome.timestamps.get("buy_requested_at") or outcome.timestamps.get("sell_requested_at") if outcome is not None else None
    order_requested_tz = market_session.dual_timezone_iso(datetime.fromisoformat(order_requested_raw)) if order_requested_raw else {"et": "", "kst": ""}
    evaluated_tz = market_session.dual_timezone_iso(datetime.now(ET))

    row = {
        "trading_date": trading_date,
        "completed_bar_at": bar_start.astimezone(ET).strftime("%H%M%S"),
        "signal_id": signal_id, "signal_type": signal_type, "direction": direction.value,
        "origin": config.ORIGIN_LIVE_CONFIRMED,  # run_once dispatch is always post-baseline/live
        "macd": macd_snap.macd, "signal": macd_snap.signal, "hist_last3": str(macd_snap.hist_last3),
        "bar_start_at_et": bar_start_tz["et"], "bar_start_at_kst": bar_start_tz["kst"],
        "bar_end_at_et": bar_end_tz["et"], "bar_end_at_kst": bar_end_tz["kst"],
        "evaluated_at_et": evaluated_tz["et"], "evaluated_at_kst": evaluated_tz["kst"],
        "detected_at_et": detected_tz["et"], "detected_at_kst": detected_tz["kst"],
        "order_requested_at_et": order_requested_tz["et"], "order_requested_at_kst": order_requested_tz["kst"],
        "order_result": order_result, "block_reason": block_reason,
        "strategy_name": config.STRATEGY_NAME, "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE, "worker_code_sha": git_sha(),
        "worker_instance_id": state.worker_instance_id or "", "session_started_at": state.session_started_at or "",
        "previous_macd": macd_snap.previous_macd if macd_snap.previous_macd is not None else "",
        "previous_signal": macd_snap.previous_signal if macd_snap.previous_signal is not None else "",
        "previous_diff": macd_snap.previous_diff if macd_snap.previous_diff is not None else "",
        "confirmed_macd": macd_snap.macd, "confirmed_signal": macd_snap.signal,
        "confirmed_diff": macd_snap.current_diff, "confirmed_direction": direction.value,
        "quote_ages": str(trace.get("quote_ages") or {}), "position_reconcile": trace.get("position_reconcile_result") or "",
        "executor_called": trace.get("order_executor_called"), "broker_called": trace.get("broker_called"),
        "broker_order_id": outcome.buy_result.order_id if outcome and outcome.buy_result else (outcome.sell_result.order_id if outcome and outcome.sell_result else ""),
        "broker_rt_cd": outcome.rt_cd or "" if outcome else "", "broker_msg_cd": outcome.msg_cd or "" if outcome else "",
        "broker_msg1": outcome.msg1 or "" if outcome else "",
        "available_usd": outcome.available_usd if outcome and outcome.available_usd is not None else "",
        "usable_usd": outcome.usable_usd if outcome and outcome.usable_usd is not None else "",
        "bid1": outcome.bid1 if outcome and outcome.bid1 is not None else "",
        "ask1": outcome.ask1 if outcome and outcome.ask1 is not None else "",
        "order_price": outcome.order_price if outcome and outcome.order_price is not None else "",
        "budget_qty": outcome.budget_qty if outcome and outcome.budget_qty is not None else "",
        "available_qty": outcome.available_qty if outcome and outcome.available_qty is not None else "",
        "final_qty": outcome.final_qty if outcome and outcome.final_qty is not None else "",
        "expected_notional_usd": outcome.expected_notional_usd if outcome and outcome.expected_notional_usd is not None else "",
        "expected_fee_usd": outcome.expected_fee_usd if outcome and outcome.expected_fee_usd is not None else "",
        "filled_qty": outcome.filled_qty if outcome and outcome.filled_qty is not None else "",
        "fill_poll_result": outcome.fill_poll_result or "" if outcome else "",
        "balance_qty": outcome.balance_qty if outcome and outcome.balance_qty is not None else "",
        "failure_stage": outcome.order_failure_stage or "" if outcome else "",
        "final_result": order_result if not block_reason else f"{order_result}:{block_reason}",
        "strong_filter_enabled": bool(state.strong_filter_enabled),
        "strong_filter_version": state.strong_filter_version or config.STRONG_FILTER_VERSION,
        "strong_score": strong_decision.score if strong_decision else "",
        "strong_required_score": strong_decision.required_score if strong_decision else "",
        "strong_approved": strong_decision.approved if strong_decision else "",
        "strong_decision": strong_decision.decision if strong_decision else "",
        "strong_block_reason": strong_decision.block_reason if strong_decision else "",
        "strong_is_reversal": strong_decision.is_reversal if strong_decision else "",
        "strong_fast_reversal": strong_decision.fast_reversal if strong_decision else "",
        "strong_component_scores": str(strong_decision.component_scores) if strong_decision else "",
        "strong_metrics": json.dumps(dict(strong_decision.metrics or {}), sort_keys=True) if strong_decision else "",
        "market_regime": state.market_regime, "daily_entry_count": int(state.daily_entry_count or 0),
        "last_entry_at": state.last_entry_at or "",
        "stop_loss_reentry_cooldown_active": trace.get("stop_loss_reentry_cooldown_active", False),
        "stop_loss_reentry_override_used": trace.get("stop_loss_reentry_override_used", False),
        "last_stop_loss_at": trace.get("last_stop_loss_at", ""),
        "cooldown_end_at": trace.get("cooldown_end_at", ""),
        "elapsed_minutes_after_stop_loss": trace.get("elapsed_minutes_after_stop_loss", ""),
    }
    written = ledger.append_signal(row)
    state.last_duplicate_signal_id = None if written else signal_id


def compute_today_signal_overview(df_1m: pd.DataFrame, *, now: datetime, session_started_at: Optional[str]) -> list[dict[str, Any]]:
    """docs §7 — 오늘 전체 신호 재계산(읽기 전용): LIVE_CONFIRMED/
    HISTORICAL_REPLAY_ONLY 분리. order_executor/strong_flag_filter/
    processed_signal_ids에 절대 영향을 주지 않는다."""
    bars_3m = resample_completed_3m(df_1m, now=now)
    bars_3m, _dropped = filter_complete_3m_bars(bars_3m, df_1m)
    if bars_3m.empty:
        return []
    today_str = now.astimezone(ET).strftime("%Y%m%d")
    today_mask = bars_3m["datetime"].dt.tz_convert(ET).dt.strftime("%Y%m%d") == today_str
    today_indices = list(bars_3m.index[today_mask])
    if not today_indices:
        return []
    session_start_dt = _parse_iso_dt(session_started_at)
    overview: list[dict[str, Any]] = []
    last_direction: Optional[Direction] = None
    for pos, idx in enumerate(today_indices):
        window = bars_3m.iloc[: idx + 1]
        snap = calculate_macd(window)
        if snap is None:
            continue
        if pos == 0:
            last_direction = None
            continue
        direction = evaluate_macd_crossover(snap, last_direction)
        if direction == Direction.HOLD:
            continue
        last_direction = direction
        bar_end = snap.bar_dt + timedelta(minutes=3)
        origin = (
            config.ORIGIN_HISTORICAL_REPLAY_ONLY
            if session_start_dt is not None and bar_end <= session_start_dt
            else config.ORIGIN_LIVE_CONFIRMED
        )
        overview.append({
            "signal_id": make_signal_id(snap.bar_dt, direction), "bar_start_at": snap.bar_dt.isoformat(),
            "bar_end_at": bar_end.isoformat(), "display_at": snap.bar_dt.isoformat(),
            "order_due_at": bar_end.isoformat(), "direction": direction.value, "origin": origin,
        })
    return overview


def run_once(*, broker, market_data: MarketDataService, state: RuntimeState, now: Optional[datetime] = None) -> TickResult:
    """One Worker cycle — no pending timers, no queues: same-tick signal->order."""
    now = now or datetime.now(ET)
    result = TickResult()
    tick_started = time.monotonic()

    if not state.auto_trade_on:
        result.skipped = "auto_trade_off"
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

    reconcile = reconcile_position_state(broker, state, now)
    if reconcile in (POSITION_DATA_ERROR, POSITION_MISMATCH, RECOVERED_FROM_BROKER, RECOVERED_TO_FLAT, order_executor.STRATEGY_OWNERSHIP_MISMATCH):
        state.order_block_reason = reconcile
        result.skipped = reconcile
        return result

    quotes = _fresh_quote_prices(market_data, (config.SIGNAL_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL))

    df_1m = market_data.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    # docs §7: 3개 완성 1분봉이 모두 있어야 confirmed 3분봉으로 취급 — 공백
    # 포함 bucket은 HISTORY_GAP으로 그 시점의 평가·필터·주문을 전부 차단한다.
    bars_3m, gap_bar_starts = filter_complete_3m_bars(bars_3m, df_1m)
    _update_history_diagnostics(state, df_1m, bars_3m, now)
    if gap_bar_starts:
        state.order_block_reason = config.HISTORY_GAP
    macd_snap = calculate_macd(bars_3m)
    if macd_snap is None:
        state.warmup_ready = False
        state.ui_mode = RuntimeStatus.BOOTSTRAPPING
        result.skipped = "NOT_READY"
        return result
    state.warmup_ready = True
    state.ui_mode = RuntimeStatus.RUNNING
    state.primary_previous_diff = macd_snap.previous_diff
    state.primary_current_diff = macd_snap.current_diff

    # ── Shadow-only: forming-bar crossover NEVER carries order/stat authority ──
    watch_price = quotes.get(config.SIGNAL_SYMBOL)
    if watch_price is not None:
        primary_result = evaluate_primary_forming_crossover(
            bars_3m, df_1m, now=now, current_price=watch_price, previous_direction=state.last_detected_direction,
        )
        if primary_result.snapshot is not None:
            state.provisional_bar_start = primary_result.snapshot.bar_dt.isoformat()
            state.provisional_bar_end = (primary_result.snapshot.bar_dt + timedelta(minutes=3)).isoformat()
            state.provisional_macd = primary_result.snapshot.macd
            state.provisional_signal = primary_result.snapshot.signal
            state.provisional_diff = primary_result.snapshot.current_diff
        state.provisional_flag = primary_result.direction if primary_result.direction != Direction.HOLD else None
        state.provisional_signal_id = primary_result.signal_id

    state.market_regime = _compute_regime(bars_3m)

    confirmed_direction = _advance_confirmed_primary(state, macd_snap)
    bar_ts_str = macd_snap.bar_dt.isoformat()

    market_state = market_session.get_us_market_state(now)
    state.market_session_state = market_state.to_dict()
    force_liquidate_time = market_state.liquidation_required
    entry_window_open = market_state.entry_allowed
    if not gap_bar_starts:
        state.order_block_reason = None if market_state.entry_allowed else market_state.reason_code

    pos = state.position

    if force_liquidate_time:
        return _force_liquidate_managed_positions(broker=broker, state=state, now=now, result=result)

    if market_state.phase == market_session.USMarketPhase.AFTER_MARKET:
        managed_symbols = _managed_liquidation_symbols()
        raw_positions = broker.get_positions() if hasattr(broker, "get_positions") else []
        remaining = [
            {"symbol": str(getattr(p, "symbol", "") or "").upper(), "quantity": int(float(getattr(p, "quantity", 0) or 0))}
            for p in (raw_positions or [])
            if str(getattr(p, "symbol", "") or "").upper() in managed_symbols and int(float(getattr(p, "quantity", 0) or 0)) > 0
        ]
        if remaining:
            state.liquidation_status = {
                **dict(state.liquidation_status or {}),
                "session_date": now.astimezone(ET).strftime("%Y%m%d"),
                "complete": False,
                "warning": "AFTER_MARKET_UNLIQUIDATED_POSITION",
                "message": "폐장 후 미청산 잔고 발견",
                "remaining_symbols": [row["symbol"] for row in remaining],
                "remaining_positions": remaining,
            }
            result.skipped = "AFTER_MARKET_UNLIQUIDATED_POSITION"
            return result

    if pos is not None and pos.quantity > 0:
        current_price = quotes.get(pos.symbol)
        if current_price is None:
            # MACD2 parity (2026-08-04): STOP_LOSS/Quick-Profit are risk-safety
            # checks on an ALREADY-held position, not a decision to take on new
            # risk, so silently skipping them just because this tick's quote
            # missed the strict freshness window would leave a real position
            # unmonitored. Fall back to the last known price for this symbol
            # (even if stale) so the checks below still run off a real,
            # recent price instead of none.
            stale_snap = market_data.get_quote(pos.symbol)
            if stale_snap is not None and not stale_snap.error and stale_snap.price > 0:
                current_price = stale_snap.price
        profit_lock_should_exit = False

        if current_price is not None:
            net_return = _net_return_pct(pos.avg_price, current_price, pos.quantity)
            exits = risk_exit.evaluate_position_exits(
                current_net_return=net_return, peak_net_return=state.peak_net_return, profit_lock_active=state.profit_lock_active,
            )
            state.peak_net_return = exits.peak_net_return
            state.profit_lock_active = exits.profit_lock_active
            if exits.exit_reason == config.EXIT_STOP_LOSS:
                outcome = order_executor.execute_exit(
                    broker=broker, symbol=pos.symbol, quantity=pos.quantity, exit_reason=config.EXIT_STOP_LOSS,
                    entry_price=pos.avg_price, strategy_owned_qty=state.strategy_owned_qty,
                    reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                )
                _apply_exit_outcome(state, outcome, exit_reason=config.EXIT_STOP_LOSS, now=now)
                result.actions.append(f"STOP_LOSS:{pos.symbol}")
                return result

            # Quick-Profit take-profit filter (MACD2 parity, 2026-08-04) — EXIT
            # LOGIC ONLY, independent of strong_filter_enabled (entry gating is
            # untouched). Always yields to STOP_LOSS (already returned above if
            # it fired this tick). Both the remembered same-minute peak AND the
            # live price must clear the bar, so a spike that has already
            # reversed by execution time can never fire a "take profit" that
            # actually sells at/below entry.
            if state.quick_profit_enabled:
                minute_high = _update_quick_profit_minute_high(state, pos.symbol, current_price, now)
                quick_profit_net_return = _net_return_pct(pos.avg_price, minute_high, pos.quantity)
                current_net_return = _net_return_pct(pos.avg_price, current_price, pos.quantity)
                if (
                    quick_profit_net_return >= config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT
                    and current_net_return >= config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT
                ):
                    outcome = order_executor.execute_exit(
                        broker=broker, symbol=pos.symbol, quantity=pos.quantity, exit_reason=config.EXIT_QUICK_PROFIT_TAKE_PROFIT,
                        entry_price=pos.avg_price, strategy_owned_qty=state.strategy_owned_qty,
                        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                    )
                    _apply_exit_outcome(state, outcome, exit_reason=config.EXIT_QUICK_PROFIT_TAKE_PROFIT, now=now)
                    result.actions.append(f"QUICK_PROFIT_TAKE_PROFIT:{pos.symbol}")
                    return result

            profit_lock_should_exit = exits.exit_reason == config.EXIT_PROFIT_LOCK

        # Pending/opposite confirmed signals get first refusal before Profit Lock.
        if state.pending_signal and not state.pending_signal.get("order_requested"):
            pending_dir = Direction(state.pending_signal["direction"])
            outcome = _execute_or_wait(
                broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap, direction=pending_dir,
                signal_id=str(state.pending_signal["signal_id"]), signal_type=str(state.pending_signal.get("signal_type") or "REVERSAL"),
                position=pos, result=result, signal_detected_at=_parse_iso_dt(state.pending_signal.get("detected_at")),
            )
            if outcome is not None:
                _apply_switch_outcome(state, outcome, pending_dir, now=now)
                result.actions.append(f"OPPOSITE_SIGNAL:{pending_dir.value}")
                state.last_evaluated_bar_ts = bar_ts_str
                return result

        # Priority 3: approved opposite (confirmed) flag switch
        if entry_window_open and confirmed_direction != Direction.HOLD and not gap_bar_starts:
            target = order_executor.target_symbol_for_direction(confirmed_direction)
            if target != pos.symbol:
                outcome = _dispatch_confirmed_signal(
                    broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                    direction=confirmed_direction, signal_type="REVERSAL", position=pos, result=result, bars_3m=bars_3m,
                )
                if _is_filtered(outcome):
                    result.actions.append(f"{config.FILTERED_OUT}:{confirmed_direction.value}")
                elif outcome is not None:
                    _apply_switch_outcome(state, outcome, confirmed_direction, now=now)
                    result.actions.append(f"OPPOSITE_SIGNAL:{confirmed_direction.value}")
                    return result
            else:
                _record_blocked_signal(state=state, macd_snap=macd_snap, direction=confirmed_direction, signal_type="HELD_SAME", reason=order_executor.BLOCK_ALREADY_HOLDING, result=result)

        if profit_lock_should_exit:
            outcome = order_executor.execute_exit(
                broker=broker, symbol=pos.symbol, quantity=pos.quantity, exit_reason=config.EXIT_PROFIT_LOCK,
                entry_price=pos.avg_price, strategy_owned_qty=state.strategy_owned_qty,
                reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
            )
            _apply_exit_outcome(state, outcome, exit_reason=config.EXIT_PROFIT_LOCK, now=now)
            result.actions.append(f"PROFIT_LOCK:{pos.symbol}")
            return result

        state.last_evaluated_bar_ts = bar_ts_str
        return result

    # ── Flat: new-entry evaluation ───────────────────────────────────────
    if state.pending_signal and not state.pending_signal.get("order_requested"):
        pending_dir = Direction(state.pending_signal["direction"])
        outcome = _execute_or_wait(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap, direction=pending_dir,
            signal_id=str(state.pending_signal["signal_id"]), signal_type=str(state.pending_signal.get("signal_type") or "INITIAL"),
            position=None, result=result, signal_detected_at=_parse_iso_dt(state.pending_signal.get("detected_at")),
        )
        if outcome is not None:
            _apply_switch_outcome(state, outcome, pending_dir, now=now)
            result.actions.append(f"ENTRY:{pending_dir.value}")
            state.last_evaluated_bar_ts = bar_ts_str
            return result

    if entry_window_open and confirmed_direction != Direction.HOLD and not gap_bar_starts:
        outcome = _dispatch_confirmed_signal(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            direction=confirmed_direction, signal_type="INITIAL", position=None, result=result, bars_3m=bars_3m,
        )
        if _is_filtered(outcome):
            result.actions.append(f"{config.FILTERED_OUT}:{confirmed_direction.value}")
        elif outcome is not None:
            _apply_switch_outcome(state, outcome, confirmed_direction, now=now)
            result.actions.append(f"ENTRY:{confirmed_direction.value}")
            return result

    state.last_evaluated_bar_ts = bar_ts_str
    return result


class TslaAutoWorker:
    """Owns exactly one background tick thread (docs §3 — Worker singleton,
    separate lock file tsla_auto_worker.lock)."""

    def __init__(self, *, broker, market_data: MarketDataService, get_state, save_state, tick_interval_sec: float = config.WORKER_INTERVAL_SEC) -> None:
        self._broker = broker
        self._market_data = market_data
        self._get_state = get_state
        self._save_state = save_state
        self._tick_interval_sec = tick_interval_sec
        self.instance_id = uuid.uuid4().hex[:12]
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._tick_count = 0
        self._last_tick_at: Optional[str] = None
        self._lock = threading.RLock()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                state = self._get_state()
                run_once(broker=self._broker, market_data=self._market_data, state=state, now=datetime.now(ET))
                with self._lock:
                    self._tick_count += 1
                    self._last_tick_at = datetime.now(ET).isoformat()
                self._save_state(state)
            except Exception:
                pass
            self._stop_event.wait(self._tick_interval_sec)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=config.WORKER_NAME)
        self._thread.start()

    def stop(self, join_timeout: float = 3.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout)
        self._thread = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def tick_stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "worker_instance_id": self.instance_id, "tick_count": self._tick_count,
                "last_tick_at": self._last_tick_at, "worker_code_sha": git_sha(),
            }
