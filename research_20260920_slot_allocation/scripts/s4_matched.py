"""S4 — 레버리지와 배분을 분리한다.

S3 의 f-사다리는 총노출이 157->191 로 같이 올라갔다. 그건 레버리지 효과다.
여기서는 각 '모양'을 스칼라 k 로 눌러 **실현 총노출을 기준(157.28)에 맞춘 뒤**
비교한다. 그러면 남는 차이는 순수 배분 효과다.

일일노출 상한(3.0)이 모양에 따라 다르게 물리는 것도 교란이므로, 대조군은
상한을 풀고(99) 따로 돌려 레버리지 곡선 자체를 그린다.
진입집합/청산/3회 한도는 전부 불변(assert 로 확인).
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


def run(sm=None, cap=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    _oc = config.X2LITE_SIZING_DAILY_EXPOSURE_CAP
    _om = config.X2LITE_SIZING_MAX_MULT
    if cap is not None:
        config.X2LITE_SIZING_DAILY_EXPOSURE_CAP = float(cap)
        config.X2LITE_SIZING_MAX_MULT = 9.0      # 모양을 왜곡하지 않도록 같이 푼다
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, slot_mult=sm)
    finally:
        config.X2LITE_SIZING_DAILY_EXPOSURE_CAP = _oc
        config.X2LITE_SIZING_MAX_MULT = _om
        H._MEMO["base"] = _pb


def expo(ts):
    return float(sum(float(t["w1a"]) for t in ts))


BASE = run()
mB = summarize(BASE, D); EB = expo(BASE)
print(f"기준 C1: 78d={mB['compound_pct']:.4f} MDD={mB['mdd_pct']:.2f} 총노출={EB:.2f}")
key = lambda t: (t["date"], t["entry_time"])

print("\n" + "=" * 128)
print("1. 레버리지 곡선 (상한 해제, 모든 슬롯 동일배수) — 배분효과 0 인 대조군")
print("=" * 128)
print(f"{'배수':8s} {'총노출':>8s} {'78d':>9s} {'ΔC1':>8s} {'MDD':>7s} {'PF':>6s}")
LEV = []
for m in (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.3):
    ts = run({1: m, 2: m, 3: m}, cap=99.0)
    mm = summarize(ts, D); e = expo(ts)
    LEV.append((e, mm["compound_pct"], mm["mdd_pct"]))
    print(f"{m:8.2f} {e:8.2f} {mm['compound_pct']:9.3f} "
          f"{mm['compound_pct']-mB['compound_pct']:+8.2f} {mm['mdd_pct']:7.2f} {mm['pf']:6.3f}",
          flush=True)
LEV.sort()
lx = np.array([x[0] for x in LEV]); ly = np.array([x[1] for x in LEV])
lm = np.array([x[2] for x in LEV])


def lev_at(e):
    """같은 총노출에서 '배분 없는 순수 레버리지' 가 냈을 78d."""
    return float(np.interp(e, lx, ly)), float(np.interp(e, lx, lm))


SHAPES = {
    "앞무겁게 (1.5,1.0,0.5)": {1: 1.5, 2: 1.0, 3: 0.5},
    "앞무겁게 (1.3,1.0,0.7)": {1: 1.3, 2: 1.0, 3: 0.7},
    "가운데 (1.0,1.4,0.6)":   {1: 1.0, 2: 1.4, 3: 0.6},
    "1·2 우대 (1.2,1.2,0.6)": {1: 1.2, 2: 1.2, 3: 0.6},
    "뒤무겁게 (0.7,1.0,1.3)": {1: 0.7, 2: 1.0, 3: 1.3},
    "뒤무겁게 (0.5,1.0,1.5)": {1: 0.5, 2: 1.0, 3: 1.5},
}

print("\n" + "=" * 128)
print("2. 노출 일치 비교 (상한 해제) — 각 모양을 스칼라 k 로 눌러 총노출을 기준에 맞춘다")
print("=" * 128)
print(f"{'모양':24s} {'k':>6s} {'총노출':>8s} {'78d':>9s} {'같은노출 레버리지':>15s} "
      f"{'배분효과':>9s} {'MDD':>7s} {'MDD(레버)':>9s} {'PF':>6s} {'WF6':>6s}")
OUT = {}
for tag, sh in SHAPES.items():
    k = 1.0
    for _ in range(4):
        ts = run({s: v * k for s, v in sh.items()}, cap=99.0)
        e = expo(ts)
        if abs(e - EB) < 0.15:
            break
        k *= EB / e
    mm = summarize(ts, D); r = V.report(tag, ts, BASE, D)
    assert r["only_a"] == 0 and r["only_b"] == 0, "진입집합이 바뀌면 안 된다"
    lv, lmdd = lev_at(e)
    OUT[tag] = ts
    print(f"{tag:24s} {k:6.3f} {e:8.2f} {mm['compound_pct']:9.3f} {lv:15.3f} "
          f"{mm['compound_pct']-lv:+9.2f} {mm['mdd_pct']:7.2f} {lmdd:9.2f} {mm['pf']:6.3f} "
          f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s}", flush=True)

pickle.dump({"out": OUT, "lev": LEV, "base": BASE, "dates": D},
            open(A.HERE / "s4.pkl", "wb"))
print("\n저장: s4.pkl")
