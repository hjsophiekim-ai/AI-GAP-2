"""
test_dynamic_exit_watcher.py — tick() 1회 실행 동작 검증(브로커/가격조회는 모킹).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import app.services.hynix_switch_state as state_module
import app.trading.dynamic_exit_watcher as watcher
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL, HYNIX_NAME
from app.models import OrderResult


class _FakeSellBroker:
    def sell(self, symbol, name, quantity, price, order_type="limit"):
        return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="sell", quantity=quantity, price=price, order_type=order_type, order_id="S1", message="ok")


def _setup_state_with_position(tmp_path, monkeypatch, entry_price=100_000.0, entry_minutes_ago=5):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    state = state_module.load_state(mode="mock")
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["position"] = {
        "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": 10, "avg_price": entry_price,
        "entry_price": entry_price,
        "entry_time": (datetime.now() - timedelta(minutes=entry_minutes_ago)).isoformat(),
        "partial_tp1_done": False, "partial_sl1_done": False,
        "highest_price": entry_price, "lowest_price": entry_price,
        "trailing_armed": False, "trailing_peak_price": None, "profit_lock_peak_pct": 0.0,
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
    _setup_state_with_position(tmp_path, monkeypatch, entry_price=100_000.0)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 103_100.0)  # +3.1% -> NORMAL TP 3.0%
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)

    fake_exit_log = tmp_path / "exit_engine_log.csv"
    monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", fake_exit_log)

    import app.trading.broker_factory as broker_factory_module
    broker = _FakeSellBroker()
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: broker)

    decision = watcher.tick(now=datetime.now())

    assert decision["action"] == "SELL_ALL"
    reloaded = state_module.load_state()
    assert reloaded["position"]["symbol"] is None  # 전량 매도되어 포지션 정리됨
    assert fake_exit_log.exists()
