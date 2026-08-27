"""Trend Establishment Gate (TEG) -- 2026-08-27 사용자 요청.

Read-only research module. Does NOT modify, import into, or get imported by
any file under app/trading/macd2/ -- pure standalone research code, exactly
like scripts/tw_gate_relaxed_optimization.py and friends. TEG is layered ON
TOP of an already-TW2-approved candidate (it never runs standalone, never
creates/suppresses a confirmed MACD crossover, never replaces TW2's own T+3
confirmation/quality-score/VWAP-veto/recent-cross-veto gate).

TEG conditions (ALL must hold for approval), exactly per the user's spec:
  1. 기존 TW2 T+3 confirmation 통과 -- re-derived HERE directly from the same
     gap series (gap_now > 0 and gap_now > gap_flag, sign-adjusted for
     direction -- literally evaluate_time_window_entry's own top-of-function
     two checks, TW_REJECT_NOT_CONFIRMED / TW_REJECT_MACD_GAP_NOT_EXPANDING),
     deliberately NOT the full ``decision.approved`` (which also folds in
     window/quality-score/entry-count-cap/duplicate-position -- those are
     separate TW2 gates, not part of the "T+3 confirmation" step itself; a
     candidate can fail the FULL TW2 decision purely on entry-count while
     still genuinely having passed T+3 confirmation, and TEG's own condition
     1 must say so -- 2026-08-27 validated against the real 8/25 12:09/8/26
     11:06 UP_RED flags, both rejected by full TW2 only via
     TW_REJECT_MAX_ENTRY_COUNT after already passing T+3 confirmation).
  2. 최근 30분 confirmed crossover <= 1 (reuses time_window_filter's own
     ``_count_recent_confirmed_crossovers`` -- the exact same crossover-
     counting machinery TW2's own recent-cross veto already uses, just a
     different threshold: TW2 vetoes at >=4, TEG requires <=1).
  3. abs(MACD-Signal) gap expanding for 2 consecutive completed 3-minute
     bars in a row (|hist[T+1]| > |hist[T]| > |hist[T-1]|, strictly).
  4. abs(EMA10-EMA20) spread also expanding for the same 2 consecutive bars
     (EMA10/EMA20 = config.MAJOR_EMA_FAST/MAJOR_EMA_SLOW spans = 10/20,
     same spans TW2's own quality-score EMA-stack check already uses --
     computed fresh here as a full series since major_flag_filter only
     ever exposes the single latest-bar scalar value).
  5. UP_RED: close > EMA10 > EMA20 at the confirmation bar. DOWN_BLUE:
     close < EMA10 < EMA20 (mirrors).
  6. price on the entry-direction-favorable side of session VWAP (reuses
     major_flag_filter._session_vwap -- the SAME VWAP computation TW2's own
     VWAP veto uses -- close >= vwap for UP_RED, close <= vwap for
     DOWN_BLUE; TEG requires the favorable side outright, not just "not
     more than threshold% unfavorable" like TW2's own softer veto).
  7. >= config.MIN_FLAG_INTERVAL_MINUTES (9) minutes since the immediately
     preceding OPPOSITE-direction confirmed flag (no prior opposite flag at
     all counts as satisfied -- vacuously true, nothing to violate).

All evaluated at the SAME confirmation bar (T+3, i.e. bars_3m's last row)
TW2 itself uses for its own approval decision -- no look-ahead beyond that
bar anywhere in this module.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

TEG_VERSION = "TEG_V1_20260827"
TEG_RECENT_CROSS_LOOKBACK_MINUTES = 30
TEG_MAX_RECENT_CROSSES = 1
TEG_EMA_FAST = config.MAJOR_EMA_FAST   # 10
TEG_EMA_SLOW = config.MAJOR_EMA_SLOW   # 20
TEG_MIN_OPPOSITE_INTERVAL_MINUTES = config.MIN_FLAG_INTERVAL_MINUTES  # 9

COND_TW2_CONFIRMED = "tw2_confirmed"
COND_RECENT_CROSS = "recent_cross_le_1"
COND_MACD_GAP_EXPANDING = "macd_gap_expanding_2bar"
COND_EMA_SPREAD_EXPANDING = "ema_spread_expanding_2bar"
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
    conditions: dict = field(default_factory=dict)     # cond_name -> bool
    metrics: dict = field(default_factory=dict)         # raw computed numbers
    reject_reasons: tuple = ()


def _insufficient(reason: str) -> TEGDecision:
    return TEGDecision(approved=False, conditions={}, metrics={}, reject_reasons=(reason,))


def evaluate_teg(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    flag_bar_dt: datetime,
    decision_at: datetime,
) -> TEGDecision:
    """``bars_3m`` truncated through the T+3 confirmation bar (its LAST row)
    -- the exact same frame the caller already passed to
    time_window_filter.evaluate_time_window_entry / evaluate_tw2_extra_vetoes.
    ``flag_bar_dt`` is the original flag bar T (one bar before the last row).
    Fully self-contained -- does not take the caller's full TW2 ``decision``
    as input at all; condition 1 (T+3 confirmation) is re-derived directly
    from the same gap series, so TEG's own approval never depends on
    whether TW2's LATER gates (window/quality-score/entry-count/duplicate-
    position) also happened to approve the same candidate."""
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

    # 1) TW2 T+3 confirmation, re-derived directly (see docstring/module
    #    header for why this is NOT the same as the full decision.approved)
    sign = 1 if direction == Direction.UP_RED else -1
    gap_flag = float(series["gap"].iloc[flag_idx]) * sign
    gap_now = float(series["gap"].iloc[confirm_idx]) * sign
    metrics["gap_flag"] = gap_flag
    metrics["gap_now"] = gap_now
    cond1 = (gap_now > 0) and (gap_now > gap_flag)
    conditions[COND_TW2_CONFIRMED] = bool(cond1)
    if not cond1:
        reasons.append(COND_TW2_CONFIRMED)

    # 2) recent 30-min confirmed crossover count <= 1
    recent_count = twf._count_recent_confirmed_crossovers(
        work, decision_at, TEG_RECENT_CROSS_LOOKBACK_MINUTES, exclude_bar_dt=flag_bar_dt,
    )
    metrics["recent_30min_cross_count"] = recent_count
    conditions[COND_RECENT_CROSS] = recent_count <= TEG_MAX_RECENT_CROSSES
    if not conditions[COND_RECENT_CROSS]:
        reasons.append(COND_RECENT_CROSS)

    # 3) |MACD-Signal gap| strictly expanding over the last 2 completed bars
    #    (confirm_idx vs confirm_idx-1 vs confirm_idx-2)
    if confirm_idx - 2 < 0:
        gap_abs = (None, None, None)
        cond3 = False
    else:
        g0 = abs(float(series["gap"].iloc[confirm_idx - 2]))
        g1 = abs(float(series["gap"].iloc[confirm_idx - 1]))
        g2 = abs(float(series["gap"].iloc[confirm_idx]))
        gap_abs = (g0, g1, g2)
        cond3 = (g2 > g1) and (g1 > g0)
    metrics["macd_gap_abs_last3"] = gap_abs
    conditions[COND_MACD_GAP_EXPANDING] = bool(cond3)
    if not conditions[COND_MACD_GAP_EXPANDING]:
        reasons.append(COND_MACD_GAP_EXPANDING)

    # 4) |EMA10-EMA20| spread strictly expanding over the same 2 bars
    close = work["close"].astype(float).reset_index(drop=True)
    ema10 = close.ewm(span=TEG_EMA_FAST, adjust=False).mean()
    ema20 = close.ewm(span=TEG_EMA_SLOW, adjust=False).mean()
    spread = (ema10 - ema20).abs()
    if confirm_idx - 2 < 0 or confirm_idx >= len(spread):
        spread_last3 = (None, None, None)
        cond4 = False
    else:
        s0 = float(spread.iloc[confirm_idx - 2])
        s1 = float(spread.iloc[confirm_idx - 1])
        s2 = float(spread.iloc[confirm_idx])
        spread_last3 = (s0, s1, s2)
        cond4 = (s2 > s1) and (s1 > s0)
    metrics["ema_spread_abs_last3"] = spread_last3
    conditions[COND_EMA_SPREAD_EXPANDING] = bool(cond4)
    if not conditions[COND_EMA_SPREAD_EXPANDING]:
        reasons.append(COND_EMA_SPREAD_EXPANDING)

    # 5) price/EMA stack alignment at the confirmation bar
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

    # 6) price on the entry-direction-favorable side of session VWAP
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

    # 7) >= 9 minutes since the immediately preceding OPPOSITE-direction flag
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
