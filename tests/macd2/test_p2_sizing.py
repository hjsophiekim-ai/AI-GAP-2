"""P2 슬롯 배분 사이징 — N1 계열 전용 선택 모드 (2026-09-21).

P2 는 **주문수량 배수만** 바꾼다. 진입/청산/러너/하루 3회 슬롯 한도는 한 줄도
건드리지 않는다. 이 파일이 증명하는 것:

  A. **기본값 BASE** — 모드를 켜지 않으면 배수가 전부 1.0 이라 기존 동작이
     조금도 바뀌지 않는다(회귀 0).
  B. **검증된 N1+C1 구성 전용** — P2 를 켜도 X2-lite / H50 에서는 적용되지
     않고, **C1 이 꺼진 N1 단독**에서도 BASE 사이징을 유지한다. P2 앵커가
     N1+C1 조합에서만 측정됐기 때문이다.
  C. **배분 정확성** — slot1/2 x1.05, 오전 slot3 x0.25, 오후 slot3 x1.00.
     오전/오후는 resolve_slot 의 session 을 그대로 받는다(새 시간기준 없음).
  D. **예산 계약** — MIN/MAX clip, 일일 노출상한 3.0(=3,000만원), 정수주
     반올림, 청산자금 당일 재사용 금지.

연구 근거: research_20260921_p2_budget_cap/ (등급 PROMISING)
"""
from __future__ import annotations

import pytest

from app.trading.macd2 import (
    config,
    order_executor,
    position_sizing as PS,
    state_store,
    time_window_3slot as tw3,
)
from app.trading.macd2.models import RuntimeState

MORNING = tw3.SESSION_MORNING
AFTERNOON = tw3.SESSION_AFTERNOON
SLOT12 = config.P2_SIZING_SLOT12_MULT              # 1.05
MO3 = config.P2_SIZING_MORNING_SLOT3_MULT          # 0.25
AF3 = config.P2_SIZING_AFTERNOON_SLOT3_MULT        # 1.00
CHOP = config.X2LITE_SIZING_CHOP_MULT              # 0.80
POST = config.X2LITE_SIZING_POST_STOP_MULT         # 1.20
LO = config.X2LITE_SIZING_MIN_MULT                 # 0.25
HI = config.X2LITE_SIZING_MAX_MULT                 # 1.50
CAP = config.X2LITE_SIZING_DAILY_EXPOSURE_CAP      # 3.00


class _Knobs:
    """monkeypatch.undo() 를 쓰지 않기 위한 holder — conftest 원장격리를 유지한다."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def mode(self, value: str):
        self.mp.setattr(config, "MACD2_SIZING_MODE", value, raising=False)


def _n1(*, budget: float = 10_000_000.0) -> RuntimeState:
    """검증된 N1+C1 구성 (P2 가 적용되는 유일한 조합)."""
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = budget
    s.time_window_2_filter_enabled = False
    s.time_window_teg_filter_enabled = False
    s.time_window_3slot_filter_enabled = False
    s.time_window_twf_filter_enabled = False
    s.time_window_x2lite_filter_enabled = False
    s.time_window_h50_filter_enabled = False
    s.time_window_n1_filter_enabled = True
    s.c1_peak_protection_enabled = True
    return s


def _n1_only() -> RuntimeState:
    """C1 이 꺼진 N1 단독 — 연구조건 밖이므로 BASE 사이징을 유지해야 한다."""
    s = _n1()
    s.c1_peak_protection_enabled = False
    return s


def _x2lite() -> RuntimeState:
    s = _n1()
    s.time_window_n1_filter_enabled = False
    s.time_window_x2lite_filter_enabled = True
    return s


def _h50() -> RuntimeState:
    s = _n1()
    s.time_window_n1_filter_enabled = False
    s.time_window_h50_filter_enabled = True
    return s


# ════════════════════════════════════════════════════════════════════════════
# A. 기본값 BASE — 회귀 0
# ════════════════════════════════════════════════════════════════════════════
def test_default_mode_is_base():
    assert config.MACD2_SIZING_MODE == config.SIZING_MODE_BASE
    assert PS.sizing_mode() == config.SIZING_MODE_BASE


def test_base_mode_never_applies_p2():
    s = _n1()
    assert PS.p2_active(s) is False
    for slot in (1, 2, 3):
        for sess in (MORNING, AFTERNOON, None):
            assert PS.p2_multiplier(s, slot, sess) == 1.0


@pytest.mark.parametrize("slot,sess", [(1, MORNING), (2, MORNING), (3, MORNING),
                                       (3, AFTERNOON), (2, AFTERNOON)])
def test_base_mode_evaluate_identical_with_or_without_slot_args(slot, sess):
    """BASE 에서는 slot/session 을 주든 말든 결과가 같아야 한다."""
    a = PS.evaluate(_n1(), entry_chop=False)
    b = PS.evaluate(_n1(), entry_chop=False, slot_number=slot, session=sess)
    assert (a.raw, a.clipped, a.applied, a.capped) == (b.raw, b.clipped, b.applied, b.capped)
    assert b.applied == 1.0 and b.p2 == 1.0


def test_base_mode_chop_and_post_stop_unchanged():
    s = _n1()
    assert PS.evaluate(s, entry_chop=True, slot_number=1, session=MORNING).applied == CHOP
    s.x2lite_first_trade_stop_loss = True
    assert PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING).applied == POST


# ════════════════════════════════════════════════════════════════════════════
# B. 다른 전략 불변
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("factory", [_x2lite, _h50])
def test_p2_does_not_touch_other_strategies(monkeypatch, factory):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = factory()
    assert PS.is_active(s) is True          # W1a 는 그대로 살아 있고
    assert PS.p2_active(s) is False         # P2 만 적용되지 않는다
    for slot in (1, 2, 3):
        assert PS.p2_multiplier(s, slot, MORNING) == 1.0
    assert PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING).applied == 1.0


def test_p2_requires_c1_active(monkeypatch):
    """C1 이 꺼진 N1 단독에서는 P2 가 발동하지 않는다."""
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1_only()
    assert PS.is_active(s) is True           # W1a 는 그대로
    assert PS.p2_active(s) is False          # P2 는 발동하지 않는다
    for slot, sess in ((1, MORNING), (2, MORNING), (3, MORNING), (3, AFTERNOON)):
        assert PS.p2_multiplier(s, slot, sess) == 1.0
        assert PS.evaluate(s, entry_chop=False,
                           slot_number=slot, session=sess).applied == 1.0


def test_p2_requires_n1_active(monkeypatch):
    """N1 토글이 꺼지면(=다른 모드) P2 도 꺼진다."""
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    monkeypatch.setattr(config, "N1_ENABLED", False, raising=False)
    assert PS.p2_active(_n1()) is False


def test_p2_active_only_for_n1_plus_c1(monkeypatch):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    assert PS.p2_active(_n1()) is True        # N1+C1 만 True
    assert PS.p2_active(_n1_only()) is False
    assert PS.p2_active(_x2lite()) is False
    assert PS.p2_active(_h50()) is False


def test_p2_inactive_when_no_3slot_mode(monkeypatch):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    s.time_window_n1_filter_enabled = False
    assert PS.p2_active(s) is False
    assert PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING) is PS.NEUTRAL


def test_unknown_mode_falls_back_to_base(monkeypatch):
    _Knobs(monkeypatch).mode("SOMETHING_ELSE")
    assert PS.sizing_mode() == config.SIZING_MODE_BASE
    assert PS.p2_active(_n1()) is False


# ════════════════════════════════════════════════════════════════════════════
# C. 배분 정확성
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("slot,sess,want", [
    (1, MORNING, SLOT12), (1, AFTERNOON, SLOT12),
    (2, MORNING, SLOT12), (2, AFTERNOON, SLOT12),
    (3, MORNING, MO3), (3, AFTERNOON, AF3),
])
def test_p2_multiplier_table(monkeypatch, slot, sess, want):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    assert PS.p2_multiplier(_n1(), slot, sess) == pytest.approx(want)


def test_p2_applied_multipliers(monkeypatch):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    assert PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING).applied == pytest.approx(1.05)
    assert PS.evaluate(s, entry_chop=False, slot_number=2, session=AFTERNOON).applied == pytest.approx(1.05)
    assert PS.evaluate(s, entry_chop=False, slot_number=3, session=MORNING).applied == pytest.approx(0.25)
    assert PS.evaluate(s, entry_chop=False, slot_number=3, session=AFTERNOON).applied == pytest.approx(1.00)


def test_p2_unknown_slot_is_failsafe(monkeypatch):
    """슬롯을 모르면 배수를 건드리지 않는다(기존 동작 유지)."""
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    assert PS.p2_multiplier(s, None, MORNING) == 1.0
    assert PS.evaluate(s, entry_chop=False).applied == 1.0
    assert PS.p2_multiplier(s, 3, None) == 1.0


def test_p2_multiplies_before_clip(monkeypatch):
    """연구와 같은 순서: 규칙배수 x P2 -> clip. clip 뒤에 곱하면 앵커가 깨진다."""
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    # CHOP 오전 slot3: 0.80 x 0.25 = 0.20 -> MIN 0.25 로 clip
    d = PS.evaluate(s, entry_chop=True, slot_number=3, session=MORNING)
    assert d.raw == pytest.approx(CHOP * MO3)
    assert d.clipped == pytest.approx(LO)
    assert d.applied == pytest.approx(LO)
    # POST_STOP 오전 slot3: 1.20 x 0.25 = 0.30 (clip 에 걸리지 않는다)
    s.x2lite_first_trade_stop_loss = True
    d = PS.evaluate(s, entry_chop=False, slot_number=3, session=MORNING)
    assert d.applied == pytest.approx(POST * MO3)
    # POST_STOP slot1: 1.20 x 1.05 = 1.26
    d = PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING)
    assert d.applied == pytest.approx(POST * SLOT12)


def test_p2_respects_individual_max_clip(monkeypatch):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    monkeypatch.setattr(config, "P2_SIZING_SLOT12_MULT", 9.0, raising=False)
    d = PS.evaluate(_n1(), entry_chop=False, slot_number=1, session=MORNING)
    assert d.applied == pytest.approx(HI)


def test_p2_decision_carries_diagnostics(monkeypatch):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    d = PS.evaluate(_n1(), entry_chop=False, slot_number=3, session=MORNING)
    assert d.p2 == pytest.approx(MO3)
    assert d.slot_number == 3 and d.session == MORNING
    assert "P2" in d.reason


# ════════════════════════════════════════════════════════════════════════════
# D. 예산 계약
# ════════════════════════════════════════════════════════════════════════════
def test_daily_capital_is_30m_at_default_budget():
    s = _n1()
    assert PS.daily_capital(s) == pytest.approx(30_000_000.0)
    assert PS.remaining_daily_budget(s) == pytest.approx(30_000_000.0)


def test_remaining_daily_budget_shrinks_and_never_goes_negative(monkeypatch):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    used = 0.0
    for slot, sess in ((1, MORNING), (2, MORNING), (3, AFTERNOON)):
        before = PS.remaining_daily_budget(s)
        d = PS.evaluate(s, entry_chop=False, slot_number=slot, session=sess)
        order = min(s.budget * d.clipped, before)
        used += order
        PS.note_entry(s, d)
        assert PS.remaining_daily_budget(s) >= 0.0
    assert used <= 30_000_000.0 + 1e-6


def test_daily_exposure_cap_still_binds_under_p2(monkeypatch):
    """P2 를 켜도 하루 3,000만원 상한은 그대로다."""
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    s.x2lite_exposure_used_today = 2.10          # 이미 2,100만원 사용
    d = PS.evaluate(s, entry_chop=False, slot_number=3, session=AFTERNOON)
    assert d.clipped == pytest.approx(1.00)
    assert d.applied == pytest.approx(0.90)      # 남은 900만원까지만
    assert d.capped is True
    s.x2lite_exposure_used_today = CAP
    assert PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING).applied == 0.0


def test_exit_does_not_return_same_day_budget(monkeypatch):
    """청산자금은 당일 예산으로 돌아오지 않는다."""
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    d = PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING)
    PS.note_entry(s, d)
    before = PS.remaining_daily_budget(s)
    PS.note_full_exit(s, config.EXIT_TW_STOP_LOSS)
    assert PS.remaining_daily_budget(s) == pytest.approx(before)
    assert PS.exposure_used(s) == pytest.approx(d.applied)


def test_daily_reset_clears_p2_budget_state(monkeypatch):
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    PS.note_entry(s, PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING))
    PS.reset_daily(s)
    assert PS.exposure_used(s) == 0.0
    assert PS.remaining_daily_budget(s) == pytest.approx(30_000_000.0)


def test_integer_quantity_rounding(monkeypatch):
    """실제 ETF 가격 기준 정수주 — 주문금액이 예산을 넘지 않는다."""
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    s = _n1()
    price = 10_145.0
    d = PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING)
    budget = s.budget * d.clipped                       # 10,500,000
    qty = order_executor.compute_order_quantity(
        PS.remaining_daily_budget(s), budget, price, safety_margin_pct=0.0)
    assert qty == int(budget // price)
    assert qty * price <= budget + 1e-6


def test_p2_daily_sequence_never_exceeds_30m(monkeypatch):
    """하루 3회 진입 전 시퀀스에서 누적 원금이 3,000만원을 넘지 않는다."""
    _Knobs(monkeypatch).mode(config.SIZING_MODE_P2)
    for chop in (False, True):
        for first_stop in (False, True):
            s = _n1()
            s.x2lite_first_trade_stop_loss = first_stop
            total = 0.0
            for slot, sess in ((1, MORNING), (2, MORNING), (3, MORNING)):
                rem = PS.remaining_daily_budget(s)
                d = PS.evaluate(s, entry_chop=chop, slot_number=slot, session=sess)
                total += min(s.budget * d.clipped, rem)
                PS.note_entry(s, d)
            assert total <= 30_000_000.0 + 1e-6, (chop, first_stop, total)
