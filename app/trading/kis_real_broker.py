"""
KisRealBroker - KIS 실전투자 계좌 브로커.

SAFETY: 6가지 안전 조건.
  1. mode == "real"
  2. config.yaml kis.real.enabled == true  OR  runtime_real_mode == True
  3. config.yaml safety.enable_real_trading == true  OR  runtime_real_mode == True
  4. 확인 문구 "REAL_ORDER_CONFIRMED" 일치  (항상 필요)
  5. 매수 전: enable_real_buy == true  OR  runtime_real_mode == True  + 주문금액 한도
  6. 매도 전: enable_real_sell == true  OR  runtime_real_mode == True

gate 1~4: __init__에서 검사 → 브로커 자체를 못 만들게 차단
gate 5:   buy()에서 검사 → OrderResult(success=False) 반환
gate 6:   sell()에서 검사 → OrderResult(success=False) 반환

runtime_real_mode=True (UI 실전모드 버튼) 이면 gate 2~3 우회.
매수/매도 모두 runtime_real_mode가 True일 때만 실제 주문 가능.
"""

from app.trading.broker_base import BrokerBase
from app.trading.kis_client import KISTokenError
from app.models import OrderResult, Position
from app.logger import logger

_REAL_MODE_BLOCKED_MSG = (
    "실전모드가 활성화되어 있지 않습니다. "
    "실제 주문을 실행하려면 실전모드 버튼을 누르고 확인 문구를 입력하세요."
)


class KisRealBroker(BrokerBase):
    """KIS 실전투자 계좌 브로커."""

    mode = "real"

    def __init__(
        self,
        kis_client,
        cfg=None,
        confirm_text: str = "",
        runtime_real_mode: bool = False,
        runtime_enable_real_buy: bool = False,
        runtime_enable_real_sell: bool = False,
        **kwargs,
    ) -> None:
        from app.config import get_config
        self._cfg = cfg or get_config()
        self._runtime_real_mode = runtime_real_mode
        self._runtime_enable_real_buy = runtime_enable_real_buy
        self._runtime_enable_real_sell = runtime_enable_real_sell

        # gate 2: kis.real.enabled OR runtime_real_mode
        kis_cfg = self._cfg._raw.get("kis", {})
        real_section = kis_cfg.get("real", {})
        real_enabled = real_section.get("enabled", False)
        if not runtime_real_mode and not real_enabled:
            raise RuntimeError(
                "실전 계좌가 비활성화되어 있습니다. "
                "현재 kis.real.enabled=false 또는 실전모드 버튼이 활성화되지 않았습니다. "
                "실제 주문을 원하면 실전모드를 활성화하세요."
            )

        # gate 3: safety.enable_real_trading OR runtime_real_mode
        if not runtime_real_mode and not self._cfg.real_trading_enabled():
            raise RuntimeError(
                "실전투자 모드가 비활성화되어 있습니다. "
                "config.yaml의 safety.enable_real_trading을 true로 설정하거나 "
                "실전모드를 활성화하세요."
            )

        # gate 4: 확인 문구 (항상 필요)
        expected = self._cfg.real_confirm_text()
        if self._cfg.require_real_confirm() and confirm_text != expected:
            raise RuntimeError(
                f"실전투자 확인 문구가 틀립니다. '{expected}'를 정확히 입력하세요."
            )

        self.kis = kis_client
        self._daily_ordered_amount: float = 0.0

    # ------------------------------------------------------------------
    # 주문 금액 안전장치 (gate 5, 매수 전용)
    # ------------------------------------------------------------------

    def _get_order_limits(self) -> dict:
        """실계좌 주문 안전한도 읽기."""
        safety = getattr(self._cfg, "safety", {}) or {}
        raw = getattr(self._cfg, "_raw", {}) or {}
        raw_safety = raw.get("safety", {}) if isinstance(raw, dict) else {}
        merged = {**raw_safety, **safety}
        merged["_use_env_auto_reduce"] = bool(raw_safety)
        if any(key in merged for key in ("max_order_amount", "max_daily_order_amount", "max_position_amount_per_symbol", "max_real_order_amount", "max_real_daily_budget")):
            return self._normalize_order_limits(merged)
        getter = getattr(self._cfg, "get_real_order_limits", None)
        if callable(getter):
            try:
                limits = getter()
                if isinstance(limits, dict):
                    return self._normalize_order_limits(limits)
            except Exception:
                pass
        return self._normalize_order_limits(merged)

    def _normalize_order_limits(self, values: dict) -> dict:
        def _num(*keys: str, default: float) -> float:
            for key in keys:
                value = values.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            return float(default)

        return {
            "per_symbol": _num("max_position_amount_per_symbol", "max_order_amount", default=10_000_000),
            "per_order": _num("max_order_amount", "max_real_order_amount", default=10_000_000),
            "daily": _num("max_daily_order_amount", "max_real_daily_budget", default=30_000_000),
            "auto_reduce": str(values.get("auto_reduce_order", values.get("auto_reduce", __import__("os").environ.get("AUTO_REDUCE_QUANTITY_ON_SAFETY_LIMIT", "false") if values.get("_use_env_auto_reduce") else "false"))).lower() in ("1", "true", "yes", "y"),
        }

    def _check_order_limits(
        self,
        quantity: int,
        price: float,
        symbol: str = "",
        allocated_budget: float = 0.0,
    ) -> tuple:
        """금액 한도 확인. 반환: (error_msg | None, error_type | None)"""
        limits = self._get_order_limits()
        order_amt = quantity * price

        orderable_cash = 0.0
        try:
            if symbol:
                orderable_cash = self.kis.get_buyable_cash(symbol=symbol, price=int(price))
        except Exception:
            pass
        if not isinstance(orderable_cash, (int, float)):
            orderable_cash = 0.0

        if limits["per_symbol"] > 0 and order_amt > limits["per_symbol"]:
            msg = (
                f"실계좌 안전한도 초과\n"
                f"• 주문금액: {order_amt:,.0f}원\n"
                f"• 종목당 보유한도: {limits['per_symbol']:,.0f}원\n"
                f"• 계좌 주문가능금액: {orderable_cash:,.0f}원\n"
                f"• 해결방법: REAL_MAX_POSITION_AMOUNT_PER_SYMBOL 또는 UI 한도를 상향하세요"
            )
            return msg, "safety_symbol_limit_exceeded"

        if order_amt > limits["per_order"]:
            msg = (
                f"실계좌 안전한도 초과\n"
                f"• 주문금액: {order_amt:,.0f}원\n"
                f"• 1회 주문 안전한도: {limits['per_order']:,.0f}원\n"
                f"• 종목별 배정예산: {allocated_budget:,.0f}원\n"
                f"• 계좌 주문가능금액: {orderable_cash:,.0f}원\n"
                f"• 해결방법: REAL_MAX_ORDER_AMOUNT 또는 UI의 1회 주문한도를 상향하세요"
            )
            return msg, "safety_per_order_limit_exceeded"

        if self._daily_ordered_amount + order_amt > limits["daily"]:
            msg = (
                f"실계좌 일일한도 초과\n"
                f"• 오늘 주문누계: {self._daily_ordered_amount:,.0f}원\n"
                f"• 이번 주문금액: {order_amt:,.0f}원\n"
                f"• 일일 주문한도: {limits['daily']:,.0f}원\n"
                f"• 해결방법: REAL_MAX_DAILY_ORDER_AMOUNT를 상향하세요"
            )
            return msg, "safety_daily_limit_exceeded"

        return None, None

    def _auto_reduce_quantity(
        self,
        quantity: int,
        price: float,
        symbol: str = "",
        allocated_budget: float = 0.0,
    ) -> tuple:
        """한도 내로 수량 자동 조정. 반환: (new_quantity, reason_str)"""
        limits = self._get_order_limits()
        safe_margin = 0.98

        orderable_cash = 0.0
        try:
            if symbol:
                orderable_cash = self.kis.get_buyable_cash(symbol=symbol, price=int(price))
        except Exception:
            pass
        if not isinstance(orderable_cash, (int, float)):
            orderable_cash = 0.0

        remaining_daily = max(0.0, limits["daily"] - self._daily_ordered_amount)
        candidates = [limits["per_order"], remaining_daily, limits["per_symbol"]]
        if orderable_cash > 0:
            candidates.append(orderable_cash)
        usable = min(candidates)

        safe_amount = usable * safe_margin
        new_qty = int(safe_amount / price) if price > 0 else 0

        if new_qty < 1:
            return 0, "한도 내 주문 불가 (최소 1주 미만)"
        if new_qty < quantity:
            return new_qty, f"한도에 맞춰 수량 자동 조정: {quantity}주 → {new_qty}주"
        return quantity, ""

    # ------------------------------------------------------------------
    # BrokerBase interface
    # ------------------------------------------------------------------

    def get_current_price(self, symbol: str) -> float | None:
        try:
            result = self.kis.get_current_price(symbol)
            return result["current_price"] if result else None
        except Exception as e:
            logger.warning("REAL get_current_price 예외 %s: %s", symbol, e)
            return None

    def get_balance(self) -> float:
        """예탁금총금액(인출가능금액 근사치) 반환 — 화면 표시용. 매수 판단에 쓰지 말 것."""
        try:
            result = self.kis.get_balance()
            return result.get("cash", 0.0)
        except Exception as e:
            logger.error("REAL get_balance 예외: %s", e)
            return 0.0

    def get_orderable_cash(self) -> float:
        """주문가능현금(ord_psbl_cash) 반환 — 매수 예산 판단에 사용."""
        try:
            return self.kis.get_buyable_cash()
        except Exception as e:
            logger.error("REAL get_orderable_cash 예외: %s", e)
            return 0.0

    def get_buyable_cash(self) -> float:
        """하위 호환 — get_orderable_cash() 위임."""
        return self.get_orderable_cash()

    def get_stock_buyable_amount(self, symbol: str = "005930", price: int = 0) -> float:
        """종목별 매수가능금액 (inquire-psbl-order 기준)."""
        try:
            return self.kis.get_buyable_cash(symbol=symbol, price=price)
        except Exception as e:
            logger.error("REAL get_stock_buyable_amount 예외 %s: %s", symbol, e)
            return 0.0

    def get_account_cash_breakdown(self) -> dict:
        """계좌 현금 상세 분리 조회."""
        try:
            return self.kis.get_account_cash_breakdown()
        except Exception as e:
            logger.error("REAL get_account_cash_breakdown 예외: %s", e)
            return {
                "withdrawable_amount": 0.0, "cash_balance": 0.0,
                "orderable_cash": 0.0, "buyable_amount": 0.0,
                "settlement_pending_cash": 0.0, "raw_fields": {}, "error": str(e),
            }

    def get_positions(self) -> list[Position]:
        """잔고 조회. API 오류 시 RuntimeError 발생."""
        result = self.kis.get_balance()
        if "error" in result:
            err = result["error"]
            logger.error("REAL get_positions 잔고 조회 오류: %s", err)
            raise RuntimeError(f"KIS 실계좌 잔고 조회 실패: {err}")
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
        logger.info("REAL get_positions: %d 종목", len(positions))
        return positions

    def buy(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        # gate 5a: enable_real_buy OR runtime flags
        real_buy_ok = (
            self._runtime_real_mode
            or self._runtime_enable_real_buy
            or self._cfg.real_buy_enabled()
        )
        if not real_buy_ok:
            logger.warning("REAL 매수 차단 (실전모드 미활성화): %s", symbol)
            return OrderResult(
                success=False, mode=self.mode, account_type="real",
                symbol=symbol, name=name, side="buy",
                quantity=quantity, price=price, order_type=order_type,
                order_id="", message=_REAL_MODE_BLOCKED_MSG,
            )

        # gate 5b: 주문금액 한도 (auto_reduce 지원)
        limits = self._get_order_limits()
        err_msg, err_type = self._check_order_limits(
            quantity, price, symbol=symbol, allocated_budget=0.0,
        )
        if err_msg:
            if err_type == "safety_per_order_limit_exceeded" and "safety rule" not in err_msg:
                err_msg = "safety rule: " + err_msg
            if err_type == "safety_daily_limit_exceeded":
                err_msg = err_msg.replace("일일한도", "일일 한도")
            if limits.get("auto_reduce", True):
                new_qty, reduce_reason = self._auto_reduce_quantity(quantity, price, symbol=symbol)
                if new_qty < 1:
                    logger.warning("REAL 매수 차단 (자동조정 실패): %s | %s", symbol, err_msg.split('\n')[0])
                    return OrderResult(
                        success=False, mode=self.mode, account_type="real",
                        symbol=symbol, name=name, side="buy",
                        quantity=quantity, price=price, order_type=order_type,
                        order_id="", message=err_msg,
                        error_type=err_type,
                    )
                logger.info("REAL 매수 수량 자동조정: %s %d→%d주 (%s)", symbol, quantity, new_qty, reduce_reason)
                quantity = new_qty
                err_msg2, err_type2 = self._check_order_limits(quantity, price, symbol=symbol)
                if err_msg2:
                    logger.warning("REAL 매수 차단 (조정 후도 초과): %s", symbol)
                    return OrderResult(
                        success=False, mode=self.mode, account_type="real",
                        symbol=symbol, name=name, side="buy",
                        quantity=quantity, price=price, order_type=order_type,
                        order_id="", message=err_msg2,
                        error_type=err_type2,
                    )
            else:
                logger.warning("REAL 매수 차단: %s", err_msg.split('\n')[0])
                return OrderResult(
                    success=False, mode=self.mode, account_type="real",
                    symbol=symbol, name=name, side="buy",
                    quantity=quantity, price=price, order_type=order_type,
                    order_id="", message=err_msg,
                    error_type=err_type,
                )

        logger.info(
            "REAL BUY: symbol=%s name=%s quantity=%d price=%s order_type=%s",
            symbol, name, quantity, price, order_type,
        )
        try:
            result = self.kis.buy(symbol, quantity, int(price), order_type)
            if result["success"]:
                self._daily_ordered_amount += quantity * price
            return OrderResult(
                success=result["success"],
                mode=self.mode,
                account_type="real",
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
            logger.error("REAL buy 예외 %s: %s", symbol, e)
            return OrderResult(
                success=False, mode=self.mode, account_type="real",
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
        # gate 6: enable_real_sell OR runtime flags
        real_sell_ok = (
            self._runtime_real_mode
            or self._runtime_enable_real_sell
            or self._cfg.real_sell_enabled()
        )
        if not real_sell_ok:
            logger.warning("REAL 매도 차단 (실전모드 미활성화): %s", symbol)
            return OrderResult(
                success=False, mode=self.mode, account_type="real",
                symbol=symbol, name=name, side="sell",
                quantity=quantity, price=price, order_type=order_type,
                order_id="", message=_REAL_MODE_BLOCKED_MSG,
            )

        logger.info(
            "REAL SELL: symbol=%s quantity=%d price=%s", symbol, quantity, price
        )
        try:
            result = self.kis.sell(symbol, quantity, int(price), order_type)
            return OrderResult(
                success=result["success"],
                mode=self.mode,
                account_type="real",
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
            logger.error("REAL sell 예외 %s: %s", symbol, e)
            return OrderResult(
                success=False, mode=self.mode, account_type="real",
                symbol=symbol, name=name, side="sell",
                quantity=quantity, price=price, order_type=order_type,
                order_id="", message=str(e),
            )
