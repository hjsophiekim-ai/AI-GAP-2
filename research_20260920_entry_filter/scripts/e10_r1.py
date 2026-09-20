"""E10 — R1(과열 진입 제외) 임계값 민감도 + 정밀검증.

R1: 진입 판정봉에서 (EMA20-EMA50)/close*100 을 보유방향 부호로 본 값이
임계값을 넘으면 진입하지 않는다. 완성봉만 쓰고 미래값은 없다.
청산(N1 래더+C1), 슬롯 3회/일, 세션창 전부 불변.
"""
import sys, pickle, time
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
A._Q3_PATH = A.HERE / "_q3base.pkl"
from common import summarize, compound, excl_topn
from app.trading.macd2 import config
from app.trading.macd2.models import Direction
import hengine5 as H
import axval as V

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
BARS = c.hynix_bars_3m
C1 = {"decide": 99.0, "strong": {}, "weak": {},
      "pp": {"arm": float(config.C1_ARM_MFE_PCT),
             "give": float(config.C1_GIVEBACK_PCT), "cond": "gap_neg"}}
H.D_VARIANTS["N1PROD"] = {}
TB = H.snap_table(BARS)


def e2050(i, d):
    sgn = 1.0 if d == Direction.UP_RED else -1.0
    cl = float(TB["close"][i])
    return (float(TB["e20"][i] - TB["e50"][i]) * sgn / cl * 100.0) if cl > 0 else 0.0


def run(thr):
    g = None if thr is None else (lambda x: "R1" if e2050(int(x["idx"]), x["direction"]) > thr else None)
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD", ax=C1, gate=g)
    finally:
        H._MEMO["base"] = _pb


BASE = run(None)
mB = summarize(BASE, D); mB30 = summarize([t for t in BASE if t["date"] in S30], W30)
assert abs(mB["compound_pct"] - 438.6268) < 1e-3
key = lambda t: (t["date"], t["entry_time"])
BK = {key(t): t for t in BASE}
RUN8 = sorted(k for k, t in BK.items() if t["peak_net_pct"] >= 8.0)
print(f"기준 C1: n={mB['trades']} 78d={mB['compound_pct']:.4f} 30d={mB30['compound_pct']:.2f} "
      f"PF={mB['pf']:.3f} MDD={mB['mdd_pct']:.2f}")

print("\n" + "=" * 152)
print("1. R1 임계값 민감도 (plateau 확인) — 값이 작을수록 더 많이 제거")
print("=" * 152)
print(f"{'임계값':10s} {'n':>4s} {'78d':>9s} {'ΔC1':>8s} {'30d':>7s} {'Δ30':>7s} {'PF':>6s} "
      f"{'MDD':>7s} {'-T1':>7s} {'-T3':>7s} {'-T10':>7s} {'앞39':>7s} {'뒤39':>7s} "
      f"{'5분할':>26s} {'WF6':>6s} {'제거':>4s} {'신규':>4s} {'run8':>4s}")
OUT = {"base": BASE}
for thr in (0.2, 0.4, 0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5, 2.0, 3.0):
    ts = run(thr); OUT[thr] = ts
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(f"R1 {thr}", ts, BASE, D)
    ka = {key(t): t for t in ts}
    dmg = sum(1 for x in RUN8 if x in ka and ka[x]["net_pct"] - BK[x]["net_pct"] < -1e-9)
    k5 = " ".join(f"{x:+5.1f}" for x in r["k5"])
    print(f"{thr:10.2f} {m['trades']:4d} {m['compound_pct']:9.3f} "
          f"{m['compound_pct']-mB['compound_pct']:+8.2f} {m30['compound_pct']:7.2f} "
          f"{m30['compound_pct']-mB30['compound_pct']:+7.2f} {m['pf']:6.3f} {m['mdd_pct']:7.2f} "
          f"{excl_topn(ts,D,1)-excl_topn(BASE,D,1):+7.2f} "
          f"{excl_topn(ts,D,3)-excl_topn(BASE,D,3):+7.2f} "
          f"{excl_topn(ts,D,10)-excl_topn(BASE,D,10):+7.2f} "
          f"{compound(ts,D[:39])-compound(BASE,D[:39]):+7.2f} "
          f"{compound(ts,D[39:])-compound(BASE,D[39:]):+7.2f} {k5:>26s} "
          f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s} "
          f"{r['only_b']:4d} {r['only_a']:4d} {dmg:4d}", flush=True)

pickle.dump({"out": OUT, "dates": D}, open(A.HERE / "e10.pkl", "wb"))
print("\n저장: e10.pkl")
