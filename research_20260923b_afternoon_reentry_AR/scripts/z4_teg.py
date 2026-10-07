import sys, pickle; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"; H._MEMO_PATH = HERE / "_memo_922.pkl"
for k in list(H._MEMO): H._MEMO[k] = {}
A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
import zrelax as Z
ctx = H.build_ctx(78)
Z.FEAT = Z.build_features(ctx.hynix_bars_3m); Z.install(); Z.set_gate(lambda f: True)
AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
ev = []
ts = A.run("N1", ctx, ["20260922"], ax=AX, events=ev)
print("R0(전면완화) 9/22 거래", len(ts))
d = pd.DataFrame([e for e in ev if e.get("kind") == "DECISION"])
d["at"] = pd.to_datetime(d["at"]).dt.strftime("%H:%M")
pd.set_option("display.width", 260); pd.set_option("display.max_colwidth", 40)
print(d[["at","direction","approved","reason","slot","session","flat"]].to_string(index=False))
b = ctx.hynix_bars_3m
i12 = None
for i in range(len(b)):
    t = pd.Timestamp(b["datetime"].iloc[i])
    if t.strftime("%Y%m%d %H:%M") == "20260922 12:09": i12 = i
print("\n12:09 flag bar p_idx =", i12)
for bucket in ("teg","tq","chop","veto"):
    v = H._MEMO.get(bucket, {}).get(i12)
    if v is not None:
        print("\n[%s]" % bucket)
        for attr in ("approved","conditions","metrics","block_reason","passed_count","required"):
            if hasattr(v, attr): print("   %-14s %s" % (attr, getattr(v, attr)))
