"""MU_MACD service — lifecycle control (start/stop), wraps its own worker
thread + its own MUMarketDataService WebSocket thread. Uses its OWN
threading.Lock (config.LOCK_FILENAME is documented but the actual mutual-
exclusion primitive is this in-process lock — a single process only ever
runs one MU_MACD worker thread) — never macd2's or tsla_auto's lock/thread.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from app.trading.macd2 import order_executor
from app.trading.macd2.broker_adapter import create_macd2_broker
from app.trading.macd2.models import SignalState
from app.trading.mu_macd import config, ledger, state_store, worker
from app.trading.mu_macd.market_data import MUMarketDataService
from app.trading.mu_macd.models import Direction, PositionSnapshot, RuntimeState

KST = config.KST
WORKER_INTERVAL_SEC = 2.0

_LOCK = threading.Lock()  # MU_MACD's own in-process lock — never shared with macd2/tsla_auto


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
        self._broker = None
        self._market_data: Optional[MUMarketDataService] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None

    def is_alive(self) -> bool:
        return bool(self._worker_thread and self._worker_thread.is_alive())

    def start(
        self, *, mode: str = "mock", budget: float = config.DEFAULT_BUDGET,
        confirm_text: str = "", runtime_enable_real_buy: bool = False, runtime_enable_real_sell: bool = False,
    ) -> dict[str, Any]:
        """mode="real" requires the SAME explicit opt-in macd2 itself requires
        (confirm_text + runtime_enable_real_buy/sell, both default OFF) —
        never silently placing a live order. Per current instructions, REAL
        orders remain disabled until explicitly validated further."""
        with _LOCK:
            if self.is_alive():
                return {"ok": False, "message": "ALREADY_RUNNING"}

            state = state_store.load_state()
            state.mode = mode
            state.budget = budget
            state.auto_trade_on = True
            state.worker_instance_id = uuid.uuid4().hex[:12]
            state.worker_started_at = datetime.now(KST).isoformat()
            state_store.save_state(state)

            broker_kwargs = {} if mode == "mock" else {
                "confirm_text": confirm_text, "runtime_real_mode": True,
                "runtime_enable_real_buy": runtime_enable_real_buy, "runtime_enable_real_sell": runtime_enable_real_sell,
            }
            self._broker = create_macd2_broker(mode, **broker_kwargs)
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
        on the very next tick without a service restart."""
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.quick_profit_enabled)
        state.quick_profit_enabled = enabled_bool
        state_store.save_state(state)
        return {"ok": True, "quick_profit_enabled": enabled_bool, "previous": prev}

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

    def stop(self) -> dict[str, Any]:
        with _LOCK:
            if self._stop_event is not None:
                self._stop_event.set()
            if self._worker_thread is not None:
                self._worker_thread.join(timeout=5.0)
            if self._market_data is not None:
                self._market_data.stop()
            state = state_store.load_state()
            state.auto_trade_on = False
            state_store.save_state(state)
            self._worker_thread = None
            return {"ok": True}

    def _run_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                state = state_store.load_state()
                if state.auto_trade_on and self._broker is not None and self._market_data is not None:
                    now = datetime.now(KST)
                    worker.run_once(broker=self._broker, market_data=self._market_data, state=state, now=now)
                    state_store.save_state(state)
            except Exception as exc:  # pragma: no cover - defensive top-level guard
                try:
                    state = state_store.load_state()
                    state.order_block_reason = f"WORKER_LOOP_ERROR:{exc!r}"
                    state_store.save_state(state)
                except Exception:
                    pass
            self._stop_event.wait(WORKER_INTERVAL_SEC)

    def status(self) -> dict[str, Any]:
        state = state_store.load_state()
        return {
            "auto_trade_on": state.auto_trade_on, "mode": state.mode, "budget": state.budget,
            "position": state.position, "worker_alive": self.is_alive(),
            "quick_profit_enabled": state.quick_profit_enabled,
            "ws_connected": state.ws_connected, "ws_last_tick_at": state.ws_last_tick_at,
            "ws_last_error": state.ws_last_error,
            "warmup_bars_1m_count": state.warmup_bars_1m_count,
            "warmup_bars_3m_count": state.warmup_bars_3m_count, "warmup_ready": state.warmup_ready,
            "last_mu_price": state.last_mu_price,
            "last_long_etf_price": state.last_long_etf_price, "last_inverse_etf_price": state.last_inverse_etf_price,
            "last_etf_quote_at": state.last_etf_quote_at,
            "last_flag_display_time": state.last_flag_display_time,
            "last_flag_direction": state.last_flag_direction, "order_block_reason": state.order_block_reason,
        }


_service_singleton: Optional[MUMacdService] = None


def get_service() -> MUMacdService:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = MUMacdService()
    return _service_singleton
