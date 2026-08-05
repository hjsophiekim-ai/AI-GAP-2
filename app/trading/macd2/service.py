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
from app.trading.macd2 import config, ledger, order_executor, state_store
from app.trading.macd2.broker_adapter import create_macd2_broker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, RuntimeStatus, SignalState
from app.trading.macd2.signal_engine import calculate_macd, resample_completed_3m
from app.trading.macd2.worker import (
    ORDER_FILL_RECONCILE_DELAY_SEC,
    ORDER_FILL_RECONCILE_RETRIES,
    Macd2Worker,
    _apply_exit_outcome,
    _apply_switch_outcome,
    _parse_iso_dt,
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


class Macd2Service:
    """Owns the MarketDataService/broker/Worker for one MACD2 run."""

    def __init__(self) -> None:
        self._market_data: Optional[MarketDataService] = None
        self._broker = None
        self._worker: Optional[Macd2Worker] = None
        self._bootstrap_attempts: int = 0
        self._last_bootstrap_at: Optional[str] = None
        self._last_bootstrap_result: Optional[dict[str, Any]] = None

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
        if state.mode != "mock":
            return False
        last_attempt = _parse_iso_dt(state.last_auto_recover_attempt_at)
        now = datetime.now(KST)
        if last_attempt is not None and (now - last_attempt).total_seconds() < config.WORKER_AUTO_RECOVER_COOLDOWN_SEC:
            return False
        state.last_auto_recover_attempt_at = now.isoformat()
        state_store.save_state(state)
        result = self.start(mode=state.mode, budget=state.budget)
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
        """
        state = state_store.load_state()
        enabled_bool = bool(enabled)
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
        if self._worker is None or not self._worker.is_alive():
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
