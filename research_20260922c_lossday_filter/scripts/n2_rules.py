# -*- coding: utf-8 -*-
"""[N2] 손실일 필터 — 인과 규칙 후보 + 평가 배터리. READ-ONLY.

규칙이 True 인 거래만 +1.5% 최초 도달봉 **종가**에 전량청산.
판정은 전부 그 거래의 **진입 시점 또는 도달 시점까지**의 완성봉/확정 실현손익만.
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g6_entry as E
import n1_lossday as N

A, TS, D, W30, OOS = N.A, N.TS, N.D, N.W30, N.OOS
B3, DAYB = E.B3, N.DAYB
rng = np.random.default_rng(20260922)

# ── 인과 feature ───────────────────────────────────────────────────────
AM10 = {}
for d in D:
    b = DAYB.get(d)
    if b is None:
        AM10[d] = np.nan; continue
    hh = b["datetime"].dt.strftime("%H%M")
    m = b[(hh >= "0900") & (hh <= "1000")]
    AM10[d] = (abs(float(m["close"].iloc[-1]) - float(m["open"].iloc[0]))
               / float(m["open"].iloc[0]) * 100.0) if len(m) >= 2 else np.nan

CUM = {}          # 진입 직전 완성봉까지의 당일 |누적|
for t in A:
    k = E.last_done(t["entry_time"])
    b = DAYB.get(t["date"])
    v = np.nan
    if b is not None and k >= 0:
        seg = b[(b["datetime"] <= B3["datetime"].iloc[k])
                & (b["datetime"].dt.strftime("%H%M") >= "0900")]
        if len(seg) >= 2:
            o = float(seg["open"].iloc[0])
            v = abs(float(seg["close"].iloc[-1]) - o) / o * 100.0
    CUM[(t["date"], t["entry_time"])] = v

PPL, PN = N.PRIOR_PL, N.PRIOR_N
PREVD = N.PREVD
DPL = N.DPL


def am_ok(t):
    """오전 확정값을 이 거래에 쓸 수 있는가 — 도달이 10:00 이후여야 한다."""
    return t["touch_time"] is not None and t["touch_time"][11:16] >= "10:00"


RULES = {
    "L0 전부 절단":              lambda t: True,
    "L1 앞선거래 손실":            lambda t: PN[(t["date"], t["entry_time"])] > 0
                                           and PPL[(t["date"], t["entry_time"])] < 0,
    "L2 오전|누적|<1.5%":         lambda t: am_ok(t) and AM10.get(t["date"], 99) < 1.5,
    "L3 오전|누적|<2.0%":         lambda t: am_ok(t) and AM10.get(t["date"], 99) < 2.0,
    "L4 진입직전|누적|<1.0%":       lambda t: CUM[(t["date"], t["entry_time"])] < 1.0,
    "L5 전일 손실":              lambda t: DPL.get(PREVD.get(t["date"]), 0.0) < 0,
    "L6 L1 or L2":            lambda t: (PN[(t["date"], t["entry_time"])] > 0
                                         and PPL[(t["date"], t["entry_time"])] < 0)
                                        or (am_ok(t) and AM10.get(t["date"], 99) < 1.5),
    "L7 L1 and 오전약(<2.0)":    lambda t: PN[(t["date"], t["entry_time"])] > 0
                                          and PPL[(t["date"], t["entry_time"])] < 0
                                          and AM10.get(t["date"], 99) < 2.0,
}

print("=" * 132)
print("[N2] 손실일 필터 — 인과 규칙")
print("=" * 132)
print(f"  BASE 30일 {K.krw_pnl(A,W30):,.0f} / OOS48 {K.krw_pnl(A,OOS):,.0f} / "
      f"78일 {K.krw_pnl(A,D):,.0f}  (PF78 {K.pf(A,D):.3f} MDD78 {K.mdd_krw(A,D):.2f}%)")
print(f"  ORACLE 천장 (손실일 완전예지): 78일 +1,566,652")
print()
print(f"  {'규칙':22s} {'절단':>4s} {'개선':>4s} {'악화':>4s} {'30d uplift':>12s} {'OOS48':>12s} "
      f"{'78d':>12s} {'PF78':>6s} {'MDD78':>7s} {'>=3손':>5s} {'>=5손':>5s} {'>=8손':>5s} {'동시양수':>6s}")
print("  " + "-" * 128)
OKS = []
for nm, fn in RULES.items():
    r = N.build(fn)
    a, b, c = N.ev(r, W30), N.ev(r, OOS), N.ev(r, D)
    hit = "OK" if (a["up"] > 0 and b["up"] > 0) else ""
    if hit:
        OKS.append((nm, fn, r, a, b, c))
    print(f"  {nm:22s} {c['cut']:4d} {c['gain']:4d} {c['loss']:4d} {a['up']:+12,.0f} "
          f"{b['up']:+12,.0f} {c['up']:+12,.0f} {c['pf']:6.3f} {c['mdd']:7.2f} "
          f"{c['h3']:5d} {c['h5']:5d} {c['h8']:5d} {hit:>6s}")

print("\n  오전 임계 민감도 (L2/L3 계열, 도달 10:00 이후분만)")
print(f"    {'X%':>5s} {'절단':>4s} {'30d':>12s} {'OOS48':>12s} {'78d':>12s} {'>=3손':>5s}")
for x in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5):
    r = N.build(lambda t, xx=x: am_ok(t) and AM10.get(t["date"], 99) < xx)
    a, b, c = N.ev(r, W30), N.ev(r, OOS), N.ev(r, D)
    print(f"    {x:5.2f} {c['cut']:4d} {a['up']:+12,.0f} {b['up']:+12,.0f} "
          f"{c['up']:+12,.0f} {c['h3']:5d}")

print("\n  L1(앞선거래 손실) 적용 가능성")
elig = [t for t in A if PN[(t["date"], t["entry_time"])] > 0]
elig_neg = [t for t in elig if PPL[(t["date"], t["entry_time"])] < 0]
print(f"    앞선 거래가 있는 거래 {len(elig)}/{len(A)}건, 그중 앞선손익<0 {len(elig_neg)}건, "
      f"그중 +1.5% 도달 {sum(1 for t in elig_neg if t['n15'] is not None)}건")
print(f"    => slot1 {sum(1 for t in A if t['slot']==1)}건은 사전정보가 없어 **원천적으로 적용 불가**")

if OKS:
    print("\n" + "=" * 100)
    print("생존 규칙 강건성")
    print("=" * 100)
    for nm, fn, r, a, b, c in OKS:
        print(f"\n  ── {nm} ──")
        for tag, W in (("30일", W30), ("OOS48", OOS), ("78일", D)):
            da, db = K.daily_krw(A, W), K.daily_krw(r, W)
            v = np.array([db[d] - da[d] for d in W])
            bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
            loo = [v.sum() - v[i] for i in range(len(v))]
            print(f"    {tag:6s} uplift {v.sum():+11,.0f}  bootstrap P(>0)={(bt>0).mean()*100:6.2f}%  "
                  f"p5={np.percentile(bt,5):+11,.0f}  LOO 양수 {sum(1 for z in loo if z>0)}/{len(loo)}일  "
                  f"영향일수 {int((v!=0).sum())}")
        rs = N.build(fn, slip=0.1)
        print(f"    +0.1% 슬리피지  30d {N.ev(rs,W30)['up']:+11,.0f}  "
              f"OOS {N.ev(rs,OOS)['up']:+11,.0f}  78d {N.ev(rs,D)['up']:+11,.0f}")
        cut = [x for x in r if x.get("cut")]
        print(f"    절단 {len(cut)}건 전수:")
        AM = {(t['date'],t['entry_time']):t for t in A}
        for x in sorted(cut, key=lambda z: (z["date"], z["entry_time"])):
            bb = AM[(x["date"], x["entry_time"])]
            print(f"      {x['date']} {x['entry_time'][11:16]} slot{x['slot']} MFE {bb['mfe']:5.2f} "
                  f"{x['base_net_pct']:+6.2f}% -> {x['net_pct']:+6.2f}%  "
                  f"ΔKRW {x['pnl_krw']-bb['pnl_krw']:+10,.0f}  일손익 {DPL[x['date']]:+10,.0f}")
else:
    print("\n  30일·OOS48 동시 양수 규칙: 0개")
