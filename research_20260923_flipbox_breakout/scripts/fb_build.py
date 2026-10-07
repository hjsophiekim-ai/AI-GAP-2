"""Build the continuous 3m frame + production flags, in two frame modes."""
from __future__ import annotations
import pandas as pd
from datetime import datetime, time as dtime, timedelta
import fb_lib as F
from app.trading.macd2.signal_engine import resample_completed_3m, calculate_macd_series
from app.trading.macd2.market_data import filter_complete_3m_bars
from app.trading.macd2.models import Direction

START = "20260615"


def dates():
    return [d for d in F.all_dates() if d >= START]


def coverage():
    rows = {}
    for d in dates():
        df = F.load_1m(d, "hynix", session_only=False)
        t = df["datetime"].dt.time
        rows[d] = {
            "n": len(df),
            "first": df["datetime"].iloc[0].strftime("%H:%M"),
            "last": df["datetime"].iloc[-1].strftime("%H:%M"),
            "pre": bool((t < dtime(9, 0)).any()),
            "ah": bool((t > dtime(15, 30)).any()),
            "full_session": df["datetime"].iloc[-1].time() >= dtime(15, 19),
        }
    return rows


def build_frame(session_only: bool, ds=None):
    ds = ds or dates()
    fr = [F.load_1m(d, "hynix", session_only=session_only) for d in ds]
    allb = (pd.concat([f for f in fr if f is not None], ignore_index=True)
            .sort_values("datetime").drop_duplicates("datetime", keep="last")
            .reset_index(drop=True))
    end = datetime.combine(pd.Timestamp(ds[-1]).date(), dtime(23, 59), tzinfo=F.KST)
    raw = resample_completed_3m(allb, now=end)
    bars, dropped = filter_complete_3m_bars(raw, allb)
    bars = bars.reset_index(drop=True)
    ser = calculate_macd_series(bars).reset_index(drop=True)
    bars["macd"] = ser["macd"]; bars["signal"] = ser["signal"]; bars["hist"] = ser["hist"]
    bars["date"] = bars["datetime"].dt.strftime("%Y%m%d")
    return bars, dropped


def flags_from(bars: pd.DataFrame, *, session_flags_only=True):
    """Production zero-cross onset, prev_direction reset per calendar day."""
    out = []
    prev = None
    last_date = None
    h = bars["hist"].to_numpy()
    dts = list(bars["datetime"])
    dates_ = list(bars["date"])
    prev_h = None
    for i in range(len(bars)):
        if last_date is None or dates_[i] != last_date:
            prev = None
            prev_h = None
        last_date = dates_[i]
        if prev_h is not None:
            if prev_h <= 0 < h[i]:
                d = Direction.UP_RED
            elif prev_h >= 0 > h[i]:
                d = Direction.DOWN_BLUE
            else:
                d = Direction.HOLD
            if d != Direction.HOLD and prev != d:
                prev = d
                t = dts[i]
                if (not session_flags_only) or (dtime(9, 0) <= t.time() <= dtime(15, 20)):
                    out.append({"date": dates_[i], "bar_i": i, "bar_start": t,
                                "known_at": t + timedelta(minutes=3),
                                "direction": d.value, "hist": float(h[i]),
                                "prev_hist": float(prev_h),
                                "macd": float(bars["macd"].iloc[i]),
                                "signal": float(bars["signal"].iloc[i])})
        prev_h = h[i]
    return out
