"""S3 — '앞 슬롯 우선배분' 사다리. 총 일일노출을 3.0 으로 고정한 재배분만 본다.

production 사이징(W1a): MIN 0.25 / MAX 1.50 / 일일노출 상한 3.00 /
chop 0.80 / 손절후 1.20. 상한 3.00 이 있으므로 (1.2,1.2,1.2) 는 실제로
(1.2,1.2,0.6) 이 되어 총 3.0 — 기준(1,1,1)과 총노출이 같다.
진입집합/청산/3회 한도 전부 불변.
"""
import sys, pickle, time
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
A._Q3_PATH = A.HERE / "_q3base.pkl"
from common import summarize, compound, excl_topn
from app.trading.macd2 import config
import hengine5 as H
import axval as V

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
C1 = {"decide": 99.0, "strong": {}, "weak": {},
      "pp": {"arm": float(config.C1_ARM_MFE_PCT),
             "give": float(config.C1_GIVEBACK_PCT), "cond": "gap_neg"}}
H.D_VARIANTS["N1PROD"] = {}


def run(sm=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, slot_mult=sm)
    finally:
        H._MEMO["base"] = _pb


BASE = run()
mB = summarize(BASE, D); mB30 = summarize([t for t in BASE if t["date"] in S30], W30)
assert abs(mB["compound_pct"] - 438.6268) < 1e-3
key = lambda t: (t["date"], t["entry_time"])
BK = {key(t): t for t in BASE}
expB = pd.Series([float(t["w1a"]) for t in BASE]).sum()
print(f"기준 C1: n={mB['trades']} 78d={mB['compound_pct']:.4f} 30d={mB30['compound_pct']:.2f} "
      f"PF={mB['pf']:.3f} MDD={mB['mdd_pct']:.2f} 총노출합={expB:.2f}")

LADDER = [
    ("기준 (1.0,1.0,1.0)",   None),
    ("f1.05 (1.05,1.05,·)",  {1: 1.05, 2: 1.05, 3: 1.05}),
    ("f1.1  (1.1,1.1,·)",    {1: 1.1, 2: 1.1, 3: 1.1}),
    ("f1.15 (1.15,1.15,·)",  {1: 1.15, 2: 1.15, 3: 1.15}),
    ("f1.2  (1.2,1.2,·)",    {1: 1.2, 2: 1.2, 3: 1.2}),
    ("f1.3  (1.3,1.3,·)",    {1: 1.3, 2: 1.3, 3: 1.3}),
    ("f1.5  (1.5,1.5,·)",    {1: 1.5, 2: 1.5, 3: 1.5}),
    ("명시 (1.2,1.2,0.6)",   {1: 1.2, 2: 1.2, 3: 0.6}),
    ("명시 (1.3,1.2,0.5)",   {1: 1.3, 2: 1.2, 3: 0.5}),
    ("명시 (1.4,1.1,0.5)",   {1: 1.4, 2: 1.1, 3: 0.5}),
    ("명시 (1.5,1.0,0.5)",   {1: 1.5, 2: 1.0, 3: 0.5}),
    ("명시 (1.1,1.1,0.8)",   {1: 1.1, 2: 1.1, 3: 0.8}),
    ("역방향 (0.8,1.0,1.2)", {1: 0.8, 2: 1.0, 3: 1.2}),
    ("역방향 (0.6,1.2,1.2)", {1: 0.6, 2: 1.2, 3: 1.2}),
]

print(f"\n{'후보':24s} {'n':>4s} {'78d':>9s} {'ΔC1':>8s} {'30d':>7s} {'Δ30':>7s} {'PF':>6s} "
      f"{'MDD':>7s} {'노출합':>7s} {'수익/노출':>9s} {'-T3':>7s} {'-T10':>7s} "
      f"{'앞39':>7s} {'뒤39':>7s} {'5분할':>26s} {'WF6':>6s}")
OUT = {}
for tag, sm in LADDER:
    t0 = time.time()
    ts = run(sm); OUT[tag] = ts
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, BASE, D)
    exp = pd.Series([float(t["w1a"]) for t in ts]).sum()
    k5 = " ".join(f"{x:+5.1f}" for x in r["k5"])
    assert r["only_a"] == 0 and r["only_b"] == 0, "진입집합이 바뀌면 안 된다"
    print(f"{tag:24s} {m['trades']:4d} {m['compound_pct']:9.3f} "
          f"{m['compound_pct']-mB['compound_pct']:+8.2f} {m30['compound_pct']:7.2f} "
          f"{m30['compound_pct']-mB30['compound_pct']:+7.2f} {m['pf']:6.3f} {m['mdd_pct']:7.2f} "
          f"{exp:7.1f} {m['compound_pct']/exp:9.3f} "
          f"{excl_topn(ts,D,3)-excl_topn(BASE,D,3):+7.2f} "
          f"{excl_topn(ts,D,10)-excl_topn(BASE,D,10):+7.2f} "
          f"{compound(ts,D[:39])-compound(BASE,D[:39]):+7.2f} "
          f"{compound(ts,D[39:])-compound(BASE,D[39:]):+7.2f} {k5:>26s} "
          f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s}  {time.time()-t0:4.0f}s", flush=True)

pickle.dump({"out": OUT, "base": BASE, "dates": D}, open(A.HERE / "s3.pkl", "wb"))
print("\n저장: s3.pkl")
