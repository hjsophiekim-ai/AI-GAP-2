# -*- coding: utf-8 -*-
"""[N4] POST_STOP 배수 — 엣지인가 레버리지인가."""
import sys; sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import numpy as np, k1_core as K, n1_lossday as N
A, TS, D, W30, OOS = N.A, N.TS, N.D, N.W30, N.OOS
rng = np.random.default_rng(20260922)
ORIG = K.POST_STOP_MULT
BASE = K.size_chain(TS, None)
b30, bo, b78 = K.krw_pnl(BASE, W30), K.krw_pnl(BASE, OOS), K.krw_pnl(BASE, D)
print(f"단조성 검사 (clip 상한 MAX_MULT = {K.MAX_MULT})")
print(f"  {'배수':>6s} {'30d':>11s} {'OOS48':>12s} {'78d':>12s} {'PF78':>6s} {'MDD78':>7s} {'사용률':>6s}")
for m in (1.00, 1.10, 1.20, 1.30, 1.40, 1.50, 1.75, 2.00):
    K.POST_STOP_MULT = m; r = K.size_chain(TS, None)
    print(f"  {m:6.2f} {K.krw_pnl(r,W30)-b30:+11,.0f} {K.krw_pnl(r,OOS)-bo:+12,.0f} "
          f"{K.krw_pnl(r,D)-b78:+12,.0f} {K.pf(r,D):6.3f} {K.mdd_krw(r,D):7.2f} "
          f"{K.budget_stats(r,D)['util_pct']:5.1f}%")
K.POST_STOP_MULT = ORIG
print("\n대조 — 전 거래를 같은 비율로 키우면 (순수 레버리지 기준선)")
print(f"  {'전거래배수':>8s} {'30d':>11s} {'OOS48':>12s} {'78d':>12s} {'PF78':>6s}")
for m in (1.02, 1.05, 1.10):
    r = K.size_chain(TS, extra_of=lambda t, mm=m: mm)
    print(f"  {m:8.2f} {K.krw_pnl(r,W30)-b30:+11,.0f} {K.krw_pnl(r,OOS)-bo:+12,.0f} "
          f"{K.krw_pnl(r,D)-b78:+12,.0f} {K.pf(r,D):6.3f}")
fired = [x for x in BASE if x["w1a_rules"] > 1.0 + 1e-9]
oth = [x for x in BASE if x["w1a_rules"] <= 1.0 + 1e-9]
print(f"\nPOST_STOP 거래 {len(fired)}건 평균 net {np.mean([x['net_pct'] for x in fired]):+.3f}% / "
      f"나머지 {len(oth)}건 {np.mean([x['net_pct'] for x in oth]):+.3f}%")
n = [x["net_pct"] for x in fired]; o = [x["net_pct"] for x in oth]
bt = np.array([np.mean(rng.choice(n, len(n))) - np.mean(rng.choice(o, len(o))) for _ in range(10000)])
print(f"부트스트랩 P(POST_STOP 평균 > 나머지) = {(bt>0).mean()*100:.2f}%")
for tag, W in (("30일", W30), ("OOS48", OOS), ("78일", D)):
    g = [x for x in fired if x["date"] in set(W)]
    print(f"  {tag:6s} {len(g):2d}건 평균 net {np.mean([x['net_pct'] for x in g]):+.3f}%  "
          f"승률 {np.mean([x['net_pct']>0 for x in g])*100:.0f}%")
