"""
MockBroker - KIS 모의투자(paper trading) 계좌를 사용하는 브로커.
"""

import requests
from datetime import datetime

from app.trading.broker_base import BrokerBase
from app.models import OrderResult, Position
from app.logger import logger


class MockBroker(BrokerBase):
    """KIS 모의투자 계좌를 사용하는 브로커."""

    mode = "mock"

    def __init__(self, kis_client):
        self.kis = kis_client

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
        """Place a buy or sell order on the KIS paper account."""
        # tr_id differs by side (모의투자 코드)
        # 매수: VTTC0802U  매도: VTTC0801U
        tr_id = "VTTC0802U" if side == "buy" else "VTTC0801U"

        # ord_dvsn: 00=지정가, 01=시장가
        ord_dvsn = "00" if order_type == "limit" else "01"
        ord_price = str(int(price)) if order_type == "limit" else "0"

        token = self.kis.get_access_token()
        if not token:
            return OrderResult(
                success=False,
                mode=self.mode,
                account_type="mock",
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
                    "MOCK %s 주문 실패 symbol=%s rt_cd=%s msg=%s",
                    side, symbol, rt_cd, msg,
                )
            else:
                logger.info(
                    "MOCK %s 주문 성공 symbol=%s quantity=%d price=%s order_id=%s",
                    side, symbol, quantity, ord_price, order_id,
                )

            return OrderResult(
                success=success,
                mode=self.mode,
                account_type="mock",
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
            logger.error("MOCK %s 주문 예외 symbol=%s: %s", side, symbol, e)
            return OrderResult(
                success=False,
                mode=self.mode,
                account_type="mock",
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

    def buy(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        return self._order("buy", symbol, name, quantity, price, order_type)

    def sell(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        return self._order("sell", symbol, name, quantity, price, order_type)

    def get_positions(self) -> list[Position]:
        """KIS 모의투자 계좌 잔고(보유종목) 조회."""
        try:
            token = self.kis.get_access_token()
            if not token:
                logger.warning("MOCK get_positions: 토큰 없음")
                return []

            url = f"{self.kis.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": self.kis.app_key,
                "appsecret": self.kis.app_secret,
                "tr_id": "VTTC8434R",
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
                logger.warning("MOCK get_positions API 오류: %s", data.get("msg1", ""))
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

            logger.info("MOCK get_positions: %d 종목", len(positions))
            return positions

        except Exception as e:
            logger.error("MOCK get_positions 예외: %s", e)
            return []

    def get_current_price(self, symbol: str) -> float | None:
        """현재가 조회."""
        try:
            result = self.kis.get_current_price(symbol)
            return result["current_price"] if result else None
        except Exception as e:
            logger.warning("MOCK get_current_price 예외 %s: %s", symbol, e)
            return None

    def get_buyable_cash(self) -> float:
        """주문 가능 현금 조회."""
        return self.kis.get_buyable_cash()

    def get_balance(self) -> float:
        """KIS 모의투자 계좌 예수금(주문가능금액) 조회."""
        try:
            token = self.kis.get_access_token()
            if not token:
                logger.warning("MOCK get_balance: 토큰 없음")
                return 0.0

            url = f"{self.kis.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": self.kis.app_key,
                "appsecret": self.kis.app_secret,
                "tr_id": "VTTC8908R",
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
                logger.warning("MOCK get_balance API 오류: %s", data.get("msg1", ""))
                return 0.0

            output = data.get("output", {})
            cash = float(output.get("ord_psbl_cash", "0") or "0")
            logger.info("MOCK get_balance: %.0f원", cash)
            return cash

        except Exception as e:
            logger.error("MOCK get_balance 예외: %s", e)
            return 0.0
