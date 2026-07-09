"""
test_dynamic_exit_watcher.py — tick() 1회 실행 동작 검증(브로커/가격조회는 모킹).

Broker가 유일한 Source of Truth이므로, 모든 테스트는 "브로커가 실제로 어떤
포지션을 들고 있다고 응답하는지"로 시나리오를 구성한다(state를 직접 조작해
포지션이 있는 것처럼 꾸미는 것만으로는 tick()이 이를 인식하면 안 된다).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import app.services.hynix_switch_state as state_module
import app.trading.dynamic_exit_watcher as watcher
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL, HYNIX_NAME
from app.models import OrderResult, Position


class _FakeSellBroker:
    def __init__(self, positions=None, cash=10_000_000.0):
        self._positions = positions or []
        self._cash = cash
        self.sell_calls = []

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        # 실제 브로커처럼 매도 후 내부 포지션을 갱신해야 이후 get_positions() 재조회가 정확해진다.
        remaining = []
        for p in self._positions:
            if p.symbol == symbol:
                if p.quantity > quantity:
                    p.quantity -= quantity
                    remaining.append(p)
                # quantity <= 매도수량이면 완전히 제거(추가하지 않음)
            else:
                remaining.append(p)
        self._positions = remaining
        return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="sell", quantity=quantity, price=price, order_type=order_type, order_id="S1", message="ok")

    def get_positions(self):
        return self._positions

    def get_buyable_cash(self):
        return self._cash


def _setup_state_with_entry_bookkeeping(tmp_path, monkeypatch, entry_price=100_000.0, entry_minutes_ago=5):
    """entry_price/entry_time 등 '우리쪽 부가 기록'만 state에 미리 넣어둔다(브로커가 모르는 정보)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    state = state_module.load_state(mode="mock")
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["position"] = {
        **state["position"], "entry_price": entry_price,
        "entry_time": (datetime.now() - timedelta(minutes=entry_minutes_ago)).isoformat(),
    }
    state_module.save_state_atomic(state)
    return tmp_path


def test_tick_does_nothing_when_auto_trade_off(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = False
    state_module.save_state_atomic(state)

    result = watcher.tick(now=datetime.now())
    assert result is None


def test_tick_executes_sell_on_take_profit(tmp_path, monkeypatch):
    _setup_state_with_entry_bookkeeping(tmp_path, monkeypatch, entry_price=100_000.0)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 103_100.0)  # +3.1% -> NORMAL TP 3.0%
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)

    fake_exit_log = tmp_path / "exit_engine_log.csv"
    monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", fake_exit_log)

    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10, avg_price=100_000.0, current_price=103_100.0)
    broker = _FakeSellBroker(positions=[hynix_position])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

    decision = watcher.tick(now=datetime.now())

    assert decision["action"] == "SELL_ALL"
    assert len(broker.sell_calls) == 1 and broker.sell_calls[0][0] == HYNIX_SYMBOL
    reloaded = state_module.load_state(mode="mock")
    assert reloaded["position"]["symbol"] is None  # 전량 매도되어 포지션 정리됨(브로커 재조회로 확정)
    assert fake_exit_log.exists()


def test_tick_returns_none_when_broker_has_no_position_despite_stale_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    # state 파일에는 하이닉스 보유로 남아있지만(예: 이전 세션의 낡은 기록), 브로커는 무보유
    state["position"] = {
        **state["position"], "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": 10,
        "avg_price": 100_000.0, "entry_price": 100_000.0, "entry_time": datetime.now().isoformat(),
    }
    state_module.save_state_atomic(state)

    broker = _FakeSellBroker(positions=[])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

    decision = watcher.tick(now=datetime.now())

    assert decision is None
    assert len(broker.sell_calls) == 0
    reloaded = state_module.load_state(mode="mock")
    assert reloaded["position"]["symbol"] is None  # 브로커 기준으로 정정되어야 함
