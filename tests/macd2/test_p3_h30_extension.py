"""H30 — CHOP + H50 활성 포지션의 B3 max-hold 유예 (2026-09-28).

H30 이 하는 일은 **하나뿐**이다: B3 max-hold(20분) 청산을, 그 시점에 H50 HOLD
가 이미 활성인 CHOP 포지션에 한해 진입 + 30분까지 미룬다. 그 밖의 모든 것은
2026-09-27 P3 스택 그대로다 -- 손절/강제청산/세션종료/H50 자신의 해제규칙은
전부 H30 보다 먼저이고, H50 이 비활성이면 이 파일의 규칙은 한 줄도 타지 않는다.

연구 근거: ``research_20260928f_p3_chop_rescue`` (origin/research/20260928-p3-followups)
  · out/log_Q2.txt   80영업일 복리 523.0481
  · out/log_H30.txt  80영업일 복리 524.5533  (Δ +1.505, h_stats.txt §14)
  · H30 = Q2(resc_frac=0.2) + h50ext=30.0    (scripts/b3run.py VAR)

잠그는 축 (사용자 요구 §12)
  D. CHOP + H50 **비활성** -> 기존 B3/Y3 와 결정이 같다
  E. CHOP + H50 활성 + 20분 -> 연장 시작
  F. 연장 중 H50 해제 -> 그 tick 에서 기존 exit 경로로 떨어진다
  G. 연장 중 +1% -> Q2 비중 부분익절 + 승격
  H. 30분 + Y3 통과 -> 승격
  I. 30분 + Y3 실패 -> 전량청산 (P3_H30_MAXHOLD_EXIT)
  J. 같은 tick 재호출에도 부분매도 1회
  K. 연장 중 재시작 복원
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import (
    chop_regime,
    config,
    p3_stack,
    small_whipsaw_hold,
    state_store,
    strategy_mode,
)
from app.trading.macd2.models import Direction
from app.trading.macd2.worker import run_once
from tests.macd2.test_early_take_profit_worker import _broker, _market, _seed_completed_bar
from tests.macd2.test_p3_worker import _isolate_shadow_ledger, _p3_state
from tests.macd2.test_tw2_3slot_worker_regression import _patch_common

KST = config.KST

MAX_HOLD = float(config.P3_B3_MAX_HOLD_MIN)          # 20.0
DEADLINE = float(config.P3_H30_EXT_MAX_HOLD_MIN)     # 30.0
ENTRY = datetime(2026, 9, 28, 9, 9, tzinfo=KST)


def _ev(elapsed_min: float, net: float, *, h50=False, h30=False,
        allow_max_hold=True, **kw) -> p3_stack.B3Decision:
    """순수 판정 한 번. 인자를 넘기지 않으면 2026-09-27 동작 그대로다."""
    return p3_stack.evaluate(
        net_return_pct=net, entry_at=ENTRY,
        now=ENTRY + timedelta(minutes=elapsed_min),
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        allow_max_hold=allow_max_hold, h50_active=h50, h30_active=h30, **kw)


# ── D. H50 비활성이면 기존 B3/Y3 와 완전히 같다 ──────────────────────────
#: (경과분, net) -> (action, exit_reason). 2026-09-27 계약을 그대로 옮긴 표다.
_LEGACY_TABLE = [
    (4.0, 1.0, p3_stack.ACTION_PARTIAL_PROMOTE, config.EXIT_P3_PARTIAL),
    (6.0, 1.2, p3_stack.ACTION_PARTIAL_PROMOTE, config.EXIT_P3_PARTIAL),
    (9.0, 1.0, p3_stack.ACTION_EXIT, config.EXIT_B3_TP),
    (25.0, 1.5, p3_stack.ACTION_EXIT, config.EXIT_B3_TP),
    (3.0, -1.0, p3_stack.ACTION_EXIT, config.EXIT_B3_SL),
    (21.0, -1.3, p3_stack.ACTION_EXIT, config.EXIT_B3_SL),
    (10.0, 0.4, p3_stack.ACTION_HOLD, None),
    (19.9, -0.9, p3_stack.ACTION_HOLD, None),
    (20.0, -0.27, p3_stack.ACTION_EXIT, config.EXIT_B3_MAXHOLD),
    (35.0, 0.3, p3_stack.ACTION_EXIT, config.EXIT_B3_MAXHOLD),
]


@pytest.mark.parametrize("elapsed, net, action, reason", _LEGACY_TABLE)
def test_h50_inactive_reproduces_the_pre_h30_contract(elapsed, net, action, reason):
    """H50 이 꺼져 있으면 H30 은 존재하지 않는 것과 같아야 한다."""
    decision = _ev(elapsed, net, h50=False, h30=False)
    assert decision.action == action
    assert decision.exit_reason == reason
    assert decision.exit_reason != config.EXIT_P3_H30_MAXHOLD


@pytest.mark.parametrize("elapsed, net, action, reason", _LEGACY_TABLE)
def test_default_arguments_are_identical_to_h50_inactive(elapsed, net, action, reason):
    """인자를 아예 넘기지 않은 호출(= 기존 호출부)과 결과가 같아야 한다."""
    legacy = p3_stack.evaluate(
        net_return_pct=net, entry_at=ENTRY,
        now=ENTRY + timedelta(minutes=elapsed), direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False)
    assert (legacy.action, legacy.exit_reason) == (action, reason)


# ── E. 20분 + H50 활성 -> 연장 ───────────────────────────────────────────
def test_max_hold_with_h50_active_extends_instead_of_exiting():
    decision = _ev(MAX_HOLD, -0.2665, h50=True)
    assert decision.action == p3_stack.ACTION_EXTEND
    assert decision.reason == "P3_H30_EXTEND"
    assert decision.exit_reason is None, "연장은 주문을 내지 않는다"
    assert decision.sell_fraction == 0.0


def test_extension_holds_through_the_whole_window():
    for elapsed in (20.0, 22.5, 25.0, 29.9):
        assert _ev(elapsed, -0.3, h50=True, h30=True).action == p3_stack.ACTION_EXTEND


def test_thirty_minutes_is_a_hard_deadline_even_while_h50_is_still_active():
    """'30분까지 무조건 보유' 가 아니다 -- 30분에 반드시 재판정을 받는다."""
    decision = _ev(DEADLINE, -0.3, h50=True, h30=True)
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_P3_H30_MAXHOLD


def test_extension_never_starts_if_h50_was_not_already_active_at_max_hold():
    assert _ev(MAX_HOLD, -0.3, h50=False).action == p3_stack.ACTION_EXIT


# ── 우선순위: H30 이 늦추는 것은 max-hold 하나뿐 ─────────────────────────
def test_stop_loss_still_wins_during_the_extension():
    decision = _ev(24.0, -1.0, h50=True, h30=True)
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_SL


def test_take_profit_path_is_evaluated_before_max_hold_during_the_extension():
    """+1% 는 max-hold 판정보다 먼저다 -- 연장 중에도 순서가 같다."""
    decision = _ev(24.0, 1.0, h50=True, h30=True)
    assert decision.action == p3_stack.ACTION_PARTIAL_PROMOTE


# ── G. 연장 중 +1% -> Q2 비중 부분익절 + 승격 ────────────────────────────
def test_one_percent_during_the_extension_takes_the_q2_slice_and_promotes():
    decision = _ev(24.0, 1.0, h50=True, h30=True, allow_max_hold=False)
    assert decision.action == p3_stack.ACTION_PARTIAL_PROMOTE
    assert decision.exit_reason == config.EXIT_P3_H30_PARTIAL
    assert decision.sell_fraction == pytest.approx(
        float(config.X2LITE_MORNING_TP1_SELL_RATIO))
    assert decision.sell_fraction == pytest.approx(0.20)
    assert decision.promote is True and decision.rescue is True
    assert decision.reason == "P3_H30_RESCUE"


def test_one_percent_after_six_minutes_without_an_extension_is_still_a_full_tp():
    """연장이 아니면 6분 초과 +1% 는 예전처럼 전량 익절이다."""
    decision = _ev(12.0, 1.0, h30=False, allow_max_hold=False)
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_TP
    assert decision.sell_fraction == pytest.approx(1.0)


def test_an_already_rescued_position_does_not_get_a_second_partial():
    """P3 rescue 를 이미 쓴 포지션은 연장 중 +1% 에서 부분매도를 또 하지 않는다."""
    decision = p3_stack.evaluate(
        net_return_pct=1.0, entry_at=ENTRY, now=ENTRY + timedelta(minutes=24),
        direction=Direction.DOWN_BLUE, already_rescued=True, already_promoted=False,
        allow_max_hold=False, h50_active=True, h30_active=True)
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_TP


# ── H / I. 30분 재판정 ───────────────────────────────────────────────────
def test_deadline_promotes_when_net_is_positive_and_y3_passes(monkeypatch):
    monkeypatch.setattr(p3_stack, "macd_gap_expanding",
                        lambda bars, now, direction: (True, "gap=+1"))
    decision = _ev(DEADLINE, 0.35, h50=True, h30=True,
                   bars_3m=object(), etf_prev_price=100.0, etf_current_price=100.5)
    assert decision.action == p3_stack.ACTION_PROMOTE
    assert decision.promote is True
    assert decision.exit_reason is None
    assert decision.reason == "Y3_PROMOTE"


def test_deadline_exits_in_full_when_net_is_not_positive():
    decision = _ev(DEADLINE, -0.62, h50=True, h30=True)
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_P3_H30_MAXHOLD
    assert decision.reason == "Y3_FAIL_NET"
    assert decision.sell_fraction == pytest.approx(1.0)


def test_deadline_exits_in_full_when_y3_conditions_fail(monkeypatch):
    monkeypatch.setattr(p3_stack, "macd_gap_expanding",
                        lambda bars, now, direction: (False, "gap=-1"))
    decision = _ev(DEADLINE, 0.35, h50=True, h30=True,
                   bars_3m=object(), etf_prev_price=100.0, etf_current_price=100.5)
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_P3_H30_MAXHOLD
    assert decision.reason == "Y3_FAIL_COND"


def test_a_position_that_never_extended_keeps_the_plain_maxhold_reason():
    """연장을 거치지 않았으면 청산사유는 예전 그대로 B3_MAXHOLD 다."""
    decision = _ev(MAX_HOLD, -0.5, h50=False, h30=False)
    assert decision.exit_reason == config.EXIT_B3_MAXHOLD


# ── F. 연장 중 H50 해제 -> 기존 exit 경로 ────────────────────────────────
def test_releasing_h50_mid_extension_falls_back_to_the_existing_exit_path():
    decision = _ev(25.0, -0.4, h50=False, h30=True)
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.reason == "Y3_FAIL_NET"
    # 연장을 거친 포지션이므로 사유는 H30 이지만, 판정 자체는 기존 Y3 그대로다.
    assert decision.exit_reason == config.EXIT_P3_H30_MAXHOLD


def test_releasing_h50_mid_extension_can_still_promote(monkeypatch):
    """해제됐어도 net>0 ∧ Y3 면 승격이다 -- Y3 규칙을 H30 이 바꾸지 않는다."""
    monkeypatch.setattr(p3_stack, "macd_gap_expanding",
                        lambda bars, now, direction: (True, "gap=+1"))
    decision = _ev(25.0, 0.4, h50=False, h30=True,
                   bars_3m=object(), etf_prev_price=100.0, etf_current_price=101.0)
    assert decision.action == p3_stack.ACTION_PROMOTE


# ── 상태 헬퍼 / 모드 표시 ────────────────────────────────────────────────
def _fresh_state():
    state = state_store.default_state()
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    return state


def test_h30_start_is_recorded_only_once():
    state = _fresh_state()
    now = ENTRY + timedelta(minutes=MAX_HOLD)
    assert p3_stack.note_h30_start(state, now, entry_at=ENTRY) is True
    assert state.p3_h30_active is True
    assert state.p3_h30_started_at == now.isoformat()
    assert state.p3_h30_deadline_at == (ENTRY + timedelta(minutes=DEADLINE)).isoformat()
    # 두 번째 tick 은 False -- P3_H30_START 로그가 중복되지 않는다.
    assert p3_stack.note_h30_start(state, now + timedelta(minutes=1),
                                   entry_at=ENTRY) is False
    assert state.p3_h30_started_at == now.isoformat(), "시작시각이 덮어써지면 안 된다"


def test_deadline_is_always_recomputed_from_entry_time():
    """저장값이 오염돼도 판정은 진입시각에서 다시 계산된다(재시작 안전)."""
    state = _fresh_state()
    p3_stack.note_h30_start(state, ENTRY + timedelta(minutes=20), entry_at=ENTRY)
    state.p3_h30_deadline_at = "2099-01-01T00:00:00+09:00"      # 손상된 사본
    assert p3_stack.h30_deadline(ENTRY) == ENTRY + timedelta(minutes=DEADLINE)


def test_position_mode_reports_h30_while_extending():
    state = _fresh_state()
    assert p3_stack.position_mode(state) == p3_stack.MODE_B3
    p3_stack.note_h30_start(state, ENTRY + timedelta(minutes=20), entry_at=ENTRY)
    assert p3_stack.position_mode(state) == p3_stack.MODE_H30
    # 승격되면 더 이상 H30 이 아니라 runner 다.
    p3_stack.note_y3_promoted(state, ENTRY + timedelta(minutes=30))
    assert p3_stack.position_mode(state) == p3_stack.MODE_Y3_RUNNER


def test_clearing_a_position_clears_the_extension():
    """연장 플래그가 다음 포지션으로 새지 않는다."""
    state = _fresh_state()
    p3_stack.note_h30_start(state, ENTRY + timedelta(minutes=20), entry_at=ENTRY)
    p3_stack.clear_position(state)
    assert state.p3_h30_active is False
    assert state.p3_h30_started_at is None and state.p3_h30_deadline_at is None
    # 신규 진입 각인도 같은 보장을 한다.
    p3_stack.note_h30_start(state, ENTRY + timedelta(minutes=20), entry_at=ENTRY)
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    assert state.p3_h30_active is False


# ── K. 재시작 복원 ───────────────────────────────────────────────────────
def test_extension_survives_a_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")
    state = _fresh_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    p3_stack.note_h30_start(state, ENTRY + timedelta(minutes=20), entry_at=ENTRY)
    state_store.save_state(state)

    restored = state_store.load_state()
    assert restored.p3_h30_active is True
    assert restored.p3_h30_started_at == state.p3_h30_started_at
    assert restored.p3_h30_deadline_at == state.p3_h30_deadline_at
    assert p3_stack.position_mode(restored) == p3_stack.MODE_H30
    # 복원 뒤에도 같은 deadline 이 나온다.
    assert p3_stack.h30_deadline(ENTRY).isoformat() == restored.p3_h30_deadline_at
    # 부분매도 각인도 함께 살아남아 중복주문이 나지 않는다.
    assert restored.p3_first_tp_at == state.p3_first_tp_at


# ── worker 경로 ──────────────────────────────────────────────────────────
def _hold_h50(state, *, now) -> None:
    """이 포지션 소유의 H50 HOLD 를 활성으로 만든다.

    ``h50_last_checked_bar_ts`` 를 미래로 두어 해제판정을 이 tick 에서 건너뛴다
    -- H50 자신의 규칙은 건드리지 않고 '지금 HOLD 중' 상태만 고정한다.
    """
    state.h50_hold_active = True
    state.h50_original_direction = Direction.DOWN_BLUE.value
    state.h50_hold_started_at = (state.position.entry_at + timedelta(minutes=1)).isoformat()
    state.h50_owner_epoch = int(state.position_epoch or 0)
    state.h50_trend_break_count = 0
    state.h50_last_checked_bar_ts = (now + timedelta(hours=1)).isoformat()


def test_worker_starts_the_extension_at_max_hold_and_places_no_order(monkeypatch, tmp_path):
    """E: 20분 + H50 활성이면 청산 대신 연장 -- 주문이 한 건도 나가지 않는다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_980.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=9_980.0,
                      entry_minutes_ago=MAX_HOLD, qty=10)
    _hold_h50(state, now=now0)
    broker = _broker(9_980.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is not None, "H50 활성 CHOP 포지션은 20분에 잘리지 않는다"
    assert state.position.quantity == 10
    assert state.p3_h30_active is True
    assert p3_stack.position_mode(state) == p3_stack.MODE_H30
    assert [o for o in broker.orders if o.side == "SELL"] == []


def test_worker_cuts_at_max_hold_when_h50_is_inactive(monkeypatch, tmp_path):
    """D: 같은 조건에서 H50 만 꺼두면 예전처럼 20분에 잘린다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_980.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=9_980.0,
                      entry_minutes_ago=MAX_HOLD, qty=10)
    assert small_whipsaw_hold.is_holding(state) is False
    broker = _broker(9_980.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is None, "H50 이 꺼져 있으면 H30 은 관여하지 않는다"
    assert state.p3_h30_active is False


def test_worker_exits_at_the_deadline_when_y3_fails(monkeypatch, tmp_path):
    """I: 30분에 net<=0 이면 연장이 끝나고 전량청산된다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_980.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=9_980.0,
                      entry_minutes_ago=DEADLINE, qty=10)
    _hold_h50(state, now=now0)
    state.p3_h30_active = True
    state.p3_h30_started_at = (now0 - timedelta(minutes=10)).isoformat()
    broker = _broker(9_980.0)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is None
    assert any(a.startswith(config.EXIT_P3_H30_MAXHOLD) for a in result.actions), result.actions


def test_worker_releases_the_extension_when_h50_drops(monkeypatch, tmp_path):
    """F: 연장 중 H50 이 해제되면 그 tick 에서 기존 exit 경로로 끝난다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_980.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=9_980.0,
                      entry_minutes_ago=25.0, qty=10)
    state.p3_h30_active = True                       # 이미 연장 중
    state.p3_h30_started_at = (now0 - timedelta(minutes=5)).isoformat()
    assert small_whipsaw_hold.is_holding(state) is False   # H50 은 해제된 상태
    broker = _broker(9_980.0)
    _patch_common(monkeypatch)

    result = run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is None, "H50 이 풀리면 유예가 즉시 끝난다"
    assert any(a.startswith(config.EXIT_P3_H30_MAXHOLD) for a in result.actions), result.actions


def test_worker_partial_sells_once_during_the_extension(monkeypatch, tmp_path):
    """G + J: 연장 중 +1% 는 20% 만 팔고, 같은 조건 tick 을 또 돌려도 한 번뿐이다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_120.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=10_120.0,
                      entry_minutes_ago=24.0, qty=10)
    _hold_h50(state, now=now0)
    state.p3_h30_active = True
    state.p3_h30_started_at = (now0 - timedelta(minutes=4)).isoformat()
    broker = _broker(10_120.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    sells = [o for o in broker.orders if o.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].requested_qty == 2, "Q2 비중 = 10주 x 20%"
    assert state.position is not None and state.position.quantity == 8
    assert state.p3_tp_rescued is True
    assert p3_stack.position_mode(state) == p3_stack.MODE_P3_RUNNER
    assert p3_stack.governs_position(state) is False

    # 같은 조건으로 한 번 더 -- 부분매도가 다시 나가면 안 된다.
    later = now0 + timedelta(seconds=5)
    _seed_completed_bar(state, now=later, price=10_120.0)
    run_once(broker=broker, state=state, market_data=svc, now=later)

    assert len([o for o in broker.orders if o.side == "SELL"]) == 1
    assert state.position is not None and state.position.quantity == 8


def test_trend_entry_never_extends_even_with_h50_active(monkeypatch, tmp_path):
    """C: TREND 진입은 P3 스택 자체를 통과하지 않는다 -- H30 도 마찬가지다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_980.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_TREND, bar_close=9_980.0,
                      entry_minutes_ago=MAX_HOLD, qty=10)
    _hold_h50(state, now=now0)
    broker = _broker(9_980.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.p3_h30_active is False, "TREND 진입에 H30 이 붙으면 안 된다"


def test_n1_mode_never_extends(monkeypatch, tmp_path):
    """A/B: P3 OFF(N1 모드)면 H30 상태가 만들어지지 않는다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_980.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_N1,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=9_980.0,
                      entry_minutes_ago=MAX_HOLD, qty=10)
    _hold_h50(state, now=now0)
    broker = _broker(9_980.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.p3_h30_active is False


# ── UI: 토글이 아니라 읽기 전용 상태다 (사용자 요구 §11) ─────────────────
def test_h30_rides_on_the_p3_mode_and_has_no_flag_of_its_own():
    """H30 은 B3/Y3 와 같은 스위치(p3_enabled) 하나에 함께 실린다.

    strategy_mode.derive() 는 **건드리지 않았다**(사용자 요구 §8 diff 0).
    그래서 H30 전용 키가 없어야 하고, P3 를 고르는 것만으로 동작해야 한다.
    """
    p3 = strategy_mode.derive(strategy_mode.MODE_P3)
    n1 = strategy_mode.derive(strategy_mode.MODE_N1)
    assert "H30" not in p3, "H30 은 별도 플래그를 갖지 않는다"
    assert all(p3[k] is True and n1[k] is False for k in ("B3", "Y3", "P3_RESCUE"))

    # P3 를 고른 state 에서만 P3 스택이 살아 있다 = H30 도 그때만 산다.
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    state.time_window_active_mode = "N1_3SLOT"
    assert p3_stack.is_active(state) is True
    strategy_mode.apply(state, strategy_mode.MODE_N1)
    assert p3_stack.is_active(state) is False


def test_no_user_facing_h30_toggle_exists_on_the_state():
    """H30 전용 사용자 토글 필드가 생기면 안 된다 -- 상태 필드만 있어야 한다."""
    state = state_store.default_state()
    assert not hasattr(state, "h30_enabled")
    assert not hasattr(state, "p3_h30_enabled")
    # 있는 것은 '지금 연장 중인가' 하나뿐이다.
    assert hasattr(state, "p3_h30_active")


def test_applying_p3_mode_does_not_start_an_extension():
    """모드를 고르는 것만으로 연장이 켜지지 않는다 -- 20분 + H50 이 조건이다."""
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    assert bool(getattr(state, "p3_h30_active", False)) is False
