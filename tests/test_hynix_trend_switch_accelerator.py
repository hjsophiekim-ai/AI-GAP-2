from __future__ import annotations

from datetime import datetime, timedelta

from app.models import OrderResult
from app.services.hynix_auto_trade_service import HYNIX_NAME, HYNIX_SYMBOL
import app.services.hynix_switch_engine as switch_engine
from app.services.hynix_switch_state import default_state
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
from app.trading.hynix_switch_position_manager import run_switch_or_entry


class Broker:
    def __init__(self, cash: float = 1_000_000.0):
        self.cash = cash
        self.buy_calls = []
        self.sell_calls = []

    def get_buyable_cash(self):
        return self.cash

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, quantity, price))
        self.cash -= quantity * price
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id=f"B{len(self.buy_calls)}", message="ok",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        self.cash += quantity * price
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type=order_type,
            order_id=f"S{len(self.sell_calls)}", message="ok",
        )


def test_strong_buy_enters_immediately_with_exploratory_size(monkeypatch):
    called = {"pullback": False}
    monkeypatch.setattr(switch_engine, "detect_pullback", lambda df: called.__setitem__("pullback", True))
    state = default_state()
    now = datetime(2026, 7, 14, 10, 0)

    gate = switch_engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_STRONG_BUY", now, {}, None, "mock")
    result = run_switch_or_entry(state, Broker(), "HYNIX_STRONG_BUY", 100_000.0, 5_000.0, now=now)

    assert gate["proceed"] is True
    assert called["pullback"] is False
    assert result["acted"] is True
    assert state["position"]["quantity"] == 2
    assert state["position"]["entry_type"] == "EXPLORATORY"
    assert state["position"]["stop_loss_pct"] == -0.8


def test_second_consecutive_strong_signal_scales_toward_confirmed_size(monkeypatch):
    monkeypatch.setattr(switch_engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "wait"})
    state = default_state()
    broker = Broker(cash=1_000_000.0)
    now = datetime(2026, 7, 14, 10, 0)

    switch_engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_STRONG_BUY", now, {}, None, "mock")
    run_switch_or_entry(state, broker, "HYNIX_STRONG_BUY", 100_000.0, 5_000.0, now=now)

    later = now + timedelta(minutes=3)
    gate = switch_engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_STRONG_BUY", later, {}, None, "mock")
    result = run_switch_or_entry(state, broker, "HYNIX_STRONG_BUY", 100_000.0, 5_000.0, now=later)

    assert gate["proceed"] is True
    assert state["last_trend_switch_plan"]["entry_type"] == "NORMAL"
    assert result["acted"] is True
    assert state["position"]["quantity"] > 2


def test_general_signal_waits_no_more_than_five_minutes(monkeypatch):
    monkeypatch.setattr(switch_engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "wait"})
    state = default_state()
    start = datetime(2026, 7, 14, 10, 0)

    first = switch_engine.evaluate_pullback_gate(state, HYNIX_SYMBOL, "HYNIX_BUY", start, {}, None, "mock")
    after_five = switch_engine.evaluate_pullback_gate(
        state, HYNIX_SYMBOL, "HYNIX_BUY", start + timedelta(minutes=5), {}, None, "mock",
    )

    assert first["proceed"] is False
    assert first["pullback_wait_remaining_seconds"] == 300
    assert after_five["proceed"] is True
    assert after_five["deadline_expired"] is True


def test_two_reversal_confirmations_switch_after_sell_fill():
    state = default_state()
    state["position"] = {
        "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": 2,
        "avg_price": 100_000.0, "entry_price": 100_000.0,
        "entry_time": datetime(2026, 7, 14, 9, 30).isoformat(),
    }
    broker = Broker(cash=800_000.0)
    now = datetime(2026, 7, 14, 10, 0)

    first = switch_engine.evaluate_pullback_gate(state, INVERSE_SYMBOL, "INVERSE_BUY", now, {}, None, "mock")
    second = switch_engine.evaluate_pullback_gate(state, INVERSE_SYMBOL, "INVERSE_BUY", now + timedelta(minutes=1), {}, None, "mock")
    result = run_switch_or_entry(state, broker, "INVERSE_BUY", 100_000.0, 5_000.0, now=now + timedelta(minutes=1))

    assert first["proceed"] is False
    assert second["proceed"] is True
    assert state["last_trend_switch_plan"]["immediate_switch"] is True
    assert broker.sell_calls and broker.sell_calls[0][0] == HYNIX_SYMBOL
    assert broker.buy_calls and broker.buy_calls[0][0] == INVERSE_SYMBOL
    assert result["acted"] is True


def test_unconfirmed_sell_blocks_opposite_buy():
    from tests.test_real_trading_readiness import _FakePositionManager

    state = default_state()
    state["position"] = {
        "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": 2,
        "avg_price": 100_000.0, "entry_price": 100_000.0,
        "entry_time": datetime(2026, 7, 14, 9, 30).isoformat(),
    }
    now = datetime(2026, 7, 14, 10, 0)
    switch_engine.evaluate_pullback_gate(state, INVERSE_SYMBOL, "INVERSE_BUY", now, {}, None, "mock")
    switch_engine.evaluate_pullback_gate(state, INVERSE_SYMBOL, "INVERSE_BUY", now + timedelta(minutes=1), {}, None, "mock")

    broker = Broker()
    pm = _FakePositionManager(remaining_symbol=HYNIX_SYMBOL, remaining_qty=2)
    result = run_switch_or_entry(
        state, broker, "INVERSE_BUY", 100_000.0, 5_000.0,
        now=now + timedelta(minutes=1), position_manager=pm,
    )

    assert result["acted"] is True
    assert broker.sell_calls
    assert broker.buy_calls == []
