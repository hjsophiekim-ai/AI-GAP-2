# -*- coding: utf-8 -*-
"""[P3] R2(ETF 추종<=0) 창별 분해 — 30일이 왜 음수인가."""
import sys; sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import numpy as np, k1_core as K
import p2_rules as R
A, D, W30, OOS, BASE = R.A, R.D, R.W30, R.OOS, R.BASE
f = R.f

for nm, fn in (("R2 ETF추종<=0%", lambda t: f(t, "ETF_추종%", 1) <= 0.0),
               ("R1 ETF역행>=3봉", lambda t: f(t, "ETF_역행봉수", 0) >= 3),
               ("R1&R2", lambda t: f(t, "ETF_역행봉수", 0) >= 3 and f(t, "ETF_추종%", 1) <= 0.0)):
    print("=" * 112); print(nm); print("=" * 112)
    print(f"  {'창':8s} {'제외':>4s} {'제외B':>5s} {'제외A':>5s} {'제외B 실현':>12s} "
          f"{'제외A 실현':>12s} {'제외 합':>12s} {'잔존 평균net':>11s} {'제외 평균net':>11s}")
    for W, tag in ((W30, "30일"), (OOS, "OOS48"), (D, "78일")):
        SW = set(W)
        g = [t for t in A if t["date"] in SW]
        dr = [t for t in g if fn(t)]
        kp = [t for t in g if not fn(t)]
        dB = [t for t in dr if t["mfe"] < 1.5]; dA = [t for t in dr if t["mfe"] >= 1.5]
        print(f"  {tag:8s} {len(dr):4d} {len(dB):5d} {len(dA):5d} "
              f"{sum(t['pnl_krw'] for t in dB):12,.0f} {sum(t['pnl_krw'] for t in dA):12,.0f} "
              f"{sum(t['pnl_krw'] for t in dr):12,.0f} "
              f"{np.mean([t['net_pct'] for t in kp]):+11.3f} "
              f"{np.mean([t['net_pct'] for t in dr]):+11.3f}")
    print(f"  30일 제외 A거래 (수익 포기분):")
    for t in sorted([t for t in A if t["date"] in set(W30) and fn(t) and t["mfe"] >= 1.5],
                    key=lambda z: -z["pnl_krw"]):
        print(f"    {t['date']} {t['entry_time'][11:16]} MFE {t['mfe']:5.2f} "
              f"net {t['net_pct']:+6.2f}% KRW {t['pnl_krw']:+10,.0f} "
              f"추종 {f(t,'ETF_추종%',0):+.2f}% 역행 {f(t,'ETF_역행봉수',0):.0f}봉")
    print()
print("=" * 112)
print("runner(MFE>=8%) 9건이 규칙에 걸리는가")
print("=" * 112)
print(f"  {'날짜':9s} {'진입':5s} {'창':4s} {'MFE':>6s} {'net%':>7s} {'추종%':>7s} {'역행':>4s} "
      f"{'R1':>3s} {'R2':>3s}")
for t in sorted([t for t in A if t["mfe"] >= 8], key=lambda z: z["date"]):
    w = "IS" if t["date"] in set(W30) else "OOS"
    print(f"  {t['date']:9s} {t['entry_time'][11:16]:5s} {w:4s} {t['mfe']:6.2f} {t['net_pct']:7.2f} "
          f"{f(t,'ETF_추종%',0):+7.2f} {f(t,'ETF_역행봉수',0):4.0f} "
          f"{'X' if f(t,'ETF_역행봉수',0)>=3 else '-':>3s} "
          f"{'X' if f(t,'ETF_추종%',1)<=0 else '-':>3s}")
