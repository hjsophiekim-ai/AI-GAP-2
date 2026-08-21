"""Optional "시간대별 최적거래 필터" — position-management (exit ladder)
half. Pure functions only; no network/state-file/broker access (mirrors
risk_exit.py's own contract). Net returns are plain percent floats (e.g.
``2.5`` for +2.5%), the same convention risk_exit.check_stop_loss/
worker._net_return_pct already use — config.py's MORNING_*/AFTERNOON_*
constants are stored as fractions (0.025) per spec §17 and converted to
percent once, at module load, below.

Only ever applies to a position that was entered BY this filter
(``state.time_window_position_active`` in worker.py) — every other exit path
(STOP_LOSS/PROFIT_LOCK/QUICK_PROFIT/OPPOSITE_SIGNAL/FORCED_LIQUIDATION for
positions opened under a different toggle or no toggle) is completely
untouched by this module. OPPOSITE_SIGNAL itself (a new, 3-minute-confirmed
opposite flag) is NOT reimplemented here — worker.py's existing confirmed-
crossover + entry-gate machinery already is a 3-minute-confirmed check by
construction, so it is reused unchanged as this filter's §12/§14 "반대
플래그도 3분 확정 방식으로 처리" requirement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.trading.macd2 import config

MORNING_TP1_PCT = config.MORNING_TP1 * 100.0
MORNING_TP1_SELL_RATIO = config.MORNING_TP1_SELL_RATIO
MORNING_TP2_PCT = config.MORNING_TP2 * 100.0
MORNING_STOP_LOSS_PCT = config.MORNING_STOP_LOSS * 100.0
MORNING_AFTER_TP1_STOP_PCT = config.MORNING_AFTER_TP1_STOP * 100.0
MORNING_TRAILING_TRIGGER_PCT = config.MORNING_TRAILING_TRIGGER * 100.0
MORNING_TRAILING_STOP_PCT = config.MORNING_TRAILING_STOP * 100.0

AFTERNOON_TP_PCT = config.AFTERNOON_TP * 100.0
AFTERNOON_STOP_LOSS_PCT = config.AFTERNOON_STOP_LOSS * 100.0
AFTERNOON_BREAKEVEN_TRIGGER_PCT = config.AFTERNOON_BREAKEVEN_TRIGGER * 100.0
AFTERNOON_BREAKEVEN_STOP_PCT = config.AFTERNOON_BREAKEVEN_STOP * 100.0
AFTERNOON_PROFIT_LOCK_TRIGGER_PCT = config.AFTERNOON_PROFIT_LOCK_TRIGGER * 100.0
AFTERNOON_PROFIT_LOCK_STOP_PCT = config.AFTERNOON_PROFIT_LOCK_STOP * 100.0


@dataclass(frozen=True)
class PositionManagementDecision:
    exit_reason: Optional[str]   # config.EXIT_TW_* label, or None == HOLD
    sell_fraction: float         # fraction of the CURRENTLY held quantity to sell (0.0-1.0)
    tp1_done: bool               # updated tp1_done flag to persist
    peak_net_return: float       # updated peak-since-entry tracker to persist
    label: str                   # human-readable ladder stage, for logging


def evaluate_morning_position(
    *, net_return_pct: float, tp1_done: bool, peak_net_return: float = 0.0,
    tp2_pct_override: Optional[float] = None,
) -> PositionManagementDecision:
    """§11-12 morning ladder.

    < TP1 (3.0%): plain stop-loss at MORNING_STOP_LOSS (-1.7%).
    >= TP1, not yet taken: sell MORNING_TP1_SELL_RATIO (50%), remaining
      quantity's stop rises to MORNING_AFTER_TP1_STOP (+0.3%).
    after TP1: once peak-since-entry reaches MORNING_TRAILING_TRIGGER
      (3.5%), remaining stop rises again to MORNING_TRAILING_STOP (+2.0%).
    >= TP2 (5.0%, or ``tp2_pct_override`` if given — 2026-08-21: TW2 raises
      this to config.TW2_MORNING_TP2 for positions it opened; every other
      caller, including MU_MACD, omits it and keeps the unchanged default):
      sell all remaining quantity.
    """
    tp2_pct = MORNING_TP2_PCT if tp2_pct_override is None else float(tp2_pct_override)
    peak = max(float(peak_net_return), float(net_return_pct))

    if not tp1_done:
        if net_return_pct >= tp2_pct:
            return PositionManagementDecision(config.EXIT_TW_TP2_FULL, 1.0, True, peak, "TP2_DIRECT")
        if net_return_pct >= MORNING_TP1_PCT:
            return PositionManagementDecision(config.EXIT_TW_TP1_PARTIAL, MORNING_TP1_SELL_RATIO, True, peak, "TP1")
        if net_return_pct <= MORNING_STOP_LOSS_PCT:
            return PositionManagementDecision(config.EXIT_TW_STOP_LOSS, 1.0, False, peak, "STOP_LOSS")
        return PositionManagementDecision(None, 0.0, False, peak, "HOLD")

    if net_return_pct >= tp2_pct:
        return PositionManagementDecision(config.EXIT_TW_TP2_FULL, 1.0, True, peak, "TP2")

    active_stop = MORNING_TRAILING_STOP_PCT if peak >= MORNING_TRAILING_TRIGGER_PCT else MORNING_AFTER_TP1_STOP_PCT
    if net_return_pct <= active_stop:
        label = "TRAILING_STOP" if active_stop == MORNING_TRAILING_STOP_PCT else "AFTER_TP1_STOP"
        reason = config.EXIT_TW_TRAILING_STOP if active_stop == MORNING_TRAILING_STOP_PCT else config.EXIT_TW_AFTER_TP1_STOP
        return PositionManagementDecision(reason, 1.0, True, peak, label)
    return PositionManagementDecision(None, 0.0, True, peak, "HOLD_AFTER_TP1")


def evaluate_afternoon_position(
    *, net_return_pct: float, peak_net_return: float = 0.0,
) -> PositionManagementDecision:
    """§13-14 afternoon ladder — full-quantity TP, no partial (spec default).

    +1.5% (peak-since-entry): stop rises to AFTERNOON_BREAKEVEN_STOP (+0.2%).
    +2.0% (peak-since-entry): stop rises to AFTERNOON_PROFIT_LOCK_STOP (+1.0%).
    +2.5%: full exit. Base stop before 1.5% is AFTERNOON_STOP_LOSS (-1.2%).
    """
    peak = max(float(peak_net_return), float(net_return_pct))

    if net_return_pct >= AFTERNOON_TP_PCT:
        return PositionManagementDecision(config.EXIT_TW_AFTERNOON_TP, 1.0, False, peak, "AFTERNOON_TP")

    if peak >= AFTERNOON_PROFIT_LOCK_TRIGGER_PCT:
        active_stop, label, reason = (
            AFTERNOON_PROFIT_LOCK_STOP_PCT, "PROFIT_LOCK_STOP", config.EXIT_TW_PROFIT_LOCK_STOP,
        )
    elif peak >= AFTERNOON_BREAKEVEN_TRIGGER_PCT:
        active_stop, label, reason = (
            AFTERNOON_BREAKEVEN_STOP_PCT, "BREAKEVEN_STOP", config.EXIT_TW_BREAKEVEN_STOP,
        )
    else:
        active_stop, label, reason = (AFTERNOON_STOP_LOSS_PCT, "STOP_LOSS", config.EXIT_TW_STOP_LOSS)

    if net_return_pct <= active_stop:
        return PositionManagementDecision(reason, 1.0, False, peak, label)
    return PositionManagementDecision(None, 0.0, False, peak, "HOLD")


def evaluate_take_profit_immediate(
    *, session: str, net_return_pct: float, tp1_done: bool,
    tp2_pct_override: Optional[float] = None,
) -> PositionManagementDecision:
    """Take-profit-only check meant to run on every live tick, NOT gated on
    a completed 3-minute bar close (2026-08-21 user request: 익절판단은
    틱뜨자마자 바로 -- a real position sat above MORNING_TP2 for 20+ minutes
    without ever being sold because evaluate_morning_position() above is only
    ever called once a bar has fully closed, and this specific position's
    return had already retraced back below TP1 by every one of those bar
    closes).

    Deliberately NEVER returns a STOP_LOSS/AFTER_TP1_STOP/TRAILING_STOP/
    BREAKEVEN_STOP/PROFIT_LOCK_STOP decision -- only TP1_PARTIAL/TP2_FULL/
    AFTERNOON_TP. The downside ladder stays exactly as bar-close-gated as
    before: mu_macd.models.RuntimeState's own 2026-08-18 incident note is
    the reason why -- a single noisy tick spiking through a STOP_LOSS level
    and never recovering is a real, bad outcome (locks in a loss the market
    would have erased); a single tick spiking through a TAKE-PROFIT level
    is the opposite risk shape (worst case: sells a genuine peak a little
    early), which is exactly what "즉시 익절" was asked for. Both MACD2's
    worker.py and MU_MACD's worker.py call this same shared function so
    the two modules' take-profit timing behavior can never drift apart.
    """
    if session == "AFTERNOON":
        if net_return_pct >= AFTERNOON_TP_PCT:
            return PositionManagementDecision(config.EXIT_TW_AFTERNOON_TP, 1.0, tp1_done, net_return_pct, "AFTERNOON_TP_TICK")
        return PositionManagementDecision(None, 0.0, tp1_done, net_return_pct, "HOLD_TICK")

    tp2_pct = MORNING_TP2_PCT if tp2_pct_override is None else float(tp2_pct_override)
    if net_return_pct >= tp2_pct:
        return PositionManagementDecision(config.EXIT_TW_TP2_FULL, 1.0, True, net_return_pct, "TP2_TICK")
    if not tp1_done and net_return_pct >= MORNING_TP1_PCT:
        return PositionManagementDecision(config.EXIT_TW_TP1_PARTIAL, MORNING_TP1_SELL_RATIO, True, net_return_pct, "TP1_TICK")
    return PositionManagementDecision(None, 0.0, tp1_done, net_return_pct, "HOLD_TICK")


def evaluate_position(
    *, session: str, net_return_pct: float, tp1_done: bool, peak_net_return: float = 0.0,
    tp2_pct_override: Optional[float] = None,
) -> PositionManagementDecision:
    """Session-dispatching convenience wrapper (``session`` == "MORNING" or
    "AFTERNOON", as returned by time_window_filter.session_for_window).
    ``tp2_pct_override`` only ever affects the MORNING ladder (2026-08-21:
    TW2's TP2 override) — AFTERNOON_TP is untouched, matching TW2's own
    tested definition (only MORNING_TP2 was raised)."""
    if session == "AFTERNOON":
        return evaluate_afternoon_position(net_return_pct=net_return_pct, peak_net_return=peak_net_return)
    return evaluate_morning_position(
        net_return_pct=net_return_pct, tp1_done=tp1_done, peak_net_return=peak_net_return,
        tp2_pct_override=tp2_pct_override,
    )
