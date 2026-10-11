"""W6 — GRACE-WINDOW 배터리. warm memo + 앞뒤 B3 동일성 검사. 재개 가능.

X0_B3      : B3 그대로 (기준 + 패치 무해성)
X1c        : grace min=0 — 유예 없는 **인과봉(idx-1)** snapshot 승격 (gap 확대)
G1         : grace 6분, gap 확대 시 승격
G2         : grace 9분, gap 확대 시 승격
G3         : grace 6분, gap+spread 둘 다
G4         : grace 6분, 직전 2개 완성봉 연속 gap 확대
X0_B3_END  : 맨 뒤 B3 재실행 (warm memo 안전성)
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
OUT = HERE / "w6.pkl"
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


def gx(grace=None):
    g = {"on": SH, "is_ar1": is_ar1, "log": [],
         "exit": {"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20}}
    if grace is not None:
        gr = dict(grace); gr["feat"] = PF
        g["grace"] = gr
    return g


BAT = [
    ("X0_B3", None),
    ("X1c_snap0", {"min": 0, "need": ["gap"], "consec": 1}),
    ("G1_grace6", {"min": 6, "need": ["gap"], "consec": 1}),
    ("G2_grace9", {"min": 9, "need": ["gap"], "consec": 1}),
    ("G3_gap_spread6", {"min": 6, "need": ["gap", "spread"], "consec": 1}),
    ("G4_consec2_6", {"min": 6, "need": ["gap"], "consec": 2}),
    ("X0_B3_END", None),
]

first = True
t00 = time.time()
for tag, gr in BAT:
    if tag in state["runs"]:
        print("%-16s (건너뜀)" % tag, flush=True)
        first = False
        continue
    if first:
        for k in list(H._MEMO):
            H._MEMO[k] = {}
        A.PROD_BASE = H._MEMO["base"]
        A.Q3_BASE = {}
        first = False
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    g = gx(gr)
    t = time.time()
    ts = A.run("N1", ctx, D80, ax=R.AX, gx=g)
    m, ms = summarize(ts, D80), summarize(ts, SEP)
    npr = sum(1 for x in ts if x.get("gx_promoted"))
    ngr = sum(1 for x in ts if x.get("gx_grace"))
    print("%-16s 80일 %3d거래 %9.4f | 9월 %2d거래 %7.4f PF %.3f MDD %6.2f WR %.1f%% "
          "| grace %d 승격 %d (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], ms["trades"], ms["compound_pct"],
             ms["pf"], ms["mdd_pct"], ms["win_rate_pct"], ngr, npr, time.time() - t), flush=True)
    state["runs"][tag] = ts
    state["logs"][tag] = g["log"]
    pickle.dump(state, open(OUT, "wb"))


def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8)) for t in ts)


if "X0_B3" in state["runs"] and "X0_B3_END" in state["runs"]:
    print("\n[warm memo 안전성] X0_B3 앞/뒤 동일:",
          sig(state["runs"]["X0_B3"]) == sig(state["runs"]["X0_B3_END"]), flush=True)
if "X0_B3" in state["runs"]:
    print("[패치 무해성] X0_B3 vs 이전 B3(w1):",
          sig(state["runs"]["X0_B3"]) == sig(W1["runs"]["B_B3"]), flush=True)
print("총 %.0f분 · saved w6.pkl" % ((time.time() - t00) / 60))
