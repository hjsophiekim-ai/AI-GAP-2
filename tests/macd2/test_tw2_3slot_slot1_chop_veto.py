"""TW2 3-SLOT Slot1 CHOP veto (2026-09-04) 회귀테스트.

검증 항목 (사용자 요구사항 그대로):
  1. entry_chop=False 이면 기존 TW2 3-SLOT 과 100% 동일 (veto 경로가 아무것도
     바꾸지 않음). 토글 OFF 이면 evaluate_entry_chop 이 아예 호출되지 않음.
  2. Slot1 + entry_chop=True 일 때만 신규진입 차단.
  3. 차단 시 슬롯 카운트(slots_used_today/morning_count/afternoon_count)가
     증가하지 않음.
  4. 차단 뒤 다음 후보가 다시 Slot1 로 평가됨.
  5. Slot2/Slot3 은 entry_chop=True 여도 차단되지 않음.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, time_window_3slot as tw3
from app.trading.macd2.models import Direction

KST = config.KST


def _now(h: int, m: int) -> datetime:
    return datetime(2026, 9, 4, h, m, tzinfo=KST)


class _Chop:
    """early_take_profit.evaluate_entry_chop 의 반환 계약만 흉내낸 스텁."""

    def __init__(self, is_chop: bool, score: int = 3, insufficient: bool = False):
        self.is_chop = is_chop
        self.score = score
        self.insufficient_data = insufficient
        self.conditions = {"recent_confirmed_crosses": is_chop}


@pytest.fixture
def spy(monkeypatch):
    """evaluate_entry_chop 호출을 기록하는 스파이."""
    calls: list = []

    def make(is_chop: bool, insufficient: bool = False):
        def _fake(bars_3m, flag_direction, decision_at):
            calls.append((flag_direction, decision_at))
            return _Chop(is_chop, insufficient=insufficient)

        from app.trading.macd2 import early_take_profit
        monkeypatch.setattr(early_take_profit, "evaluate_entry_chop", _fake)
        return calls

    return make


# ── 1) entry_chop=False -> 기존과 동일 ──────────────────────────────────────
def test_no_veto_when_not_chop(spy):
    calls = spy(is_chop=False)
    d = tw3.evaluate_slot1_chop_veto(None, Direction.UP_RED, _now(9, 6), slot_number=1)
    assert d.vetoed is False
    assert d.applicable is True
    assert d.is_chop is False
    assert d.reason is None
    assert len(calls) == 1


def test_toggle_off_never_calls_chop_evaluator(spy):
    calls = spy(is_chop=True)
    d = tw3.evaluate_slot1_chop_veto(
        None, Direction.UP_RED, _now(9, 6), slot_number=1, enabled=False)
    assert d.vetoed is False
    assert d.applicable is False
    assert d.reason == "disabled"
    assert calls == [], "OFF 이면 CHOP 판정 자체를 호출하지 않아야 한다"


def test_insufficient_data_does_not_veto(spy):
    spy(is_chop=True, insufficient=True)
    d = tw3.evaluate_slot1_chop_veto(None, Direction.UP_RED, _now(9, 6), slot_number=1)
    assert d.vetoed is False
    assert d.reason == "insufficient_data"


# ── 2) Slot1 + chop 일 때만 차단 / 5) Slot2·Slot3 은 영향 없음 ──────────────
@pytest.mark.parametrize("slot_number,expect_veto", [(1, True), (2, False), (3, False),
                                                     (None, False)])
def test_veto_only_applies_to_slot1(spy, slot_number, expect_veto):
    calls = spy(is_chop=True)
    d = tw3.evaluate_slot1_chop_veto(
        None, Direction.DOWN_BLUE, _now(9, 30), slot_number=slot_number)
    assert d.vetoed is expect_veto
    if expect_veto:
        assert d.reason == config.TW2_3SLOT_REJECT_SLOT1_ENTRY_CHOP
        assert len(calls) == 1
    else:
        assert calls == [], "Slot1 이 아니면 CHOP 판정을 호출조차 하지 않아야 한다"


# ── 3) 차단 시 슬롯 카운트가 증가하지 않음 (worker 계약) ────────────────────
def test_slot_counters_only_increment_on_executed_entry():
    """후보 판정 함수(_resolve_tw2_3slot_candidate_body) 안에서 슬롯 카운터
    증가문이 ``final_state == SignalState.EXECUTED`` 분기 뒤에만 있는지 검증.
    veto 는 decision.approved=False 로 끝나 그 분기에 도달하지 못하므로
    구조적으로 슬롯이 소비되지 않는다.

    (worker.py 에는 증가 지점이 하나 더 있으나 그것은 브로커 포지션 입양
    /reconcile 경로 — 실제 보유 포지션이 발견됐을 때만 도는 곳이라 차단된
    후보와는 무관하다. 아래에서 그 경로가 실제 포지션 조건에 걸려 있는지도
    함께 확인한다.)"""
    import inspect
    from app.trading.macd2 import worker

    body = inspect.getsource(worker._resolve_tw2_3slot_candidate_body)
    marker = "if outcome is not None and outcome.final_state == SignalState.EXECUTED:"
    assert marker in body
    head, tail = body.split(marker, 1)
    for field in ("tw2_3slot_slots_used_today", "tw2_3slot_morning_count",
                  "tw2_3slot_afternoon_count"):
        inc = f"state.{field} = int(state.{field} or 0) + 1"
        assert inc not in head, f"{field} 증가문이 후보 판정 구간에 있다 (슬롯 소비 위험)"
        assert inc in tail, f"{field} 증가문이 EXECUTED 분기에 없다"

    # 나머지 한 곳(입양 경로)은 실제 포지션이 없을 때만 도는 분기 안에 있다.
    src = inspect.getsource(worker)
    inc = "state.tw2_3slot_slots_used_today = int(state.tw2_3slot_slots_used_today or 0) + 1"
    assert src.count(inc) == 2, "슬롯 증가 지점이 예상(2곳)과 다르다 — 재검토 필요"
    adopt = src.split(inc)[0].rsplit("if not state.time_window_position_active", 1)
    assert len(adopt) == 2, "입양 경로 증가문이 position-inactive 가드 안에 있어야 한다"


def test_veto_branch_runs_after_all_existing_gates():
    """veto 분기가 기존 게이트 체인(quality/TEG/slot-cap) *뒤*에 있고,
    final_approved 가 True 인 경우에만 도는지 소스로 확인."""
    import inspect
    from app.trading.macd2 import worker

    body = inspect.getsource(worker._resolve_tw2_3slot_candidate_body)
    i_teg = body.index("config.TW2_3SLOT_REJECT_TEG")
    i_veto = body.index("evaluate_slot1_chop_veto")
    # "base_decision = dataclasses.replace(" 와 구분하려고 들여쓰기까지 포함해 찾는다
    i_decision = body.index("\n    decision = dataclasses.replace(")
    assert i_teg < i_veto < i_decision, "veto 는 기존 게이트 뒤·decision 조립 전이어야 한다"
    guard = body[body.rindex("if final_approved:", 0, i_veto):i_veto]
    assert "if final_approved:" in guard


# ── 4) 차단 뒤 다음 후보가 다시 Slot1 으로 평가됨 ──────────────────────────
def test_next_candidate_is_slot1_again_after_veto():
    """veto 는 slots_used_today 를 건드리지 않으므로 resolve_slot 은 다음
    후보에도 그대로 slot_number=1 을 돌려준다."""
    kwargs = dict(morning_count=0, afternoon_count=0, direction=Direction.UP_RED,
                  is_flat=True, last_afternoon_direction=None)
    first = tw3.resolve_slot(now=_now(9, 6), slots_used_today=0, **kwargs)
    assert first.slot_allowed and first.slot_number == 1
    # 차단 -> 카운터 그대로 0
    again = tw3.resolve_slot(now=_now(9, 30), slots_used_today=0, **kwargs)
    assert again.slot_allowed and again.slot_number == 1
    # 실제 체결이 있어야만 Slot2 로 넘어간다
    after_fill = tw3.resolve_slot(now=_now(9, 45), slots_used_today=1,
                                  **{**kwargs, "morning_count": 1})
    assert after_fill.slot_number == 2


# ── 기존 슬롯/게이트 로직 불변 확인 ────────────────────────────────────────
def test_resolve_slot_behaviour_unchanged():
    """veto 추가가 resolve_slot 의 기존 반환을 바꾸지 않았는지 핵심 케이스."""
    base = dict(direction=Direction.UP_RED, is_flat=True, last_afternoon_direction=None)
    # 오전 1·2번째: 추가 게이트 없음
    for used, mc in ((0, 0), (1, 1)):
        d = tw3.resolve_slot(now=_now(9, 30), slots_used_today=used,
                             morning_count=mc, afternoon_count=0, **base)
        assert d.slot_allowed and not d.requires_quality_gate and not d.requires_teg_gate
    # 오전 3번째: Trend Quality 게이트
    d = tw3.resolve_slot(now=_now(10, 30), slots_used_today=2, morning_count=2,
                         afternoon_count=0, **base)
    assert d.slot_allowed and d.requires_quality_gate and not d.requires_teg_gate
    # 오후: TEG 게이트 필수
    d = tw3.resolve_slot(now=_now(13, 0), slots_used_today=1, morning_count=1,
                         afternoon_count=0, **base)
    assert d.slot_allowed and d.requires_teg_gate and not d.requires_quality_gate
    # 일 3회 캡
    d = tw3.resolve_slot(now=_now(13, 0), slots_used_today=config.TW2_3SLOT_DAILY_CAP,
                         morning_count=3, afternoon_count=0, **base)
    assert not d.slot_allowed and d.reject_reason == config.TW2_3SLOT_REJECT_SLOT_CAP


def test_veto_reason_is_not_whipsaw_tolerant():
    """veto 거절은 휩쏘 관용 보류 대상이 아니어야 한다(기존 반대신호 청산 경로
    그대로 = 백테스트 재현과 일치)."""
    assert (config.TW2_3SLOT_REJECT_SLOT1_ENTRY_CHOP
            not in config.TW_WHIPSAW_REJECT_REASONS)


# ── worker 레벨 통합 (실제 run_once 체인) ──────────────────────────────────
from app.trading.macd2 import worker  # noqa: E402
from app.trading.macd2.models import Direction as _D  # noqa: E402
from tests.macd2.fake_broker import FakeBroker  # noqa: E402
from tests.macd2.test_early_take_profit_worker import _market  # noqa: E402
from tests.macd2.test_tw2_3slot_worker_regression import (  # noqa: E402
    _PRIOR_DAY, _approved, _fresh_3slot_state, _patch_common,
    _prime_3slot_pending, _quality, _teg,
)


def _run_slot1(monkeypatch, *, early_tp_on: bool):
    """이 픽스처의 합성봉은 CHOP으로 판정된다(test_early_take_profit_worker 의
    test_entry_stores_the_chop_verdict_when_the_filter_is_on 주석 참고)."""
    svc, now0 = _market()
    state = _fresh_3slot_state()
    state.early_tp_filter_enabled = early_tp_on
    broker = FakeBroker(cash=10_000_000.0,
                        quotes={config.LONG_SYMBOL: 15_000.0,
                                config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(),
                  quality_decision=_quality(True), teg_decision=_teg(True))
    _prime_3slot_pending(state, _D.UP_RED, before=_PRIOR_DAY)
    worker.run_once(broker=broker, market_data=svc, state=state, now=now0)
    return state


def test_worker_blocks_slot1_chop_entry_and_does_not_consume_the_slot(monkeypatch):
    state = _run_slot1(monkeypatch, early_tp_on=True)
    assert state.position is None, "Slot1 + CHOP 진입은 체결되면 안 된다"
    assert state.tw2_3slot_slots_used_today == 0, "차단 시 슬롯을 소비하면 안 된다"
    assert state.tw2_3slot_morning_count == 0
    assert state.tw2_3slot_afternoon_count == 0
    assert state.last_tw2_3slot_block_reason == config.TW2_3SLOT_REJECT_SLOT1_ENTRY_CHOP


def test_worker_slot1_entry_unchanged_when_early_tp_filter_is_off(monkeypatch):
    """조기익절 필터 OFF 이면 veto 도 동작하지 않아 기존 동작 그대로."""
    state = _run_slot1(monkeypatch, early_tp_on=False)
    assert state.tw2_3slot_slots_used_today == 1, "OFF 이면 기존대로 진입해야 한다"
    assert state.position is not None


def test_worker_slot1_entry_unchanged_when_veto_toggle_is_off(monkeypatch):
    """config 토글만 꺼도 기존 동작 그대로(조기익절은 켜둔 상태)."""
    monkeypatch.setattr(config, "TW2_3SLOT_SLOT1_CHOP_VETO", False)
    state = _run_slot1(monkeypatch, early_tp_on=True)
    assert state.tw2_3slot_slots_used_today == 1
    assert state.position is not None
