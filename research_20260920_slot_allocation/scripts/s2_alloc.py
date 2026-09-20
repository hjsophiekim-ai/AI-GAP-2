"""S2 — 슬롯 배분 후보 1차 스캔.

진입조건(N1 승인집합)·청산(N1 래더+C1)·하루 3회 한도는 전부 불변.
바꾸는 것은 '그 3개를 누구에게 주느냐'(A: 선택) 와 '얼마씩 주느냐'(B: 크기) 뿐.
전부 진입 판정시점 정보만 쓴다 — 미래값 없음.
"""
import sys, pickle, time
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
A._Q3_PATH = A.HERE / "_q3base.pkl"
from common import summarize, compound, excl_topn
from app.trading.macd2 import config
from app.trading.macd2 import time_window_3slot as tw3
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
KST = config.KST


def tq_of(g):
    q = H._memo("tq", int(g["flag_idx"]),
                lambda: tw3.evaluate_trend_quality(g["bars_slice"], g["direction"]))
    return int(getattr(q, "passed_count", 0))


def trend_of(g):
    i = int(g["idx"]); d = g["direction"]
    return bool(H._snap_ok(TB, i, d))


def hhmm(g):
    return g["decision_at"].astimezone(KST)


def run(gate=None, slot_mult=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, gate=gate, slot_mult=slot_mult)
    finally:
        H._MEMO["base"] = _pb


BASE = run()
mB = summarize(BASE, D); mB30 = summarize([t for t in BASE if t["date"] in S30], W30)
assert abs(mB["compound_pct"] - 438.6268) < 1e-3, mB["compound_pct"]
key = lambda t: (t["date"], t["entry_time"])
BK = {key(t): t for t in BASE}
RUN8 = sorted(k for k, t in BK.items() if t["peak_net_pct"] >= 8.0)
print(f"기준 C1: n={mB['trades']} 78d={mB['compound_pct']:.4f} 30d={mB30['compound_pct']:.2f} "
      f"PF={mB['pf']:.3f} MDD={mB['mdd_pct']:.2f}")

CAND = [
    # ── A. 선택 규칙 (누구에게 슬롯을 주는가) ──────────────────────────────
    ("A1 오후진입 금지",        lambda g: "A1" if g["session"] == tw3.SESSION_AFTERNOON else None, None),
    ("A2 오전 최대2 (오후예약)", lambda g: "A2" if (g["session"] == tw3.SESSION_MORNING
                                                and g["slot_number"] == 3) else None, None),
    ("A3 13시 이후 금지",       lambda g: "A3" if hhmm(g).hour >= 13 else None, None),
    ("A4 그날 4번째 플래그부터 금지", lambda g: "A4" if g.get("flag_ord", 0) >= 4 else None, None),
    ("A5 slot3 품질 tq>=4",     lambda g: "A5" if (g["slot_number"] == 3 and tq_of(g) < 4) else None, None),
    ("A6 slot3 상위추세 정렬",   lambda g: "A6" if (g["slot_number"] == 3 and not trend_of(g)) else None, None),
    ("A7 slot2+ 품질 tq>=4",    lambda g: "A7" if (g["slot_number"] >= 2 and tq_of(g) < 4) else None, None),
    ("A8 slot1 품질 tq>=3",     lambda g: "A8" if (g["slot_number"] == 1 and tq_of(g) < 3) else None, None),
    # ── B. 크기 규칙 (같은 진입에 얼마씩) ─────────────────────────────────
    ("B1 slot3 절반",           None, {1: 1.0, 2: 1.0, 3: 0.5}),
    ("B2 slot3 0.6 / slot2 1.2", None, {1: 1.0, 2: 1.2, 3: 0.6}),
    ("B3 slot1,2 1.2 / slot3 0.6", None, {1: 1.2, 2: 1.2, 3: 0.6}),
    ("B4 slot3 0",              None, {1: 1.0, 2: 1.0, 3: 0.0}),
    ("B5 slot1 0.8 / 2,3 1.2",  None, {1: 0.8, 2: 1.2, 3: 1.2}),
]

print(f"\n{'후보':26s} {'n':>4s} {'78d':>9s} {'ΔC1':>8s} {'30d':>7s} {'Δ30':>7s} {'PF':>6s} "
      f"{'MDD':>7s} {'-T3':>7s} {'-T10':>7s} {'앞39':>7s} {'뒤39':>7s} "
      f"{'5분할':>26s} {'WF6':>6s} {'신규':>4s} {'소멸':>4s} {'run8':>4s}")
OUT = {"기준 C1": BASE}
for tag, g, sm in CAND:
    t0 = time.time()
    ts = run(gate=g, slot_mult=sm)
    OUT[tag] = ts
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, BASE, D)
    ka = {key(t): t for t in ts}
    dmg = sum(1 for x in RUN8 if x in ka and ka[x]["net_pct"] - BK[x]["net_pct"] < -1e-9)
    k5 = " ".join(f"{x:+5.1f}" for x in r["k5"])
    print(f"{tag:26s} {m['trades']:4d} {m['compound_pct']:9.3f} "
          f"{m['compound_pct']-mB['compound_pct']:+8.2f} {m30['compound_pct']:7.2f} "
          f"{m30['compound_pct']-mB30['compound_pct']:+7.2f} {m['pf']:6.3f} {m['mdd_pct']:7.2f} "
          f"{excl_topn(ts,D,3)-excl_topn(BASE,D,3):+7.2f} "
          f"{excl_topn(ts,D,10)-excl_topn(BASE,D,10):+7.2f} "
          f"{compound(ts,D[:39])-compound(BASE,D[:39]):+7.2f} "
          f"{compound(ts,D[39:])-compound(BASE,D[39:]):+7.2f} {k5:>26s} "
          f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s} "
          f"{r['only_a']:4d} {r['only_b']:4d} {dmg:4d}  {time.time()-t0:4.0f}s", flush=True)

pickle.dump({"out": OUT, "dates": D}, open(A.HERE / "s2.pkl", "wb"))
print("\n저장: s2.pkl")
