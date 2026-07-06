"""
Tests for DryRunBroker.
The broker persists state to data/orders/*.json; we patch the path so tests
do not pollute real data directories.
"""
import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.models import Position
from app.trading.dry_run_broker import DryRunBroker


# ---------------------------------------------------------------------------
# Fixture: isolated broker with a temporary data directory
# ---------------------------------------------------------------------------

@pytest.fixture
def broker(tmp_path):
    """Return a DryRunBroker whose state files land in a temp directory."""
    with patch("app.trading.dry_run_broker._DATA_DIR", tmp_path):
        b = DryRunBroker.__new__(DryRunBroker)
        b._initial_balance = 10_000_000.0
        b._balance = 10_000_000.0
        b._positions = {}
        b._bought_today = set()
        b._buy_counter = 0
        b._sell_counter = 0
        b._today = "20260616"
        # Override _state_path to use tmp directory
        b._state_path = lambda: tmp_path / f"{b._today}_dry_portfolio.json"
        yield b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_buy_creates_position(broker):
    result = broker.buy("000001", "테스트주식", quantity=2, price=50_000)
    assert result.success is True
    positions = broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.symbol == "000001"
    assert pos.quantity == 2
    assert pos.avg_price == 50_000


def test_duplicate_buy_rejected(broker):
    broker.buy("000001", "테스트주식", quantity=1, price=50_000)
    result = broker.buy("000001", "테스트주식", quantity=1, price=50_000)
    assert result.success is False
    assert "중복" in result.message


def test_sell_removes_position(broker):
    broker.buy("000001", "테스트주식", quantity=2, price=50_000)
    result = broker.sell("000001", "테스트주식", quantity=2, price=51_000)
    assert result.success is True
    assert len(broker.get_positions()) == 0


def test_sell_nonexistent_fails(broker):
    result = broker.sell("999999", "없는종목", quantity=1, price=10_000)
    assert result.success is False
    assert "보유 종목 없음" in result.message


def test_partial_sell_reduces_quantity(broker):
    broker.buy("000001", "테스트주식", quantity=2, price=50_000)
    result = broker.sell("000001", "테스트주식", quantity=1, price=51_000)
    assert result.success is True
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 1
