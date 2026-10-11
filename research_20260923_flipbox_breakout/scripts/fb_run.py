"""Runner: build frames, detect clusters over the sensitivity grid, score."""
from __future__ import annotations
import pickle, numpy as np, pandas as pd
from datetime import time as dtime, timedelta
import fb_lib as F, fb_build as B
from fb_cluster import DayBars, detect

HORIZONS = [3, 6, 9, 15, 30, 60]


class Px:
    def __init__(self, df):
        self.ts = pd.DatetimeIndex(df["datetime"]).as_unit("ns").asi8
        self.c = df["close"].to_numpy(float)
        self.h = df["high"].to_numpy(float)
        self.l = df["low"].to_numpy(float)
        self.last = df["datetime"].iloc[-1] if len(df) else None
    def close_at(self, when):
        i = int(np.searchsorted(self.ts, pd.Timestamp(when).as_unit("ns").value, side="right"))
        return None if i == 0 else float(self.c[i - 1])
    def exact(self, when):
        v = pd.Timestamp(when).as_unit("ns").value
        i = int(np.searchsorted(self.ts, v, side="left"))
        return float(self.c[i]) if i < len(self.ts) and self.ts[i] == v else None
    def window(self, a, b):
        va = pd.Timestamp(a).as_unit("ns").value; vb = pd.Timestamp(b).as_unit("ns").value
        i = int(np.searchsorted(self.ts, va, side="left")); j = int(np.searchsorted(self.ts, vb, side="right"))
        if j <= i: return None, None
        return float(self.h[i:j].max()), float(self.l[i:j].min())



def load_all():
    with open("frames.pkl", "rb") as fh:
        z = pickle.load(fh)
    ba, fa, cov = z["ba"], z["fa"], z["cov"]
    days = {}
    for d in B.dates():
        sub = ba[ba["date"] == d]
        sub = sub[(sub["datetime"].dt.time >= dtime(9, 0)) & (sub["datetime"].dt.time <= dtime(15, 30))]
        if sub.empty: continue
        px = {}
        for tag in ("hynix", "long", "inverse"):
            f = F.load_1m(d, tag, session_only=False)
            if f is not None: px[tag] = Px(f)
        days[d] = DayBars(sub, px)
    flags_by_day = {}
    for f in fa:
        flags_by_day.setdefault(f["date"], []).append(f)
    return days, flags_by_day, cov


def score(rec, day: DayBars):
    """Post-hoc performance for a breakout record (LABEL ONLY)."""
    if not rec["breakout"]: return rec
    up = rec["last_dir"] == "UP_RED"
    bo_close_t = rec["bo_bar"] + timedelta(minutes=3)
    fill_t = bo_close_t                       # 1m bar labelled bo_close_t closes 1 min later
    hx = day.px.get("hynix")
    etf_tag = "long" if up else "inverse"
    et = day.px.get(etf_tag)
    out = dict(rec)
    out["fill_at"] = fill_t
    for nm, p in (("hx", hx), ("etf", et)):
        if p is None:
            for h in HORIZONS: out[f"{nm}_r{h}"] = None
            out[f"{nm}_mfe"] = out[f"{nm}_mae"] = out[f"{nm}_entry"] = None
            continue
        e = p.exact(fill_t) or p.close_at(fill_t)
        out[f"{nm}_entry"] = e
        if e is None or e <= 0:
            for h in HORIZONS: out[f"{nm}_r{h}"] = None
            out[f"{nm}_mfe"] = out[f"{nm}_mae"] = None
            continue
        last = p.last
        for h in HORIZONS:
            tt = fill_t + timedelta(minutes=h)
            if last is None or tt > last:
                out[f"{nm}_r{h}"] = None; continue
            v = p.close_at(tt)
            if v is None: out[f"{nm}_r{h}"] = None; continue
            r = (v - e) / e * 100.0
            out[f"{nm}_r{h}"] = r if (nm == "etf" or up) else -r
        out[f"{nm}_trunc"] = bool(last is None or (fill_t + timedelta(minutes=60)) > last)
        hi, lo = p.window(fill_t, fill_t + timedelta(minutes=60))
        if hi is None:
            out[f"{nm}_mfe"] = out[f"{nm}_mae"] = None
        elif nm == "etf" or up:
            out[f"{nm}_mfe"] = (hi - e) / e * 100.0; out[f"{nm}_mae"] = (lo - e) / e * 100.0
        else:
            out[f"{nm}_mfe"] = (e - lo) / e * 100.0; out[f"{nm}_mae"] = (e - hi) / e * 100.0
    return out


def run(W, M, inclusive, days, flags_by_day):
    rows = []
    for d, db in days.items():
        for r in detect(db, flags_by_day.get(d, []), W, M, inclusive):
            rows.append(score(r, db))
    return pd.DataFrame(rows)
