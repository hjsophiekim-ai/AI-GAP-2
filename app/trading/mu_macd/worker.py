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

import uuid
from datetime import datetime, timedelta
from typing import Optional

from app.trading.macd2 import order_executor
from app.trading.macd2.models import SignalState
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m
from app.trading.mu_macd import config, ledger
from app.trading.mu_macd.market_data import MUMarketDataService
from app.trading.mu_macd.models import Direction, PositionSnapshot, RuntimeState, TickResult

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
    result = _do_reconcile(broker, state)
    state.position_reconcile_diag = {"comparison_result": result}
    return result


def _do_reconcile(broker, state: RuntimeState) -> str:
    try:
        broker_positions = broker.get_positions()
    except Exception as exc:
        return f"ERROR:{exc!r}"

    held = {}
    for p in broker_positions or []:
        symbol = str(getattr(p, "symbol", "") or "")
        qty = int(getattr(p, "quantity", 0) or 0)
        if symbol in config.TRADE_SYMBOLS and qty > 0:
            held[symbol] = p

    runtime_symbol = state.position.symbol if state.position else None
    runtime_qty = int(state.position.quantity) if state.position else 0

    if runtime_qty <= 0 and not held:
        state.position = None
        return "MATCH_FLAT"

    if runtime_qty > 0 and runtime_symbol in held:
        broker_row = held[runtime_symbol]
        broker_qty = int(getattr(broker_row, "quantity", 0) or 0)
        if broker_qty == runtime_qty:
            return "MATCH_POSITION"
        # Broker is authority on real holdings.
        state.position = PositionSnapshot(
            symbol=runtime_symbol, quantity=broker_qty,
            avg_price=float(getattr(broker_row, "avg_price", 0.0) or state.position.avg_price),
            entry_at=state.position.entry_at,
        )
        return "RECOVERED_QTY_MISMATCH"

    if runtime_qty <= 0 and held:
        symbol, row = next(iter(held.items()))
        state.position = PositionSnapshot(
            symbol=symbol, quantity=int(getattr(row, "quantity", 0) or 0),
            avg_price=float(getattr(row, "avg_price", 0.0) or 0.0), entry_at=datetime.now(KST),
        )
        return "RECOVERED_FROM_BROKER"

    if runtime_qty > 0 and not held:
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
    if now.astimezone(KST).time() < config.SESSION_OPEN:
        return config.BLOCK_ENTRY_WINDOW_CLOSED
    if now.astimezone(KST).time() >= config.NEW_ENTRY_CUTOFF:
        return config.BLOCK_ENTRY_WINDOW_CLOSED
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
        current_price = broker.get_quote(pos.symbol) if hasattr(broker, "get_quote") else None
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
