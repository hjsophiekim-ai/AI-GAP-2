# -*- coding: utf-8 -*-
"""[E section3] 최종 조합 A/B/C/D 비교 + bootstrap. READ-ONLY."""
from __future__ import annotations
import sys
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g7_combo as G

A, W30, OOS, D = G.A, G.W30, G.OOS, G.D
LOCK = dict(floor=0.5, rp_mfe3=True, rp_trend=True)     # 30일 기준 최선의 lock
G2 = G.GATES["G2 CHOP & 비추세"]
G1b = G.GATES["G1b gapD<143.8 (임계적합)"]

COMBOS = {
    "A BASE (N1+C1)":              G.build(None, None),
    "B Entry Gate G2":             G.build(G2, None),
    "B' Entry Gate G1b":           G.build(G1b, None),
    "C Profit-Lock P1+RP":         G.build(None, LOCK["floor"], rp_mfe3=True, rp_trend=True),
    "D G2 + P1+RP":                G.build(G2, LOCK["floor"], rp_mfe3=True, rp_trend=True),
    "D' G1b + P1+RP":              G.build(G1b, LOCK["floor"], rp_mfe3=True, rp_trend=True),
}
nB30 = sum(1 for t in A if t["date"] in set(W30) and t["mfe"] < 1.5)
n3 = lambda w: sum(1 for t in A if t["date"] in set(w) and t["mfe"] >= 3)

print("=" * 150)
print("[E section3] 최종 조합 — 30일 / 앞48일 OOS / 78일")
print("=" * 150)
for W, tag in ((W30, "30일 (IS)"), (OOS, "앞48일 (OOS)"), (D, "78일")):
    print(f"\n  ── {tag} ── BASE {K.krw_pnl(A,W):,.0f} KRW / PF {K.pf(A,W):.3f} / "
          f"MDD {K.mdd_krw(A,W):.2f}% / 거래 {sum(1 for t in A if t['date'] in set(W))} / "
          f"MFE>=3% {n3(W)}건")
    print(f"  {'조합':22s} {'거래':>4s} {'P/L':>12s} {'uplift':>12s} {'PF':>6s} {'MDD%':>6s} "
          f"{'B제거':>5s} {'B제거율':>6s} {'>=3손':>5s} {'>=5손':>5s} {'>=8손':>5s} "
          f"{'lock발동':>7s} {'-Top1Δ':>11s} {'-Top3Δ':>11s} {'boot P(>0)':>10s}")
    print("  " + "-" * 146)
    for nm, sim in COMBOS.items():
        m = G.ev(sim, W)
        nb = sum(1 for t in A if t["date"] in set(W) and t["mfe"] < 1.5)
        p0, _, _ = G.boot(sim, W)
        dmg3 = m["d3"] + m["h3"]
        print(f"  {nm:22s} {m['n']:4d} {m['krw']:12,.0f} {m['up']:+12,.0f} {m['pf']:6.3f} "
              f"{m['mdd']:6.2f} {m['dropB']:5d} {m['dropB']/max(1,nb)*100:5.1f}% "
              f"{dmg3:5d} {m['d5']+m['h5']:5d} {m['d8']+m['h8']:5d} {m['fired']:7d} "
              f"{m['t1']:+11,.0f} {m['t3']:+11,.0f} {p0:9.2f}%")

print()
print("=" * 104)
print("보조 확인")
print("=" * 104)
sim = COMBOS["C Profit-Lock P1+RP"]
fired = [x for x in sim if x["fired"] and x["date"] in set(W30)]
print(f"  C 발동 {len(fired)}건 (30일)")
print(f"    {'날짜':9s} {'진입':5s} {'MFE':>6s} {'기존%':>7s} {'lock%':>7s} {'차이':>7s} {'기존사유':26s}")
for x in sorted(fired, key=lambda z: (z["date"], z["entry_time"])):
    print(f"    {x['date']:9s} {x['entry_time'][11:16]:5s} "
          f"{G.MFE[(x['date'],x['entry_time'])]:6.2f} {x['base_net_pct']:7.2f} {x['net_pct']:7.2f} "
          f"{x['net_pct']-x['base_net_pct']:+7.2f} {x['base_exit_reason']:26s}")
print("\n  강건성 (C, 30일)")
for tag, kw in (("기준", {}), ("+0.1% 슬리피지", {"slip": 0.1}), ("+1분 지연", {"delay": 1})):
    s = G.build(None, LOCK["floor"], rp_mfe3=True, rp_trend=True, **kw)
    print(f"    {tag:14s} uplift {K.ev if False else G.ev(s, W30)['up']:+12,.0f}")
print("\n  G2 제거 9건 상세 (30일)")
keys = {(x["date"], x["entry_time"]) for x in COMBOS["B Entry Gate G2"]}
for t in A:
    if t["date"] in set(W30) and (t["date"], t["entry_time"]) not in keys:
        print(f"    {t['date']} {t['entry_time'][11:16]} MFE {t['mfe']:5.2f}  net {t['net_pct']:+6.2f}  "
              f"KRW {t['pnl_krw']:+10,.0f}  {t['exit_reason']}")
