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


class _StubBrokerWithStockBuyable(_StubBroker):
    """Mirrors KisMockBroker/KisRealBroker's real, symbol-scoped lookup —
    deliberately returns a DIFFERENT value than the account-level
    get_orderable_cash() to prove the adapter prefers the symbol-scoped one."""

    def __init__(self):
        super().__init__()
        self.stock_buyable_calls = []

    def get_stock_buyable_amount(self, symbol: str = "005930", price: int = 0) -> float:
        self.stock_buyable_calls.append((symbol, price))
        return {"0193T0": 6_000_000.0, "0197X0": 4_000_000.0}.get(symbol, 0.0)


class _StubKis:
    def __init__(self):
        self.calls = []

    def get_buyable_cash_raw(self, symbol="005930", price=0, ord_dvsn=None):
        self.calls.append((symbol, price, ord_dvsn))
        return {
            "output": {"nrcvb_buy_qty": "123", "psbl_qty_calc_unpr": "15000"},
            "ord_psbl_cash": 9_208_577.0,
            "nrcvb_buy_amt": 9_253_492.0,
            "nrcvb_buy_qty": 123,
            "psbl_qty": 123,
            "psbl_qty_calc_unpr": 15_000.0,
            "rt_cd": "0",
            "msg_cd": "20310000",
            "msg1": "OK",
        }


class _StubBrokerWithKis(_StubBroker):
    def __init__(self):
        super().__init__()
        self.kis = _StubKis()


def test_get_orderable_cash_uses_symbol_scoped_lookup_when_available():
    """2026-07-27 fix: get_orderable_cash("005930")-style account-level calls
    silently query an unrelated placeholder symbol under the hood — the
    adapter must prefer the real per-symbol KIS lookup instead."""
    stub = _StubBrokerWithStockBuyable()
    adapter = MockBrokerAdapter(broker=stub)

    assert adapter.get_orderable_cash("0193T0") == 6_000_000.0
    assert adapter.get_orderable_cash("0197X0") == 4_000_000.0
    assert stub.stock_buyable_calls == [("0193T0", 0), ("0197X0", 0)]


def test_get_buy_sizing_quote_uses_same_symbol_and_market_ord_dvsn():
    stub = _StubBrokerWithKis()
    adapter = MockBrokerAdapter(broker=stub)

    quote = adapter.get_buy_sizing_quote("0193T0", price=15_000.0, order_type="market")

    assert stub.kis.calls == [("0193T0", 0, "01")]
    assert quote.order_type == "market"
    assert quote.ord_dvsn == "01"
    assert quote.nrcvb_buy_amt == 9_253_492.0
    assert quote.nrcvb_buy_qty == 123
    assert quote.psbl_qty_calc_unpr == 15_000.0
    assert quote.rt_cd == "0"
    assert quote.msg_cd == "20310000"
    assert quote.msg1 == "OK"


def test_get_orderable_cash_falls_back_without_stock_buyable_amount():
    """A broker double lacking get_stock_buyable_amount (e.g. older test
    stubs) must keep working exactly as before."""
    adapter = MockBrokerAdapter(broker=_StubBroker())
    assert adapter.get_orderable_cash("0193T0") == 8_500_000.0


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
