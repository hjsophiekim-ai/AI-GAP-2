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


@dataclass
class TickResult:
    """One run_once() call's outcome — diagnostic only, never persisted
    verbatim (state carries the durable fields)."""

    actions: list[str] = field(default_factory=list)
    skipped: Optional[str] = None
    signal_detected_at: Optional[str] = None
