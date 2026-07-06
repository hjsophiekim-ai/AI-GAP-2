# app/models/__init__.py
# app/models.py와 app/models/ 패키지 충돌 해소:
# Python은 패키지(디렉터리)를 파일보다 우선시하므로
# app/models.py의 모든 클래스를 여기서 직접 re-export합니다.
#
# broker_base.py, order_manager.py 등에서
# `from app.models import OrderResult, Position` 형태로 사용합니다.

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class StockData:
    symbol: str
    name: str
    market: str = ""
    previous_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    current_price: float = 0.0
    volume: int = 0
    trade_value: float = 0.0
    change_rate: float = 0.0
    gap_rate: float = 0.0
    sector: str = ""
    is_etf: bool = False
    is_etn: bool = False
    is_preferred: bool = False
    is_spac: bool = False
    is_reit: bool = False
    is_warning: bool = False
    is_halt: bool = False
    source: str = ""
    date: str = ""
    time: str = ""

    def gap_rate_calc(self) -> float:
        if self.previous_close and self.open:
            return (self.open - self.previous_close) / self.previous_close * 100
        return self.gap_rate

    def open_to_current_rate(self) -> float:
        if self.open:
            return (self.current_price - self.open) / self.open * 100
        return 0.0

    def high_from_open_rate(self) -> float:
        if self.open:
            return (self.high - self.open) / self.open * 100
        return 0.0


@dataclass
class StockFeatures:
    symbol: str
    name: str
    date: str = ""
    gap_rate: float = 0.0
    open_to_current_rate: float = 0.0
    high_from_open_rate: float = 0.0
    low_from_open_rate: float = 0.0
    current_from_high_rate: float = 0.0
    trade_value_score: float = 0.0
    volume_score: float = 0.0
    gap_score: float = 0.0
    price_strength_score: float = 0.0
    high_break_score: float = 0.0
    volatility_score: float = 0.0
    liquidity_score: float = 0.0
    risk_penalty: float = 0.0
    total_rule_score: float = 0.0
    ma5: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    ma120: Optional[float] = None
    close_above_ma20: Optional[int] = None
    close_above_ma60: Optional[int] = None
    ma20_slope: Optional[float] = None
    ma60_slope: Optional[float] = None
    volume_ratio_5d: Optional[float] = None
    volume_ratio_20d: Optional[float] = None
    trade_value_ratio_20d: Optional[float] = None
    day3_return: Optional[float] = None
    day5_return: Optional[float] = None
    day20_return: Optional[float] = None
    week52_high_ratio: Optional[float] = None
    recent_high_breakout: Optional[int] = None
    recent_volatility: Optional[float] = None


@dataclass
class StockLabel:
    symbol: str
    date: str = ""
    label_profit_3pct: int = 0
    label_profit_5pct: int = 0
    label_no_stop: int = 0
    label_good_trade: int = 0


@dataclass
class Candidate:
    rank: int
    symbol: str
    name: str
    current_price: float
    open: float
    high: float
    low: float
    previous_close: float
    gap_rate: float
    open_to_current_rate: float
    trade_value: float
    ml_score: float = 0.0
    rule_score: float = 0.0
    final_score: float = 0.0
    selected_reason: str = ""
    risk_comment: str = ""
    exclude_reason: str = ""
    theme: str = ""
    matched_themes: str = ""
    quality_bonus: float = 0.0
    momentum_bonus: float = 0.0
    ma_bonus: float = 0.0
    theme_leader_bonus: float = 0.0
    risk_penalty_q: float = 0.0
    liquidity_penalty: float = 0.0
    overheat_penalty: float = 0.0
    warning_reason: str = ""
    penalty_reason: str = ""
    fallback_included: bool = False
    hard_excluded: bool = False
    relaxed_mode_applied: bool = False


@dataclass
class BuyPlan:
    rank: int
    symbol: str
    name: str
    current_price: float
    allocated_quantity: int
    allocated_amount: float
    remaining_budget_after: float
    allocation_round: int
    allocation_status: str


@dataclass
class OrderResult:
    success: bool
    mode: str
    account_type: str
    symbol: str
    name: str
    side: str
    quantity: int
    price: float
    order_type: str
    order_id: str
    message: str
    raw: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    http_status: int = 0
    error_type: str = ""
    excluded_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "mode": self.mode,
            "account_type": self.account_type,
            "symbol": self.symbol,
            "name": self.name,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "order_type": self.order_type,
            "order_id": self.order_id,
            "message": self.message,
            "timestamp": self.timestamp,
            "http_status": self.http_status,
            "error_type": self.error_type,
            "excluded_reason": self.excluded_reason,
        }


@dataclass
class Position:
    symbol: str
    name: str
    quantity: int
    avg_price: float
    current_price: float = 0.0
    buy_order_id: str = ""
    opened_at: str = ""

    @property
    def profit_rate(self) -> float:
        if self.avg_price:
            return (self.current_price - self.avg_price) / self.avg_price * 100
        return 0.0

    @property
    def profit_amount(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity

    @property
    def market_value(self) -> float:
        return self.current_price * self.quantity

    @property
    def cost(self) -> float:
        return self.avg_price * self.quantity


__all__ = [
    "StockData",
    "StockFeatures",
    "StockLabel",
    "Candidate",
    "BuyPlan",
    "OrderResult",
    "Position",
]
