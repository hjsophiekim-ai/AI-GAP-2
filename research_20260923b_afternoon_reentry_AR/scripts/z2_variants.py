"""Z2 — 오후 동일방향 차단 완화 변형 비교. READ-ONLY."""
import sys, pickle, time; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
import axlib as A, hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
ce.CACHE_DIR = HERE / "cache922"
H._CTX_CACHE = HERE / "_ctx_922.pkl"; H._MEMO_PATH = HERE / "_memo_922.pkl"
H.load_memo(); A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
import zrelax as Z
from common import summarize, pnl

ctx = H.build_ctx(78)
Z.FEAT = Z.build_features(ctx.hynix_bars_3m)
Z.install()
AX = {"decide": 99.0, "strong": {}, "weak": {}, "pp": {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}}

z1 = pickle.load(open(HERE / "z1.pkl", "rb"))
blk = [e for e in z1["events"] if e.get("reason") == "TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND"]
print("=== 차단된 13건의 의사결정 시점 특징 ===")
rows = []
for e in blk:
    t = pd.Timestamp(e["at"])
    f = Z.FEAT.get((e["date"], t.strftime("%H:%M")))
    s = 1.0 if e["direction"] == "UP_RED" else -1.0
    if f is None:
        rows.append(dict(date=e["date"], at=t.strftime("%H:%M"), dir=e["direction"], feat="NO_FEAT")); continue
    rows.append(dict(date=e["date"], at=t.strftime("%H:%M"), dir="R" if s > 0 else "B",
                     ema_signed=s * f["ema_gap_pct"], vwap_signed=s * f["vwap_dev_pct"],
                     extreme=f["new_hi"] if s > 0 else f["new_lo"],
                     move_signed=s * f["from_open_pct"], close=f["close"]))
F = pd.DataFrame(rows)
pd.set_option("display.width", 220)
print(F.to_string(index=False, float_format=lambda v: f"{v:+.3f}"))

GATES = {
    "R0_all":        lambda f: True,
    "R1_trend":      lambda f: f["ema_signed"] > 0,
    "R2_extreme":    lambda f: bool(f["extreme"]),
    "R3_vwap":       lambda f: f["vwap_signed"] > 0,
    "R4_trend+ext":  lambda f: f["ema_signed"] > 0 and bool(f["extreme"]),
    "R5_trend+vwap": lambda f: f["ema_signed"] > 0 and f["vwap_signed"] > 0,
}
res = {}
Z.set_gate(None)
t0 = time.time(); base = A.run("N1", ctx, ctx.dates, ax=AX); H.save_memo()
mb = summarize(base, ctx.dates)
print("\nBASE  n=%d 복리=%.4f PF=%.4f MDD=%.3f 월%.2f%% (%.0fs)" % (
    mb["trades"], mb["compound_pct"], mb["pf"], mb["mdd_pct"], mb["monthly_pct"], time.time() - t0), flush=True)
res["BASE"] = (base, mb, [])
for name, g in GATES.items():
    Z.set_gate(g)
    t0 = time.time(); ts = A.run("N1", ctx, ctx.dates, ax=AX); H.save_memo()
    m = summarize(ts, ctx.dates)
    hits = list(Z.HITS)
    print("%-14s n=%d(+%d) 복리=%.4f (%+.4f) PF=%.4f MDD=%.3f 월%.2f%% 완화열림=%d (%.0fs)" % (
        name, m["trades"], m["trades"] - mb["trades"], m["compound_pct"],
        m["compound_pct"] - mb["compound_pct"], m["pf"], m["mdd_pct"], m["monthly_pct"],
        len(hits), time.time() - t0), flush=True)
    res[name] = (ts, m, hits)
pickle.dump({k: (v[1], v[2]) for k, v in res.items()}, open(HERE / "z2_summary.pkl", "wb"))
pickle.dump({k: v[0] for k, v in res.items()}, open(HERE / "z2_trades.pkl", "wb"))
