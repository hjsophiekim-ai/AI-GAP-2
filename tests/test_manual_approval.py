"""
manual_approval.py 테스트.

검증 항목:
  - 후보 제안(propose) 후 승인(approve) 시 매수 체결 + position_guard 자동 등록.
  - REAL 모드에서 비밀번호 불일치 시 거부.
  - 거부(reject) 처리.
  - 승인 후 등록된 포지션은 이후 position_guard의 자동매도 대상이 된다 (핵심 불변식).
"""

from unittest.mock import MagicMock

from app.execution.manual_approval import ManualApprovalQueue
from app.execution.position_guard import PositionGuard
from app.strategy.policy_base import PolicyCandidate
from app.models import OrderResult


def _candidate(symbol="000660", name="SK하이닉스", price=200000.0):
    return PolicyCandidate(
        symbol=symbol, name=name, entry_price=price,
        stop_loss_price=price * 0.988, take_profit1_price=price * 1.02, take_profit2_price=price * 1.03,
        reason="테스트 후보", policy_name="policy_leader_top3",
    )


def _order_executor(success=True):
    executor = MagicMock()
    executor.buy.return_value = OrderResult(
        success=success, mode="mock", account_type="mock", symbol="000660", name="SK하이닉스",
        side="buy", quantity=1, price=200000.0, order_type="limit",
        order_id="T-1" if success else "", message="ok" if success else "실패",
    )
    return executor


def test_propose_creates_pending_approval():
    executor = _order_executor()
    guard = PositionGuard(executor)
    queue = ManualApprovalQueue(executor, guard)

    pending = queue.propose([_candidate()], quantities={"000660": 1})
    assert len(pending) == 1
    assert queue.list_pending()[0].symbol == "000660"


def test_approve_paper_mode_registers_position_for_auto_exit():
    """승인 매수 체결 후 position_guard에 즉시 등록되어야 한다 (수동매수 자동매도 보호)."""
    executor = _order_executor(success=True)
    guard = PositionGuard(executor)
    queue = ManualApprovalQueue(executor, guard)

    pending = queue.propose([_candidate()], quantities={"000660": 1})
    result = queue.approve(pending[0].id, is_real_mode=False)

    assert result.success is True
    executor.buy.assert_called_once()
    open_positions = guard.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].source == "manual"
    assert queue.list_pending() == []  # 더 이상 대기 목록에 없음


def test_approve_real_mode_wrong_password_rejected():
    executor = _order_executor(success=True)
    guard = PositionGuard(executor)
    queue = ManualApprovalQueue(executor, guard)

    pending = queue.propose([_candidate()], quantities={"000660": 1})
    result = queue.approve(
        pending[0].id, is_real_mode=True, password="WRONG", required_password="REAL_ORDER_CONFIRMED",
    )

    assert result.success is False
    assert result.error_type == "password_rejected"
    executor.buy.assert_not_called()
    assert guard.get_open_positions() == []


def test_approve_real_mode_correct_password_succeeds():
    executor = _order_executor(success=True)
    guard = PositionGuard(executor)
    queue = ManualApprovalQueue(executor, guard)

    pending = queue.propose([_candidate()], quantities={"000660": 1})
    result = queue.approve(
        pending[0].id, is_real_mode=True, password="REAL_ORDER_CONFIRMED", required_password="REAL_ORDER_CONFIRMED",
    )

    assert result.success is True
    executor.buy.assert_called_once()


def test_reject_marks_status_and_prevents_approval():
    executor = _order_executor()
    guard = PositionGuard(executor)
    queue = ManualApprovalQueue(executor, guard)

    pending = queue.propose([_candidate()], quantities={"000660": 1})
    assert queue.reject(pending[0].id) is True
    assert queue.list_pending() == []

    # 이미 거부된 건은 다시 승인할 수 없다.
    result = queue.approve(pending[0].id)
    assert result is None


def test_failed_buy_does_not_register_position():
    executor = _order_executor(success=False)
    guard = PositionGuard(executor)
    queue = ManualApprovalQueue(executor, guard)

    pending = queue.propose([_candidate()], quantities={"000660": 1})
    result = queue.approve(pending[0].id, is_real_mode=False)

    assert result.success is False
    assert guard.get_open_positions() == []
