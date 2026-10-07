"""FLIP-BOX cluster + breakout detection (strictly causal)."""
from __future__ import annotations
import numpy as np, pandas as pd
from datetime import time as dtime, timedelta
import fb_lib as F

LAST_FLAG_TIME = dtime(15, 20)
EXPIRE_MIN = 60          # cluster expires this long after the last flag bar
SESSION_LAST_BAR = dtime(15, 18)


class DayBars:
    """3m bars + 1m series for one date."""
    def __init__(self, bars3: pd.DataFrame, px1m: dict):
        self.b = bars3.reset_index(drop=True)
        self.t = list(self.b["datetime"])
        self.hi = self.b["high"].to_numpy(float)
        self.lo = self.b["low"].to_numpy(float)
        self.cl = self.b["close"].to_numpy(float)
        self.vol = self.b["volume"].to_numpy(float)
        self.hist = self.b["hist"].to_numpy(float)
        self.px = px1m


def detect(day_bars: DayBars, flags: list, W: int, M: int, inclusive: bool):
    """Return list of cluster records for one day."""
    t = day_bars.t
    n = len(t)
    idx_of = {tt: i for i, tt in enumerate(t)}
    fl = [f for f in flags if f["bar_start"] in idx_of]
    out = []
    consumed_until = -1          # bar index up to which a cluster already ran
    k = 0
    while k < len(fl):
        # ---- trigger test at flag k (causal: only flags <= k) ----
        span = [j for j in range(k + 1)
                if ((fl[k]["bar_start"] - fl[j]["bar_start"]).total_seconds() / 60.0 <= W
                    if inclusive else
                    (fl[k]["bar_start"] - fl[j]["bar_start"]).total_seconds() / 60.0 < W)]
        if len(span) - 1 < M:
            k += 1
            continue
        first_i = idx_of[fl[span[0]]["bar_start"]]
        if first_i <= consumed_until:
            k += 1
            continue
        # ---- cluster is live ----
        cl_flags = [fl[j] for j in span]
        last_k = k
        last_i = idx_of[fl[k]["bar_start"]]
        box_hi = float(np.max(day_bars.hi[first_i:last_i + 1]))
        box_lo = float(np.min(day_bars.lo[first_i:last_i + 1]))
        last_dir = fl[k]["direction"]
        i = last_i + 1
        rec = None
        while i < n:
            if t[i].time() > SESSION_LAST_BAR:
                break
            # new flag on this bar?
            nf = None
            if last_k + 1 < len(fl) and idx_of[fl[last_k + 1]["bar_start"]] == i:
                nf = fl[last_k + 1]
            if nf is not None:
                last_k += 1
                cl_flags.append(nf)
                last_i = i
                box_hi = max(box_hi, float(np.max(day_bars.hi[first_i:last_i + 1])))
                box_lo = min(box_lo, float(np.min(day_bars.lo[first_i:last_i + 1])))
                last_dir = nf["direction"]
                i += 1
                continue
            # expiry
            if (t[i] - t[last_i]).total_seconds() / 60.0 > EXPIRE_MIN:
                break
            c = day_bars.cl[i]
            up = last_dir == "UP_RED"
            b1 = (c > box_hi) if up else (c < box_lo)
            if b1:
                dh = day_bars.hist[i] - day_bars.hist[i - 1]
                b2 = (dh > 0) if up else (dh < 0)
                nxt = None
                if i + 1 < n and (t[i + 1] - t[i]).total_seconds() == 180:
                    c2 = day_bars.cl[i + 1]
                    nxt = (c2 > box_hi) if up else (c2 < box_lo)
                rec = dict(breakout=True, bo_i=i, bo_bar=t[i],
                           bo_close=float(c), bo_vol=float(day_bars.vol[i]),
                           b2=bool(b2), b3=bool(b2 and bool(nxt)),
                           next_outside=(None if nxt is None else bool(nxt)),
                           hist_delta=float(dh), hist_at_bo=float(day_bars.hist[i]))
                break
            i += 1
        mid = (box_hi + box_lo) / 2.0
        base = dict(
            date=day_bars.b["date"].iloc[0], W=W, M=M, inclusive=inclusive,
            n_flags=len(cl_flags), transitions=len(cl_flags) - 1,
            start_bar=t[first_i], last_flag_bar=t[last_i],
            seq="".join("R" if f["direction"] == "UP_RED" else "B" for f in cl_flags),
            last_dir=last_dir, box_hi=box_hi, box_lo=box_lo,
            range_pct=(box_hi - box_lo) / mid * 100.0,
            span_min=(t[last_i] - t[first_i]).total_seconds() / 60.0,
        )
        if rec is None:
            base.update(breakout=False, bo_i=None, bo_bar=None, bo_close=None,
                        bo_vol=None, b2=False, b3=False, next_outside=None,
                        hist_delta=None, hist_at_bo=None)
            consumed_until = min(i, n - 1)
        else:
            base.update(rec)
            base["bo_pct"] = ((rec["bo_close"] - box_hi) / box_hi * 100.0
                              if last_dir == "UP_RED" else
                              (box_lo - rec["bo_close"]) / box_lo * 100.0)
            base["latency_min"] = (rec["bo_bar"] - t[last_i]).total_seconds() / 60.0
            consumed_until = rec["bo_i"]
        base.setdefault("bo_pct", None)
        base.setdefault("latency_min", None)
        out.append(base)
        k = last_k + 1
    return out
