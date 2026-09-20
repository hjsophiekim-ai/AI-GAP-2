"""S8 — M0 의 LOO(8건 각각 되살리기) / 월 제외 / '슬롯 자체를 남기는' 대안.

M0 = 오전(09:00~11:00) 3번째 진입에 자금을 배정하지 않고 남는 한도를 나머지에
     균등 재배분. 진입집합/청산/3회 한도 불변. 비교기준 = 같은 노출의 무배분 레버리지.
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
S4 = pickle.load(open(A.HERE / "s4.pkl", "rb"))
LEV = sorted(S4["lev"]); lx = np.array([x[0] for x in LEV]); ly = np.array([x[1] for x in LEV])
MO = tw3.SESSION_MORNING
KST = config.KST
rng = np.random.default_rng(20260920)


def run(sm=None, cap=None, gate=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    _oc = config.X2LITE_SIZING_DAILY_EXPOSURE_CAP; _om = config.X2LITE_SIZING_MAX_MULT
    if cap is not None:
        config.X2LITE_SIZING_DAILY_EXPOSURE_CAP = float(cap)
        config.X2LITE_SIZING_MAX_MULT = 9.0
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, slot_mult=sm, gate=gate)
    finally:
        config.X2LITE_SIZING_DAILY_EXPOSURE_CAP = _oc
        config.X2LITE_SIZING_MAX_MULT = _om
        H._MEMO["base"] = _pb


expo = lambda ts: float(sum(float(t["w1a"]) for t in ts))
BASE = run(); EB = expo(BASE)
LEVBASE = run(lambda s, ss, dt=None: 1.0, cap=99.0)
l30 = summarize([t for t in LEVBASE if t["date"] in S30], W30)["compound_pct"]
MS = sorted([t for t in BASE if t["slot_number"] == 3 and t["session"] == MO],
            key=lambda x: x["date"])
MDATES = [t["date"] for t in MS]


def matched(fn, tag, show=True, gate=None):
    k = 1.0; ts = None
    for _ in range(6):
        ts = run((lambda s, ss, dt, _f=fn, _k=k: _f(s, ss, dt) * _k), cap=99.0, gate=gate)
        e = expo(ts)
        if abs(e - EB) < 0.12:
            break
        k *= EB / e
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, LEVBASE, D)
    lv = float(np.interp(e, lx, ly))
    if show:
        print(f"{tag:26s} {k:6.3f} {m['compound_pct']:9.3f} {m['compound_pct']-lv:+9.2f} "
              f"{m30['compound_pct']-l30:+8.2f} "
              f"{compound(ts,D[:39])-compound(LEVBASE,D[:39]):+8.2f} "
              f"{compound(ts,D[39:])-compound(LEVBASE,D[39:]):+8.2f} "
              f"{m['mdd_pct']:7.2f} {m['pf']:6.3f} "
              f"{' '.join(f'{x:+5.1f}' for x in r['k5']):>26s} "
              f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s} "
              f"{r['only_a']:3d}/{r['only_b']:<3d}", flush=True)
    return ts, m, r, lv


HDR = (f"{'후보':26s} {'k':>6s} {'78d':>9s} {'배분효과':>9s} {'30d':>8s} {'앞39':>8s} "
       f"{'뒤39':>8s} {'MDD':>7s} {'PF':>6s} {'5분할':>26s} {'WF6':>6s} {'신규/소멸'}")
print("=" * 160); print("1. LOO — 8건 중 1건씩 되살린다 (그 날짜만 배수 1.0 유지)"); print("=" * 160)
print(HDR)
matched(lambda s, ss, dt=None: 0.0 if (s == 3 and ss == MO) else 1.0, "M0 (8건 전부 제외)")
for t in MS:
    d0 = t["date"]
    matched((lambda s, ss, dt, _d=d0: 1.0 if dt == _d else (0.0 if (s == 3 and ss == MO) else 1.0)),
            f"  {d0} 되살림 ({t['net_pct']:+.2f})")

print("\n" + "=" * 160); print("2. 월 제외 — 한 달을 통째로 빼도 남는가"); print("=" * 160)
print(HDR)
for mo in ("202606", "202607", "202608"):
    sub = [d for d in D if not d.startswith(mo)]
    k = 1.0
    for _ in range(6):
        ts = run((lambda s, ss, dt, _k=k: (0.0 if (s == 3 and ss == MO) else 1.0) * _k), cap=99.0)
        e = expo(ts)
        if abs(e - EB) < 0.12:
            break
        k *= EB / e
    a = compound(ts, sub); b = compound(LEVBASE, sub)
    print(f"  {mo} 제외 ({len(sub)}일): M0 {a:9.3f} vs 레버리지 {b:9.3f}  차이 {a-b:+8.2f}")

print("\n" + "=" * 160); print("3. 대안 — 자금만 빼는 대신 '진입 자체를 안 하고 슬롯을 남긴다'"); print("=" * 160)
print(HDR)
g = lambda x: "M1" if (x["slot_number"] == 3 and x["session"] == MO) else None
matched(lambda s, ss, dt=None: 1.0, "M1 오전3번째 진입금지", gate=g)
matched(lambda s, ss, dt=None: 0.0 if (s == 3 and ss == MO) else 1.0, "M0 자금만 0 (재게시)")

pickle.dump({"dates": D}, open(A.HERE / "s8.pkl", "wb"))
print("\n저장: s8.pkl")
