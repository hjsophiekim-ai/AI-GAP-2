"""Y1 — X1c 승격조건 민감도 (좁게 3종). warm memo + 앞뒤 B3 동일성. 재개 가능.

Y0_B3  : 기준 (패치 무해성 + parity)
Y1_gap : net>0 ∧ gap확대                  (= 기존 X1c)
Y2_gs  : net>0 ∧ gap확대 ∧ spread확대
Y3_ge  : net>0 ∧ gap확대 ∧ ETF추종
Y0_END : 맨 뒤 B3 재실행
전부 유예 0분(즉시 판정), 조건은 **마지막 완성봉(idx-1)** 기준. ETF 는 1분 호가(인과적).
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
OUT = HERE / "y1.pkl"
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
SH = W1["shadow"]

ctx = H.build_ctx(81)
D80 = [d for d in ctx.dates if "20260527" <= d <= "20260922"]
SEP = [d for d in D80 if d >= "20260901"]
print("80일 %s~%s · 9월 %d일" % (D80[0], D80[-1], len(SEP)), flush=True)
R.Z.FEAT = R.Z.build_features(ctx.hynix_bars_3m)
R.Z.install()

b = ctx.hynix_bars_3m.reset_index(drop=True)
date = pd.to_datetime(b["datetime"]).dt.strftime("%Y%m%d").values
ef = b["close"].ewm(span=12, adjust=False).mean()
es = b["close"].ewm(span=26, adjust=False).mean()
gap = (ef - es - (ef - es).ewm(span=9, adjust=False).mean()).values
e20 = b["close"].ewm(span=20, adjust=False).mean().values
e50 = b["close"].ewm(span=50, adjust=False).mean().values
spr = e20 - e50
dgap = np.zeros(len(b)); dspread = np.zeros(len(b))
for i in range(1, len(b)):
    if date[i] == date[i - 1]:
        dgap[i] = gap[i] - gap[i - 1]
        dspread[i] = spr[i] - spr[i - 1]
PF = {"dgap": dgap, "dspread": dspread}

state = pickle.load(open(OUT, "rb")) if OUT.exists() else {"runs": {}, "logs": {}}
state["dates"] = D80
state["shadow"] = SH


def is_ar1(day, rec_at):
    k = rec_at.astimezone(H.KST)
    return (k.strftime("%Y%m%d"), k.strftime("%H:%M")) in R.Z.RELAXED_KEYS


def gx(need=None):
    g = {"on": SH, "is_ar1": is_ar1, "log": [],
         "exit": {"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20}}
    if need is not None:
        g["grace"] = {"min": 0, "need": need, "consec": 1, "feat": PF}
    return g


BAT = [("Y0_B3", None), ("Y1_gap", ["gap"]), ("Y2_gap_spread", ["gap", "spread"]),
       ("Y3_gap_etf", ["gap", "etf"]), ("Y0_B3_END", None)]

first = True
t00 = time.time()
for tag, need in BAT:
    if tag in state["runs"]:
        print("%-16s (건너뜀)" % tag, flush=True); first = False; continue
    if first:
        for k in list(H._MEMO):
            H._MEMO[k] = {}
        A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
        first = False
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    g = gx(need)
    t = time.time()
    ts = A.run("N1", ctx, D80, ax=R.AX, gx=g)
    m, ms = summarize(ts, D80), summarize(ts, SEP)
    npr = sum(1 for x in ts if x.get("gx_promoted"))
    print("%-16s 80일 %3d거래 %9.4f | 9월 %2d거래 %7.4f PF %.3f MDD %6.2f WR %.1f%% | 승격 %d (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], ms["trades"], ms["compound_pct"],
             ms["pf"], ms["mdd_pct"], ms["win_rate_pct"], npr, time.time() - t), flush=True)
    state["runs"][tag] = ts
    state["logs"][tag] = g["log"]
    pickle.dump(state, open(OUT, "wb"))


def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8)) for t in ts)


if "Y0_B3" in state["runs"] and "Y0_B3_END" in state["runs"]:
    print("\n[warm memo] Y0_B3 앞/뒤 동일:", sig(state["runs"]["Y0_B3"]) == sig(state["runs"]["Y0_B3_END"]))
print("[무해성] Y0_B3 vs 이전 B3:", sig(state["runs"]["Y0_B3"]) == sig(W1["runs"]["B_B3"]))
W6 = pickle.load(open(HERE / "w6.pkl", "rb"))
if "Y1_gap" in state["runs"] and "X1c_snap0" in W6["runs"]:
    print("[재현] Y1_gap vs 기존 X1c:", sig(state["runs"]["Y1_gap"]) == sig(W6["runs"]["X1c_snap0"]))
print("총 %.0f분 · saved y1.pkl" % ((time.time() - t00) / 60))
