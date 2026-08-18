"""MU_MACD data types.

``Direction`` and ``PositionSnapshot`` are imported directly from
app.trading.macd2.models — both are frozen/stateless value types with no
module-level mutable state, so reusing them creates a type-identity link
(needed anyway: order_executor.target_symbol_for_direction expects THIS
exact Direction enum) but no runtime data-sharing between MU_MACD and
MACD2. ``RuntimeState`` below is MU_MACD's own, separate mutable state
class — never shared with macd2.models.RuntimeState.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.trading.macd2.models import Direction, PositionSnapshot  # noqa: F401  (re-exported)

__all__ = ["Direction", "PositionSnapshot", "RuntimeState", "TickResult"]


@dataclass
class RuntimeState:
    """MU_MACD's own runtime snapshot — never shares fields/paths with
    macd2.models.RuntimeState or app.trading.tsla_auto's state."""

    schema_version: int = 1

    # ── user/UI controls ────────────────────────────────────────────────
    auto_trade_on: bool = False
    mode: str = "mock"  # "mock" | "real"
    budget: float = 0.0
    quick_profit_enabled: bool = False
    # 2026-08-14: pause NEW entries only -- MU price collection, flag
    # detection/signal-ledger recording, and existing-position management
    # (stop loss/quick profit/forced liquidation/reconcile) all keep running
    # unaffected; see config.BLOCK_ENTRY_PAUSED_BY_USER.
    entry_paused: bool = False

    # ── position ─────────────────────────────────────────────────────────
    position: Optional[PositionSnapshot] = None
    last_position_reconcile_at: Optional[str] = None
    position_reconcile_diag: dict[str, Any] = field(default_factory=dict)

    # ── day-rollover / signal bookkeeping ───────────────────────────────
    session_date: Optional[str] = None
    last_detected_direction: Optional[str] = None  # Direction.value of the last CONFIRMED flag
    last_confirmed_bar_ts: Optional[str] = None  # bar_start isoformat of the last bar this state evaluated
    last_flag_display_time: Optional[str] = None  # bar_start (KIS-screen convention: shown time = bar START)
    last_flag_confirmed_at: Optional[str] = None  # bar_start + 3min (actual order-authority moment)
    last_flag_direction: Optional[str] = None
    processed_signal_ids: list[str] = field(default_factory=list)

    # ── WebSocket health (gates NEW entries only — never gates an exit) ──
    ws_connected: bool = False
    ws_last_tick_at: Optional[str] = None
    ws_last_error: Optional[str] = None
    ws_reconnect_count: int = 0
    ws_subscribed_at: Optional[str] = None

    # ── warm-up (gates NEW entries only) ─────────────────────────────────
    warmup_bars_1m_count: int = 0
    warmup_bars_3m_count: int = 0
    warmup_ready: bool = False

    # ── last-tick diagnostics ────────────────────────────────────────────
    last_mu_price: Optional[float] = None
    last_mu_tvol: Optional[int] = None
    last_long_etf_price: Optional[float] = None  # config.LONG_SYMBOL (0193T0) broker quote
    last_inverse_etf_price: Optional[float] = None  # config.INVERSE_SYMBOL (0197X0) broker quote
    last_etf_quote_at: Optional[str] = None

    # ── order/gate diagnostics ───────────────────────────────────────────
    order_block_reason: Optional[str] = None
    last_broker_order_result: Optional[str] = None
    last_broker_order_symbol: Optional[str] = None
    last_broker_order_side: Optional[str] = None
    last_broker_order_at: Optional[str] = None

    # ── worker/process diagnostics ───────────────────────────────────────
    worker_instance_id: Optional[str] = None
    worker_started_at: Optional[str] = None
    tick_seq_total: int = 0
    last_tick_at: Optional[str] = None
    # 2026-08-13 fix: last time status() auto-restarted a dead worker thread
    # after a process restart (Render idle-sleep/redeploy) -- rate-limits
    # recovery attempts, see config.WORKER_AUTO_RECOVER_COOLDOWN_SEC.
    last_auto_recover_attempt_at: Optional[str] = None

    # ── Optional "시간대별 최적거래 필터" (2026-08-15) — reuses macd2's own
    # time_window_filter/time_window_position_manager pure functions by
    # import; this state only tracks the two-bar (T -> T+3) pending
    # candidate and, once a position is opened by this filter, its own
    # ladder progress. Default OFF (config.TIME_WINDOW_FILTER_ENABLED_DEFAULT).
    time_window_filter_enabled: bool = False
    time_window_pending_flag_direction: Optional[str] = None  # Direction.value
    time_window_pending_flag_bar_ts: Optional[str] = None
    time_window_position_active: bool = False
    time_window_tp1_done: bool = False
    time_window_peak_net_return: float = 0.0
    # 2026-08-18 real-incident fix: TP1/TP2/STOP_LOSS used to be judged off the
    # traded ETF's raw live tick every ~2s (see worker._advance_time_window_
    # position_management), so a single-tick spike/dip could trip STOP_LOSS
    # and never recover -- exactly what macd2's OWN time-window ladder already
    # avoids via _advance_stop_loss_bar's completed-3m-bar-close requirement.
    # These three mirror that same state shape (kept "time_window"-namespaced
    # since only THIS filter's ladder uses them, unlike macd2's module-level
    # stop_loss_bar_* fields which back its plain non-TW Stop Loss too).
    time_window_stop_loss_bar_symbol: Optional[str] = None
    time_window_stop_loss_entry_bar_ts: Optional[str] = None
    time_window_stop_loss_bar_ts: Optional[str] = None
    time_window_stop_loss_bar_close: Optional[float] = None
    time_window_morning_entry_count: int = 0
    time_window_afternoon_entry_count: int = 0
    last_time_window_score: Optional[float] = None
    last_time_window_decision: Optional[str] = None
    last_time_window_block_reason: Optional[str] = None

    # ── Optional "TW 1 blue" 예외진입 (2026-08-19) — a sub-toggle of the TW
    # filter (meaningless when time_window_filter_enabled is False): a
    # DOWN_BLUE candidate the TW gate itself REJECTS still gets one extra
    # entry per day, no other condition. Mirrors app.trading.macd2's own
    # down_blue_exception_filter_enabled field exactly (same conditions/logic,
    # ported here per user request). daily_down_blue_exception_used is
    # session-scoped (reset on day rollover); the toggle itself survives.
    down_blue_exception_filter_enabled: bool = False
    down_blue_exception_filter_enabled_at: Optional[str] = None
    down_blue_exception_filter_enabled_by: Optional[str] = None
    down_blue_exception_filter_version: str = ""
    daily_down_blue_exception_used: bool = False
    last_down_blue_exception_at: Optional[str] = None


@dataclass
class TickResult:
    """One run_once() call's outcome — diagnostic only, never persisted
    verbatim (state carries the durable fields)."""

    actions: list[str] = field(default_factory=list)
    skipped: Optional[str] = None
    signal_detected_at: Optional[str] = None
