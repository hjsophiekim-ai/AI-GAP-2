"""
RealBroker - KIS 실전투자 계좌를 사용하는 브로커.

SAFETY: 인스턴스 생성 시 config.safety.enable_real_trading == True 여부를 확인합니다.
        비활성화 상태이면 RuntimeError를 발생시켜 실수로 실전 주문이 나가는 것을 방지합니다.
"""

import requests

from app.trading.broker_base import BrokerBase
from app.models import OrderResult, Position
from app.logger import logger


class RealBroker(BrokerBase):
    """KIS 실전투자 계좌를 사용하는 브로커."""

    mode = "real"

    def __init__(self, kis_client, cfg=None, confirm_text: str = ""):
        from app.config import get_config
        self._cfg = cfg or get_config()

        # Safety gate 1: enable_real_trading flag
        if not self._cfg.real_trading_enabled():
            raise RuntimeError("실전투자 모드가 비활성화되어 있습니다. config.yaml의 safety.enable_real_trading을 true로 설정하세요.")

        # Safety gate 2: confirm text
        expected = self._cfg.real_confirm_text()
        if self._cfg.require_real_confirm() and confirm_text != expected:
            raise RuntimeError(f"실전투자 안전 확인 문구가 틀립니다. '{expected}'를 정확히 입력하세요.")

        self.kis = kis_client
        self._daily_ordered_amount: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _order(
        self,
        side: str,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        """Place a buy or sell order on the KIS real account."""
        # tr_id differs by side (실전투자 코드)
        # 매수: TTTC0802U  매도: TTTC0801U
        tr_id = "TTTC0802U" if side == "buy" else "TTTC0801U"

        ord_dvsn = "00" if order_type == "limit" else "01"
        ord_price = str(int(price)) if order_type == "limit" else "0"

        logger.info(
            "REAL ORDER: symbol=%s name=%s side=%s quantity=%d price=%s order_type=%s",
            symbol, name, side, quantity, ord_price, order_type,
        )

        token = self.kis.get_access_token()
        if not token:
            return OrderResult(
                success=False,
                mode=self.mode,
                account_type="real",
                symbol=symbol,
                name=name,
                side=side,
                quantity=quantity,
                price=price,
                order_type=order_type,
                order_id="",
                message="KIS 토큰을 가져올 수 없습니다.",
            )

        url = f"{self.kis.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": self.kis.app_key,
            "appsecret": self.kis.app_secret,
            "tr_id": tr_id,
        }
        body = {
            "CANO": self.kis.account_no,
            "ACNT_PRDT_CD": self.kis.product_code,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": ord_price,
        }

        try:
            resp = requests.post(url, json=body, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "-1")
            msg = data.get("msg1", "")
            output = data.get("output", {})
            order_id = output.get("ODNO", "") if output else ""

            success = rt_cd == "0"
            if not success:
                logger.warning(
                    "REAL %s 주문 실패 symbol=%s rt_cd=%s msg=%s",
                    side, symbol, rt_cd, msg,
                )
            else:
                logger.info(
                    "REAL %s 주문 성공 symbol=%s quantity=%d price=%s order_id=%s",
                    side, symbol, quantity, ord_price, order_id,
                )

            return OrderResult(
                success=success,
                mode=self.mode,
                account_type="real",
                symbol=symbol,
                name=name,
                side=side,
                quantity=quantity,
                price=price,
                order_type=order_type,
                order_id=order_id,
                message=msg,
                raw=data,
            )

        except Exception as e:
            logger.error("REAL %s 주문 예외 symbol=%s: %s", side, symbol, e)
            return OrderResult(
                success=False,
                mode=self.mode,
                account_type="real",
                symbol=symbol,
                name=name,
                side=side,
                quantity=quantity,
                price=price,
                order_type=order_type,
                order_id="",
                message=str(e),
            )

    # ------------------------------------------------------------------
    # BrokerBase interface
    # ------------------------------------------------------------------

    def _check_real_limits(self, quantity: int, price: float) -> str | None:
        """추가 금액 안전장치 확인. 위반 시 사유 문자열 반환, 통과 시 None."""
        safety = self._cfg.safety
        order_amt = quantity * price
        max_order = float(safety.get("max_real_order_amount", 1_000_000))
        max_daily = float(safety.get("max_real_daily_budget", 1_000_000))
        if order_amt > max_order:
            return f"주문금액 초과: {order_amt:,.0f}원 > 한도 {max_order:,.0f}원"
        if self._daily_ordered_amount + order_amt > max_daily:
            return f"일일 한도 초과: {self._daily_ordered_amount + order_amt:,.0f}원 > {max_daily:,.0f}원"
        return None

    def get_current_price(self, symbol: str) -> float | None:
        try:
            result = self.kis.get_current_price(symbol)
            return result["current_price"] if result else None
        except Exception as e:
            logger.warning("REAL get_current_price 예외 %s: %s", symbol, e)
            return None

    def get_buyable_cash(self) -> float:
        return self.kis.get_buyable_cash()

    def buy(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        limit_msg = self._check_real_limits(quantity, price)
        if limit_msg:
            logger.warning("REAL 주문 차단: %s", limit_msg)
            return OrderResult(
                success=False, mode=self.mode, account_type="real",
                symbol=symbol, name=name, side="buy", quantity=quantity,
                price=price, order_type=order_type, order_id="",
                message=limit_msg,
            )
        result = self._order("buy", symbol, name, quantity, price, order_type)
        if result.success:
            self._daily_ordered_amount += quantity * price
        return result

    def sell(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        logger.info("REAL ORDER: symbol=%s quantity=%d price=%s", symbol, quantity, price)
        return self._order("sell", symbol, name, quantity, price, order_type)

    def get_positions(self) -> list[Position]:
        """KIS 실전투자 계좌 잔고(보유종목) 조회."""
        try:
            token = self.kis.get_access_token()
            if not token:
                logger.warning("REAL get_positions: 토큰 없음")
                return []

            url = f"{self.kis.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": self.kis.app_key,
                "appsecret": self.kis.app_secret,
                "tr_id": "TTTC8434R",
            }
            params = {
                "CANO": self.kis.account_no,
                "ACNT_PRDT_CD": self.kis.product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }

            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "-1")
            if rt_cd != "0":
                logger.warning("REAL get_positions API 오류: %s", data.get("msg1", ""))
                return []

            items = data.get("output1", [])
            if not isinstance(items, list):
                return []

            positions = []
            for item in items:
                qty = int(item.get("hldg_qty", "0") or "0")
                if qty <= 0:
                    continue
                positions.append(
                    Position(
                        symbol=item.get("pdno", ""),
                        name=item.get("prdt_name", ""),
                        quantity=qty,
                        avg_price=float(item.get("pchs_avg_pric", "0") or "0"),
                        current_price=float(item.get("prpr", "0") or "0"),
                    )
                )

            logger.info("REAL get_positions: %d 종목", len(positions))
            return positions

        except Exception as e:
            logger.error("REAL get_positions 예외: %s", e)
            return []

    def get_balance(self) -> float:
        """KIS 실전투자 계좌 예수금(주문가능금액) 조회."""
        try:
            token = self.kis.get_access_token()
            if not token:
                logger.warning("REAL get_balance: 토큰 없음")
                return 0.0

            url = f"{self.kis.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": self.kis.app_key,
                "appsecret": self.kis.app_secret,
                "tr_id": "TTTC8908R",
            }
            params = {
                "CANO": self.kis.account_no,
                "ACNT_PRDT_CD": self.kis.product_code,
                "PDNO": "005930",  # dummy symbol required by API
                "ORD_UNPR": "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            }

            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "-1")
            if rt_cd != "0":
                logger.warning("REAL get_balance API 오류: %s", data.get("msg1", ""))
                return 0.0

            output = data.get("output", {})
            cash = float(output.get("ord_psbl_cash", "0") or "0")
            logger.info("REAL get_balance: %.0f원", cash)
            return cash

        except Exception as e:
            logger.error("REAL get_balance 예외: %s", e)
            return 0.0
