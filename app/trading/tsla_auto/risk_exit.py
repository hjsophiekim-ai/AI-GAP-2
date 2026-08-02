"""TSLA_AUTO risk/exit decision logic — pure functions only.

No network, state file, or broker access. Structure and values mirror
app/trading/macd2/risk_exit.py exactly (docs TSLA_AUTO_COPY_MAP.md —
COPY_WITH_US_MARKET_CHANGE, but the Stop Loss/Profit Lock FORMULAS
themselves are unchanged from MACD2's actual values — only the priority
ORDER around them differs, and that ordering lives in worker.py, not here).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.trading.tsla_auto import config


def check_stop_loss(net_return_pct: float, sl_pct: float = config.STOP_LOSS_NET_PCT) -> bool:
    """True when net return vs ETF entry has crossed the stop-loss threshold."""
    return net_return_pct <= sl_pct


@dataclass(frozen=True)
class ProfitLockState:
    peak_net_return: float
    current_net_return: float
    giveback_pct: float
    profit_lock_active: bool
    should_exit: bool


def update_profit_lock_tracker(
    *, current_net_return: float, peak_net_return: float = 0.0, profit_lock_active: bool = False,
    activate_pct: float = config.PROFIT_LOCK_ACTIVATE_NET_PCT, giveback_pp: float = config.PROFIT_LOCK_GIVEBACK_PP,
) -> ProfitLockState:
    """Activates once ``current_net_return`` reaches ``activate_pct``; once
    active, exits when the giveback from the peak reaches ``giveback_pp``."""
    peak = max(float(peak_net_return), float(current_net_return))
    active = bool(profit_lock_active) or float(current_net_return) >= float(activate_pct)
    giveback = max(0.0, peak - float(current_net_return)) if active else 0.0
    exit_enabled = bool(getattr(config, "PROFIT_LOCK_EXIT_ENABLED", True))
    should_exit = exit_enabled and active and giveback >= float(giveback_pp)
    return ProfitLockState(
        peak_net_return=round(peak, 6), current_net_return=round(float(current_net_return), 6),
        giveback_pct=round(giveback, 6), profit_lock_active=active, should_exit=should_exit,
    )


@dataclass(frozen=True)
class PositionExitDecision:
    peak_net_return: float
    current_net_return: float
    giveback_pct: float
    profit_lock_active: bool
    exit_reason: Optional[str]  # config.EXIT_STOP_LOSS / config.EXIT_PROFIT_LOCK / None


def evaluate_position_exits(
    *, current_net_return: float, peak_net_return: float = 0.0, profit_lock_active: bool = False,
    sl_pct: float = config.STOP_LOSS_NET_PCT, activate_pct: float = config.PROFIT_LOCK_ACTIVATE_NET_PCT,
    giveback_pp: float = config.PROFIT_LOCK_GIVEBACK_PP,
) -> PositionExitDecision:
    """Combine stop-loss + Profit Lock with stop-loss taking priority between
    the two. Does not decide FORCED_LIQUIDATION or OPPOSITE_SIGNAL — the full
    5-step priority chain (docs §12) lives in worker.py."""
    tracker = update_profit_lock_tracker(
        current_net_return=current_net_return, peak_net_return=peak_net_return,
        profit_lock_active=profit_lock_active, activate_pct=activate_pct, giveback_pp=giveback_pp,
    )
    if check_stop_loss(current_net_return, sl_pct=sl_pct):
        exit_reason: Optional[str] = config.EXIT_STOP_LOSS
    elif tracker.should_exit:
        exit_reason = config.EXIT_PROFIT_LOCK
    else:
        exit_reason = None
    return PositionExitDecision(
        peak_net_return=tracker.peak_net_return, current_net_return=tracker.current_net_return,
        giveback_pct=tracker.giveback_pct, profit_lock_active=tracker.profit_lock_active, exit_reason=exit_reason,
    )
