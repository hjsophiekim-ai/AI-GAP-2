import sys; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"; H._MEMO_PATH = HERE / "_memo_922.pkl"
for k in list(H._MEMO): H._MEMO[k] = {}
A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
ctx = H.build_ctx(78)
ax = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
ev = []
ts = A.run("N1", ctx, ["20260922"], ax=ax, events=ev)
print("trades", len(ts))
d = pd.DataFrame([e for e in ev if e.get("date") == "20260922"])
pd.set_option("display.width", 300); pd.set_option("display.max_colwidth", 44)
if len(d):
    d["at"] = pd.to_datetime(d["at"]).dt.strftime("%H:%M")
    cols = [c for c in ("at","kind","direction","approved","reason","slot","session","flat") if c in d.columns]
    print(d[cols].to_string(index=False))
