# -*- coding: utf-8 -*-
"""[N3] W1a POST_STOP x1.20 은 손실일에 해로운가 — 사이징만, 청산 불변."""
from __future__ import annotations
import sys
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import n1_lossday as N

A, TS, D, W30, OOS = N.A, N.TS, N.D, N.W30, N.OOS
DPL = N.DPL
rng = np.random.default_rng(20260922)
ORIG = K.POST_STOP_MULT
BASE = K.size_chain(TS, None)
b = {w: K.krw_pnl(BASE, w) for w in (tuple(W30), tuple(OOS), tuple(D))}
RUN8 = {(t["date"], t["entry_time"]) for t in A if t["mfe"] >= 8}

print("=" * 118)
print("[N3] 첫거래 STOP_LOSS 후 배수 (production 현행 x1.20) 민감도")
print("=" * 118)
fired = [t for t in BASE if t["w1a_rules"] > 1.0 + 1e-9]
print(f"  POST_STOP 발동 거래 {len(fired)}건 / 78일")
print(f"    그중 최종 이익 {sum(1 for t in fired if t['net_pct']>0)}건 / "
      f"손실 {sum(1 for t in fired if t['net_pct']<=0)}건  "
      f"평균 net {np.mean([t['net_pct'] for t in fired]):+.3f}%")
print(f"    발동일의 일손익: 손실일 {sum(1 for t in fired if DPL[t['date']]<0)}건 / "
      f"수익일 {sum(1 for t in fired if DPL[t['date']]>=0)}건")
print()
print(f"  {'배수':>6s} {'30d uplift':>12s} {'OOS48':>12s} {'78d':>12s} {'PF78':>6s} "
      f"{'MDD78':>7s} {'사용률':>6s} {'runner손상':>9s} {'boot P(>0) 78d':>14s}")
print("  " + "-" * 110)
for m in (1.00, 1.10, 1.20, 1.30):
    K.POST_STOP_MULT = m
    r = K.size_chain(TS, None)
    dmg = sum(1 for x, y in zip(BASE, r)
              if (x["date"], x["entry_time"]) in RUN8 and abs(x["net_pct"] - y["net_pct"]) > 1e-9)
    da, db = K.daily_krw(BASE, D), K.daily_krw(r, D)
    v = np.array([db[d] - da[d] for d in D])
    bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
    mark = "  <= 현행" if abs(m - ORIG) < 1e-9 else ""
    print(f"  {m:6.2f} {K.krw_pnl(r,W30)-b[tuple(W30)]:+12,.0f} "
          f"{K.krw_pnl(r,OOS)-b[tuple(OOS)]:+12,.0f} {K.krw_pnl(r,D)-b[tuple(D)]:+12,.0f} "
          f"{K.pf(r,D):6.3f} {K.mdd_krw(r,D):7.2f} {K.budget_stats(r,D)['util_pct']:5.1f}% "
          f"{dmg:9d} {(bt>0).mean()*100:13.2f}%{mark}")
K.POST_STOP_MULT = ORIG
