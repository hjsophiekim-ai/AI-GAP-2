"""Typed data models for TSLA_AUTO.

All datetime fields are timezone-aware (America/New_York internally; dual
ET/KST display is computed at read time via market_session.dual_timezone_iso,
never stored twice). No network, state file, or UI code lives here — this
module only defines shapes. Structure mirrors app/trading/macd2/models.py
(docs/TSLA_AUTO_COPY_MAP.md — COPY_AND_RENAME) but is never imported from it.
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


class SignalState(str, Enum):
    DETECTED = "DETECTED"
    BUY_REQUESTED = "BUY_REQUESTED"
    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"
    WAITING = "WAITING"
    ORDER_REQUESTED = "ORDER_REQUESTED"
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


class MarketRegime(str, Enum):
    NORMAL = "NORMAL"
    CHOP = "CHOP"
    UNKNOWN = "UNKNOWN"


def _require_tz_aware(dt: datetime, field_name: str) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field_name} must be timezone-aware, got naive datetime: {dt!r}")
    return dt


@dataclass(frozen=True)
class MacdSnapshot:
    """Latest completed-3m-bar MACD state (ET bar_dt). ``hist_last3`` is
    (oldest, middle, newest)."""

    bar_dt: datetime  # newest completed 3m bar's open time, tz-aware ET
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
class ConfirmedMacdFlag:
    """MACD color state and order-authoritative confirmed crossover flag."""

    bar_dt: datetime
    raw_color: Direction
    previous_raw_color: Direction
    confirmed_flag: Direction
    published_signal_id: Optional[str]
    previous_macd: Optional[float]
    previous_signal: Optional[float]
    macd: float
    signal: float
    hist: float

    def __post_init__(self) -> None:
        _require_tz_aware(self.bar_dt, "ConfirmedMacdFlag.bar_dt")


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
class PositionSnapshot:
    symbol: Optional[str]
    quantity: int
    avg_price: float
    entry_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.entry_at is not None:
            _require_tz_aware(self.entry_at, "PositionSnapshot.entry_at")


@dataclass(frozen=True)
class StrongFlagDecision:
    """Result of the optional Hybrid strong-flag order gate (filter only)."""

    approved: bool
    score: float
    required_score: float
    decision: str
    reasons: tuple[str, ...]
    component_scores: dict[str, float]
    metrics: dict[str, Any]
    is_reversal: bool
    fast_reversal: bool
    regime: str = "UNKNOWN"
    block_reason: Optional[str] = None


@dataclass
class RuntimeState:
    """TSLA_AUTO's own runtime snapshot — never shares fields/paths with
    MACD2/MACD v1/Enhanced (docs §3)."""

    schema_version: int = 1
    strategy_id: str = "TSLA_AUTO"
    ui_mode: RuntimeStatus = RuntimeStatus.STOPPED
    auto_trade_on: bool = False
    mode: str = "READ_ONLY"  # READ_ONLY | MOCK | REAL
    budget_usd: float = 10_000.0
    stopped: bool = True
    stopped_reason: Optional[str] = None
    session_date: Optional[str] = None  # ET trading date YYYYMMDD
    warmup_ready: bool = False

    last_signal_direction: Optional[Direction] = None
    last_detected_direction: Optional[Direction] = None
    last_executed_direction: Optional[Direction] = None
    current_episode_direction: Optional[Direction] = None
    last_evaluated_bar_ts: Optional[str] = None
    last_confirmed_bar_ts: Optional[str] = None
    processed_signal_ids: list[str] = field(default_factory=list)
    pending_signal: Optional[dict[str, Any]] = None
    position: Optional[PositionSnapshot] = None
    account_holding_qty: int = 0
    strategy_owned_qty: int = 0
    strategy_average_price: float = 0.0
    strategy_order_ids: list[str] = field(default_factory=list)
    peak_net_return: float = 0.0
    profit_lock_active: bool = False
    order_block_reason: Optional[str] = None
    position_reconcile_diag: dict[str, Any] = field(default_factory=dict)
    last_position_reconcile_at: Optional[str] = None
    market_session_state: dict[str, Any] = field(default_factory=dict)
    liquidation_status: dict[str, Any] = field(default_factory=dict)

    strategy_name: str = "TSLA_AUTO"
    strategy_version: str = ""
    signal_rule: str = ""
    worker_code_sha: Optional[str] = None
    session_started_at: Optional[str] = None
    session_baseline_bar_ts: Optional[str] = None
    worker_instance_id: Optional[str] = None

    primary_previous_diff: Optional[float] = None
    primary_current_diff: Optional[float] = None
    primary_relation: Optional[str] = None
    latest_primary_flag: Optional[Direction] = None
    latest_primary_signal_id: Optional[str] = None

    # ── Provisional/candidate — shadow display ONLY, never order authority
    # (docs §8 — TSLA_AUTO is designed from day one so this can never regain
    # order authority the way MACD2's did on 2026-07-31). ───────────────────
    provisional_bar_start: Optional[str] = None
    provisional_bar_end: Optional[str] = None
    provisional_macd: Optional[float] = None
    provisional_signal: Optional[float] = None
    provisional_diff: Optional[float] = None
    provisional_flag: Optional[Direction] = None
    provisional_signal_id: Optional[str] = None

    updated_at: Optional[str] = None

    # ── Broker order result (most recent, any leg) ─────────────────────────
    last_broker_order_id: Optional[str] = None
    last_broker_order_result: Optional[str] = None
    last_broker_order_symbol: Optional[str] = None
    last_broker_order_side: Optional[str] = None
    last_broker_order_at: Optional[str] = None
    last_duplicate_signal_id: Optional[str] = None

    # ── Order sizing diagnostics (most recent entry/switch attempt) ────────
    last_order_available_usd: Optional[float] = None
    last_order_usable_usd: Optional[float] = None
    last_order_bid1: Optional[float] = None
    last_order_ask1: Optional[float] = None
    last_order_order_price: Optional[float] = None
    last_order_budget_qty: Optional[int] = None
    last_order_available_qty: Optional[int] = None
    last_order_final_qty: Optional[int] = None
    last_order_expected_notional_usd: Optional[float] = None
    last_order_expected_fee_usd: Optional[float] = None
    last_order_rt_cd: Optional[str] = None
    last_order_msg_cd: Optional[str] = None
    last_order_msg1: Optional[str] = None
    last_order_failure_stage: Optional[str] = None
    last_order_filled_qty: Optional[int] = None
    last_order_fill_poll_result: Optional[str] = None
    last_order_balance_qty: Optional[int] = None

    # ── 1m/3m history freshness display ─────────────────────────────────────
    today_1m_bar_count: Optional[int] = None
    history_newest_at: Optional[str] = None
    last_completed_3m_bar_at: Optional[str] = None

    # ── QUOTE_STALE recovery diagnostics ────────────────────────────────────
    last_quote_stale_signal_id: Optional[str] = None
    last_quote_stale_retry_count: Optional[int] = None
    last_quote_stale_result: Optional[str] = None

    # ── Optional Hybrid strong-flag filter (order authority gate only) ─────
    strong_filter_enabled: bool = False
    strong_filter_enabled_at: Optional[str] = None
    strong_filter_enabled_by: Optional[str] = None
    strong_filter_version: str = ""
    daily_entry_count: int = 0
    last_entry_at: Optional[str] = None
    last_exit_at: Optional[str] = None
    last_exit_direction: Optional[Direction] = None
    last_score: Optional[float] = None
    last_required_score: Optional[float] = None
    last_approved: Optional[bool] = None
    last_decision: Optional[str] = None
    last_block_reason: Optional[str] = None
    last_is_reversal: Optional[bool] = None
    last_fast_reversal: Optional[bool] = None
    last_component_scores: Optional[dict[str, Any]] = None
    last_metrics: Optional[dict[str, Any]] = None
    last_signal_id: Optional[str] = None

    # ── Market regime (docs §10/§11 — NORMAL/CHOP/UNKNOWN, daily targets) ──
    market_regime: str = "UNKNOWN"

    # ── (신규) Stop-loss re-entry cooldown (docs §12 — TSLA_AUTO only) ──────
    last_stop_loss_exit_at: Optional[str] = None
    stop_loss_cooldown_direction: Optional[Direction] = None
    stop_loss_reentry_override_used_today: bool = False

    # ── Optional Quick-Profit take-profit exit (MACD2 parity, 2026-08-04) ───
    quick_profit_enabled: bool = False
    quick_profit_enabled_at: Optional[str] = None
    quick_profit_enabled_by: Optional[str] = None
    quick_profit_minute_symbol: Optional[str] = None
    quick_profit_minute_bucket: Optional[str] = None
    quick_profit_minute_high: Optional[float] = None

    def __post_init__(self) -> None:
        pass
