"""S6 — 배분효과의 정체 확인: slot3 삭감인가, 오전 3번째 삭감인가, 오후 삭감인가.

전부 노출 일치(157.28). 비교기준 = 같은 노출의 무배분 레버리지(417.87).
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
MO, AF = tw3.SESSION_MORNING, tw3.SESSION_AFTERNOON


def run(sm=None, cap=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    _oc = config.X2LITE_SIZING_DAILY_EXPOSURE_CAP; _om = config.X2LITE_SIZING_MAX_MULT
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
BASE = run(); EB = expo(BASE)
LEVBASE = run(lambda s, ss, dt=None: 1.0, cap=99.0)
l30 = summarize([t for t in LEVBASE if t["date"] in S30], W30)["compound_pct"]
print(f"기준 노출 {EB:.2f} / 레버리지 대조 78d {summarize(LEVBASE,D)['compound_pct']:.3f}")

CASES = {
    "모든 slot3 x0.5":      lambda s, ss, dt=None: 0.5 if s == 3 else 1.0,
    "오전 slot3 만 x0.5":    lambda s, ss, dt=None: 0.5 if (s == 3 and ss == MO) else 1.0,
    "오후 slot3 만 x0.5":    lambda s, ss, dt=None: 0.5 if (s == 3 and ss == AF) else 1.0,
    "오전 slot3 만 x0.0":    lambda s, ss, dt=None: 0.0 if (s == 3 and ss == MO) else 1.0,
    "오후 전체 x0.5":        lambda s, ss, dt=None: 0.5 if ss == AF else 1.0,
    "오전 전체 x0.5":        lambda s, ss, dt=None: 0.5 if ss == MO else 1.0,
    "slot1 만 x0.5":        lambda s, ss, dt=None: 0.5 if s == 1 else 1.0,
    "slot2 만 x0.5":        lambda s, ss, dt=None: 0.5 if s == 2 else 1.0,
}
print(f"\n{'경우':22s} {'k':>6s} {'노출':>7s} {'78d':>9s} {'배분효과':>9s} {'30d배분':>8s} "
      f"{'앞39':>8s} {'뒤39':>8s} {'MDD':>7s} {'PF':>6s} {'5분할':>26s} {'WF6':>6s}")
for tag, fn in CASES.items():
    k = 1.0; ts = None
    for _ in range(6):
        ts = run((lambda s, ss, dt, _f=fn, _k=k: _f(s, ss, dt) * _k), cap=99.0)
        e = expo(ts)
        if abs(e - EB) < 0.12:
            break
        k *= EB / e
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, LEVBASE, D)
    assert r["only_a"] == 0 and r["only_b"] == 0
    lv = float(np.interp(e, lx, ly))
    print(f"{tag:22s} {k:6.3f} {e:7.2f} {m['compound_pct']:9.3f} "
          f"{m['compound_pct']-lv:+9.2f} {m30['compound_pct']-l30:+8.2f} "
          f"{compound(ts,D[:39])-compound(LEVBASE,D[:39]):+8.2f} "
          f"{compound(ts,D[39:])-compound(LEVBASE,D[39:]):+8.2f} "
          f"{m['mdd_pct']:7.2f} {m['pf']:6.3f} "
          f"{' '.join(f'{x:+5.1f}' for x in r['k5']):>26s} "
          f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s}", flush=True)
