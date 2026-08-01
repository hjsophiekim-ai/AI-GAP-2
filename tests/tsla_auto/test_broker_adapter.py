from __future__ import annotations

import pytest

from app.trading.tsla_auto import config
from app.trading.tsla_auto.broker_adapter import MockBrokerAdapter, RealBrokerAdapter, create_tsla_auto_broker
from app.trading.tsla_auto.kis_overseas_adapter import KisOverseasApiConfirmationRequired, OverseasOrderResult, OverseasOrderRow


def test_mock_adapter_routes_buy_limit_to_kis_overseas_mock(monkeypatch):
    from app.trading.tsla_auto import kis_overseas_adapter

    calls = []

    def fake_order(mode, symbol, side, qty, price, *, exchange_code):
        calls.append((mode, symbol, side, qty, price, exchange_code))
        return OverseasOrderResult(True, "MOCKODNO1", symbol, side, qty, rt_cd="0", msg1="accepted")

    monkeypatch.setattr(kis_overseas_adapter, "place_overseas_limit_order", fake_order)

    result = MockBrokerAdapter().buy_limit("TSLL", 1, 7.12, "cid")

    assert result.success is True
    assert result.order_id == "MOCKODNO1"
    assert calls == [("mock", "TSLL", "BUY", 1, 7.12, "NASD")]


def test_mock_adapter_routes_sell_using_fresh_bid_limit(monkeypatch):
    from app.trading.tsla_auto import kis_overseas_adapter

    calls = []

    monkeypatch.setattr(
        kis_overseas_adapter,
        "fetch_overseas_asking_price",
        lambda mode, symbol, *, exchange_code: ({"ok": True, "ask1": 7.20, "bid1": 7.18}, None),
    )

    def fake_order(mode, symbol, side, qty, price, *, exchange_code):
        calls.append((mode, symbol, side, qty, price, exchange_code))
        return OverseasOrderResult(True, "MOCKSELL1", symbol, side, qty, rt_cd="0", msg1="accepted")

    monkeypatch.setattr(kis_overseas_adapter, "place_overseas_limit_order", fake_order)

    result = MockBrokerAdapter().sell_market("TSLL", 1, "cid")

    assert result.success is True
    assert result.order_id == "MOCKSELL1"
    assert calls == [("mock", "TSLL", "SELL", 1, 7.17, "NASD")]


def test_mock_adapter_routes_cancel_to_kis_overseas_mock(monkeypatch):
    from app.trading.tsla_auto import kis_overseas_adapter

    calls = []

    def fake_cancel(mode, order_id, symbol, *, exchange_code):
        calls.append((mode, order_id, symbol, exchange_code))
        return OverseasOrderResult(True, order_id, symbol, "CANCEL", 1, rt_cd="0", msg1="cancelled")

    monkeypatch.setattr(kis_overseas_adapter, "cancel_overseas_order", fake_cancel)

    result = MockBrokerAdapter().cancel_order("MOCKODNO1", "TSLL")

    assert result.success is True
    assert calls == [("mock", "MOCKODNO1", "TSLL", "NASD")]


def test_broker_adapter_get_open_orders_routes_to_kis_overseas(monkeypatch):
    from app.trading.tsla_auto import kis_overseas_adapter

    rows = [OverseasOrderRow("O1", "TSLL", "BUY", 10, 0, 10, 30.0, 0.0)]
    calls = []

    def fake_open_orders(mode, symbol="", *, exchange_code="NASD"):
        calls.append((mode, symbol, exchange_code))
        return rows, None, {"rt_cd": "0"}

    monkeypatch.setattr(kis_overseas_adapter, "fetch_overseas_open_orders", fake_open_orders)

    assert MockBrokerAdapter().get_open_orders() == rows
    assert calls == [("mock", "", "NASD")]


def test_broker_adapter_get_open_orders_fails_closed_on_unsupported(monkeypatch):
    from app.trading.tsla_auto import kis_overseas_adapter

    monkeypatch.setattr(
        kis_overseas_adapter,
        "fetch_overseas_open_orders",
        lambda mode, symbol="", *, exchange_code="NASD": ([], "MOCK_OPEN_ORDERS_UNSUPPORTED_BY_KIS", {}),
    )

    with pytest.raises(KisOverseasApiConfirmationRequired):
        MockBrokerAdapter().get_open_orders()


def test_tslz_exchange_is_officially_resolved():
    assert config.QUOTE_EXCHANGE_BY_SYMBOL[config.INVERSE_SYMBOL] == "AMS"
    assert config.ORDER_EXCHANGE_BY_SYMBOL[config.INVERSE_SYMBOL] == "AMEX"


def test_real_adapter_disabled_by_default():
    with pytest.raises(PermissionError):
        RealBrokerAdapter()


def test_real_adapter_can_only_construct_when_gate_is_enabled(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_REAL_ORDER", True)
    adapter = RealBrokerAdapter(runtime_allow_real_order=True)
    assert adapter.mode == "real"


def test_create_tsla_auto_broker_factory():
    assert isinstance(create_tsla_auto_broker("mock"), MockBrokerAdapter)
    with pytest.raises(ValueError):
        create_tsla_auto_broker("unknown")


def test_mock_adapter_is_market_open_reflects_session_status(monkeypatch):
    from app.trading.tsla_auto import market_session

    adapter = MockBrokerAdapter()
    monkeypatch.setattr(market_session, "classify_session_status", lambda *a, **k: "REGULAR")
    assert adapter.is_market_open() is True
    monkeypatch.setattr(market_session, "classify_session_status", lambda *a, **k: "CLOSED")
    assert adapter.is_market_open() is False


def test_sell_blocks_when_bid_is_unavailable(monkeypatch):
    from app.trading.tsla_auto import kis_overseas_adapter

    monkeypatch.setattr(
        kis_overseas_adapter,
        "fetch_overseas_asking_price",
        lambda mode, symbol, *, exchange_code: ({"ok": False, "ask1": 7.20, "bid1": 0.0}, None),
    )

    with pytest.raises(KisOverseasApiConfirmationRequired):
        MockBrokerAdapter().sell_market("TSLL", 1, "cid")


def test_mock_adapter_uses_current_price_when_mock_ask_endpoint_is_unsupported(monkeypatch):
    from app.trading.tsla_auto import kis_overseas_adapter
    from app.trading.tsla_auto.kis_overseas_adapter import OverseasQuote

    monkeypatch.setattr(
        kis_overseas_adapter,
        "fetch_overseas_asking_price",
        lambda mode, symbol, *, exchange_code: (None, "MOCK_ASKING_PRICE_UNSUPPORTED_BY_KIS"),
    )
    monkeypatch.setattr(
        kis_overseas_adapter,
        "fetch_overseas_current_price",
        lambda mode, symbol, *, exchange_code: (
            OverseasQuote(symbol=symbol, price=7.21, open=7.0, high=7.3, low=7.0, volume=1),
            None,
        ),
    )

    quote = MockBrokerAdapter().get_fresh_ask1("TSLL")

    assert quote["ok"] is True
    assert quote["ask1"] == 7.21
    assert quote["source"] == "mock_current_price_fallback"
