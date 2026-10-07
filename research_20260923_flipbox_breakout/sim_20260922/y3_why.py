"""왜 12:09 DOWN_BLUE 는 진입이 안 됐나 — memo 버킷 덤프."""
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
b = ctx.hynix_bars_3m
ax = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
ts = A.run("N1", ctx, ["20260922"], ax=ax)
idx = {}
for i, d in sorted(ctx.flags_by_idx.items()):
    t = pd.Timestamp(b["datetime"].iloc[i])
    if t.strftime("%Y%m%d") == "20260922":
        idx[t.strftime("%H:%M")] = (i, d.value)
print("flag bar -> p_idx:", {k: v[0] for k, v in idx.items()})
print()
for hhmm, (i, dv) in idx.items():
    print("== %s %s (p_idx=%d) ==" % (hhmm, dv, i))
    for bucket in ("base", "base_q3", "tq", "teg", "chop", "veto", "whip", "h50h", "h50r"):
        d = H._MEMO.get(bucket, {})
        hits = {k: v for k, v in d.items() if (k == i or (isinstance(k, tuple) and i in k))}
        if hits:
            for k, v in hits.items():
                s = repr(v)
                print("   %-8s %s -> %s" % (bucket, k, s[:220]))
    print()
