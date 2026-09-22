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

import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

from app.trading import strategy_ownership
from app.trading.macd2 import config, ledger, order_executor, state_store
from app.trading.macd2 import peak_protection
from app.trading.macd2 import n1_adaptive
from app.trading.macd2 import position_sizing
from app.trading.macd2 import small_whipsaw_hold
from app.trading.macd2 import time_window_3slot
from app.trading.macd2.broker_adapter import create_macd2_broker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeStatus, SignalState
from app.trading.macd2.signal_engine import calculate_macd, resample_completed_3m
from app.trading.macd2.worker import (
    ORDER_FILL_RECONCILE_DELAY_SEC,
    ORDER_FILL_RECONCILE_RETRIES,
    Macd2Worker,
    _apply_exit_outcome,
    _apply_switch_outcome,
    _parse_iso_dt,
    abandon_pending_time_window_candidate_if_any,
    abandon_pending_tw2_3slot_candidate_if_any,
    compute_today_signal_overview,
    git_sha,
    initialize_strategy_session,
    run_once,
)

KST = config.KST


def other_strategy_active() -> tuple[bool, str]:
    """docs §15: block MACD2 start if Enhanced or MACD v1 is really active."""
    return strategy_ownership.other_owner_active(strategy_ownership.MACD2)


def _record_manual_entry_signal(state, direction: Direction, signal_id: str, now: datetime, outcome) -> None:
    """Signal-ledger row for a manual entry button click (2026-08-04) —
    execution-ledger recording already happens inside
    order_executor.execute_signal itself (_record_leg); this only adds the
    signal-ledger side so the click shows up next to normal MACD-confirmed
    signals, tagged ``signal_type=MANUAL_ENTRY`` (no macd_snap backs it, so
    the MACD-specific columns are left blank rather than faked)."""
    block_reason = outcome.block_reason or ""
    row = {
        "trading_date": now.strftime("%Y%m%d"),
        "completed_bar_at": now.strftime("%H%M%S"),
        "signal_id": signal_id,
        "signal_type": "MANUAL_ENTRY",
        "direction": direction.value,
        "detected_at": now.isoformat(),
        "order_requested_at": outcome.timestamps.get("buy_requested_at", ""),
        "order_result": outcome.final_state.value,
        "block_reason": block_reason,
        "signal_bar_at": now.isoformat(),
        "signal_confirmed_at": now.isoformat(),
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": "MANUAL_ENTRY_UI_BUTTON",
        "worker_code_sha": git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        "confirmed_direction": direction.value,
        "executor_called": True,
        "broker_called": bool(outcome.broker_called),
        "broker_order_id": outcome.buy_result.order_id if outcome.buy_result else "",
        "order_price": outcome.order_price,
        "order_type": outcome.order_type or "",
        "requested_qty": outcome.final_qty,
        "final_qty": outcome.quantity,
        "filled_qty": outcome.filled_qty,
        "fill_poll_result": outcome.fill_poll_result or "",
        "balance_qty": outcome.balance_qty,
        "failure_stage": outcome.order_failure_stage or "",
        "final_result": f"{outcome.final_state.value}:{block_reason}" if block_reason else outcome.final_state.value,
    }
    ledger.append_signal(row)


def _record_manual_liquidation_signal(
    state, symbol: str, direction: Optional[Direction], signal_id: str, now: datetime, outcome, signal_type: str,
) -> None:
    """Signal-ledger row for a manual full-sell (2026-08-04) — mirrors
    ``_record_manual_entry_signal`` so a user-initiated liquidation shows up
    in the same audit trail as MACD-confirmed signals instead of leaving
    only the execution-ledger leg (previously the only place "수동 매도"
    appeared at all, an asymmetry with manual_entry's signal-ledger row)."""
    block_reason = outcome.block_reason or ""
    row = {
        "trading_date": now.strftime("%Y%m%d"),
        "completed_bar_at": now.strftime("%H%M%S"),
        "signal_id": signal_id,
        "signal_type": signal_type,
        "direction": direction.value if direction is not None else "",
        "detected_at": now.isoformat(),
        "order_requested_at": outcome.timestamps.get("sell_requested_at", ""),
        "order_result": outcome.final_state.value,
        "block_reason": block_reason,
        "signal_bar_at": now.isoformat(),
        "signal_confirmed_at": now.isoformat(),
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": "MANUAL_LIQUIDATION_UI_BUTTON",
        "worker_code_sha": git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        "confirmed_direction": direction.value if direction is not None else "",
        "executor_called": True,
        "broker_called": bool(outcome.sell_result is not None),
        "broker_order_id": outcome.sell_result.order_id if outcome.sell_result else "",
        "final_result": f"{outcome.final_state.value}:{block_reason}" if block_reason else outcome.final_state.value,
    }
    ledger.append_signal(row)


def _history_diag_of(market_data) -> dict[str, Any]:
    """``history_diag()`` 를 가진 MarketData 에서만 진단 dict 를 꺼낸다.
    없으면 빈 dict — UI/스냅샷 계약을 깨지 않는다."""
    fn = getattr(market_data, "history_diag", None)
    if fn is None:
        return {}
    try:
        return dict(fn())
    except Exception:  # pragma: no cover - 진단이 대시보드를 죽이면 안 된다
        return {}



def _sync_c1_with_mode(state, changed_by: str = "ui") -> None:
    """C1 은 N1 계열(X2-lite 계열 래더) 전용 overlay 다 — 그 계열이 아닌
    모드로 바뀌면 토글을 자동으로 끈다(조기익절이 3-SLOT 계열에 의존해
    자동으로 꺼지는 것과 같은 관례). 켜는 일은 절대 하지 않는다."""
    if not bool(getattr(state, "c1_peak_protection_enabled", False)):
        return
    if bool(getattr(state, "time_window_n1_filter_enabled", False)):
        return
    state.c1_peak_protection_enabled = False
    state.c1_peak_protection_enabled_at = datetime.now(KST).isoformat()
    state.c1_peak_protection_enabled_by = f"AUTO_MODE_NOT_N1:{changed_by}"
    peak_protection.clear(state)

log = logging.getLogger(__name__)


class Macd2Service:
    """Owns the MarketDataService/broker/Worker for one MACD2 run."""

    def __init__(self) -> None:
        # 2026-08-19: marks THIS process as the genuine live app/service
        # process, allowing ledger.append_signal/append_execution and
        # state_store.save_state to write to the real production paths --
        # set here (not only inside Macd2Worker.start()) because several
        # real, legitimate code paths above (e.g. config.AUTO_TRADE_HARD_
        # DISABLED's early return, other_strategy_active's block) call
        # state_store.save_state() and return BEFORE a Worker thread is ever
        # started. An ad-hoc/replay script that never constructs this class
        # at all (every scripts/_tmp_*.py replay so far) never sets this, so
        # it still gets refused unless it redirects the ledger/state paths
        # itself first -- see ledger.py's own docstring for the 2026-08-19
        # incident this guards against.
        os.environ[ledger.LIVE_WORKER_MARKER_ENV] = str(os.getpid())
        self._market_data: Optional[MarketDataService] = None
        self._broker = None
        self._worker: Optional[Macd2Worker] = None
        self._bootstrap_attempts: int = 0
        self._last_bootstrap_at: Optional[str] = None
        self._last_bootstrap_result: Optional[dict[str, Any]] = None
        # 2026-08-24 fix: first time _auto_recover_worker() was blocked by an
        # unconfirmed teardown (see start()'s _require_confirmed_teardown) --
        # reset to None as soon as a recovery attempt is not blocked for this
        # reason. Lets auto-recover force through anyway once blocked for too
        # long (QUOTE_UPDATER_FORCE_REPLACE_AGE_SEC), the same escape hatch
        # worker.py's quote-updater self-heal uses, so a genuinely
        # permanently-hung old instance (2026-08-20 incident) doesn't stall
        # recovery forever.
        self._teardown_stuck_since: Optional[datetime] = None
        # ── history updater watchdog (2026-09-17) ─────────────────────────
        # worker 루프와 **완전히 독립**이다. 2026-09-16 사고 때 시세까지 멈춘
        # 것은 worker._run_loop 자체가 돌지 않았다는 뜻이라, 자가복구를 그 루프
        # 안에 두면 같은 실패를 또 놓친다. 그래서 UI 스냅샷 경로
        # (get_snapshot/supervisor_status)에서 돈다 -- worker 가
        # alive-but-stuck 이어도 이건 계속 동작한다.
        self._history_recover_attempt_at: Optional[datetime] = None
        self._history_recover_streak: int = 0
        self._history_watchdog_last: dict[str, Any] = {}

    def _within_history_watch_window(self, now: datetime) -> bool:
        """정규 거래시간에만 감시한다. 장 마감/프리마켓/주말에는 새 봉이 안 오는
        것이 정상이므로 STALE 복구를 반복하면 안 된다(2026-09-17 사용자 조건).
        장 시작 직후 grace 는 worker._within_open_grace_window 와 같은 60초."""
        local = now.astimezone(KST)
        if local.weekday() >= 5:
            return False
        if not (config.SESSION_OPEN <= local.time() < config.FORCE_LIQUIDATE_AT):
            return False
        open_dt = local.replace(
            hour=config.SESSION_OPEN.hour, minute=config.SESSION_OPEN.minute,
            second=0, microsecond=0,
        )
        return local >= open_dt + timedelta(seconds=config.HISTORY_WATCHDOG_OPEN_GRACE_SEC)

    def _history_watchdog(self, state, now: Optional[datetime] = None) -> dict[str, Any]:
        """history updater 의 죽음/정체를 감지하고 **그 스레드만** 되살린다.

        하는 일:  1분봉 수집 스레드 stop/join -> start (정확히 1개)
        하지 않는 일: 자동매매 worker 재시작, 주문 실행/복원, 전략 state 변경.
        데이터 수집 복구와 REAL 자동매매 재개는 분리한다(2026-09-17 사용자 결정)
        -- 그래서 MOCK 전용인 ``_auto_recover_worker`` 와 달리 REAL 에서도 돈다.

        신규진입 차단은 이 함수가 새로 만들지 않는다. 데이터가 낡으면
        ``worker.py`` 의 기존 HISTORY_STALE 게이트(quote_history_mismatch_reason
        -> entry_window_open)가 이미 막고 있고, 복구가 막힌 동안에도 그 차단은
        그대로 유지된다."""
        now = now or datetime.now(KST)
        md = self._market_data
        verdict: Optional[str] = None
        action: Optional[str] = None
        if (md is None
                or not hasattr(md, "history_stale_age_sec")
                or not hasattr(md, "recover_history_updater")
                or not bool(getattr(state, "auto_trade_on", False))):
            # 의도적으로 내려둔 상태 -- 되살리지 않는다.
            self._history_watchdog_last = {"verdict": None, "action": None,
                                           "checked_at": now.isoformat()}
            return self._history_watchdog_last
        if not self._within_history_watch_window(now):
            self._history_watchdog_last = {"verdict": None, "action": "OUT_OF_SESSION",
                                           "checked_at": now.isoformat()}
            return self._history_watchdog_last

        alive = md.history_updater_alive()
        stale_age = md.history_stale_age_sec(now)
        if not alive:
            verdict = config.HISTORY_UPDATER_DEAD
        elif stale_age is not None and stale_age > config.HISTORY_STALE_MAX_SEC:
            verdict = config.HISTORY_UPDATER_ALIVE_BUT_STALE

        if verdict is None:
            # 정상 갱신 중 -- 아무 동작도 하지 않는다.
            self._history_recover_streak = 0
            self._history_watchdog_last = {"verdict": None, "action": None,
                                           "checked_at": now.isoformat(),
                                           "stale_age_sec": stale_age}
            return self._history_watchdog_last

        cooldown = (config.WORKER_AUTO_RECOVER_COOLDOWN_SEC
                    if self._history_recover_streak < config.HISTORY_WATCHDOG_FAST_RETRY_LIMIT
                    else config.QUOTE_UPDATER_FORCE_REPLACE_AGE_SEC)
        last = self._history_recover_attempt_at
        if last is not None and (now - last).total_seconds() < cooldown:
            action = "COOLDOWN"
        else:
            self._history_recover_attempt_at = now
            self._history_recover_streak += 1
            action = md.recover_history_updater(now=now)
        self._history_watchdog_last = {
            "verdict": verdict, "action": action, "checked_at": now.isoformat(),
            "stale_age_sec": stale_age, "streak": self._history_recover_streak,
        }
        return self._history_watchdog_last

    def _auto_recover_worker(self, state) -> bool:
        """2026-08-04 fix: a fresh process (Render free-tier idle-sleep,
        redeploy, or crash — the Worker/broker/market-data live only as
        this instance's Python attributes, docs/deploy_render.md's
        ephemeral filesystem) previously left ``auto_trade_on=True``
        permanently WORKER_STALLED with no automatic recovery, silently
        producing 0 flags/orders until a human noticed and clicked
        "자동매매 시작" again. Retries the same ``start()`` path
        automatically — MOCK mode only (REAL mode must always go through
        the UI's explicit confirm-text re-entry, never auto-reactivated),
        and rate-limited by WORKER_AUTO_RECOVER_COOLDOWN_SEC so a
        persistently-failing bootstrap can't hammer KIS on every
        auto-refresh tick. Returns True if a live worker resulted.
        """
        if config.AUTO_TRADE_HARD_DISABLED:
            # Don't leave a stale auto_trade_on=True sitting around looking
            # like it merely stalled -- make the persisted state say plainly
            # why it will never come back on its own.
            state.auto_trade_on = False
            state.ui_mode = RuntimeStatus.STOPPED
            state.order_block_reason = "MACD2_AUTO_TRADE_HARD_DISABLED"
            state_store.save_state(state)
            return False
        if state.mode != "mock":
            return False
        last_attempt = _parse_iso_dt(state.last_auto_recover_attempt_at)
        now = datetime.now(KST)
        if last_attempt is not None and (now - last_attempt).total_seconds() < config.WORKER_AUTO_RECOVER_COOLDOWN_SEC:
            return False
        state.last_auto_recover_attempt_at = now.isoformat()
        state_store.save_state(state)
        force_through = (
            self._teardown_stuck_since is not None
            and (now - self._teardown_stuck_since).total_seconds() > config.QUOTE_UPDATER_FORCE_REPLACE_AGE_SEC
        )
        result = self.start(mode=state.mode, budget=state.budget, _require_confirmed_teardown=not force_through)
        if result.get("message") == "PREVIOUS_INSTANCE_STILL_STOPPING":
            if self._teardown_stuck_since is None:
                self._teardown_stuck_since = now
        else:
            self._teardown_stuck_since = None
        return bool(result.get("ok")) and bool(self._worker and self._worker.is_alive())

    def _persist_worker_stall_if_needed(self, state):
        worker_alive = bool(self._worker and self._worker.is_alive())
        if state.auto_trade_on and not worker_alive:
            if self._auto_recover_worker(state):
                return state_store.load_state()
            state = state_store.load_state()
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
        _require_confirmed_teardown: bool = False,
    ) -> dict[str, Any]:
        # 2026-08-20 fix (real incident: after a Render idle-sleep/redeploy, a
        # still-alive-but-stuck Worker thread made start() refuse forever
        # with ALREADY_RUNNING and no way to force a clean restart from the
        # UI -- the exact same class of bug the 2026-08-14 MU_MACD fix
        # already addresses there, see
        # app.trading.mu_macd.service.MUMacdService.start()'s own
        # _stop_worker_and_market_data_locked() call at this same relative
        # position). Tear down the existing worker/market_data first, then
        # fall through to start fresh exactly as if nothing had been
        # running -- _attempt_bootstrap() below always builds a brand-new
        # Macd2Worker + MarketDataService regardless, so no further
        # special-casing is needed past this point.
        #
        # 2026-08-21 fix (real incident: Render memory climbing again in a
        # repeating recover/die cycle right after the quote-updater leak
        # above was fixed): this teardown used to run ONLY when
        # self._worker.is_alive() was True. But _auto_recover_worker() calls
        # start() precisely WHEN the worker is dead (WORKER_THREAD_DEAD) --
        # the one case this condition always skipped. self._market_data
        # (and its still-running quote-updater/history-updater threads) was
        # then silently replaced at "self._market_data = MarketDataService(
        # ...)" below without ever being stopped, orphaning a full pair of
        # live background threads every single ~30s auto-recover cooldown
        # cycle for as long as the worker kept dying -- each one still
        # polling KIS forever, compounding the very contention that was
        # killing the worker in the first place. Tearing down market_data is
        # unconditional now: whether or not the worker thread itself was
        # still alive, any previous market_data must be stopped before a new
        # one replaces it.
        worker_confirmed_stopped = True
        if self._worker is not None and self._worker.is_alive():
            worker_confirmed_stopped = self._worker.stop(join_timeout=5.0)
        market_data_confirmed_stopped = True
        if self._market_data is not None:
            q_stopped = self._market_data.stop_quote_updater(join_timeout=2.0)
            h_stopped = self._market_data.stop_history_updater(join_timeout=2.0)
            market_data_confirmed_stopped = q_stopped and h_stopped

        # 2026-08-24 fix (real incident: Render memory 20%->60% over ~2h under
        # sustained KIS rate limiting): _auto_recover_worker() calls start()
        # every WORKER_AUTO_RECOVER_COOLDOWN_SEC (30s) whenever the worker is
        # dead. The teardown above only BEST-EFFORT-asks the old worker/
        # market_data threads to stop -- under sustained contention a stuck
        # KIS retry chain routinely outlives these join timeouts, so the old
        # threads (and the old MarketDataService's KIS client + history
        # frame) were still alive and running every single time, and
        # "self._market_data = MarketDataService(...)" below unconditionally
        # replaced them with a brand-new instance anyway -- net-positive
        # instance/thread accumulation for as long as the contention lasted.
        # Only auto-recover (never an explicit manual/UI start -- that must
        # always be able to force a fresh restart per the 2026-08-20 fix
        # above) refuses to pile a new instance on top when teardown isn't
        # confirmed; it simply retries on the next cooldown tick instead.
        if _require_confirmed_teardown and not (worker_confirmed_stopped and market_data_confirmed_stopped):
            return {"ok": False, "message": "PREVIOUS_INSTANCE_STILL_STOPPING"}

        if config.AUTO_TRADE_HARD_DISABLED:
            state = state_store.load_state()
            state.auto_trade_on = False
            state.order_block_reason = "MACD2_AUTO_TRADE_HARD_DISABLED"
            state_store.save_state(state)
            return {"ok": False, "message": "MACD2_AUTO_TRADE_HARD_DISABLED"}

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

        # 2026-08-04 fix: run_once() synchronously, once, right here — BEFORE
        # spawning the background thread — so a same-day restart's "leave
        # the newest bar for a live tick" catch-up (initialize_strategy_
        # session) always actually gets that tick, even if the hosting
        # process dies again immediately after this call returns (Render
        # idle-sleep/redeploy can be that abrupt, and relying on the
        # background thread's first loop iteration left a window where a
        # confirmed flag was found on the NEXT restart's catch-up walk but
        # its order was never dispatched, repeating for every flag until a
        # restart happened to survive long enough). run_once()'s own
        # bar-key dedup makes a second evaluation of the same bar (e.g. by
        # the Worker thread's own first loop iteration moments later) a
        # safe no-op — never duplicated.
        try:
            run_once(broker=self._broker, market_data=self._market_data, state=state, now=datetime.now(KST))
        except Exception:
            pass  # best-effort catch-up tick; the Worker's own loop retries every tick regardless
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
            if symbol == config.LONG_SYMBOL:
                direction = Direction.UP_RED
            elif symbol == config.INVERSE_SYMBOL:
                direction = Direction.DOWN_BLUE
            else:
                direction = None
            now = datetime.now(KST)
            signal_id = f"USER_LIQUIDATION_{symbol}_{now.strftime('%Y%m%d%H%M%S')}"
            _record_manual_liquidation_signal(state, symbol, direction, signal_id, now, outcome, "USER_LIQUIDATION")
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
            state.profit_lock_symbol = None
            state.profit_lock_entry_bar_ts = None
            state.profit_lock_last_bar_ts = None
            state.profit_lock_bars_since_entry = 0
            state.profit_lock_gap_history = []
            state.profit_lock_peak_return_pct = 0.0
            state.profit_lock_current_support_gap = None
            state.profit_lock_max_support_gap = None
            state.profit_lock_gap_ratio = None
            state.profit_lock_contraction_count = 0
            state.profit_lock_drawdown_pct = 0.0
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

    def set_sideways_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional 추세전환장(sideways/whipsaw) order
        gate. Only updates runtime state — never places orders or liquidates.
        Takes effect from the next confirmed flag; open positions unchanged.
        When ON, this gate takes priority over major_filter_enabled — the
        two gates are never both active at once (worker._judge_entry_gate).
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.sideways_filter_enabled)
        state.sideways_filter_enabled = enabled_bool
        state.sideways_filter_version = config.SIDEWAYS_FILTER_VERSION
        state.sideways_filter_enabled_at = datetime.now(KST).isoformat()
        state.sideways_filter_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "sideways_filter_enabled": enabled_bool,
            "previous": prev,
            "sideways_filter_enabled_at": state.sideways_filter_enabled_at,
            "sideways_filter_enabled_by": state.sideways_filter_enabled_by,
            "sideways_filter_version": state.sideways_filter_version,
        }

    def set_trend_persistence_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional Trend Persistence order gate.

        Only updates runtime state — never places orders or liquidates.
        Takes effect from the next confirmed flag; open positions unchanged.
        When ON, sideways_filter_enabled and major_filter_enabled both take
        priority over this gate — the three are never more than one active
        for the same signal (worker._judge_entry_gate).
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.trend_persistence_filter_enabled)
        state.trend_persistence_filter_enabled = enabled_bool
        state.trend_persistence_filter_version = config.TREND_PERSISTENCE_FILTER_VERSION
        state.trend_persistence_filter_enabled_at = datetime.now(KST).isoformat()
        state.trend_persistence_filter_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "trend_persistence_filter_enabled": enabled_bool,
            "previous": prev,
            "trend_persistence_filter_enabled_at": state.trend_persistence_filter_enabled_at,
            "trend_persistence_filter_enabled_by": state.trend_persistence_filter_enabled_by,
            "trend_persistence_filter_version": state.trend_persistence_filter_version,
        }

    def set_single_entry_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional Daily Single-Entry order gate.

        Only updates runtime state — never places orders or liquidates.
        Takes effect from the next confirmed flag; open positions unchanged.
        When ON, sideways_filter_enabled, major_filter_enabled, and
        trend_persistence_filter_enabled all take priority over this gate —
        the four are never more than one active for the same signal
        (worker._judge_entry_gate).
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.single_entry_filter_enabled)
        state.single_entry_filter_enabled = enabled_bool
        state.single_entry_filter_version = config.SINGLE_ENTRY_FILTER_VERSION
        state.single_entry_filter_enabled_at = datetime.now(KST).isoformat()
        state.single_entry_filter_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "single_entry_filter_enabled": enabled_bool,
            "previous": prev,
            "single_entry_filter_enabled_at": state.single_entry_filter_enabled_at,
            "single_entry_filter_enabled_by": state.single_entry_filter_enabled_by,
            "single_entry_filter_version": state.single_entry_filter_version,
        }

    def set_time_window_teg_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional TEGv2 count-cap bypass on top of
        TW2. Enabling TEG also enables TW2, because TEG only evaluates TW2
        candidates rejected solely by the daily entry-count cap. Disabling
        TEG leaves TW2 unchanged.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.time_window_teg_filter_enabled)
        state.time_window_teg_filter_enabled = enabled_bool
        state.time_window_teg_filter_version = config.TIME_WINDOW_TEG_FILTER_VERSION
        state.time_window_teg_filter_enabled_at = datetime.now(KST).isoformat()
        state.time_window_teg_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and not state.time_window_2_filter_enabled:
            state.time_window_2_filter_enabled = True
            state.time_window_2_filter_version = config.TIME_WINDOW_2_FILTER_VERSION
            state.time_window_2_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_2_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and state.time_window_3slot_filter_enabled:
            # TW2 3-SLOT (2026-09-01) shares this same priority tier — 3-way
            # mutual exclusion, same pattern as TW1/TW2 before it.
            state.time_window_3slot_filter_enabled = False
            state.time_window_3slot_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_3slot_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="TW2_3SLOT_DISABLED_BY_TEG_ENABLE",
            )
        if enabled_bool and state.time_window_twf_filter_enabled:
            # TWF 3-SLOT (2026-09-07) 도 같은 우선순위 tier -- 4-way 상호배제.
            state.time_window_twf_filter_enabled = False
            state.time_window_twf_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_twf_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="TWF_3SLOT_DISABLED_BY_OTHER_MODE_ENABLE",
            )
        if enabled_bool and state.time_window_x2lite_filter_enabled:
            # X2-lite 도 같은 tier — 상호배타 (2026-09-12).
            state.time_window_x2lite_filter_enabled = False
            state.time_window_x2lite_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_x2lite_filter_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "time_window_teg_filter_enabled": enabled_bool,
            "previous": prev,
            "time_window_teg_filter_enabled_at": state.time_window_teg_filter_enabled_at,
            "time_window_teg_filter_enabled_by": state.time_window_teg_filter_enabled_by,
            "time_window_teg_filter_version": state.time_window_teg_filter_version,
            "time_window_2_filter_enabled": bool(state.time_window_2_filter_enabled),
            "time_window_3slot_filter_enabled": bool(state.time_window_3slot_filter_enabled),
            "time_window_twf_filter_enabled": bool(state.time_window_twf_filter_enabled),
            "time_window_x2lite_filter_enabled": bool(state.time_window_x2lite_filter_enabled),
        }

    def set_time_window_2_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle TW2. TEGv2 is an optional TW2 sub-filter, so
        turning TW2 off also turns TEG off; turning TW2 on leaves TEG at its
        current value. The optional "+1 DOWN_BLUE 예외진입" sub-filter uses
        the same TW2 candidate path.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.time_window_2_filter_enabled)
        state.time_window_2_filter_enabled = enabled_bool
        state.time_window_2_filter_version = config.TIME_WINDOW_2_FILTER_VERSION
        state.time_window_2_filter_enabled_at = datetime.now(KST).isoformat()
        state.time_window_2_filter_enabled_by = str(changed_by or "ui")
        if not enabled_bool and state.time_window_teg_filter_enabled:
            state.time_window_teg_filter_enabled = False
            state.time_window_teg_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_teg_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and state.time_window_3slot_filter_enabled:
            # TW2 3-SLOT (2026-09-01) shares this same priority tier — 3-way
            # mutual exclusion, same pattern as TW1/TW2 before it.
            state.time_window_3slot_filter_enabled = False
            state.time_window_3slot_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_3slot_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="TW2_3SLOT_DISABLED_BY_TW2_ENABLE",
            )
        if enabled_bool and state.time_window_twf_filter_enabled:
            # TWF 3-SLOT (2026-09-07) 도 같은 우선순위 tier -- 4-way 상호배제.
            state.time_window_twf_filter_enabled = False
            state.time_window_twf_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_twf_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="TWF_3SLOT_DISABLED_BY_OTHER_MODE_ENABLE",
            )
        if not enabled_bool:
            # 2026-08-28 real incident fix: turning TW2 off (which also forces
            # TEG off, above) used to leave an already-pending T+3 candidate
            # (state.time_window_pending_flag_direction) silently orphaned
            # forever -- _resolve_time_window_candidate no-ops the instant
            # both filters are off and never revisits it, even if the filter
            # is re-enabled later. Explicitly abandon it here (logged, never
            # silently dropped) instead. Pure state/ledger cleanup -- does not
            # touch MACD calculation, TW2/TEGv2 scoring, or order dispatch.
            abandon_pending_time_window_candidate_if_any(
                state, datetime.now(KST), reason="TW2_DISABLED_BY_USER",
            )
        if enabled_bool and state.time_window_x2lite_filter_enabled:
            # X2-lite 도 같은 tier — 상호배타 (2026-09-12).
            state.time_window_x2lite_filter_enabled = False
            state.time_window_x2lite_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_x2lite_filter_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "time_window_2_filter_enabled": enabled_bool,
            "previous": prev,
            "time_window_2_filter_enabled_at": state.time_window_2_filter_enabled_at,
            "time_window_2_filter_enabled_by": state.time_window_2_filter_enabled_by,
            "time_window_2_filter_version": state.time_window_2_filter_version,
            "time_window_teg_filter_enabled": bool(state.time_window_teg_filter_enabled),
            "time_window_3slot_filter_enabled": bool(state.time_window_3slot_filter_enabled),
            "time_window_twf_filter_enabled": bool(state.time_window_twf_filter_enabled),
            "time_window_x2lite_filter_enabled": bool(state.time_window_x2lite_filter_enabled),
        }

    def set_time_window_3slot_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle TW2 3-SLOT — a THIRD, separately selectable
        time-window mode (2026-09-01 사용자 요청), mutually exclusive with
        BOTH TW2 and TEG (enabling this forces both of those off; enabling
        either of those forces this off, in their own setters above). See
        app/trading/macd2/time_window_3slot.py's module docstring for what
        this mode reuses (TW2's own T+3/quality-score/extra-veto gate,
        TEGv2, the position-management ladder, whipsaw-tolerant reversal
        exit — all completely unmodified) vs. new (its own 3-slot entry
        orchestration). Default OFF (config.TW2_3SLOT_FILTER_DEFAULT) — TW2
        remains the live default; only updates runtime state, never places
        orders directly.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.time_window_3slot_filter_enabled)
        state.time_window_3slot_filter_enabled = enabled_bool
        state.time_window_3slot_filter_version = config.TW2_3SLOT_FILTER_VERSION
        state.time_window_3slot_filter_enabled_at = datetime.now(KST).isoformat()
        state.time_window_3slot_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and state.time_window_twf_filter_enabled:
            # TWF 3-SLOT 과도 상호배타 (같은 tier).
            state.time_window_twf_filter_enabled = False
            state.time_window_twf_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_twf_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled):
            state.time_window_2_filter_enabled = False
            state.time_window_2_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_2_filter_enabled_by = str(changed_by or "ui")
            state.time_window_teg_filter_enabled = False
            state.time_window_teg_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_teg_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_time_window_candidate_if_any(
                state, datetime.now(KST), reason="TW2_DISABLED_BY_TW2_3SLOT_ENABLE",
            )
        if not enabled_bool:
            # Same 2026-08-28-class orphaned-candidate fix as TW2's own
            # toggle-off path, for this mode's own separate pending state.
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="TW2_3SLOT_DISABLED_BY_USER",
            )
            # 조기익절 필터는 TW2 3-SLOT 전용 서브필터라서 이 모드가 꺼지면
            # 존재 의미가 없다 -- 사용자 요청대로 함께 강제로 끈다. (실행
            # 경로에서도 early_take_profit.is_enabled/is_active가 두 토글을
            # AND로 요구하므로 상태가 어긋나도 발동 자체가 불가능하지만, UI에
            # "켜져 있는데 절대 안 걸리는 필터"가 남아 보이는 것을 막는다.)
            # 2026-09-07: 조기익절은 TW2 3-SLOT / TWF 3-SLOT 공통 서브필터가
            # 됐으므로, 이쪽을 끄더라도 TWF 가 켜져 있으면 그대로 살려 둔다.
            if state.early_tp_filter_enabled and not state.time_window_twf_filter_enabled:
                state.early_tp_filter_enabled = False
                state.early_tp_filter_enabled_at = datetime.now(KST).isoformat()
                state.early_tp_filter_enabled_by = "AUTO_TW2_3SLOT_DISABLED"
        _sync_c1_with_mode(state, changed_by)
        if enabled_bool and state.time_window_x2lite_filter_enabled:
            # X2-lite 도 같은 tier — 상호배타 (2026-09-12).
            state.time_window_x2lite_filter_enabled = False
            state.time_window_x2lite_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_x2lite_filter_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "time_window_3slot_filter_enabled": enabled_bool,
            "previous": prev,
            "time_window_3slot_filter_enabled_at": state.time_window_3slot_filter_enabled_at,
            "time_window_3slot_filter_enabled_by": state.time_window_3slot_filter_enabled_by,
            "time_window_3slot_filter_version": state.time_window_3slot_filter_version,
            "time_window_2_filter_enabled": bool(state.time_window_2_filter_enabled),
            "time_window_teg_filter_enabled": bool(state.time_window_teg_filter_enabled),
            "time_window_twf_filter_enabled": bool(state.time_window_twf_filter_enabled),
            "time_window_x2lite_filter_enabled": bool(state.time_window_x2lite_filter_enabled),
            "early_tp_filter_enabled": bool(state.early_tp_filter_enabled),
        }

    def set_time_window_twf_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle TWF 3-SLOT (2026-09-07 사용자 요청) -- a FOURTH,
        separately selectable time-window mode in the same priority tier as
        TW2 / TEGv2 / TW2 3-SLOT (enabling this forces those three off; each of
        those forces this off in its own setter).

        **진입 로직은 TW2 3-SLOT 과 완전히 동일하다** -- worker 의 같은
        _judge_tw2_3slot_flag / _resolve_tw2_3slot_candidate 경로를 그대로 타고,
        같은 tw2_3slot_* 슬롯 카운터/후보 필드를 쓰며, 하루 3회 cap 과
        "오전에 남은 슬롯만 13:00~14:50 에 사용" 규칙도 TW2 3-SLOT 이 이미
        하던 그대로다(worker._resolve_tw2_3slot_candidate_body 의
        window_blocked_by_morning_only 분기). 다른 것은 청산 임계값 3개뿐이며
        (time_window_3slot.exit_overrides: 오전손절 -1.4% / TP1이후 잔량 stop
        +2.0% / 오후 TP +3.0%) 그것도 time_window_position_manager 에 override
        인자로만 전달되므로 TW2 3-SLOT 과 MU_MACD 의 동작은 조금도 바뀌지 않는다.

        조기익절 필터는 이 전략에 하드코딩돼 있지 않다 -- TW2 3-SLOT 과 공유하는
        별도 토글이고 여기서도 독립적으로 ON/OFF 된다. 기본 OFF
        (config.TWF_3SLOT_FILTER_DEFAULT). 상태만 갱신하고 주문을 내지 않는다.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.time_window_twf_filter_enabled)
        state.time_window_twf_filter_enabled = enabled_bool
        state.time_window_twf_filter_version = config.TWF_3SLOT_FILTER_VERSION
        state.time_window_twf_filter_enabled_at = datetime.now(KST).isoformat()
        state.time_window_twf_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled):
            state.time_window_2_filter_enabled = False
            state.time_window_2_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_2_filter_enabled_by = str(changed_by or "ui")
            state.time_window_teg_filter_enabled = False
            state.time_window_teg_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_teg_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_time_window_candidate_if_any(
                state, datetime.now(KST), reason="TW2_DISABLED_BY_TWF_3SLOT_ENABLE",
            )
        if enabled_bool and state.time_window_3slot_filter_enabled:
            state.time_window_3slot_filter_enabled = False
            state.time_window_3slot_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_3slot_filter_enabled_by = str(changed_by or "ui")
        if not enabled_bool:
            # 두 3-SLOT 모드가 같은 pending 필드를 공유하므로, 끌 때도 TW2
            # 3-SLOT 과 완전히 같은 고아 후보 정리를 한다.
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="TWF_3SLOT_DISABLED_BY_USER",
            )
            if state.early_tp_filter_enabled and not state.time_window_3slot_filter_enabled:
                state.early_tp_filter_enabled = False
                state.early_tp_filter_enabled_at = datetime.now(KST).isoformat()
                state.early_tp_filter_enabled_by = "AUTO_TWF_3SLOT_DISABLED"
        _sync_c1_with_mode(state, changed_by)
        if enabled_bool and state.time_window_x2lite_filter_enabled:
            # X2-lite 도 같은 tier — 상호배타 (2026-09-12).
            state.time_window_x2lite_filter_enabled = False
            state.time_window_x2lite_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_x2lite_filter_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "time_window_twf_filter_enabled": enabled_bool,
            "previous": prev,
            "time_window_twf_filter_enabled_at": state.time_window_twf_filter_enabled_at,
            "time_window_twf_filter_enabled_by": state.time_window_twf_filter_enabled_by,
            "time_window_twf_filter_version": state.time_window_twf_filter_version,
            "time_window_2_filter_enabled": bool(state.time_window_2_filter_enabled),
            "time_window_teg_filter_enabled": bool(state.time_window_teg_filter_enabled),
            "time_window_3slot_filter_enabled": bool(state.time_window_3slot_filter_enabled),
            "time_window_x2lite_filter_enabled": bool(state.time_window_x2lite_filter_enabled),
            "early_tp_filter_enabled": bool(state.early_tp_filter_enabled),
        }

    def set_time_window_x2lite_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle X2-lite (2026-09-12 사용자 요청) — a FIFTH,
        separately selectable time-window mode in the same priority tier as
        TW2 / TEGv2 / TW2 3-SLOT / TW TEG 3-SLOT (enabling this forces those
        four off; each of those forces this off in its own setter).

        **진입 로직은 TW TEG 3-SLOT 과 100% 동일하다** — worker 의 같은
        _judge_tw2_3slot_flag / _resolve_tw2_3slot_candidate 경로를 그대로 타고,
        같은 tw2_3slot_* 슬롯 카운터/후보 필드를 쓰며, MACD 플래그 탐지 / T+3 /
        TEGv2 / Trend Quality / 슬롯 배분 / 하루 3회 cap / 신규진입 cutoff /
        same-direction·opposite-direction 규칙 / 신호원장 propagation 이 한 줄도
        다르지 않다. CHOP 후보에 TEGv2 를 추가로 요구하는 TW TEG 3-SLOT 의 진입
        규칙까지 공유한다(time_window_3slot.requires_chop_teg_gate).

        다른 것은 **청산 파라미터뿐**이다 (time_window_3slot.exit_overrides /
        morning_tp2_pct_override: TP1 분할 20% / trailing 2.8% / TP2 5.0% /
        오전손절 -1.3% / TP1이후 잔량 stop +2.0% / 오후 TP +3.0%). 전부
        time_window_position_manager 에 override 인자로만 전달되므로 TW2 3-SLOT /
        TW TEG 3-SLOT / MU_MACD 의 동작은 조금도 바뀌지 않는다.

        조기익절(ETP)은 이 전략에 **내장**돼 자동 ON 이며 trigger +1.5% /
        floor +1.0% 로 고정된다 — 별도 "조기익절 필터" 토글은 이 모드에서 아예
        참조되지 않으므로 중복 적용이 구조적으로 불가능하다
        (early_take_profit.is_enabled / thresholds). 그 토글이 켜져 있었다면
        혼동을 막기 위해 함께 꺼 준다.

        기본 OFF (config.X2LITE_3SLOT_FILTER_DEFAULT). 상태만 갱신하고 주문을
        내지 않는다.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.time_window_x2lite_filter_enabled)
        state.time_window_x2lite_filter_enabled = enabled_bool
        state.time_window_x2lite_filter_version = config.X2LITE_3SLOT_FILTER_VERSION
        state.time_window_x2lite_filter_enabled_at = datetime.now(KST).isoformat()
        state.time_window_x2lite_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled):
            state.time_window_2_filter_enabled = False
            state.time_window_2_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_2_filter_enabled_by = str(changed_by or "ui")
            state.time_window_teg_filter_enabled = False
            state.time_window_teg_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_teg_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_time_window_candidate_if_any(
                state, datetime.now(KST), reason="TW2_DISABLED_BY_X2LITE_ENABLE",
            )
        for _flag in ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
                      "time_window_h50_filter_enabled",
                      "time_window_n1_filter_enabled"):
            if enabled_bool and getattr(state, _flag, False):
                setattr(state, _flag, False)
                setattr(state, f"{_flag}_at", datetime.now(KST).isoformat())
                setattr(state, f"{_flag}_by", str(changed_by or "ui"))
        if enabled_bool and state.early_tp_filter_enabled:
            # X2-lite 는 조기익절을 내장한다 — 별도 토글이 켜진 채 남아 있으면
            # "두 번 적용되는 것처럼" 보이므로 꺼 둔다(실행경로상 이 모드에서는
            # 그 토글을 읽지도 않는다).
            state.early_tp_filter_enabled = False
            state.early_tp_filter_enabled_at = datetime.now(KST).isoformat()
            state.early_tp_filter_enabled_by = "AUTO_X2LITE_BUILTIN_ETP"
        if not enabled_bool:
            # 세 3-SLOT 모드가 같은 pending 필드를 공유하므로, 끌 때도 TW2
            # 3-SLOT / TW TEG 3-SLOT 과 완전히 같은 고아 후보 정리를 한다.
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="X2LITE_DISABLED_BY_USER",
            )
        state_store.save_state(state)
        return {
            "ok": True,
            "time_window_x2lite_filter_enabled": enabled_bool,
            "previous": prev,
            "time_window_x2lite_filter_enabled_at": state.time_window_x2lite_filter_enabled_at,
            "time_window_x2lite_filter_enabled_by": state.time_window_x2lite_filter_enabled_by,
            "time_window_x2lite_filter_version": state.time_window_x2lite_filter_version,
            "time_window_2_filter_enabled": bool(state.time_window_2_filter_enabled),
            "time_window_teg_filter_enabled": bool(state.time_window_teg_filter_enabled),
            "time_window_3slot_filter_enabled": bool(state.time_window_3slot_filter_enabled),
            "time_window_twf_filter_enabled": bool(state.time_window_twf_filter_enabled),
            "time_window_h50_filter_enabled": bool(state.time_window_h50_filter_enabled),
            "early_tp_filter_enabled": bool(state.early_tp_filter_enabled),
        }

    def set_time_window_h50_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle **H50 = X2-lite + W1a + 작은휩쏘 HOLD** (2026-09-15).

        X2-lite 와 같은 우선순위 tier 의 여섯 번째 모드다(켜면 TW2 / TEGv2 /
        TW2 3-SLOT / TW TEG 3-SLOT / X2-lite 가 전부 꺼지고, 그쪽 세터들도
        이쪽을 끈다).

        **진입 로직 · 청산 파라미터 · W1a 사이징이 X2-lite 와 100% 동일하다** —
        time_window_3slot.MODES_X2LITE_FAMILY 로 묶여 exit_overrides /
        morning_tp2_pct_override / requires_chop_teg_gate /
        early_take_profit.is_enabled·thresholds / position_sizing.is_active 가
        전부 X2-lite 와 같은 값을 돌려준다. H50 때문에 진입이 추가되거나
        삭제되지 않는다.

        다른 것은 **반대신호 청산 하나뿐**이다(app/trading/macd2/small_whipsaw_hold.py):
        보유 중 반대 플래그가 정상 확정됐을 때 (a) 보유방향 == 구조적 상위추세
        (LONG: EMA20>EMA50 / SHORT: EMA20<EMA50) 이고 (b) 최근 60분 high-low
        range <= 2.35% 이면 그 청산을 보류(HOLD)한다. HOLD 중에도 하드스톱
        -1.30% / TP1 / TP2 / 트레일링 / ETP / 강제청산은 전부 그대로 살아 있고,
        반대방향 신규진입은 하지 않는다. 해제는 추세가 반대로 2봉 연속
        전환되거나 HOLD 시작 후 60분이 지나면 일어난다.

        검증: data/validation/macd2/small_whipsaw_hold_20260915 (발견),
        data/validation/macd2/h50_stress_20260915 (압박테스트 — 등급
        **BORDERLINE**, bootstrap 94.9% 로 95% 미달). 그래서 기본 OFF 다
        (config.H50_3SLOT_FILTER_DEFAULT). 상태만 갱신하고 주문을 내지 않는다.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.time_window_h50_filter_enabled)
        state.time_window_h50_filter_enabled = enabled_bool
        state.time_window_h50_filter_version = config.H50_3SLOT_FILTER_VERSION
        state.time_window_h50_filter_enabled_at = datetime.now(KST).isoformat()
        state.time_window_h50_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled):
            state.time_window_2_filter_enabled = False
            state.time_window_2_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_2_filter_enabled_by = str(changed_by or "ui")
            state.time_window_teg_filter_enabled = False
            state.time_window_teg_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_teg_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_time_window_candidate_if_any(
                state, datetime.now(KST), reason="TW2_DISABLED_BY_H50_ENABLE",
            )
        for _flag in ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
                      "time_window_x2lite_filter_enabled",
                      "time_window_n1_filter_enabled"):
            if enabled_bool and getattr(state, _flag, False):
                setattr(state, _flag, False)
                setattr(state, f"{_flag}_at", datetime.now(KST).isoformat())
                setattr(state, f"{_flag}_by", str(changed_by or "ui"))
        if enabled_bool and state.early_tp_filter_enabled:
            # X2-lite 와 같이 조기익절을 내장한다 — 중복처럼 보이지 않게 꺼 둔다.
            state.early_tp_filter_enabled = False
            state.early_tp_filter_enabled_at = datetime.now(KST).isoformat()
            state.early_tp_filter_enabled_by = "AUTO_H50_BUILTIN_ETP"
        if not enabled_bool:
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="H50_DISABLED_BY_USER",
            )
            # 모드를 끄면 남아 있던 HOLD 상태도 정리한다(포지션은 건드리지 않는다 --
            # 다음 tick 부터 기존 X2-lite/3-SLOT 반대신호 청산 규칙이 그대로 적용).
            small_whipsaw_hold.clear(state)
        state_store.save_state(state)
        return {
            "ok": True,
            "time_window_h50_filter_enabled": enabled_bool,
            "previous": prev,
            "time_window_h50_filter_enabled_at": state.time_window_h50_filter_enabled_at,
            "time_window_h50_filter_enabled_by": state.time_window_h50_filter_enabled_by,
            "time_window_h50_filter_version": state.time_window_h50_filter_version,
            "time_window_2_filter_enabled": bool(state.time_window_2_filter_enabled),
            "time_window_teg_filter_enabled": bool(state.time_window_teg_filter_enabled),
            "time_window_3slot_filter_enabled": bool(state.time_window_3slot_filter_enabled),
            "time_window_twf_filter_enabled": bool(state.time_window_twf_filter_enabled),
            "time_window_x2lite_filter_enabled": bool(state.time_window_x2lite_filter_enabled),
            "early_tp_filter_enabled": bool(state.early_tp_filter_enabled),
            "h50_hold_active": bool(state.h50_hold_active),
        }

    def set_c1_peak_protection_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle **C1 Peak Protection** (2026-09-19 연구).

        N1 계열 래더 위에 얹는 **청산 전용 overlay** 다. 새 전략 모드가 아니다 —
        진입 로직 / off_tp2(8%↔4%) 적응 / TP1 / TP2 / 손절 / after-TP1 스탑 /
        trailing / 강제청산 / W1a 사이징 / 슬롯 / T+3 / quality / TEG 는 한 줄도
        바뀌지 않는다(app/trading/macd2/peak_protection.py 참고).

        발동 조건: 보유 포지션의 MFE(틱 관측)가 config.C1_ARM_MFE_PCT(5.0%) 에
        도달한 뒤, 완성 3분봉에서 MACD-Signal gap 이 보유방향 반대로 **부호
        전환**되고 동시에 MFE 대비 config.C1_GIVEBACK_PCT(1.5%p) 이상 반납하면
        잔량 전량청산. 기존 래더가 그 봉에서 이미 청산했으면 C1 은 평가조차
        되지 않는다(worker 의 _advance_c1_peak_protection 은 H50/whipsaw-watch
        와 같은 자리에서 호출된다).

        검증: data/validation/macd2/c1_peak_protection_20260919/README.md — 78영업일 N1 기준
        401.09 -> 438.63 (+37.54%p), 발동 7건 전부 개선(악화 0), TP2 8% runner
        손상 0, MDD 동일, 진입집합 diff 0, WF 6분할 4승 0패. 등급 **PROMISING**
        (OOS 없음 / 발동 7건 / 크기의 83%가 7월 4건). 그래서 **기본 OFF** 다
        (config.C1_FILTER_DEFAULT). 상태만 갱신하고 주문을 내지 않는다.

        C1 은 X2-lite / H50(= N1 계열 래더) 모드에서만 켤 수 있다. 그 밖의
        모드에서 켜려 하면 거부하고 이유를 돌려준다.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.c1_peak_protection_enabled)
        in_family = bool(state.time_window_n1_filter_enabled)
        if enabled_bool and not in_family:
            return {
                "ok": False,
                "reason": "C1_REQUIRES_N1_MODE",
                "message": "C1 Peak Protection 은 N1 모드에서만 켤 수 있습니다.",
                "c1_peak_protection_enabled": prev,
                "previous": prev,
            }
        state.c1_peak_protection_enabled = enabled_bool
        state.c1_peak_protection_version = config.C1_FILTER_VERSION
        state.c1_peak_protection_enabled_at = datetime.now(KST).isoformat()
        state.c1_peak_protection_enabled_by = str(changed_by or "ui")
        if not enabled_bool:
            # 끄면 보유기간 상태(arm/MFE/멱등키)만 정리한다. 포지션은 건드리지
            # 않는다 -- 다음 tick 부터 기존 래더만으로 관리된다.
            peak_protection.clear(state)
        state_store.save_state(state)
        return {
            "ok": True,
            "c1_peak_protection_enabled": enabled_bool,
            "previous": prev,
            "c1_peak_protection_enabled_at": state.c1_peak_protection_enabled_at,
            "c1_peak_protection_enabled_by": state.c1_peak_protection_enabled_by,
            "c1_peak_protection_version": state.c1_peak_protection_version,
            "c1_armed": bool(state.c1_armed),
            "c1_arm_mfe_pct": float(config.C1_ARM_MFE_PCT),
            "c1_giveback_pct": float(config.C1_GIVEBACK_PCT),
        }

    def set_smart_sizing_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle **SMART 사이징** (2026-09-22).

        P2 슬롯 배분과 toxic confirmation 감액을 **하나의 정책**으로 합친 토글이다.
        이전의 P2 토글을 대체한다(별도 Toxic 토글을 만들지 않는다).

            if toxic:  x0.25            <- 슬롯과 무관한 override (곱하지 않음)
            else:      slot1/2 x1.05 · 오전slot3 x0.25 · 오후slot3 x1.00

        toxic = confirmation_weak AND ema20_50_directional_pct < -0.20
          confirmation_weak : 플래그봉 시작~진입 직전 **보유할 ETF** 수익률 <= 0%
          ema20_50_directional : 하이닉스 EMA20-EMA50 을 보유방향 부호로 정규화한 %

        **사이징 전용**이다. 진입 판정 / 슬롯 배분 / T+3 / quality / TEG / 하루 3회
        상한 / TP1 / TP2 / 손절 / trailing / 조기익절 / C1 / H50 은 한 줄도 바뀌지
        않는다 — 이미 승인된 진입의 **주문수량 배수**만 바꾼다.

        검증: research_20260922d_confirmation_path/ (전략 E "TOXIC-OVERRIDE").
        78영업일 N1+C1 기준 uplift +2,106,468 KRW, PF 2.658->3.228,
        MDD -3.08%->-2.53%, 거래 158건 동일(진입집합/청산 diff 0),
        30일 +423,935 · OOS48 +1,682,533 · 앞39/뒤39 둘 다 양수 · 5분할 5/5 · WF6 6/6,
        runner MFE>=5%/>=8% 손상 0건, 일예산 30M 초과 0일.
        등급은 **PROMISING** 이지 ADOPT 가 아니다 — 30일 bootstrap 84.5%(기준 95%),
        효과의 96%가 slot1 toxic 12건에서 나온다. 그래서 **기본 OFF** 다.

        **N1 과 C1 이 둘 다 켜져 있을 때만** 켤 수 있다 — 앵커가 그 조합에서만
        측정됐기 때문이다. 상태만 갱신하고 주문을 내지 않는다.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(getattr(state, "smart_sizing_enabled", False))
        n1_on = bool(state.time_window_n1_filter_enabled)
        c1_on = bool(state.c1_peak_protection_enabled)
        if enabled_bool and not (n1_on and c1_on):
            missing = " + ".join(x for x, on in (("N1", n1_on), ("C1", c1_on)) if not on)
            return {
                "ok": False,
                "reason": "SMART_REQUIRES_N1_AND_C1",
                "message": f"SMART 사이징은 N1 + C1 이 모두 켜져 있어야 합니다 (현재 꺼짐: {missing}).",
                "smart_sizing_enabled": prev,
                "previous": prev,
            }
        state.smart_sizing_enabled = enabled_bool
        state.smart_sizing_enabled_at = datetime.now(KST).isoformat()
        state.smart_sizing_enabled_by = str(changed_by or "ui")
        # legacy 미러 — 구버전 코드로 롤백해도 토글이 살아남는다.
        state.p2_sizing_enabled = enabled_bool
        state.p2_sizing_enabled_at = state.smart_sizing_enabled_at
        state.p2_sizing_enabled_by = state.smart_sizing_enabled_by
        state_store.save_state(state)
        return {
            "ok": True,
            "smart_sizing_enabled": enabled_bool,
            "previous": prev,
            "smart_sizing_enabled_at": state.smart_sizing_enabled_at,
            "smart_sizing_enabled_by": state.smart_sizing_enabled_by,
            "sizing_mode": position_sizing.sizing_mode(state),
            "forced_by_env": position_sizing.forced_by_env(),
            "slot12_mult": float(config.P2_SIZING_SLOT12_MULT),
            "morning_slot3_mult": float(config.P2_SIZING_MORNING_SLOT3_MULT),
            "afternoon_slot3_mult": float(config.P2_SIZING_AFTERNOON_SLOT3_MULT),
            "toxic_mult": float(config.SMART_TOXIC_MULT),
            "toxic_ema20_50_max_pct": float(config.TOXIC_EMA20_50_MAX_PCT),
            "toxic_confirm_return_max_pct": float(config.TOXIC_CONFIRM_RETURN_MAX_PCT),
            "daily_capital": float(config.DEFAULT_BUDGET) * float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP),
        }

    # 2026-09-22: 구 이름 호환 alias (UI 롤백/외부 호출 대비).
    def set_p2_sizing_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """Deprecated — ``set_smart_sizing_enabled`` 로 위임한다."""
        out = self.set_smart_sizing_enabled(enabled, changed_by=changed_by)
        if "smart_sizing_enabled" in out:
            out["p2_sizing_enabled"] = out["smart_sizing_enabled"]
        return out

    def set_time_window_n1_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle **N1** (2026-09-20).

        X2-lite / H50 과 같은 우선순위 tier 의 일곱 번째 모드다(켜면 TW2 /
        TEGv2 / TW2 3-SLOT / TW TEG 3-SLOT / X2-lite / H50 이 전부 꺼지고,
        그쪽 세터들도 이쪽을 끈다).

        **진입은 H50 과 동일하고 quality 임계값만 4 -> 3 이다** — 그래서
        진입집합이 H50 과 다르다(78일 H50 157거래 vs N1 158거래). 슬롯/T+3/
        TW2 veto/TEGv2/CHOP 게이트/W1a 사이징/small whipsaw HOLD 는 전부
        X2-lite·H50 과 같은 코드를 그대로 쓴다.

        청산이 다른 점 (app/trading/macd2/n1_adaptive.py):
        상위추세(보유방향 기준 close/EMA20/EMA50/기울기, H50 과 **같은 EMA
        상수**) 여부로 오전 래더 3개 값이 **완성봉마다** 전환된다.

            추세 ok   : TP1 3.5 / TP1 매도비중 0.0 / TP2 8.0
            추세 아님 : TP1 3.0 / TP1 매도비중 0.2 / TP2 4.0

        고정값은 손절 -1.30 / after-TP1 2.00 / trailing trigger 3.50 /
        **trailing stop 1.50**(X2-lite 2.80) / **오후TP 4.00**(X2-lite 3.00) /
        조기익절 **1.50 -> 0.80**(X2-lite 1.50 -> 1.00). 강제청산 15:00 불변.

        검증: data/validation/macd2/n1_production_20260920/ — 78영업일
        (0527~0918) 158거래 / 복리 401.0853%, 진입·청산 parity diff 0
        (연구엔진이 이 브랜치의 production 정의를 읽어 재현). 기본 OFF.
        상태만 갱신하고 주문을 내지 않는다.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.time_window_n1_filter_enabled)
        state.time_window_n1_filter_enabled = enabled_bool
        state.time_window_n1_filter_version = config.N1_3SLOT_FILTER_VERSION
        state.time_window_n1_filter_enabled_at = datetime.now(KST).isoformat()
        state.time_window_n1_filter_enabled_by = str(changed_by or "ui")
        if enabled_bool and (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled):
            state.time_window_2_filter_enabled = False
            state.time_window_2_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_2_filter_enabled_by = str(changed_by or "ui")
            state.time_window_teg_filter_enabled = False
            state.time_window_teg_filter_enabled_at = datetime.now(KST).isoformat()
            state.time_window_teg_filter_enabled_by = str(changed_by or "ui")
            abandon_pending_time_window_candidate_if_any(
                state, datetime.now(KST), reason="TW2_DISABLED_BY_N1_ENABLE",
            )
        for _flag in ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
                      "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled"):
            if enabled_bool and getattr(state, _flag, False):
                setattr(state, _flag, False)
                setattr(state, f"{_flag}_at", datetime.now(KST).isoformat())
                setattr(state, f"{_flag}_by", str(changed_by or "ui"))
        if enabled_bool and state.early_tp_filter_enabled:
            # X2-lite/H50 와 같이 조기익절을 내장한다 — 중복처럼 보이지 않게 꺼 둔다.
            state.early_tp_filter_enabled = False
            state.early_tp_filter_enabled_at = datetime.now(KST).isoformat()
            state.early_tp_filter_enabled_by = "AUTO_N1_BUILTIN_ETP"
        if not enabled_bool:
            abandon_pending_tw2_3slot_candidate_if_any(
                state, datetime.now(KST), reason="N1_DISABLED_BY_USER",
            )
            # 모드를 끄면 adaptive 판정 캐시와 HOLD 상태를 정리한다(포지션은
            # 건드리지 않는다 -- 다음 tick 부터 기존 규칙이 그대로 적용).
            n1_adaptive.clear(state)
            small_whipsaw_hold.clear(state)
        # C1 은 N1 전용 overlay -- N1 을 끄면 자동으로 함께 꺼진다.
        _sync_c1_with_mode(state, changed_by)
        state_store.save_state(state)
        return {
            "ok": True,
            "time_window_n1_filter_enabled": enabled_bool,
            "previous": prev,
            "time_window_n1_filter_enabled_at": state.time_window_n1_filter_enabled_at,
            "time_window_n1_filter_enabled_by": state.time_window_n1_filter_enabled_by,
            "time_window_n1_filter_version": state.time_window_n1_filter_version,
            "time_window_2_filter_enabled": bool(state.time_window_2_filter_enabled),
            "time_window_teg_filter_enabled": bool(state.time_window_teg_filter_enabled),
            "time_window_3slot_filter_enabled": bool(state.time_window_3slot_filter_enabled),
            "time_window_twf_filter_enabled": bool(state.time_window_twf_filter_enabled),
            "time_window_x2lite_filter_enabled": bool(state.time_window_x2lite_filter_enabled),
            "time_window_h50_filter_enabled": bool(state.time_window_h50_filter_enabled),
            "early_tp_filter_enabled": bool(state.early_tp_filter_enabled),
            "n1_regime_state": state.n1_regime_state,
            "n1_effective_tp2": state.n1_effective_tp2,
        }

    def set_early_tp_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the "조기익절 필터" — a separately selectable,
        risk-management-ONLY sub-filter of TW2 3-SLOT (2026-09-03 사용자 요청).

        Not a new strategy and not an entry filter: TW2 3-SLOT's MACD
        zero-cross / T+3 re-confirmation / TW2 vetoes / slot allocation /
        Trend Quality / TEGv2 are completely unmodified and this toggle
        cannot affect any of them. It only adds one extra downside rung for a
        position that ALREADY exists, and only for positions whose entry
        confirmation bar was classified CHOP — see
        app/trading/macd2/early_take_profit.py and config.py's EARLY_TP_*
        block (60-business-day TRAIN40/OOS20 validation numbers included).

        Requires TW2 3-SLOT to be ON: enabling this while 3-SLOT is off is
        rejected (ok=False) rather than silently stored, and turning 3-SLOT
        off later force-disables this toggle in
        set_time_window_3slot_filter_enabled above. Default OFF
        (config.EARLY_TP_FILTER_DEFAULT). Only updates runtime state, never
        places orders directly.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        prev = bool(state.early_tp_filter_enabled)
        if enabled_bool and not time_window_3slot.is_3slot_enabled(state):
            # 2026-09-07: TW2 3-SLOT 또는 TWF 3-SLOT 중 하나면 된다.
            return {
                "ok": False,
                "reason": "TW2_3SLOT_REQUIRED",
                "early_tp_filter_enabled": prev,
                "time_window_3slot_filter_enabled": False,
                "time_window_twf_filter_enabled": False,
            }
        state.early_tp_filter_enabled = enabled_bool
        state.early_tp_filter_version = config.EARLY_TP_FILTER_VERSION
        state.early_tp_filter_enabled_at = datetime.now(KST).isoformat()
        state.early_tp_filter_enabled_by = str(changed_by or "ui")
        if not enabled_bool:
            # 포지션 종속 상태는 남겨두면 다음에 다시 켤 때 오래된 진입시점
            # 판정이 살아 있는 것처럼 보인다. 토글을 끄는 순간 무효화한다
            # (청산 경로는 is_active로 이미 막혀 있으므로 안전한 정리 작업).
            state.time_window_entry_chop = False
            state.early_tp_peak_net_return = 0.0
        state_store.save_state(state)
        return {
            "ok": True,
            "early_tp_filter_enabled": enabled_bool,
            "previous": prev,
            "early_tp_filter_enabled_at": state.early_tp_filter_enabled_at,
            "early_tp_filter_enabled_by": state.early_tp_filter_enabled_by,
            "early_tp_filter_version": state.early_tp_filter_version,
            "time_window_3slot_filter_enabled": bool(state.time_window_3slot_filter_enabled),
            "time_window_twf_filter_enabled": bool(state.time_window_twf_filter_enabled),
            "time_window_x2lite_filter_enabled": bool(state.time_window_x2lite_filter_enabled),
            "early_tp_trigger_pct": float(config.EARLY_TP_TRIGGER_PCT),
            "early_tp_floor_pct": float(config.EARLY_TP_FLOOR_PCT),
        }

    def set_down_blue_exception_filter_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional "탈락 DOWN_BLUE 예외진입" sub-filter
        -- a DOWN_BLUE candidate the TW gate itself rejects still gets one
        extra entry per trading day, no other condition (2026-08-18, backtest
        rationale in config.py's TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT
        docstring). Only updates runtime state -- never places orders. Works
        identically under either TW2 (time_window_2_filter_enabled) or the
        TEG filter (time_window_teg_filter_enabled) — 2026-08-21 사용자 요청,
        TEG filter replaced TW1 in this "either" 2026-08-27; has no effect
        while BOTH are off (no TW candidates ever exist to reject).
        """
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
        """UI command: toggle the optional "무필터 09:00-11:00" 즉시청산 진입모드
        (2026-08-20) -- a 6th peer entry gate (see worker._judge_no_filter_flag):
        approve any confirmed flag within 09:00-11:00, no quality score, and a
        rejected reversal under this gate ALWAYS sells immediately (never the
        TIME_WINDOW filter's whipsaw-tolerant hold). Only updates runtime state
        -- never places orders. Mutually exclusive with TW2/TEG as a tier
        (time_window_teg_filter_enabled or time_window_2_filter_enabled —
        whichever one is on wins over this gate, per worker._judge_entry_gate's
        existing priority order).
        """
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

    def set_quick_profit_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the optional Quick-Profit take-profit filter.

        EXIT LOGIC ONLY — never places/changes an entry, never touches
        major_filter_enabled or sideways_filter_enabled (entry gating is
        completely independent of this toggle). Only updates runtime state.
        Takes effect from the next tick; ON makes an already-held position
        exit in full the moment its net return reaches
        config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT, on top of whichever entry
        mode (일반거래/강한 플래그/추세전환장) is currently active. OFF restores
        the existing STOP_LOSS/OPPOSITE_SIGNAL/FORCED_LIQUIDATION-only exit
        behavior exactly as before this toggle existed.

        2026-08-05: mutually exclusive with profit_lock_enabled (docs §10
        Profit Lock spec) — turning this ON while Profit Lock is already ON
        is refused; Profit Lock must be turned off first.
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        if enabled_bool and bool(state.profit_lock_enabled):
            return {"ok": False, "message": "PROFIT_LOCK_ALREADY_ON", "quick_profit_enabled": bool(state.quick_profit_enabled)}
        prev = bool(state.quick_profit_enabled)
        state.quick_profit_enabled = enabled_bool
        state.quick_profit_enabled_at = datetime.now(KST).isoformat()
        state.quick_profit_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "quick_profit_enabled": enabled_bool,
            "previous": prev,
            "quick_profit_enabled_at": state.quick_profit_enabled_at,
            "quick_profit_enabled_by": state.quick_profit_enabled_by,
        }

    def set_profit_lock_enabled(self, enabled: bool, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI command: toggle the Profit Lock MACD-convergence early exit
        (docs §10 2026-08-05 spec — replaces the old net-return-giveback
        Profit Lock entirely).

        EXIT LOGIC ONLY — never places/changes an entry, never touches
        major_filter_enabled/sideways_filter_enabled/Stop Loss/forced
        liquidation/opposite-flag switching. Only updates runtime state;
        takes effect from the next newly-completed WATCH_SYMBOL 3-minute bar.
        Default OFF (2026-08-05: all filters default OFF). Mutually exclusive with quick_profit_enabled — turning
        this ON while Quick Profit is already ON is refused; Quick Profit
        must be turned off first. OFF disables the Profit Lock exit
        completely (existing STOP_LOSS/OPPOSITE_SIGNAL/FORCED_LIQUIDATION/
        QUICK_PROFIT behavior is entirely unaffected either way).
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
        if enabled_bool and bool(state.quick_profit_enabled):
            return {"ok": False, "message": "QUICK_PROFIT_ALREADY_ON", "profit_lock_enabled": bool(state.profit_lock_enabled)}
        prev = bool(state.profit_lock_enabled)
        state.profit_lock_enabled = enabled_bool
        state.profit_lock_enabled_at = datetime.now(KST).isoformat()
        state.profit_lock_enabled_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {
            "ok": True,
            "profit_lock_enabled": enabled_bool,
            "previous": prev,
            "profit_lock_enabled_at": state.profit_lock_enabled_at,
            "profit_lock_enabled_by": state.profit_lock_enabled_by,
        }

    def arm_scheduled_entry(self, direction: str, *, changed_by: str = "ui") -> dict[str, Any]:
        """UI "09:03 예약 매수" 버튼 (2026-08-06) — 지금 즉시 매수하지 않고,
        오늘 config.SCHEDULED_ENTRY_TIME(09:03) 이후 첫 tick에 worker.run_once가
        자동으로 지정 방향 ETF를 예산 내 전량매수하도록 예약만 한다(개장 직후
        데이터 부족으로 이른 시간대 MACD 플래그를 놓치기 쉬운 문제 대응).
        같은 방향 버튼을 다시 누르면 예약 해제(토글) — 다른 방향을 누르면
        기존 예약을 그 방향으로 교체한다. 오늘 이미 체결/포기됐으면
        (scheduled_entry_executed_at) 재예약을 거부한다(하루 1회).
        Only updates runtime state — never places an order itself; the
        actual buy happens inside worker.run_once at fire time, using the
        SAME order_executor.execute_signal path as manual_entry, so it is
        recorded in both ledgers and managed by the normal held-position
        priority chain (손절/반대플래그청산/프로핏락/퀵프로핏) afterward.
        """
        if direction not in (Direction.UP_RED.value, Direction.DOWN_BLUE.value):
            return {"ok": False, "message": "INVALID_DIRECTION"}
        state = state_store.load_state()
        # 2026-09-16 사고 수정 (1차 방어): 3-SLOT 계열에서는 예약 자체를 만들지
        # 않는다. 여기서 막아야 state 에 armed_direction 이 아예 생기지 않는다.
        if not time_window_3slot.scheduled_entry_supported(state):
            return {"ok": False,
                    "message": config.SCHEDULED_ENTRY_NOT_SUPPORTED_IN_MODE,
                    "mode": time_window_3slot.active_3slot_mode(state)}
        if state.scheduled_entry_executed_at:
            return {"ok": False, "message": "ALREADY_DECIDED_TODAY", "last_result": state.scheduled_entry_last_result}

        direction_enum = Direction(direction)
        now = datetime.now(KST)
        if state.scheduled_entry_armed_direction == direction_enum:
            state.scheduled_entry_armed_direction = None
            state.scheduled_entry_armed_at = None
            state.scheduled_entry_armed_by = None
            state_store.save_state(state)
            return {"ok": True, "armed": False, "direction": direction}

        state.scheduled_entry_armed_direction = direction_enum
        state.scheduled_entry_armed_at = now.isoformat()
        state.scheduled_entry_armed_by = str(changed_by or "ui")
        state_store.save_state(state)
        return {"ok": True, "armed": True, "direction": direction, "armed_at": state.scheduled_entry_armed_at}

    def manual_entry(self, direction: str) -> dict[str, Any]:
        """UI 수동 진입 버튼 ("현재시점 레버리지/인버스 전량매수") — 2026-08-04
        추가. MACD 신호 확정이나 강한 플래그/추세전환장 필터를 전혀 거치지
        않고, 지정한 방향의 ETF를 예산 내에서 즉시 지정가 매수한다(기존
        order_executor.execute_signal을 그대로 재사용 — 별도 매수 로직
        재구현 없음). 프리마켓 등 시스템이 못 본 신호를 사람이 판단해서
        수동으로 진입시키는 용도이므로, 이미 포지션을 보유 중이면 거부하고
        아무 것도 하지 않는다(전량매도 후 스위칭은 이 버튼의 범위 밖).
        체결 성공 시 이후의 손절/퀵프로핏/반대플래그청산/강제청산은 전부
        기존 run_once 로직이 정상적으로 이 포지션을 관리한다. 체결원장은
        execute_signal이 이미 기록하고, 신호원장에는 signal_type=
        "MANUAL_ENTRY"로 별도 기록한다.
        """
        if direction not in (Direction.UP_RED.value, Direction.DOWN_BLUE.value):
            return {"ok": False, "message": "INVALID_DIRECTION"}
        if self._worker is None or not self._worker.is_alive():
            return {"ok": False, "message": "WORKER_NOT_RUNNING"}
        if self._broker is None or self._market_data is None:
            return {"ok": False, "message": "NOT_STARTED"}

        state = state_store.load_state()
        if not state.auto_trade_on:
            return {"ok": False, "message": "AUTO_TRADE_OFF"}
        if state.position is not None and state.position.quantity > 0:
            return {"ok": False, "message": "ALREADY_HOLDING_POSITION"}

        direction_enum = Direction(direction)
        target_symbol = order_executor.target_symbol_for_direction(direction_enum)
        now = datetime.now(KST)
        quote_snap = self._market_data.get_quote(target_symbol)
        if quote_snap is None or quote_snap.error or quote_snap.price <= 0:
            return {"ok": False, "message": "QUOTE_UNAVAILABLE"}

        signal_id = f"MANUAL_{direction}_{now.strftime('%Y%m%d%H%M%S')}"
        outcome = order_executor.execute_signal(
            broker=self._broker, direction=direction_enum, signal_id=signal_id,
            quotes={target_symbol: quote_snap.price}, position=None, budget=state.budget,
            reconcile_retries=ORDER_FILL_RECONCILE_RETRIES,
            reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
        )

        if outcome.final_state == SignalState.EXECUTED:
            _apply_switch_outcome(state, outcome, direction_enum, now)
        else:
            state.order_block_reason = outcome.block_reason
        _record_manual_entry_signal(state, direction_enum, signal_id, now, outcome)
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
        """UI 수동 진입 버튼과 짝을 이루는 "수동 전량매도" 버튼 (2026-08-04
        추가) — 현재 보유 중인 포지션을 지금 즉시 시장가로 전량 매도한다
        (기존 order_executor.execute_exit를 그대로 재사용 — STOP_LOSS/
        FORCED_LIQUIDATION과 동일한, 이미 검증된 매도 경로). "자동매매 중지
        및 일괄매도"와 달리 auto_trade_on은 그대로 두므로, 다음 확정 신호부터
        기존 run_once 로직이 계속 정상적으로 감시/매매한다. 체결원장은
        execute_exit가 이미 기록하고, 신호원장에는 signal_type=
        "MANUAL_LIQUIDATION"으로 별도 기록해 수동매수 버튼과 동일하게
        원장에서 추적 가능하게 한다.
        """
        # 2026-09-21 실사고: 이 버튼이 `state.position` 만 보고 있었다.
        # 런타임 상태가 브로커와 어긋난 순간(= 정확히 비상매도가 필요한 순간)
        # `NO_POSITION_TO_SELL` 로 조용히 실패했고, 사용자는 KIS 앱에서 직접
        # 팔아야 했다. 그 수동매도가 시스템 밖 청산이 되어 stale H50 사고로
        # 이어졌다. 비상 매도 경로는 **브로커를 권위로** 삼는다.
        #
        #   - worker 스레드 생존은 요구하지 않는다(워커가 죽어도 팔 수 있어야 한다)
        #   - auto_trade_on 도 요구하지 않는다(보유 중이면 언제든 팔 수 있어야 한다)
        #   - state.position 은 진입가(avg_price) 보정에만 쓴다
        if self._broker is None:
            return {"ok": False, "message": "NOT_STARTED"}

        state = state_store.load_state()
        broker_qty = 0
        broker_symbol = None
        broker_avg = 0.0
        broker_known = True          # 조회가 성공해 '실제 보유수량' 을 아는가
        fetch_error = None
        try:
            for bp in self._broker.get_positions():
                sym = str(getattr(bp, "symbol", "") or "")
                qty = int(getattr(bp, "quantity", 0) or 0)
                if sym in config.TRADE_SYMBOLS and qty > 0:
                    broker_symbol, broker_qty = sym, qty
                    broker_avg = float(getattr(bp, "avg_price", 0.0) or 0.0)
                    break
        except Exception as exc:
            log.exception("[MACD2] manual_exit: broker position fetch failed")
            broker_known = False
            fetch_error = exc

        if broker_symbol is not None:
            # 브로커가 보유분을 알려줬다 — 그 수량이 권위다.
            pos = PositionSnapshot(
                symbol=broker_symbol, quantity=broker_qty,
                avg_price=(float(state.position.avg_price)
                           if (state.position is not None
                               and state.position.symbol == broker_symbol
                               and state.position.avg_price)
                           else broker_avg),
                entry_at=(state.position.entry_at if state.position is not None
                          else datetime.now(KST)),
            )
        elif broker_known:
            # 조회는 성공했고 결과가 '보유 0' 이다. 로컬에 잔재가 남아 있어도
            # **허수 매도를 내지 않는다** — 계좌에 없는 수량을 파는 주문은
            # 거부될 뿐이고, 원장에 유령 행만 남긴다.
            return {"ok": False, "message": "NO_POSITION_TO_SELL"}
        elif state.position is not None and state.position.quantity > 0:
            # 조회 '실패' 는 '보유 0' 과 다르다 — 로컬이 알고 있으면 그걸로 판다.
            pos = state.position
        else:
            return {"ok": False, "message": f"POSITIONS_FETCH_FAILED:{fetch_error!r}"}

        now = datetime.now(KST)
        signal_id = f"MANUAL_EXIT_{pos.symbol}_{now.strftime('%Y%m%d%H%M%S')}"
        outcome = order_executor.execute_exit(
            broker=self._broker, symbol=pos.symbol, quantity=pos.quantity,
            exit_reason=config.EXIT_MANUAL_LIQUIDATION, entry_price=pos.avg_price,
            reconcile_retries=ORDER_FILL_RECONCILE_RETRIES,
            reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
        )

        if pos.symbol == config.LONG_SYMBOL:
            direction = Direction.UP_RED
        elif pos.symbol == config.INVERSE_SYMBOL:
            direction = Direction.DOWN_BLUE
        else:
            direction = None
        if outcome.final_state == SignalState.EXECUTED:
            _apply_exit_outcome(state, outcome)
        else:
            state.order_block_reason = outcome.block_reason
        _record_manual_liquidation_signal(state, pos.symbol, direction, signal_id, now, outcome, "MANUAL_LIQUIDATION")
        state_store.save_state(state)

        return {
            "ok": outcome.final_state == SignalState.EXECUTED,
            "final_state": outcome.final_state.value,
            "block_reason": outcome.block_reason,
            "symbol": pos.symbol,
            "quantity": pos.quantity,
            "price": outcome.sell_result.executed_price if outcome.sell_result else None,
        }

    def get_snapshot(self) -> dict[str, Any]:
        state = state_store.load_state()
        state = self._persist_worker_stall_if_needed(state)
        # history updater watchdog -- worker 루프 밖에서 도는 유일한 지점이다
        # (2026-09-17). 정상일 때는 완전한 no-op 이고, 어떤 경우에도 예외를
        # 위로 던지지 않는다: 진단 패널 한 칸 때문에 대시보드 전체가 죽으면
        # 안 된다.
        try:
            history_watchdog = self._history_watchdog(state)
        except Exception as exc:  # pragma: no cover - 방어용
            history_watchdog = {"verdict": None, "action": f"WATCHDOG_ERROR: {exc}"[:200]}
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
            # ── 데이터 수집 스레드 진단 (2026-09-17) ──────────────────────
            # thread alive 와 data fresh 는 서로 다른 문제라 따로 노출한다 --
            # 2026-09-16 은 정확히 "앞은 참인데 뒤가 거짓"인 경우였다.
            "history_watchdog": history_watchdog,
            # 진단 키는 있으면 붙이고 없으면 조용히 건너뛴다 -- quote_statuses/
            # quote_status 가 이미 쓰는 것과 같은 방어 규약(테스트/구버전
            # MarketData 더블이 이 메서드를 갖고 있지 않을 수 있다).
            **_history_diag_of(self._market_data),
        }

    def supervisor_status(self) -> dict[str, Any]:
        state = self._persist_worker_stall_if_needed(state_store.load_state())
        try:
            self._history_watchdog(state)
        except Exception:  # pragma: no cover - 방어용
            pass
        stats = self._worker.tick_stats() if self._worker is not None else {}
        worker_alive = bool(self._worker and self._worker.is_alive())
        return {
            "worker_alive": worker_alive,
            "runtime_ui_mode": state.ui_mode.value,
            "order_block_reason": state.order_block_reason,
            "active_worker_count": 1 if worker_alive else 0,
            "quote_updater_alive": bool(self._market_data and self._market_data.quote_updater_alive()),
            "history_updater_alive": bool(self._market_data and self._market_data.history_updater_alive()),
            # 진단 키는 있으면 붙이고 없으면 조용히 건너뛴다 -- quote_statuses/
            # quote_status 가 이미 쓰는 것과 같은 방어 규약(테스트/구버전
            # MarketData 더블이 이 메서드를 갖고 있지 않을 수 있다).
            **_history_diag_of(self._market_data),
            "history_watchdog": dict(self._history_watchdog_last),
            "bootstrap_attempts": self._bootstrap_attempts,
            "bootstrap_last_attempt_at": self._last_bootstrap_at,
            **stats,
        }


_service_instance: Optional[Macd2Service] = None
_service_instance_lock = threading.Lock()


def get_service() -> Macd2Service:
    """Process-level singleton — the UI must call this, never construct its
    own Macd2Service/Worker/MarketDataService (docs §14/§16).

    2026-09-03 real incident fix: the check-then-set on ``_service_instance``
    below was completely unguarded. Streamlit runs each browser session's
    script execution on its own thread within the SAME process, so two
    sessions requesting this page for the first time at nearly the same
    moment (e.g. right after a cold start/redeploy) could both observe
    ``_service_instance is None`` and each construct their OWN Macd2Service
    -- each with its own Worker, MarketDataService, and (once .start() is
    called on it, e.g. via auto-recover) its own independently-ticking
    thread. Whichever object wins the final module-level assignment becomes
    "the" service everything AFTER that moment talks to, but the LOSING
    Macd2Service's Worker thread (daemon=True, nothing left holding a
    reference to stop it) keeps running forever, invisibly, doing its own
    load-state/tick/save-state cycle against the SAME shared state.json/
    signal-ledger CSV with zero coordination with the winning instance.
    Real evidence this happened: 2026-09-03's signal ledger shows two rows
    written ~7 seconds apart with different worker_instance_id but the same
    session_started_at, one using macd_snap data a full 24 minutes stale
    relative to the other -- several T+3 candidates that day resolved with
    no ledger trace at all, consistent with this exact kind of uncoordinated
    concurrent read-modify-write corrupting/losing state between the two
    loops. Double-checked locking with a plain threading.Lock closes the
    race -- the second thread now blocks on the lock and returns the SAME
    already-constructed instance instead of building its own."""
    global _service_instance
    if _service_instance is None:
        with _service_instance_lock:
            if _service_instance is None:
                _service_instance = Macd2Service()
    return _service_instance
