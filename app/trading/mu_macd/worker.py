"""MU_MACD worker — one MU tick/clock-driven decision per run_once() call.

Reuses macd2's stateless, generic building blocks directly:
  - signal_engine.resample_completed_3m / calculate_macd / evaluate_macd_crossover
    (pure functions; calculate_macd internally reads macd2.config.EMA_FAST/
    SLOW/SIGNAL=12/26/9 — the standard MACD parameters, intentionally shared,
    not a MU_MACD-specific tunable). evaluate_macd_crossover is the SAME
    zero-line-crossing rule macd2.worker._advance_confirmed_primary actually
    uses for real order authority (verified 2026-08-12: it is NOT the 3-
    consecutive-histogram "raw KIS color" rule used inside major_flag_filter
    for a different purpose — using that rule for MU produced spurious extra
    flags in this project's own research scratchpad; this worker exists
    specifically to use the CORRECT rule everywhere).
  - order_executor.execute_signal / execute_exit / target_symbol_for_direction
    (fully generic: broker/quotes/position/budget are all passed in, no
    macd2 state is read or written).
  - models.Direction / PositionSnapshot (frozen value types, no shared
    mutable state).

Never imports macd2.worker, macd2.state_store, or macd2.ledger — MU_MACD's
own state_store/ledger modules are the only place this process's state is
read or written.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from app.trading.macd2 import order_executor
from app.trading.macd2 import time_window_filter as twf
from app.trading.macd2 import time_window_position_manager as twpm
from app.trading.macd2.models import SignalState
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, forming_bar_window, resample_completed_3m
from app.trading.mu_macd import config, ledger
from app.trading.mu_macd.market_data import MUMarketDataService
from app.trading.mu_macd.models import Direction, PositionSnapshot, RuntimeState, TickResult

_TW_EXIT_REASON_MAP = {
    "TIME_WINDOW_STOP_LOSS": config.EXIT_TW_STOP_LOSS,
    "TIME_WINDOW_TP1_PARTIAL": config.EXIT_TW_TP1_PARTIAL,
    "TIME_WINDOW_TP2_FULL": config.EXIT_TW_TP2_FULL,
    "TIME_WINDOW_AFTER_TP1_STOP": config.EXIT_TW_AFTER_TP1_STOP,
    "TIME_WINDOW_TRAILING_STOP": config.EXIT_TW_TRAILING_STOP,
}

KST = config.KST


def _now_iso(now: datetime) -> str:
    return now.astimezone(KST).isoformat()


def _apply_day_rollover(state: RuntimeState, now: datetime) -> None:
    today_str = now.astimezone(KST).strftime("%Y%m%d")
    if state.session_date == today_str:
        return
    state.session_date = today_str
    state.last_detected_direction = None
    state.last_confirmed_bar_ts = None
    state.processed_signal_ids = []
    # Time-window filter's daily entry counts and any pending (unresolved)
    # T+3 candidate are session-scoped; the toggle itself and an ALREADY-open
    # position's own ladder state survive the rollover (mirrors macd2's own
    # _apply_day_rollover exactly).
    state.time_window_morning_entry_count = 0
    state.time_window_afternoon_entry_count = 0
    state.time_window_pending_flag_direction = None
    state.time_window_pending_flag_bar_ts = None
    # "TW 1 blue" 예외진입(2026-08-19)의 "하루 1회" 소진 플래그도 마찬가지로
    # session-scoped; 토글(down_blue_exception_filter_enabled)은 그대로 유지.
    state.daily_down_blue_exception_used = False


def _should_reconcile(state: RuntimeState, now: datetime) -> bool:
    """A HELD position always reconciles (accuracy matters for stop-loss
    math). FLAT only reconciles once per RECONCILE_INTERVAL_SEC_WHEN_FLAT --
    calling broker.get_positions() every ~2s tick hit KIS's per-second rate
    limit within seconds in the 2026-08-12 live mock-mode smoke test."""
    if state.position is not None and state.position.quantity > 0:
        return True
    if not state.last_position_reconcile_at:
        return True
    try:
        last = datetime.fromisoformat(state.last_position_reconcile_at)
    except ValueError:
        return True
    return (now - last).total_seconds() >= config.RECONCILE_INTERVAL_SEC_WHEN_FLAT


def _reconcile_position(broker, state: RuntimeState, now: datetime) -> str:
    """Minimal, MU_MACD-scoped reconciliation — only ever looks at
    config.TRADE_SYMBOLS (the same two ETFs macd2 trades), never touches
    macd2's own position/state."""
    if not _should_reconcile(state, now):
        return str((state.position_reconcile_diag or {}).get("comparison_result") or "SKIPPED_THROTTLED")
    state.last_position_reconcile_at = now.astimezone(KST).isoformat()
    result = _do_reconcile(broker, state, now)
    state.position_reconcile_diag = {"comparison_result": result}
    return result


def _record_reconcile_correction(
    *, symbol: str, side: str, qty_delta: int, price: Optional[float],
    position_before: int, position_after: int, note: str, now: datetime,
) -> None:
    """Execution-ledger row for a quantity jump discovered ONLY via broker
    reconciliation, never through a normal order fill (2026-08-13 real
    incident: a partial-fill BUY's cancel attempt did not actually stop the
    resting KIS limit order, which kept filling in the background for
    ~24 minutes -- state.position silently jumped from 110 to 994 shares
    with zero ledger trace, because _do_reconcile only ever overwrote
    state.position and returned a diagnostic string, never wrote a ledger
    row). ``price`` is a best-effort IMPLIED average for the newly
    discovered quantity (derived from the before/after blended avg_price
    that IS available from the broker) -- KIS never gives us a genuine
    per-fill price for shares we never placed/tracked an order for, so this
    is clearly marked via ``note`` as an inferred correction, not a real
    order confirmation."""
    order_id = f"RECONCILE:{symbol}:{now.strftime('%Y%m%d%H%M%S%f')}"
    ledger.append_execution({
        "timestamp": now.isoformat(), "signal_id": "", "order_id": order_id,
        "symbol": symbol, "side": side,
        "requested_qty": abs(qty_delta), "executed_qty": abs(qty_delta),
        "requested_price": price if price is not None else "",
        "executed_price": price if price is not None else "",
        "success": True, "exit_reason": note,
        "position_before": position_before, "position_after": position_after,
        "strategy_name": config.STRATEGY_NAME, "strategy_version": config.STRATEGY_VERSION,
    })


def _query_held_positions(broker) -> Optional[dict[str, Any]]:
    """Returns {symbol: Position} for config.TRADE_SYMBOLS currently held
    (qty>0), or None if the broker call itself raised."""
    try:
        broker_positions = broker.get_positions()
    except Exception:
        return None
    held: dict[str, Any] = {}
    for p in broker_positions or []:
        symbol = str(getattr(p, "symbol", "") or "")
        qty = int(getattr(p, "quantity", 0) or 0)
        if symbol in config.TRADE_SYMBOLS and qty > 0:
            held[symbol] = p
    return held


def _matches_runtime(held: dict[str, Any], runtime_symbol: Optional[str], runtime_qty: int) -> bool:
    if runtime_qty <= 0:
        return not held
    if runtime_symbol not in held:
        return False
    return int(getattr(held[runtime_symbol], "quantity", 0) or 0) == runtime_qty


def _do_reconcile(
    broker, state: RuntimeState, now: datetime, *,
    confirm_retries: int = config.RECONCILE_CONFIRM_RETRIES,
    confirm_delay_sec: float = config.RECONCILE_CONFIRM_DELAY_SEC,
) -> str:
    held = _query_held_positions(broker)
    if held is None:
        return "ERROR:get_positions_failed"

    runtime_symbol = state.position.symbol if state.position else None
    runtime_qty = int(state.position.quantity) if state.position else 0

    if _matches_runtime(held, runtime_symbol, runtime_qty):
        if runtime_qty <= 0:
            state.position = None
            return "MATCH_FLAT"
        return "MATCH_POSITION"

    # A single KIS inquire-balance read that disagrees with our own tracked
    # position can be stale/settlement-lagged (the same latency class
    # order_executor.py's _reconcile_to_zero/_reconcile_buy_fill already
    # retry a real SELL/BUY around) -- re-confirm before trusting it enough
    # to overwrite state.position and write an untracked-correction ledger
    # row (2026-08-14 real incident: exactly one such stale read wiped a
    # genuinely still-held position with zero fill info).
    for _ in range(max(0, confirm_retries)):
        if confirm_delay_sec > 0:
            time.sleep(confirm_delay_sec)
        recheck = _query_held_positions(broker)
        if recheck is None:
            continue
        held = recheck
        if _matches_runtime(held, runtime_symbol, runtime_qty):
            if runtime_qty <= 0:
                state.position = None
                return "MATCH_FLAT"
            return "MATCH_POSITION"

    # Mismatch persisted across the recheck window -- treat as real.
    if runtime_qty <= 0 and not held:
        state.position = None
        return "MATCH_FLAT"

    if runtime_qty > 0 and runtime_symbol in held:
        broker_row = held[runtime_symbol]
        broker_qty = int(getattr(broker_row, "quantity", 0) or 0)
        # Broker is authority on real holdings.
        old_qty, old_avg = runtime_qty, state.position.avg_price
        new_avg = float(getattr(broker_row, "avg_price", 0.0) or old_avg)
        delta = broker_qty - old_qty
        if delta > 0:
            implied_price = ((new_avg * broker_qty) - (old_avg * old_qty)) / delta
            _record_reconcile_correction(
                symbol=runtime_symbol, side="BUY", qty_delta=delta, price=implied_price,
                position_before=old_qty, position_after=broker_qty,
                note="RECONCILE_QTY_INCREASE_UNTRACKED_FILL", now=now,
            )
        else:
            _record_reconcile_correction(
                symbol=runtime_symbol, side="SELL", qty_delta=-delta, price=new_avg,
                position_before=old_qty, position_after=broker_qty,
                note="RECONCILE_QTY_DECREASE_UNTRACKED", now=now,
            )
        state.position = PositionSnapshot(
            symbol=runtime_symbol, quantity=broker_qty, avg_price=new_avg,
            entry_at=state.position.entry_at,
        )
        return "RECOVERED_QTY_MISMATCH"

    if runtime_qty <= 0 and held:
        symbol, row = next(iter(held.items()))
        broker_qty = int(getattr(row, "quantity", 0) or 0)
        avg_price = float(getattr(row, "avg_price", 0.0) or 0.0)
        _record_reconcile_correction(
            symbol=symbol, side="BUY", qty_delta=broker_qty, price=avg_price,
            position_before=0, position_after=broker_qty,
            note="RECONCILE_POSITION_DISCOVERED_UNTRACKED", now=now,
        )
        state.position = PositionSnapshot(symbol=symbol, quantity=broker_qty, avg_price=avg_price, entry_at=now)
        return "RECOVERED_FROM_BROKER"

    if runtime_qty > 0 and not held:
        # Broker shows flat but we don't know the sell price at all (get_positions()
        # never tells us that) -- price is left blank rather than fabricated.
        _record_reconcile_correction(
            symbol=runtime_symbol, side="SELL", qty_delta=runtime_qty, price=None,
            position_before=runtime_qty, position_after=0,
            note="RECONCILE_POSITION_VANISHED_UNTRACKED", now=now,
        )
        state.position = None
        return "RECOVERED_TO_FLAT"

    return "POSITION_MISMATCH"


def _record_signal(
    *, state: RuntimeState, bar_start: datetime, confirmed_at: datetime, direction: Direction,
    macd_val: float, signal_val: float, hist_val: float, signal_type: str,
    order_result: str, block_reason: Optional[str],
) -> str:
    signal_id = f"{bar_start.astimezone(KST):%Y%m%d}_{bar_start.astimezone(KST):%H%M%S}_{direction.value}"
    ledger.append_signal({
        "trading_date": bar_start.astimezone(KST).strftime("%Y%m%d"),
        "bar_start_at": bar_start.isoformat(), "confirmed_at": confirmed_at.isoformat(),
        "signal_id": signal_id, "signal_type": signal_type, "direction": direction.value,
        "macd": macd_val, "signal": signal_val, "hist": hist_val,
        "detected_at": confirmed_at.isoformat(),
        "order_result": order_result, "block_reason": block_reason or "",
        "strategy_name": config.STRATEGY_NAME, "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "worker_instance_id": state.worker_instance_id or "",
        "ws_connected": state.ws_connected, "ws_last_tick_at": state.ws_last_tick_at or "",
        "ws_last_error": state.ws_last_error or "",
        "warmup_bars_3m_count": state.warmup_bars_3m_count, "warmup_ready": state.warmup_ready,
        "final_result": order_result,
    })
    return signal_id


def _entry_gate_block_reason(state: RuntimeState, now: datetime) -> Optional[str]:
    """NEW-ENTRY-ONLY gates (never applied to an exit)."""
    if state.entry_paused:
        return config.BLOCK_ENTRY_PAUSED_BY_USER
    if now.astimezone(KST).time() < config.SESSION_OPEN:
        return config.BLOCK_ENTRY_WINDOW_CLOSED
    if now.astimezone(KST).time() >= config.NEW_ENTRY_CUTOFF:
        return config.BLOCK_ENTRY_WINDOW_CLOSED
    if config.MIDDAY_ENTRY_PAUSE_START <= now.astimezone(KST).time() < config.MIDDAY_ENTRY_PAUSE_END:
        return config.BLOCK_MIDDAY_ENTRY_PAUSE
    if not state.ws_connected:
        return config.BLOCK_WS_DISCONNECTED
    if state.ws_last_tick_at is None:
        return config.BLOCK_WS_STALE
    last_tick = datetime.fromisoformat(state.ws_last_tick_at)
    if (now - last_tick).total_seconds() > config.WS_STALE_MAX_SEC:
        return config.BLOCK_WS_STALE
    if not state.warmup_ready:
        return config.BLOCK_WARMUP_INSUFFICIENT
    return None


def _reset_time_window_position_state(state: RuntimeState) -> None:
    state.time_window_position_active = False
    state.time_window_tp1_done = False
    state.time_window_peak_net_return = 0.0
    state.time_window_stop_loss_bar_symbol = None
    state.time_window_stop_loss_entry_bar_ts = None
    state.time_window_stop_loss_bar_ts = None
    state.time_window_stop_loss_bar_close = None


def _advance_time_window_stop_loss_bar(state: RuntimeState, symbol: str, current_price: float, now: datetime) -> Optional[float]:
    """Mirrors app.trading.macd2.worker._advance_stop_loss_bar exactly (same
    completed-3m-bar-close tracking, tick-sampled from repeated broker quotes
    -- no dependency on the MU signal feed, same as that function's own
    docstring requires): a single live tick touching TP1/TP2/STOP_LOSS is
    NOT enough to fire the ladder any more -- only a bar that has FULLY
    completed past the entry bar counts. 2026-08-18 real incident: a
    momentary spike-then-drop tripped STOP_LOSS on a single bad tick right
    before the position would have gone deeply profitable; macd2's own
    time-window ladder already avoided this exact failure mode via this same
    pattern -- MU_MACD's copy of the ladder had never been given it."""
    bar_start, _bar_end = forming_bar_window(now)
    bar_key = bar_start.isoformat()

    if state.time_window_stop_loss_bar_symbol != symbol or state.time_window_stop_loss_entry_bar_ts is None:
        state.time_window_stop_loss_bar_symbol = symbol
        state.time_window_stop_loss_entry_bar_ts = bar_key
        state.time_window_stop_loss_bar_ts = bar_key
        state.time_window_stop_loss_bar_close = current_price
        return None

    if bar_key == state.time_window_stop_loss_bar_ts:
        state.time_window_stop_loss_bar_close = current_price
        return None

    completed_bar_ts = state.time_window_stop_loss_bar_ts
    completed_close = state.time_window_stop_loss_bar_close
    state.time_window_stop_loss_bar_ts = bar_key
    state.time_window_stop_loss_bar_close = current_price
    if completed_bar_ts is None or completed_bar_ts <= state.time_window_stop_loss_entry_bar_ts:
        return None
    return completed_close


def _advance_time_window_position_management(
    *, broker, state: RuntimeState, pos: Optional[PositionSnapshot], now: datetime,
) -> Optional[str]:
    """Position-management half of the time-window filter — a position THIS
    filter opened manages its own STOP_LOSS(-1.7%)/TP1(+3.0%-50%)/ratcheted-
    stop/TP2(+5.0%) ladder via app.trading.macd2.time_window_position_
    manager.evaluate_morning_position (import only, same as everywhere else
    in this integration).

    CRITICAL: called EARLY in run_once, using ONLY the traded ETF's own
    broker quote — same as MU_MACD's own plain STOP_LOSS check and for the
    exact same reason (this module's own docstring: "Never gated on WS
    health -- an exit always uses the traded ETF's own broker quote, never
    the MU feed"). It must NEVER be gated behind bars_3m/macd_snap
    readiness (WARMUP_MIN_3M_BARS) the way flag/candidate detection
    legitimately is -- a held position's risk management must keep running
    even during a post-restart warm-up window when the MU feed has no
    history yet (this module always starts cold, per config.py's own
    WARMUP_MIN_3M_BARS comment), otherwise a real loss could run
    completely unmonitored for up to ~90 minutes after every restart.
    Returns a short action label, or None if nothing happened this tick.
    """
    if pos is None or pos.quantity <= 0 or not state.time_window_position_active:
        return None
    current_price = broker.get_quote(pos.symbol) if hasattr(broker, "get_quote") else None
    if not current_price:
        return None
    completed_close = _advance_time_window_stop_loss_bar(state, pos.symbol, float(current_price), now)
    if completed_close is None:
        return None
    net_return = (float(completed_close) - pos.avg_price) / pos.avg_price * 100.0
    pm = twpm.evaluate_morning_position(
        net_return_pct=net_return, tp1_done=state.time_window_tp1_done,
        peak_net_return=state.time_window_peak_net_return,
    )
    state.time_window_peak_net_return = pm.peak_net_return
    state.time_window_tp1_done = pm.tp1_done
    if pm.exit_reason is None:
        return None
    mu_exit_reason = _TW_EXIT_REASON_MAP.get(pm.exit_reason, pm.exit_reason)
    if pm.exit_reason == "TIME_WINDOW_TP1_PARTIAL" and pos.quantity > 1:
        sell_qty = min(pos.quantity - 1, max(1, round(pos.quantity * pm.sell_fraction)))
        remaining = pos.quantity - sell_qty
        outcome = order_executor.execute_partial_exit(
            broker=broker, symbol=pos.symbol, sell_qty=sell_qty, remaining_qty=remaining,
            exit_reason=mu_exit_reason, entry_price=pos.avg_price, ledger_module=ledger,
        )
        if outcome.final_state == SignalState.EXECUTED:
            state.position = PositionSnapshot(
                symbol=pos.symbol, quantity=remaining, avg_price=pos.avg_price, entry_at=pos.entry_at,
            )
        return f"{mu_exit_reason}:{pos.symbol}"
    outcome = order_executor.execute_exit(
        broker=broker, symbol=pos.symbol, quantity=pos.quantity,
        exit_reason=mu_exit_reason, entry_price=pos.avg_price, ledger_module=ledger,
    )
    if outcome.final_state == SignalState.EXECUTED:
        state.position = None
        _reset_time_window_position_state(state)
    return f"{mu_exit_reason}:{pos.symbol}"


def _advance_time_window_filter(
    *, broker, state: RuntimeState, now: datetime, bars_3m, macd_snap,
    confirmed_direction: Direction, pos: Optional[PositionSnapshot],
) -> Optional[str]:
    """Candidate-tracking half of the time-window filter (2026-08-15 사용자
    요청) — reuses app.trading.macd2.time_window_filter.evaluate_time_
    window_entry (same two-bar T->T+3 delayed confirmation + per-window
    quality-score gate) BY IMPORT ONLY. Only reached once bars_3m/macd_snap
    are ready (entry decisions legitimately need MACD data) -- position
    MANAGEMENT for an already-open position is handled separately and
    earlier by _advance_time_window_position_management, which does NOT
    have this readiness requirement. Only invoked when state.time_window_
    filter_enabled is True. Returns a short action label, or None when
    nothing happened this tick (still waiting on a pending T+3 candidate).

    2026-08-15: state.entry_paused ("신규진입 일시정지") is honored here too,
    independently of this filter's own toggle -- see the entry_paused check
    right before the buy leg below.
    """
    # 1) a fresh confirmed crossover always becomes (replaces) the pending
    #    T+3 candidate — never dispatched on its own bar.
    if confirmed_direction != Direction.HOLD:
        state.time_window_pending_flag_direction = confirmed_direction.value
        state.time_window_pending_flag_bar_ts = macd_snap.bar_dt.isoformat()
        state.last_time_window_decision = config.BLOCK_TW_PENDING_CONFIRMATION
        state.last_time_window_block_reason = config.BLOCK_TW_PENDING_CONFIRMATION
        return f"TW_PENDING:{confirmed_direction.value}"

    # 3) resolve a pending candidate exactly one bar after its own flag bar
    if not state.time_window_pending_flag_direction or not state.time_window_pending_flag_bar_ts:
        return None
    flag_bar_dt = datetime.fromisoformat(state.time_window_pending_flag_bar_ts)
    if macd_snap.bar_dt == flag_bar_dt:
        return None  # still sitting on the flag's own bar -- wait for T+3

    direction = Direction(state.time_window_pending_flag_direction)
    state.time_window_pending_flag_direction = None
    state.time_window_pending_flag_bar_ts = None
    signal_type = "REVERSAL" if (pos is not None and pos.quantity > 0) else "INITIAL"

    position_direction = None
    if pos is not None and pos.quantity > 0:
        position_direction = Direction.UP_RED if pos.symbol == config.LONG_SYMBOL else Direction.DOWN_BLUE

    decision = twf.evaluate_time_window_entry(
        bars_3m, direction, flag_bar_dt, now,
        position_direction=position_direction,
        morning_entry_count=int(state.time_window_morning_entry_count or 0),
        afternoon_entry_count=int(state.time_window_afternoon_entry_count or 0),
    )
    state.last_time_window_score = decision.score
    state.last_time_window_decision = decision.decision
    state.last_time_window_block_reason = decision.block_reason

    # Optional "TW 1 blue" 예외진입 (2026-08-19) -- mirrors app.trading.macd2's
    # own down_blue_exception_filter_enabled sub-toggle exactly (same
    # conditions/logic, see that module's config.py for the backtest
    # rationale this was ported from). A DOWN_BLUE candidate the real TW
    # gate above just rejected (for ANY reason) still gets exactly one extra
    # entry per trading day, no other condition -- but never while a
    # position is already open (never overrides/switches an existing
    # TW-managed position; that stays governed by the real gate only).
    down_blue_exception_applied = (
        not decision.approved
        and state.down_blue_exception_filter_enabled
        and direction == Direction.DOWN_BLUE
        and not state.daily_down_blue_exception_used
        and not (pos is not None and pos.quantity > 0)
    )

    if not decision.approved and not down_blue_exception_applied:
        _record_signal(
            state=state, bar_start=flag_bar_dt, confirmed_at=now, direction=direction,
            macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
            signal_type=signal_type, order_result="BLOCKED", block_reason=decision.block_reason,
        )
        return f"TW_REJECTED:{direction.value}:{decision.decision}"

    if down_blue_exception_applied:
        state.daily_down_blue_exception_used = True
        state.last_down_blue_exception_at = now.astimezone(KST).isoformat()

    target_symbol = order_executor.target_symbol_for_direction(direction)

    # "신규진입 일시정지" (state.entry_paused) must function independently of
    # this filter's own ON/OFF toggle -- same semantics as the legacy path's
    # entry_block_reason gate (see its own UI help text): an opposite-
    # direction held position is still SOLD (mirrors the legacy "opposite
    # flag sells regardless of the entry gate" rule), only the follow-up
    # re-buy leg is what's paused. A flat/no-position tick simply records no
    # entry at all.
    if state.entry_paused:
        if pos is not None and pos.quantity > 0 and pos.symbol != target_symbol:
            outcome = order_executor.execute_exit(
                broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                exit_reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=pos.avg_price,
                ledger_module=ledger,
            )
            if outcome.final_state == SignalState.EXECUTED:
                state.position = None
                _reset_time_window_position_state(state)
            _record_signal(
                state=state, bar_start=flag_bar_dt, confirmed_at=now, direction=direction,
                macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
                signal_type=signal_type, order_result=outcome.final_state.value,
                block_reason=config.BLOCK_ENTRY_PAUSED_BY_USER,
            )
            return f"TW_ENTRY_PAUSED_SELL_ONLY:{direction.value}"
        _record_signal(
            state=state, bar_start=flag_bar_dt, confirmed_at=now, direction=direction,
            macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
            signal_type=signal_type, order_result="BLOCKED", block_reason=config.BLOCK_ENTRY_PAUSED_BY_USER,
        )
        return f"TW_ENTRY_PAUSED:{direction.value}"

    quotes: dict[str, float] = {}
    if hasattr(broker, "get_quote"):
        symbols_needed = {target_symbol}
        if pos is not None:
            symbols_needed.add(pos.symbol)
        for sym in symbols_needed:
            q = broker.get_quote(sym)
            if q:
                quotes[sym] = float(q)

    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=str(uuid.uuid4()),
        quotes=quotes, position=pos, budget=state.budget, ledger_module=ledger,
    )
    _record_signal(
        state=state, bar_start=flag_bar_dt, confirmed_at=now, direction=direction,
        macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
        signal_type=signal_type, order_result=outcome.final_state.value, block_reason=outcome.block_reason,
    )
    if outcome.final_state == SignalState.EXECUTED:
        state.position = PositionSnapshot(
            symbol=target_symbol, quantity=outcome.quantity,
            avg_price=outcome.filled_avg_price or 0.0, entry_at=now,
        )
        state.time_window_position_active = True
        state.time_window_tp1_done = False
        state.time_window_peak_net_return = 0.0
        # Seed the completed-bar stop-loss tracker so the bar CONTAINING this
        # entry fill is excluded from its own first evaluation (mirrors
        # macd2.worker._apply_switch_outcome's identical seeding) -- without
        # this, _advance_time_window_stop_loss_bar's own defensive fallback
        # would still self-seed correctly on its first call, just up to one
        # tick later than the true entry bar.
        _entry_bar_start, _ = forming_bar_window(now)
        state.time_window_stop_loss_bar_symbol = target_symbol
        state.time_window_stop_loss_entry_bar_ts = _entry_bar_start.isoformat()
        state.time_window_stop_loss_bar_ts = _entry_bar_start.isoformat()
        state.time_window_stop_loss_bar_close = state.position.avg_price
        window = decision.metrics.get("window") if decision.metrics else None
        if window is None:
            # A rejected decision (down_blue_exception_applied path) may not
            # have classified a window at all -- e.g. an early reject like
            # macd_signal_not_held short-circuits before window lookup.
            window = twf.classify_window(macd_snap.bar_dt.astimezone(KST).time())
        session = twf.session_for_window(window)
        if session == "MORNING":
            state.time_window_morning_entry_count = int(state.time_window_morning_entry_count or 0) + 1
        elif session == "AFTERNOON":
            state.time_window_afternoon_entry_count = int(state.time_window_afternoon_entry_count or 0) + 1
        return f"TW_ENTRY_VIA_DOWN_BLUE_EXCEPTION:{direction.value}" if down_blue_exception_applied else f"TW_ENTRY:{direction.value}"
    return f"TW_ENTRY_BLOCKED:{direction.value}"


def run_once(*, broker, market_data: MUMarketDataService, state: RuntimeState, now: datetime) -> TickResult:
    result = TickResult()
    _apply_day_rollover(state, now)

    state.tick_seq_total += 1
    state.last_tick_at = _now_iso(now)
    state.ws_connected = bool(market_data.ws_connected)
    state.ws_last_error = market_data.ws_last_error
    if market_data.ws_last_tick_at is not None:
        state.ws_last_tick_at = market_data.ws_last_tick_at.astimezone(KST).isoformat()
    state.last_mu_price = market_data.last_price
    state.last_mu_tvol = market_data.last_tvol

    # ── ETF quotes for display -- fetched every tick regardless of position
    # (existing entry/exit logic below fetches its own quotes when it needs
    # them for order sizing; this is purely for the UI to show live ETF
    # prices, not used for any order decision). ─────────────────────────────
    if hasattr(broker, "get_quote"):
        long_quote = broker.get_quote(config.LONG_SYMBOL)
        if long_quote:
            state.last_long_etf_price = float(long_quote)
        inverse_quote = broker.get_quote(config.INVERSE_SYMBOL)
        if inverse_quote:
            state.last_inverse_etf_price = float(inverse_quote)
        state.last_etf_quote_at = _now_iso(now)

    reconcile = _reconcile_position(broker, state, now)
    result.actions.append(f"RECONCILE:{reconcile}")

    # ── Held position: Stop Loss / Forced Liquidation — checked EVERY tick,
    # independent of whether a new MACD flag fires this tick (price can
    # breach either threshold between flags; a real exit must never wait for
    # the next flag). Never gated on WS health -- an exit always uses the
    # traded ETF's own broker quote, never the MU feed. ─────────────────────
    pos = state.position
    if pos is not None and pos.quantity > 0:
        # A position opened by the time-window filter manages its own
        # STOP_LOSS/take-profit ladder via _advance_time_window_position_
        # management, called HERE (not gated on bars_3m/macd_snap warm-up
        # readiness the way flag/candidate detection legitimately is below --
        # see that function's own docstring for why this must never go dark)
        # -- this plain STOP_LOSS/QUICK_PROFIT check must not also act on the
        # SAME position. FORCED_LIQUIDATION right below stays universal
        # regardless (applies to every position).
        tw_managed = state.time_window_filter_enabled and state.time_window_position_active
        if tw_managed:
            tw_pm_action = _advance_time_window_position_management(broker=broker, state=state, pos=pos, now=now)
            if tw_pm_action is not None:
                result.actions.append(tw_pm_action)
                if state.position is None or state.position.quantity != pos.quantity:
                    return result
        current_price = broker.get_quote(pos.symbol) if (not tw_managed and hasattr(broker, "get_quote")) else None
        if current_price:
            net_return = (float(current_price) - pos.avg_price) / pos.avg_price * 100.0
            if net_return <= config.STOP_LOSS_NET_PCT:
                outcome = order_executor.execute_exit(
                    broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                    exit_reason=config.EXIT_STOP_LOSS, entry_price=pos.avg_price,
                    ledger_module=ledger,
                )
                if outcome.final_state == SignalState.EXECUTED:
                    state.position = None
                    result.actions.append(f"STOP_LOSS:{pos.symbol}")
                    return result
            elif state.quick_profit_enabled and net_return >= config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT:
                outcome = order_executor.execute_exit(
                    broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                    exit_reason=config.EXIT_QUICK_PROFIT_TAKE_PROFIT, entry_price=pos.avg_price,
                    ledger_module=ledger,
                )
                if outcome.final_state == SignalState.EXECUTED:
                    state.position = None
                    result.actions.append(f"QUICK_PROFIT_TAKE_PROFIT:{pos.symbol}")
                    return result
        if now.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            outcome = order_executor.execute_exit(
                broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                exit_reason=config.EXIT_FORCED_LIQUIDATION, entry_price=pos.avg_price,
                ledger_module=ledger,
            )
            if outcome.final_state == SignalState.EXECUTED:
                state.position = None
                _reset_time_window_position_state(state)
                result.actions.append(f"FORCED_LIQUIDATION:{pos.symbol}")
                return result
        pos = state.position  # re-read: still held if neither exit fired

    df_1m = market_data.get_history_df()
    state.warmup_bars_1m_count = market_data.warmup_bars_1m_count()

    bars_3m = resample_completed_3m(df_1m, now=now)
    state.warmup_bars_3m_count = int(len(bars_3m))
    state.warmup_ready = state.warmup_bars_3m_count >= config.WARMUP_MIN_3M_BARS

    macd_snap = calculate_macd(bars_3m)
    if macd_snap is None:
        result.skipped = "NOT_READY"
        return result

    bar_start = macd_snap.bar_dt  # KIS-screen convention: DISPLAYED flag time is the bar's own START, not its close.
    confirmed_at = bar_start.astimezone(KST) + timedelta(minutes=3)  # order authority only fires once the bar actually closes
    bar_ts_str = bar_start.isoformat()
    if bar_ts_str == state.last_confirmed_bar_ts:
        # Same bar already evaluated this tick-cycle -- HOLD, no repeat action.
        result.skipped = "SAME_BAR_ALREADY_EVALUATED"
        return result
    state.last_confirmed_bar_ts = bar_ts_str

    prev_direction = Direction(state.last_detected_direction) if state.last_detected_direction else None
    confirmed_direction = evaluate_macd_crossover(macd_snap, prev_direction)

    entry_block_reason = _entry_gate_block_reason(state, now)
    pos = state.position  # re-read again -- the Stop Loss/Forced Liquidation block above may have cleared it

    if state.time_window_filter_enabled:
        # Fully replaces the legacy immediate-entry/reversal logic below with
        # macd2's own two-bar (T -> T+3) delayed confirmation (see
        # _advance_time_window_filter; ladder/position-management for an
        # already-open position was already handled earlier via
        # _advance_time_window_position_management, independent of whether
        # bars_3m/macd_snap were ready this tick) -- this branch always
        # returns, whether or not anything actually happened this tick, so
        # the legacy code further down is structurally unreachable while
        # this toggle is ON.
        tw_action = _advance_time_window_filter(
            broker=broker, state=state, now=now, bars_3m=bars_3m, macd_snap=macd_snap,
            confirmed_direction=confirmed_direction, pos=pos,
        )
        if tw_action is not None:
            result.actions.append(tw_action)
        if confirmed_direction != Direction.HOLD:
            state.last_detected_direction = confirmed_direction.value
            state.last_flag_display_time = bar_start.isoformat()
            state.last_flag_confirmed_at = confirmed_at.isoformat()
            state.last_flag_direction = confirmed_direction.value
        return result

    if confirmed_direction == Direction.HOLD:
        # Still advance the baseline so a later real flag isn't compared
        # against a stale prior_direction (mirrors macd2's own baseline-only
        # first-evaluated-bar semantics).
        return result
    state.last_detected_direction = confirmed_direction.value
    state.last_flag_display_time = bar_start.isoformat()
    state.last_flag_confirmed_at = confirmed_at.isoformat()
    state.last_flag_direction = confirmed_direction.value

    target_symbol = order_executor.target_symbol_for_direction(confirmed_direction)

    # ── Held position: flag-driven reversal only (Stop Loss/Forced
    # Liquidation already handled unconditionally above) ───────────────────
    if pos is not None and pos.quantity > 0:
        if target_symbol == pos.symbol:
            result.actions.append("HELD_SAME")
            return result

        # Opposite MU flag: SELL always happens regardless of the entry gate
        # (sell-only/no-re-entry under a data-quality doubt — same principle
        # macd2 itself uses); the follow-up BUY only runs when the gate is
        # clear. execute_exit (never execute_signal) is used whenever the
        # gate is blocked, so the buy leg is structurally impossible to
        # reach — not merely skipped after the fact.
        if entry_block_reason is not None:
            outcome = order_executor.execute_exit(
                broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                exit_reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=pos.avg_price,
                ledger_module=ledger,
            )
            signal_id = _record_signal(
                state=state, bar_start=bar_start, confirmed_at=confirmed_at, direction=confirmed_direction,
                macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
                signal_type="REVERSAL", order_result=outcome.final_state.value,
                block_reason=entry_block_reason,
            )
            if outcome.final_state == SignalState.EXECUTED:
                state.position = None
            result.actions.append(f"OPPOSITE_SIGNAL_SELL_ONLY:{confirmed_direction.value}")
            state.order_block_reason = entry_block_reason
            return result

        quotes = {}
        if hasattr(broker, "get_quote"):
            for sym in (pos.symbol, target_symbol):
                q = broker.get_quote(sym)
                if q:
                    quotes[sym] = float(q)
        outcome = order_executor.execute_signal(
            broker=broker, direction=confirmed_direction, signal_id=str(uuid.uuid4()),
            quotes=quotes, position=pos, budget=state.budget,
            ledger_module=ledger,
        )
        signal_id = _record_signal(
            state=state, bar_start=bar_start, confirmed_at=confirmed_at, direction=confirmed_direction,
            macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
            signal_type="REVERSAL", order_result=outcome.final_state.value,
            block_reason=outcome.block_reason,
        )
        if outcome.final_state == SignalState.EXECUTED:
            state.position = PositionSnapshot(symbol=target_symbol, quantity=outcome.quantity,
                                                avg_price=outcome.filled_avg_price or 0.0, entry_at=now)
            result.actions.append(f"OPPOSITE_SIGNAL:{confirmed_direction.value}")
        else:
            result.actions.append(f"OPPOSITE_SIGNAL_SELL_ONLY:{confirmed_direction.value}")
        state.order_block_reason = outcome.block_reason
        return result

    # ── Flat: new entry ──────────────────────────────────────────────────
    if entry_block_reason is not None:
        _record_signal(
            state=state, bar_start=bar_start, confirmed_at=confirmed_at, direction=confirmed_direction,
            macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
            signal_type="INITIAL", order_result="BLOCKED", block_reason=entry_block_reason,
        )
        state.order_block_reason = entry_block_reason
        result.skipped = entry_block_reason
        return result

    quotes = {}
    if hasattr(broker, "get_quote"):
        q = broker.get_quote(target_symbol)
        if q:
            quotes[target_symbol] = float(q)
    outcome = order_executor.execute_signal(
        broker=broker, direction=confirmed_direction, signal_id=str(uuid.uuid4()),
        quotes=quotes, position=None, budget=state.budget,
        ledger_module=ledger,
    )
    _record_signal(
        state=state, bar_start=bar_start, confirmed_at=confirmed_at, direction=confirmed_direction,
        macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
        signal_type="INITIAL", order_result=outcome.final_state.value, block_reason=outcome.block_reason,
    )
    if outcome.final_state == SignalState.EXECUTED:
        state.position = PositionSnapshot(symbol=target_symbol, quantity=outcome.quantity,
                                            avg_price=outcome.filled_avg_price or 0.0, entry_at=now)
        result.actions.append(f"ENTRY:{confirmed_direction.value}")
    else:
        result.actions.append(f"ENTRY_BLOCKED:{confirmed_direction.value}")
    state.order_block_reason = outcome.block_reason
    return result


def run_flags_only(*, market_data: MUMarketDataService, state: RuntimeState, now: datetime) -> TickResult:
    """No-broker MU flag detection -- keeps MU price collection/warmup and
    MACD flag detection + signal-ledger recording running even when there is
    no authenticated broker to trade with (2026-08-14: REAL mode's
    KisRealBroker refuses to even construct without the confirm phrase, so
    after a process restart a real held position's reconcile/stop-loss/
    quick-profit/forced-liquidation genuinely cannot resume until the human
    re-enters it -- see service.py's _auto_recover_flags_only/
    config.BLOCK_REAL_BROKER_NOT_AUTHENTICATED).

    This function NEVER reads or writes state.position, NEVER calls
    order_executor, and NEVER reconciles against a broker -- it only
    advances the exact same day-rollover/MACD-baseline bookkeeping
    run_once() does (same pure resample_completed_3m/calculate_macd/
    evaluate_macd_crossover functions), so the dashboard/signal ledger keep
    showing real flags forming while order authority stays fully paused.
    """
    result = TickResult()
    _apply_day_rollover(state, now)

    state.tick_seq_total += 1
    state.last_tick_at = _now_iso(now)
    state.ws_connected = bool(market_data.ws_connected)
    state.ws_last_error = market_data.ws_last_error
    if market_data.ws_last_tick_at is not None:
        state.ws_last_tick_at = market_data.ws_last_tick_at.astimezone(KST).isoformat()
    state.last_mu_price = market_data.last_price
    state.last_mu_tvol = market_data.last_tvol

    df_1m = market_data.get_history_df()
    state.warmup_bars_1m_count = market_data.warmup_bars_1m_count()

    bars_3m = resample_completed_3m(df_1m, now=now)
    state.warmup_bars_3m_count = int(len(bars_3m))
    state.warmup_ready = state.warmup_bars_3m_count >= config.WARMUP_MIN_3M_BARS

    macd_snap = calculate_macd(bars_3m)
    if macd_snap is None:
        result.skipped = "NOT_READY"
        return result

    bar_start = macd_snap.bar_dt
    confirmed_at = bar_start.astimezone(KST) + timedelta(minutes=3)
    bar_ts_str = bar_start.isoformat()
    if bar_ts_str == state.last_confirmed_bar_ts:
        result.skipped = "SAME_BAR_ALREADY_EVALUATED"
        return result
    state.last_confirmed_bar_ts = bar_ts_str

    prev_direction = Direction(state.last_detected_direction) if state.last_detected_direction else None
    confirmed_direction = evaluate_macd_crossover(macd_snap, prev_direction)
    if confirmed_direction == Direction.HOLD:
        return result

    state.last_detected_direction = confirmed_direction.value
    state.last_flag_display_time = bar_start.isoformat()
    state.last_flag_confirmed_at = confirmed_at.isoformat()
    state.last_flag_direction = confirmed_direction.value

    signal_type = "REVERSAL" if (state.position is not None and state.position.quantity > 0) else "INITIAL"
    _record_signal(
        state=state, bar_start=bar_start, confirmed_at=confirmed_at, direction=confirmed_direction,
        macd_val=macd_snap.macd, signal_val=macd_snap.signal, hist_val=macd_snap.hist,
        signal_type=signal_type, order_result="BLOCKED", block_reason=config.BLOCK_REAL_BROKER_NOT_AUTHENTICATED,
    )
    state.order_block_reason = config.BLOCK_REAL_BROKER_NOT_AUTHENTICATED
    result.skipped = config.BLOCK_REAL_BROKER_NOT_AUTHENTICATED
    result.actions.append(f"FLAGS_ONLY_NO_BROKER:{confirmed_direction.value}")
    return result
