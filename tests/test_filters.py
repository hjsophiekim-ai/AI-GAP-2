"""
Tests for StockFilter.
All tests use direct StockData construction and a minimal config stub
so that no API keys or external services are required.
"""
import pytest

from app.models import StockData
from app.strategy.filters import StockFilter


# ---------------------------------------------------------------------------
# Minimal config stub — avoids reading config.yaml during tests
# ---------------------------------------------------------------------------

class _TradingCfg:
    def get(self, key, default=None):
        defaults = {
            "min_trade_value": 3_000_000_000,
            "min_gap_rate": 2.0,
            "max_gap_rate": 25.0,
        }
        return defaults.get(key, default)


class _FiltersCfg:
    def get(self, key, default=None):
        defaults = {
            "exclude_etf": True,
            "exclude_etn": True,
            "exclude_preferred_stock": True,
            "exclude_spac": True,
            "exclude_reit": True,
            "exclude_warning_stock": True,
            "exclude_halt": True,
            "min_price": 1000,
        }
        return defaults.get(key, default)


class _StubConfig:
    trading = _TradingCfg()
    filters = _FiltersCfg()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stock_filter():
    return StockFilter(cfg=_StubConfig())


def _valid_stock(**overrides) -> StockData:
    """Return a StockData that passes all filters by default."""
    defaults = dict(
        symbol="000001",
        name="테스트주식",
        current_price=10000,
        open=10600,
        previous_close=10000,
        gap_rate=6.0,
        trade_value=5_000_000_000,
        is_warning=False,
        is_halt=False,
        is_etf=False,
        is_etn=False,
        is_preferred=False,
        is_spac=False,
        is_reit=False,
    )
    defaults.update(overrides)
    return StockData(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_etf_excluded(stock_filter):
    stock = _valid_stock(symbol="069500", name="KODEX 200")
    passed, excluded = stock_filter.filter_stocks([stock])
    assert len(passed) == 0
    assert len(excluded) == 1
    assert excluded[0]["symbol"] == "069500"


def test_etn_excluded(stock_filter):
    stock = _valid_stock(symbol="580001", name="삼성 ETN")
    passed, excluded = stock_filter.filter_stocks([stock])
    assert len(passed) == 0
    assert len(excluded) == 1


def test_preferred_excluded(stock_filter):
    """Name ending with '우' must be excluded as a preferred stock."""
    stock = _valid_stock(symbol="005935", name="삼성전자우")
    passed, excluded = stock_filter.filter_stocks([stock])
    assert len(passed) == 0
    assert len(excluded) == 1
    assert "우선주" in excluded[0]["reason"]


def test_preferred_not_excluded(stock_filter):
    """'우' embedded but NOT at the end should NOT be excluded as preferred."""
    stock = _valid_stock(symbol="316140", name="우리금융")
    passed, excluded = stock_filter.filter_stocks([stock])
    # Should not be filtered out for the preferred-stock reason
    preferred_reasons = [e for e in excluded if "우선주" in e.get("reason", "")]
    assert len(preferred_reasons) == 0
    # The stock itself should pass (no other filter should block it)
    assert len(passed) == 1


def test_spac_excluded(stock_filter):
    stock = _valid_stock(symbol="310000", name="DB금융스팩10호")
    passed, excluded = stock_filter.filter_stocks([stock])
    assert len(passed) == 0
    assert len(excluded) == 1
    assert "스팩" in excluded[0]["reason"]


def test_reit_excluded(stock_filter):
    stock = _valid_stock(symbol="088980", name="맥쿼리인프라리츠")
    passed, excluded = stock_filter.filter_stocks([stock])
    assert len(passed) == 0
    assert len(excluded) == 1
    assert "리츠" in excluded[0]["reason"]


def test_warning_excluded(stock_filter):
    stock = _valid_stock(symbol="999999", name="경고종목", is_warning=True)
    passed, excluded = stock_filter.filter_stocks([stock])
    assert len(passed) == 0
    assert len(excluded) == 1


def test_low_trade_value_excluded(stock_filter):
    """Trade value below 3B (500M) should be excluded."""
    stock = _valid_stock(symbol="000002", name="소형주", trade_value=500_000_000)
    passed, excluded = stock_filter.filter_stocks([stock])
    assert len(passed) == 0
    assert len(excluded) == 1
    assert "거래대금" in excluded[0]["reason"]


def test_valid_stock_passes(stock_filter):
    stock = _valid_stock()
    passed, excluded = stock_filter.filter_stocks([stock])
    assert len(passed) == 1
    assert len(excluded) == 0
