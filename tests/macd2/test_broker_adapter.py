"""Unit tests for app.trading.macd2.broker_adapter — wraps a stub BrokerBase, no real broker."""
from __future__ import annotations

import pytest

from app.models import OrderResult, Position
from app.trading.macd2.broker_adapter import MockBrokerAdapter, RealBrokerAdapter


class _StubBroker:
    mode = "mock"

    def __init__(self):
        self._positions = [Position(symbol="0193T0", name="KODEX", quantity=10, avg_price=15000.0, current_price=15500.0)]
        self.buy_calls = []
        self.sell_calls = []

    def get_balance(self):
        return 9_000_000.0

    def get_orderable_cash(self):
        return 8_500_000.0

    def get_current_price(self, symbol):
        return {"0193T0": 15500.0, "000660": 150000.0}.get(symbol)

    def get_positions(self):
        return list(self._positions)

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, quantity, order_type))
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id="ORD-1", message="OK",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, order_type))
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type=order_type,
            order_id="ORD-2", message="OK",
        )


def test_mock_adapter_wraps_cash_and_quote():
    adapter = MockBrokerAdapter(broker=_StubBroker())
    assert adapter.get_cash() == 9_000_000.0
    assert adapter.get_orderable_cash("0193T0") == 8_500_000.0
    assert adapter.get_quote("0193T0") == 15500.0
    assert adapter.get_quote("9999999") is None


def test_mock_adapter_get_position_lookup_and_reconcile():
    adapter = MockBrokerAdapter(broker=_StubBroker())
    pos = adapter.get_position("0193T0")
    assert pos is not None and pos.quantity == 10
    assert adapter.get_position("0197X0") is None
    assert adapter.reconcile_position("0193T0") == 10
    assert adapter.reconcile_position("0197X0") == 0


def test_mock_adapter_buy_sell_use_market_order_type():
    stub = _StubBroker()
    adapter = MockBrokerAdapter(broker=stub)

    buy_result = adapter.buy_market("0193T0", 5, "cid-1")
    assert buy_result.success is True
    assert buy_result.side == "BUY"
    assert buy_result.executed_qty == 5
    assert stub.buy_calls == [("0193T0", 5, "market")]

    sell_result = adapter.sell_market("0193T0", 5, "cid-2")
    assert sell_result.success is True
    assert stub.sell_calls == [("0193T0", 5, "market")]


def test_wait_for_execution_documents_synchronous_confirmation():
    adapter = MockBrokerAdapter(broker=_StubBroker())
    with pytest.raises(NotImplementedError):
        adapter.wait_for_execution("ORD-1", timeout=5.0)


def test_real_adapter_raises_without_valid_gate(monkeypatch):
    import app.trading.broker_factory as broker_factory

    def _fake_create_broker(**kwargs):
        raise RuntimeError("실전 계좌가 비활성화되어 있습니다.")

    monkeypatch.setattr(broker_factory, "create_broker", _fake_create_broker)
    with pytest.raises(RuntimeError):
        RealBrokerAdapter(confirm_text="WRONG")
