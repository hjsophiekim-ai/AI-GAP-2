"""MACD2 risk/exit decision logic — pure functions only.

No network, state file, UI, TradeCostEngine, or broker access — net-return
percentages are computed elsewhere (order_executor.py, using the actual ETF
entry price) and passed in here as plain floats. Independent from
app.trading.macd_hynix_strategy per the 2026-07-23 design decision (docs/
MACD2_LOGIC.md is the sole source of truth; v1 is reference-only, compared
in tests/macd2/test_parity.py).

Priority order (docs §10) is: 1) 15:00 forced liquidation, 2) stop loss,
3) opposite signed-B signal, 4) profit lock, 5) hold. Only (2) and (4) are
decided here — (1) and (3) require wall-clock and signal state that live in
worker.py (Phase 3), which combines all four into the full priority chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.trading.macd2 import config


def check_stop_loss(net_return_pct: float, sl_pct: float = config.STOP_LOSS_NET_PCT) -> bool:
    """True when net return vs ETF entry has crossed the stop-loss threshold (docs §10)."""
    return net_return_pct <= sl_pct


@dataclass(frozen=True)
class ProfitLockState:
    peak_net_return: float
    current_net_return: float
    giveback_pct: float
    profit_lock_active: bool
    should_exit: bool


def update_profit_lock_tracker(
    *,
    current_net_return: float,
    peak_net_return: float = 0.0,
    profit_lock_active: bool = False,
    activate_pct: float = config.PROFIT_LOCK_ACTIVATE_NET_PCT,
    giveback_pp: float = config.PROFIT_LOCK_GIVEBACK_PP,
) -> ProfitLockState:
    """Update peak/current/giveback and decide the Profit Lock exit (docs §10).

    Activates once ``current_net_return`` reaches ``activate_pct``; once
    active, exits when the giveback from the peak reaches ``giveback_pp``
    percentage points.
    """
    peak = max(float(peak_net_return), float(current_net_return))
    active = bool(profit_lock_active) or float(current_net_return) >= float(activate_pct)
    giveback = max(0.0, peak - float(current_net_return)) if active else 0.0
    should_exit = active and giveback >= float(giveback_pp)
    return ProfitLockState(
        peak_net_return=round(peak, 6),
        current_net_return=round(float(current_net_return), 6),
        giveback_pct=round(giveback, 6),
        profit_lock_active=active,
        should_exit=should_exit,
    )


@dataclass(frozen=True)
class PositionExitDecision:
    peak_net_return: float
    current_net_return: float
    giveback_pct: float
    profit_lock_active: bool
    exit_reason: Optional[str]  # config.EXIT_STOP_LOSS / config.EXIT_PROFIT_LOCK / None


def evaluate_position_exits(
    *,
    current_net_return: float,
    peak_net_return: float = 0.0,
    profit_lock_active: bool = False,
    sl_pct: float = config.STOP_LOSS_NET_PCT,
    activate_pct: float = config.PROFIT_LOCK_ACTIVATE_NET_PCT,
    giveback_pp: float = config.PROFIT_LOCK_GIVEBACK_PP,
) -> PositionExitDecision:
    """Combine stop-loss + Profit Lock with stop-loss taking priority (docs §10).

    Does not decide FORCED_LIQUIDATION or OPPOSITE_SIGNAL — those depend on
    wall-clock time and live signal state owned by worker.py (Phase 3).
    """
    tracker = update_profit_lock_tracker(
        current_net_return=current_net_return,
        peak_net_return=peak_net_return,
        profit_lock_active=profit_lock_active,
        activate_pct=activate_pct,
        giveback_pp=giveback_pp,
    )
    if check_stop_loss(current_net_return, sl_pct=sl_pct):
        exit_reason: Optional[str] = config.EXIT_STOP_LOSS
    elif tracker.should_exit:
        exit_reason = config.EXIT_PROFIT_LOCK
    else:
        exit_reason = None
    return PositionExitDecision(
        peak_net_return=tracker.peak_net_return,
        current_net_return=tracker.current_net_return,
        giveback_pct=tracker.giveback_pct,
        profit_lock_active=tracker.profit_lock_active,
        exit_reason=exit_reason,
    )
