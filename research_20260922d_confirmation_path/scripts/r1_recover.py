# -*- coding: utf-8 -*-
"""[R1] weak 43건을 recovering / still-weak 로 나눌 수 있는가. READ-ONLY.

weak = confirmation 구간(플래그봉 시작 ~ 진입 직전) 보유ETF 수익률 <= 0%
판정은 전부 `datetime < entry_time` 인 1분봉 + `idx <= 판정봉` 인 3분봉만 쓴다.
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g6_entry as E
import p1_confirm as C
import q1_confsize as Q

A, TS, D, W30, OOS = C.A, C.TS, C.D, C.W30, C.OOS
B3, HIST = C.B3, C.HIST
H1, H1T, ETF = C.H1, C.H1T, C.ETF
BASE, BMAP, AMAP = Q.BASE, Q.BMAP, Q.AMAP
rng = np.random.default_rng(20260922)
WEAK = [t for t in A if Q.is_weak(t)]


def rec_feats(t) -> dict:
    """회복 관련 feature — 전부 진입 직전까지의 정보."""
    k = E.last_done(t["entry_time"])
    p = k - 1
    up = t["direction"] == "UP_RED"
    sgn = 1.0 if up else -1.0
    ent = pd.Timestamp(t["entry_time"])
    t0 = pd.Timestamp(B3["datetime"].iloc[p])
    eb, et = ETF[t["symbol"]]
    em = eb[(et >= t0) & (et < ent)]
    hm = H1[(H1T >= t0) & (H1T < ent)]
    if len(em) < 4 or len(hm) < 4:
        return {}
    ec = pd.to_numeric(em["close"], errors="coerce").to_numpy(float)
    el = pd.to_numeric(em["low"], errors="coerce").to_numpy(float)
    hc = pd.to_numeric(hm["close"], errors="coerce").to_numpy(float)
    o = ec[0]
    f = {}
    # 1) 최저점 -> 진입직전 회복폭
    f["회복폭%(저가기준)"] = (ec[-1] - el.min()) / el.min() * 100.0
    f["회복폭%(종가기준)"] = (ec[-1] - ec.min()) / ec.min() * 100.0
    f["저점이후경과봉"] = float(len(ec) - 1 - int(np.argmin(ec)))
    # 2) 마지막 1분 / 2분 ETF 수익률
    f["ETF_마지막1분%"] = (ec[-1] - ec[-2]) / ec[-2] * 100.0
    f["ETF_마지막2분%"] = (ec[-1] - ec[-3]) / ec[-3] * 100.0 if len(ec) >= 3 else np.nan
    # 3) 마지막 2분 하이닉스-ETF 방향 일치
    hd = sgn * (hc[-1] - hc[-3]) if len(hc) >= 3 else 0.0
    ed = ec[-1] - ec[-3] if len(ec) >= 3 else 0.0
    f["마지막2분_방향일치"] = 1.0 if (hd > 0 and ed > 0) else 0.0
    # 4) MACD gap 후반 재확대 (판정봉 > 플래그봉)
    f["gap_재확대"] = 1.0 if sgn * HIST[k] > sgn * HIST[p] else 0.0
    # 5) 초반 vs 후반 momentum 변화
    h = len(ec) // 2
    early = (ec[h] - ec[0]) / ec[0] * 100.0
    late = (ec[-1] - ec[h]) / ec[h] * 100.0
    f["후반-초반_momentum"] = late - early
    f["후반_momentum%"] = late
    f["진입직전_vs_시작%"] = (ec[-1] - o) / o * 100.0
    return f


FE = {}
for t in WEAK:
    v = rec_feats(t)
    if v:
        FE[(t["date"], t["entry_time"])] = v
KEYS = list(next(iter(FE.values())).keys())


def auc(pos, neg):
    pos = [x for x in pos if x == x]; neg = [x for x in neg if x == x]
    if not pos or not neg:
        return float("nan")
    return sum((1.0 if a > b else 0.5 if a == b else 0.0)
               for a in pos for b in neg) / (len(pos) * len(neg))


def f(t, k, d=np.nan):
    return FE.get((t["date"], t["entry_time"]), {}).get(k, d)


if __name__ == "__main__":
    print("=" * 124)
    print("[R1] weak 43건의 내부 구조")
    print("=" * 124)
    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
        SW = set(W)
        g = [t for t in WEAK if t["date"] in SW]
        win = [t for t in g if BMAP[(t["date"], t["entry_time"])]["pnl_krw"] > 0]
        print(f"  {tag:10s} weak {len(g):2d}건  수익 {len(win):2d} / 손실 {len(g)-len(win):2d}  "
              f"MFE>=3% {sum(1 for t in g if t['mfe']>=3)}건 / >=5% {sum(1 for t in g if t['mfe']>=5)} / "
              f">=8% {sum(1 for t in g if t['mfe']>=8)}  실현 "
              f"{sum(BMAP[(t['date'],t['entry_time'])]['pnl_krw'] for t in g):+12,.0f}")

    print(f"\n  weak 43건 전수 (회복 feature 포함)")
    hdr = (f"  {'날짜':9s} {'진입':5s} {'창':4s} {'MFE':>5s} {'net%':>6s} {'KRW':>10s} "
           f"{'추종%':>6s} {'회복폭':>6s} {'끝1분':>6s} {'끝2분':>6s} {'방향일치':>5s} "
           f"{'gap재확대':>6s} {'후-초':>6s}")
    print(hdr)
    for t in sorted(WEAK, key=lambda z: (z["date"], z["entry_time"])):
        b = BMAP[(t["date"], t["entry_time"])]
        w = "IS" if t["date"] in set(W30) else "OOS"
        mark = "  <== 20260806 weak runner" if t["date"] == "20260806" else ""
        print(f"  {t['date']:9s} {t['entry_time'][11:16]:5s} {w:4s} {t['mfe']:5.2f} "
              f"{t['net_pct']:6.2f} {b['pnl_krw']:10,.0f} {C.FE[(t['date'],t['entry_time'])]['ETF_추종%']:6.2f} "
              f"{f(t,'회복폭%(저가기준)',0):6.2f} {f(t,'ETF_마지막1분%',0):6.2f} "
              f"{f(t,'ETF_마지막2분%',0):6.2f} {f(t,'마지막2분_방향일치',0):5.0f} "
              f"{f(t,'gap_재확대',0):6.0f} {f(t,'후반-초반_momentum',0):6.2f}{mark}")

    # ── 판별력 ──────────────────────────────────────────────────────
    print("\n" + "=" * 112)
    print("회복 feature 의 판별력 (weak 43건 내부)")
    print("=" * 112)
    LAB = {"수익 vs 손실": lambda t: BMAP[(t["date"], t["entry_time"])]["pnl_krw"] > 0,
           "MFE>=3% vs 미만": lambda t: t["mfe"] >= 3.0,
           "MFE>=1.5% vs 미만": lambda t: t["mfe"] >= 1.5}
    print(f"  {'feature':22s} " + " ".join(f"{n:>18s}" for n in LAB))
    print(f"  {'':22s} " + " ".join(f"{'78일/30일/OOS':>18s}" for _ in LAB))
    print("  " + "-" * 82)
    for k in KEYS:
        cells = []
        for _, fn in LAB.items():
            sub = []
            for W in (D, W30, OOS):
                SW = set(W)
                pos = [f(t, k) for t in WEAK if t["date"] in SW and fn(t)]
                neg = [f(t, k) for t in WEAK if t["date"] in SW and not fn(t)]
                sub.append(auc(pos, neg))
            cells.append("/".join(f"{x:.2f}" if x == x else " -- " for x in sub))
        print(f"  {k:22s} " + " ".join(f"{c:>18s}" for c in cells))
