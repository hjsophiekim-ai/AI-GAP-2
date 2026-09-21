# -*- coding: utf-8 -*-
"""[H3] 최선의 일단위 신호(하이닉스 당일 |누적변화|)로 러너날 게이트 전수 스윕.

AUC 0.692 (오전 09:00~10:00 |누적|) 가 이 세션에서 찾은 최고 판별력이다.
그 축으로 임계값을 훑어 "안 가는 날만 +1.5% 절단" 이 성립하는 칸이 있는지 본다.
READ-ONLY. 판정은 전부 완성봉/인과 정보.
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g6_entry as E
import h1_daygate as H

A, TS, D, W30, OOS = H.A, H.TS, H.D, H.W30, H.OOS
rng = np.random.default_rng(20260922)

# 09:00~10:00 완성봉 |누적| (10:00 이후에만 사용 가능)
AM = {}
for d, b in H.DAYB.items():
    m = b[b["datetime"].dt.strftime("%H%M") <= "1000"]
    if len(m):
        o = float(m["open"].iloc[0])
        AM[d] = abs(float(m["close"].iloc[-1]) - o) / o * 100.0

DMAX = defaultdict(float)
for t in A:
    DMAX[t["date"]] = max(DMAX[t["date"]], t["mfe"])


def touch_hhmm(t):
    tp = t["tp"]
    if tp is None:
        return None
    import t1_path as P
    return str(P.path(t)["datetime"].iloc[tp["i"]])[11:16]


TH = {(t["date"], t["entry_time"]): touch_hhmm(t) for t in A}

print("=" * 122)
print("[H3] 러너날 게이트 — 하이닉스 당일 |누적변화| 기준 (AUC 0.692 축)")
print("=" * 122)
print(f"  ORACLE 상한 재확인 (미래정보): 일단위 예지 78일 +1,878,813 / 거래단위 예지 +4,498,217")
print()

# ── (1) 도달 시점까지의 당일 |누적| 기준 ────────────────────────────────
print("(1) 도달 시점 기준 — |09:00~도달봉 누적| >= X 면 러너날로 보고 현행 유지")
print(f"  {'X%':>5s} {'절단':>4s} {'개선':>4s} {'악화':>4s} {'30d uplift':>12s} {'OOS48':>12s} "
      f"{'78d':>12s} {'PF78':>6s} {'>=3손':>5s} {'>=5손':>5s} {'>=8손':>5s} {'동시양수':>6s}")
print("  " + "-" * 118)
ok1 = []
for x in (0.5, 1.0, 1.3, 1.6, 2.0, 2.5, 3.0):
    r = H.build(lambda t, xx=x: abs(H._f(t, "cum", 0.0)) >= xx)
    a, b, c = H.ev(r, W30), H.ev(r, OOS), H.ev(r, D)
    hit = "OK" if (a["up"] > 0 and b["up"] > 0) else ""
    if hit:
        ok1.append(x)
    print(f"  {x:5.1f} {c['cut']:4d} {c['gain']:4d} {c['loss']:4d} {a['up']:+12,.0f} "
          f"{b['up']:+12,.0f} {c['up']:+12,.0f} {c['pf']:6.3f} {c['h3']:5d} {c['h5']:5d} "
          f"{c['h8']:5d} {hit:>6s}")

# ── (2) 오전(09:00~10:00) 확정 |누적| 기준, 10:00 이후 도달분에만 적용 ──
print("\n(2) 오전 확정 기준 — 09:00~10:00 |누적| < X 이고 도달이 10:00 이후일 때만 절단")
print(f"  {'X%':>5s} {'절단':>4s} {'개선':>4s} {'악화':>4s} {'30d uplift':>12s} {'OOS48':>12s} "
      f"{'78d':>12s} {'PF78':>6s} {'>=3손':>5s} {'>=5손':>5s} {'>=8손':>5s} {'동시양수':>6s}")
print("  " + "-" * 118)


def sel2(t, x):
    """True = 현행 유지."""
    hh = TH.get((t["date"], t["entry_time"]))
    if hh is None or hh < "10:00":
        return True                      # 오전값이 아직 없다 -> 손대지 않는다
    return AM.get(t["date"], 99.0) >= x


ok2 = []
for x in (1.0, 1.3, 1.6, 2.0, 2.5, 3.0):
    r = H.build(lambda t, xx=x: sel2(t, xx))
    a, b, c = H.ev(r, W30), H.ev(r, OOS), H.ev(r, D)
    hit = "OK" if (a["up"] > 0 and b["up"] > 0) else ""
    if hit:
        ok2.append((x, r, a, b, c))
    print(f"  {x:5.1f} {c['cut']:4d} {c['gain']:4d} {c['loss']:4d} {a['up']:+12,.0f} "
          f"{b['up']:+12,.0f} {c['up']:+12,.0f} {c['pf']:6.3f} {c['h3']:5d} {c['h5']:5d} "
          f"{c['h8']:5d} {hit:>6s}")

print(f"\n  (1) 동시양수 {len(ok1)}칸 / (2) 동시양수 {len(ok2)}칸")

# ── 생존 칸이 있으면 강건성 ─────────────────────────────────────────────
for x, r, a, b, c in ok2:
    print(f"\n  ── X={x} 강건성 ──")
    for tag, W in (("30일", W30), ("OOS48", OOS), ("78일", D)):
        da, db = K.daily_krw(H.build(lambda t: True), W), K.daily_krw(r, W)
        v = np.array([db[d] - da[d] for d in W])
        bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
        loo = [v.sum() - v[i] for i in range(len(v))]
        print(f"    {tag:6s} bootstrap P(>0)={(bt>0).mean()*100:6.2f}%  "
              f"p5={np.percentile(bt,5):+11,.0f}  LOO 양수 {sum(1 for z in loo if z>0)}/{len(loo)}일")
    rs = H.build(lambda t, xx=x: sel2(t, xx), slip=0.1)
    print(f"    +0.1% 슬리피지  30d {H.ev(rs,W30)['up']:+11,.0f}  OOS {H.ev(rs,OOS)['up']:+11,.0f}  "
          f"78d {H.ev(rs,D)['up']:+11,.0f}")
