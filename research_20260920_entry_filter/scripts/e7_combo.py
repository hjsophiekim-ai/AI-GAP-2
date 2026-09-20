"""E7 — 제거측(나쁜 진입 veto) 후보 + VWAP 완화와의 결합.

제거 후보는 전부 진입시점 특징(완성봉)만 쓴다. 청산(N1 래더+C1), 슬롯 3회/일,
세션창은 불변. gate 훅은 '승인된 진입'만 거절할 수 있다.
"""
import sys, pickle, time
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np
import pandas as pd
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
VETO_MEMO = {}
VWAP_BEST = float(sys.argv[1]) if len(sys.argv) > 1 else -2.0


def feats(g):
    """진입시점 특징. bars 전체프레임 캐시에서 idx 만 읽는다(값 동일)."""
    i = int(g["idx"])
    tb = H.snap_table(BARS); fb = H.feat_table(BARS)
    sgn = 1.0 if g["direction"] == Direction.UP_RED else -1.0
    cl = float(tb["close"][i]); vw = float(fb["vwap"][i])
    v = BARS["volume"].astype(float).to_numpy()
    vb = v[max(0, i - 19): i + 1]
    return {
        "e20_e50_pct": (float(tb["e20"][i] - tb["e50"][i]) * sgn / cl * 100.0) if cl > 0 else 0.0,
        "close_e20_pct": (float(cl - tb["e20"][i]) * sgn / cl * 100.0) if cl > 0 else 0.0,
        "vwap_pct": ((cl - vw) * sgn / vw * 100.0) if vw > 0 else 0.0,
        "gap_exp": (float(fb["gap"][i] - fb["gap"][i - 1]) * sgn if i > 0 else 0.0),
        "slope50": float(tb["s50"][i]) * sgn,
        "vol_ratio": (float(v[i] / vb.mean()) if len(vb) and vb.mean() > 0 else 1.0),
    }


def mk_gate(rule):
    def g(ctx_):
        f = feats(ctx_)
        return rule(f, ctx_)
    return g


def run(thr, gate=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    _pv = H._MEMO["veto"]
    _old = config.TW2_VWAP_VETO_THRESHOLD_PCT
    if thr is not None:
        config.TW2_VWAP_VETO_THRESHOLD_PCT = float(thr)
        H._MEMO["veto"] = VETO_MEMO.setdefault(float(thr), {})
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, gate=gate)
    finally:
        config.TW2_VWAP_VETO_THRESHOLD_PCT = _old
        H._MEMO["base"] = _pb; H._MEMO["veto"] = _pv


BASE = run(None)
mB = summarize(BASE, D); mB30 = summarize([t for t in BASE if t["date"] in S30], W30)
assert abs(mB["compound_pct"] - 438.6268) < 1e-3, mB["compound_pct"]
key = lambda t: (t["date"], t["entry_time"])
BK = {key(t): t for t in BASE}
RUN8 = sorted(k for k, t in BK.items() if t["peak_net_pct"] >= 8.0)
print(f"기준 C1: n={mB['trades']} 78d={mB['compound_pct']:.4f} 30d={mB30['compound_pct']:.2f}")

RULES = {
    "R1 과열 e20-e50>1.0":   lambda f, g: "R1" if f["e20_e50_pct"] > 1.0 else None,
    "R1b 과열 e20-e50>1.5":  lambda f, g: "R1b" if f["e20_e50_pct"] > 1.5 else None,
    "R2 종가-e20>1.0":       lambda f, g: "R2" if f["close_e20_pct"] > 1.0 else None,
    "R3 vwap>+2.0":          lambda f, g: "R3" if f["vwap_pct"] > 2.0 else None,
    "R4 거래량<0.8x":         lambda f, g: "R4" if f["vol_ratio"] < 0.8 else None,
}
TESTS = [("제거만 " + k, None, mk_gate(v)) for k, v in RULES.items()]
TESTS += [(f"완화만 veto{VWAP_BEST:g}", VWAP_BEST, None)]
TESTS += [(f"완화+제거 {k}", VWAP_BEST, mk_gate(v)) for k, v in RULES.items()]

print(f"\n{'후보':26s} {'n':>4s} {'78d':>9s} {'ΔC1':>8s} {'30d':>7s} {'Δ30':>7s} "
      f"{'PF':>6s} {'MDD':>7s} {'승률':>5s} {'-T3':>7s} {'-T10':>7s} "
      f"{'앞39':>7s} {'뒤39':>7s} {'WF6':>6s} {'신규':>4s} {'소멸':>4s} {'run8':>5s}")
OUT = {"기준 C1": BASE}
for tag, thr, g in TESTS:
    t0 = time.time()
    ts = run(thr, gate=g)
    OUT[tag] = ts
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, BASE, D)
    ka = {key(t): t for t in ts}
    dmg = sum(1 for x in RUN8 if x in ka and ka[x]["net_pct"] - BK[x]["net_pct"] < -1e-9)
    print(f"{tag:26s} {m['trades']:4d} {m['compound_pct']:9.3f} "
          f"{m['compound_pct']-mB['compound_pct']:+8.2f} {m30['compound_pct']:7.2f} "
          f"{m30['compound_pct']-mB30['compound_pct']:+7.2f} {m['pf']:6.3f} {m['mdd_pct']:7.2f} "
          f"{m['win_rate_pct']:5.1f} "
          f"{excl_topn(ts,D,3)-excl_topn(BASE,D,3):+7.2f} "
          f"{excl_topn(ts,D,10)-excl_topn(BASE,D,10):+7.2f} "
          f"{compound(ts,D[:39])-compound(BASE,D[:39]):+7.2f} "
          f"{compound(ts,D[39:])-compound(BASE,D[39:]):+7.2f} "
          f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s} "
          f"{r['only_a']:4d} {r['only_b']:4d} {dmg:5d}  {time.time()-t0:4.0f}s", flush=True)

pickle.dump({"out": OUT, "dates": D, "vwap_best": VWAP_BEST},
            open(A.HERE / "e7.pkl", "wb"))
print("\n저장: e7.pkl")
