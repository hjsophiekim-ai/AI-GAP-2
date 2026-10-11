"""W3 — MAX-HOLD 연장 배터리 (80일 ctx, 9월 평가). 재개 가능.

X0 = B3 재현(연장 없음) — 패치 무해성 확인용. w1.pkl 의 B_B3 와 거래집합이 같아야 한다.
X1 NG   promote  : net>0 ∧ gap확대 -> 래더 승격(무기한)
X2 NGE  promote  : X1 + ETF 추종
X3 NG   extend×1 : +20분 1회
X4 NG   extend×3 : +20분씩 최대 3회(매회 재평가, 최대 80분)
X5 S3   promote  : net>0 ∧ (gap/spread/etf/vwap/noopp 중 3개 이상)
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
from app.trading.macd2.models import Direction

ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx81.pkl"
H._MEMO_PATH = HERE / "_memo81.pkl"
OUT = HERE / "w3.pkl"
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
SH = W1["shadow"]

ctx = H.build_ctx(81)
ALL = list(ctx.dates)
D80 = [d for d in ALL if "20260527" <= d <= "20260922"]
SEP = [d for d in D80 if d >= "20260901"]
print("80일 %s~%s · 9월 %d일" % (D80[0], D80[-1], len(SEP)), flush=True)
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
flags = ctx.flags_by_idx
no_up = np.ones(len(bars), bool); no_dn = np.ones(len(bars), bool)
for i in range(len(bars)):
    rec = [k for k in flags if i - 10 <= k <= i and date[k] == date[i]]
    no_up[i] = not any(flags[k] == Direction.DOWN_BLUE for k in rec)
    no_dn[i] = not any(flags[k] == Direction.UP_RED for k in rec)
PF = {"dgap": dgap, "dspread": dspread, "vwapd": vwapd,
      "no_opp_up": no_up, "no_opp_dn": no_dn}

state = pickle.load(open(OUT, "rb")) if OUT.exists() else {"runs": {}, "logs": {}}
state["dates"] = D80
state["shadow"] = SH


def is_ar1(day, rec_at):
    k = rec_at.astimezone(H.KST)
    return (k.strftime("%Y%m%d"), k.strftime("%H:%M")) in R.Z.RELAXED_KEYS


def gx(ext=None):
    g = {"on": SH, "is_ar1": is_ar1, "log": [],
         "exit": {"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20}}
    if ext:
        e = dict(ext); e["feat"] = PF
        g["ext"] = e
    return g


BAT = [
    ("X0_B3", None),
    ("X1_NG_prom", {"rule": "NG", "mode": "promote"}),
    ("X2_NGE_prom", {"rule": "NGE", "mode": "promote"}),
    ("X3_NG_ext1", {"rule": "NG", "mode": "extend", "max_ext": 1, "add": 20}),
    ("X4_NG_ext3", {"rule": "NG", "mode": "extend", "max_ext": 3, "add": 20}),
    ("X5_S3_prom", {"rule": "S3", "mode": "promote"}),
]


def hard_reset():
    for k in list(H._MEMO):
        H._MEMO[k] = {}
    A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}


t00 = time.time()
for tag, ext in BAT:
    if tag in state["runs"]:
        print("%-12s (건너뜀)" % tag, flush=True)
        continue
    hard_reset()
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    g = gx(ext)
    t = time.time()
    ts = A.run("N1", ctx, D80, ax=R.AX, gx=g)
    m, ms = summarize(ts, D80), summarize(ts, SEP)
    nprom = sum(1 for x in ts if x.get("gx_promoted"))
    next_ = sum(1 for x in ts if int(x.get("gx_ext_count") or 0) > 0)
    print("%-12s 80일 %3d거래 %9.4f | 9월 %2d거래 %7.4f PF %.3f MDD %6.2f WR %.1f%% "
          "| 승격 %d 연장 %d (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], ms["trades"], ms["compound_pct"],
             ms["pf"], ms["mdd_pct"], ms["win_rate_pct"], nprom, next_, time.time() - t),
          flush=True)
    state["runs"][tag] = ts
    state["logs"][tag] = g["log"]
    pickle.dump(state, open(OUT, "wb"))


def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8)) for t in ts)


if "X0_B3" in state["runs"]:
    print("\n[무해성] X0_B3 vs w1.B_B3 거래집합 동일:",
          sig(state["runs"]["X0_B3"]) == sig(W1["runs"]["B_B3"]), flush=True)
print("총 %.0f분 · saved w3.pkl" % ((time.time() - t00) / 60))
