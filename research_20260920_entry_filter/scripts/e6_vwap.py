"""E6 — 1차 후보: VWAP veto 임계값 스캔.

production 파라미터 `TW2_VWAP_VETO_THRESHOLD_PCT`(현재 -1.0) 하나만 움직인다.
새 지표/새 임계값을 만들지 않으므로 단조 축에서 plateau 를 그대로 볼 수 있다.
슬롯 3회/일 한도, 세션창, quality/TEG 게이트, 청산(N1 래더+C1) 전부 불변.
"""
import sys, pickle, time
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
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
VETO_MEMO = {}           # 임계값별 veto 메모 (기본값은 기존 메모 재사용)


def run(thr, **kw):
    """thr = TW2_VWAP_VETO_THRESHOLD_PCT. None 이면 production 기본값."""
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    _pv = H._MEMO["veto"]
    _old = config.TW2_VWAP_VETO_THRESHOLD_PCT
    if thr is not None:
        config.TW2_VWAP_VETO_THRESHOLD_PCT = float(thr)
        H._MEMO["veto"] = VETO_MEMO.setdefault(float(thr), {})
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, **kw)
    finally:
        config.TW2_VWAP_VETO_THRESHOLD_PCT = _old
        H._MEMO["base"] = _pb; H._MEMO["veto"] = _pv


OUT = {}
BASE = run(None)
OUT["기준 C1 (veto -1.0)"] = BASE
mB = summarize(BASE, D)
mB30 = summarize([t for t in BASE if t["date"] in S30], W30)
print(f"기준 C1: n={mB['trades']} 78d={mB['compound_pct']:.4f} 30d={mB30['compound_pct']:.2f}")
assert abs(mB["compound_pct"] - 438.6268) < 1e-3, mB["compound_pct"]

key = lambda t: (t["date"], t["entry_time"])
BK = {key(t): t for t in BASE}
RUN8 = sorted(k for k, t in BK.items() if t["peak_net_pct"] >= 8.0)

print(f"\n{'후보':22s} {'n':>4s} {'78d':>9s} {'ΔC1':>8s} {'30d':>7s} {'Δ30':>7s} "
      f"{'PF':>6s} {'MDD':>7s} {'승률':>5s} {'일승률':>6s} {'-T1':>7s} {'-T3':>7s} {'-T10':>7s} "
      f"{'앞39':>7s} {'뒤39':>7s} {'WF6':>6s} {'신규':>4s} {'소멸':>4s} {'run8':>5s}")
ROWS = {}
for thr in (-1.0, -1.25, -1.5, -2.0, -2.5, -3.0, -5.0, -999.0):
    t0 = time.time()
    ts = run(thr)
    tag = f"veto {thr:g}" + (" (현행)" if thr == -1.0 else "")
    OUT[tag] = ts
    m = summarize(ts, D)
    m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, BASE, D)
    ka = {key(t): t for t in ts}
    dmg = sum(1 for x in RUN8 if x in ka and ka[x]["net_pct"] - BK[x]["net_pct"] < -1e-9)
    ROWS[tag] = (m, m30, r, dmg)
    print(f"{tag:22s} {m['trades']:4d} {m['compound_pct']:9.3f} "
          f"{m['compound_pct']-mB['compound_pct']:+8.2f} {m30['compound_pct']:7.2f} "
          f"{m30['compound_pct']-mB30['compound_pct']:+7.2f} {m['pf']:6.3f} {m['mdd_pct']:7.2f} "
          f"{m['win_rate_pct']:5.1f} {m['day_win_rate_pct']:6.1f} "
          f"{excl_topn(ts,D,1)-excl_topn(BASE,D,1):+7.2f} "
          f"{excl_topn(ts,D,3)-excl_topn(BASE,D,3):+7.2f} "
          f"{excl_topn(ts,D,10)-excl_topn(BASE,D,10):+7.2f} "
          f"{compound(ts,D[:39])-compound(BASE,D[:39]):+7.2f} "
          f"{compound(ts,D[39:])-compound(BASE,D[39:]):+7.2f} "
          f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s} "
          f"{r['only_a']:4d} {r['only_b']:4d} {dmg:5d}  {time.time()-t0:4.0f}s", flush=True)

pickle.dump({"out": OUT, "dates": D}, open(A.HERE / "e6.pkl", "wb"))
print("\n저장: e6.pkl")
