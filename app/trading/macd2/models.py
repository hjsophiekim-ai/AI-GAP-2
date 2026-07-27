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
    previous_macd: Optional[float] = None
    previous_signal: Optional[float] = None

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
    provisional_bar_start: Optional[str] = None
    provisional_bar_end: Optional[str] = None
    provisional_macd: Optional[float] = None
    provisional_signal: Optional[float] = None
    provisional_diff: Optional[float] = None
    provisional_flag: Optional[Direction] = None
    provisional_signal_id: Optional[str] = None
    provisional_evaluated_at: Optional[str] = None
    provisional_input_now: Optional[str] = None
    provisional_quote_price: Optional[float] = None
    provisional_last_1m_at: Optional[str] = None
    provisional_last_1m_close: Optional[float] = None
    provisional_price_scale_note: Optional[str] = None
    provisional_detected_at: Optional[str] = None
    provisional_order_requested_at: Optional[str] = None
    provisional_ordered_bar_ts: Optional[str] = None
    signed_b_shadow_direction: Optional[Direction] = None
    signed_b_shadow_hist_last3: tuple[float, float, float] = field(default_factory=tuple)
    updated_at: Optional[str] = None

    # ── Provisional two-tick candidate gate (2026-07-27 momentary-crossing fix) ──
    candidate_flag: Optional[Direction] = None  # CANDIDATE_UP_RED/CANDIDATE_DOWN_BLUE display only — no order authority
    candidate_bar_ts: Optional[str] = None
    candidate_first_seen_at: Optional[str] = None
    candidate_first_diff: Optional[float] = None
    candidate_confirmed_at: Optional[str] = None
    candidate_confirmed_diff: Optional[float] = None

    # ── Broker order result (most recent, any leg) ─────────────────────────
    last_broker_order_id: Optional[str] = None
    last_broker_order_result: Optional[str] = None
    last_broker_order_symbol: Optional[str] = None
    last_broker_order_side: Optional[str] = None
    last_broker_order_at: Optional[str] = None

    # ── Signal-ledger duplicate-write visibility ───────────────────────────
    last_duplicate_signal_id: Optional[str] = None

    # ── Order sizing diagnostics (most recent entry/switch attempt) ────────
    last_order_orderable_cash: Optional[float] = None
    last_order_nrcvb_buy_amt: Optional[float] = None
    last_order_nrcvb_buy_qty: Optional[int] = None
    last_order_psbl_qty_calc_unpr: Optional[float] = None
    last_order_ask1: Optional[float] = None
    last_order_order_price: Optional[float] = None
    last_order_order_type: Optional[str] = None
    last_order_usable_cash: Optional[float] = None
    last_order_limit_buyable_qty: Optional[int] = None
    last_order_budget_qty: Optional[int] = None
    last_order_final_qty: Optional[int] = None
    last_order_sizing_rt_cd: Optional[str] = None
    last_order_sizing_msg_cd: Optional[str] = None
    last_order_sizing_msg1: Optional[str] = None
    last_order_sizing_price: Optional[float] = None
    last_order_requested_qty: Optional[int] = None
    last_order_expected_amount: Optional[float] = None
    last_order_failure_stage: Optional[str] = None
    last_order_filled_qty: Optional[int] = None
    last_order_fill_poll_result: Optional[str] = None
    last_order_balance_qty: Optional[int] = None

    # ── Confirmed (completed-bar) Primary crossover — order authority
    # (2026-07-27 KIS-parity fix: moved off the forming/provisional bar) ────
    last_confirmed_bar_ts: Optional[str] = None  # completed 3m bar_dt last evaluated (exactly once each)

    # ── 1m/3m history freshness display (docs 2026-07-27 §1) ───────────────
    today_1m_bar_count: Optional[int] = None
    history_newest_at: Optional[str] = None
    last_completed_3m_bar_at: Optional[str] = None

    # ── QUOTE_STALE recovery diagnostics (2026-07-27 fix) — most recent
    # confirmed-signal quote-stale retry attempt, kept for the UI, separate
    # from any restored/historical onset. ──────────────────────────────────
    last_quote_stale_signal_id: Optional[str] = None
    last_quote_stale_quote_ages: Optional[str] = None  # str(dict) — symbol -> age_sec at signal detection
    last_quote_stale_retry_count: Optional[int] = None
    last_quote_stale_result: Optional[str] = None  # "RECOVERED" / "MISSED_SIGNAL_QUOTE_STALE"
    quote_history_mismatch_reason: Optional[str] = None  # None when consistent
