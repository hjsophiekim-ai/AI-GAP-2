"""Unit tests for app.trading.tsla_auto.broker_adapter.

Confirms the unconfirmed-KIS-overseas-order-TR gate: every order/balance
method raises KisOverseasApiConfirmationRequired for both MOCK and REAL —
never silently succeeds (docs §5/§17).
"""
from __future__ import annotations

import pytest

from app.trading.tsla_auto import config
from app.trading.tsla_auto.broker_adapter import (
    MockBrokerAdapter,
    RealBrokerAdapter,
    create_tsla_auto_broker,
)
from app.trading.tsla_auto.kis_overseas_adapter import KisOverseasApiConfirmationRequired


def test_mock_adapter_order_methods_raise_confirmation_required():
    adapter = MockBrokerAdapter()
    with pytest.raises(KisOverseasApiConfirmationRequired):
        adapter.get_fresh_ask1("TSLL")
    with pytest.raises(KisOverseasApiConfirmationRequired):
        adapter.buy_limit("TSLL", 1, 30.0, "cid")
    with pytest.raises(KisOverseasApiConfirmationRequired):
        adapter.sell_market("TSLL", 1, "cid")
    with pytest.raises(KisOverseasApiConfirmationRequired):
        adapter.cancel_order("order-1", "TSLL")


def test_real_adapter_disabled_by_default():
    with pytest.raises(PermissionError):
        RealBrokerAdapter()


def test_real_adapter_still_raises_confirmation_required_even_if_somehow_constructed(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_REAL_ORDER", True)
    adapter = RealBrokerAdapter(runtime_allow_real_order=True)
    with pytest.raises(KisOverseasApiConfirmationRequired):
        adapter.buy_limit("TSLL", 1, 30.0, "cid")


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
