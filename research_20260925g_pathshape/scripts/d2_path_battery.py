"""D2 — 경로형태 TP-RESCUE 배터리. warm memo + 앞뒤 B3 동일성. 재개 가능.

A_B3  : 기준 (패치 무해성)
B_Y3  : B3 + Y3 (max-hold 승격)
P1    : B + rescue(+1% 도달 ≤ 12분)
P2    : B + rescue(≤ 12분 ∧ EMA20 이격z ≥ 2.0)
P3    : B + rescue(+1% 도달 ≤ 6분)
A_END : 맨 뒤 B3

임계는 관측 최솟값이 아니라 **둥근 값**(6분=2봉 / 12분=4봉 / z 2.0)으로 잡았다.
rescue 구조는 기존과 동일: 통과 시 50% 익절 + 잔량 N1/C1 승격.
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
OUT = HERE / "d2.pkl"
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
Z2 = pickle.load(open(HERE / "z2.pkl", "rb"))
SH = W1["shadow"]

ctx = H.build_ctx(81)
D80 = [d for d in ctx.dates if "20260527" <= d <= "20260922"]
SEP = [d for d in D80 if d >= "20260901"]
print("80일 %s~%s · 9월 %d일" % (D80[0], D80[-1], len(SEP)), flush=True)
R.Z.FEAT = R.Z.build_features(ctx.hynix_bars_3m)
R.Z.install()

b = ctx.hynix_bars_3m.reset_index(drop=True)
date = pd.to_datetime(b["datetime"]).dt.strftime("%Y%m%d").values
close = b["close"].astype(float).values
ef = b["close"].ewm(span=12, adjust=False).mean()
es = b["close"].ewm(span=26, adjust=False).mean()
gap = (ef - es - (ef - es).ewm(span=9, adjust=False).mean()).values
e20 = b["close"].ewm(span=20, adjust=False).mean().values
e50 = b["close"].ewm(span=50, adjust=False).mean().values
spr = e20 - e50
dgap = np.zeros(len(b)); dspread = np.zeros(len(b)); ret = np.zeros(len(b))
for i in range(1, len(b)):
    if date[i] == date[i - 1]:
        dgap[i] = gap[i] - gap[i - 1]
        dspread[i] = spr[i] - spr[i - 1]
        ret[i] = (close[i] - close[i - 1]) / close[i - 1] * 100
vol40 = pd.Series(ret).shift(1).rolling(40, min_periods=20).std().values
with np.errstate(invalid="ignore", divide="ignore"):
    ema20z_up = np.where(np.isfinite(vol40) & (vol40 > 0),
                         ((close - e20) / close * 100.0) / vol40, np.nan)
PF = {"dgap": dgap, "dspread": dspread, "ema20z_up": ema20z_up}

state = pickle.load(open(OUT, "rb")) if OUT.exists() else {"runs": {}, "logs": {}}
state["dates"] = D80
state["shadow"] = SH


def is_ar1(day, rec_at):
    k = rec_at.astimezone(H.KST)
    return (k.strftime("%Y%m%d"), k.strftime("%H:%M")) in R.Z.RELAXED_KEYS


def gx(path=None):
    g = {"on": SH, "is_ar1": is_ar1, "log": [],
         "exit": {"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20},
         "grace": {"min": 0, "need": ["gap", "etf"], "consec": 1, "feat": PF}}
    if path is not None:
        g["rescue"] = {"rule": "PATH", "ratio": 0.5, "feat": PF, "path": path}
    return g


BAT = [("A_B3", "b3"), ("B_Y3", None),
       ("P1_fast12", {"max_min": 12}),
       ("P2_fast12_ez2", {"max_min": 12, "ema20z": 2.0}),
       ("P3_fast6", {"max_min": 6}),
       ("A_END", "b3")]

first = True
t00 = time.time()
for tag, cfg in BAT:
    if tag in state["runs"]:
        print("%-14s (건너뜀)" % tag, flush=True); first = False; continue
    if first:
        for k in list(H._MEMO):
            H._MEMO[k] = {}
        A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
        first = False
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    if cfg == "b3":
        g = {"on": SH, "is_ar1": is_ar1, "log": [],
             "exit": {"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20}}
    else:
        g = gx(cfg)
    t = time.time()
    ts = A.run("N1", ctx, D80, ax=R.AX, gx=g)
    m, ms = summarize(ts, D80), summarize(ts, SEP)
    npr = sum(1 for x in ts if x.get("gx_promoted"))
    nrs = sum(1 for x in ts if x.get("gx_rescue"))
    print("%-14s 80일 %3d거래 %9.4f | 9월 %2d거래 %7.4f PF %.3f MDD %6.2f WR %.1f%% "
          "| 승격 %d 구조 %d (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], ms["trades"], ms["compound_pct"],
             ms["pf"], ms["mdd_pct"], ms["win_rate_pct"], npr, nrs, time.time() - t), flush=True)
    state["runs"][tag] = ts
    state["logs"][tag] = g["log"]
    pickle.dump(state, open(OUT, "wb"))


def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8)) for t in ts)


print("\n[무해성] A_B3 vs z2.A_B3:", sig(state["runs"]["A_B3"]) == sig(Z2["runs"]["A_B3"]))
if "A_END" in state["runs"]:
    print("[warm memo] A_B3 앞/뒤:", sig(state["runs"]["A_B3"]) == sig(state["runs"]["A_END"]))
if "B_Y3" in state["runs"]:
    print("[재현] B_Y3 vs z2.B_Y3:", sig(state["runs"]["B_Y3"]) == sig(Z2["runs"]["B_Y3"]))
print("총 %.0f분 · saved d2.pkl" % ((time.time() - t00) / 60))
