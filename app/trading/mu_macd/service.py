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

from app.trading.macd2.broker_adapter import create_macd2_broker
from app.trading.mu_macd import config, ledger, state_store, worker
from app.trading.mu_macd.market_data import MUMarketDataService
from app.trading.mu_macd.models import RuntimeState

KST = config.KST
WORKER_INTERVAL_SEC = 2.0

_LOCK = threading.Lock()  # MU_MACD's own in-process lock — never shared with macd2/tsla_auto


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
