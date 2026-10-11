"""Z1 — 78일 전수: SAME_DIRECTION_AFTERNOON_2ND 로 차단된 후보 수집. READ-ONLY."""
import sys, pickle; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"; H._MEMO_PATH = HERE / "_memo_922.pkl"
H.load_memo(); A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
ctx = H.build_ctx(78)
print("ctx %d일 %s~%s" % (len(ctx.dates), ctx.dates[0], ctx.dates[-1]), flush=True)
AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}
ev = []
ts = A.run("N1", ctx, ctx.dates, ax=AX, events=ev)
H.save_memo()
from common import summarize
print("BASE N1+C1:", summarize(ts, ctx.dates), flush=True)
E = pd.DataFrame([e for e in ev if e.get("kind") == "DECISION"])
E["at"] = pd.to_datetime(E["at"])
blk = E[E.reason == "TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND"]
print("\n차단 건수: %d / %d일" % (len(blk), blk.date.nunique()))
print(blk[["date", "at", "direction", "slot", "flat"]].assign(
    at=blk["at"].dt.strftime("%H:%M")).to_string(index=False))
print("\n전체 거절사유 분포:")
print(E[~E.approved].reason.value_counts().to_string())
pickle.dump({"events": ev, "trades": ts, "dates": ctx.dates}, open(HERE / "z1.pkl", "wb"))
