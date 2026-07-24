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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from app.trading.macd2 import config, ledger, order_executor, risk_exit
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState, RuntimeStatus, SignalState
from app.trading.macd2.signal_engine import calculate_macd, evaluate_signed_b, make_signal_id, resample_completed_3m
from app.trading.trading_cost_engine import TradeCostEngine

KST = config.KST

POSITION_MISMATCH = "POSITION_MISMATCH"


@dataclass
class TickResult:
    ok: bool = True
    actions: list[str] = field(default_factory=list)
    error: Optional[str] = None
    skipped: Optional[str] = None
    signal_detected_at: Optional[str] = None
    order_requested_at: Optional[str] = None


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
    state.last_evaluated_bar_ts = None
    state.processed_signal_ids = []
    state.peak_net_return = 0.0
    state.profit_lock_active = False


def _position_mismatch_reason(broker, state: RuntimeState) -> Optional[str]:
    """Real account holdings (one ``broker.get_positions()`` call) vs
    ``state.position`` — returns ``POSITION_MISMATCH`` on any disagreement
    (including a failed account query, which fails closed) so the caller can
    block every order this tick until it clears."""
    try:
        real_positions = {p.symbol: int(p.quantity) for p in broker.get_positions() if p.quantity}
    except Exception:
        return POSITION_MISMATCH
    expected_symbol = state.position.symbol if state.position and state.position.quantity > 0 else None
    expected_qty = int(state.position.quantity) if expected_symbol else 0
    for symbol in config.TRADE_SYMBOLS:
        want = expected_qty if symbol == expected_symbol else 0
        have = real_positions.get(symbol, 0)
        if want != have:
            return POSITION_MISMATCH
    return None


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

    if not state.auto_trade_on:
        result.skipped = "auto_trade_off"
        return result

    _apply_day_rollover(state, now)

    mismatch = _position_mismatch_reason(broker, state)
    if mismatch:
        state.order_block_reason = mismatch
        result.skipped = mismatch
        return result

    quotes = _fresh_quote_prices(market_data, (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL))

    # Worker never calls KIS itself and never triggers the incremental merge —
    # MarketDataService's own history-updater thread refreshes this cache in
    # the background (docs §8/§11); this only reads the cached snapshot.
    df_1m = market_data.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    macd_snap = calculate_macd(bars_3m)
    if macd_snap is None:
        state.warmup_ready = False
        result.skipped = "NOT_READY"
        return result
    state.warmup_ready = True

    trading_date = now.strftime("%Y%m%d")
    bar_ts_str = macd_snap.bar_dt.isoformat()
    is_new_bar = bar_ts_str != state.last_evaluated_bar_ts

    before_open = now.time() < config.SESSION_OPEN
    entry_cutoff_passed = now.time() >= config.NEW_ENTRY_CUTOFF
    force_liquidate_time = now.time() >= config.FORCE_LIQUIDATE_AT
    entry_window_open = (not before_open) and (not entry_cutoff_passed)

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

        if is_new_bar and entry_window_open:
            pattern = evaluate_signed_b(macd_snap, state.last_signal_direction)
            if pattern != Direction.HOLD:
                target = order_executor.target_symbol_for_direction(pattern)
                if target != pos.symbol:
                    signal_id = make_signal_id(trading_date, macd_snap.bar_dt.strftime("%H%M%S"), pattern)
                    signal_detected_at = datetime.now(KST)
                    result.signal_detected_at = signal_detected_at.isoformat()
                    outcome = order_executor.execute_signal(
                        broker=broker, direction=pattern, signal_id=signal_id, quotes=quotes,
                        position=pos, budget=state.budget,
                        processed_signal_ids=frozenset(state.processed_signal_ids),
                    )
                    result.order_requested_at = outcome.timestamps.get("sell_requested_at") or outcome.timestamps.get("buy_requested_at")
                    _record_signal_ledger(trading_date, macd_snap, pattern, "REVERSAL", signal_id, signal_detected_at, outcome)
                    _apply_switch_outcome(state, outcome, pattern)
                    result.actions.append(f"OPPOSITE_SIGNAL:{pattern.value}")
                    state.last_evaluated_bar_ts = bar_ts_str
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
    if is_new_bar and entry_window_open:
        pattern = evaluate_signed_b(macd_snap, state.last_signal_direction)
        if pattern != Direction.HOLD:
            signal_id = make_signal_id(trading_date, macd_snap.bar_dt.strftime("%H%M%S"), pattern)
            signal_detected_at = datetime.now(KST)
            result.signal_detected_at = signal_detected_at.isoformat()
            outcome = order_executor.execute_signal(
                broker=broker, direction=pattern, signal_id=signal_id, quotes=quotes,
                position=None, budget=state.budget,
                processed_signal_ids=frozenset(state.processed_signal_ids),
            )
            result.order_requested_at = outcome.timestamps.get("buy_requested_at")
            _record_signal_ledger(trading_date, macd_snap, pattern, "INITIAL", signal_id, signal_detected_at, outcome)
            _apply_switch_outcome(state, outcome, pattern)
            result.actions.append(f"ENTRY:{pattern.value}")

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
        state.last_signal_bar_ts = outcome.timestamps.get("evaluated_at")
        state.peak_net_return = 0.0
        state.profit_lock_active = False
    elif outcome.sell_result is not None and outcome.sell_result.success and outcome.sell_qty_after == 0:
        state.position = None
        state.peak_net_return = 0.0
        state.profit_lock_active = False
    if outcome.signal_id and outcome.signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [outcome.signal_id]
    state.order_block_reason = outcome.block_reason


def _record_signal_ledger(trading_date, macd_snap, direction, signal_type, signal_id, detected_at, outcome) -> None:
    order_result = outcome.final_state.value
    ledger.append_signal({
        "trading_date": trading_date,
        "completed_bar_at": macd_snap.bar_dt.strftime("%H%M%S"),
        "signal_id": signal_id,
        "signal_type": signal_type,
        "direction": direction.value,
        "macd": macd_snap.macd,
        "signal": macd_snap.signal,
        "hist_last3": str(macd_snap.hist_last3),
        "detected_at": detected_at.isoformat(),
        "order_requested_at": outcome.timestamps.get("buy_requested_at") or outcome.timestamps.get("sell_requested_at") or "",
        "order_result": order_result,
        "block_reason": outcome.block_reason or "",
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
        self._lock = threading.RLock()
        self._instance_id = uuid.uuid4().hex[:12]
        self._started_at: Optional[datetime] = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

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
            }

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                state = self._get_state()
                run_once(broker=self._broker, market_data=self._market_data, state=state, now=datetime.now(KST))
                self._save_state(state)
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
