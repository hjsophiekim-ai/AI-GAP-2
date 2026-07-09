"""
test_hynix_inverse_pressure_score.py — calculate_inverse_pressure_score() 검증.
"""

from __future__ import annotations

from app.models.hynix_inverse_pressure_score import (
    calculate_inverse_pressure_score, INVERSE_STRONG_BUY, INVERSE_BUY, HOLD, HYNIX_BUY_FAVORED,
)


def _tech(detail: dict) -> dict:
    return {"hynix_technical_score": 50.0, "reason_top5": [], "warnings": [], "detail": detail}


def _momentum(detail: dict | None = None) -> dict:
    return {"intraday_momentum_score": 50.0, "reason_top5": [], "warnings": [], "detail": detail or {}}


def _micron(score: float) -> dict:
    return {"existing_micron_score": score, "warnings": []}


def test_ma200_breach_increases_inverse_pressure_score():
    below = calculate_inverse_pressure_score(_tech({"ma200_position_pct": -5.0}), _momentum(), _micron(50.0))
    above = calculate_inverse_pressure_score(_tech({"ma200_position_pct": 5.0}), _momentum(), _micron(50.0))
    assert below["inverse_pressure_score"] > above["inverse_pressure_score"]


def test_micron_weak_increases_inverse_pressure_score():
    weak = calculate_inverse_pressure_score(_tech({}), _momentum(), _micron(10.0))
    strong = calculate_inverse_pressure_score(_tech({}), _momentum(), _micron(90.0))
    assert weak["inverse_pressure_score"] > strong["inverse_pressure_score"]


def test_tier_thresholds():
    assert calculate_inverse_pressure_score(_tech({}), _momentum(), _micron(50.0))["inverse_pressure_score"] < 30 \
        or calculate_inverse_pressure_score(_tech({}), _momentum(), _micron(50.0))["inverse_pressure_tier"] in (HOLD, HYNIX_BUY_FAVORED)


def test_no_investor_flow_excludes_that_component():
    result_without = calculate_inverse_pressure_score(_tech({}), _momentum(), _micron(50.0), investor_flow=None)
    assert not any("수급" in r for r in result_without.get("reason_top5", []))
