"""MU_MACD service — lifecycle control (start/stop), wraps its own worker
thread + its own MUMarketDataService WebSocket thread. Uses its OWN
threading.Lock (config.LOCK_FILENAME is documented but the actual mutual-
exclusion primitive is this in-process lock — a single process only ever
runs one MU_MACD worker thread) — never macd2's or tsla_auto's lock/thread.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from app.trading import strategy_ownership
from app.trading.macd2 import order_executor
from app.trading.macd2.broker_adapter import create_macd2_broker
from app.trading.macd2.models import SignalState
from app.trading.mu_macd import config, ledger, state_store, worker
from app.trading.mu_macd.market_data import MUMarketDataService
from app.trading.mu_macd.models import Direction, PositionSnapshot, RuntimeState

KST = config.KST
WORKER_INTERVAL_SEC = 2.0

_LOCK = threading.Lock()  # MU_MACD's own in-process lock — never shared with macd2/tsla_auto


def other_strategy_active() -> tuple[bool, str]:
    """2026-08-15: MACD2 and MU_MACD trade the identical two ETF symbols
    (0193T0/0197X0) in the same KIS account from independent signal sources
    -- block MU_MACD start() if MACD2 (or Enhanced) is really active, the
    same way app.trading.macd2.service.other_strategy_active() already
    blocks MACD2's own start() against MU_MACD (and Enhanced)."""
    return strategy_ownership.other_owner_active(strategy_ownership.MU_MACD)


def _record_manual_signal(
    state: RuntimeState, *, signal_id: str, signal_type: str, direction: Optional[Direction],
    now: datetime, order_result: str, block_reason: Optional[str],
) -> None:
    """Signal-ledger row for a manual buy/liquidate button click (2026-08-13)
    — mirrors macd2.service's _record_manual_entry_signal/
    _record_manual_liquidation_signal, adapted to MU_MACD's own (smaller)
    SIGNAL_LEDGER_COLUMNS schema. Execution-ledger recording already
    happens inside order_executor.execute_signal/execute_exit itself (via
    ledger_module=ledger, MU_MACD's OWN ledger module -- never macd2's)."""
    ledger.append_signal({
        "trading_date": now.astimezone(KST).strftime("%Y%m%d"),
        "confirmed_at": now.isoformat(),
        "signal_id": signal_id,
        "signal_type": signal_type,
        "direction": direction.value if direction is not None else "",
        "detected_at": now.isoformat(),
        "order_result": order_result,
        "block_reason": block_reason or "",
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": f"{signal_type}_UI_BUTTON",
        "worker_instance_id": state.worker_instance_id or "",
        "ws_connected": state.ws_connected, "ws_last_tick_at": state.ws_last_tick_at or "",
        "ws_last_error": state.ws_last_error or "",
        "warmup_bars_3m_count": state.warmup_bars_3m_count, "warmup_ready": state.warmup_ready,
        "final_result": f"{order_result}:{block_reason}" if block_reason else order_result,
    })


class MUMacdService:
    def __init__(self) -> None:
        # 2026-08-19: marks THIS process as the genuine live app/service
        # process -- see app.trading.macd2.service.Macd2Service's identical
        # marker and app.trading.macd2.ledger's docstring for the incident
        # this guards against. An ad-hoc/replay script that never constructs
        # this class (every scripts/_tmp_*.py replay so far) never sets
        # this, so it is still refused by ledger.append_signal/
        # append_execution and state_store.save_state unless it redirects
        # those paths itself first.
        os.environ[ledger.LIVE_WORKER_MARKER_ENV] = str(os.getpid())
        self._broker = None
        self._market_data: Optional[MUMarketDataService] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        # 2026-08-14: broker-less "flags only" shadow (see
        # _auto_recover_flags_only) -- its OWN market_data/thread/event,
        # never shared with the real worker's own attributes above.
        self._flags_only_market_data: Optional[MUMarketDataService] = None
        self._flags_only_thread: Optional[threading.Thread] = None
        self._flags_only_stop_event: Optional[threading.Event] = None

    def is_alive(self) -> bool:
        return bool(self._worker_thread and self._worker_thread.is_alive())

    def _flags_only_alive(self) -> bool:
        return bool(self._flags_only_thread and self._flags_only_thread.is_alive())

    def start(
        self, *, mode: str = "mock", budget: float = config.DEFAULT_BUDGET,
        confirm_text: str = "", runtime_enable_real_buy: bool = False, runtime_enable_real_sell: bool = False,
    ) -> dict[str, Any]:
        """mode="real" requires the SAME explicit opt-in macd2 itself requires
        (confirm_text + runtime_enable_real_buy/sell, both default OFF) —
        never silently placing a live order. Per current instructions, REAL
        orders remain disabled until explicitly validated further."""
        with _LOCK:
            # 2026-08-14 fix (real incident: after every commit/redeploy the
            # user has to click "자동매매 시작" again, but a still-alive
            # worker thread -- e.g. MOCK's own auto-recovery already having
            # kicked in from an earlier status() poll before they even
            # looked at the page -- disabled the button entirely with no
            # way to force a clean restart). Treat a re-click while already
            # alive as "restart", not "refuse": tear down the existing
            # worker/market_data first, then fall through to start fresh
            # exactly as if nothing had been running.
            if self.is_alive():
                self._stop_worker_and_market_data_locked()

            # 2026-08-14: release the broker-less flags-only shadow's WS
            # connection first -- about to open the real one below, and two
            # simultaneous MU WS subscriptions on the same KIS account would
            # be wasteful/risk a duplicate-subscription issue. Today's bars
            # were already being persisted to disk by the shadow the whole
            # time, so load_today_bars() below picks up right where it left
            # off -- no warmup gap from this handoff.
            self._stop_flags_only()

            # 2026-08-15: refuse to start while MACD2 (or Enhanced) is
            # really live -- see other_strategy_active()'s own docstring.
            # Checked here (same relative position macd2.service.start()
            # itself uses: after the restart-cleanly teardown, before any
            # state mutation/broker construction) so a blocked start never
            # half-mutates state.auto_trade_on/worker_started_at first.
            active, reason = other_strategy_active()
            if active:
                state = state_store.load_state()
                state.order_block_reason = reason
                state_store.save_state(state)
                return {"ok": False, "message": reason}

            state = state_store.load_state()
            state.mode = mode
            state.budget = budget
            state.auto_trade_on = True
            state.order_block_reason = None
            state.worker_instance_id = uuid.uuid4().hex[:12]
            state.worker_started_at = datetime.now(KST).isoformat()
            state_store.save_state(state)

            broker_kwargs = {} if mode == "mock" else {
                "confirm_text": confirm_text, "runtime_real_mode": True,
                "runtime_enable_real_buy": runtime_enable_real_buy, "runtime_enable_real_sell": runtime_enable_real_sell,
            }
            # 2026-08-14 fix: a wrong REAL confirm phrase (or any other
            # KisRealBroker.__init__ safety-gate failure -- missing env
            # vars, account conflict, etc.) raises a plain RuntimeError.
            # Uncaught, that would both (a) propagate out of start() as an
            # unhandled exception instead of a clean {"ok": False, ...}
            # (mirrors the exact try/except macd2's own service.start()
            # already has around this same call) and (b) leave
            # auto_trade_on=True/worker_started_at stamped above even
            # though no worker ever actually started -- status() would
            # then treat it as a stalled-but-still-wanted-on REAL session
            # and spin up the flags-only shadow for a login attempt that
            # never even happened.
            try:
                self._broker = create_macd2_broker(mode, **broker_kwargs)
            except Exception as exc:
                state.auto_trade_on = False
                state.worker_instance_id = None
                state.worker_started_at = None
                state.order_block_reason = f"BROKER_CREATE_FAILED:{exc}"
                state_store.save_state(state)
                return {"ok": False, "message": str(exc)}
            # 2026-08-12 fix: market data mode is DELIBERATELY always "real"
            # here, regardless of the broker's mock/real trading mode. There
            # is no mock WebSocket feed to fall back to -- MU's day-session
            # ticks only ever come from the real KIS WS. mode="mock" trading
            # means "use a paper-trading broker, driven by REAL live MU
            # signals" (mock=safe order execution, not fake market data).
            # MUMarketDataService's OWN "mock" mode exists only for unit
            # tests that never call .start() at all (they inject ticks
            # directly) -- never for this live service path.
            self._market_data = MUMarketDataService(mode="real")
            # 2026-08-13 fix: restore today's already-persisted 1-minute bars
            # BEFORE subscribing, so a same-day restart (e.g. right after a
            # code deploy) resumes warmup instead of blindly waiting out
            # WARMUP_MIN_3M_BARS*3min (90min) again with zero order/flag
            # authority in the meantime -- see market_data.py's module
            # docstring "fix #2".
            self._market_data.load_today_bars()
            self._market_data.start()

            self._stop_event = threading.Event()
            self._worker_thread = threading.Thread(target=self._run_loop, daemon=True, name="mu-macd-worker")
            self._worker_thread.start()
            return {"ok": True}

    def set_quick_profit_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional 2.5% Quick Profit take-profit
        exit. Only updates runtime state -- worker.run_once() reads
        state.quick_profit_enabled fresh every tick, so this takes effect
        on the very next tick without a service restart.

        2026-08-13 fix (real incident: toggling ON, then a later page
        refresh showed it OFF again): this load-modify-save had NO locking
        against _run_loop's OWN load-run_once-save cycle (every
        WORKER_INTERVAL_SEC=2s, on a separate thread). If a toggle landed
        between the worker tick's load and its save, the tick's save would
        silently clobber the toggle back to whatever it loaded (a classic
        lost-update race) -- invisible mid-session because the checkbox
        widget itself keeps showing Streamlit's own session_state value
        until a fresh page load reveals the true (reverted) persisted
        value. Both sides now share _LOCK so a toggle and a worker tick can
        never interleave.
        """
        with _LOCK:
            state = state_store.load_state()
            enabled_bool = bool(enabled)
            prev = bool(state.quick_profit_enabled)
            state.quick_profit_enabled = enabled_bool
            state_store.save_state(state)
        return {"ok": True, "quick_profit_enabled": enabled_bool, "previous": prev}

    def set_time_window_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional "시간대별 최적거래 필터" (reuses
        macd2.time_window_filter/time_window_position_manager directly, same
        entry logic and same stop-loss/take-profit ladder as that module's
        own filter). Only updates runtime state -- worker.run_once() reads
        state.time_window_filter_enabled fresh every tick. Shares _LOCK with
        _run_loop for the same reason set_quick_profit_enabled does.
        """
        with _LOCK:
            state = state_store.load_state()
            enabled_bool = bool(enabled)
            prev = bool(state.time_window_filter_enabled)
            state.time_window_filter_enabled = enabled_bool
            state_store.save_state(state)
        return {"ok": True, "time_window_filter_enabled": enabled_bool, "previous": prev}

    def set_down_blue_exception_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional "TW 1 blue" sub-filter of the
        time-window optimal trading filter -- a DOWN_BLUE candidate the TW
        gate itself rejects still gets one extra entry per trading day, no
        other condition (2026-08-19, ported from app.trading.macd2's own
        down_blue_exception_filter_enabled with identical conditions/logic --
        see that module's config.py for the backtest rationale). Only updates
        runtime state -- never places orders. Has no effect while time_window_
        filter_enabled is False (no TW candidates ever exist to reject).
        Shares _LOCK with _run_loop for the same reason set_quick_profit_
        enabled does.
        """
        with _LOCK:
            state = state_store.load_state()
            enabled_bool = bool(enabled)
            prev = bool(state.down_blue_exception_filter_enabled)
            state.down_blue_exception_filter_enabled = enabled_bool
            state.down_blue_exception_filter_version = config.TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION
            state.down_blue_exception_filter_enabled_at = datetime.now(KST).isoformat()
            state.down_blue_exception_filter_enabled_by = str(changed_by or "ui")
            state_store.save_state(state)
        return {
            "ok": True,
            "down_blue_exception_filter_enabled": enabled_bool,
            "previous": prev,
            "down_blue_exception_filter_enabled_at": state.down_blue_exception_filter_enabled_at,
            "down_blue_exception_filter_enabled_by": state.down_blue_exception_filter_enabled_by,
            "down_blue_exception_filter_version": state.down_blue_exception_filter_version,
        }

    def set_no_filter_0900_1100_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional "무필터 09:00-11:00" 즉시청산
        진입모드 (2026-08-20) -- restricts the pre-existing legacy (TW-off)
        immediate-entry/immediate-reversal-exit path to 09:00-11:00 new
        entries only (see worker._entry_gate_block_reason); the legacy
        path's own always-immediate reversal-sell/STOP_LOSS/QUICK_PROFIT/
        FORCED_LIQUIDATION behavior is completely unchanged. Only updates
        runtime state -- never places orders. Mutually exclusive with
        time_window_filter_enabled (TW wins if both are on).
        """
        with _LOCK:
            state = state_store.load_state()
            enabled_bool = bool(enabled)
            prev = bool(state.no_filter_0900_1100_enabled)
            state.no_filter_0900_1100_enabled = enabled_bool
            state.no_filter_0900_1100_filter_version = config.NO_FILTER_0900_1100_FILTER_VERSION
            state.no_filter_0900_1100_enabled_at = datetime.now(KST).isoformat()
            state.no_filter_0900_1100_enabled_by = str(changed_by or "ui")
            state_store.save_state(state)
        return {
            "ok": True,
            "no_filter_0900_1100_enabled": enabled_bool,
            "previous": prev,
            "no_filter_0900_1100_enabled_at": state.no_filter_0900_1100_enabled_at,
            "no_filter_0900_1100_enabled_by": state.no_filter_0900_1100_enabled_by,
            "no_filter_0900_1100_filter_version": state.no_filter_0900_1100_filter_version,
        }

    def set_entry_paused(self, enabled: bool) -> dict[str, Any]:
        """UI command: pause/resume NEW entries only (2026-08-14) -- MU price
        collection (WS/1m bars), the worker tick loop, MACD flag detection/
        signal-ledger recording, and existing-position management (stop
        loss/quick profit/forced liquidation/reconcile) all keep running
        unaffected; see worker._entry_gate_block_reason /
        config.BLOCK_ENTRY_PAUSED_BY_USER. Shares _LOCK with _run_loop for
        the same reason set_quick_profit_enabled does -- see its docstring.
        """
        with _LOCK:
            state = state_store.load_state()
            enabled_bool = bool(enabled)
            prev = bool(state.entry_paused)
            state.entry_paused = enabled_bool
            state_store.save_state(state)
        return {"ok": True, "entry_paused": enabled_bool, "previous": prev}

    def manual_entry(self, direction: str) -> dict[str, Any]:
        """UI 수동 진입 버튼 ("현재시점 레드(레버리지)/블루(인버스) 전량매수")
        — 2026-08-13 추가, macd2.service.manual_entry와 동일한 정책을 그대로
        따른다. MU MACD 신호 확정(3분봉 크로스오버)을 전혀 거치지 않고,
        지정한 방향의 ETF를 현재 예산 내에서 즉시 시장가 매수한다
        (order_executor.execute_signal 재사용 — 별도 매수/사이징 로직 재구현
        없음, ledger_module=MU_MACD 자신의 ledger 모듈이라 체결이 macd2
        원장이 아니라 mu_macd_execution_ledger.csv에 기록된다). 이미
        포지션을 보유 중이면 거부하고 아무 것도 하지 않는다(전량매도 후
        스위칭은 이 버튼의 범위 밖 — 먼저 수동 전량청산을 누른 뒤 다시
        호출해야 한다). 체결 성공 시 이후의 손절/퀵프로핏/반대플래그청산/
        강제청산은 전부 기존 run_once가 매 tick(WORKER_INTERVAL_SEC=2초)마다
        정상적으로 이 포지션을 관리한다 — state.position이 어떻게
        채워졌는지 worker.py는 구분하지 않는다."""
        if direction not in (Direction.UP_RED.value, Direction.DOWN_BLUE.value):
            return {"ok": False, "message": "INVALID_DIRECTION"}
        if not self.is_alive():
            return {"ok": False, "message": "WORKER_NOT_RUNNING"}
        if self._broker is None:
            return {"ok": False, "message": "NOT_STARTED"}

        # 2026-08-13 fix: shares _LOCK with _run_loop/set_quick_profit_enabled
        # (see set_quick_profit_enabled's docstring) -- without it, a worker
        # tick's own load-run_once-save cycle could clobber this button's
        # state.position update with a stale save.
        with _LOCK:
            state = state_store.load_state()
            if not state.auto_trade_on:
                return {"ok": False, "message": "AUTO_TRADE_OFF"}
            if state.position is not None and state.position.quantity > 0:
                return {"ok": False, "message": "ALREADY_HOLDING_POSITION"}

            direction_enum = Direction(direction)
            target_symbol = order_executor.target_symbol_for_direction(direction_enum)
            now = datetime.now(KST)
            quote = self._broker.get_quote(target_symbol) if hasattr(self._broker, "get_quote") else None
            if not quote:
                return {"ok": False, "message": "QUOTE_UNAVAILABLE"}

            signal_id = f"MANUAL_{direction}_{now.strftime('%Y%m%d%H%M%S')}"
            outcome = order_executor.execute_signal(
                broker=self._broker, direction=direction_enum, signal_id=signal_id,
                quotes={target_symbol: float(quote)}, position=None, budget=state.budget,
                ledger_module=ledger,
            )

            if outcome.final_state == SignalState.EXECUTED:
                state.position = PositionSnapshot(
                    symbol=target_symbol, quantity=outcome.quantity,
                    avg_price=outcome.filled_avg_price or 0.0, entry_at=now,
                )
            else:
                state.order_block_reason = outcome.block_reason
            _record_manual_signal(
                state, signal_id=signal_id, signal_type="MANUAL_ENTRY", direction=direction_enum,
                now=now, order_result=outcome.final_state.value, block_reason=outcome.block_reason,
            )
            state_store.save_state(state)

        return {
            "ok": outcome.final_state == SignalState.EXECUTED,
            "final_state": outcome.final_state.value,
            "block_reason": outcome.block_reason,
            "symbol": target_symbol,
            "quantity": outcome.quantity,
            "price": outcome.filled_avg_price or outcome.order_price,
        }

    def manual_exit(self) -> dict[str, Any]:
        """수동 진입 버튼과 짝을 이루는 "현재 보유물량 전량청산" 버튼
        (2026-08-13 추가) — 현재 보유 중인 포지션을 지금 즉시 시장가로
        전량 매도한다(order_executor.execute_exit 재사용 — STOP_LOSS/
        FORCED_LIQUIDATION과 동일한, 이미 검증된 매도 경로,
        exit_reason=EXIT_MANUAL_LIQUIDATION). auto_trade_on은 그대로
        두므로 다음 확정 신호부터 기존 run_once가 계속 정상적으로
        감시/매매한다."""
        if not self.is_alive():
            return {"ok": False, "message": "WORKER_NOT_RUNNING"}
        if self._broker is None:
            return {"ok": False, "message": "NOT_STARTED"}

        # 2026-08-13 fix: see manual_entry's comment -- same _LOCK sharing
        # against _run_loop's own load-run_once-save cycle.
        with _LOCK:
            state = state_store.load_state()
            if not state.auto_trade_on:
                return {"ok": False, "message": "AUTO_TRADE_OFF"}
            if state.position is None or state.position.quantity <= 0:
                return {"ok": False, "message": "NO_POSITION_TO_SELL"}

            pos = state.position
            now = datetime.now(KST)
            signal_id = f"MANUAL_EXIT_{pos.symbol}_{now.strftime('%Y%m%d%H%M%S')}"
            outcome = order_executor.execute_exit(
                broker=self._broker, symbol=pos.symbol, quantity=pos.quantity,
                exit_reason=config.EXIT_MANUAL_LIQUIDATION, entry_price=pos.avg_price,
                ledger_module=ledger,
            )

            if pos.symbol == config.LONG_SYMBOL:
                direction: Optional[Direction] = Direction.UP_RED
            elif pos.symbol == config.INVERSE_SYMBOL:
                direction = Direction.DOWN_BLUE
            else:
                direction = None

            if outcome.final_state == SignalState.EXECUTED:
                state.position = None
            else:
                state.order_block_reason = outcome.block_reason
            _record_manual_signal(
                state, signal_id=signal_id, signal_type="MANUAL_LIQUIDATION", direction=direction,
                now=now, order_result=outcome.final_state.value, block_reason=outcome.block_reason,
            )
            state_store.save_state(state)

        return {
            "ok": outcome.final_state == SignalState.EXECUTED,
            "final_state": outcome.final_state.value,
            "block_reason": outcome.block_reason,
            "symbol": pos.symbol,
            "quantity": pos.quantity,
            "price": outcome.sell_result.executed_price if outcome.sell_result else None,
        }

    def _auto_recover_worker(self, state: RuntimeState) -> bool:
        """2026-08-13 fix (real incident: a held position rode a real loss
        past STOP_LOSS_NET_PCT and a confirmed flag with neither ever
        acting, because the process had restarted -- Render idle-sleep or
        a redeploy -- and nobody had clicked "시작" again since; MU_MACD's
        Worker/broker/market-data are plain in-process attributes with no
        persistence, so run_once() simply stopped executing entirely).
        Mirrors macd2.service._auto_recover_worker exactly: retries the
        same start() path automatically -- MOCK mode only, REAL mode must
        always go through the UI's explicit confirm-text re-entry, never
        silently reactivated -- rate-limited by
        config.WORKER_AUTO_RECOVER_COOLDOWN_SEC so a persistently-failing
        bootstrap can't hammer KIS on every UI auto-refresh tick. Returns
        True if a live worker resulted."""
        if state.mode != "mock":
            return False
        last_attempt = None
        if state.last_auto_recover_attempt_at:
            try:
                last_attempt = datetime.fromisoformat(state.last_auto_recover_attempt_at)
            except ValueError:
                last_attempt = None
        now = datetime.now(KST)
        if last_attempt is not None and (now - last_attempt).total_seconds() < config.WORKER_AUTO_RECOVER_COOLDOWN_SEC:
            return False
        state.last_auto_recover_attempt_at = now.isoformat()
        state_store.save_state(state)
        result = self.start(mode=state.mode, budget=state.budget)
        return bool(result.get("ok")) and self.is_alive()

    def stop(self) -> dict[str, Any]:
        with _LOCK:
            self._stop_worker_and_market_data_locked()
            self._stop_flags_only()
            state = state_store.load_state()
            state.auto_trade_on = False
            state_store.save_state(state)
            return {"ok": True}

    def _stop_worker_and_market_data_locked(self) -> None:
        """Tears down the current worker thread + its market_data. Never
        acquires _LOCK itself -- callers (stop(), and start() re-starting
        an already-alive worker) already hold it, and rely on the same
        "loop only re-checks its stop event outside the lock" pattern
        _run_loop uses so join() below can never deadlock. Deliberately
        never nulls self._stop_event (see _stop_flags_only's docstring for
        why -- the same race applies here)."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5.0)
        if self._market_data is not None:
            self._market_data.stop()
        self._worker_thread = None

    def _stop_flags_only(self) -> None:
        """Never acquires _LOCK itself -- callers (start()/stop(), both
        already holding it) rely on the exact same "loop only re-checks its
        stop event outside the lock" pattern _run_loop/stop() already use,
        so join() below can never deadlock against a lock the caller holds.

        Deliberately never nulls self._flags_only_stop_event (mirrors
        _run_loop/stop() never nulling self._stop_event either) -- if
        join() times out because the loop thread is currently blocked
        acquiring _LOCK (held by this very call's caller), the thread will
        still dereference self._flags_only_stop_event once it finally gets
        the lock and finishes that one last tick; nulling it here raced
        that dereference into an AttributeError in practice.
        """
        if self._flags_only_stop_event is not None:
            self._flags_only_stop_event.set()
        if self._flags_only_thread is not None:
            self._flags_only_thread.join(timeout=5.0)
        if self._flags_only_market_data is not None:
            self._flags_only_market_data.stop()
        self._flags_only_thread = None
        self._flags_only_market_data = None

    def _auto_recover_flags_only(self, state: RuntimeState) -> bool:
        """2026-08-14: when _auto_recover_worker refuses (REAL mode's
        KisRealBroker can't even be constructed without the human
        re-entering the confirm phrase, so real order/reconcile authority
        genuinely cannot come back on its own), MU price collection + MACD
        flag detection don't need that broker at all -- keep them alive via
        a broker-less worker.run_flags_only() loop, so the dashboard/signal
        ledger keep showing real flags forming while order authority stays
        paused for re-authentication. No-op (returns False) for mode=="mock"
        -- mock's own _auto_recover_worker already brings back a full
        worker+broker, no shadow needed. Idempotent: a no-op if the shadow
        is already running."""
        if state.mode == "mock":
            return False
        with _LOCK:
            if self._flags_only_alive():
                return True
            if self.is_alive():
                return False  # the real worker came back between the caller's check and now
            self._flags_only_market_data = MUMarketDataService(mode="real")
            self._flags_only_market_data.load_today_bars()
            self._flags_only_market_data.start()
            self._flags_only_stop_event = threading.Event()
            self._flags_only_thread = threading.Thread(
                target=self._run_flags_only_loop, daemon=True, name="mu-macd-flags-only",
            )
            self._flags_only_thread.start()
            return True

    def _run_flags_only_loop(self) -> None:
        while self._flags_only_stop_event is not None and not self._flags_only_stop_event.is_set():
            try:
                with _LOCK:
                    state = state_store.load_state()
                    if (
                        state.auto_trade_on and not self.is_alive()
                        and self._flags_only_market_data is not None
                    ):
                        now = datetime.now(KST)
                        worker.run_flags_only(market_data=self._flags_only_market_data, state=state, now=now)
                        state_store.save_state(state)
            except Exception as exc:  # pragma: no cover - defensive top-level guard
                try:
                    with _LOCK:
                        state = state_store.load_state()
                        state.order_block_reason = f"FLAGS_ONLY_LOOP_ERROR:{exc!r}"
                        state_store.save_state(state)
                except Exception:
                    pass
            self._flags_only_stop_event.wait(WORKER_INTERVAL_SEC)

    def _run_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                # 2026-08-13 fix: this whole load-run_once-save cycle now
                # shares _LOCK with set_quick_profit_enabled/manual_entry/
                # manual_exit -- see set_quick_profit_enabled's docstring
                # for the lost-update race this closes. Held for the
                # duration of one tick (including run_once's broker calls),
                # so a UI mutation waits at most ~one tick instead of ever
                # racing a stale save.
                with _LOCK:
                    state = state_store.load_state()
                    if state.auto_trade_on and self._broker is not None and self._market_data is not None:
                        now = datetime.now(KST)
                        worker.run_once(broker=self._broker, market_data=self._market_data, state=state, now=now)
                        state_store.save_state(state)
            except Exception as exc:  # pragma: no cover - defensive top-level guard
                try:
                    with _LOCK:
                        state = state_store.load_state()
                        state.order_block_reason = f"WORKER_LOOP_ERROR:{exc!r}"
                        state_store.save_state(state)
                except Exception:
                    pass
            self._stop_event.wait(WORKER_INTERVAL_SEC)

    def status(self) -> dict[str, Any]:
        state = state_store.load_state()
        if state.auto_trade_on and not self.is_alive():
            if self._auto_recover_worker(state):
                state = state_store.load_state()
            else:
                # 2026-08-14: REAL mode can't auto-recover the broker itself
                # (requires the human's confirm phrase), but MU price
                # collection + flag detection can keep running in the
                # meantime -- see _auto_recover_flags_only's docstring.
                self._auto_recover_flags_only(state)
        elif self.is_alive():
            self._stop_flags_only()  # the real worker is up -- no shadow needed
        return {
            "auto_trade_on": state.auto_trade_on, "mode": state.mode, "budget": state.budget,
            "position": state.position, "worker_alive": self.is_alive(),
            "flags_only_active": self._flags_only_alive(),
            "quick_profit_enabled": state.quick_profit_enabled,
            "entry_paused": state.entry_paused,
            "ws_connected": state.ws_connected, "ws_last_tick_at": state.ws_last_tick_at,
            "ws_last_error": state.ws_last_error,
            "warmup_bars_1m_count": state.warmup_bars_1m_count,
            "warmup_bars_3m_count": state.warmup_bars_3m_count, "warmup_ready": state.warmup_ready,
            "last_mu_price": state.last_mu_price,
            "last_long_etf_price": state.last_long_etf_price, "last_inverse_etf_price": state.last_inverse_etf_price,
            "last_etf_quote_at": state.last_etf_quote_at,
            "last_flag_display_time": state.last_flag_display_time,
            "last_flag_direction": state.last_flag_direction, "order_block_reason": state.order_block_reason,
            "time_window_filter_enabled": state.time_window_filter_enabled,
            "time_window_position_active": state.time_window_position_active,
            "time_window_tp1_done": state.time_window_tp1_done,
            "time_window_morning_entry_count": state.time_window_morning_entry_count,
            "time_window_afternoon_entry_count": state.time_window_afternoon_entry_count,
            "last_time_window_score": state.last_time_window_score,
            "last_time_window_decision": state.last_time_window_decision,
            "last_time_window_block_reason": state.last_time_window_block_reason,
            "down_blue_exception_filter_enabled": state.down_blue_exception_filter_enabled,
            "daily_down_blue_exception_used": state.daily_down_blue_exception_used,
            "no_filter_0900_1100_enabled": state.no_filter_0900_1100_enabled,
        }


_service_singleton: Optional[MUMacdService] = None


def get_service() -> MUMacdService:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = MUMacdService()
    return _service_singleton
