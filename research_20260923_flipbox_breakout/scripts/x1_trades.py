"""Emit N1+C1 trades with entry/exit timestamps + H50 HOLD state. READ-ONLY."""
import sys, time, pickle
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
HERE = Path(__file__).resolve().parent
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache"
H._CTX_CACHE = HERE / "_ctx_B.pkl"
H._MEMO_PATH = HERE / "_memo_B.pkl"
import axlib as A
from common import summarize
import pandas as pd

H.load_memo()
A.PROD_BASE = H._MEMO["base"]
ctx = H.build_ctx(78)
print("ctx", len(ctx.dates), ctx.dates[0], ctx.dates[-1], flush=True)
ax = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
t0 = time.time()
ts = A.run("N1", ctx, ctx.dates, ax=ax)
m = summarize(ts, ctx.dates)
print("N1+C1 trades=%d  (%.0fs)" % (len(ts), time.time() - t0), flush=True)
print(m, flush=True)
print("TYPE", type(ts[0]))
if isinstance(ts[0], dict):
    print("KEYS", sorted(ts[0].keys()))
    df = pd.DataFrame(ts)
else:
    from dataclasses import asdict
    df = pd.DataFrame([asdict(t) for t in ts])
df.to_csv(HERE / "n1c1_trades.csv", index=False)
print(df.columns.tolist())
print("written", HERE / "n1c1_trades.csv")
