"""hynix_primary_trend.py — today's PRIMARY_TREND classification (UP/DOWN/RANGE).

Separates the day's dominant trend from 1/3/5-minute noise so a single short-term
pullback can't flip HYNIX<->INVERSE by itself. PRIMARY_TREND is classified from
the opening gap, today's cumulative VWAP, 15/30-minute EMA slope, and 15-minute
swing (higher/lower high-low) structure, with relative volume carried along as
supporting context (it never decides the direction by itself).

While PRIMARY_TREND is UP, a 1/3/5-minute decline is treated as a PULLBACK, not
a reversal — new INVERSE entries are blocked while price stays above VWAP/EMA20.
Flipping to INVERSE requires VWAP breakdown + a 15-minute downtrend + a broken
swing low to all be confirmed on two consecutive checks (DOWN is the mirror
case for flipping to HYNIX). Only in RANGE is the Fast Watcher's rapid
switching allowed to act on its own.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

PRIMARY_TREND_UP = "UP"
PRIMARY_TREND_DOWN = "DOWN"
PRIMARY_TREND_RANGE = "RANGE"

GAP_UP = "UP"
GAP_DOWN = "DOWN"
GAP_FLAT = "FLAT"

MOVE_PULLBACK = "PULLBACK"
MOVE_RANGE_SIGNAL = "RANGE_SIGNAL"
MOVE_ALIGNED = "ALIGNED"
MOVE_REVERSAL_CANDIDATE = "REVERSAL_CANDIDATE"

_GAP_FLAT_THRESHOLD_PCT = 0.15  # opening gap smaller than this counts as no gap (FLAT)
_EMA_FLAT_SLOPE_PCT = 0.02  # 15/30m EMA slope smaller than this counts as no trend (FLAT)
_REVERSAL_CONFIRMATIONS_REQUIRED = 2


def _ema_slope_pct(closes, span: int) -> Optional[float]:
    if closes is None or len(closes) < 2:
        return None
    ema = closes.ewm(span=min(span, len(closes)), adjust=False).mean()
    if len(ema) < 2 or not ema.iloc[-2]:
        return None
    return round((float(ema.iloc[-1]) / float(ema.iloc[-2]) - 1.0) * 100.0, 4)


def _slope_to_direction(slope_pct: Optional[float]) -> str:
    if slope_pct is None:
        return "FLAT"
    if slope_pct >= _EMA_FLAT_SLOPE_PCT:
        return "UP"
    if slope_pct <= -_EMA_FLAT_SLOPE_PCT:
        return "DOWN"
    return "FLAT"


def _swing_structure(df, lookback: int = 4) -> dict:
    """Whether the last `lookback` bars' highs/lows are climbing (HH/HL) or falling (LH/LL)."""
    if df is None or len(df) < lookback:
        return {"higher_high": False, "higher_low": False, "lower_high": False, "lower_low": False}
    work = df.sort_values("datetime").tail(lookback)
    highs = work["high"].tolist()
    lows = work["low"].tolist()
    return {
        "higher_high": all(highs[i] >= highs[i - 1] for i in range(1, len(highs))),
        "higher_low": all(lows[i] >= lows[i - 1] for i in range(1, len(lows))),
        "lower_high": all(highs[i] <= highs[i - 1] for i in range(1, len(highs))),
        "lower_low": all(lows[i] <= lows[i - 1] for i in range(1, len(lows))),
    }


def _daily_vwap(df) -> Optional[float]:
    if df is None or df.empty or "volume" not in df.columns:
        return None
    vol = df["volume"].fillna(0)
    if vol.sum() <= 0:
        return round(float(df["close"].mean()), 4)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    return round(float((typical * vol).sum() / vol.sum()), 4)


def _relative_volume(df, recent: int = 5, baseline: int = 20) -> Optional[float]:
    if df is None or len(df) < recent or "volume" not in df.columns:
        return None
    work = df.sort_values("datetime")
    recent_vol = work["volume"].tail(recent).mean()
    base_vol = work["volume"].tail(min(baseline, len(work))).mean()
    if not base_vol:
        return None
    return round(float(recent_vol / base_vol), 4)


def compute_primary_trend(df_1min, *, prev_close: Optional[float] = None, now: Optional[datetime] = None) -> dict:
    """Classify today's PRIMARY_TREND from the day's 1-minute bars.

    Falls back to RANGE (never assumed UP/DOWN) whenever there isn't enough data
    to judge — RANGE is also the state in which Fast Watcher rapid switching is
    allowed, so an honest "don't know yet" is the safe default.
    """
    now = now or datetime.now()
    result = {
        "primary_trend": PRIMARY_TREND_RANGE, "gap_direction": GAP_FLAT, "gap_pct": None,
        "above_vwap": None, "vwap": None, "trend_15m": "FLAT", "trend_30m": "FLAT",
        "ema_slope_15m_pct": None, "ema_slope_30m_pct": None,
        "ema20": None, "ema50": None, "above_ema20": None,
        "swing_15m": {}, "relative_volume": None, "last_price": None,
        "computed_at": now.isoformat(timespec="seconds"), "reasons": [], "up_votes": 0, "down_votes": 0,
    }
    if df_1min is None or getattr(df_1min, "empty", True) or len(df_1min) < 20:
        result["reasons"].append("insufficient 1m bars for primary trend (need >=20)")
        return result

    work = df_1min.sort_values("datetime").copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["close"])
    if work.empty:
        result["reasons"].append("no valid close prices")
        return result

    last_close = float(work["close"].iloc[-1])
    result["last_price"] = last_close

    if prev_close and prev_close > 0:
        open_price = float(work["open"].iloc[0]) if "open" in work.columns else last_close
        gap_pct = (open_price / prev_close - 1.0) * 100.0
        result["gap_pct"] = round(gap_pct, 4)
        if gap_pct >= _GAP_FLAT_THRESHOLD_PCT:
            result["gap_direction"] = GAP_UP
        elif gap_pct <= -_GAP_FLAT_THRESHOLD_PCT:
            result["gap_direction"] = GAP_DOWN
    else:
        result["reasons"].append("prev_close unavailable - gap not evaluated")

    vwap = _daily_vwap(work)
    result["vwap"] = vwap
    result["above_vwap"] = (last_close >= vwap) if vwap is not None else None

    if len(work["close"]) >= 20:
        result["ema20"] = round(float(work["close"].ewm(span=20, adjust=False).mean().iloc[-1]), 4)
        result["above_ema20"] = last_close >= result["ema20"]
    if len(work["close"]) >= 50:
        result["ema50"] = round(float(work["close"].ewm(span=50, adjust=False).mean().iloc[-1]), 4)

    from app.data_sources.auto_market_collector import _resample_minutes

    df_15 = _resample_minutes(work, 15)
    df_30 = _resample_minutes(work, 30)

    slope_15 = _ema_slope_pct(df_15["close"], span=6) if df_15 is not None and len(df_15) >= 2 else None
    slope_30 = _ema_slope_pct(df_30["close"], span=6) if df_30 is not None and len(df_30) >= 2 else None
    result["ema_slope_15m_pct"] = slope_15
    result["ema_slope_30m_pct"] = slope_30
    result["trend_15m"] = _slope_to_direction(slope_15)
    result["trend_30m"] = _slope_to_direction(slope_30)

    swing_15 = _swing_structure(df_15) if df_15 is not None else {}
    result["swing_15m"] = swing_15
    result["relative_volume"] = _relative_volume(work)

    up_votes = down_votes = 0
    if result["gap_direction"] == GAP_UP:
        up_votes += 1
    elif result["gap_direction"] == GAP_DOWN:
        down_votes += 1
    if result["above_vwap"] is True:
        up_votes += 1
    elif result["above_vwap"] is False:
        down_votes += 1
    if result["trend_15m"] == "UP":
        up_votes += 1
    elif result["trend_15m"] == "DOWN":
        down_votes += 1
    if result["trend_30m"] == "UP":
        up_votes += 1
    elif result["trend_30m"] == "DOWN":
        down_votes += 1
    if swing_15.get("higher_high") and swing_15.get("higher_low"):
        up_votes += 1
    elif swing_15.get("lower_high") and swing_15.get("lower_low"):
        down_votes += 1

    # 15/30m trend + VWAP are the core axis of this decision — needs at least 3 votes
    # and a clear majority over the opposite side to call a real trend instead of RANGE.
    # Volume never picks a direction by itself; relative_volume is context only.
    if up_votes >= 3 and up_votes > down_votes:
        result["primary_trend"] = PRIMARY_TREND_UP
    elif down_votes >= 3 and down_votes > up_votes:
        result["primary_trend"] = PRIMARY_TREND_DOWN
    result["up_votes"], result["down_votes"] = up_votes, down_votes
    return result


def classify_short_term_move(primary_trend: str, short_term_direction: Optional[str]) -> str:
    """Classify a 1/3/5-minute move against PRIMARY_TREND.

    UP trend + a short-term DOWN move is a PULLBACK (not a reversal), and the
    mirror case (DOWN trend + short-term UP) is also a PULLBACK. In RANGE, the
    short-term signal is taken at face value (RANGE_SIGNAL) since Fast Watcher's
    rapid switching is allowed there. Aligned moves are just ALIGNED.
    """
    if primary_trend == PRIMARY_TREND_UP and short_term_direction == "DOWN":
        return MOVE_PULLBACK
    if primary_trend == PRIMARY_TREND_DOWN and short_term_direction == "UP":
        return MOVE_PULLBACK
    if primary_trend == PRIMARY_TREND_RANGE:
        return MOVE_RANGE_SIGNAL
    return MOVE_ALIGNED


def new_inverse_entry_blocked(primary_trend: str, above_vwap: Optional[bool], above_ema20: Optional[bool]) -> bool:
    """Block new INVERSE entries while PRIMARY_TREND=UP and price holds above VWAP/EMA20."""
    if primary_trend != PRIMARY_TREND_UP:
        return False
    return bool(above_vwap) and bool(above_ema20)


def new_hynix_entry_blocked(primary_trend: str, above_vwap: Optional[bool], above_ema20: Optional[bool]) -> bool:
    """Mirror of new_inverse_entry_blocked for PRIMARY_TREND=DOWN: block new HYNIX
    entries while price holds below VWAP/EMA20."""
    if primary_trend != PRIMARY_TREND_DOWN:
        return False
    return (above_vwap is False) and (above_ema20 is False)


def default_reversal_confirmation_state() -> dict:
    return {"consecutive_count": 0, "last_direction": None, "last_confirmed_at": None, "should_switch": False}


def update_reversal_confirmation(
    tracker: Optional[dict], *, vwap_broken: bool, trend_15m_against: bool, swing_broken: bool,
    target_direction: str, now: datetime,
) -> dict:
    """Require the same target_direction's 3 conditions (VWAP breakdown/breakout,
    15-minute trend against the current primary trend, and a broken major swing
    low/high) to hold on two consecutive checks before confirming a flip.

    A single confirmed cycle is never enough — this is what makes the reversal
    "확인된 반전" rather than a same-cycle guess: any cycle where all three
    conditions aren't met resets the streak to zero.
    """
    state = dict(tracker) if tracker else default_reversal_confirmation_state()
    all_confirmed = bool(vwap_broken and trend_15m_against and swing_broken)
    if not all_confirmed:
        state["consecutive_count"] = 0
        state["last_direction"] = None
        state["should_switch"] = False
        return state
    if state.get("last_direction") == target_direction:
        state["consecutive_count"] = state.get("consecutive_count", 0) + 1
    else:
        state["consecutive_count"] = 1
        state["last_direction"] = target_direction
    state["last_confirmed_at"] = now.isoformat()
    state["should_switch"] = state["consecutive_count"] >= _REVERSAL_CONFIRMATIONS_REQUIRED
    return state
