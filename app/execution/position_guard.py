"""
position_guard.py

가장 중요한 모듈: 진입 경로(자동매수/수동승인매수/UI 직접등록/KIS 잔고감지)와
무관하게 모든 포지션을 감시하고, 조건 충족 시 반드시 자동매도한다.

"수동매수라도 자동매도 보호"가 핵심 — force_auto_exit 설정과 무관하게
손절/시간청산은 절대 수동승인을 기다리지 않는다.

자동매도 조건 (evaluate_and_execute 1회 호출당 포지션별로 최초 매칭 규칙 적용):
  1. +2.0% 도달: 50% 익절 (최초 1회만)
  2. +3.0% 도달: 전량 익절
  3. -1.2% 도달: 전량 손절
  4. 09:20 기준 저점 이탈: 전량 손절
  5. VWAP 재이탈 + 약세 신호: 절반 또는 전량 손절
  6. 시간청산 시각 도달: 전량 시간청산
  7. 시장유형 D/E 악화: 위험 재평가 후 위험하면 전량매도
  8. 데이터 오류 누적: 신규매수 금지(policy 레이어), 기존 포지션은 보수적 유지(강제매도 안 함)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

DEFAULT_TP1_PCT = 2.0
DEFAULT_TP2_PCT = 3.0
DEFAULT_SL_PCT = -1.2
DEFAULT_FORCE_EXIT_TIME = "11:10"


@dataclass
class GuardedPosition:
    symbol: str
    name: str
    quantity: int
    avg_price: float
    source: str = "auto"  # auto | manual | ui | kis_detected
    entry_time: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    reference_0920_low: Optional[float] = None
    vwap: Optional[float] = None
    tp1_executed: bool = False
    status: str = "open"  # open | closed
    stop_loss_price: Optional[float] = None
    take_profit1_price: Optional[float] = None
    take_profit2_price: Optional[float] = None

    def profit_rate(self, current_price: float) -> float:
        if not self.avg_price:
            return 0.0
        return (current_price - self.avg_price) / self.avg_price * 100


class PositionGuard:
    def __init__(self, order_executor, cfg: dict = None, risk_manager=None):
        self.order_executor = order_executor
        self.cfg = cfg or {}
        self.risk_manager = risk_manager
        self._positions: dict[str, GuardedPosition] = {}

    # ------------------------------------------------------------------
    # Registration — 진입 경로 무관하게 모든 포지션을 등록한다.
    # ------------------------------------------------------------------

    def register_position(self, position: GuardedPosition) -> None:
        existing = self._positions.get(position.symbol)
        if existing and existing.status == "open":
            logger.debug("[PositionGuard] 이미 감시 중: %s", position.symbol)
            return
        self._positions[position.symbol] = position
        logger.info(
            "[PositionGuard] 포지션 등록(감시 시작): %s %s %d주 @ %.0f (source=%s)",
            position.symbol, position.name, position.quantity, position.avg_price, position.source,
        )

    def sync_from_broker(self, broker) -> int:
        """KIS 잔고에서 아직 감시하지 않는 포지션을 찾아 등록한다."""
        try:
            broker_positions = broker.get_positions()
        except Exception as exc:
            logger.warning("[PositionGuard] 브로커 잔고 조회 실패: %s", exc)
            return 0

        added = 0
        for p in broker_positions:
            if p.symbol in self._positions and self._positions[p.symbol].status == "open":
                continue
            self.register_position(GuardedPosition(
                symbol=p.symbol, name=p.name, quantity=p.quantity,
                avg_price=p.avg_price, source="kis_detected",
            ))
            added += 1
        if added:
            logger.info("[PositionGuard] KIS 잔고에서 %d개 미감시 포지션 발견 → 등록", added)
        return added

    def get_open_positions(self) -> list[GuardedPosition]:
        return [p for p in self._positions.values() if p.status == "open"]

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate_and_execute(
        self,
        current_prices: dict[str, dict],
        now_hm: str = None,
        regime: str = "",
        data_error_streak: int = 0,
        alert_level: str = "NONE",
    ) -> list[dict]:
        now_hm = now_hm or datetime.now().strftime("%H:%M")
        tp1_pct = self.cfg.get("take_profit1_pct", DEFAULT_TP1_PCT)
        tp2_pct = self.cfg.get("take_profit2_pct", DEFAULT_TP2_PCT)
        sl_pct = self.cfg.get("stop_loss_pct", DEFAULT_SL_PCT)
        force_exit_time = self.cfg.get("force_exit_time", DEFAULT_FORCE_EXIT_TIME)

        actions: list[dict] = []

        for position in self.get_open_positions():
            price_info = current_prices.get(position.symbol)
            if not price_info or price_info.get("price") is None:
                logger.warning(
                    "[PositionGuard] %s 현재가 없음 — 이번 tick 보수적 유지(강제매도 안함)",
                    position.symbol,
                )
                continue

            price = price_info["price"]
            profit_rate = position.profit_rate(price)

            action = self._decide_action(
                position, price, profit_rate, now_hm, regime,
                tp1_pct, tp2_pct, sl_pct, force_exit_time, alert_level,
            )
            if action is None:
                continue

            executed = self._execute_action(position, price, action)
            if executed:
                actions.append(executed)

        return actions

    def _decide_action(
        self, position, price, profit_rate, now_hm, regime,
        tp1_pct, tp2_pct, sl_pct, force_exit_time, alert_level="NONE",
    ) -> Optional[dict]:
        # 6. 시간청산 (최우선 — 절대 방치 금지)
        if now_hm >= force_exit_time:
            return {"reason": "time_exit", "sell_ratio": 1.0}

        # 3/4. 손절 (전량) — 09:20 저점 이탈도 동일 취급
        if profit_rate <= sl_pct:
            return {"reason": "stop_loss", "sell_ratio": 1.0}
        if position.reference_0920_low and price < position.reference_0920_low:
            return {"reason": "0920_low_break", "sell_ratio": 1.0}

        # 2. 전량 익절
        if profit_rate >= tp2_pct:
            return {"reason": "take_profit2", "sell_ratio": 1.0}

        # 1. 50% 익절 (최초 1회)
        if profit_rate >= tp1_pct and not position.tp1_executed:
            return {"reason": "take_profit1", "sell_ratio": 0.5}

        # 5. VWAP 재이탈 + 약세 신호
        if position.vwap and price < position.vwap and profit_rate < 0:
            return {"reason": "vwap_breakdown", "sell_ratio": 1.0 if profit_rate < sl_pct / 2 else 0.5}

        # 7. 시장유형 악화 (D/E) — 이미 손실 중이면 위험 회피
        if regime in ("D", "E") and profit_rate < 0:
            return {"reason": "regime_deteriorated", "sell_ratio": 1.0}

        # Market Alert CRITICAL: 뚜렷하게 수익 중(>=tp1_pct)이 아니면 위험청산.
        # "손절가 도달 전이라도 위험청산 옵션 제공"의 자동 실행 버전 — 이미 확실히
        # 수익 구간(1차 익절선 이상)인 포지션까지 강제로 팔지는 않는다.
        if alert_level == "CRITICAL" and profit_rate < tp1_pct:
            return {"reason": "critical_alert_defensive", "sell_ratio": 1.0}

        return None

    def _execute_action(self, position: GuardedPosition, price: float, action: dict) -> Optional[dict]:
        sell_ratio = action["sell_ratio"]
        reason = action["reason"]
        qty = position.quantity if sell_ratio >= 1.0 else max(1, int(position.quantity * sell_ratio))
        qty = min(qty, position.quantity)

        result = self.order_executor.sell(
            symbol=position.symbol, name=position.name, quantity=qty, price=price,
            reason=reason, source=f"position_guard:{position.source}",
        )

        record = {
            "symbol": position.symbol, "name": position.name, "reason": reason,
            "quantity": qty, "price": price, "profit_rate": position.profit_rate(price),
            "success": result.success, "order_id": result.order_id, "source": position.source,
        }

        if not result.success:
            logger.error(
                "[PositionGuard][긴급] %s 매도 실패(reason=%s): %s",
                position.symbol, reason, result.message,
            )
            return record

        if self.risk_manager is not None:
            pnl_amount = (price - position.avg_price) * qty
            realized_pct = position.profit_rate(price)
            is_loss_reason = reason in ("stop_loss", "0920_low_break") or (
                reason in ("regime_deteriorated", "critical_alert_defensive") and realized_pct < 0
            )
            self.risk_manager.record_trade_result(
                symbol=position.symbol, pnl_amount=pnl_amount,
                pnl_pct=realized_pct,
                was_stop_loss=is_loss_reason,
            )

        if qty >= position.quantity:
            position.status = "closed"
            position.quantity = 0
        else:
            position.quantity -= qty
            if reason == "take_profit1":
                position.tp1_executed = True

        logger.info(
            "[PositionGuard] 매도 실행: %s %s %d주 @ %.0f (%s, 수익률 %.2f%%)",
            position.symbol, position.name, qty, price, reason, record["profit_rate"],
        )
        return record

    # ------------------------------------------------------------------
    def get_status(self) -> list[dict]:
        """UI 표시용 감시 상태 (손절/익절/시간청산 예정 조건 포함)."""
        status = []
        for p in self.get_open_positions():
            status.append({
                "symbol": p.symbol, "name": p.name, "quantity": p.quantity,
                "avg_price": p.avg_price, "source": p.source,
                "stop_loss_price": p.stop_loss_price, "take_profit1_price": p.take_profit1_price,
                "take_profit2_price": p.take_profit2_price, "tp1_executed": p.tp1_executed,
                "entry_time": p.entry_time,
            })
        return status
