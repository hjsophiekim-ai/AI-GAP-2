"""
KIS (한국투자증권) Open API interface for market data.
Works gracefully when API keys are not configured (returns empty results).
"""

import time
import requests
from typing import Optional

from app.logger import logger
from app.config import get_config


class KISMarketData:
    """Korea Investment & Securities Open API client for market data."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        account_no: str,
        product_code: str = "01",
        use_paper: bool = True,
    ):
        self.app_key = app_key or ""
        self.app_secret = app_secret or ""
        self.account_no = account_no or ""
        self.product_code = product_code or "01"
        self.use_paper = use_paper

        if use_paper:
            self.base_url = "https://openapivts.koreainvestment.com:29443"
        else:
            self.base_url = "https://openapi.koreainvestment.com:9443"

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def is_configured(self) -> bool:
        """Returns True if app_key and app_secret are non-empty."""
        return bool(self.app_key and self.app_secret)

    def get_access_token(self) -> str:
        """
        Fetch OAuth2 access token from KIS API.
        Caches token until expiry (typically 24 hours).
        POST /oauth2/tokenP
        """
        now = time.time()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        if not self.is_configured():
            logger.info("KIS API not configured, skipping")
            return ""

        url = f"{self.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        headers = {"Content-Type": "application/json"}

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            token = data.get("access_token", "")
            expires_in = int(data.get("expires_in", 86400))

            if not token:
                logger.error("KIS token response missing access_token: %s", data)
                return ""

            self._access_token = token
            # Subtract 60 seconds buffer before actual expiry
            self._token_expires_at = now + expires_in - 60
            logger.info("KIS access token obtained, expires in %ds", expires_in)
            return self._access_token

        except requests.exceptions.RequestException as e:
            logger.error("KIS get_access_token HTTP error: %s", e)
            return ""
        except Exception as e:
            logger.error("KIS get_access_token unexpected error: %s", e)
            return ""

    def _get_headers(self, tr_id: str, custtype: str = "P") -> dict:
        """Build common request headers for KIS API calls."""
        token = self.get_access_token()
        return {
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": custtype,
        }

    def get_stock_price(self, symbol: str) -> Optional[dict]:
        """
        Fetch current price info for a single stock.
        GET /uapi/domestic-stock/v1/quotations/inquire-price
        tr_id: FHKST01010100
        Returns dict with output fields or None on error.
        """
        if not self.is_configured():
            logger.info("KIS API not configured, skipping")
            return None

        token = self.get_access_token()
        if not token:
            return None

        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self._get_headers("FHKST01010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "-1")
            if rt_cd != "0":
                msg = data.get("msg1", "Unknown error")
                logger.warning("KIS get_stock_price error for %s: [%s] %s", symbol, rt_cd, msg)
                return None

            output = data.get("output", {})
            if not output:
                logger.warning("KIS get_stock_price empty output for %s", symbol)
                return None

            return output

        except requests.exceptions.RequestException as e:
            logger.error("KIS get_stock_price HTTP error for %s: %s", symbol, e)
            return None
        except Exception as e:
            logger.error("KIS get_stock_price unexpected error for %s: %s", symbol, e)
            return None

    def get_top_gainers(self, market: str = "0", count: int = 100) -> list[dict]:
        """
        Fetch top gaining stocks by fluctuation rate ranking.
        GET /uapi/domestic-stock/v1/ranking/fluctuation
        tr_id: FHPST01700000

        market: "0"=전체, "1"=코스피, "2"=코스닥
        count: number of results to return (max 100 per call)
        Returns list of stock dicts from output (may be empty on error).
        """
        if not self.is_configured():
            logger.info("KIS API not configured, skipping")
            return []

        token = self.get_access_token()
        if not token:
            return []

        url = f"{self.base_url}/uapi/domestic-stock/v1/ranking/fluctuation"
        headers = self._get_headers("FHPST01700000")
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20170",
            "fid_input_iscd": market,
            "fid_rank_sort_cls_code": "0",      # 0=상승률 기준 내림차순
            "fid_input_cnt_1": str(count),
            "fid_prc_cls_code": "0",             # 0=전일대비등락률
            "fid_input_price_1": "",
            "fid_input_price_2": "",
            "fid_vol_cnt": "",
            "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0",
            "fid_div_cls_code": "0",
            "fid_rsfl_rate1": "",
            "fid_rsfl_rate2": "",
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "-1")
            if rt_cd != "0":
                msg = data.get("msg1", "Unknown error")
                logger.warning("KIS get_top_gainers error: [%s] %s", rt_cd, msg)
                return []

            output = data.get("output", [])
            if not isinstance(output, list):
                logger.warning("KIS get_top_gainers unexpected output type: %s", type(output))
                return []

            logger.info("KIS get_top_gainers returned %d items", len(output))
            return output

        except requests.exceptions.RequestException as e:
            logger.error("KIS get_top_gainers HTTP error: %s", e)
            return []
        except Exception as e:
            logger.error("KIS get_top_gainers unexpected error: %s", e)
            return []

    def get_stock_info(self, symbol: str) -> Optional[dict]:
        """
        Fetch basic stock info (name, sector, etc.) for a symbol.
        GET /uapi/domestic-stock/v1/quotations/search-stock-info
        tr_id: CTPF1002R (모의투자) / CTPF1002R (실전)
        Returns dict or None on error.
        """
        if not self.is_configured():
            logger.info("KIS API not configured, skipping")
            return None

        token = self.get_access_token()
        if not token:
            return None

        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/search-stock-info"
        headers = self._get_headers("CTPF1002R")
        params = {
            "PRDT_TYPE_CD": "300",
            "PDNO": symbol,
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "-1")
            if rt_cd != "0":
                msg = data.get("msg1", "Unknown error")
                logger.warning("KIS get_stock_info error for %s: [%s] %s", symbol, rt_cd, msg)
                return None

            output = data.get("output", {})
            return output if output else None

        except requests.exceptions.RequestException as e:
            logger.error("KIS get_stock_info HTTP error for %s: %s", symbol, e)
            return None
        except Exception as e:
            logger.error("KIS get_stock_info unexpected error for %s: %s", symbol, e)
            return None

    def get_pre_market_price(self, symbol: str) -> Optional[dict]:
        """
        Fetch pre-market (장전) price data for gap analysis.
        GET /uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn
        tr_id: FHKST01010200
        Returns dict or None on error.
        """
        if not self.is_configured():
            logger.info("KIS API not configured, skipping")
            return None

        token = self.get_access_token()
        if not token:
            return None

        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
        headers = self._get_headers("FHKST01010200")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "-1")
            if rt_cd != "0":
                msg = data.get("msg1", "Unknown error")
                logger.warning("KIS get_pre_market_price error for %s: [%s] %s", symbol, rt_cd, msg)
                return None

            output = data.get("output1", {})
            return output if output else None

        except requests.exceptions.RequestException as e:
            logger.error("KIS get_pre_market_price HTTP error for %s: %s", symbol, e)
            return None
        except Exception as e:
            logger.error("KIS get_pre_market_price unexpected error for %s: %s", symbol, e)
            return None

    def get_daily_ohlcv(self, symbol: str, start_date: str, end_date: str, period: str = "D") -> list[dict]:
        """
        Fetch daily OHLCV (일봉) data for a stock.
        GET /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice
        tr_id: FHKST03010100

        start_date, end_date: "YYYYMMDD" format
        period: "D"=일봉, "W"=주봉, "M"=월봉
        Returns list of daily bar dicts or [] on error.
        """
        if not self.is_configured():
            logger.info("KIS API not configured, skipping")
            return []

        token = self.get_access_token()
        if not token:
            return []

        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = self._get_headers("FHKST03010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",   # 0=수정주가 반영
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "-1")
            if rt_cd != "0":
                msg = data.get("msg1", "Unknown error")
                logger.warning("KIS get_daily_ohlcv error for %s: [%s] %s", symbol, rt_cd, msg)
                return []

            output2 = data.get("output2", [])
            if not isinstance(output2, list):
                return []

            return output2

        except requests.exceptions.RequestException as e:
            logger.error("KIS get_daily_ohlcv HTTP error for %s: %s", symbol, e)
            return []
        except Exception as e:
            logger.error("KIS get_daily_ohlcv unexpected error for %s: %s", symbol, e)
            return []


class KISClient(KISMarketData):
    """
    Convenience wrapper around KISMarketData.
    Provides factory method and availability check.
    """

    @classmethod
    def from_config(cls, cfg=None) -> "KISClient":
        """Create KISClient instance from config object."""
        if cfg is None:
            cfg = get_config()

        kis = cfg.kis
        app_key = kis.get("app_key", "")
        app_secret = kis.get("app_secret", "")
        account_no = kis.get("account_no", "")
        product_code = kis.get("product_code", "01")
        use_paper = kis.get("use_paper_account", True)

        return cls(
            app_key=app_key,
            app_secret=app_secret,
            account_no=account_no,
            product_code=product_code,
            use_paper=use_paper,
        )

    def is_available(self) -> bool:
        """Returns True if configured and a token can be obtained."""
        if not self.is_configured():
            return False
        try:
            token = self.get_access_token()
            return bool(token)
        except Exception as e:
            logger.error("KIS is_available check failed: %s", e)
            return False
