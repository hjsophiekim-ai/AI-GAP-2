"""
Tests for FeatureBuilder.
Uses direct StockData construction with no external dependencies.
"""
import pytest

from app.models import StockData
from app.features.feature_builder import FeatureBuilder


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _stock(**kwargs) -> StockData:
    defaults = dict(
        symbol="000001",
        name="테스트주식",
        previous_close=10000,
        open=10600,
        current_price=10700,
        high=10750,
        low=10500,
        volume=500_000,
        trade_value=5_000_000_000,
        gap_rate=0.0,  # will be computed from prev_close/open
    )
    defaults.update(kwargs)
    return StockData(**defaults)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def builder():
    return FeatureBuilder()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_gap_rate_calculated(builder):
    """prev_close=10000, open=10600 -> gap_rate should be ~6.0."""
    stock = _stock(previous_close=10000, open=10600)
    features_list = builder.build_features([stock])
    assert len(features_list) == 1
    gap = features_list[0].gap_rate
    assert abs(gap - 6.0) < 0.01


def test_total_score_in_range(builder):
    """total_rule_score must be in [0, 100] for typical inputs."""
    stock = _stock()
    features_list = builder.build_features([stock])
    assert len(features_list) == 1
    score = features_list[0].total_rule_score
    assert 0 <= score <= 100


def test_risk_penalty_applied(builder):
    """Stock with gap_rate > 15 should have a non-zero risk_penalty."""
    # prev_close=10000, open=11700 -> gap ~17%
    stock = _stock(previous_close=10000, open=11700, current_price=11800, high=11800, low=11600)
    features_list = builder.build_features([stock])
    assert len(features_list) == 1
    assert features_list[0].risk_penalty > 0


def test_no_crash_missing_data(builder):
    """Stock with zero prices (missing data) must not raise; returns valid features."""
    stock = _stock(previous_close=0, open=0, current_price=0, high=0, low=0, volume=0, trade_value=0)
    # build_features catches exceptions internally and skips or returns partial results
    try:
        features_list = builder.build_features([stock])
        # If it succeeds, score must still be in valid range
        for f in features_list:
            assert 0 <= f.total_rule_score <= 100
    except Exception as exc:
        pytest.fail(f"build_features raised unexpectedly: {exc}")
