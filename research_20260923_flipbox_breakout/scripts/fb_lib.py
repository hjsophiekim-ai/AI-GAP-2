"""FLIP-BOX BREAKOUT research library (READ-ONLY).

Independent reconstruction of MACD2 flags from raw 1-minute price data using
the PRODUCTION signal_engine functions only. Does not import any replay flag
list as ground truth.
"""
from __future__ import annotations

import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(r"C:\Users\FURSYS\Desktop\AI-GAP 2")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2.market_data import filter_complete_3m_bars  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd, evaluate_macd_crossover, resample_completed_3m,
)

KST = config.KST
CACHE = ROOT / "data" / "cache"

SESSION_OPEN = dtime(9, 0)
SESSION_END = dtime(15, 30)


def all_dates(tag: str = "hynix") -> list[str]:
    out = set()
    for p in CACHE.glob(f"replay_*_{tag}_1m.csv"):
        parts = p.stem.split("_")
        if len(parts) >= 3 and parts[1].isdigit() and len(parts[1]) == 8 and "(" not in p.stem:
            out.add(parts[1])
    return sorted(out)


def load_1m(date: str, tag: str, *, session_only: bool = True) -> Optional[pd.DataFrame]:
    p = CACHE / f"replay_{date}_{tag}_1m.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize(KST)
    df = df.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    # only the stated trading date (files sometimes carry neighbours)
    df = df[df["datetime"].dt.strftime("%Y%m%d") == date]
    if session_only:
        t = df["datetime"].dt.time
        df = df[(t >= SESSION_OPEN) & (t <= SESSION_END)]
    return df.reset_index(drop=True) if len(df) else None


def build_day(date: str, prior: Optional[str], *, session_only: bool = True):
    """Return (bars_3m, flags, df_1m) for one trading date.

    bars_3m: completeness-filtered completed 3m bars for warmup-day + date.
    flags:   list of dicts for `date` only, built with the production
             zero-cross onset rule, prev_direction reset at day start.
    """
    cur = load_1m(date, "hynix", session_only=session_only)
    if cur is None:
        return None, None, None
    frames = []
    if prior is not None:
        pv = load_1m(prior, "hynix", session_only=session_only)
        if pv is not None:
            frames.append(pv)
    frames.append(cur)
    allbars = pd.concat(frames, ignore_index=True).sort_values("datetime")
    allbars = allbars.drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)

    end = datetime.combine(pd.Timestamp(date).date(), dtime(23, 59), tzinfo=KST)
    raw = resample_completed_3m(allbars, now=end)
    bars, _dropped = filter_complete_3m_bars(raw, allbars)
    if bars is None or bars.empty:
        return None, None, None
    bars = bars.reset_index(drop=True)
    bars["date"] = bars["datetime"].dt.strftime("%Y%m%d")

    flags = []
    prev_dir = None
    last_date = None
    for i in range(len(bars)):
        snap = calculate_macd(bars.iloc[: i + 1])
        if snap is None:
            continue
        bd = bars["date"].iloc[i]
        if last_date is None or bd != last_date:
            prev_dir = None
        last_date = bd
        d = evaluate_macd_crossover(snap, prev_dir)
        if d in (Direction.UP_RED, Direction.DOWN_BLUE):
            prev_dir = d
            if bd == date:
                flags.append({
                    "date": date,
                    "bar_i": i,
                    "bar_start": bars["datetime"].iloc[i],
                    "known_at": bars["datetime"].iloc[i] + timedelta(minutes=3),
                    "direction": d.value,
                    "macd": snap.macd,
                    "signal": snap.signal,
                    "gap": snap.macd - snap.signal,
                })
    day_bars = bars[bars["date"] == date].reset_index(drop=True)
    return day_bars, flags, cur
