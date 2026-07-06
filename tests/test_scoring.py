"""
Tests for Scorer.
Uses direct StockData construction with no external dependencies.
"""
import pytest

from app.models import StockData
from app.strategy.scoring import Scorer


# ---------------------------------------------------------------------------
# Config stub
# ---------------------------------------------------------------------------

class _StubConfig:
    trading = {}
    filters = {}

    def get(self, *keys, default=None):
        return default


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_stock(**kwargs) -> StockData:
    defaults = dict(
        symbol="000001",
        name="테스트",
        previous_close=10000,
        open=10600,
        current_price=10700,
        high=10750,
        low=10500,
        trade_value=5_000_000_000,
        gap_rate=6.0,
        volume=500_000,
    )
    defaults.update(kwargs)
    return StockData(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def scorer():
    return Scorer(cfg=_StubConfig())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_high_gap_score(scorer):
    """gap_rate=6.0 should give gap_score=20 (5 <= gap < 8 bracket)."""
    stock = _make_stock(gap_rate=6.0)
    result = scorer._compute_score(stock)
    assert result["gap_score"] == 20


def test_excessive_gap_penalty(scorer):
    """gap_rate=18 (>15) should receive a negative penalty."""
    stock = _make_stock(gap_rate=18.0)
    result = scorer._compute_score(stock)
    assert result["penalties"] < 0


def test_high_trade_value_score(scorer):
    """trade_value=100B (1e11) should give trade_value_score=25."""
    stock = _make_stock(trade_value=100_000_000_000)
    result = scorer._compute_score(stock)
    assert result["trade_value_score"] == 25


def test_total_score_range(scorer):
    """total_score should always be within [0, 100] regardless of inputs."""
    test_cases = [
        _make_stock(gap_rate=0.0, trade_value=0, current_price=1000, open=1000, high=1000, low=900),
        _make_stock(gap_rate=25.0, trade_value=1_000_000_000_000),
        _make_stock(gap_rate=6.0, trade_value=50_000_000_000, open=10000, current_price=10200, high=10200, low=9800),
        _make_stock(gap_rate=15.0, open=10000, current_price=8000, high=10000, low=7000),
    ]
    for stock in test_cases:
        result = scorer._compute_score(stock)
        score = result["total_score"]
        assert 0 <= score <= 100, f"total_score={score} out of range for {stock}"


def test_price_strength_positive(scorer):
    """Positive open_to_current_rate (current > open) should give price_strength_score > 0."""
    # open=10000, current=10200 -> +2% from open
    stock = _make_stock(open=10000, current_price=10200, high=10200, low=9900, gap_rate=5.0)
    result = scorer._compute_score(stock)
    assert result["price_strength_score"] > 0
