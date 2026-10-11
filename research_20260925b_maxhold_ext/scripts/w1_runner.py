"""W1 — RUNNER PROMOTION 배터리 (80일 ctx, 9월 평가). 재개 가능.

승격 판정은 +1.0% 를 **처음 도달한 틱**에서 그 시점까지의 값만 쓴다. MFE 사후값 미사용.
"""
import pickle, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
import axlib as A
import rlib as R
from common import summarize

ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx81.pkl"
H._MEMO_PATH = HERE / "_memo81.pkl"
OUT = HERE / "w1.pkl"

ctx = H.build_ctx(81)
ALL = list(ctx.dates)
D80 = [d for d in ALL if "20260527" <= d <= "20260922"]
assert len(D80) == 80, len(D80)
SEP = [d for d in D80 if d >= "20260901"]
print("80일 %s~%s · 9월 %d일 %s" % (D80[0], D80[-1], len(SEP), SEP), flush=True)
R.Z.FEAT = R.Z.build_features(ctx.hynix_bars_3m)
R.Z.install()

bars = ctx.hynix_bars_3m.reset_index(drop=True)
close = bars["close"].astype(float).values
high = bars["high"].astype(float).values
low = bars["low"].astype(float).values
vol = bars["volume"].astype(float).values
date = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d").values
ef = bars["close"].ewm(span=12, adjust=False).mean()
es = bars["close"].ewm(span=26, adjust=False).mean()
gap = (ef - es - (ef - es).ewm(span=9, adjust=False).mean()).values
e20 = bars["close"].ewm(span=20, adjust=False).mean().values
e50 = bars["close"].ewm(span=50, adjust=False).mean().values
spread = e20 - e50
dgap = np.zeros(len(bars)); dspread = np.zeros(len(bars))
for i in range(1, len(bars)):
    if date[i] == date[i - 1]:
        dgap[i] = gap[i] - gap[i - 1]
        dspread[i] = spread[i] - spread[i - 1]
vwap = np.full(len(bars), np.nan)
cur, pv, pvv = None, 0.0, 0.0
tp3 = (high + low + close) / 3.0
for i in range(len(bars)):
    if date[i] != cur:
        cur, pv, pvv = date[i], 0.0, 0.0
    v = vol[i] if vol[i] > 0 else 1.0
    pv += tp3[i] * v; pvv += v
    vwap[i] = pv / pvv
vwapd = (close - vwap) / close * 100.0
from app.trading.macd2.models import Direction
flags = ctx.flags_by_idx
no_opp_up = np.ones(len(bars), bool)
no_opp_dn = np.ones(len(bars), bool)
for i in range(len(bars)):
    lo = i - 10
    rec = [k for k in flags if lo <= k <= i and date[k] == date[i]]
    no_opp_up[i] = not any(flags[k] == Direction.DOWN_BLUE for k in rec)
    no_opp_dn[i] = not any(flags[k] == Direction.UP_RED for k in rec)
PF = {"dgap": dgap, "dspread": dspread, "vwapd": vwapd,
      "no_opp_up": no_opp_up, "no_opp_dn": no_opp_dn}

state = pickle.load(open(OUT, "rb")) if OUT.exists() else {"runs": {}, "logs": {}}
state["dates"] = D80


def hard_reset():
    for k in list(H._MEMO):
        H._MEMO[k] = {}
    A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}


def run(tag, gx=None):
    hard_reset()
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    t = time.time()
    ts = A.run("N1", ctx, D80, ax=R.AX, **({"gx": gx} if gx else {}))
    m = summarize(ts, D80)
    ms = summarize(ts, SEP)
    print("%-10s 80일 %3d거래 %9.4f | 9월 %2d거래 %7.4f PF %.3f MDD %6.2f WR %.1f%% (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], ms["trades"], ms["compound_pct"],
             ms["pf"], ms["mdd_pct"], ms["win_rate_pct"], time.time() - t), flush=True)
    return ts


# ── BASE (shadow 산출용) ──
if "A_BASE" not in state["runs"]:
    state["runs"]["A_BASE"] = run("A_BASE")
    pickle.dump(state, open(OUT, "wb"))
B = pd.DataFrame(state["runs"]["A_BASE"]).sort_values("exit_time").reset_index(drop=True)

if "shadow" not in state:
    extn = pd.to_datetime(B.exit_time, utc=True).dt.tz_localize(None).values
    h50v = B.h50_held.astype(float).values
    tp1v = B.tp1_hit.astype(float).values
    rec = pd.to_datetime(bars["datetime"])
    rec = (rec.dt.tz_convert("UTC").dt.tz_localize(None) if rec.dt.tz is not None
           else rec) + pd.Timedelta(minutes=3)
    SH = np.zeros(len(bars), bool)
    for i in range(len(bars)):
        n = int(np.searchsorted(extn, np.datetime64(rec.iloc[i]), side="left"))
        if n >= 10:
            SH[i] = (h50v[n - 10:n].mean() >= 0.40) and (tp1v[n - 10:n].mean() <= 0.20)
    state["shadow"] = SH
    pickle.dump(state, open(OUT, "wb"))
SH = state["shadow"]
t2 = pd.DataFrame({"d": date, "on": SH}).groupby("d").on.mean()
print("SHADOW: 9월 ON %.1f%% · 비9월 ON %.1f%%"
      % (100 * t2[[d for d in t2.index if d in set(SEP)]].mean(),
         100 * t2[[d for d in t2.index if d in set(D80) and d < "20260901"]].mean()), flush=True)


def is_ar1(day, rec_at):
    k = rec_at.astimezone(H.KST)
    return (k.strftime("%Y%m%d"), k.strftime("%H:%M")) in R.Z.RELAXED_KEYS


def gx(rule=None):
    g = {"on": SH, "is_ar1": is_ar1, "log": [],
         "exit": {"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20}}
    if rule:
        g["prom"] = {"rule": rule, "feat": PF}
    return g


BAT = [("B_B3", None), ("C_R1", "R1"), ("D_R2", "R2"), ("E_R3", "R3"),
       ("F_R4", "R4"), ("G_S3", "S3"), ("H_S4", "S4")]
for tag, rule in BAT:
    if tag in state["runs"]:
        print("%-10s (건너뜀)" % tag, flush=True)
        continue
    g = gx(rule)
    state["runs"][tag] = run(tag, g)
    state["logs"][tag] = g["log"]
    pickle.dump(state, open(OUT, "wb"))
print("saved w1.pkl")
