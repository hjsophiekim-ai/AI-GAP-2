"""S5 — 노출 일치 정밀격자 + 대안설명(세션효과) 확인 + 검증.

모든 후보는 스칼라 k 로 눌러 실현 총노출을 기준(157.28)에 맞춘다.
따라서 표의 '배분효과' 는 레버리지가 제거된 순수 배분 기여다.
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
S4 = pickle.load(open(A.HERE / "s4.pkl", "rb"))
LEV = sorted(S4["lev"])
lx = np.array([x[0] for x in LEV]); ly = np.array([x[1] for x in LEV])


def run(sm=None, cap=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    _oc = config.X2LITE_SIZING_DAILY_EXPOSURE_CAP
    _om = config.X2LITE_SIZING_MAX_MULT
    if cap is not None:
        config.X2LITE_SIZING_DAILY_EXPOSURE_CAP = float(cap)
        config.X2LITE_SIZING_MAX_MULT = 9.0
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, slot_mult=sm)
    finally:
        config.X2LITE_SIZING_DAILY_EXPOSURE_CAP = _oc
        config.X2LITE_SIZING_MAX_MULT = _om
        H._MEMO["base"] = _pb


expo = lambda ts: float(sum(float(t["w1a"]) for t in ts))
BASE = run(); mB = summarize(BASE, D); EB = expo(BASE)
mB30 = summarize([t for t in BASE if t["date"] in S30], W30)
LEVBASE = run({1: 1.0, 2: 1.0, 3: 1.0}, cap=99.0)   # 노출 160.64 짜리 레버리지 대조
print(f"기준 C1: 78d={mB['compound_pct']:.4f} 30d={mB30['compound_pct']:.2f} "
      f"MDD={mB['mdd_pct']:.2f} 총노출={EB:.2f}")

# slot3 가 곧 오후인가? (대안설명 확인)
T = pd.DataFrame([{"slot": t["slot_number"], "session": t["session"],
                   "net": float(t["net_pct"])} for t in BASE])
print("\n슬롯 x 세션 교차 (대안설명 확인):")
for s in (1, 2, 3):
    g = T[T["slot"] == s]
    mo = g[g["session"] == "MORNING"]; af = g[g["session"] == "AFTERNOON"]
    print(f"  slot{s}: n={len(g):3d}  오전 {len(mo):3d}건 평균{mo['net'].mean():+6.2f}  "
          f"오후 {len(af):3d}건 평균{(af['net'].mean() if len(af) else 0):+6.2f}")


def matched(sh, tag, verbose=True):
    k = 1.0; ts = None
    for _ in range(5):
        ts = run({s: v * k for s, v in sh.items()}, cap=99.0)
        e = expo(ts)
        if abs(e - EB) < 0.12:
            break
        k *= EB / e
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, LEVBASE, D)
    assert r["only_a"] == 0 and r["only_b"] == 0
    lv = float(np.interp(e, lx, ly))
    l30 = summarize([t for t in LEVBASE if t["date"] in S30], W30)["compound_pct"]
    if verbose:
        print(f"{tag:22s} {k:6.3f} {e:7.2f} {m['compound_pct']:9.3f} {lv:9.3f} "
              f"{m['compound_pct']-lv:+9.2f} {m30['compound_pct']-l30:+8.2f} "
              f"{m['mdd_pct']:7.2f} {m['pf']:6.3f} "
              f"{compound(ts,D[:39])-compound(LEVBASE,D[:39]):+8.2f} "
              f"{compound(ts,D[39:])-compound(LEVBASE,D[39:]):+8.2f} "
              f"{' '.join(f'{x:+5.1f}' for x in r['k5']):>26s} "
              f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s}", flush=True)
    return ts, m, r, e


print("\n" + "=" * 150)
print("정밀격자 (전부 총노출 157.3 으로 일치. 비교기준 = 같은 노출의 무배분 레버리지)")
print("=" * 150)
print(f"{'모양(slot1,2,3)':22s} {'k':>6s} {'노출':>7s} {'78d':>9s} {'레버리지':>9s} "
      f"{'배분효과':>9s} {'30d배분':>8s} {'MDD':>7s} {'PF':>6s} {'앞39':>8s} {'뒤39':>8s} "
      f"{'5분할':>26s} {'WF6':>6s}")
GRID = {
    "(1.0,1.0,1.0) 균등": {1: 1.0, 2: 1.0, 3: 1.0},
    "(1.0,1.0,0.7)": {1: 1.0, 2: 1.0, 3: 0.7},
    "(1.0,1.0,0.5)": {1: 1.0, 2: 1.0, 3: 0.5},
    "(1.1,1.1,0.7)": {1: 1.1, 2: 1.1, 3: 0.7},
    "(1.2,1.2,0.6)": {1: 1.2, 2: 1.2, 3: 0.6},
    "(1.3,1.3,0.4)": {1: 1.3, 2: 1.3, 3: 0.4},
    "(1.0,1.2,0.8)": {1: 1.0, 2: 1.2, 3: 0.8},
    "(1.0,1.3,0.7)": {1: 1.0, 2: 1.3, 3: 0.7},
    "(1.0,1.4,0.6)": {1: 1.0, 2: 1.4, 3: 0.6},
    "(1.0,1.5,0.5)": {1: 1.0, 2: 1.5, 3: 0.5},
    "(1.1,1.3,0.6)": {1: 1.1, 2: 1.3, 3: 0.6},
    "(1.3,1.1,0.6)": {1: 1.3, 2: 1.1, 3: 0.6},
}
RES = {}
for tag, sh in GRID.items():
    RES[tag] = matched(sh, tag)

pickle.dump({"res": {k: v[0] for k, v in RES.items()}, "base": BASE,
             "levbase": LEVBASE, "dates": D}, open(A.HERE / "s5.pkl", "wb"))
print("\n저장: s5.pkl")
