"""H50 (작은 휩쏘 HOLD) 수명주기 — worker.run_once() 경로 테스트 (2026-09-17).

이 파일이 고정하는 것은 **기존 H50 사양 그대로**다. 2026-09-16 사고 이후
H50 HOLD 에 whipsaw-watch 재확인을 붙이는 안을 구현·검증했으나 기존 H50 보다
열위라 **코드에서 제거**했다(70일 복리 +193.51% vs +204.18%, 근거 전량은
`data/validation/macd2/h50_whipsaw_watch_20260917/README.md`). 그래서 여기서는
"연결이 없다"는 것까지 명시적으로 고정한다.

고정하는 계약
-------------
0. H50 HOLD 는 whipsaw-watch 를 **arm 하지 않는다** — `whipsaw_watch_active` 는
   H50 경로에서 절대 True 가 되지 않고, `WHIPSAW_WATCH_DETERIORATION_EXIT` 도
   나오지 않는다. (TW2/TW2 3-SLOT 자신의 watch 는 이 파일이 건드리지 않는다.)
1. H50 OFF 이면 X2-lite + W1a 와 액션·주문이 diff 0
2. HOLD 기본동작 — 반대 플래그를 청산하지 않고 유지, 슬롯도 소비하지 않음
3. 해제는 구조추세 2봉 이탈 / 최대 60분 **두 가지뿐**
4. SL/TP/트레일링/ETP/강제청산이 항상 H50 보다 우선
5. HOLD 중 반대방향 신규진입 없음
6. **stale HOLD state 회귀** — 포지션이 어떤 사유로 닫히든 H50 HOLD 상태가
   같이 정리돼, 그날 다음 HOLD 가 낡은 시작시각을 물려받지 않는다
7. 장중 재시작 라운드트립
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
    _sine_1m_closes,
    _teg as _teg_3slot,
    tw2_3slot_market_data,
)
from app.trading.macd2 import bar_archive, bar_ledger, ledger
from app.trading.macd2.market_data import MarketDataService

KST = config.KST


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


def _forbid_watch_arming(monkeypatch):
    """H50 경로가 whipsaw-watch 를 건드리면 **즉시 테스트 실패**시킨다.
    (TW2/TW2 3-SLOT 자신의 whipsaw-hold 분기는 이 파일의 시나리오에 등장하지
    않는다 — 여기서는 H50 이 반대 플래그를 먼저 가로채기 때문이다.)"""
    def _blow_up(*_a, **_kw):
        raise AssertionError(
            "H50 HOLD 가 whipsaw-watch 를 arm 했다 — 2026-09-17 결정으로 이 연결은 "
            "코드에서 제거됐다(config.py 의 H50 절 참조)."
        )

    monkeypatch.setattr(worker, "_start_whipsaw_watch", _blow_up)


def _watch_never_deteriorates(monkeypatch):
    """혹시라도 watch 가 켜져 있으면 '악화 아님'만 답하게 해서, 청산의 주체가
    H50 인지 watch 인지 헷갈리지 않게 한다."""
    monkeypatch.setattr(
        worker.time_window_filter, "evaluate_whipsaw_watch",
        lambda *a, **kw: WhipsawWatchDecision(
            should_sell=False, should_release=False,
            current_gap=58.0, current_ema_spread=12.0),
    )


# ── 0. 연결 제거 확인 ──────────────────────────────────────────────────────

def test_config_has_no_h50_whipsaw_watch_toggle_any_more():
    """2026-09-17 사용자 결정으로 H50↔whipsaw-watch 연결은 토글째 제거됐다.
    되살리려면 먼저 재검증할 것 — 검증기록은
    `data/validation/macd2/h50_whipsaw_watch_20260917/`."""
    assert not hasattr(config, "H50_WHIPSAW_WATCH_ENABLED")


def test_h50_hold_never_arms_a_whipsaw_watch(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _forbid_watch_arming(monkeypatch)
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in result.actions), result.actions
    assert state.whipsaw_watch_active is False
    assert state.whipsaw_watch_direction is None


def test_a_held_h50_position_never_exits_via_whipsaw_watch_deterioration(
    tw2_3slot_market_data, monkeypatch,
):
    """2026-09-16 의 gap 확대 수열(495 -> 855)을 그대로 먹여도 H50 HOLD 중에는
    `WHIPSAW_WATCH_DETERIORATION_EXIT` 가 나오지 않는다 — watch 가 애초에 arm
    되지 않기 때문이다. 해제는 H50 자신의 두 조건으로만 일어난다."""
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True), releases=[_release(False), _release(False)])
    monkeypatch.setattr(
        worker.time_window_filter, "evaluate_whipsaw_watch",
        lambda *a, **kw: WhipsawWatchDecision(
            should_sell=True, should_release=False,
            current_gap=855.05, current_ema_spread=40.0),
    )
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))

    assert not any(config.WHIPSAW_WATCH_DETERIORATION_EXIT in a for a in result.actions), result.actions
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert state.h50_hold_active is True


# ── 1. H50 OFF parity ─────────────────────────────────────────────────────

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
    """merge 조건: H50 이 꺼져 있으면 플래그/주문/액션이 X2-lite + W1a 와
    완전히 같아야 한다."""
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


# ── 2. HOLD 기본동작 ──────────────────────────────────────────────────────

def test_small_whipsaw_hold_keeps_the_position_without_an_order(
    tw2_3slot_market_data, monkeypatch,
):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _forbid_watch_arming(monkeypatch)
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))
    orders_before = len(broker.orders)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in result.actions), result.actions
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert len(broker.orders) == orders_before, "HOLD 는 주문을 내지 않는다"
    assert state.h50_hold_active is True
    assert state.h50_original_direction == Direction.DOWN_BLUE.value
    assert state.order_block_reason == config.H50_HOLD_BLOCK_REASON


# ── 3. 해제조건은 두 가지뿐 ───────────────────────────────────────────────

@pytest.mark.parametrize("reason,release", [
    ("TREND_BREAK", _release(True, reason="TREND_BREAK", breaks=2)),
    ("MAX_HOLD", _release(True, reason="MAX_HOLD", elapsed=61.0)),
])
def test_h50_release_conditions_exit(tw2_3slot_market_data, monkeypatch, reason, release):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True), releases=[_release(False), release])
    _watch_never_deteriorates(monkeypatch)
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.h50_hold_active is True

    result = run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))

    assert any(a.startswith(swh.EXIT_SMALL_WHIPSAW_HOLD) for a in result.actions), (reason, result.actions)
    assert state.position is None
    assert state.h50_hold_active is False


def test_hold_survives_a_bar_that_meets_neither_release_condition(
    tw2_3slot_market_data, monkeypatch,
):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True), releases=[_release(False)])
    _forbid_watch_arming(monkeypatch)
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    orders_before = len(broker.orders)
    run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))

    assert len(broker.orders) == orders_before
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert state.h50_hold_active is True


# ── 4. SL/TP/강제청산 우선 + stale HOLD state 회귀 ─────────────────────────

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
    _forbid_watch_arming(monkeypatch)
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.h50_hold_active is True

    broker.set_quote(config.INVERSE_SYMBOL, 9_700.0)
    outcome = worker.order_executor.execute_exit(
        broker=broker, symbol=config.INVERSE_SYMBOL, quantity=10,
        exit_reason=config.EXIT_TW_STOP_LOSS, entry_price=10_000.0,
        reconcile_retries=1, reconcile_delay_sec=0.0,
    )
    worker._apply_exit_outcome(state, outcome, exit_reason=config.EXIT_TW_STOP_LOSS)

    assert state.h50_hold_active is False
    assert state.h50_hold_started_at is None
    assert state.h50_original_direction is None
    assert state.h50_trend_break_count == 0
    assert state.h50_last_checked_bar_ts is None


@pytest.mark.parametrize("exit_reason", [
    "TIME_WINDOW_STOP_LOSS",
    "TIME_WINDOW_AFTER_TP1_STOP",
    "TIME_WINDOW_TRAILING_STOP",
    "EARLY_TAKE_PROFIT",
    "FORCED_LIQUIDATION",
])
def test_every_full_exit_reason_clears_the_hold(exit_reason):
    """`_apply_exit_outcome` 은 모든 전량청산 경로가 지나는 단 하나의 함수다 —
    사유와 무관하게 H50 HOLD 상태가 끝나야 한다."""
    state = _h50_state()
    state.h50_hold_active = True
    state.h50_hold_started_at = datetime(2026, 9, 16, 13, 33, tzinfo=KST).isoformat()
    state.h50_original_direction = Direction.UP_RED.value
    state.h50_trend_break_count = 1
    state.h50_last_checked_bar_ts = datetime(2026, 9, 16, 13, 33, tzinfo=KST).isoformat()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10,
                                      avg_price=10_000.0, entry_at=datetime.now(KST))

    broker = _broker()
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")
    broker._positions[config.LONG_SYMBOL].avg_price = 10_000.0
    outcome = worker.order_executor.execute_exit(
        broker=broker, symbol=config.LONG_SYMBOL, quantity=10, exit_reason=exit_reason,
        entry_price=10_000.0, reconcile_retries=1, reconcile_delay_sec=0.0,
    )
    worker._apply_exit_outcome(state, outcome, exit_reason=exit_reason)

    assert state.h50_hold_active is False
    assert state.h50_hold_started_at is None


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
    _forbid_watch_arming(monkeypatch)
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


# ── 5. HOLD 중 반대방향 신규진입 없음 ──────────────────────────────────────

def test_hold_never_opens_the_opposite_position(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _forbid_watch_arming(monkeypatch)
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))
    slots_before = int(state.tw2_3slot_slots_used_today or 0)

    run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert config.LONG_SYMBOL not in broker._positions
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert int(state.tw2_3slot_slots_used_today or 0) == slots_before, (
        "HOLD 는 슬롯을 소비하지 않는다"
    )


# ── 6. 재시작 라운드트립 ──────────────────────────────────────────────────

def test_hold_survives_a_state_roundtrip(tw2_3slot_market_data, monkeypatch):
    """장중 재시작 시나리오 — HOLD 는 자기 시작시각을 그대로 들고 복원돼야
    한다(복원 실패 시 남은 HOLD 시간이 통째로 사라진다)."""
    svc, now0 = tw2_3slot_market_data
    state = _h50_state()
    broker = _broker()
    _seed_inverse_position(state, broker, now0, mode=tw3.MODE_X2LITE_H50_3SLOT)
    _patch_common_3slot(monkeypatch, entry_decision=_approved_3slot(),
                        quality_decision=_quality_3slot(True, 5), teg_decision=_teg_3slot(True))
    _patch_h50(monkeypatch, hold=_hold(True))
    _forbid_watch_arming(monkeypatch)
    _prime_3slot_pending(state, Direction.UP_RED, before=now0 - timedelta(minutes=6))
    run_once(broker=broker, market_data=svc, state=state, now=now0)

    state_store.save_state(state)
    restored = state_store.load_state()

    assert restored.h50_hold_active is True
    assert restored.h50_original_direction == state.h50_original_direction
    assert restored.h50_hold_started_at == state.h50_hold_started_at
    assert restored.h50_trend_break_count == state.h50_trend_break_count
    assert restored.whipsaw_watch_active is False
