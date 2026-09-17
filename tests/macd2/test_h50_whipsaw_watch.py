"""H50 HOLD ↔ whipsaw-watch 연결 (2026-09-17) — worker.run_once() 경로 테스트.

배경 (2026-09-16 실거래)
------------------------
12:48 RED 진입 → 13:30 BLUE 반대 플래그 → 13:33 `H50_SMALL_WHIPSAW_HOLD` 발동.
그 뒤 BLUE 방향 MACD gap 이 −58 → −495 → −855 → −1018 → −1106 으로 계속
확대됐는데도 계속 HOLD 했다. H50 의 해제조건은 EMA20/50 구조추세 2봉 이탈과
60분 경과뿐이라, "보류가 틀렸는지" 확인할 장치가 하나도 없었기 때문이다.

TW2 / TW2 3-SLOT 의 whipsaw-hold 분기는 2026-09-02 사고 이후 정확히 그
장치(`WHIPSAW_WATCH_DETERIORATION_EXIT`)를 갖고 있는데, **H50 분기만**
`_start_whipsaw_watch` 를 부르지 않아 `whipsaw_watch_active` 가 영영 False 였고
`_advance_whipsaw_watch` 가 영구 no-op 이었다.

이 파일이 고정하는 계약
-----------------------
A. H50 OFF 이면 X2-lite + W1a 와 주문·액션·원장이 diff 0 (새 코드 완전 no-op)
B. HOLD 기본동작 — 반대 플래그를 청산하지 않고 유지, **그리고 watch 를 arm**
C. HOLD 이후 gap 이 계속 확대되면 기존 deterioration 조건으로 EXIT
D. gap 이 다시 완화되면 청산하지 않고 HOLD 유지
E. 구조추세 2봉 이탈 → 기존 H50 release 대로 EXIT
F. 최대 HOLD 시간 경과 → EXIT
G. HOLD 중에도 SL/TP/강제청산이 우선하고, 그때 H50 상태가 함께 정리된다
H. HOLD 중 반대방향 신규진입 없음
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, small_whipsaw_hold as swh, state_store, worker
from app.trading.macd2 import time_window_3slot as tw3
from app.trading.macd2.models import (
    Direction, PositionSnapshot, RuntimeState, WhipsawWatchDecision,
)
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (
    _1m_frame,
    _BOOTSTRAP_NOW,
    _PRIOR_DAY,
    _SESSION_START_NOW,
    _approved as _approved_3slot,
    _patch_common as _patch_common_3slot,
    _prime_3slot_pending,
    _quality as _quality_3slot,
    _rejected as _rejected_3slot,
    _sine_1m_closes,
    _teg as _teg_3slot,
    tw2_3slot_market_data,
)
from app.trading.macd2 import bar_archive, bar_ledger, ledger
from app.trading.macd2.market_data import MarketDataService

KST = config.KST

#: import 시점(어떤 fixture 도 돌기 전)의 기본값을 붙잡아 둔다 — 아래 autouse
#: fixture 가 켜 버리므로 테스트 안에서 `config` 를 읽으면 기본값이 아니다.
_DEFAULT_WATCH_LINK = config.H50_WHIPSAW_WATCH_ENABLED


# ── 기본값 / 켠 상태 ───────────────────────────────────────────────────────

def test_watch_link_default_is_off_after_the_2026_09_17_validation():
    """2026-09-17 재검증에서 채택기준 미달(기존 H50 대비 30일·70일 복리 둘 다
    열위)이라 **기본 OFF** 로 내렸다. 근거표는 `config.py` 의
    `H50_WHIPSAW_WATCH_ENABLED` 주석에 있다. 재검증 없이 이 기본값을 True 로
    되돌리지 말 것."""
    assert _DEFAULT_WATCH_LINK is False


@pytest.fixture(autouse=True)
def _watch_link_on(monkeypatch):
    """아래 테스트들은 "켜져 있을 때의 메커니즘"을 검증하므로 명시적으로 켠다.
    (기본 OFF 라는 사실 자체는 바로 위 테스트가 따로 고정한다.)"""
    monkeypatch.setattr(config, "H50_WHIPSAW_WATCH_ENABLED", True)


# ── helpers ───────────────────────────────────────────────────────────────

def _make_market_data():
    """`tw2_3slot_market_data` 와 같은 레시피로 **매번 새 서비스**를 만든다.
    같은 서비스 인스턴스로 두 시나리오를 연달아 돌리면 두 번째가 이미 소비된
    완성봉을 보게 돼 비교 자체가 성립하지 않는다(parity 비교 전용)."""
    df_1m = _1m_frame(_PRIOR_DAY, _sine_1m_closes(300))
    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0,
              config.WATCH_SYMBOL: 100.0}
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a, **kw: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quotes.get(symbol), None),
    )
    assert svc.bootstrap(now=_BOOTSTRAP_NOW).ok
    svc.refresh_quotes()
    return svc, _SESSION_START_NOW


def _reset_dispatch_history() -> None:
    """같은 봉/같은 signal_id 를 두 번째 시나리오에서 다시 평가할 수 있게
    "이미 처리했다" 기록을 전부 비운다 — 봉원장/아카이브의 evaluated-bar 집합
    (520f17f), 체결·신호 원장, 그리고 `ledger.try_claim_signal_dispatch` 의
    O_EXCL claim 파일. parity 비교 전용 헬퍼다."""
    bar_archive._reset_for_tests()
    bar_ledger._reset_for_tests()
    for path in (ledger.SIGNAL_LEDGER_PATH, ledger.EXECUTION_LEDGER_PATH,
                 bar_ledger.BAR_LEDGER_PATH):
        if path.exists():
            path.unlink()
    for claim in ledger.LOGS_DIR_PATH.glob("macd2_dispatch_claim_*.json"):
        claim.unlink()


def _mode_state(mode_flag: str, *, budget: float = 10_000_000.0) -> RuntimeState:
    """3-SLOT tier 에서 정확히 한 모드만 켠 state."""
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    for flag in ("time_window_2_filter_enabled", "time_window_teg_filter_enabled",
                 "time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
                 "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled"):
        setattr(state, flag, flag == mode_flag)
    return state


def _h50_state(**kw) -> RuntimeState:
    return _mode_state("time_window_h50_filter_enabled", **kw)


def _x2lite_state(**kw) -> RuntimeState:
    return _mode_state("time_window_x2lite_filter_enabled", **kw)


def _seed_inverse_position(state: RuntimeState, broker: FakeBroker, now: datetime,
                           *, mode: str, qty: int = 10, avg: float = 10_000.0) -> None:
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=qty,
                                      avg_price=avg, entry_at=now)
    state.time_window_position_active = True
    state.time_window_active_mode = mode
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    state.tw2_3slot_morning_count = 1
    broker.buy_market(config.INVERSE_SYMBOL, qty, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = avg


def _broker() -> FakeBroker:
    return FakeBroker(cash=10_000_000.0,
                      quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})


def _hold(should_hold: bool, *, trend: str = swh.TREND_DOWN, rng: float = 1.2) -> swh.HoldDecision:
    return swh.HoldDecision(
        should_hold=should_hold, trend=trend, range_pct=rng,
        trend_ok=should_hold, range_ok=should_hold, insufficient_data=False,
        reason=("SMALL_WHIPSAW_HOLD" if should_hold else "RANGE_TOO_WIDE"),
    )


def _release(should_release: bool, *, reason: str = "HOLDING",
             breaks: int = 0, elapsed: float | None = 3.0) -> swh.ReleaseDecision:
    return swh.ReleaseDecision(
        should_release=should_release, trend=swh.TREND_DOWN,
        trend_break_count=breaks, elapsed_min=elapsed, reason=reason,
    )


def _patch_h50(monkeypatch, *, hold: swh.HoldDecision,
               releases: list[swh.ReleaseDecision] | None = None):
    """`evaluate_hold` / `evaluate_release` 만 고정한다 — `is_active`/`is_holding`
    /state 헬퍼는 production 그대로 돌려 배선까지 같이 검증한다."""
    monkeypatch.setattr(worker.small_whipsaw_hold, "evaluate_hold", lambda *a, **kw: hold)
    seq = list(releases or [_release(False)])
    calls = {"n": 0}

    def _fake_release(*_a, **_kw):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(worker.small_whipsaw_hold, "evaluate_release", _fake_release)
    return calls


def _canned_watch(monkeypatch, decisions: list[WhipsawWatchDecision]):
    calls = {"n": 0}

    def _fake(*_a, **_kw):
        i = min(calls["n"], len(decisions) - 1)
        calls["n"] += 1
        return decisions[i]

    monkeypatch.setattr(worker.time_window_filter, "evaluate_whipsaw_watch", _fake)
    return calls


_WATCH_HOLD = WhipsawWatchDecision(should_sell=False, should_release=False,
                                   current_gap=58.0, current_ema_spread=12.0)
_WATCH_WORSE = WhipsawWatchDecision(should_sell=True, should_release=False,
                                    current_gap=495.0, current_ema_spread=40.0)
_WATCH_BETTER = WhipsawWatchDecision(should_sell=False, should_release=True,
                                     current_gap=-5.0, current_ema_spread=-2.0)


# ── A. H50 OFF parity — 새 코드가 완전 no-op ───────────────────────────────

def _run_opposite_flag_scenario(svc, now0, state, monkeypatch) -> tuple[list, list, RuntimeState]:
    broker = _broker()
    _seed_inverse_position(state, broker, now0,
                           mode=tw3.active_3slot_mode(state) or "TW2_3SLOT")
    _patch_common_3slot(
        monkeypatch,
        entry_decision=_approved_3slot(),
        quality_decision=_quality_3slot(True, 5),
        teg_decision=_teg_3slot(True),
    )
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)
    return list(result.actions), list(broker.orders), state


def test_h50_off_is_byte_identical_to_x2lite_on_the_same_opposite_flag(monkeypatch):
    """merge 조건 A: H50 이 꺼져 있으면 플래그/주문/액션이 X2-lite + W1a 와
    완전히 같아야 한다. 새로 추가된 `_start_whipsaw_watch` 호출이 H50 모드
    바깥으로 새지 않는다는 뜻이다."""
    svc_a, now0 = _make_market_data()
    x2_actions, x2_orders, x2_state = _run_opposite_flag_scenario(
        svc_a, now0, _x2lite_state(), monkeypatch)

    _reset_dispatch_history()

    svc_b, _ = _make_market_data()
    monkeypatch.setattr(config, "H50_ENABLED", False)
    h50_actions, h50_orders, h50_state = _run_opposite_flag_scenario(
        svc_b, now0, _h50_state(), monkeypatch)

    assert h50_actions == x2_actions
    def _legs(orders):
        return [(o.symbol, o.side, o.requested_qty, o.executed_qty, o.success)
                for o in orders]

    assert _legs(h50_orders) == _legs(x2_orders)
    assert (h50_state.position is None) == (x2_state.position is None)
    assert h50_state.whipsaw_watch_active == x2_state.whipsaw_watch_active
    assert h50_state.h50_hold_active is False


def test_watch_link_toggle_off_keeps_the_pre_2026_09_17_h50_behaviour(
    tw2_3slot_market_data, monkeypatch,
):
    """`MACD2_H50_WHIPSAW_WATCH_ENABLED=0` 이면 HOLD 는 그대로 나되 watch 는
    arm 되지 않는다 — 연결 이전 동작으로 정확히 되돌아간다."""
    svc, now0 = tw2_3slot_market_data
    monkeypatch.setattr(config, "H50_WHIPSAW_WATCH_ENABLED", False)
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in result.actions), result.actions
    assert state.h50_hold_active is True
    assert state.whipsaw_watch_active is False


# ── B. HOLD 기본동작 + watch arm ───────────────────────────────────────────

def test_small_whipsaw_hold_keeps_the_position_and_arms_the_watch(
    tw2_3slot_market_data, monkeypatch,
):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _canned_watch(monkeypatch, [_WATCH_HOLD])
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))
    orders_before = len(broker.orders)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in result.actions), result.actions
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert len(broker.orders) == orders_before, "HOLD 는 주문을 내지 않는다"
    assert state.h50_hold_active is True
    assert state.h50_original_direction == Direction.DOWN_BLUE.value
    # 2026-09-17: 여기가 이 변경의 핵심 — 예전에는 영영 False 였다.
    assert state.whipsaw_watch_active is True
    assert state.whipsaw_watch_direction == Direction.UP_RED
    assert state.whipsaw_watch_last_gap == _WATCH_HOLD.current_gap


# ── C. HOLD 이후 gap 확대 → 기존 deterioration 조건으로 EXIT ───────────────

def test_hold_then_continued_gap_deterioration_exits_via_the_existing_watch(
    tw2_3slot_market_data, monkeypatch,
):
    """2026-09-16 사건의 구조: HOLD 이후 반대방향 gap 이 계속 확대되면 기존
    production deterioration 조건(gap AND EMA spread 동시 재확대)이 EXIT 를
    낸다. H50 자신의 해제조건(추세 2봉/60분)은 아직 만족되지 않은 상태다."""
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True), releases=[_release(False)])
    _canned_watch(monkeypatch, [_WATCH_HOLD, _WATCH_WORSE])
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.whipsaw_watch_active is True
    assert state.position is not None

    result = run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))

    assert any(a.startswith(config.WHIPSAW_WATCH_DETERIORATION_EXIT) for a in result.actions), result.actions
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.whipsaw_watch_active is False
    assert state.h50_hold_active is False, "청산됐으면 H50 HOLD 상태도 같이 끝나야 한다"


# ── D. gap 이 다시 완화되면 청산하지 않고 HOLD 유지 ────────────────────────

def test_hold_survives_when_the_opposite_gap_relaxes_again(
    tw2_3slot_market_data, monkeypatch,
):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True), releases=[_release(False)])
    _canned_watch(monkeypatch, [_WATCH_HOLD, _WATCH_BETTER])
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    orders_before = len(broker.orders)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))

    assert any(a.startswith(config.WHIPSAW_WATCH_RELEASED) for a in result.actions), result.actions
    assert len(broker.orders) == orders_before, "release 는 주문을 내지 않는다"
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert state.whipsaw_watch_active is False
    assert state.h50_hold_active is True, "watch 만 끝나고 H50 HOLD 는 계속된다"


# ── E/F. H50 자신의 해제조건은 그대로 살아 있다 ────────────────────────────

@pytest.mark.parametrize("reason,release", [
    ("TREND_BREAK", _release(True, reason="TREND_BREAK", breaks=2)),
    ("MAX_HOLD", _release(True, reason="MAX_HOLD", elapsed=61.0)),
])
def test_h50_own_release_conditions_still_exit(
    tw2_3slot_market_data, monkeypatch, reason, release,
):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True), releases=[_release(False), release])
    # watch 는 계속 "아직 아니다" 만 답하게 둬서 EXIT 주체가 H50 임을 확실히 한다.
    _canned_watch(monkeypatch, [_WATCH_HOLD])
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.h50_hold_active is True

    result = run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))

    assert any(a.startswith(swh.EXIT_SMALL_WHIPSAW_HOLD) for a in result.actions), (reason, result.actions)
    assert state.position is None
    assert state.h50_hold_active is False


# ── G. SL/TP/강제청산 우선 + 그때 H50 상태 정리 ────────────────────────────

def test_stop_loss_during_hold_wins_and_clears_the_h50_hold_state(
    tw2_3slot_market_data, monkeypatch,
):
    """H50 HOLD 중에도 하드스톱이 먼저다. 그리고 그 청산이 H50 HOLD 상태를
    같이 끝내야 한다 — 그러지 않으면 그날 다음 H50 HOLD 가 낡은
    `h50_hold_started_at` 을 물려받아 다음 완성봉에서 곧바로 MAX_HOLD 로
    풀려 버린다(HOLD 가 사실상 무효)."""
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _canned_watch(monkeypatch, [_WATCH_HOLD])
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.h50_hold_active is True and state.whipsaw_watch_active is True

    # 보유 인버스가 하드스톱 아래로 — production 청산 래더가 그대로 발동한다.
    broker.set_quote(config.INVERSE_SYMBOL, 9_700.0)
    outcome = worker.order_executor.execute_exit(
        broker=broker, symbol=config.INVERSE_SYMBOL, quantity=10,
        exit_reason=config.EXIT_TW_STOP_LOSS, entry_price=10_000.0,
        reconcile_retries=1, reconcile_delay_sec=0.0,
    )
    worker._apply_exit_outcome(state, outcome, exit_reason=config.EXIT_TW_STOP_LOSS)

    assert state.h50_hold_active is False
    assert state.h50_hold_started_at is None
    assert state.whipsaw_watch_active is False


def test_a_stale_hold_never_short_circuits_the_next_hold_of_the_day(
    tw2_3slot_market_data, monkeypatch,
):
    """위 정리가 되면, 같은 날 두 번째 HOLD 가 자기 시작시각으로 새로
    시작한다(= `note_hold_start` 가 다시 불린다)."""
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _canned_watch(monkeypatch, [_WATCH_HOLD])
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    first_started_at = state.h50_hold_started_at
    assert first_started_at is not None

    broker.set_quote(config.INVERSE_SYMBOL, 9_700.0)
    outcome = worker.order_executor.execute_exit(
        broker=broker, symbol=config.INVERSE_SYMBOL, quantity=10,
        exit_reason=config.EXIT_TW_STOP_LOSS, entry_price=10_000.0,
        reconcile_retries=1, reconcile_delay_sec=0.0,
    )
    worker._apply_exit_outcome(state, outcome, exit_reason=config.EXIT_TW_STOP_LOSS)

    later = now0 + timedelta(minutes=30)
    _seed_inverse_position(state, broker, later, mode=tw3.MODE_X2LITE_H50_3SLOT)
    state.processed_signal_ids = []
    _prime_3slot_pending(state, Direction.UP_RED, before=later - timedelta(minutes=6))
    run_once(broker=broker, market_data=svc, state=state, now=later)

    assert state.h50_hold_active is True
    assert state.h50_hold_started_at != first_started_at, (
        "두 번째 HOLD 가 낡은 시작시각을 물려받으면 안 된다"
    )


# ── H. HOLD 중 반대방향 신규진입 없음 ──────────────────────────────────────

def test_hold_never_opens_the_opposite_position(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _canned_watch(monkeypatch, [_WATCH_HOLD])
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))
    slots_before = int(state.tw2_3slot_slots_used_today or 0)

    run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert config.LONG_SYMBOL not in broker._positions
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert int(state.tw2_3slot_slots_used_today or 0) == slots_before, (
        "HOLD 는 슬롯을 소비하지 않는다"
    )
