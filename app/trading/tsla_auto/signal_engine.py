"""TSLA_AUTO signal engine — pure functions only.

No network, state file, UI, or broker access. Implements docs/TSLA_AUTO_LOGIC.md
§MACD계산/§Primary신호/§Candidate-Shadow exactly — same MACD(12,26,9,
adjust=False) formula as app/trading/macd2/signal_engine.py, re-implemented
here (never imported from there, docs §3 완전 분리). The only real
difference from MACD2 is the session anchor: 3-minute bars are anchored to
09:30 ET (America/New_York), not 09:00 KST.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from app.trading.tsla_auto import config
from app.trading.tsla_auto.models import ConfirmedMacdFlag, Direction, MacdSnapshot

_THREE_MIN_COLUMNS = ("datetime", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class PrimaryCrossoverResult:
    """Common Primary MACD decision for Worker/UI/replay/tests. ``snapshot``
    is the completed-bars-plus-forming-bar MACD state — shadow display only;
    see worker.py for why this never carries order authority."""

    snapshot: Optional[MacdSnapshot]
    direction: Direction
    signal_id: Optional[str]


def _empty_3m_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_THREE_MIN_COLUMNS))


def _require_tz_aware_scalar(dt: datetime, label: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{label} must be timezone-aware (America/New_York), got naive datetime: {dt!r}")


def resample_completed_3m(one_minute_bars: Optional[pd.DataFrame], now: datetime) -> pd.DataFrame:
    """1-minute bars -> completed 3-minute bars only (docs §7).

    A 3m bar is included only when its 3-minute window has fully closed as of
    ``now``. ``label="left", closed="left"`` — the bar's own name/signal_id is
    always its START time (ET). 09:30 anchor happens automatically: 09:30 is
    already a multiple of 3 minutes from midnight, so pandas' default "3min"
    bin origin aligns with 09:30/09:33/... exactly like MACD2's 09:00 anchor.
    """
    _require_tz_aware_scalar(now, "resample_completed_3m(now=...)")
    if one_minute_bars is None or one_minute_bars.empty:
        return _empty_3m_frame()
    if "datetime" not in one_minute_bars.columns:
        raise ValueError("resample_completed_3m: one_minute_bars is missing a 'datetime' column")

    work = one_minute_bars.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    if work["datetime"].dt.tz is None:
        raise ValueError("resample_completed_3m: one_minute_bars['datetime'] must be timezone-aware")
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
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

    EMA settings are docs-fixed: fast=12, slow=26, signal=9, ``adjust=False``.
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
        previous_macd=round(float(macd.iloc[-2]), 6) if len(macd) >= 2 else None,
        previous_signal=round(float(signal.iloc[-2]), 6) if len(signal) >= 2 else None,
    )


def _floor_3m(dt: datetime) -> datetime:
    return dt.replace(minute=dt.minute - (dt.minute % 3), second=0, microsecond=0)


def forming_bar_window(now: datetime) -> tuple[datetime, datetime]:
    _require_tz_aware_scalar(now, "forming_bar_window(now=...)")
    start = _floor_3m(now.astimezone(config.ET))
    return start, start + timedelta(minutes=3)


def calculate_provisional_macd(
    completed_three_minute_bars: Optional[pd.DataFrame],
    one_minute_bars: Optional[pd.DataFrame],
    *,
    now: datetime,
    current_price: float,
) -> Optional[MacdSnapshot]:
    """Completed 3m bars plus the currently forming 3m bar — SHADOW DISPLAY
    ONLY (docs §8/§Candidate-Shadow). worker.py must never let this feed
    order_executor/strong_flag_filter/processed_signal_ids/signal ledger."""
    _require_tz_aware_scalar(now, "calculate_provisional_macd(now=...)")
    if current_price <= 0:
        return None
    if completed_three_minute_bars is None or completed_three_minute_bars.empty:
        return None

    forming_start, _forming_end = forming_bar_window(now)
    if forming_start.date() != now.astimezone(config.ET).date():
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
            raise ValueError("calculate_provisional_macd: one_minute_bars['datetime'] must be timezone-aware")
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


def evaluate_macd_crossover(
    macd_snapshot: MacdSnapshot,
    previous_direction: Optional[Direction],
) -> Direction:
    """Primary MACD crossover onset from previous diff to current diff (docs §8)."""
    pattern = raw_crossover_direction(macd_snapshot.previous_diff, macd_snapshot.current_diff)
    if pattern is None:
        return Direction.HOLD

    if previous_direction == pattern:
        return Direction.HOLD
    return pattern


def raw_crossover_direction(previous_diff: Optional[float], current_diff: Optional[float]) -> Optional[Direction]:
    if previous_diff is None or current_diff is None:
        return None
    prev = float(previous_diff)
    cur = float(current_diff)
    if prev <= 0 and cur > 0:
        return Direction.UP_RED
    if prev >= 0 and cur < 0:
        return Direction.DOWN_BLUE
    return None


def raw_color_for_snapshot(macd_snapshot: MacdSnapshot) -> Direction:
    """Current completed-bar MACD color state. This is informational only."""
    if macd_snapshot.macd > macd_snapshot.signal:
        return Direction.UP_RED
    if macd_snapshot.macd < macd_snapshot.signal:
        return Direction.DOWN_BLUE
    return Direction.HOLD


def previous_raw_color_for_snapshot(macd_snapshot: MacdSnapshot) -> Direction:
    if macd_snapshot.previous_macd is None or macd_snapshot.previous_signal is None:
        return Direction.HOLD
    if macd_snapshot.previous_macd > macd_snapshot.previous_signal:
        return Direction.UP_RED
    if macd_snapshot.previous_macd < macd_snapshot.previous_signal:
        return Direction.DOWN_BLUE
    return Direction.HOLD


def evaluate_confirmed_macd_flag(
    macd_snapshot: MacdSnapshot,
    previous_published_direction: Optional[Direction] = None,
) -> ConfirmedMacdFlag:
    """Split raw color from the one-shot, order-authoritative crossover flag."""
    confirmed = evaluate_macd_crossover(macd_snapshot, previous_published_direction)
    signal_id = make_signal_id(macd_snapshot.bar_dt, confirmed) if confirmed != Direction.HOLD else None
    return ConfirmedMacdFlag(
        bar_dt=macd_snapshot.bar_dt,
        raw_color=raw_color_for_snapshot(macd_snapshot),
        previous_raw_color=previous_raw_color_for_snapshot(macd_snapshot),
        confirmed_flag=confirmed,
        published_signal_id=signal_id,
        previous_macd=macd_snapshot.previous_macd,
        previous_signal=macd_snapshot.previous_signal,
        macd=macd_snapshot.macd,
        signal=macd_snapshot.signal,
        hist=macd_snapshot.hist,
    )


def evaluate_primary_forming_crossover(
    completed_three_minute_bars: Optional[pd.DataFrame],
    one_minute_bars: Optional[pd.DataFrame],
    *,
    now: datetime,
    current_price: float,
    previous_direction: Optional[Direction] = None,
) -> PrimaryCrossoverResult:
    """Forming-bar crossover — SHADOW DISPLAY ONLY. See worker.py: only the
    confirmed, completed-3m-bar crossover has order authority."""
    snap = calculate_provisional_macd(
        completed_three_minute_bars, one_minute_bars, now=now, current_price=current_price,
    )
    if snap is None:
        return PrimaryCrossoverResult(None, Direction.HOLD, None)
    direction = evaluate_macd_crossover(snap, previous_direction)
    signal_id = make_provisional_signal_id(snap.bar_dt, direction) if direction != Direction.HOLD else None
    return PrimaryCrossoverResult(snap, direction, signal_id)


def is_tradeable_completed_bar(bar_dt: datetime, now_et: datetime) -> bool:
    _require_tz_aware_scalar(bar_dt, "is_tradeable_completed_bar(bar_dt=...)")
    _require_tz_aware_scalar(now_et, "is_tradeable_completed_bar(now_et=...)")
    bar_et = bar_dt.astimezone(config.ET)
    now_et_ = now_et.astimezone(config.ET)
    if bar_et.date() != now_et_.date():
        return False
    if bar_et.time() < config.SESSION_OPEN:
        return False
    return bar_et + timedelta(minutes=3) <= now_et_.replace(second=0, microsecond=0)


def make_signal_id(completed_bar_dt: datetime, direction: Direction) -> str:
    """signal_id is derived only from the completed bar's own ET date/time
    (docs §8: "HHMMSS는 ET의 bar_start_at이다")."""
    _require_tz_aware_scalar(completed_bar_dt, "make_signal_id(completed_bar_dt=...)")
    bar_et = completed_bar_dt.astimezone(config.ET)
    return f"{bar_et:%Y%m%d}_{bar_et:%H%M%S}_{direction.value}"


def make_provisional_signal_id(forming_bar_dt: datetime, direction: Direction) -> str:
    _require_tz_aware_scalar(forming_bar_dt, "make_provisional_signal_id(forming_bar_dt=...)")
    bar_et = forming_bar_dt.astimezone(config.ET)
    return f"{bar_et:%Y%m%d}_{bar_et:%H%M%S}_{direction.value}_PROVISIONAL"
