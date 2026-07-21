from __future__ import annotations

from datetime import datetime, timedelta

from app.models import OrderResult
from app.data_sources.hynix_long_collector import LONG_NAME, LONG_SYMBOL
import app.services.hynix_switch_engine as switch_engine
from app.services.hynix_switch_state import default_state
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
from app.trading.hynix_switch_position_manager import run_switch_or_entry


class Broker:
    def __init__(self, cash: float = 1_000_000.0):
        self.cash = cash
        self.buy_calls = []
        self.sell_calls = []
        self.positions = []

    def get_buyable_cash(self):
        return self.cash

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, quantity, price))
        self.cash -= quantity * price
        self.positions = [{"symbol": symbol, "name": name, "quantity": quantity, "avg_price": price}]
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id=f"B{len(self.buy_calls)}", message="ok",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        self.cash += quantity * price
        self.positions = []
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type=order_type,
            order_id=f"S{len(self.sell_calls)}", message="ok",
        )

    def get_positions(self):
        return list(self.positions)


def test_strong_buy_enters_immediately_with_exploratory_size(monkeypatch):
    called = {"pullback": False}
    monkeypatch.setattr(switch_engine, "detect_pullback", lambda df: called.__setitem__("pullback", True))
    state = default_state()
    now = datetime(2026, 7, 14, 10, 0)

    gate = switch_engine.evaluate_pullback_gate(state, LONG_SYMBOL, "HYNIX_STRONG_BUY", now, {}, None, "mock")
    result = run_switch_or_entry(state, Broker(), "HYNIX_STRONG_BUY", 100_000.0, 5_000.0, now=now)

    assert gate["proceed"] is True
    assert called["pullback"] is False
    assert result["acted"] is True
    assert state["position"]["quantity"] == 3  # 요구사항6: exploratory 30% (1,000,000원의 30%=300,000원 // 100,000원 = 3주)
    assert state["position"]["entry_type"] == "EXPLORATORY"
    assert state["position"]["stop_loss_pct"] == -0.8


def test_second_consecutive_strong_signal_stays_exploratory(monkeypatch):
    monkeypatch.setattr(switch_engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "wait"})
    state = default_state()
    broker = Broker(cash=1_000_000.0)
    now = datetime(2026, 7, 14, 10, 0)

    switch_engine.evaluate_pullback_gate(state, LONG_SYMBOL, "HYNIX_STRONG_BUY", now, {}, None, "mock")
    run_switch_or_entry(state, broker, "HYNIX_STRONG_BUY", 100_000.0, 5_000.0, now=now)

    later = now + timedelta(minutes=3)
    gate = switch_engine.evaluate_pullback_gate(state, LONG_SYMBOL, "HYNIX_STRONG_BUY", later, {}, None, "mock")
    result = run_switch_or_entry(state, broker, "HYNIX_STRONG_BUY", 100_000.0, 5_000.0, now=later)

    assert gate["proceed"] is True
    assert state["last_trend_switch_plan"]["entry_type"] == "EXPLORATORY"
    assert result["acted"] is False
    assert state["position"]["quantity"] == 3  # 요구사항6: exploratory 30% (1,000,000원의 30%=300,000원 // 100,000원 = 3주)


def test_general_signal_waits_no_more_than_two_minutes(monkeypatch):
    """_PULLBACK_PATIENCE_MINUTES=2(2026-07-15 committed change) — 눌림목 대기는
    최대 2분이며, 그 이후에는 데드라인 만료로 강제 진입한다."""
    monkeypatch.setattr(switch_engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "wait"})
    state = default_state()
    start = datetime(2026, 7, 14, 10, 0)

    first = switch_engine.evaluate_pullback_gate(state, LONG_SYMBOL, "HYNIX_BUY", start, {}, None, "mock")
    after_three = switch_engine.evaluate_pullback_gate(
        state, LONG_SYMBOL, "HYNIX_BUY", start + timedelta(minutes=3), {}, None, "mock",
    )

    assert first["proceed"] is False
    assert first["pullback_wait_remaining_seconds"] == 120
    # 3분 뒤에는 진입이 허용되어야 한다 — 데드라인 만료 경로든(2분 초과), 그 전에
    # confirm_tracker가 같은 신호의 반복 확인으로 즉시 진입을 승인하는 경로든
    # (둘 다 "더 이상 기다리지 않는다"는 핵심 요구사항을 만족하는 정상 경로다).
    assert after_three["proceed"] is True


def test_two_reversal_confirmations_switch_after_sell_fill():
    class _ConfirmedEmptyBroker(Broker):
        """매도 후 브로커가 실제로 빈 포지션을 보고하는(전량매도 확인됨) 경우만 이
        테스트에서 시뮬레이션한다 — 미확인 매도를 검증하는 다른 테스트들은 이
        오버라이드 없이 그대로 AttributeError로 "미확인" 상태를 시뮬레이션한다."""

        def get_positions(self):
            return []

    state = default_state()
    state["position"] = {
        "symbol": LONG_SYMBOL, "name": LONG_NAME, "quantity": 2,
        "avg_price": 100_000.0, "entry_price": 100_000.0,
        "entry_time": datetime(2026, 7, 14, 9, 30).isoformat(),
    }
    broker = _ConfirmedEmptyBroker(cash=800_000.0)
    now = datetime(2026, 7, 14, 10, 0)

    first = switch_engine.evaluate_pullback_gate(state, INVERSE_SYMBOL, "INVERSE_BUY", now, {}, None, "mock")
    second = switch_engine.evaluate_pullback_gate(state, INVERSE_SYMBOL, "INVERSE_BUY", now + timedelta(minutes=1), {}, None, "mock")
    result = run_switch_or_entry(state, broker, "INVERSE_BUY", 100_000.0, 5_000.0, now=now + timedelta(minutes=1))

    assert first["proceed"] is False
    assert second["proceed"] is True
    assert state["last_trend_switch_plan"]["immediate_switch"] is True
    assert broker.sell_calls and broker.sell_calls[0][0] == LONG_SYMBOL
    assert broker.buy_calls and broker.buy_calls[0][0] == INVERSE_SYMBOL
    assert result["acted"] is True


def test_unconfirmed_sell_blocks_opposite_buy():
    from tests.test_real_trading_readiness import _FakePositionManager

    state = default_state()
    state["position"] = {
        "symbol": LONG_SYMBOL, "name": LONG_NAME, "quantity": 2,
        "avg_price": 100_000.0, "entry_price": 100_000.0,
        "entry_time": datetime(2026, 7, 14, 9, 30).isoformat(),
    }
    now = datetime(2026, 7, 14, 10, 0)
    switch_engine.evaluate_pullback_gate(state, INVERSE_SYMBOL, "INVERSE_BUY", now, {}, None, "mock")
    switch_engine.evaluate_pullback_gate(state, INVERSE_SYMBOL, "INVERSE_BUY", now + timedelta(minutes=1), {}, None, "mock")

    broker = Broker()
    broker.positions = [{"symbol": LONG_SYMBOL, "name": LONG_NAME, "quantity": 2, "avg_price": 100_000.0}]
    pm = _FakePositionManager(remaining_symbol=LONG_SYMBOL, remaining_qty=2)
    result = run_switch_or_entry(
        state, broker, "INVERSE_BUY", 100_000.0, 5_000.0,
        now=now + timedelta(minutes=1), position_manager=pm,
    )

    assert result["acted"] is True
    assert broker.sell_calls
    assert broker.buy_calls == []


def test_daily_loss_below_threshold_blocks_new_entry(monkeypatch):
    """요구사항(2026-07-20) — 일 손실이 daily_loss_block_pct(-2.0%) 이하이면
    신규진입이 즉시 차단된다(사용자 실측: -2.19% <= -2.0%)."""
    monkeypatch.setattr(switch_engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "wait"})
    state = default_state()
    state["realized_pnl_today_pct"] = -2.19
    now = datetime(2026, 7, 14, 10, 0)

    gate = switch_engine.evaluate_pullback_gate(state, LONG_SYMBOL, "HYNIX_BUY", now, {}, None, "mock")

    assert gate["proceed"] is False
    assert "일 손실" in gate["message"]
    assert "-2.19" in gate["message"]


def test_daily_loss_block_override_resumes_trading(monkeypatch):
    """요구사항(2026-07-20) — 사용자가 daily_loss_block_override 토글을 켜면
    같은 -2.19% 일손실 상황에서도 신규진입 차단 사유에 더 이상 '일 손실'이
    나타나지 않고(다른 정상 눌림목 대기 흐름으로 진행), 일손실과 무관하게
    거래가 재개된다."""
    monkeypatch.setattr(switch_engine, "detect_pullback", lambda df: {"is_pullback": False, "reason": "wait"})
    state = default_state()
    state["realized_pnl_today_pct"] = -2.19
    state["daily_loss_block_override"] = True
    now = datetime(2026, 7, 14, 10, 0)

    gate = switch_engine.evaluate_pullback_gate(state, LONG_SYMBOL, "HYNIX_BUY", now, {}, None, "mock")

    assert "일 손실" not in (gate["message"] or "")
    # 오버라이드가 켜지면 일손실이 전혀 없을 때와 동일한 정상 눌림목 대기 흐름으로
    # 진행된다(최대 2분 대기 - test_general_signal_waits_no_more_than_two_minutes와 동일 경로).
    assert gate["pullback_wait_remaining_seconds"] == 120
