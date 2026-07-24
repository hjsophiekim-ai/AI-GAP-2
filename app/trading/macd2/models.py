"""Typed data models for MACD2.

All datetime fields are timezone-aware Asia/Seoul (KST). No network, state
file, or UI code lives here — this module only defines shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional


class Direction(str, Enum):
    UP_RED = "UP_RED"
    DOWN_BLUE = "DOWN_BLUE"
    HOLD = "HOLD"
    NOT_READY = "NOT_READY"


class SignalType(str, Enum):
    INITIAL = "INITIAL"
    REVERSAL = "REVERSAL"


class SignalState(str, Enum):
    DETECTED = "DETECTED"
    ORDER_INTENT_CREATED = "ORDER_INTENT_CREATED"
    SELL_REQUESTED = "SELL_REQUESTED"
    SELL_CONFIRMED = "SELL_CONFIRMED"
    BUY_REQUESTED = "BUY_REQUESTED"
    KIS_ACCEPTED = "KIS_ACCEPTED"
    EXECUTED = "EXECUTED"
    POSITION_CONFIRMED = "POSITION_CONFIRMED"
    LEDGER_RECORDED = "LEDGER_RECORDED"
    BLOCKED = "BLOCKED"
    WAITING = "WAITING"
    ORDER_REQUESTED = "ORDER_REQUESTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class RuntimeStatus(str, Enum):
    STOPPED = "STOPPED"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    READY = "READY"
    RUNNING = "RUNNING"
    DATA_ERROR = "DATA_ERROR"
    SIGNAL_ERROR = "SIGNAL_ERROR"
    ORDER_BLOCKED = "ORDER_BLOCKED"
    WORKER_STALLED = "WORKER_STALLED"


def _require_tz_aware(dt: datetime, field_name: str) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field_name} must be timezone-aware KST, got naive datetime: {dt!r}")
    return dt


@dataclass(frozen=True)
class MinuteBar:
    dt: datetime  # bar open time, tz-aware KST
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        _require_tz_aware(self.dt, "MinuteBar.dt")


@dataclass(frozen=True)
class ThreeMinuteBar:
    dt: datetime  # bar open time, tz-aware KST
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        _require_tz_aware(self.dt, "ThreeMinuteBar.dt")

    @property
    def close_at(self) -> datetime:
        return self.dt + timedelta(minutes=3)


@dataclass(frozen=True)
class MacdSnapshot:
    """Latest completed-3m-bar MACD state.

    ``hist_last3`` is (oldest, middle, newest) — i.e. (h2, h1, h0) in the
    docs/MACD2_LOGIC.md §6 naming where h0 is the newest completed bar.
    """

    bar_dt: datetime  # newest completed 3m bar's open time, tz-aware KST
    macd: float
    signal: float
    hist: float
    hist_last3: tuple[float, float, float]
    completed_3m_count: int
    previous_diff: Optional[float] = None
    current_diff: Optional[float] = None
    relation: str = "EQUAL"

    def __post_init__(self) -> None:
        _require_tz_aware(self.bar_dt, "MacdSnapshot.bar_dt")
        if len(self.hist_last3) != 3:
            raise ValueError(f"hist_last3 must have exactly 3 values, got {self.hist_last3!r}")


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    price: float
    fetched_at: datetime
    age_sec: Optional[float]
    source: str
    error: Optional[str] = None

    def __post_init__(self) -> None:
        _require_tz_aware(self.fetched_at, "QuoteSnapshot.fetched_at")


@dataclass(frozen=True)
class TradeSignal:
    trading_date: str  # YYYYMMDD
    completed_bar_at: str  # HHMMSS
    signal_id: str
    signal_type: SignalType
    direction: Direction
    macd: float
    signal: float
    hist_last3: tuple[float, float, float]
    detected_at: datetime

    def __post_init__(self) -> None:
        _require_tz_aware(self.detected_at, "TradeSignal.detected_at")


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: Optional[str]
    quantity: int
    avg_price: float
    entry_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.entry_at is not None:
            _require_tz_aware(self.entry_at, "PositionSnapshot.entry_at")


@dataclass(frozen=True)
class OrderIntent:
    signal_id: str
    symbol: str
    side: str  # "BUY" / "SELL"
    requested_qty: int
    client_order_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_tz_aware(self.created_at, "OrderIntent.created_at")


@dataclass(frozen=True)
class OrderResult:
    signal_id: str
    symbol: str
    side: str
    requested_qty: int
    success: bool
    order_id: Optional[str] = None
    executed_qty: int = 0
    requested_price: Optional[float] = None
    executed_price: Optional[float] = None
    broker_response: Optional[dict[str, Any]] = None


@dataclass
class RuntimeState:
    """MACD2's own runtime snapshot — never shares fields/paths with MACD v1."""

    schema_version: int = 1
    ui_mode: RuntimeStatus = RuntimeStatus.STOPPED
    auto_trade_on: bool = False
    mode: str = "mock"
    budget: float = 10_000_000.0
    stopped: bool = True
    stopped_reason: Optional[str] = None
    session_date: Optional[str] = None
    warmup_ready: bool = False
    last_signal_direction: Optional[Direction] = None
    last_detected_direction: Optional[Direction] = None
    last_executed_direction: Optional[Direction] = None
    current_episode_direction: Optional[Direction] = None
    last_signal_bar_ts: Optional[str] = None
    last_evaluated_bar_ts: Optional[str] = None
    processed_signal_ids: list[str] = field(default_factory=list)
    pending_signal: Optional[dict[str, Any]] = None
    position: Optional[PositionSnapshot] = None
    peak_net_return: float = 0.0
    profit_lock_active: bool = False
    order_block_reason: Optional[str] = None
    position_reconcile_diag: dict[str, Any] = field(default_factory=dict)
    last_position_reconcile_at: Optional[str] = None
    strategy_name: str = "MACD2"
    strategy_version: str = ""
    signal_rule: str = ""
    session_started_at: Optional[str] = None
    session_baseline_bar_ts: Optional[str] = None
    baseline_relation: Optional[str] = None
    worker_instance_id: Optional[str] = None
    primary_previous_diff: Optional[float] = None
    primary_current_diff: Optional[float] = None
    primary_relation: Optional[str] = None
    latest_primary_flag: Optional[Direction] = None
    latest_primary_signal_id: Optional[str] = None
    signed_b_shadow_direction: Optional[Direction] = None
    signed_b_shadow_hist_last3: tuple[float, float, float] = field(default_factory=tuple)
    updated_at: Optional[str] = None
