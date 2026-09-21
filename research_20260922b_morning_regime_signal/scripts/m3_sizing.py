# -*- coding: utf-8 -*-
"""[M3] 오전강도 분위 -> sizing 배수. 진입/청산 불변이므로 runner 손상 구조적 0."""
from __future__ import annotations
import sys
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g6_entry as E
import m2_causal as M2

A, TS, D, W30, OOS = E.A, E.TS, E.D, E.W30, E.OOS
rng = np.random.default_rng(20260922)
CUM = M2.CUM
ABSV = {k: abs(v) for k, v in CUM.items()}
qs = np.nanpercentile([v for v in ABSV.values()], [25, 50, 75])
print("=" * 112)
print("[M3] 오전강도(진입직전 당일 |누적|) 분위 sizing — 경계 "
      f"{qs[0]:.3f} / {qs[1]:.3f} / {qs[2]:.3f} %")
print("=" * 112)


def qof(t):
    v = ABSV.get((t["date"], t["entry_time"]), np.nan)
    if v != v:
        return 2                                  # 판정 불가 -> 중립(Q3)
    return int(np.searchsorted(qs, v))            # 0=Q1약 .. 3=Q4강


SCH = {
    "S0 고정 1.00":                 [1.00, 1.00, 1.00, 1.00],
    "S1 0.75/0.90/1.00/1.10":     [0.75, 0.90, 1.00, 1.10],
    "S2 0.80/0.90/1.00/1.05":     [0.80, 0.90, 1.00, 1.05],
    "S3 약한날만 0.75":               [0.75, 1.00, 1.00, 1.00],
}
BASE = K.size_chain(TS, None)
b30, bo, b78 = (K.krw_pnl(BASE, W) for W in (W30, OOS, D))
RUN8 = {(t["date"], t["entry_time"]) for t in A if t["mfe"] >= 8}
print(f"  BASE  30일 {b30:,.0f} / OOS48 {bo:,.0f} / 78일 {b78:,.0f}  "
      f"PF78 {K.pf(BASE,D):.3f}  MDD78 {K.mdd_krw(BASE,D):.2f}%  "
      f"사용률 {K.budget_stats(BASE,D)['util_pct']:.1f}%")
print()
print(f"  {'전략':26s} {'30d uplift':>12s} {'OOS48':>12s} {'78d':>12s} {'PF78':>6s} "
      f"{'MDD78':>7s} {'사용률':>6s} {'runner손상':>9s} {'-Top1Δ':>10s} {'-Top3Δ':>10s} {'-Top5Δ':>10s}")
print("  " + "-" * 128)
RES = {}
for nm, mult in SCH.items():
    r = K.size_chain(TS, extra_of=lambda t, m=mult: m[qof(t)])
    RES[nm] = r
    dmg = sum(1 for x, y in zip(BASE, r)
              if (x["date"], x["entry_time"]) in RUN8 and abs(x["net_pct"] - y["net_pct"]) > 1e-9)
    bs = K.budget_stats(r, D)
    print(f"  {nm:26s} {K.krw_pnl(r,W30)-b30:+12,.0f} {K.krw_pnl(r,OOS)-bo:+12,.0f} "
          f"{K.krw_pnl(r,D)-b78:+12,.0f} {K.pf(r,D):6.3f} {K.mdd_krw(r,D):7.2f} "
          f"{bs['util_pct']:5.1f}% {dmg:9d} "
          + " ".join(f"{K.excl_top_krw(r,D,k)-K.excl_top_krw(BASE,D,k):+10,.0f}" for k in (1, 3, 5)))

print("\n  부트스트랩 10,000회 (일단위)")
for nm in list(SCH)[1:]:
    r = RES[nm]
    line = f"    {nm:26s}"
    for W, tag in ((W30, "30일"), (OOS, "OOS48"), (D, "78일")):
        da, db = K.daily_krw(BASE, W), K.daily_krw(r, W)
        v = np.array([db[d] - da[d] for d in W])
        bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
        line += f"  {tag} P(>0)={(bt>0).mean()*100:6.2f}%"
    print(line)

print("\n  분위별 거래수/평균 net% (인과 기준)")
for i, lab in enumerate(["Q1약", "Q2", "Q3", "Q4강"]):
    g = [t for t in A if qof(t) == i]
    print(f"    {lab:5s} {len(g):3d}건  평균 net {np.mean([t['net_pct'] for t in g]):+6.3f}%  "
          f"평균 MFE {np.mean([t['mfe'] for t in g]):5.2f}  "
          f">=3% {np.mean([t['mfe']>=3 for t in g])*100:4.1f}%  "
          f"실현 {sum(t['pnl_krw'] for t in g):+12,.0f}")
