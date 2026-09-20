"""S9 — 실운용 형태 확인. production 사이징 상한(일 3.0 / 개별 1.5 / 최소 0.25) 그대로.

P 후보: 오전(09:00~11:00) 3번째 진입의 사이징 배수를 production 최소값
        X2LITE_SIZING_MIN_MULT(0.25) 로 낮추고, 그만큼을 slot1·2 에 돌린다.
        새 임계값을 만들지 않는다 -- 0.25 는 이미 production 상수다.
진입집합/청산/3회 한도 불변(assert).
"""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
A._Q3_PATH = A.HERE / "_q3base.pkl"
from common import summarize, compound, excl_topn
from app.trading.macd2 import config
from app.trading.macd2 import time_window_3slot as tw3
import hengine5 as H
import axval as V

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
C1 = {"decide": 99.0, "strong": {}, "weak": {},
      "pp": {"arm": float(config.C1_ARM_MFE_PCT),
             "give": float(config.C1_GIVEBACK_PCT), "cond": "gap_neg"}}
H.D_VARIANTS["N1PROD"] = {}
MO = tw3.SESSION_MORNING
MIN = float(config.X2LITE_SIZING_MIN_MULT)
rng = np.random.default_rng(20260920)


def run(sm=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, slot_mult=sm)
    finally:
        H._MEMO["base"] = _pb


expo = lambda ts: float(sum(float(t["w1a"]) for t in ts))
BASE = run(); mB = summarize(BASE, D); EB = expo(BASE)
mB30 = summarize([t for t in BASE if t["date"] in S30], W30)
assert abs(mB["compound_pct"] - 438.6268) < 1e-3
key = lambda t: (t["date"], t["entry_time"])
BK = {key(t): t for t in BASE}
RUN8 = sorted(k for k, t in BK.items() if t["peak_net_pct"] >= 8.0)
print(f"기준 C1 (현행): n={mB['trades']} 78d={mB['compound_pct']:.4f} 30d={mB30['compound_pct']:.2f} "
      f"PF={mB['pf']:.3f} MDD={mB['mdd_pct']:.2f} 총노출={EB:.2f}")
print(f"production 사이징 상수: MIN={MIN} MAX={config.X2LITE_SIZING_MAX_MULT} "
      f"일노출상한={config.X2LITE_SIZING_DAILY_EXPOSURE_CAP}")

CAND = {
    "P0 오전slot3=0.25 (재배분 없음)": lambda s, ss, dt=None: MIN if (s == 3 and ss == MO) else 1.0,
    "P1 + slot1,2 x1.02":  lambda s, ss, dt=None: MIN if (s == 3 and ss == MO) else 1.02,
    "P2 + slot1,2 x1.05":  lambda s, ss, dt=None: MIN if (s == 3 and ss == MO) else 1.05,
    "P3 + slot1,2 x1.10":  lambda s, ss, dt=None: MIN if (s == 3 and ss == MO) else 1.10,
    "(참고) 오전slot3=0.5 +1.02": lambda s, ss, dt=None: 0.5 if (s == 3 and ss == MO) else 1.02,
}
print(f"\n{'후보':30s} {'n':>4s} {'노출':>7s} {'78d':>9s} {'ΔC1':>8s} {'30d':>7s} {'Δ30':>7s} "
      f"{'PF':>6s} {'MDD':>7s} {'-T3':>8s} {'-T10':>8s} {'앞39':>8s} {'뒤39':>8s} "
      f"{'5분할':>26s} {'WF6':>6s} {'run8':>4s}")
OUT = {"기준": BASE}
for tag, fn in CAND.items():
    ts = run(fn); OUT[tag] = ts
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, BASE, D)
    assert r["only_a"] == 0 and r["only_b"] == 0, "진입집합이 바뀌면 안 된다"
    ka = {key(t): t for t in ts}
    dmg = sum(1 for x in RUN8 if x in ka and ka[x]["net_pct"] - BK[x]["net_pct"] < -1e-9)
    print(f"{tag:30s} {m['trades']:4d} {expo(ts):7.2f} {m['compound_pct']:9.3f} "
          f"{m['compound_pct']-mB['compound_pct']:+8.2f} {m30['compound_pct']:7.2f} "
          f"{m30['compound_pct']-mB30['compound_pct']:+7.2f} {m['pf']:6.3f} {m['mdd_pct']:7.2f} "
          f"{excl_topn(ts,D,3)-excl_topn(BASE,D,3):+8.2f} "
          f"{excl_topn(ts,D,10)-excl_topn(BASE,D,10):+8.2f} "
          f"{compound(ts,D[:39])-compound(BASE,D[:39]):+8.2f} "
          f"{compound(ts,D[39:])-compound(BASE,D[39:]):+8.2f} "
          f"{' '.join(f'{x:+5.1f}' for x in r['k5']):>26s} "
          f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s} {dmg:4d}", flush=True)

# 최종 후보 부트스트랩 + 월별
best = OUT["P1 + slot1,2 x1.02"]
ua = {d: compound([t for t in best if t["date"] == d], [d]) for d in D}
ub = {d: compound([t for t in BASE if t["date"] == d], [d]) for d in D}
da = np.array([ua[d] - ub[d] for d in D])
idx = rng.integers(0, len(D), size=(10000, len(D)))
bs = da[idx].mean(axis=1)
print(f"\nP1 일단위 부트스트랩 10000회: 평균 {bs.mean():+.4f} / >0 {100*(bs>0).mean():.1f}% "
      f"/ 5%분위 {np.percentile(bs,5):+.4f}")
print("P1 월별 ΔC1: " + " ".join(
    f"{k}={compound(best,[x for x in D if x.startswith(k)])-compound(BASE,[x for x in D if x.startswith(k)]):+.1f}"
    for k in ("202605", "202606", "202607", "202608", "202609")))
pickle.dump({"out": OUT, "dates": D}, open(A.HERE / "s9.pkl", "wb"))
print("\n저장: s9.pkl")
