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


@dataclass(frozen=True)
class MajorFlagDecision:
    """Result of the optional Hybrid MAJOR_FLAG order gate (filter only)."""

    approved: bool
    score: float
    required_score: float
    decision: str
    reasons: tuple[str, ...]
    component_scores: dict[str, float]
    metrics: dict[str, Any]
    is_reversal: bool
    fast_reversal: bool
    block_reason: Optional[str] = None


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
    # 2026-08-05 fix: initialize_strategy_session sets this when a same-day
    # restart is detected with NO persisted last_confirmed_bar_ts (state.json
    # was lost, e.g. a Render redeploy/disk hiccup) — a toggle the user set
    # earlier today (major_filter_enabled/sideways_filter_enabled/
    # quick_profit_enabled/profit_lock_enabled) may have silently reverted to
    # its config default at that moment, since a lost toggle preference can
    # never be reconstructed from market data (unlike signal history). UI
    # shows a prominent warning while this is set; cleared on day rollover.
    possible_toggle_reset_at: Optional[str] = None
    # 2026-08-04 fix: last time get_snapshot() auto-restarted a WORKER_STALLED
    # worker (rate-limits recovery attempts — see config.WORKER_AUTO_RECOVER_COOLDOWN_SEC).
    last_auto_recover_attempt_at: Optional[str] = None
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

    # ── Optional Hybrid MAJOR_FLAG filter (order authority gate only) ──────
    major_filter_enabled: bool = False
    major_filter_enabled_at: Optional[str] = None
    major_filter_enabled_by: Optional[str] = None
    major_filter_version: str = ""
    daily_major_entry_count: int = 0
    last_major_entry_at: Optional[str] = None
    last_major_exit_at: Optional[str] = None
    last_major_exit_direction: Optional[Direction] = None
    last_major_score: Optional[float] = None
    last_major_required_score: Optional[float] = None
    last_major_approved: Optional[bool] = None
    last_major_decision: Optional[str] = None
    last_major_block_reason: Optional[str] = None
    last_major_is_reversal: Optional[bool] = None
    last_major_fast_reversal: Optional[bool] = None
    last_major_component_scores: Optional[dict[str, Any]] = None
    last_major_metrics: Optional[dict[str, Any]] = None
    last_major_signal_id: Optional[str] = None

    # ── Optional 추세전환장(sideways/whipsaw) entry filter (order authority
    # gate only) — mutually exclusive with MAJOR_FLAG: when this is ON it
    # takes priority over major_filter_enabled (see worker._judge_entry_gate).
    sideways_filter_enabled: bool = False
    sideways_filter_enabled_at: Optional[str] = None
    sideways_filter_enabled_by: Optional[str] = None
    sideways_filter_version: str = ""
    daily_sideways_entry_count: int = 0
    last_sideways_entry_at: Optional[str] = None
    last_sideways_score: Optional[float] = None
    last_sideways_required_score: Optional[float] = None
    last_sideways_approved: Optional[bool] = None
    last_sideways_decision: Optional[str] = None
    last_sideways_block_reason: Optional[str] = None
    last_sideways_component_scores: Optional[dict[str, Any]] = None
    last_sideways_metrics: Optional[dict[str, Any]] = None
    last_sideways_signal_id: Optional[str] = None

    # ── Optional Quick-Profit take-profit filter (EXIT LOGIC ONLY) —
    # independent of major_filter_enabled/sideways_filter_enabled; never
    # affects which entries are placed, only exits an already-held position.
    # 2026-08-05: judged directly off each tick's live quote (no remembered
    # "1분 고점" state needed any more — see worker.py's exit-check block).
    quick_profit_enabled: bool = False
    quick_profit_enabled_at: Optional[str] = None
    quick_profit_enabled_by: Optional[str] = None

    # ── Stop Loss 3-minute completed-bar gating (docs 2026-08-02 Exit Rule:
    # 3-Minute Confirmed Bars) — no real ETF 1분봉 feed exists (market_data.py
    # only tracks WATCH_SYMBOL history), so the traded ETF's own completed
    # 3-minute bar close is approximated from the live quote stream.
    stop_loss_bar_symbol: Optional[str] = None
    stop_loss_entry_bar_ts: Optional[str] = None
    stop_loss_bar_ts: Optional[str] = None
    stop_loss_bar_close: Optional[float] = None

    # ── Profit Lock — MACD convergence early exit (2026-08-05: replaces the
    # old net-return-giveback Profit Lock — EXIT LOGIC ONLY, mutually
    # exclusive with quick_profit_enabled). Evaluated once per newly-completed
    # WATCH_SYMBOL(000660) 3-minute bar while a position is held, off the SAME
    # confirmed MACD/Signal already computed for flag generation — never a
    # second MACD calculation, never the forming bar. See config.py's
    # PROFIT_LOCK_* constants for the 5 exit conditions.
    profit_lock_enabled: bool = False
    profit_lock_enabled_at: Optional[str] = None
    profit_lock_enabled_by: Optional[str] = None
    profit_lock_symbol: Optional[str] = None
    profit_lock_entry_bar_ts: Optional[str] = None
    profit_lock_last_bar_ts: Optional[str] = None
    profit_lock_bars_since_entry: int = 0
    profit_lock_gap_history: list[float] = field(default_factory=list)
    profit_lock_peak_return_pct: float = 0.0
    profit_lock_current_support_gap: Optional[float] = None
    profit_lock_max_support_gap: Optional[float] = None
    profit_lock_gap_ratio: Optional[float] = None
    profit_lock_contraction_count: int = 0
    profit_lock_drawdown_pct: float = 0.0

    # ── 09:03 예약 매수 (2026-08-06) — 개장 직후 데이터 부족으로 이른 시간대
    # MACD 플래그를 놓치기 쉬운 문제 대응: 미리 방향을 예약해두면 오늘
    # config.SCHEDULED_ENTRY_TIME(09:03)에 worker.run_once가 자동으로 지정
    # 방향 ETF를 전량매수한다 (하루 1회, 체결 후에는 기존 손절/반대플래그청산/
    # 프로핏락/퀵프로핏 로직이 그대로 관리 — manual_entry와 동일한 경로).
    # armed_direction/executed_at은 매일 자정 이후 첫 tick에서
    # _apply_day_rollover가 초기화하므로 매일 다시 예약해야 한다.
    scheduled_entry_armed_direction: Optional[Direction] = None
    scheduled_entry_armed_at: Optional[str] = None
    scheduled_entry_armed_by: Optional[str] = None
    scheduled_entry_executed_at: Optional[str] = None
    scheduled_entry_last_result: Optional[str] = None
    # 2026-08-07 (사용자 요청): True from the moment the scheduled entry
    # actually fills until config.SCHEDULED_ENTRY_PROTECTION_UNTIL (09:10) --
    # while True, a confirmed OPPOSITE flag is caught/logged but does not
    # sell/switch the held position (STOP_LOSS/PROFIT_LOCK/QUICK_PROFIT/
    # forced liquidation are unaffected). Reset to False on any position
    # close/switch and on day rollover.
    scheduled_entry_protected: bool = False
