"""MACD2 lifecycle service — single entry point (docs §14).

start()/stop()/get_snapshot()/supervisor_status() own the full lifecycle:
quote-cache-ready -> bootstrap -> Worker start, in that order. The quote
updater is started before bootstrap runs and kept running even if bootstrap
fails (docs §21 2026-07-24 bootstrap fix: 현재가 조회와 bootstrap 생명주기
분리) — a data-collection failure blocks signal/order evaluation only, never
live price display. The Worker is never started before bootstrap succeeds,
and order authority (``auto_trade_on``) is never opened before that (docs
§14). ``retry_bootstrap()`` lets the UI retry bootstrap without spawning a
new thread or reconstructing the broker/market-data service.

Mutual exclusion with Enhanced / MACD v1 (docs §15) is delegated to
``app.trading.strategy_ownership`` — a shared, read-only adapter that checks
each system's real ``auto_trade_on`` state AND a freshness check on that
system's own heartbeat/tick timestamp (a crashed process with a stuck flag
is not treated as active). MACD v1's runtime file is read as plain JSON by
that adapter (never via importing MACD v1 production code, and never written
by MACD2). Enhanced and MACD v1 now also check MACD2 back through the same
adapter — closing the one-way limitation an earlier version of this module
had (see docs §15 / the final report).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.trading import strategy_ownership
from app.trading.macd2 import config, state_store
from app.trading.macd2.broker_adapter import create_macd2_broker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import RuntimeStatus
from app.trading.macd2.worker import Macd2Worker

KST = config.KST


def other_strategy_active() -> tuple[bool, str]:
    """docs §15: block MACD2 start if Enhanced or MACD v1 is really active."""
    return strategy_ownership.other_owner_active(strategy_ownership.MACD2)


class Macd2Service:
    """Owns the MarketDataService/broker/Worker for one MACD2 run."""

    def __init__(self) -> None:
        self._market_data: Optional[MarketDataService] = None
        self._broker = None
        self._worker: Optional[Macd2Worker] = None
        self._bootstrap_attempts: int = 0
        self._last_bootstrap_at: Optional[str] = None
        self._last_bootstrap_result: Optional[dict[str, Any]] = None

    def start(
        self,
        *,
        mode: str = "mock",
        budget: float = config.DEFAULT_BUDGET,
        real_kwargs: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if self._worker is not None and self._worker.is_alive():
            return {"ok": False, "message": "ALREADY_RUNNING"}

        active, reason = other_strategy_active()
        if active:
            state = state_store.load_state()
            state.order_block_reason = reason
            state_store.save_state(state)
            return {"ok": False, "message": reason}

        state = state_store.load_state()
        state.mode = mode
        state.budget = float(budget)
        state.stopped = False
        state.stopped_reason = None
        state.order_block_reason = None
        state.ui_mode = RuntimeStatus.BOOTSTRAPPING
        state_store.save_state(state)

        try:
            self._broker = create_macd2_broker(mode, **(real_kwargs or {}))
        except Exception as exc:
            state = state_store.load_state()
            state.ui_mode = RuntimeStatus.DATA_ERROR
            state.order_block_reason = f"BROKER_CREATE_FAILED:{exc}"
            state_store.save_state(state)
            return {"ok": False, "message": str(exc)}

        self._market_data = MarketDataService(mode=mode)
        self._bootstrap_attempts = 0
        self._last_bootstrap_at = None
        self._last_bootstrap_result = None

        # Quote lifecycle is independent of bootstrap (docs §21): get an
        # initial read and start the background updater regardless of
        # whether history bootstrap succeeds below, so live prices are never
        # blocked by a data-collection failure.
        try:
            self._market_data.refresh_quotes()
        except Exception:
            pass  # per-symbol errors surface via get_quote()/QuoteSnapshot.error
        self._market_data.start_quote_updater(interval_sec=1.0)

        return self._attempt_bootstrap()

    def retry_bootstrap(self) -> dict[str, Any]:
        """Manual bootstrap retry (docs §21: 재시도 버튼) — reuses the
        existing broker/MarketDataService/quote updater; never spawns a new
        thread. No-op if the Worker is already running."""
        if self._market_data is None or self._broker is None:
            return {"ok": False, "message": "NOT_STARTED"}
        if self._worker is not None and self._worker.is_alive():
            return {"ok": True, "message": "ALREADY_RUNNING"}
        return self._attempt_bootstrap()

    def _attempt_bootstrap(self) -> dict[str, Any]:
        self._bootstrap_attempts += 1
        now = datetime.now(KST)
        self._last_bootstrap_at = now.isoformat()
        boot = self._market_data.bootstrap(now=now)
        self._last_bootstrap_result = dict(boot.__dict__)

        state = state_store.load_state()
        state.warmup_ready = boot.ok
        if not boot.ok:
            state.ui_mode = RuntimeStatus.DATA_ERROR
            state.order_block_reason = f"WARMUP_BOOTSTRAP:{boot.reason}"
            state_store.save_state(state)
            # Worker/order loop never starts — quote updater keeps running.
            return {"ok": False, "message": boot.reason, "bootstrap": boot.__dict__}

        state.ui_mode = RuntimeStatus.READY
        state_store.save_state(state)

        # auto_trade_on/RUNNING must be persisted BEFORE the Worker thread
        # starts — the thread's own first tick calls load_state()/save_state()
        # concurrently, and starting it first would race a stale READY state
        # back over this one.
        state.auto_trade_on = True
        state.ui_mode = RuntimeStatus.RUNNING
        state_store.save_state(state)

        self._market_data.start_history_updater(interval_sec=config.WORKER_INTERVAL_SEC)
        self._worker = Macd2Worker(
            broker=self._broker, market_data=self._market_data,
            get_state=state_store.load_state, save_state=state_store.save_state,
        )
        self._worker.start()
        return {"ok": True, "bootstrap": boot.__dict__}

    def stop(self, reason: str = "user_stop") -> dict[str, Any]:
        if self._worker is not None:
            self._worker.stop(join_timeout=5.0)
        if self._market_data is not None:
            self._market_data.stop_quote_updater(join_timeout=2.0)
            self._market_data.stop_history_updater(join_timeout=2.0)

        state = state_store.load_state()
        state.auto_trade_on = False
        state.stopped = True
        state.stopped_reason = reason
        state.ui_mode = RuntimeStatus.STOPPED
        state_store.save_state(state)
        return {"ok": True}

    def get_snapshot(self) -> dict[str, Any]:
        state = state_store.load_state()
        quotes: dict[str, Any] = {}
        if self._market_data is not None:
            for symbol in (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL):
                quotes[symbol] = self._market_data.get_quote(symbol)
        return {
            "state": state,
            "worker": self._worker.tick_stats() if self._worker is not None else None,
            "quotes": quotes,
            "bootstrap_diag": self._market_data.get_last_bootstrap_diag() if self._market_data is not None else {},
            "bootstrap_attempts": self._bootstrap_attempts,
            "bootstrap_last_attempt_at": self._last_bootstrap_at,
            "bootstrap_last_result": self._last_bootstrap_result,
        }

    def supervisor_status(self) -> dict[str, Any]:
        stats = self._worker.tick_stats() if self._worker is not None else {}
        worker_alive = bool(self._worker and self._worker.is_alive())
        return {
            "worker_alive": worker_alive,
            "active_worker_count": 1 if worker_alive else 0,
            "quote_updater_alive": bool(self._market_data and self._market_data.quote_updater_alive()),
            "history_updater_alive": bool(self._market_data and self._market_data.history_updater_alive()),
            "bootstrap_attempts": self._bootstrap_attempts,
            "bootstrap_last_attempt_at": self._last_bootstrap_at,
            **stats,
        }


_service_instance: Optional[Macd2Service] = None


def get_service() -> Macd2Service:
    """Process-level singleton — the UI must call this, never construct its
    own Macd2Service/Worker/MarketDataService (docs §14/§16)."""
    global _service_instance
    if _service_instance is None:
        _service_instance = Macd2Service()
    return _service_instance
