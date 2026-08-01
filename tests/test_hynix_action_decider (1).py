"""
test_hynix_action_decider.py — decide_hynix_or_inverse_action() 검증.
"""

from __future__ import annotations

from app.models.hynix_action_decider import (
    decide_hynix_or_inverse_action, HYNIX_STRONG_BUY, HYNIX_BUY, HOLD, INVERSE_BUY, INVERSE_STRONG_BUY,
)


def _enhanced(enhanced=50.0, inverse=50.0, micron=50.0, tech=50.0, momentum=50.0, base=50.0, valid=True):
    return {
        "enhanced_score": enhanced, "inverse_pressure_score": inverse,
        "existing_micron_score": micron, "hynix_technical_score": tech, "intraday_momentum_score": momentum,
        "base_prediction_score": base,
        "data_valid": {"base_prediction": valid, "existing_micron": valid, "hynix_technical": valid, "intraday_momentum": valid},
    }


def test_strong_buy_threshold():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=80, inverse=20, micron=60, tech=60))
    assert result["final_action"] == HYNIX_STRONG_BUY


def test_buy_threshold():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=62, inverse=30, micron=55, tech=55))
    assert result["final_action"] == HYNIX_BUY


def test_inverse_strong_buy_threshold():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=25, inverse=75, micron=45, tech=45))
    assert result["final_action"] == INVERSE_STRONG_BUY


def test_inverse_buy_threshold():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=40, inverse=55, micron=45, tech=45))
    assert result["final_action"] == INVERSE_BUY


def test_hold_mid_range():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=50, inverse=40, micron=50, tech=50))
    assert result["final_action"] == HOLD


def test_inverse_buy_not_shadowed_by_hold_band():
    """요구사항(2026-07-16 사용자 리포트: "우세방향은 INVERSE인데 최종판단은 HOLD") —
    enhanced_score=48이면 정규화된 inverse_score=52로 inverse_buy_min(50)을 이미
    넘지만, 예전 코드는 HOLD 밴드(45~59, enhanced_score 기준) 체크가 인버스 임계값
    체크보다 먼저 실행돼 무조건 HOLD를 반환했다 — inverse_buy_min=50 설정값이
    사실상 죽은 코드였다(실질 문턱이 몰래 55로 올라가 있었음)."""
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=48, micron=48, tech=48))
    assert result["final_action"] == INVERSE_BUY
    assert result["inverse_pressure_score"] == 52


def test_raw_inverse_conflict_does_not_override_hynix_polarity():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=70, inverse=60, micron=55, tech=55))
    assert result["final_action"] == HYNIX_BUY
    assert result["inverse_pressure_score"] == 30
    assert any("raw inverse" in reason or "polarity" in reason for reason in result["reasons"])


def test_hynix_momentum_never_adds_to_inverse_score():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=88, inverse=90, micron=50, tech=90, momentum=100))
    assert result["final_action"] == HYNIX_STRONG_BUY
    assert result["inverse_pressure_score"] == 12


def test_conflict_micron_strong_up_tech_strong_down_returns_hold():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=80, inverse=10, micron=85, tech=20))
    assert result["final_action"] == HOLD


def test_insufficient_data_returns_hold():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=90, inverse=5, valid=False))
    assert result["final_action"] == HOLD


def test_score_gap_flag():
    result = decide_hynix_or_inverse_action(_enhanced(enhanced=48, inverse=47, micron=48, tech=48))
    assert result["score_gap_below_forced_trade_threshold"] is True
