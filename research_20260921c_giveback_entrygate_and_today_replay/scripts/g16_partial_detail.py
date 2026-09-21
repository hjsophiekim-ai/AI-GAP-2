# -*- coding: utf-8 -*-
"""[PT2] PT15-X 분해 / 강건성 / Q1~Q4. READ-ONLY."""
from __future__ import annotations
import sys
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g15_partial as G

A, D, W30, OOS = G.A, G.D, G.W30, G.OOS
FRACS = G.FRACS
rng = np.random.default_rng(20260921)
WINS = (("30일 IS", W30), ("앞48일 OOS", OOS), ("78일", D))


def delta(t, frac):
    """이 거래에서 부분익절이 만든 KRW 차이 (양수=보호, 음수=기회손실)."""
    if t["n15"] is None or t["qty"] <= 0 or frac <= 0:
        return 0.0
    tq = max(0, min(int(t["qty"]), int(round(t["qty"] * frac))))
    return tq * t["entry_price"] * (t["n15"] - t["net_pct"]) / 100.0


print("=" * 118)
print("[PT2] 부분익절 분해 — Δ = tp수량 x 진입가 x (도달봉종가net - 원장net) / 100")
print("=" * 118)

# ── Q1 / Q2 / Q3 : 버킷 분해 ────────────────────────────────────────────
BUCK = [
    ("Q1 도달 & 최종<+1.0% (짧은랠리)", lambda t: t["n15"] is not None and t["net_pct"] < 1.0),
    ("   도달 & 최종>=+1.0%",           lambda t: t["n15"] is not None and t["net_pct"] >= 1.0),
    ("   MFE < 3%  (도달분만)",          lambda t: t["n15"] is not None and t["mfe"] < 3.0),
    ("   MFE >= 3%",                    lambda t: t["n15"] is not None and t["mfe"] >= 3.0),
    ("Q2 MFE >= 5%",                    lambda t: t["n15"] is not None and t["mfe"] >= 5.0),
    ("   MFE >= 8% (runner)",           lambda t: t["n15"] is not None and t["mfe"] >= 8.0),
]
for wname, W in WINS:
    SW = set(W)
    print(f"\n── {wname} ── 버킷별 Δ (KRW)")
    print(f"  {'버킷':32s} {'건수':>4s} " + " ".join(f"{n.split()[1]:>12s}" for n, _ in FRACS[1:]))
    print("  " + "-" * 100)
    for bname, fn in BUCK:
        g = [t for t in A if t["date"] in SW and fn(t)]
        cells = " ".join(f"{sum(delta(t, f) for t in g):+12,.0f}" for _, f in FRACS[1:])
        print(f"  {bname:32s} {len(g):4d} {cells}")
    tot = [t for t in A if t["date"] in SW and t["n15"] is not None]
    print(f"  {'Q3 순효과 (보호 - 기회손실)':32s} {len(tot):4d} "
          + " ".join(f"{sum(delta(t, f) for t in tot):+12,.0f}" for _, f in FRACS[1:]))
    pos = " ".join(f"{('보호' if sum(max(0,delta(t,f)) for t in tot) else ''):>12s}" for _, f in FRACS[1:])
    print(f"  {'  (그중 보호 합)':32s} {'':4s} "
          + " ".join(f"{sum(max(0.0, delta(t, f)) for t in tot):+12,.0f}" for _, f in FRACS[1:]))
    print(f"  {'  (그중 기회손실 합)':32s} {'':4s} "
          + " ".join(f"{sum(min(0.0, delta(t, f)) for t in tot):+12,.0f}" for _, f in FRACS[1:]))

# ── runner 개별 손상 ───────────────────────────────────────────────────
print("\n" + "=" * 118)
print("runner (MFE >= 8%) 개별 수익 감소액")
print("=" * 118)
run8 = sorted([t for t in A if t["mfe"] >= 8.0], key=lambda t: t["date"])
print(f"  {'날짜':9s} {'진입':5s} {'창':4s} {'MFE':>6s} {'도달봉net':>8s} {'원장net':>8s} "
      f"{'수량':>5s} " + " ".join(f"{n.split()[1]:>11s}" for n, _ in FRACS[1:]))
for t in run8:
    w = "IS" if t["date"] in set(W30) else "OOS"
    if t["n15"] is None:
        continue
    print(f"  {t['date']:9s} {t['entry_time'][11:16]:5s} {w:4s} {t['mfe']:6.2f} "
          f"{t['n15']:8.2f} {t['net_pct']:8.2f} {t['qty']:5d} "
          + " ".join(f"{delta(t, f):+11,.0f}" for _, f in FRACS[1:]))
print(f"  {'합계':>54s} " + " ".join(
    f"{sum(delta(t, f) for t in run8):+11,.0f}" for _, f in FRACS[1:]))

# ── Q4 plateau ─────────────────────────────────────────────────────────
print("\n" + "=" * 100)
print("Q4 plateau 검사 — 비율 10%p 늘릴 때마다 uplift 가 얼마나 더 나빠지는가")
print("=" * 100)
for wname, W in WINS:
    base = K.krw_pnl(G.BASE, W)
    ups = [K.krw_pnl(G.variant(f), W) - base for _, f in FRACS[1:]]
    print(f"  {wname:11s} " + " ".join(f"{u:+12,.0f}" for u in ups))
    per10 = [u / (f * 10) for u, (_, f) in zip(ups, FRACS[1:])]
    print(f"  {'  10%당':11s} " + " ".join(f"{p:+12,.0f}" for p in per10)
          + "   <= 일정하면 plateau 없음(순수 선형 비용)")

# ── 강건성 ─────────────────────────────────────────────────────────────
print("\n" + "=" * 112)
print("강건성 — 체결/반올림 민감도 (78일 uplift KRW)")
print("=" * 112)
print(f"  {'조건':28s} " + " ".join(f"{n.split()[1]:>13s}" for n, _ in FRACS[1:]))
for tag, kw in (("기준 (도달봉 종가, 반올림)", {}),
                ("+0.1% 슬리피지", {"slip": 0.1}),
                ("정수주 내림(floor)", {"rounding": "floor"}),
                ("슬리피지 & 내림", {"slip": 0.1, "rounding": "floor"})):
    base = K.krw_pnl(G.BASE, D)
    print(f"  {tag:28s} " + " ".join(
        f"{K.krw_pnl(G.variant(f, **kw), D) - base:+13,.0f}" for _, f in FRACS[1:]))

# ── bootstrap / LOO ────────────────────────────────────────────────────
print("\n" + "=" * 112)
print("부트스트랩 10,000회 (일단위) / LOO (하루씩 제거)")
print("=" * 112)
for wname, W in WINS:
    print(f"\n  ── {wname} ──")
    da = K.daily_krw(G.BASE, W)
    for nm, f in FRACS[1:]:
        r = G.RUNS[nm]
        db = K.daily_krw(r, W)
        v = np.array([db[d] - da[d] for d in W])
        bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
        loo = [v.sum() - v[i] for i in range(len(v))]
        print(f"    {nm:12s} P(>0)={(bt>0).mean()*100:6.2f}%  p5={np.percentile(bt,5):+12,.0f}  "
              f"중앙={np.percentile(bt,50):+12,.0f}  p95={np.percentile(bt,95):+12,.0f}  "
              f"LOO 양수 {sum(1 for x in loo if x > 0)}/{len(loo)}일")
