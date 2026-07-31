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
from app.trading.macd2 import config, order_executor, state_store
from app.trading.macd2.broker_adapter import create_macd2_broker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import RuntimeStatus
from app.trading.macd2.signal_engine import calculate_macd, resample_completed_3m
from app.trading.macd2.worker import Macd2Worker, compute_today_signal_overview, git_sha, initialize_strategy_session

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

    def _persist_worker_stall_if_needed(self, state):
        worker_alive = bool(self._worker and self._worker.is_alive())
        if state.auto_trade_on and not worker_alive:
            state.ui_mode = RuntimeStatus.WORKER_STALLED
            state.order_block_reason = "WORKER_THREAD_DEAD"
            state_store.save_state(state)
        return state

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
        self._market_data.start_history_updater(interval_sec=config.WORKER_INTERVAL_SEC)
        self._worker = Macd2Worker(
            broker=self._broker, market_data=self._market_data,
            get_state=state_store.load_state, save_state=state_store.save_state,
        )
        state = initialize_strategy_session(
            state, self._market_data, now=datetime.now(KST), worker_instance_id=self._worker.instance_id,
        )
        state.auto_trade_on = True
        state.ui_mode = RuntimeStatus.RUNNING
        state_store.save_state(state)
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

    def stop_and_liquidate_all(self, reason: str = "user_stop_liquidate_all") -> dict[str, Any]:
        """UI "자동매매 중지 및 일괄매도" 버튼 — Worker를 먼저 멈춰 더 이상 새
        신호로 매매하지 않도록 한 뒤, 그 시점에 실제로 보유 중인 모든
        TRADE_SYMBOLS 포지션을 order_executor.execute_exit로 시장가 매도한다
        (worker.py의 FORCED_LIQUIDATION과 동일한, 이미 검증된 매도 경로 재사용
        — 별도 매도 로직 재구현 없음). 브로커 조회 실패/미시작 상태에서도
        안전하게 실패를 보고한다."""
        if self._worker is not None:
            self._worker.stop(join_timeout=5.0)

        if self._broker is None:
            return {"ok": False, "message": "NOT_STARTED", "results": []}

        state = state_store.load_state()
        results: list[dict[str, Any]] = []
        try:
            raw_positions = list(self._broker.get_positions())
        except Exception as exc:
            raw_positions = []
            results.append({"symbol": None, "quantity": 0, "ok": False, "final_state": "FAILED", "block_reason": f"POSITIONS_FETCH_FAILED:{exc!r}"})

        for pos in raw_positions:
            symbol = str(getattr(pos, "symbol", "") or "")
            qty = int(getattr(pos, "quantity", 0) or 0)
            if symbol not in config.TRADE_SYMBOLS or qty <= 0:
                continue
            entry_price = float(getattr(pos, "avg_price", 0.0) or 0.0)
            if state.position is not None and state.position.symbol == symbol and state.position.avg_price:
                entry_price = float(state.position.avg_price)
            outcome = order_executor.execute_exit(
                broker=self._broker, symbol=symbol, quantity=qty,
                exit_reason=config.EXIT_USER_LIQUIDATION, entry_price=entry_price,
            )
            results.append({
                "symbol": symbol, "quantity": qty,
                "ok": outcome.final_state.value == "EXECUTED",
                "final_state": outcome.final_state.value,
                "block_reason": outcome.block_reason,
            })

        if self._market_data is not None:
            self._market_data.stop_quote_updater(join_timeout=2.0)
            self._market_data.stop_history_updater(join_timeout=2.0)

        all_ok = all(r.get("ok") for r in results) if results else True
        state = state_store.load_state()
        state.auto_trade_on = False
        state.stopped = True
        state.stopped_reason = reason
        state.ui_mode = RuntimeStatus.STOPPED
        if all_ok:
            state.position = None
            state.peak_net_return = 0.0
            state.profit_lock_active = False
        state_store.save_state(state)
        return {"ok": all_ok, "results": results}

    def set_major_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle optional MAJOR_FLAG order gate.

        Only updates runtime state — never places orders or liquidates.
        Takes effect from the next confirmed flag; open positions unchanged.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.major_filter_enabled)
        state.major_filter_enabled = enabled_bool
        state.major_filter_version = config.MAJOR_FILTER_VERSION
        state.major_filter_enabled_at = datetime.now(KST).isoformat()
        state.major_filter_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "major_filter_enabled": enabled_bool,
            "previous": prev,
            "major_filter_enabled_at": state.major_filter_enabled_at,
            "major_filter_enabled_by": state.major_filter_enabled_by,
            "major_filter_version": state.major_filter_version,
        }

    def get_snapshot(self) -> dict[str, Any]:
        state = state_store.load_state()
        state = self._persist_worker_stall_if_needed(state)
        quotes: dict[str, Any] = {}
        if self._market_data is not None:
            for symbol in (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL):
                quotes[symbol] = self._market_data.get_quote(symbol)
        quote_statuses = (
            self._market_data.quote_statuses()
            if self._market_data is not None and hasattr(self._market_data, "quote_statuses") else {}
        )
        quote_status = (
            self._market_data.quote_status()
            if self._market_data is not None and hasattr(self._market_data, "quote_status") else "DEAD"
        )
        primary_macd = None
        primary_signal = None
        today_signal_overview: list[dict[str, Any]] = []
        if self._market_data is not None:
            try:
                df_1m = self._market_data.get_history_df()
                now = datetime.now(KST)
                snap = calculate_macd(resample_completed_3m(df_1m, now=now))
                if snap is not None:
                    primary_macd = snap.macd
                    primary_signal = snap.signal
                # docs §3: recomputed, read-only "오늘 전체 신호" overview
                # (LIVE_CONFIRMED vs HISTORICAL_REPLAY_ONLY) — never touches
                # order_executor/major_flag_filter/processed_signal_ids.
                today_signal_overview = compute_today_signal_overview(
                    df_1m, now=now, session_started_at=state.session_started_at,
                )
            except Exception:
                pass
        return {
            "state": state,
            "worker": self._worker.tick_stats() if self._worker is not None else None,
            "quotes": quotes,
            "quote_statuses": quote_statuses,
            "quote_status": quote_status,
            "primary_macd": primary_macd,
            "primary_signal": primary_signal,
            "today_signal_overview": today_signal_overview,
            "worker_code_sha": git_sha(),
            "bootstrap_diag": self._market_data.get_last_bootstrap_diag() if self._market_data is not None else {},
            "bootstrap_attempts": self._bootstrap_attempts,
            "bootstrap_last_attempt_at": self._last_bootstrap_at,
            "bootstrap_last_result": self._last_bootstrap_result,
        }

    def supervisor_status(self) -> dict[str, Any]:
        state = self._persist_worker_stall_if_needed(state_store.load_state())
        stats = self._worker.tick_stats() if self._worker is not None else {}
        worker_alive = bool(self._worker and self._worker.is_alive())
        return {
            "worker_alive": worker_alive,
            "runtime_ui_mode": state.ui_mode.value,
            "order_block_reason": state.order_block_reason,
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
