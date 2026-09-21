# -*- coding: utf-8 -*-
"""[H4] 최근접 셀(M=30/floor=-0.3) 절단 13건 전수 — 왜 지는지."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import h2_earlycut as X, h1_daygate as H
r = X.early(30, -0.3)
cut = sorted([x for x in r if x.get("cut")], key=lambda z: (z["date"], z["entry_time"]))
AM = {(t["date"], t["entry_time"]): t for t in H.A}
print(f"M=30분 / floor=-0.3%  절단 {len(cut)}건 (78일 uplift -226,701)")
print(f"  {'날짜':9s} {'진입':5s} {'MFE':>6s} {'30분net':>7s} {'원장net':>7s} {'차이':>7s} "
      f"{'ΔKRW':>10s} {'기존사유':24s}")
tot = 0
for x in cut:
    b = AM[(x["date"], x["entry_time"])]
    dk = x["pnl_krw"] - b["pnl_krw"]; tot += dk
    print(f"  {x['date']:9s} {x['entry_time'][11:16]:5s} {b['mfe']:6.2f} {x['net_pct']:7.2f} "
          f"{x['base_net_pct']:7.2f} {x['net_pct']-x['base_net_pct']:+7.2f} {dk:+10,.0f} "
          f"{b['exit_reason']:24s}")
print(f"  {'합계':>58s} {tot:+10,.0f}")
print("\n  개선 8건 합계 "
      f"{sum(x['pnl_krw']-AM[(x['date'],x['entry_time'])]['pnl_krw'] for x in cut if x['pnl_krw']>AM[(x['date'],x['entry_time'])]['pnl_krw']):+,.0f} / "
      f"악화 5건 합계 "
      f"{sum(x['pnl_krw']-AM[(x['date'],x['entry_time'])]['pnl_krw'] for x in cut if x['pnl_krw']<AM[(x['date'],x['entry_time'])]['pnl_krw']):+,.0f}")
