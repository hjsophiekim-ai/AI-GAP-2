# -*- coding: utf-8 -*-
"""[P2] confirmation-path 기반 진입 배제 규칙. READ-ONLY.

측정 방식: removal-only — 규칙에 걸린 거래를 **없던 것으로** 제거한다.
엔진 재실행이 불가해 슬롯이 비어 생겼을 대체 진입은 재현하지 않는다.
w1a CHOP/post-stop 배수와 일예산 30M 재배분은 size_chain 이 순차 반영한다.
"""
from __future__ import annotations
import sys
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g6_entry as E
import p1_confirm as C

A, TS, D, W30, OOS = C.A, C.TS, C.D, C.W30, C.OOS
FE = C.FE
AMAP = {(t["date"], t["entry_time"]): t for t in A}
BASE = K.size_chain(TS, None)
rng = np.random.default_rng(20260922)


def f(t, k, d=np.nan):
    return FE.get((t["date"], t["entry_time"]), {}).get(k, d)


def build(drop):
    out = [dict(r) for r in TS
           if not drop(AMAP[(r["date"], r["entry_time"])])]
    return K.size_chain(out, None)


def ev(rows, W, drop):
    SW = set(W)
    keys = {(x["date"], x["entry_time"]) for x in rows}
    dropped = [t for t in A if t["date"] in SW and (t["date"], t["entry_time"]) not in keys]
    base = K.krw_pnl(BASE, W)
    return {"up": K.krw_pnl(rows, W) - base, "pf": K.pf(rows, W), "mdd": K.mdd_krw(rows, W),
            "n": sum(1 for x in rows if x["date"] in SW),
            "dB": sum(1 for t in dropped if t["mfe"] < 1.5),
            "dA": sum(1 for t in dropped if t["mfe"] >= 1.5),
            "d3": sum(1 for t in dropped if t["mfe"] >= 3),
            "d5": sum(1 for t in dropped if t["mfe"] >= 5),
            "d8": sum(1 for t in dropped if t["mfe"] >= 8),
            "t1": K.excl_top_krw(rows, W, 1) - K.excl_top_krw(BASE, W, 1),
            "t3": K.excl_top_krw(rows, W, 3) - K.excl_top_krw(BASE, W, 3)}


RULES = {
    "R1 ETF역행 >=3봉":        lambda t: f(t, "ETF_역행봉수", 0) >= 3,
    "R2 ETF추종 <= 0%":       lambda t: f(t, "ETF_추종%", 1) <= 0.0,
    "R3 하이닉스 미갱신":          lambda t: f(t, "고점갱신", 1) == 0.0,
    "R1+R2 (or)":           lambda t: f(t, "ETF_역행봉수", 0) >= 3 or f(t, "ETF_추종%", 1) <= 0.0,
    "R1&R2 (and)":          lambda t: f(t, "ETF_역행봉수", 0) >= 3 and f(t, "ETF_추종%", 1) <= 0.0,
    "R1+R3 (or)":           lambda t: f(t, "ETF_역행봉수", 0) >= 3 or f(t, "고점갱신", 1) == 0.0,
    "R2&R3 (and)":          lambda t: f(t, "ETF_추종%", 1) <= 0.0 and f(t, "고점갱신", 1) == 0.0,
}

print("=" * 136)
print("[P2] confirmation-path 진입 배제 규칙 (removal-only)")
print("=" * 136)
for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
    print(f"  BASE {tag:10s} {K.krw_pnl(BASE,W):12,.0f} KRW  PF {K.pf(BASE,W):.3f}  "
          f"MDD {K.mdd_krw(BASE,W):6.2f}%  거래 {sum(1 for t in BASE if t['date'] in set(W))}")
print()
print(f"  {'규칙':18s} {'제외':>4s} {'B제거':>5s} {'A제거':>5s} {'>=3':>4s} {'>=5':>4s} {'>=8':>4s} "
      f"{'30d uplift':>12s} {'OOS48':>12s} {'78d':>12s} {'PF78':>6s} {'MDD78':>7s} {'동시양수':>6s}")
print("  " + "-" * 132)
OK = []
for nm, fn in RULES.items():
    r = build(fn)
    a, b, c = ev(r, W30, fn), ev(r, OOS, fn), ev(r, D, fn)
    hit = "OK" if (a["up"] > 0 and b["up"] > 0) else ""
    if hit:
        OK.append((nm, fn, r, a, b, c))
    print(f"  {nm:18s} {c['dA']+c['dB']:4d} {c['dB']:5d} {c['dA']:5d} {c['d3']:4d} {c['d5']:4d} "
          f"{c['d8']:4d} {a['up']:+12,.0f} {b['up']:+12,.0f} {c['up']:+12,.0f} "
          f"{c['pf']:6.3f} {c['mdd']:7.2f} {hit:>6s}")

print("\n  임계 민감도 — ETF 역행봉수 >= X (confirmation 5봉 중)")
print(f"    {'X':>2s} {'제외':>4s} {'B':>3s} {'A':>3s} {'>=3':>4s} {'>=8':>4s} {'30d':>12s} "
      f"{'OOS48':>12s} {'78d':>12s} {'PF78':>6s} {'동시양수':>6s}")
for x in (2, 3, 4, 5):
    fn = lambda t, xx=x: f(t, "ETF_역행봉수", 0) >= xx
    r = build(fn)
    a, b, c = ev(r, W30, fn), ev(r, OOS, fn), ev(r, D, fn)
    print(f"    {x:2d} {c['dA']+c['dB']:4d} {c['dB']:3d} {c['dA']:3d} {c['d3']:4d} {c['d8']:4d} "
          f"{a['up']:+12,.0f} {b['up']:+12,.0f} {c['up']:+12,.0f} {c['pf']:6.3f} "
          f"{'OK' if a['up']>0 and b['up']>0 else '':>6s}")

print("\n  임계 민감도 — ETF 추종% <= X")
print(f"    {'X%':>6s} {'제외':>4s} {'B':>3s} {'A':>3s} {'>=3':>4s} {'>=8':>4s} {'30d':>12s} "
      f"{'OOS48':>12s} {'78d':>12s} {'PF78':>6s} {'동시양수':>6s}")
for x in (-0.3, -0.1, 0.0, 0.1, 0.3):
    fn = lambda t, xx=x: f(t, "ETF_추종%", 99) <= xx
    r = build(fn)
    a, b, c = ev(r, W30, fn), ev(r, OOS, fn), ev(r, D, fn)
    print(f"    {x:6.2f} {c['dA']+c['dB']:4d} {c['dB']:3d} {c['dA']:3d} {c['d3']:4d} {c['d8']:4d} "
          f"{a['up']:+12,.0f} {b['up']:+12,.0f} {c['up']:+12,.0f} {c['pf']:6.3f} "
          f"{'OK' if a['up']>0 and b['up']>0 else '':>6s}")

if OK:
    print("\n" + "=" * 110)
    print("생존 규칙 강건성")
    print("=" * 110)
    for nm, fn, r, a, b, c in OK:
        print(f"\n  ── {nm} ──  제외 {c['dA']+c['dB']}건 (B {c['dB']} / A {c['dA']}), "
              f"runner>=8% 손상 {c['d8']}건")
        for tag, W, m in (("30일", W30, a), ("OOS48", OOS, b), ("78일", D, c)):
            da, db = K.daily_krw(BASE, W), K.daily_krw(r, W)
            v = np.array([db[d] - da[d] for d in W])
            bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
            loo = [v.sum() - v[i] for i in range(len(v))]
            print(f"    {tag:6s} uplift {m['up']:+11,.0f}  PF {m['pf']:6.3f}  MDD {m['mdd']:6.2f}  "
                  f"boot P(>0)={(bt>0).mean()*100:6.2f}%  p5={np.percentile(bt,5):+11,.0f}  "
                  f"LOO 양수 {sum(1 for z in loo if z>0)}/{len(loo)}일  "
                  f"-Top1Δ {m['t1']:+,.0f}  -Top3Δ {m['t3']:+,.0f}")
        keys = {(x["date"], x["entry_time"]) for x in r}
        dropped = sorted([t for t in A if (t["date"], t["entry_time"]) not in keys],
                         key=lambda z: (z["date"], z["entry_time"]))
        print(f"    제외 거래 전수 ({len(dropped)}건):")
        for t in dropped:
            w = "IS" if t["date"] in set(W30) else "OOS"
            print(f"      {t['date']} {t['entry_time'][11:16]} {w:3s} slot{t['slot']} "
                  f"MFE {t['mfe']:5.2f} net {t['net_pct']:+6.2f}% KRW {t['pnl_krw']:+10,.0f} "
                  f"역행 {f(t,'ETF_역행봉수',0):.0f}봉 추종 {f(t,'ETF_추종%',0):+.2f}%")
else:
    print("\n  30일·OOS48 동시 양수 규칙: 0개")
