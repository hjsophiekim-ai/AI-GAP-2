"""MACD2 signal engine — pure functions only.

No network, state file, UI, or broker access. Implements docs/MACD2_LOGIC.md
§§4-6 exactly (independent from app.trading.macd_hynix_strategy — see
docs/MACD2_LOGIC.md header and the 2026-07-23 design decision to keep MACD2
fully separate from MACD v1). Live, replay, and test code must all call these
same functions — no duplicate implementations.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.models import Direction, MacdSnapshot

_THREE_MIN_COLUMNS = ("datetime", "open", "high", "low", "close", "volume")


def _empty_3m_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_THREE_MIN_COLUMNS))


def _require_tz_aware_scalar(dt: datetime, label: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{label} must be timezone-aware KST, got naive datetime: {dt!r}")


def resample_completed_3m(one_minute_bars: Optional[pd.DataFrame], now: datetime) -> pd.DataFrame:
    """1-minute bars -> completed 3-minute bars only (docs §4).

    A 3m bar is included only when its 3-minute window has fully closed as of
    ``now`` (``bar_open + 3min <= now`` with seconds/microseconds zeroed).
    Incomplete (still-forming) 3m bars are never returned. resample uses
    ``label="left", closed="left"`` explicitly (pandas' own default for a
    fixed "3min" frequency, made explicit here per docs §5's "구현 전 확인"
    requirement to fix the label/closed convention in code).

    Raises ``ValueError`` on a malformed schema (missing/naive datetime
    column) rather than silently returning empty — an empty *input* (no rows
    yet, e.g. cold start) is a normal case and returns an empty frame.
    """
    _require_tz_aware_scalar(now, "resample_completed_3m(now=...)")
    if one_minute_bars is None or one_minute_bars.empty:
        return _empty_3m_frame()
    if "datetime" not in one_minute_bars.columns:
        raise ValueError("resample_completed_3m: one_minute_bars is missing a 'datetime' column")

    work = one_minute_bars.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    if work["datetime"].dt.tz is None:
        raise ValueError(
            "resample_completed_3m: one_minute_bars['datetime'] must be timezone-aware KST"
        )
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    # "수정된 같은 1분봉은 최신 값으로 교체" (docs §4) — keep the last occurrence per timestamp.
    work = work.drop_duplicates(subset=["datetime"], keep="last")
    if work.empty:
        return _empty_3m_frame()

    indexed = work.set_index("datetime")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in indexed.columns:
        agg["volume"] = "sum"
    bars = (
        indexed.resample("3min", label="left", closed="left")
        .agg(agg)
        .dropna(subset=["close"])
        .reset_index()
    )
    if bars.empty:
        return bars

    cutoff = now.replace(second=0, microsecond=0)
    completed = bars[bars["datetime"] + timedelta(minutes=3) <= cutoff]
    return completed.reset_index(drop=True)


def calculate_macd(three_minute_bars: Optional[pd.DataFrame]) -> Optional[MacdSnapshot]:
    """Completed 3m bars -> latest MacdSnapshot, or ``None`` if not enough data.

    EMA settings are docs-fixed: fast=12, slow=26, signal=9, ``adjust=False``
    (docs §5). Requires at least ``EMA_SLOW`` closes to produce a defined
    EMA26, and at least 3 histogram points to report ``hist_last3``.
    """
    if three_minute_bars is None or three_minute_bars.empty:
        return None
    if "datetime" not in three_minute_bars.columns or "close" not in three_minute_bars.columns:
        raise ValueError("calculate_macd: three_minute_bars must have 'datetime' and 'close' columns")

    closes = pd.to_numeric(three_minute_bars["close"], errors="coerce").dropna()
    if len(closes) < config.EMA_SLOW:
        return None

    ema_fast = closes.ewm(span=config.EMA_FAST, adjust=False).mean()
    ema_slow = closes.ewm(span=config.EMA_SLOW, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=config.EMA_SIGNAL, adjust=False).mean()
    hist = macd - signal
    if len(hist) < 3:
        return None

    h0, h1, h2 = float(hist.iloc[-1]), float(hist.iloc[-2]), float(hist.iloc[-3])
    bar_dt = pd.Timestamp(three_minute_bars["datetime"].iloc[-1]).to_pydatetime()
    return MacdSnapshot(
        bar_dt=bar_dt,
        macd=round(float(macd.iloc[-1]), 6),
        signal=round(float(signal.iloc[-1]), 6),
        hist=round(h0, 6),
        hist_last3=(round(h2, 6), round(h1, 6), round(h0, 6)),
        completed_3m_count=int(len(three_minute_bars)),
    )


def evaluate_signed_b(
    macd_snapshot: MacdSnapshot,
    previous_direction: Optional[Direction],
) -> Direction:
    """signed-B flag for the latest completed 3m bar (docs §6).

    UP_RED requires h0>0, h1>0, d0=h0-h1>0, d1=h1-h2>0, AND the previously
    confirmed direction is not already UP_RED (repeat-signal suppression).
    DOWN_BLUE is the mirror condition. Everything else is HOLD.
    """
    h2, h1, h0 = macd_snapshot.hist_last3
    d0 = h0 - h1
    d1 = h1 - h2

    if h0 > 0 and h1 > 0 and d0 > 0 and d1 > 0:
        pattern = Direction.UP_RED
    elif h0 < 0 and h1 < 0 and d0 < 0 and d1 < 0:
        pattern = Direction.DOWN_BLUE
    else:
        return Direction.HOLD

    if previous_direction == pattern:
        return Direction.HOLD
    return pattern


def make_signal_id(trading_date: str, completed_bar_at: str, direction: Direction) -> str:
    """docs §6: ``trading_date_completedBarAt_direction`` (e.g. 20260723_102700_DOWN_BLUE)."""
    if len(trading_date) != 8 or not trading_date.isdigit():
        raise ValueError(f"make_signal_id: trading_date must be YYYYMMDD, got {trading_date!r}")
    if len(completed_bar_at) != 6 or not completed_bar_at.isdigit():
        raise ValueError(f"make_signal_id: completed_bar_at must be HHMMSS, got {completed_bar_at!r}")
    return f"{trading_date}_{completed_bar_at}_{direction.value}"
