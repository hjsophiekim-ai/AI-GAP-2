import re
from app.models import StockData
from app.config import get_config
from app.logger import logger


ETF_ETN_KEYWORDS = [
    "KODEX", "TIGER", "ACE", "SOL", "PLUS", "KBSTAR", "KOSEF", "HANARO", "ARIRANG",
    "ETN", "ETF", "레버리지", "인버스", "선물", "합성", "TR", "RISE", "FOCUS", "TREX",
    "TIMEFOLIO", "WOORI",
]

PREFERRED_PATTERN = re.compile(r'(?:우B?|[0-9]+우B?)$')


class StockFilter:
    def __init__(self, cfg=None):
        self.cfg = cfg if cfg is not None else get_config()

    def filter_stocks(self, stocks: list[StockData]) -> tuple[list[StockData], list[dict]]:
        passed = []
        excluded = []
        for stock in stocks:
            reason = self._apply_single(stock)
            if reason is None:
                passed.append(stock)
            else:
                logger.info(f"[필터제외] {stock.symbol} {stock.name} - {reason}")
                excluded.append({"symbol": stock.symbol, "name": stock.name, "reason": reason})
        return passed, excluded

    def is_etf_etn(self, stock: StockData) -> bool:
        if stock.is_etf or stock.is_etn:
            return True
        name_upper = stock.name.upper()
        for kw in ETF_ETN_KEYWORDS:
            if kw.upper() in name_upper:
                return True
        return False

    def is_preferred_stock(self, stock: StockData) -> bool:
        if stock.is_preferred:
            return True
        return bool(PREFERRED_PATTERN.search(stock.name))

    def is_spac(self, stock: StockData) -> bool:
        if stock.is_spac:
            return True
        if "스팩" in stock.name:
            return True
        if re.search(r'[0-9]+호스팩', stock.name):
            return True
        return False

    def is_reit(self, stock: StockData) -> bool:
        if stock.is_reit:
            return True
        if "리츠" in stock.name:
            return True
        if "reits" in stock.name.lower():
            return True
        return False

    def is_warning(self, stock: StockData) -> bool:
        return stock.is_warning or stock.is_halt

    def has_sufficient_trade_value(self, stock: StockData) -> bool:
        min_tv = self.cfg.trading.get("min_trade_value", 3_000_000_000)
        return stock.trade_value >= min_tv

    def has_valid_price(self, stock: StockData) -> bool:
        min_price = self.cfg.filters.get("min_price", 1000)
        return stock.current_price >= min_price

    def has_valid_gap_rate(self, stock: StockData) -> bool:
        min_gap = self.cfg.trading.get("min_gap_rate", 2.0)
        max_gap = self.cfg.trading.get("max_gap_rate", 25.0)
        gap = stock.gap_rate
        if gap == 0 and stock.previous_close > 0 and stock.open > 0:
            gap = (stock.open - stock.previous_close) / stock.previous_close * 100
        return min_gap <= gap <= max_gap

    def _apply_single(self, stock: StockData) -> str | None:
        filters_cfg = self.cfg.filters

        if filters_cfg.get("exclude_etf", True) and self.is_etf_etn(stock):
            return "ETF/ETN"

        if filters_cfg.get("exclude_etn", True) and stock.is_etn:
            return "ETN"

        if filters_cfg.get("exclude_preferred_stock", True) and self.is_preferred_stock(stock):
            return "우선주"

        if filters_cfg.get("exclude_spac", True) and self.is_spac(stock):
            return "스팩"

        if filters_cfg.get("exclude_reit", True) and self.is_reit(stock):
            return "리츠"

        if filters_cfg.get("exclude_warning_stock", True) and self.is_warning(stock):
            return "투자경고/거래정지"

        if filters_cfg.get("exclude_halt", True) and stock.is_halt:
            return "거래정지"

        if not self.has_valid_price(stock):
            min_price = self.cfg.filters.get("min_price", 1000)
            return f"가격미달({stock.current_price} < {min_price})"

        if not self.has_sufficient_trade_value(stock):
            min_tv = self.cfg.trading.get("min_trade_value", 3_000_000_000)
            return f"거래대금미달({stock.trade_value:,.0f} < {min_tv:,.0f})"

        if not self.has_valid_gap_rate(stock):
            min_gap = self.cfg.trading.get("min_gap_rate", 2.0)
            max_gap = self.cfg.trading.get("max_gap_rate", 25.0)
            gap = stock.gap_rate
            if gap == 0 and stock.previous_close > 0 and stock.open > 0:
                gap = (stock.open - stock.previous_close) / stock.previous_close * 100
            return f"갭율범위외({gap:.2f}%, 허용:{min_gap}~{max_gap}%)"

        return None
