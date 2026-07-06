"""
Tests for SellManager.
Uses direct Position construction with no external dependencies.
"""
import pytest

from app.models import Position
from app.trading.sell_manager import SellManager


# ---------------------------------------------------------------------------
# Config stub
# ---------------------------------------------------------------------------

class _StubConfig:
    def __init__(self, **trading_overrides):
        defaults = {
            "first_take_profit_rate": 3.0,
            "second_take_profit_rate": 5.0,
            "stop_loss_rate": -1.5,
            "force_sell_time": "13:00",
            "emergency_sell_time": "15:10",
        }
        defaults.update(trading_overrides)
        self.trading = defaults
        self.filters = {}

    def get(self, *keys, default=None):
        return default


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _position(symbol: str, avg_price: float, current_price: float, quantity: int = 2) -> Position:
    return Position(
        symbol=symbol,
        name=f"종목{symbol}",
        quantity=quantity,
        avg_price=avg_price,
        current_price=current_price,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_profit_3pct_half_sell():
    """Position at +3.5% with 2 shares -> action=sell_half."""
    sm = SellManager(cfg=_StubConfig())
    pos = _position("000001", avg_price=10000, current_price=10350, quantity=2)
    results = sm.evaluate_positions([pos])
    assert len(results) == 1
    assert results[0]["action"] == "sell_half"


def test_profit_3pct_single_share():
    """Position at +3.5% with only 1 share -> action=sell_all (cannot split)."""
    sm = SellManager(cfg=_StubConfig())
    pos = _position("000001", avg_price=10000, current_price=10350, quantity=1)
    results = sm.evaluate_positions([pos])
    assert len(results) == 1
    assert results[0]["action"] == "sell_all"


def test_profit_5pct_sell_all():
    """Position at +5.5% -> action=sell_all (second take-profit triggered)."""
    sm = SellManager(cfg=_StubConfig())
    pos = _position("000001", avg_price=10000, current_price=10550, quantity=2)
    results = sm.evaluate_positions([pos])
    assert len(results) == 1
    assert results[0]["action"] == "sell_all"


def test_stop_loss():
    """Position at -1.6% -> action=sell_all, reason contains '손절'."""
    sm = SellManager(cfg=_StubConfig())
    pos = _position("000001", avg_price=10000, current_price=9840, quantity=2)
    results = sm.evaluate_positions([pos])
    assert len(results) == 1
    assert results[0]["action"] == "sell_all"
    assert "손절" in results[0]["reason"]


def test_hold_position():
    """Position at +1.0% -> action=hold (no take-profit or stop-loss triggered)."""
    sm = SellManager(cfg=_StubConfig())
    pos = _position("000001", avg_price=10000, current_price=10100, quantity=2)
    results = sm.evaluate_positions([pos])
    assert len(results) == 1
    assert results[0]["action"] == "hold"


def test_time_exit_1300():
    """At 13:00 all positions should get action=sell_all."""
    sm = SellManager(cfg=_StubConfig())
    positions = [
        _position("000001", avg_price=10000, current_price=10050),
        _position("000002", avg_price=20000, current_price=20100),
    ]
    results = sm.check_time_exits(positions, current_time="13:00")
    assert len(results) == 2
    for r in results:
        assert r["action"] == "sell_all"


def test_emergency_exit_1510():
    """At 15:10 all positions should get action=sell_all."""
    sm = SellManager(cfg=_StubConfig())
    positions = [
        _position("000001", avg_price=10000, current_price=10050),
        _position("000002", avg_price=20000, current_price=20100),
    ]
    results = sm.check_time_exits(positions, current_time="15:10")
    assert len(results) == 2
    for r in results:
        assert r["action"] == "sell_all"


def test_time_exit_1150():
    """At 11:50 all positions should get action=sell_all with '11:50' in reason."""
    sm = SellManager(cfg=_StubConfig(bulk_sell_1150_time="11:50"))
    positions = [
        _position("000001", avg_price=10000, current_price=10050),
        _position("000002", avg_price=20000, current_price=20100),
    ]
    results = sm.check_time_exits(positions, current_time="11:50")
    assert len(results) == 2
    for r in results:
        assert r["action"] == "sell_all"
        assert "11:50" in r["reason"]


def test_before_1150_no_time_exit():
    """At 11:49, 11:50 bulk sell should NOT trigger."""
    sm = SellManager(cfg=_StubConfig(bulk_sell_1150_time="11:50"))
    positions = [_position("000001", avg_price=10000, current_price=10050)]
    results = sm.check_time_exits(positions, current_time="11:49")
    assert len(results) == 0


def test_1300_overrides_1150():
    """At 13:00, reason should reference 13:00 (force_sell_time), not 11:50."""
    sm = SellManager(cfg=_StubConfig(bulk_sell_1150_time="11:50"))
    positions = [_position("000001", avg_price=10000, current_price=10050)]
    results = sm.check_time_exits(positions, current_time="13:00")
    assert len(results) == 1
    assert "13:00" in results[0]["reason"]
    assert "11:50" not in results[0]["reason"]
