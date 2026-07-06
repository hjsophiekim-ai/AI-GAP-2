"""
manual_approval.py

MANUAL_APPROVAL 모드: 매수 후보를 큐에 올려 UI에 표시하고, 사용자가 승인
버튼을 눌러야 매수가 실행된다. REAL 모드에서는 password(확인문구)가
cfg.real_confirm_text()와 일치해야 승인이 통과된다.

핵심 불변식: 승인 후 체결된 포지션은 즉시 position_guard에 등록되어
이후 손절/익절/시간청산은 절대 수동승인을 기다리지 않고 자동 실행된다.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.models import OrderResult
from app.execution.position_guard import GuardedPosition

_id_counter = itertools.count(1)


@dataclass
class PendingApproval:
    id: int
    symbol: str
    name: str
    entry_price: float
    stop_loss_price: float
    take_profit1_price: float
    take_profit2_price: float
    reason: str
    policy_name: str
    quantity: int = 0
    status: str = "pending"  # pending | approved | rejected | filled | failed
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


class ManualApprovalQueue:
    def __init__(self, order_executor, position_guard, cfg: dict = None):
        self.order_executor = order_executor
        self.position_guard = position_guard
        self.cfg = cfg or {}
        self._pending: dict[int, PendingApproval] = {}

    # ------------------------------------------------------------------
    def propose(self, candidates: list, quantities: dict = None) -> list[PendingApproval]:
        """policy가 생성한 PolicyCandidate 목록을 승인 대기열에 올린다."""
        quantities = quantities or {}
        created = []
        for c in candidates:
            approval = PendingApproval(
                id=next(_id_counter),
                symbol=c.symbol, name=c.name, entry_price=c.entry_price,
                stop_loss_price=c.stop_loss_price,
                take_profit1_price=c.take_profit1_price,
                take_profit2_price=c.take_profit2_price,
                reason=c.reason, policy_name=c.policy_name,
                quantity=quantities.get(c.symbol, 0),
            )
            self._pending[approval.id] = approval
            created.append(approval)
        logger.info("[ManualApproval] 매수 후보 %d건 승인대기 등록", len(created))
        return created

    def list_pending(self) -> list[PendingApproval]:
        return [a for a in self._pending.values() if a.status == "pending"]

    # ------------------------------------------------------------------
    def approve(
        self,
        approval_id: int,
        quantity: int = None,
        password: str = None,
        is_real_mode: bool = False,
        required_password: str = None,
    ) -> Optional[OrderResult]:
        approval = self._pending.get(approval_id)
        if approval is None:
            logger.warning("[ManualApproval] 존재하지 않는 승인건: %s", approval_id)
            return None
        if approval.status != "pending":
            logger.warning("[ManualApproval] 이미 처리된 승인건: %s (status=%s)", approval_id, approval.status)
            return None

        if is_real_mode:
            if not required_password or password != required_password:
                approval.status = "failed"
                logger.warning("[ManualApproval] REAL 모드 비밀번호 불일치 — 매수 거부: %s", approval.symbol)
                return OrderResult(
                    success=False, mode="real", account_type="real",
                    symbol=approval.symbol, name=approval.name, side="buy",
                    quantity=quantity or approval.quantity, price=approval.entry_price,
                    order_type="limit", order_id="", message="비밀번호 불일치 — REAL 주문 거부",
                    error_type="password_rejected",
                )

        qty = quantity if quantity is not None else approval.quantity
        if qty <= 0:
            approval.status = "failed"
            return OrderResult(
                success=False, mode="unknown", account_type="unknown",
                symbol=approval.symbol, name=approval.name, side="buy",
                quantity=qty, price=approval.entry_price, order_type="limit",
                order_id="", message="수량 오류", error_type="validation_error",
            )

        approval.status = "approved"
        result = self.order_executor.buy(
            symbol=approval.symbol, name=approval.name, quantity=qty,
            price=approval.entry_price, reason=approval.reason, source="manual_approval",
        )

        if result.success:
            approval.status = "filled"
            self.position_guard.register_position(GuardedPosition(
                symbol=approval.symbol, name=approval.name, quantity=result.quantity,
                avg_price=result.price or approval.entry_price, source="manual",
                stop_loss_price=approval.stop_loss_price,
                take_profit1_price=approval.take_profit1_price,
                take_profit2_price=approval.take_profit2_price,
            ))
            logger.info(
                "[ManualApproval] 승인 매수 체결 + 자동매도 감시 등록 완료: %s %d주",
                approval.symbol, result.quantity,
            )
        else:
            approval.status = "failed"

        return result

    def reject(self, approval_id: int) -> bool:
        approval = self._pending.get(approval_id)
        if approval is None or approval.status != "pending":
            return False
        approval.status = "rejected"
        logger.info("[ManualApproval] 후보 거부: %s", approval.symbol)
        return True
