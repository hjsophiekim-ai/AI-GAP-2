# -*- coding: utf-8 -*-
"""[G section5b] R2 의 창 의존성 해부 — 30일 밖에서도 성립하는가. READ-ONLY."""
from __future__ import annotations
import sys
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g3_rules as G

A, TS, D, W30 = G.A, G.TS, G.D, G.W30
AMAP = G.AMAP
OOS = D[:48]

print("=" * 116)
print("[G section5b] 창 의존성 — R2 는 최근 30일에서만 좋은가")
print("=" * 116)
print(f"  {'창':16s} {'영업일':>5s} {'발동':>4s} {'개선':>4s} {'악화':>4s} {'BASE':>12s} "
      f"{'R2':>12s} {'uplift':>11s} {'PF(BASE→R2)':>16s} {'MDD(BASE→R2)':>15s}")
print("  " + "-" * 112)
m = G.metrics(G.R2, W=D)
for tag, W in (("최근 30일 (IS)", W30), ("앞 48일 (OOS)", OOS), ("78일 전체", D)):
    up = K.krw_pnl(m["sz"], W) - K.krw_pnl(A, W)
    fired = [x for x in m["sim"] if x["fired"] and x["date"] in set(W)]
    print(f"  {tag:16s} {len(W):5d} {len(fired):4d} "
          f"{sum(1 for x in fired if x['net_pct']>x['base_net_pct']+1e-9):4d} "
          f"{sum(1 for x in fired if x['net_pct']<x['base_net_pct']-1e-9):4d} "
          f"{K.krw_pnl(A,W):12,.0f} {K.krw_pnl(m['sz'],W):12,.0f} {up:+11,.0f} "
          f"{K.pf(A,W):7.3f} → {K.pf(m['sz'],W):6.3f} {K.mdd_krw(A,W):7.2f} → {K.mdd_krw(m['sz'],W):5.2f}")

SM = {(x["date"], x["entry_time"]): x for x in m["sz"]}
print(f"\n  78일 발동 전량 ({sum(1 for x in m['sim'] if x['fired'])}건) — OOS 구간 표시")
print(f"    {'날짜':9s} {'구간':5s} {'진입':5s} {'기존%':>7s} {'R2%':>7s} {'차이':>7s} {'ΔKRW':>10s} {'기존사유':26s}")
tot_is = tot_oos = 0
for x in sorted([z for z in m["sim"] if z["fired"]], key=lambda z: (z["date"], z["entry_time"])):
    k = (x["date"], x["entry_time"])
    dk = SM[k]["pnl_krw"] - AMAP[k]["pnl_krw"]
    seg = "IS" if x["date"] in set(W30) else "OOS"
    tot_is += dk if seg == "IS" else 0
    tot_oos += dk if seg == "OOS" else 0
    print(f"    {x['date']:9s} {seg:5s} {x['entry_time'][11:16]:5s} {x['base_net_pct']:7.2f} "
          f"{x['net_pct']:7.2f} {x['net_pct']-x['base_net_pct']:+7.2f} {dk:+10,.0f} {x['base_exit_reason']:26s}")
print(f"    IS 합계 {tot_is:+,.0f} / OOS 합계 {tot_oos:+,.0f}")

print("\n  종가위치 임계 민감도 — 78일 uplift (30일과 부호가 같은가)")
print(f"    {'pos<':>6s} {'30일 uplift':>13s} {'78일 uplift':>13s} {'OOS48 uplift':>14s}")
for q in (0.25, 0.30, 0.35, 0.40, 0.50):
    mm = G.metrics(lambda t, p, qq=q: G.R2(t, p, pos_thr=qq), W=D)
    print(f"    {q:6.2f} {K.krw_pnl(mm['sz'],W30)-K.krw_pnl(A,W30):+13,.0f} "
          f"{mm['up']:+13,.0f} {K.krw_pnl(mm['sz'],OOS)-K.krw_pnl(A,OOS):+14,.0f}")

print("\n  참고 — R1/R3 도 78일에서 확인")
for name, fn in (("R1 트레일 -1.0%p", G.R1), ("R3 gap 2연속축소", G.R3)):
    mm = G.metrics(fn, W=D)
    print(f"    {name:18s} 78일 uplift {mm['up']:+12,.0f}  PF {mm['pf']:.3f} (BASE {K.pf(A,D):.3f})  "
          f"발동 {mm['n']}  runner손상 {mm['runner']}")
