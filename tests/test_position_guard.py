"""
position_guard.py 테스트 — 가장 중요한 안전장치.

검증 항목:
  - 수동승인 매수 포지션도 -1.2% 도달 시 자동손절
  - 수동승인 매수 포지션도 11:10 도달 시 자동 시간청산
  - +2.0% 도달 시 50% 익절, +3.0% 도달 시 전량 익절
  - KIS 잔고에서 감지한 포지션도 동일하게 보호
"""

from unittest.mock import MagicMock

from app.execution.position_guard import PositionGuard, GuardedPosition
from app.models import OrderResult, Position


def _fake_order_executor():
    executor = MagicMock()

    def _sell(symbol, name, quantity, price, reason="", source=""):
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type="market",
            order_id="T-1", message="ok",
        )

    executor.sell.side_effect = _sell
    return executor


def test_manual_position_auto_stop_loss():
    """수동승인 매수 포지션도 -1.2% 도달 시 절대 수동승인 기다리지 않고 자동손절."""
    executor = _fake_order_executor()
    guard = PositionGuard(executor, cfg={"stop_loss_pct": -1.2, "take_profit1_pct": 2.0, "take_profit2_pct": 3.0})
    guard.register_position(GuardedPosition(symbol="000660", name="SK하이닉스", quantity=10, avg_price=100000, source="manual"))

    current_prices = {"000660": {"price": 98800.0}}  # -1.2%
    actions = guard.evaluate_and_execute(current_prices, now_hm="10:00", regime="A")

    assert len(actions) == 1
    assert actions[0]["reason"] == "stop_loss"
    assert actions[0]["quantity"] == 10
    executor.sell.assert_called_once()
    assert guard.get_open_positions() == []


def test_manual_position_auto_time_exit():
    """수동승인 매수 포지션도 11:10 도달 시 전량 시간청산 (수동승인 대기 없이)."""
    executor = _fake_order_executor()
    guard = PositionGuard(executor, cfg={"force_exit_time": "11:10"})
    guard.register_position(GuardedPosition(symbol="005930", name="삼성전자", quantity=5, avg_price=70000, source="manual"))

    current_prices = {"005930": {"price": 70100.0}}  # 거의 본전
    actions = guard.evaluate_and_execute(current_prices, now_hm="11:10", regime="A")

    assert len(actions) == 1
    assert actions[0]["reason"] == "time_exit"
    assert guard.get_open_positions() == []


def test_take_profit1_sells_half_only():
    executor = _fake_order_executor()
    guard = PositionGuard(executor, cfg={"take_profit1_pct": 2.0, "take_profit2_pct": 3.0, "stop_loss_pct": -1.2})
    guard.register_position(GuardedPosition(symbol="042700", name="한미반도체", quantity=10, avg_price=100000, source="auto"))

    actions = guard.evaluate_and_execute({"042700": {"price": 102100.0}}, now_hm="10:00", regime="A")  # +2.1%

    assert actions[0]["reason"] == "take_profit1"
    assert actions[0]["quantity"] == 5
    remaining = guard.get_open_positions()
    assert len(remaining) == 1
    assert remaining[0].quantity == 5
    assert remaining[0].tp1_executed is True


def test_take_profit2_sells_all():
    executor = _fake_order_executor()
    guard = PositionGuard(executor, cfg={"take_profit1_pct": 2.0, "take_profit2_pct": 3.0, "stop_loss_pct": -1.2})
    guard.register_position(GuardedPosition(symbol="042700", name="한미반도체", quantity=10, avg_price=100000, source="auto"))

    actions = guard.evaluate_and_execute({"042700": {"price": 103500.0}}, now_hm="10:00", regime="A")  # +3.5%

    assert actions[0]["reason"] == "take_profit2"
    assert actions[0]["quantity"] == 10
    assert guard.get_open_positions() == []


def test_kis_detected_position_is_also_protected():
    """UI/자동/수동 경로가 아니라 KIS 잔고에서 감지한 포지션도 동일하게 보호된다."""
    executor = _fake_order_executor()
    guard = PositionGuard(executor, cfg={"stop_loss_pct": -1.2})

    broker = MagicMock()
    broker.get_positions.return_value = [
        Position(symbol="000660", name="SK하이닉스", quantity=3, avg_price=200000, current_price=200000),
    ]
    added = guard.sync_from_broker(broker)
    assert added == 1
    assert guard.get_open_positions()[0].source == "kis_detected"

    actions = guard.evaluate_and_execute({"000660": {"price": 197000.0}}, now_hm="10:00", regime="A")  # -1.5%
    assert actions[0]["reason"] == "stop_loss"


def test_missing_price_does_not_force_sell():
    """현재가를 못 가져온 tick에는 강제매도하지 않고 다음 tick까지 보수적으로 유지한다."""
    executor = _fake_order_executor()
    guard = PositionGuard(executor, cfg={"stop_loss_pct": -1.2})
    guard.register_position(GuardedPosition(symbol="000660", name="SK하이닉스", quantity=1, avg_price=200000, source="auto"))

    actions = guard.evaluate_and_execute({}, now_hm="10:00", regime="A")
    assert actions == []
    assert len(guard.get_open_positions()) == 1
    executor.sell.assert_not_called()
