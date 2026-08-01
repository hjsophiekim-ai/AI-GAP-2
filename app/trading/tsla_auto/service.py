"""TSLA_AUTO lifecycle service — single entry point (docs §3/§13/§15).

start()/stop()/get_snapshot()/supervisor_status() own the full lifecycle.
Never imports app.trading.macd2.* (service/worker/state_store/ledger/
broker_adapter/order_executor) and never participates in
app.trading.strategy_ownership's domestic 3-way mutual exclusion — TSLA_AUTO
trades no domestic symbol, so it is not a domestic order-authority
competitor (docs §13).

Command file (data/commands/tsla_auto/) records every start/stop/filter-toggle
command for auditability — UI never mutates Worker/order state directly,
only calls these methods (docs §15).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.tsla_auto import config, market_session, order_executor, state_store
from app.trading.tsla_auto.broker_adapter import create_tsla_auto_broker
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.models import RuntimeStatus
from app.trading.tsla_auto.signal_engine import calculate_macd, resample_completed_3m
from app.trading.tsla_auto.worker import TslaAutoWorker, compute_today_signal_overview, git_sha, initialize_strategy_session
from app.utils.data_paths import data_path

ET = config.ET

COMMANDS_DIR: Path = data_path("commands", "tsla_auto")
COMMANDS_PATH: Path = COMMANDS_DIR / "tsla_auto_commands.jsonl"
LOCK_DIR: Path = data_path("runtime", "tsla_auto")
LOCK_PATH: Path = LOCK_DIR / config.WORKER_LOCK_FILENAME


def _write_command(action: str, **fields: Any) -> None:
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"action": action, "at": datetime.now(ET).isoformat(), "strategy_id": config.STRATEGY_ID, **fields}
    with open(COMMANDS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class TslaAutoService:
    """Owns the MarketDataService/broker/Worker for one TSLA_AUTO run."""

    def __init__(self) -> None:
        self._market_data: Optional[MarketDataService] = None
        self._broker = None
        self._worker: Optional[TslaAutoWorker] = None
        self._lock = threading.RLock()
        self._bootstrap_attempts = 0
        self._last_bootstrap_at: Optional[str] = None
        self._last_bootstrap_result: Optional[dict[str, Any]] = None

    def _acquire_lock(self) -> None:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategy_id": config.STRATEGY_ID, "worker_name": config.WORKER_NAME,
            "pid": os.getpid(), "acquired_at": datetime.now(ET).isoformat(),
        }
        LOCK_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _release_lock(self) -> None:
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    def start(self, *, mode: str, budget_usd: float, real_kwargs: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self._lock:
            state = state_store.load_state()
            state.mode = mode
            state.budget_usd = float(budget_usd)

            if mode == "READ_ONLY":
                # docs §18 Phase 2: READ_ONLY connects to the REAL, confirmed
                # KIS overseas quote/minute-candle endpoints (kis_overseas_adapter)
                # to genuinely validate live data access — but constructs no
                # broker at all, so no order/balance call is ever possible.
                self._market_data = MarketDataService(mode="REAL")
                self._broker = None
            else:
                self._market_data = MarketDataService(mode=mode)
                self._broker = create_tsla_auto_broker("mock" if mode == "MOCK" else "real", **(real_kwargs or {}))

            self._market_data.start_quote_updater(interval_sec=1.0)
            now = datetime.now(ET)
            boot = self._market_data.bootstrap(now=now)
            self._bootstrap_attempts += 1
            self._last_bootstrap_at = now.isoformat()
            self._last_bootstrap_result = {"ok": boot.ok, "reason": boot.reason}

            if mode == "READ_ONLY":
                state.auto_trade_on = False
                state.ui_mode = RuntimeStatus.READY
                state_store.save_state(state)
                _write_command("start", mode=mode, ok=False, reason="READ_ONLY_NO_TRADE")
                return {"ok": True, "reason": boot.reason}

            self._market_data.start_history_updater(interval_sec=config.WORKER_INTERVAL_SEC)
            worker_id = None
            self._worker = TslaAutoWorker(
                broker=self._broker, market_data=self._market_data,
                get_state=state_store.load_state, save_state=state_store.save_state,
            )
            initialize_strategy_session(state, self._market_data, now=now, worker_instance_id=self._worker.instance_id)
            state.auto_trade_on = True
            state.ui_mode = RuntimeStatus.RUNNING if boot.ok else RuntimeStatus.BOOTSTRAPPING
            state.stopped = False
            state_store.save_state(state)
            self._acquire_lock()
            self._worker.start()
            _write_command("start", mode=mode, ok=True, bootstrap_ok=boot.ok, reason=boot.reason)
            return {"ok": True, "reason": boot.reason}

    def stop(self, *, liquidate: bool = False) -> dict[str, Any]:
        with self._lock:
            state = state_store.load_state()
            state.auto_trade_on = False
            state.stopped = True
            state.ui_mode = RuntimeStatus.STOPPED
            if liquidate and state.position is not None and self._broker is not None:
                outcome = order_executor.execute_exit(
                    broker=self._broker, symbol=state.position.symbol, quantity=state.position.quantity,
                    exit_reason=config.EXIT_USER_LIQUIDATION, entry_price=state.position.avg_price,
                )
                if outcome.final_state.value == "EXECUTED":
                    state.position = None
            state_store.save_state(state)
            if self._worker is not None:
                self._worker.stop()
            if self._market_data is not None:
                self._market_data.stop_quote_updater(join_timeout=2.0)
                self._market_data.stop_history_updater(join_timeout=2.0)
            self._release_lock()
            _write_command("stop", liquidate=liquidate)
            return {"ok": True}

    def set_strong_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        with self._lock:
            state = state_store.load_state()
            state.strong_filter_enabled = bool(enabled)
            state.strong_filter_enabled_at = datetime.now(ET).isoformat()
            state.strong_filter_enabled_by = changed_by
            state_store.save_state(state)
            _write_command("set_strong_filter_enabled", enabled=bool(enabled), changed_by=changed_by)
            return {"ok": True, "strong_filter_enabled_at": state.strong_filter_enabled_at}

    def retry_bootstrap(self) -> dict[str, Any]:
        with self._lock:
            if self._market_data is None:
                return {"ok": False, "reason": "NOT_STARTED"}
            now = datetime.now(ET)
            boot = self._market_data.bootstrap(now=now)
            self._bootstrap_attempts += 1
            self._last_bootstrap_at = now.isoformat()
            self._last_bootstrap_result = {"ok": boot.ok, "reason": boot.reason}
            return {"ok": boot.ok, "reason": boot.reason}

    def get_snapshot(self) -> dict[str, Any]:
        state = state_store.load_state()
        quotes: dict[str, Any] = {}
        if self._market_data is not None:
            for symbol in (config.SIGNAL_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL):
                quotes[symbol] = self._market_data.get_quote(symbol)
        quote_status = self._market_data.quote_status() if self._market_data is not None else "DEAD"
        primary_macd = None
        primary_signal = None
        today_signal_overview: list[dict[str, Any]] = []
        if self._market_data is not None:
            try:
                df_1m = self._market_data.get_history_df()
                now = datetime.now(ET)
                snap = calculate_macd(resample_completed_3m(df_1m, now=now))
                if snap is not None:
                    primary_macd = snap.macd
                    primary_signal = snap.signal
                today_signal_overview = compute_today_signal_overview(df_1m, now=now, session_started_at=state.session_started_at)
            except Exception:
                pass
        return {
            "state": state, "worker": self._worker.tick_stats() if self._worker is not None else None,
            "quotes": quotes, "quote_status": quote_status, "primary_macd": primary_macd, "primary_signal": primary_signal,
            "today_signal_overview": today_signal_overview, "worker_code_sha": git_sha(),
            "us_market_state": market_session.get_us_market_state().to_dict(),
            "bootstrap_diag": self._market_data.get_last_bootstrap_diag() if self._market_data is not None else {},
            "bootstrap_attempts": self._bootstrap_attempts, "bootstrap_last_attempt_at": self._last_bootstrap_at,
            "bootstrap_last_result": self._last_bootstrap_result,
        }

    def supervisor_status(self) -> dict[str, Any]:
        stats = self._worker.tick_stats() if self._worker is not None else {}
        worker_alive = bool(self._worker and self._worker.is_alive())
        return {
            "worker_alive": worker_alive, "active_worker_count": 1 if worker_alive else 0,
            "quote_updater_alive": bool(self._market_data and self._market_data.quote_updater_alive()),
            "history_updater_alive": bool(self._market_data and self._market_data.history_updater_alive()),
            "bootstrap_attempts": self._bootstrap_attempts, "bootstrap_last_attempt_at": self._last_bootstrap_at,
            "lock_path": str(LOCK_PATH), "lock_exists": LOCK_PATH.exists(),
            **stats,
        }


_service_instance: Optional[TslaAutoService] = None


def get_service() -> TslaAutoService:
    """Process-level singleton — the UI must call this, never construct its
    own TslaAutoService/Worker/MarketDataService (docs §3)."""
    global _service_instance
    if _service_instance is None:
        _service_instance = TslaAutoService()
    return _service_instance
