from datetime import datetime
from typing import Optional

from app.logger import logger
from app.config import get_config
from app.models import StockData
from app.data.naver_volume_spike_collector import collect_volume_spike_stocks
from app.data.kis_market_data import KISMarketData
from app.data.sample_data import generate_sample_gap_stocks


def is_market_open() -> bool:
    """Returns True if current time is between 09:00 and 15:30."""
    now = datetime.now().time()
    market_open = datetime.strptime("09:00", "%H:%M").time()
    market_close = datetime.strptime("15:30", "%H:%M").time()
    return market_open <= now <= market_close


def is_pre_market() -> bool:
    """Returns True if current time is before 09:00."""
    now = datetime.now().time()
    market_open = datetime.strptime("09:00", "%H:%M").time()
    return now < market_open


class DataCollector:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()
        self._kis: Optional[KISMarketData] = None

        kis_cfg = self.cfg._raw.get("kis", {})
        mock_section = kis_cfg.get("mock", {})
        import os
        has_kis = (
            os.getenv(mock_section.get("app_key_env", ""), "")
            and os.getenv(mock_section.get("app_secret_env", ""), "")
            and os.getenv(mock_section.get("account_no_env", ""), "")
        )
        if has_kis:
            try:
                import os
                app_key = os.getenv(mock_section.get("app_key_env", ""), "")
                app_secret = os.getenv(mock_section.get("app_secret_env", ""), "")
                account_no = os.getenv(mock_section.get("account_no_env", ""), "")
                product_code = os.getenv(mock_section.get("product_code_env", ""), "01")
                self._kis = KISMarketData(
                    app_key=app_key,
                    app_secret=app_secret,
                    account_no=account_no,
                    product_code=product_code,
                    use_paper=True,
                )
            except Exception as e:
                logger.warning(f"KISMarketData 초기화 실패: {e}")
                self._kis = None

    def _get_primary_source(self) -> str:
        """Returns 'naver' if before market open (09:00) or no KIS config.
        Returns 'kis' if after market open and KIS configured."""
        if self._kis is None:
            return "naver"
        if is_pre_market():
            return "naver"
        return "kis"

    def collect_gap_candidates(self, date_str: str = None) -> dict:
        """
        갭상승 후보 종목 수집.

        Returns
        -------
        dict
            {
              "candidates": list[StockData],
              "source": str,          # "naver" | "kis" | "sample"
              "is_sample": bool,
            }
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        raw_list: list = []
        used_source: str = "unknown"
        is_sample = False

        # ── 1차: Naver 거래량급증 (sise_quant_high.naver) ──────────────
        try:
            raw_list = collect_volume_spike_stocks(max_pages=3, max_stocks=80)
            if raw_list and len(raw_list) >= 5:
                used_source = "naver_volume_spike"
                logger.info(f"[DataCollector] 거래량급증에서 {len(raw_list)}개 수집")
            else:
                logger.warning(f"[DataCollector] 거래량급증 결과 부족({len(raw_list)}개)")
                raw_list = []
        except Exception as e:
            logger.warning(f"[DataCollector] 거래량급증 수집 오류: {e}")
            raw_list = []

        # ── 2차: KIS (있을 경우) ────────────────────────────────────────
        if not raw_list and self._kis is not None:
            try:
                raw_list = self._kis.get_gap_candidates(date_str=date_str)
                if raw_list and len(raw_list) >= 5:
                    used_source = "kis"
                    logger.info(f"[DataCollector] KIS에서 {len(raw_list)}개 수집")
                else:
                    raw_list = []
            except Exception as e:
                logger.warning(f"[DataCollector] KIS 수집 오류: {e}")
                raw_list = []

        # ── 3차: 샘플 데이터 fallback ───────────────────────────────────
        if not raw_list:
            logger.warning("[DataCollector] 실제 데이터 없음 → 샘플 데이터 사용")
            raw_list = generate_sample_gap_stocks()
            used_source = "sample"
            is_sample = True
            logger.info(f"[DataCollector] 샘플 {len(raw_list)}개 로드")

        # ── StockData 변환 ──────────────────────────────────────────────
        result: list[StockData] = []
        for raw in raw_list:
            if isinstance(raw, StockData):
                result.append(raw)
                continue
            stock = self._to_stock_data(raw)
            if stock is not None:
                result.append(stock)

        logger.info(f"[DataCollector] 최종 {len(result)}개 (소스: {used_source})")
        return {"candidates": result, "source": used_source, "is_sample": is_sample}

    def _to_stock_data(self, raw: dict) -> Optional[StockData]:
        """Converts raw dict to StockData.
        Returns None if symbol or current_price is missing."""
        if not raw:
            return None

        symbol = raw.get("symbol") or raw.get("code") or raw.get("ticker")
        if not symbol:
            return None

        current_price = raw.get("current_price") or raw.get("price") or raw.get("close")
        if not current_price:
            return None

        try:
            current_price = float(current_price)
        except (TypeError, ValueError):
            return None

        def _float(key, alt_keys=None, default=0.0):
            val = raw.get(key)
            if val is None and alt_keys:
                for ak in alt_keys:
                    val = raw.get(ak)
                    if val is not None:
                        break
            try:
                return float(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        def _int(key, alt_keys=None, default=0):
            val = raw.get(key)
            if val is None and alt_keys:
                for ak in alt_keys:
                    val = raw.get(ak)
                    if val is not None:
                        break
            try:
                return int(float(val)) if val is not None else default
            except (TypeError, ValueError):
                return default

        def _bool(key, default=False):
            val = raw.get(key)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes")
            return bool(val)

        return StockData(
            symbol=str(symbol),
            name=str(raw.get("name", "")),
            market=str(raw.get("market", "")),
            previous_close=_float("previous_close", ["prev_close", "yesterday_close"]),
            open=_float("open", ["open_price"]),
            high=_float("high", ["high_price"]),
            low=_float("low", ["low_price"]),
            current_price=current_price,
            volume=_int("volume"),
            trade_value=_float("trade_value", ["trading_value", "amount"]),
            change_rate=_float("change_rate", ["chg_rate", "rate"]),
            gap_rate=_float("gap_rate", ["gap"]),
            sector=str(raw.get("sector", "")),
            is_etf=_bool("is_etf"),
            is_etn=_bool("is_etn"),
            is_preferred=_bool("is_preferred"),
            is_spac=_bool("is_spac"),
            is_reit=_bool("is_reit"),
            is_warning=_bool("is_warning"),
            is_halt=_bool("is_halt"),
            source=str(raw.get("source", "")),
            date=str(raw.get("date", "")),
            time=str(raw.get("time", "")),
        )

    def refresh_prices(self, symbols: list) -> dict:
        """Gets current prices for a list of symbols.
        Returns {symbol: price} dict.
        Uses KIS if available, else Naver, else returns empty dict.
        """
        if not symbols:
            return {}

        prices = {}

        # Try KIS first if available
        if self._kis is not None:
            try:
                prices = self._kis.get_current_prices(symbols)
                if prices:
                    logger.debug(f"[DataCollector] KIS 현재가 조회: {len(prices)}개")
                    return prices
            except Exception as e:
                logger.warning(f"[DataCollector] KIS 현재가 조회 실패: {e} → Naver 시도")

        # Naver 현재가 조회는 별도 구현 시 추가 (현재는 KIS 전용)


        logger.warning(f"[DataCollector] 현재가 조회 전체 실패 — 빈 dict 반환")
        return {}
