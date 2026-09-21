# -*- coding: utf-8 -*-
"""[G section5] 생존 규칙 R2 정밀 평가 + 강건성. READ-ONLY."""
from __future__ import annotations
import sys, pickle
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g3_rules as G
from g1_extract import ROWS, cls_def1, cls_def2

A, TS, W30, D = G.A, G.TS, G.W30, G.D
S30 = set(W30)
rng = np.random.default_rng(20260921)
AMAP = G.AMAP

print("=" * 128)
print("[G section5] R2 '도달봉 되밀림' 정밀 평가")
print("=" * 128)

m = G.metrics(G.R2)
SM = {(x["date"], x["entry_time"]): x for x in m["sz"]}
print(f"\n발동 거래 전량 ({m['n']}건, 30일)")
print(f"  {'날짜':9s} {'방향':9s} {'sl':>2s} {'진입':5s} {'도달':5s} {'종가위치':>6s} "
      f"{'기존사유':26s} {'기존%':>7s} {'R2%':>7s} {'차이':>7s} {'ΔKRW':>10s} {'분류':10s}")
print("  " + "-" * 122)
RMAP = {(r["date"], r["entry_time"]): r for r in ROWS}
tot = 0
for x in sorted(m["fired"], key=lambda z: (z["date"], z["entry_time"])):
    k = (x["date"], x["entry_time"]); r = RMAP[k]
    dk = SM[k]["pnl_krw"] - AMAP[k]["pnl_krw"]; tot += dk
    cl = ("SEVERE" if r["net_pct"] <= 0 else "GIVEBACK" if cls_def1(r) else
          "GIVE2" if cls_def2(r) else "KEEPER")
    print(f"  {x['date']:9s} {x['direction']:9s} {x['slot']:2d} {x['entry_time'][11:16]:5s} "
          f"{r['touch_time'][11:16]:5s} {G.F.FE[k]['종가위치']:6.3f} "
          f"{x['base_exit_reason']:26s} {x['base_net_pct']:7.2f} {x['net_pct']:7.2f} "
          f"{x['net_pct']-x['base_net_pct']:+7.2f} {dk:+10,.0f} {cl:10s}")
print(f"  {'합계':>100s} {tot:+10,.0f}")

# ── 포착률 ─────────────────────────────────────────────────────────────
sev = [r for r in ROWS if r["net_pct"] <= 0]
g1 = [r for r in ROWS if cls_def1(r)]
g2 = [r for r in ROWS if cls_def2(r)]
kp1 = [r for r in ROWS if not cls_def1(r)]
FK = {(x["date"], x["entry_time"]) for x in m["fired"]}
def rate(tag, grp):
    hit = sum(1 for r in grp if (r["date"], r["entry_time"]) in FK)
    print(f"  {tag:26s} {hit:2d}/{len(grp):2d} = {hit/max(1,len(grp))*100:5.1f}%")
print("\n포착률 (R2 발동이 각 그룹을 얼마나 잡았나)")
rate("C. SEVERE (최종<=0)", sev)
rate("B. GIVEBACK 정의1", g1)
rate("B. GIVEBACK 정의2", g2)
rate("A. KEEPER 정의1 (오탐)", kp1)

# ── 강건성 ─────────────────────────────────────────────────────────────
print("\n" + "=" * 100)
print("강건성")
print("=" * 100)
base30 = K.krw_pnl(A, W30)
for tag, kw in (("기준 (완성봉 종가 체결)", {}),
                ("+0.1% 슬리피지", {"slip": 0.1}),
                ("+1분 지연", {"delay": 1}),
                ("+0.1% 슬리피지 & +1분 지연", {"slip": 0.1, "delay": 1})):
    x = G.metrics(G.R2, **kw)
    print(f"  {tag:26s} 발동 {x['n']:2d}  uplift {x['up']:+11,.0f}  PF {x['pf']:6.3f}  "
          f"MDD {x['mdd']:6.2f}  개선/악화 {x['gain']}/{x['loss']}")

print("\n  창 확대 (78영업일 전체)")
x78 = G.metrics(G.R2, W=D)
print(f"  {'78일':26s} 발동 {x78['n']:2d}  {x78['krw']:,.0f} KRW  uplift {x78['up']:+11,.0f}  "
      f"PF {x78['pf']:6.3f}  MDD {x78['mdd']:6.2f}  개선/악화 {x78['gain']}/{x78['loss']}  "
      f"runner손상 {x78['runner']}")
print(f"  {'앞 48일 (OOS)':26s} uplift {K.krw_pnl(m['sz'], D[:48]) - K.krw_pnl(A, D[:48]):+11,.0f}  "
      f"PF {K.pf(m['sz'], D[:48]):.3f} (BASE {K.pf(A, D[:48]):.3f})")

# ── 부트스트랩 ─────────────────────────────────────────────────────────
da, db = K.daily_krw(A, W30), K.daily_krw(m["sz"], W30)
v = np.array([db[d] - da[d] for d in W30])
bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
print(f"\n  부트스트랩 10,000회(일단위,30일)  P(>0)={(bt>0).mean()*100:.2f}%  "
      f"p5={np.percentile(bt,5):+,.0f}  중앙={np.percentile(bt,50):+,.0f}  "
      f"p95={np.percentile(bt,95):+,.0f}  영향일수={int((v!=0).sum())}")
da78, db78 = K.daily_krw(A, D), K.daily_krw(x78["sz"], D)
v78 = np.array([db78[d] - da78[d] for d in D])
bt78 = v78[rng.integers(0, len(v78), size=(10000, len(v78)))].sum(axis=1)
print(f"  부트스트랩 10,000회(일단위,78일)  P(>0)={(bt78>0).mean()*100:.2f}%  "
      f"p5={np.percentile(bt78,5):+,.0f}  중앙={np.percentile(bt78,50):+,.0f}")

# ── LOO / L2O ──────────────────────────────────────────────────────────
print("\n  LOO (하루씩 제거, 30일)")
loo = [(sum(v) - v[i], W30[i]) for i in range(len(W30))]
neg = [x for x in loo if x[0] <= 0]
print(f"    uplift 범위 {min(x[0] for x in loo):+,.0f} ~ {max(x[0] for x in loo):+,.0f}  "
      f"부호 유지 {len(loo)-len(neg)}/{len(loo)}일")
imp = sorted(zip(W30, v), key=lambda z: -abs(z[1]))[:5]
print("    영향 큰 날: " + ", ".join(f"{d}({x:+,.0f})" for d, x in imp))
print("\n  발동거래 단위 LOO (8건 중 1건씩 제외)")
for x in sorted(m["fired"], key=lambda z: (z["date"], z["entry_time"])):
    k = (x["date"], x["entry_time"])
    dk = SM[k]["pnl_krw"] - AMAP[k]["pnl_krw"]
    print(f"    -{k[0]} {k[1][11:16]}  잔여 uplift {m['up']-dk:+11,.0f}"
          f"{'   <= 부호반전' if (m['up']-dk) <= 0 else ''}")

# ── 플라세보 ───────────────────────────────────────────────────────────
print("\n" + "=" * 100)
print("플라세보 — 43건 중 무작위 8건을 '도달 다음봉 종가'에 청산 (10,000회)")
print("=" * 100)
CAND = [r for r in ROWS]
PRE = {}
for r in CAND:
    p = G.prof(r)
    j = min(p["i"] + 1, p["n"] - 1)
    PRE[(r["date"], r["entry_time"])] = (float(p["nc"][j]), j < p["n"] - 1)
idx = np.arange(len(CAND))
ups = np.empty(10000)
for s in range(10000):
    pick = {(CAND[i]["date"], CAND[i]["entry_time"]) for i in rng.choice(idx, 8, replace=False)}
    sim = []
    for t in TS:
        r = dict(t); k = (t["date"], t["entry_time"])
        if k in pick and PRE[k][1]:
            r["net_pct"] = PRE[k][0]; r["exit_reason"] = G.EXIT_TAG
        sim.append(r)
    ups[s] = K.krw_pnl(K.size_chain(sim, None), W30) - base30
print(f"  실제 R2 uplift {m['up']:+,.0f} / 플라세보 평균 {ups.mean():+,.0f} "
      f"p95 {np.percentile(ups,95):+,.0f}")
print(f"  백분위 = {(ups < m['up']).mean()*100:.2f}%  (95% 이상이어야 우연 아님)")
