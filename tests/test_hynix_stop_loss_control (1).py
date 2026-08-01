"""
test_hynix_stop_loss_control.py — 손절 실행 방식(AUTO/ALERT_ONLY/BATCH_MANUAL) 검증.

요구된 6개 케이스:
  1) real 주문로그는 있으나 잔고 증가가 확인되지 않으면 체결 미확인(verify_order_confirmed=False)
  2) mock 매수가 real 보유 포지션으로 표시되지 않음(브로커/모드 완전 분리)
  3) 자동손절(AUTO) 모드에서 손절가 도달 시 매도가 실행됨
  4) 수동손절 알림만(ALERT_ONLY) 모드에서는 매도가 실행되지 않고 알림만 남음
  5) 일괄 수동손절(execute_manual_stop_loss) 버튼 클릭 시 대상 종목 전량 매도
  6) real 계좌에 보유수량이 없으면 자동손절 안전조건이 실패해 주문을 내지 않음
"""

from __future__ import annotations

from datetime import datetime, timedelta

import app.services.hynix_switch_state as state_module
import app.trading.dynamic_exit_watcher as watcher
import app.trading.hynix_stop_loss_control as slc
from app.data_sources.hynix_long_collector import LONG_SYMBOL as HYNIX_SYMBOL, LONG_NAME as HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME
from app.models import OrderResult, Position
from app.trading.hynix_position_common import HynixPositionManager


class _FakeBroker:
    def __init__(self, positions=None, cash=10_000_000.0):
        self._positions = positions or []
        self._cash = cash
        self.sell_calls = []

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        remaining = []
        for p in self._positions:
            if p.symbol == symbol:
                if p.quantity > quantity:
                    p.quantity -= quantity
                    remaining.append(p)
            else:
                remaining.append(p)
        self._positions = remaining
        return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="sell", quantity=quantity, price=price, order_type=order_type, order_id="S1", message="ok")

    def get_positions(self):
        return self._positions

    def get_buyable_cash(self):
        return self._cash


class _StaleBroker(_FakeBroker):
    """sell()은 성공 응답을 주지만 내부 포지션을 갱신하지 않는 브로커 — 체결 미확인 시나리오용."""

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        return OrderResult(success=True, mode="real", account_type="real", symbol=symbol, name=name,
                            side="sell", quantity=quantity, price=price, order_type=order_type, order_id="R1", message="ok")


def _setup_state_with_entry(tmp_path, monkeypatch, mode="mock", entry_price=100_000.0, entry_minutes_ago=5):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode=mode)
    state["auto_trade_on"] = True
    state["mode"] = mode
    state["position"] = {
        **state["position"], "entry_price": entry_price,
        "entry_time": (datetime.now() - timedelta(minutes=entry_minutes_ago)).isoformat(),
    }
    state_module.save_state_atomic(state)
    return state


def test_verify_order_confirmed_false_when_balance_unchanged(tmp_path, monkeypatch):
    """1) real 주문로그(성공 응답)는 있으나 브로커 재조회 시 잔고가 그대로면 체결 미확인."""
    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10, avg_price=100_000.0, current_price=95_000.0)
    stale_broker = _StaleBroker(positions=[hynix_position])
    pm = HynixPositionManager(stale_broker, mode="real")
    pm.sync(force=True)

    order_result = stale_broker.sell(HYNIX_SYMBOL, HYNIX_NAME, 10, 95_000.0)
    assert order_result.success is True  # 주문 자체는 성공 응답

    confirmed = slc.verify_order_confirmed(pm, HYNIX_SYMBOL, expect_cleared=True)
    assert confirmed is False  # 재조회해도 브로커 보유수량이 그대로 -> 체결 미확인


def test_mock_buy_not_reflected_as_real_holding(tmp_path, monkeypatch):
    """2) mock 브로커의 매수 포지션이 real 모드 PositionManager에는 절대 반영되지 않음."""
    mock_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=5, avg_price=110_000.0, current_price=112_000.0)
    mock_broker = _FakeBroker(positions=[mock_position])
    real_broker = _FakeBroker(positions=[])  # 실계좌는 완전히 비어있음

    mock_pm = HynixPositionManager(mock_broker, mode="mock")
    real_pm = HynixPositionManager(real_broker, mode="real")
    mock_pm.sync(force=True)
    real_pm.sync(force=True)

    assert mock_pm.current_position["symbol"] == HYNIX_SYMBOL
    assert real_pm.current_position["symbol"] is None  # real은 별도 브로커 인스턴스라 mock 보유가 섞이지 않음


def test_auto_mode_sells_on_stop_loss_hit(tmp_path, monkeypatch):
    """3) 손절모드=AUTO(기본값), mock 모드에서 손절가 도달 시 즉시 매도 실행."""
    _setup_state_with_entry(tmp_path, monkeypatch, mode="mock", entry_price=100_000.0)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 98_400.0)  # -1.6% -> NORMAL SL 1.5% 이하
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")
    monkeypatch.setattr(slc, "_STOP_LOSS_LOG_PATH", tmp_path / "stop_loss_log.csv")

    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10, avg_price=100_000.0, current_price=98_400.0)
    broker = _FakeBroker(positions=[hynix_position])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

    decision = watcher.tick(now=datetime.now())

    assert decision["action"] == "SELL_ALL"
    assert len(broker.sell_calls) == 1
    reloaded = state_module.load_state(mode="mock")
    assert reloaded["position"]["symbol"] is None
    assert reloaded.get("pending_manual_stop_loss_alert") is None


def test_alert_only_mode_blocks_auto_sell(tmp_path, monkeypatch):
    """4) 손절모드=ALERT_ONLY면 손절가 도달해도 매도하지 않고 알림만 남긴다."""
    state = _setup_state_with_entry(tmp_path, monkeypatch, mode="mock", entry_price=100_000.0)
    state["stop_loss_mode"] = slc.STOP_LOSS_MODE_ALERT_ONLY
    state_module.save_state_atomic(state)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 98_400.0)
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")
    monkeypatch.setattr(slc, "_STOP_LOSS_LOG_PATH", tmp_path / "stop_loss_log.csv")

    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=10, avg_price=100_000.0, current_price=98_400.0)
    broker = _FakeBroker(positions=[hynix_position])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)

    decision = watcher.tick(now=datetime.now())

    assert decision["action"] == "SELL_ALL"  # 엔진 판단은 여전히 SELL_ALL이지만
    assert len(broker.sell_calls) == 0  # 실제 매도 주문은 나가지 않음
    reloaded = state_module.load_state(mode="mock")
    assert reloaded["position"]["symbol"] == HYNIX_SYMBOL  # 포지션이 그대로 유지됨
    assert reloaded.get("pending_manual_stop_loss_alert") is not None
    assert reloaded["pending_manual_stop_loss_alert"]["symbol"] == HYNIX_SYMBOL


def test_execute_manual_stop_loss_sells_all_targets(tmp_path, monkeypatch):
    """5) '자동매매 대상 전량 청산' 버튼 클릭 시 하이닉스/인버스 보유분 전량 매도."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(slc, "_STOP_LOSS_LOG_PATH", tmp_path / "stop_loss_log.csv")

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state_module.save_state_atomic(state)

    hynix_position = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=7, avg_price=100_000.0, current_price=101_000.0)
    inverse_position = Position(symbol=INVERSE_SYMBOL, name=INVERSE_NAME, quantity=3, avg_price=5_000.0, current_price=5_100.0)
    broker = _FakeBroker(positions=[hynix_position, inverse_position])

    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)
    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 101_000.0 if symbol == HYNIX_SYMBOL else 5_100.0)

    result = slc.execute_manual_stop_loss("mock", symbol_filter=None)

    assert result["success"] is True
    sold_symbols = {call[0] for call in broker.sell_calls}
    assert sold_symbols == {HYNIX_SYMBOL, INVERSE_SYMBOL}
    assert broker.get_positions() == []  # 전량 매도 후 브로커에 보유 종목 없음


def test_real_auto_stop_loss_blocked_without_broker_holding(tmp_path, monkeypatch):
    """6) real 계좌에 실제 보유수량이 없으면 자동손절 안전조건이 실패해 주문을 내지 않음."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="real")
    state["mode"] = "real"
    state["auto_trade_on"] = True
    state_module.save_state_atomic(state)

    empty_broker = _FakeBroker(positions=[])  # real 계좌에 실제로는 아무것도 없음
    pm = HynixPositionManager(empty_broker, mode="real")
    pm.sync(force=True)

    safety = slc.check_auto_stop_loss_safety(state, "real", pm, HYNIX_SYMBOL, datetime.now().replace(hour=10, minute=0))

    assert safety["ok"] is False
    assert any("보유 확인 실패" in reason for reason in safety["failed_checks"])
    assert len(empty_broker.sell_calls) == 0
