"""Trend Establishment Gate v2 -- 2026-08-27 사용자 피드백 반영.

v1 (scripts/teg_gate.py, kept unchanged/still importable) required conditions
3+4 to be STRICTLY, per-bar monotonically expanding in ABSOLUTE VALUE over
3 consecutive points. That rejected the user's own real target flag (2026-
08-25 12:09 UP_RED) purely because of a one-bar dip-then-jump pattern right
at the crossover boundary (|hist| went 207.4 -> 154.9 -> 375.97 -- net
strongly positive, but not step-monotonic). User-confirmed fix direction:
switch to SIGNED directional net-change over a 2-3 bar window instead of
strict per-bar absolute monotonicity. This module implements exactly that;
v1 is left alone so both remain independently runnable/comparable.

Conditions 1, 2, 5, 6, 7 are BYTE-IDENTICAL to v1 (see teg_gate.py's own
docstring for their full rationale) -- only 3 and 4 change:

  3. MACD-Signal diff, signed directional acceleration: the SIGNED hist
     value (MACD-Signal, sign-adjusted so positive == favorable for this
     flag's direction -- NOT abs()) must have a positive NET change from
     bar (confirm_idx-2) to confirm_idx (the primary 2-bar-window check),
     AND -- when a 4th bar back exists -- the net change from
     (confirm_idx-3) to confirm_idx must ALSO be positive (secondary
     consistency check: the acceleration isn't just a one-off blip against
     the slightly-longer trend; skipped/auto-pass when that bar isn't
     available yet, e.g. very early in the session). Individual bar-to-bar
     steps inside the window are NOT required to be individually
     monotonic -- a dip-then-jump that nets out strongly positive PASSES.
     The net change must also clear TEG_V2_HIST_DELTA_FLOOR (non-trivial,
     not noise) -- see threshold derivation below.
  4. EMA10-EMA20 spread, signed directional expansion: same treatment on
     the SIGNED (EMA10-EMA20) value (not abs()) -- net change over the same
     2-bar primary / 3-bar secondary-consistency window, floored at
     TEG_V2_SPREAD_DELTA_FLOOR.

── "non-trivial" threshold derivation (2026-08-27) ──────────────────────
No exact number was specified, so one was calibrated empirically rather
than guessed: scripts/_tmp_teg_v2_threshold_calibration.py computed the
bar-to-bar |Δhist| and |Δ(EMA10-EMA20)| distributions across EVERY
completed 3-minute bar in the same 60-business-day window this gate is
backtested over (2026-06-01..2026-08-26, n=18,181 bar-to-bar deltas each).
TEG_V2_HIST_DELTA_FLOOR/TEG_V2_SPREAD_DELTA_FLOOR are set to the **20th
percentile** of those two distributions respectively (142.11 / 203.37,
rounded below) -- i.e. a net 2-bar change must be at least as large as a
"typical small but real" single-bar move, filtering out only the bottom
~20% smallest/near-zero (noise-level) changes, not an arbitrary round
number. Percentile chosen (not median/mean) because both distributions are
strongly right-skewed (a handful of large moves inflate the mean); 20th
percentile is a deliberately LOOSE floor (matching the user's "relax the
strict monotonic requirement" direction) -- most genuine 2-bar net moves
clear it easily, it only screens out the flattest, most negligible ones.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.trading.macd2 import config
from app.trading.macd2 import time_window_filter as twf
from app.trading.macd2.major_flag_filter import _as_direction, _prepare_bars, _session_vwap
from app.trading.macd2.models import Direction

TEG_VERSION = "TEG_V2_20260827_SIGNED_NET_CHANGE"
TEG_RECENT_CROSS_LOOKBACK_MINUTES = 30
TEG_MAX_RECENT_CROSSES = 1
TEG_EMA_FAST = config.MAJOR_EMA_FAST   # 10
TEG_EMA_SLOW = config.MAJOR_EMA_SLOW   # 20
TEG_MIN_OPPOSITE_INTERVAL_MINUTES = config.MIN_FLAG_INTERVAL_MINUTES  # 9

# Empirically calibrated (see module docstring) -- 20th percentile of the
# bar-to-bar |Δhist| / |Δ(EMA10-EMA20)| distributions over the full 60-day
# backtest window (n=18,181 each), computed by
# scripts/_tmp_teg_v2_threshold_calibration.py on 2026-08-27.
TEG_V2_HIST_DELTA_FLOOR = 142.11
TEG_V2_SPREAD_DELTA_FLOOR = 203.37

COND_TW2_CONFIRMED = "tw2_confirmed"
COND_RECENT_CROSS = "recent_cross_le_1"
COND_MACD_GAP_EXPANDING = "macd_gap_signed_net_expanding"
COND_EMA_SPREAD_EXPANDING = "ema_spread_signed_net_expanding"
COND_EMA_STACK = "price_ema_stack_aligned"
COND_VWAP = "vwap_favorable_side"
COND_MIN_INTERVAL = "min_9min_since_opposite_flag"

ALL_CONDITIONS = (
    COND_TW2_CONFIRMED, COND_RECENT_CROSS, COND_MACD_GAP_EXPANDING,
    COND_EMA_SPREAD_EXPANDING, COND_EMA_STACK, COND_VWAP, COND_MIN_INTERVAL,
)


@dataclass(frozen=True)
class TEGDecision:
    approved: bool
    conditions: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    reject_reasons: tuple = ()


def _insufficient(reason: str) -> TEGDecision:
    return TEGDecision(approved=False, conditions={}, metrics={}, reject_reasons=(reason,))


def _signed_net_change_condition(
    signed_series: pd.Series, confirm_idx: int, floor: float,
) -> tuple[bool, dict[str, Any]]:
    """Primary: net change (confirm_idx-2 -> confirm_idx) is positive and
    >= floor. Secondary consistency (only when confirm_idx-3 exists): net
    change (confirm_idx-3 -> confirm_idx) is ALSO positive (sign agreement
    only, no floor on the longer window -- it exists to catch a one-bar
    fluke against the broader trend, not to double the strictness)."""
    detail: dict[str, Any] = {}
    if confirm_idx - 2 < 0:
        detail["primary_net_2bar"] = None
        return False, detail
    v_now = float(signed_series.iloc[confirm_idx])
    v_2back = float(signed_series.iloc[confirm_idx - 2])
    net_2bar = v_now - v_2back
    detail["value_now"] = v_now
    detail["value_2bar_back"] = v_2back
    detail["primary_net_2bar"] = net_2bar
    primary_ok = (net_2bar > 0) and (net_2bar >= floor)

    secondary_ok = True
    if confirm_idx - 3 >= 0:
        v_3back = float(signed_series.iloc[confirm_idx - 3])
        net_3bar = v_now - v_3back
        detail["value_3bar_back"] = v_3back
        detail["secondary_net_3bar"] = net_3bar
        secondary_ok = net_3bar > 0
    else:
        detail["secondary_net_3bar"] = None  # not available yet -- auto-pass

    return bool(primary_ok and secondary_ok), detail


def evaluate_teg(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    flag_bar_dt: datetime,
    decision_at: datetime,
) -> TEGDecision:
    """Same contract/signature as teg_gate.evaluate_teg (v1) -- fully self-
    contained, ``bars_3m`` truncated through the T+3 confirmation bar (last
    row). Conditions 1/2/5/6/7 identical to v1; 3/4 use signed net-change
    (see module docstring)."""
    direction = _as_direction(flag_direction)
    if direction is None:
        return _insufficient("invalid_direction")

    work = _prepare_bars(bars_3m)
    if work is None or len(work) < max(TEG_EMA_SLOW, 3) + 1:
        return _insufficient("insufficient_bars")

    series = twf._gap_series(work)
    if series is None or len(series) < 3:
        return _insufficient("insufficient_macd_series")

    flag_rows = series.index[series["datetime"] == pd.Timestamp(flag_bar_dt)]
    if len(flag_rows) == 0 or int(flag_rows[-1]) != len(series) - 2:
        return _insufficient("flag_bar_not_one_before_confirm_bar")
    flag_idx = int(flag_rows[-1])
    confirm_idx = len(series) - 1

    conditions: dict[str, bool] = {}
    metrics: dict[str, Any] = {}
    reasons: list[str] = []

    sign = 1 if direction == Direction.UP_RED else -1

    # 1) TW2 T+3 confirmation, re-derived directly (identical to v1)
    gap_flag = float(series["gap"].iloc[flag_idx]) * sign
    gap_now = float(series["gap"].iloc[confirm_idx]) * sign
    metrics["gap_flag"] = gap_flag
    metrics["gap_now"] = gap_now
    cond1 = (gap_now > 0) and (gap_now > gap_flag)
    conditions[COND_TW2_CONFIRMED] = bool(cond1)
    if not cond1:
        reasons.append(COND_TW2_CONFIRMED)

    # 2) recent 30-min confirmed crossover count <= 1 (identical to v1)
    recent_count = twf._count_recent_confirmed_crossovers(
        work, decision_at, TEG_RECENT_CROSS_LOOKBACK_MINUTES, exclude_bar_dt=flag_bar_dt,
    )
    metrics["recent_30min_cross_count"] = recent_count
    conditions[COND_RECENT_CROSS] = recent_count <= TEG_MAX_RECENT_CROSSES
    if not conditions[COND_RECENT_CROSS]:
        reasons.append(COND_RECENT_CROSS)

    # 3) MACD-Signal diff, SIGNED net-change acceleration
    signed_hist = series["gap"] * sign
    cond3, hist_detail = _signed_net_change_condition(signed_hist, confirm_idx, TEG_V2_HIST_DELTA_FLOOR)
    metrics["macd_gap_signed_net"] = hist_detail
    conditions[COND_MACD_GAP_EXPANDING] = bool(cond3)
    if not cond3:
        reasons.append(COND_MACD_GAP_EXPANDING)

    # 4) EMA10-EMA20 spread, SIGNED net-change expansion
    close = work["close"].astype(float).reset_index(drop=True)
    ema10 = close.ewm(span=TEG_EMA_FAST, adjust=False).mean()
    ema20 = close.ewm(span=TEG_EMA_SLOW, adjust=False).mean()
    signed_spread = (ema10 - ema20) * sign
    cond4, spread_detail = _signed_net_change_condition(signed_spread, confirm_idx, TEG_V2_SPREAD_DELTA_FLOOR)
    metrics["ema_spread_signed_net"] = spread_detail
    conditions[COND_EMA_SPREAD_EXPANDING] = bool(cond4)
    if not cond4:
        reasons.append(COND_EMA_SPREAD_EXPANDING)

    # 5) price/EMA stack alignment at the confirmation bar (identical to v1)
    close_now = float(close.iloc[confirm_idx])
    ema10_now = float(ema10.iloc[confirm_idx])
    ema20_now = float(ema20.iloc[confirm_idx])
    metrics["close"] = close_now
    metrics["ema10"] = ema10_now
    metrics["ema20"] = ema20_now
    if direction == Direction.UP_RED:
        cond5 = close_now > ema10_now > ema20_now
    else:
        cond5 = close_now < ema10_now < ema20_now
    conditions[COND_EMA_STACK] = bool(cond5)
    if not conditions[COND_EMA_STACK]:
        reasons.append(COND_EMA_STACK)

    # 6) price on the entry-direction-favorable side of session VWAP (identical to v1)
    vwap_series = _session_vwap(work)
    vwap_now = float(vwap_series.iloc[confirm_idx]) if confirm_idx < len(vwap_series) else float("nan")
    metrics["vwap"] = vwap_now if pd.notna(vwap_now) else None
    if pd.isna(vwap_now) or vwap_now <= 0:
        cond6 = False
    elif direction == Direction.UP_RED:
        cond6 = close_now >= vwap_now
    else:
        cond6 = close_now <= vwap_now
    conditions[COND_VWAP] = bool(cond6)
    if not conditions[COND_VWAP]:
        reasons.append(COND_VWAP)

    # 7) >= 9 minutes since the immediately preceding OPPOSITE-direction flag (identical to v1)
    prev_opposite_idx = twf._find_previous_opposite_flag(series, flag_idx, direction)
    if prev_opposite_idx is None:
        interval_minutes = None
        cond7 = True
    else:
        interval_minutes = (
            series["datetime"].iloc[flag_idx] - series["datetime"].iloc[prev_opposite_idx]
        ).total_seconds() / 60.0
        cond7 = interval_minutes >= TEG_MIN_OPPOSITE_INTERVAL_MINUTES
    metrics["minutes_since_prior_opposite_flag"] = interval_minutes
    conditions[COND_MIN_INTERVAL] = bool(cond7)
    if not conditions[COND_MIN_INTERVAL]:
        reasons.append(COND_MIN_INTERVAL)

    approved = all(conditions.get(c, False) for c in ALL_CONDITIONS)
    return TEGDecision(approved=approved, conditions=conditions, metrics=metrics, reject_reasons=tuple(reasons))
