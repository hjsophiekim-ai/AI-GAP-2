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
    previous_diff = round(float(hist.iloc[-2]), 6)
    current_diff = round(float(hist.iloc[-1]), 6)
    relation = "ABOVE" if current_diff > 0 else ("BELOW" if current_diff < 0 else "EQUAL")
    bar_dt = pd.Timestamp(three_minute_bars["datetime"].iloc[-1]).to_pydatetime()
    return MacdSnapshot(
        bar_dt=bar_dt,
        macd=round(float(macd.iloc[-1]), 6),
        signal=round(float(signal.iloc[-1]), 6),
        hist=round(h0, 6),
        hist_last3=(round(h2, 6), round(h1, 6), round(h0, 6)),
        completed_3m_count=int(len(three_minute_bars)),
        previous_diff=previous_diff,
        current_diff=current_diff,
        relation=relation,
    )


def _floor_3m(dt: datetime) -> datetime:
    return dt.replace(minute=dt.minute - (dt.minute % 3), second=0, microsecond=0)


def calculate_provisional_macd(
    completed_three_minute_bars: Optional[pd.DataFrame],
    one_minute_bars: Optional[pd.DataFrame],
    *,
    now: datetime,
    current_price: float,
) -> Optional[MacdSnapshot]:
    """Completed 3m bars plus the currently forming 3m bar.

    The forming bar keeps the cached 1m OHLC already available to the Worker,
    but its close/high/low are refreshed with the latest 000660 quote. This
    uses the same MACD calculation as confirmed bars; only the last row is
    provisional.
    """
    _require_tz_aware_scalar(now, "calculate_provisional_macd(now=...)")
    if current_price <= 0:
        return None
    if completed_three_minute_bars is None or completed_three_minute_bars.empty:
        return None

    forming_start = _floor_3m(now.astimezone(config.KST))
    if forming_start.date() != now.astimezone(config.KST).date():
        return None
    if forming_start.time() < config.SESSION_OPEN:
        return None

    completed = completed_three_minute_bars.copy().sort_values("datetime")
    completed["datetime"] = pd.to_datetime(completed["datetime"], errors="coerce")
    completed = completed.dropna(subset=["datetime"])
    completed = completed[completed["datetime"] < forming_start]
    if completed.empty:
        return None

    prev_close = float(pd.to_numeric(completed["close"], errors="coerce").dropna().iloc[-1])
    open_price = prev_close
    high_price = max(prev_close, float(current_price))
    low_price = min(prev_close, float(current_price))
    volume = 0.0

    if one_minute_bars is not None and not one_minute_bars.empty and "datetime" in one_minute_bars.columns:
        one_min = one_minute_bars.copy()
        one_min["datetime"] = pd.to_datetime(one_min["datetime"], errors="coerce")
        if one_min["datetime"].dt.tz is None:
            raise ValueError(
                "calculate_provisional_macd: one_minute_bars['datetime'] must be timezone-aware KST"
            )
        one_min = one_min.dropna(subset=["datetime"]).sort_values("datetime")
        forming_rows = one_min[(one_min["datetime"] >= forming_start) & (one_min["datetime"] <= now)]
        if not forming_rows.empty:
            open_price = float(pd.to_numeric(forming_rows["open"], errors="coerce").dropna().iloc[0])
            highs = (
                pd.to_numeric(forming_rows["high"], errors="coerce").dropna()
                if "high" in forming_rows.columns else pd.Series(dtype=float)
            )
            lows = (
                pd.to_numeric(forming_rows["low"], errors="coerce").dropna()
                if "low" in forming_rows.columns else pd.Series(dtype=float)
            )
            vols = (
                pd.to_numeric(forming_rows["volume"], errors="coerce").dropna()
                if "volume" in forming_rows.columns else pd.Series(dtype=float)
            )
            high_price = max(float(highs.max()) if not highs.empty else open_price, float(current_price))
            low_price = min(float(lows.min()) if not lows.empty else open_price, float(current_price))
            volume = float(vols.sum()) if not vols.empty else 0.0

    forming = pd.DataFrame([{
        "datetime": forming_start,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": float(current_price),
        "volume": volume,
    }])
    return calculate_macd(pd.concat([completed, forming], ignore_index=True))


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


def signed_b_condition(macd_snapshot: MacdSnapshot) -> Direction:
    """Raw signed-B condition for the latest bar, without onset suppression."""
    return evaluate_signed_b(macd_snapshot, None)


def evaluate_macd_crossover(
    macd_snapshot: MacdSnapshot,
    previous_direction: Optional[Direction],
) -> Direction:
    """Primary MACD2 order signal: completed-bar MACD/Signal crossover onset."""
    previous_diff = macd_snapshot.previous_diff
    current_diff = macd_snapshot.current_diff
    if previous_diff is None:
        previous_diff = macd_snapshot.hist_last3[-2]
    if current_diff is None:
        current_diff = macd_snapshot.macd - macd_snapshot.signal

    if previous_diff <= 0 and current_diff > 0:
        pattern = Direction.UP_RED
    elif previous_diff >= 0 and current_diff < 0:
        pattern = Direction.DOWN_BLUE
    else:
        return Direction.HOLD

    if previous_direction == pattern:
        return Direction.HOLD
    return pattern


def is_tradeable_completed_bar(bar_dt: datetime, now_kst: datetime) -> bool:
    _require_tz_aware_scalar(bar_dt, "is_tradeable_completed_bar(bar_dt=...)")
    _require_tz_aware_scalar(now_kst, "is_tradeable_completed_bar(now_kst=...)")
    bar_kst = bar_dt.astimezone(config.KST)
    now_kst = now_kst.astimezone(config.KST)
    if bar_kst.date() != now_kst.date():
        return False
    if bar_kst.time() < config.SESSION_OPEN:
        return False
    return bar_kst + timedelta(minutes=3) <= now_kst.replace(second=0, microsecond=0)


def make_signal_id(completed_bar_dt: datetime, direction: Direction) -> str:
    """Signal id is derived only from the completed bar's own KST date/time."""
    _require_tz_aware_scalar(completed_bar_dt, "make_signal_id(completed_bar_dt=...)")
    bar_kst = completed_bar_dt.astimezone(config.KST)
    return f"{bar_kst:%Y%m%d}_{bar_kst:%H%M%S}_{direction.value}"


def make_provisional_signal_id(forming_bar_dt: datetime, direction: Direction) -> str:
    _require_tz_aware_scalar(forming_bar_dt, "make_provisional_signal_id(forming_bar_dt=...)")
    bar_kst = forming_bar_dt.astimezone(config.KST)
    return f"{bar_kst:%Y%m%d}_{bar_kst:%H%M%S}_{direction.value}_PROVISIONAL"
