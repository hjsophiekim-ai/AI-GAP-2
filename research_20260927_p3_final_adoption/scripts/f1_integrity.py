"""F1 - 최종 무결성: BASE cold 2회 동일 + no-op gx 훅 diff 0."""
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
OUT = HERE / "f1.pkl"
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
SH = W1["shadow"]
ctx = H.build_ctx(81)
D80 = [d for d in ctx.dates if "20260527" <= d <= "20260922"]
print("ctx 81일 %s~%s · 대상 80일 %s~%s" % (ctx.dates[0], ctx.dates[-1], D80[0], D80[-1]), flush=True)
R.Z.FEAT = R.Z.build_features(ctx.hynix_bars_3m); R.Z.install()
st = pickle.load(open(OUT, "rb")) if OUT.exists() else {"runs": {}}

def is_ar1(day, rec_at):
    k = rec_at.astimezone(H.KST)
    return (k.strftime("%Y%m%d"), k.strftime("%H:%M")) in R.Z.RELAXED_KEYS

def hard():
    for k in list(H._MEMO): H._MEMO[k] = {}
    A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}

def run(tag, gx=None, cold=True):
    if cold: hard()
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    t = time.time()
    ts = A.run("N1", ctx, D80, ax=R.AX, **({"gx": gx} if gx else {}))
    m = summarize(ts, D80)
    print("%-10s %3d거래 복리 %9.4f (%.0fs)" % (tag, m["trades"], m["compound_pct"], time.time()-t), flush=True)
    return ts

for tag, gx_, cold in (("BASE2", None, True),
                       ("MARK", {"on": SH, "is_ar1": is_ar1, "log": []}, False)):
    if tag in st["runs"]:
        print("%-10s (건너뜀)" % tag, flush=True); continue
    st["runs"][tag] = run(tag, gx_, cold)
    pickle.dump(st, open(OUT, "wb"))

def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8), round(float(t["w1a"]), 8)) for t in ts)

B1 = W1["runs"]["A_BASE"]
print("\n[BASE cold 2회 동일]", sig(B1) == sig(st["runs"]["BASE2"]))
print("[no-op gx 훅 diff 0]", sig(B1) == sig(st["runs"]["MARK"]))
