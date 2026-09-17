"""history updater watchdog — 2026-09-17 (2026-09-16 14:04 정지 사고 대응).

사고
----
14:04 이후 현재가/1분봉이 동시에 멈췄고 프로세스를 재시작하기 전까지 복구되지
않았다. 확정된 구조적 결함: (1) history updater 는 죽음/정체 감지와 자가복구가
**전혀 없었다**, (2) 루프가 `except Exception: pass` 로 전부 삼켜 "살아서 성공
중"과 "살아서 계속 실패 중"을 구분할 수 없었다, (3) 시세 자가복구는
`worker._run_loop` **안에** 있어서 루프 자체가 멈추면 같이 죽는다.

이 파일이 고정하는 계약
-----------------------
1. 정상 updater -> 재기동 0
2. dead updater -> 1회 복구
3. alive-but-stale (thread 는 살아 있는데 새 봉이 180초 이상 없음) -> 1회 복구
4. 복구 후 새 봉이 들어오면 stale age 가 초기화된다
5. 기존 스레드 종료 미확인 -> HISTORY_UPDATER_RECOVERY_BLOCKED, **중복 updater 0**
6. 연속 폭주 0 (쿨다운)
7. 비거래시간/주말/장시작 grace 에서는 복구하지 않는다
8. 자동매매 OFF 면 되살리지 않는다
9. REAL 에서도 **데이터 수집 스레드만** 복구한다 — worker 재시작/주문 0
10. stale 동안 신규진입 차단은 기존 게이트가 그대로 유지한다
11. watchdog 은 worker 와 독립 경로(get_snapshot)에서 돈다
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, market_data as md_module, service as service_module
from app.trading.macd2 import state_store
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import RuntimeState

KST = config.KST
_TRADING_NOW = datetime(2026, 9, 17, 13, 0, tzinfo=KST)   # 목요일 장중


# ── helpers ───────────────────────────────────────────────────────────────

def _bars(start: datetime, n: int) -> pd.DataFrame:
    return pd.DataFrame([
        {"datetime": start + timedelta(minutes=i), "open": 100.0, "high": 100.1,
         "low": 99.9, "close": 100.0, "volume": 10}
        for i in range(n)
    ])


def _svc(fetch=None) -> MarketDataService:
    """네트워크 없는 MarketDataService."""
    frames = {"df": _bars(_TRADING_NOW - timedelta(minutes=10), 10)}

    def _default_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return frames["df"], {"received_count": len(frames["df"])}

    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=fetch or _default_fetch,
        fetch_quote=lambda mode, symbol: (100.0, None),
    )
    svc._frames_for_test = frames  # 테스트에서 새 봉을 밀어 넣기 위한 핸들
    return svc


def _service_with(md: MarketDataService, *, auto_trade_on=True, mode="mock"):
    svc = service_module.Macd2Service()
    svc._market_data = md
    state = state_store.default_state()
    state.auto_trade_on = auto_trade_on
    state.mode = mode
    return svc, state


def _freeze_success(md, at) -> None:
    """updater 스레드가 실시간 시계로 기록해 버린 값을 덮어써 테스트를 결정적으로
    만든다 (스레드는 시작하자마자 한 번 fetch 하므로 그대로 두면 판정이 실행
    시각에 따라 흔들린다)."""
    md._history_last_success_at = at
    md._history_newest_bar_at = at
    md._history_last_recovered_at = None


def _idle_fetch(mode, symbol, count, hour1):
    """오류도 없고 새 봉도 없는 응답 = alive-but-stale 의 실제 모습."""
    del mode, symbol, count, hour1
    return md_module._empty_1m_frame(), {"received_count": 0}


def _history_threads() -> int:
    return sum(1 for t in threading.enumerate()
               if t.name == "macd2-history-updater" and t.is_alive())


@pytest.fixture(autouse=True)
def _no_stray_updater_threads():
    """테스트가 남긴 수집 스레드가 다음 테스트로 새지 않게 한다."""
    before = _history_threads()
    yield
    for t in threading.enumerate():
        if t.name == "macd2-history-updater" and t.is_alive():
            t.join(timeout=0.1)
    assert _history_threads() <= before + 0, "테스트가 살아있는 updater 스레드를 남겼다"


# ── 1. 진단값이 실제로 기록되는가 (예전에는 전부 삼켰다) ───────────────────

def test_successful_fetch_records_success_time_and_clears_error():
    md = _svc()
    md.merge_incremental_1m(now=_TRADING_NOW)
    diag = md.history_diag(now=_TRADING_NOW)
    assert diag["history_last_success_at"] is not None
    assert diag["history_last_attempt_at"] is not None
    assert diag["history_last_error"] is None
    assert diag["history_consecutive_failures"] == 0
    assert diag["history_stale_age_sec"] == pytest.approx(0.0, abs=1.0)


def test_fetch_error_is_recorded_instead_of_silently_swallowed():
    def _boom(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return md_module._empty_1m_frame(), {"error": "KIS 500"}

    md = _svc(fetch=_boom)
    md.merge_incremental_1m(now=_TRADING_NOW)
    md.merge_incremental_1m(now=_TRADING_NOW)
    diag = md.history_diag(now=_TRADING_NOW)
    assert diag["history_last_error"] == "KIS 500"
    assert diag["history_consecutive_failures"] == 2
    assert diag["history_last_success_at"] is None


def test_an_error_free_but_empty_response_is_not_counted_as_success():
    """alive-but-stale 의 정체 — KIS 가 오류 없이 빈 페이지를 주는 경우다.
    이것을 성공으로 세면 감시 자체가 무의미해진다."""
    def _empty(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return md_module._empty_1m_frame(), {"received_count": 0}

    md = _svc(fetch=_empty)
    md.merge_incremental_1m(now=_TRADING_NOW)
    assert md.history_diag(now=_TRADING_NOW)["history_last_success_at"] is None


def test_repeating_the_same_bars_is_not_a_new_success():
    """같은 봉만 계속 돌려받는 것도 데이터가 전진하지 않는 것이다."""
    md = _svc()
    md.merge_incremental_1m(now=_TRADING_NOW)
    first = md.history_diag(now=_TRADING_NOW)["history_last_success_at"]
    md.merge_incremental_1m(now=_TRADING_NOW + timedelta(seconds=300))
    assert md.history_diag()["history_last_success_at"] == first


# ── 2. watchdog 판정 ──────────────────────────────────────────────────────

def test_healthy_updater_triggers_no_recovery():
    md = _svc(fetch=_idle_fetch)
    md.start_history_updater(interval_sec=3600.0)
    try:
        _freeze_success(md, _TRADING_NOW)
        svc, state = _service_with(md)
        out = svc._history_watchdog(state, now=_TRADING_NOW + timedelta(seconds=5))
        assert out["verdict"] is None
        assert out["action"] is None
        assert md.history_diag()["history_recovery_count"] == 0
    finally:
        md.stop_history_updater()


def test_dead_updater_is_detected_and_recovered_exactly_once():
    md = _svc(fetch=_idle_fetch)
    _freeze_success(md, _TRADING_NOW)
    assert not md.history_updater_alive()      # 아직 시작하지 않음 = DEAD
    svc, state = _service_with(md)
    try:
        out = svc._history_watchdog(state, now=_TRADING_NOW + timedelta(seconds=5))
        assert out["verdict"] == config.HISTORY_UPDATER_DEAD
        assert out["action"] == config.HISTORY_UPDATER_RECOVERED
        assert md.history_updater_alive()
        assert md.live_history_updater_count() == 1
        assert md.history_diag()["history_recovery_count"] == 1
    finally:
        md.stop_history_updater()


def test_alive_but_stale_updater_is_detected_and_recovered():
    md = _svc(fetch=_idle_fetch)
    md.start_history_updater(interval_sec=3600.0)   # 살아 있지만 새 봉이 없다
    try:
        _freeze_success(md, _TRADING_NOW)
        assert md.history_updater_alive()
        svc, state = _service_with(md)
        stale_now = _TRADING_NOW + timedelta(seconds=config.HISTORY_STALE_MAX_SEC + 30)
        out = svc._history_watchdog(state, now=stale_now)
        assert out["verdict"] == config.HISTORY_UPDATER_ALIVE_BUT_STALE
        assert out["action"] == config.HISTORY_UPDATER_RECOVERED
        assert md.live_history_updater_count() == 1, "중복 updater 가 생겼다"
        assert md.history_diag()["history_recovery_count"] == 1
    finally:
        md.stop_history_updater()


def test_stale_age_resets_once_new_bars_arrive_again():
    md = _svc()
    md.merge_incremental_1m(now=_TRADING_NOW)
    late = _TRADING_NOW + timedelta(seconds=600)
    assert md.history_stale_age_sec(late) > config.HISTORY_STALE_MAX_SEC
    # 새 봉 도착
    md._frames_for_test["df"] = _bars(_TRADING_NOW + timedelta(minutes=10), 5)
    md.merge_incremental_1m(now=late)
    assert md.history_stale_age_sec(late) == pytest.approx(0.0, abs=1.0)


# ── 3. 중복 금지 / 폭주 금지 ───────────────────────────────────────────────

def test_unconfirmed_stop_blocks_recovery_and_never_adds_a_second_updater():
    """종료가 확인되지 않으면 새 스레드를 억지로 올리지 않는다.
    (monkeypatch 대신 인스턴스 속성만 바꾼다 — tests/macd2 에서 monkeypatch.undo()
    는 conftest 의 원장 격리까지 풀어 버리므로 쓰지 않는다.)"""
    md = _svc(fetch=_idle_fetch)
    md.start_history_updater(interval_sec=3600.0)
    real_stop = md.stop_history_updater
    try:
        before = md.live_history_updater_count()
        md.stop_history_updater = lambda **kw: False        # 종료 미확인 시뮬레이션
        result = md.recover_history_updater(now=_TRADING_NOW)
        assert result == config.HISTORY_UPDATER_RECOVERY_BLOCKED
        assert md.live_history_updater_count() == before, "중복 updater 가 생겼다"
        assert md.history_diag()["history_recovery_count"] == 0
        assert md.history_diag()["history_last_recovery_result"] == config.HISTORY_UPDATER_RECOVERY_BLOCKED
    finally:
        md.stop_history_updater = real_stop
        real_stop()


def test_recovery_is_rate_limited_so_it_never_storms():
    md = _svc(fetch=_idle_fetch)
    _freeze_success(md, _TRADING_NOW)
    svc, state = _service_with(md)
    try:
        t0 = _TRADING_NOW + timedelta(seconds=5)
        assert svc._history_watchdog(state, now=t0)["action"] == config.HISTORY_UPDATER_RECOVERED
        md.stop_history_updater()
        # 쿨다운 안에서는 몇 번을 불러도 재기동하지 않는다
        for i in range(1, 6):
            out = svc._history_watchdog(state, now=t0 + timedelta(seconds=i))
            assert out["action"] == "COOLDOWN", out
        assert md.history_diag()["history_recovery_count"] == 1
    finally:
        md.stop_history_updater()


def test_retry_interval_backs_off_after_repeated_failures():
    """연속 실패가 쌓이면 재시도 간격이 늘어난다(포기하지는 않는다)."""
    md = _svc(fetch=_idle_fetch)
    _freeze_success(md, _TRADING_NOW)
    svc, state = _service_with(md)
    try:
        t = _TRADING_NOW + timedelta(seconds=5)
        for _ in range(config.HISTORY_WATCHDOG_FAST_RETRY_LIMIT):
            svc._history_watchdog(state, now=t)
            md.stop_history_updater()
            t += timedelta(seconds=config.WORKER_AUTO_RECOVER_COOLDOWN_SEC + 1)
        # 이제 30초 간격으로는 더 이상 재시도하지 않는다
        out = svc._history_watchdog(
            state, now=t + timedelta(seconds=config.WORKER_AUTO_RECOVER_COOLDOWN_SEC + 1))
        assert out["action"] == "COOLDOWN"
        # 300초를 넘기면 다시 시도한다
        out = svc._history_watchdog(
            state, now=t + timedelta(seconds=config.QUOTE_UPDATER_FORCE_REPLACE_AGE_SEC + 1))
        assert out["action"] == config.HISTORY_UPDATER_RECOVERED
    finally:
        md.stop_history_updater()


# ── 4. 적용 구간 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("when,label", [
    (datetime(2026, 9, 17, 8, 30, tzinfo=KST), "프리마켓"),
    (datetime(2026, 9, 17, 9, 0, 30, tzinfo=KST), "장시작 grace"),
    (datetime(2026, 9, 17, 15, 30, tzinfo=KST), "장 마감 후"),
    (datetime(2026, 9, 19, 13, 0, tzinfo=KST), "토요일"),
])
def test_no_recovery_outside_the_trading_window(when, label):
    md = _svc(fetch=_idle_fetch)
    _freeze_success(md, _TRADING_NOW)
    svc, state = _service_with(md)
    out = svc._history_watchdog(state, now=when)
    assert out["action"] in (None, "OUT_OF_SESSION"), label
    assert not md.history_updater_alive(), label
    assert md.history_diag()["history_recovery_count"] == 0, label


def test_auto_trade_off_is_never_resurrected():
    """의도적으로 내려둔 상태를 watchdog 이 되살리면 안 된다."""
    md = _svc(fetch=_idle_fetch)
    svc, state = _service_with(md, auto_trade_on=False)
    out = svc._history_watchdog(state, now=_TRADING_NOW)
    assert out["action"] is None
    assert not md.history_updater_alive()


# ── 5. REAL 에서도 데이터 스레드만 — worker/주문은 건드리지 않는다 ─────────

def test_real_mode_recovers_the_data_thread_but_never_the_worker(monkeypatch):
    md = _svc(fetch=_idle_fetch)
    _freeze_success(md, _TRADING_NOW)
    svc, state = _service_with(md, mode="real")
    calls = {"start": 0, "auto_recover": 0}
    monkeypatch.setattr(svc, "start", lambda *a, **kw: calls.__setitem__("start", calls["start"] + 1))
    monkeypatch.setattr(
        svc, "_auto_recover_worker",
        lambda *a, **kw: calls.__setitem__("auto_recover", calls["auto_recover"] + 1))
    try:
        out = svc._history_watchdog(state, now=_TRADING_NOW + timedelta(seconds=5))
        assert out["action"] == config.HISTORY_UPDATER_RECOVERED, "REAL 에서도 데이터 복구는 된다"
        assert md.history_updater_alive()
        assert calls == {"start": 0, "auto_recover": 0}, "worker 를 건드렸다"
        assert svc._worker is None
    finally:
        md.stop_history_updater()


def test_watchdog_never_touches_strategy_state():
    md = _svc(fetch=_idle_fetch)
    _freeze_success(md, _TRADING_NOW)
    svc, state = _service_with(md)
    snapshot_before = dict(vars(state))
    try:
        svc._history_watchdog(state, now=_TRADING_NOW + timedelta(seconds=5))
        assert dict(vars(state)) == snapshot_before, "watchdog 이 전략 state 를 바꿨다"
    finally:
        md.stop_history_updater()


# ── 6. stale 동안 신규진입 차단은 기존 게이트가 유지 ───────────────────────

def test_stale_history_still_blocks_new_entries_through_the_existing_gate():
    """이 hotfix 는 새 차단 게이트를 만들지 않는다 — 데이터가 낡으면
    `_update_history_freshness_diag` 가 HISTORY_STALE 을 세우고, run_once 의
    `entry_window_open` 이 그 값을 보고 신규진입을 막는 기존 동작 그대로다."""
    from app.trading.macd2.worker import _update_history_freshness_diag

    state = state_store.default_state()
    now = _TRADING_NOW
    # 마지막 봉이 임계값보다 확실히 오래됐어야 한다(_bars 는 start 부터 +1분씩).
    stale_df = _bars(now - timedelta(seconds=config.HISTORY_STALE_MAX_SEC * 3), 3)
    _update_history_freshness_diag(
        state, df_1m=stale_df, macd_snap=None, watch_price=None, now=now)
    assert state.quote_history_mismatch_reason == "HISTORY_STALE"

    fresh_df = _bars(now - timedelta(seconds=30), 3)
    _update_history_freshness_diag(
        state, df_1m=fresh_df, macd_snap=None, watch_price=None, now=now)
    assert state.quote_history_mismatch_reason is None


# ── 7. worker 와 독립 경로에서 돈다 ───────────────────────────────────────

def test_watchdog_runs_from_the_snapshot_path_even_with_no_worker(monkeypatch):
    """2026-09-16 은 worker 루프 자체가 안 돌았다. 그러므로 watchdog 이
    worker 루프 안에 있으면 의미가 없다 — worker 가 아예 없어도 동작해야 한다."""
    md = _svc(fetch=_idle_fetch)
    _freeze_success(md, _TRADING_NOW)
    svc, state = _service_with(md)
    assert svc._worker is None
    monkeypatch.setattr(service_module.state_store, "load_state", lambda: state)
    monkeypatch.setattr(svc, "_persist_worker_stall_if_needed", lambda s: s)
    try:
        called = {}

        def _spy(st, now=None):
            called["hit"] = True
            return {"verdict": None, "action": None}

        monkeypatch.setattr(svc, "_history_watchdog", _spy)
        snap = svc.get_snapshot()
        assert called.get("hit") is True
        assert "history_watchdog" in snap
        assert "history_updater_alive" in snap
        assert "history_stale_age_sec" in snap
    finally:
        md.stop_history_updater()


def test_snapshot_separates_thread_alive_from_data_fresh():
    md = _svc(fetch=_idle_fetch)
    md.start_history_updater(interval_sec=3600.0)
    try:
        _freeze_success(md, _TRADING_NOW)
        diag = md.history_diag(now=_TRADING_NOW + timedelta(seconds=600))
        assert diag["history_updater_alive"] is True          # thread alive
        assert diag["history_stale_age_sec"] > config.HISTORY_STALE_MAX_SEC   # data NOT fresh
    finally:
        md.stop_history_updater()


# ── 8. history 가 죽어 있는 동안에도 기존 포지션 안전로직은 그대로 ─────────

def test_risk_management_still_runs_while_history_is_dead_and_no_new_entry():
    """이 hotfix 가 지켜야 하는 마지막 조건: 1분봉이 끊긴 동안
    **신규 진입은 막히되 보유 포지션의 하드스톱은 계속 동작**해야 한다.

    history 가 통째로 비어 있는 워커 tick(= 2026-09-16 14:04 이후의 모습)을
    그대로 재현한다 — `_update_history_freshness_diag` 가 HISTORY_EMPTY 를
    세워 신규진입을 막고, `_advance_held_position_risk_management` 는 그와
    무관하게 손절을 낸다."""
    from tests.macd2.test_worker_held_position_risk_management_warmup import (
        _cold_market_data, _fresh_state, _seed_held_since,
    )
    from app.trading.macd2.models import PositionSnapshot
    from app.trading.macd2.worker import run_once
    from tests.macd2.fake_broker import FakeBroker

    quotes = {config.LONG_SYMBOL: 14_000.0, config.INVERSE_SYMBOL: 10_000.0,
              config.WATCH_SYMBOL: 100.0}
    market_data = _cold_market_data(quotes)          # history 비어 있음
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")
    orders_before = len(broker.orders)

    state = _fresh_state()
    entered_at = datetime(2026, 1, 7, 9, 0, 0, tzinfo=KST)
    now0 = entered_at + timedelta(minutes=9)
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10,
                                      avg_price=15_000.0, entry_at=entered_at)
    _seed_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at,
                     last_bar_close=14_000.0)

    result = run_once(broker=broker, market_data=market_data, state=state, now=now0)

    # 1분봉이 하나도 없는 tick 에서도 하드스톱은 나간다.
    # _advance_held_position_risk_management 가 macd_snap/history 준비 여부와
    # 무관하게 먼저 평가되므로, 이 tick 은 손절로 끝나고 history 진단 계산까지
    # 가지도 않는다 — 그게 올바른 우선순위다(신규진입 차단 배선은 바로 아래
    # test_stale_history_is_the_reported_entry_block_reason 가 따로 고정한다).
    # 기본 전략(H50/X2-lite 계열)에서는 TW 래더가 관리하므로 라벨이
    # TIME_WINDOW_STOP_LOSS 다. 어느 쪽이든 "손절이 나갔다"가 핵심이다.
    assert any("STOP_LOSS" in a for a in result.actions), result.actions
    assert state.position is None
    # (3) 신규 매수는 한 건도 없다
    new_orders = broker.orders[orders_before:]
    assert not any(o.side == "BUY" for o in new_orders), new_orders


def test_stale_history_is_the_reported_entry_block_reason():
    """stale 이 실제로 **신규진입 차단 사유**로 쓰이는지 — 이 hotfix 는 새
    게이트를 만들지 않고 이 기존 배선에 그대로 올라탄다."""
    from app.trading.macd2.worker import _confirmed_signal_order_gate_block_reason

    state = state_store.default_state()
    mid_session = datetime(2026, 9, 17, 13, 0, tzinfo=KST)
    assert _confirmed_signal_order_gate_block_reason(state, mid_session) == "ENTRY_WINDOW_CLOSED"
    state.quote_history_mismatch_reason = "HISTORY_STALE"
    assert _confirmed_signal_order_gate_block_reason(state, mid_session) == "HISTORY_STALE"
