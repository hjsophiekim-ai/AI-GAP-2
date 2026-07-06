"""
KisMockBroker - KIS 모의투자(paper trading) 계좌 브로커.

KISClient에 모든 API 호출을 위임합니다.
API 키/토큰은 절대 로그에 출력하지 않습니다.
"""

from app.trading.broker_base import BrokerBase
from app.trading.kis_client import KISTokenError
from app.models import OrderResult, Position
from app.logger import logger


class KisMockBroker(BrokerBase):
    """KIS 모의투자 계좌 브로커."""

    mode = "mock"

    def __init__(self, kis_client) -> None:
        self.kis = kis_client

    # ------------------------------------------------------------------
    # BrokerBase interface
    # ------------------------------------------------------------------

    def get_current_price(self, symbol: str) -> float | None:
        try:
            result = self.kis.get_current_price(symbol)
            return result["current_price"] if result else None
        except Exception as e:
            logger.warning("MOCK get_current_price 예외 %s: %s", symbol, e)
            return None

    def get_balance(self) -> float:
        """예탁금총금액(인출가능금액 근사치) 반환 — 화면 표시용. 매수 판단에 쓰지 말 것."""
        try:
            result = self.kis.get_balance()
            return result.get("cash", 0.0)
        except Exception as e:
            logger.error("MOCK get_balance 예외: %s", e)
            return 0.0

    def get_orderable_cash(self) -> float:
        """주문가능현금(ord_psbl_cash) 반환 — 매수 예산 판단에 사용."""
        try:
            return self.kis.get_buyable_cash()
        except Exception as e:
            logger.error("MOCK get_orderable_cash 예외: %s", e)
            return 0.0

    def get_buyable_cash(self) -> float:
        """하위 호환 — get_orderable_cash() 위임."""
        return self.get_orderable_cash()

    def get_stock_buyable_amount(self, symbol: str = "005930", price: int = 0) -> float:
        """종목별 매수가능금액."""
        try:
            return self.kis.get_buyable_cash(symbol=symbol, price=price)
        except Exception as e:
            logger.error("MOCK get_stock_buyable_amount 예외 %s: %s", symbol, e)
            return 0.0

    def get_account_cash_breakdown(self) -> dict:
        """계좌 현금 상세 분리 조회."""
        try:
            return self.kis.get_account_cash_breakdown()
        except Exception as e:
            logger.error("MOCK get_account_cash_breakdown 예외: %s", e)
            return {
                "withdrawable_amount": 0.0, "cash_balance": 0.0,
                "orderable_cash": 0.0, "buyable_amount": 0.0,
                "settlement_pending_cash": 0.0, "raw_fields": {}, "error": str(e),
            }

    def get_positions(self) -> list[Position]:
        result = self.kis.get_balance()
        if "error" in result:
            err = result["error"]
            logger.error("MOCK get_positions 잔고 조회 오류: %s", err)
            raise RuntimeError(f"KIS 모의계좌 잔고 조회 실패: {err}")
        positions = []
        for item in (result.get("positions") or []):
            positions.append(
                Position(
                    symbol=item["symbol"],
                    name=item["name"],
                    quantity=item["quantity"],
                    avg_price=item["avg_price"],
                    current_price=item["current_price"],
                )
            )
        logger.info("MOCK get_positions: %d 종목", len(positions))
        return positions

    def buy(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        try:
            result = self.kis.buy(symbol, quantity, int(price), order_type)
            return OrderResult(
                success=result["success"],
                mode=self.mode,
                account_type="mock",
                symbol=symbol,
                name=name,
                side="buy",
                quantity=quantity,
                price=price,
                order_type=order_type,
                order_id=result.get("order_id", ""),
                message=result.get("message", ""),
                raw=result.get("raw", {}),
                http_status=result.get("http_status", 0),
            )
        except KISTokenError:
            raise
        except Exception as e:
            logger.error("MOCK buy 예외 %s: %s", symbol, e)
            return OrderResult(
                success=False, mode=self.mode, account_type="mock",
                symbol=symbol, name=name, side="buy",
                quantity=quantity, price=price, order_type=order_type,
                order_id="", message=str(e),
                error_type="exception",
            )

    def sell(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        try:
            result = self.kis.sell(symbol, quantity, int(price), order_type)
            return OrderResult(
                success=result["success"],
                mode=self.mode,
                account_type="mock",
                symbol=symbol,
                name=name,
                side="sell",
                quantity=quantity,
                price=price,
                order_type=order_type,
                order_id=result.get("order_id", ""),
                message=result.get("message", ""),
                raw=result.get("raw", {}),
            )
        except Exception as e:
            logger.error("MOCK sell 예외 %s: %s", symbol, e)
            return OrderResult(
                success=False, mode=self.mode, account_type="mock",
                symbol=symbol, name=name, side="sell",
                quantity=quantity, price=price, order_type=order_type,
                order_id="", message=str(e),
            )
