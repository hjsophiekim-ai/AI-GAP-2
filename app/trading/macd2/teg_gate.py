"""Trend Establishment Gate (TEG) — production module, 2026-08-27.

FROZEN. This is the exact condition logic validated in scripts/teg_gate_v2.py
(signed net-change conditions 3/4) and re-verified via TRAIN/OOS split in
scripts/teg_c_train_oos_validation.py — conditions 1, 2, 5, 6, 7 and the
signed-net-change shape of 3/4 must never be modified without a fresh
backtest validation. Only ever called from worker._resolve_time_window_
candidate, and ONLY as a bypass check for a candidate that has already
cleared TW2's own T+3/quality-score/extra-veto gate in every respect except
either the daily entry-count cap OR (2026-08-27 사용자 요청, NOT covered by
the TRAIN/OOS validation below — see worker.py's bypass block for the exact
scope) the TW_MORNING_ONLY afternoon time-window block — never a
standalone/alternate entry gate.

Conditions (ALL must hold for approval):
  1. TW2's own T+3 confirmation, re-derived directly from the gap series
     (gap_now > 0 and gap_now > gap_flag, sign-adjusted for direction) —
     deliberately NOT the full evaluate_time_window_entry() decision (which
     also folds in window/quality-score/entry-count-cap/duplicate-position
     — a candidate can fail THAT purely on entry-count while still having
     genuinely passed T+3 confirmation, which is exactly the case this
     module exists to rescue).
  2. Confirmed-crossover count in the trailing 30 minutes <= 1 (reuses
     time_window_filter._count_recent_confirmed_crossovers — the same
     crossover-counting machinery TW2's own recent-cross veto uses, just a
     stricter threshold: TW2 vetoes at >=4, TEG requires <=1).
  3. MACD-Signal diff, SIGNED directional net-change acceleration: the
     signed hist value (sign-adjusted so positive == favorable for this
     flag's direction) must show a positive net change from bar
     confirm_idx-2 to confirm_idx (primary), floored at
     TEG_HIST_DELTA_FLOOR (non-trivial, not noise) — AND, when a 4th bar
     back exists, the net change from confirm_idx-3 to confirm_idx must
     ALSO be positive (secondary consistency check, sign-only, no floor).
     Individual bar-to-bar steps inside the window are NOT required to be
     individually monotonic — a dip-then-jump that nets out strongly
     positive PASSES (2026-08-27 fix: the original v1 spec required strict
     per-bar absolute-value monotonicity, which rejected a real target flag
     — 2026-08-25 12:09 UP_RED — purely because of a one-bar dip right at
     the crossover boundary; see scripts/teg_gate_v2.py's own docstring).
  4. EMA10-EMA20 spread (config.MAJOR_EMA_FAST/MAJOR_EMA_SLOW spans = 10/20),
     SIGNED directional net-change expansion — same treatment as 3, floored
     at TEG_SPREAD_DELTA_FLOOR.
  5. UP_RED: close > EMA10 > EMA20 at the confirmation bar. DOWN_BLUE:
     close < EMA10 < EMA20 (mirrors).
  6. Price on the entry-direction-favorable side of session VWAP (reuses
     major_flag_filter._session_vwap — the SAME VWAP computation TW2's own
     VWAP veto uses — close >= vwap for UP_RED, close <= vwap for
     DOWN_BLUE).
  7. >= config.MIN_FLAG_INTERVAL_MINUTES (9) minutes since the immediately
     preceding OPPOSITE-direction confirmed flag (no prior opposite flag at
     all counts as satisfied — vacuously true, nothing to violate).

All evaluated at the SAME confirmation bar (T+3, i.e. bars_3m's last row)
TW2 itself uses for its own approval decision — no look-ahead beyond that
bar anywhere in this module.

── FROZEN threshold derivation (2026-08-27) — never auto-retuned ──────────
TEG_HIST_DELTA_FLOOR / TEG_SPREAD_DELTA_FLOOR are the 20th percentile of the
bar-to-bar |Δhist| / |Δ(EMA10-EMA20)| distributions, computed ONLY over the
TRAIN period 2026-06-01..2026-07-28 (40 business days, n=11,847 bar-to-bar
deltas each — scripts/teg_c_train_oos_validation.py's
calibrate_thresholds()), deliberately EXCLUDING the OOS period 2026-07-
29..08-26 to avoid look-ahead/OOS contamination (an earlier full-60-day
calibration, 142.11/203.37, was discarded for exactly this reason). Applied
UNCHANGED to the held-out OOS period: variant C (TW2 + this bypass) beat
variant A (plain TW2) on ALL FOUR OOS metrics — entries 52->58, total%
20.865->25.493, compound% 20.015->25.501, MDD% 13.012->13.012 (tied). See
data/validation/teg_c_train_oos/summary.json for the full validation.
These two numbers are FROZEN — do not recompute/retune them without a fresh
TRAIN/OOS backtest confirming the change still improves OOS on all decision
metrics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Union

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2 import time_window_filter as twf
from app.trading.macd2.major_flag_filter import _as_direction, _prepare_bars, _session_vwap
from app.trading.macd2.models import Direction

TEG_VERSION = config.TIME_WINDOW_TEG_FILTER_VERSION
TEG_RECENT_CROSS_LOOKBACK_MINUTES = 30
TEG_MAX_RECENT_CROSSES = 1
TEG_EMA_FAST = config.MAJOR_EMA_FAST   # 10
TEG_EMA_SLOW = config.MAJOR_EMA_SLOW   # 20
TEG_MIN_OPPOSITE_INTERVAL_MINUTES = config.MIN_FLAG_INTERVAL_MINUTES  # 9

# FROZEN (see module docstring) — TRAIN-only (2026-06-01~07-28) 20th
# percentile of bar-to-bar |Δhist| / |Δ(EMA10-EMA20)|, n=11,847 each.
# Never auto-retuned; never recomputed on live/OOS data.
TEG_HIST_DELTA_FLOOR = 162.928
TEG_SPREAD_DELTA_FLOOR = 242.760

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
    only, no floor on the longer window)."""
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
        detail["secondary_net_3bar"] = None

    return bool(primary_ok and secondary_ok), detail


def evaluate_teg(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    flag_bar_dt: datetime,
    decision_at: datetime,
) -> TEGDecision:
    """``bars_3m`` truncated through the T+3 confirmation bar (its LAST row)
    — the exact same frame the caller (worker._resolve_time_window_candidate)
    already passed to time_window_filter.evaluate_time_window_entry /
    evaluate_tw2_extra_vetoes. ``flag_bar_dt`` is the original flag bar T
    (one bar before the last row). Fully self-contained — pure function, no
    state access, no order/broker calls, no look-ahead beyond the supplied
    frame's last row."""
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

    # 1) T+3 confirmation, re-derived directly
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

    # 3) MACD-Signal diff, SIGNED net-change acceleration
    signed_hist = series["gap"] * sign
    cond3, hist_detail = _signed_net_change_condition(signed_hist, confirm_idx, TEG_HIST_DELTA_FLOOR)
    metrics["macd_gap_signed_net"] = hist_detail
    conditions[COND_MACD_GAP_EXPANDING] = bool(cond3)
    if not cond3:
        reasons.append(COND_MACD_GAP_EXPANDING)

    # 4) EMA10-EMA20 spread, SIGNED net-change expansion
    close = work["close"].astype(float).reset_index(drop=True)
    ema10 = close.ewm(span=TEG_EMA_FAST, adjust=False).mean()
    ema20 = close.ewm(span=TEG_EMA_SLOW, adjust=False).mean()
    signed_spread = (ema10 - ema20) * sign
    cond4, spread_detail = _signed_net_change_condition(signed_spread, confirm_idx, TEG_SPREAD_DELTA_FLOOR)
    metrics["ema_spread_signed_net"] = spread_detail
    conditions[COND_EMA_SPREAD_EXPANDING] = bool(cond4)
    if not cond4:
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
