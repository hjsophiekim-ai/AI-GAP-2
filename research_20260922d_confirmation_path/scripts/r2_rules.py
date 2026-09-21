# -*- coding: utf-8 -*-
"""[R2] weak 세분화 규칙 — recovering 이면 x1.00, still-weak 이면 x0.50.

사이징 전용. 진입집합/거래수/슬롯/청산 전부 BASE 동일, 대체진입 없음,
감액분 재배분 없음(일예산 소진·노출누적은 BASE 배수로 고정).
"""
from __future__ import annotations
import sys
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import q1_confsize as Q
import r1_recover as R

A, D, W30, OOS = R.A, R.D, R.W30, R.OOS
BASE, BMAP, AMAP = Q.BASE, Q.BMAP, Q.AMAP
f, is_weak = R.f, Q.is_weak
rng = np.random.default_rng(20260922)
KEY0806 = ("20260806", [t for t in A if t["date"] == "20260806"][0]["entry_time"])

RECOVER = {
    "V1 마지막2분 ETF > 0":   lambda t: f(t, "ETF_마지막2분%", -9) > 0.0,
    "V2 마지막2분 방향일치":     lambda t: f(t, "마지막2분_방향일치", 0) == 1.0,
    "V3 회복폭(저가) >= 0.5%": lambda t: f(t, "회복폭%(저가기준)", 0) >= 0.5,
    "(대조) 회복 판정 없음":     lambda t: False,
    "(역) 마지막2분 ETF < -0.5%": lambda t: f(t, "ETF_마지막2분%", 0) >= -0.5,
}


def run(rec_fn, mult=0.50):
    """weak 이면서 recovering 이 아닌 거래만 감액."""
    return Q.apply(mult, weak_fn=lambda t: is_weak(t) and not rec_fn(t))


def ev(rows, W):
    SW = set(W)
    base = K.krw_pnl(BASE, W)
    cut = [r for r in rows if r.get("weak") and r["date"] in SW]
    return {"up": K.krw_pnl(rows, W) - base, "pf": K.pf(rows, W), "mdd": K.mdd_krw(rows, W),
            "n": sum(1 for r in rows if r["date"] in SW), "cut": len(cut),
            "h3": sum(1 for r in cut if r["mfe"] >= 3), "h5": sum(1 for r in cut if r["mfe"] >= 5),
            "h8": sum(1 for r in cut if r["mfe"] >= 8),
            "t1": K.excl_top_krw(rows, W, 1) - K.excl_top_krw(BASE, W, 1),
            "t3": K.excl_top_krw(rows, W, 3) - K.excl_top_krw(BASE, W, 3),
            "t5": K.excl_top_krw(rows, W, 5) - K.excl_top_krw(BASE, W, 5)}


print("=" * 134)
print("[R2] weak 세분화 — recovering x1.00 / still-weak x0.50")
print("=" * 134)
print(f"  BASE 30일 {K.krw_pnl(BASE,W30):,.0f} / OOS48 {K.krw_pnl(BASE,OOS):,.0f} / "
      f"78일 {K.krw_pnl(BASE,D):,.0f}  PF78 {K.pf(BASE,D):.3f}  MDD78 {K.mdd_krw(BASE,D):.2f}%")
print(f"  weak 43건 중 MFE>=3% 3건 / >=5% 2건 / >=8% 1건 (20260806 09:06)")
print()
print(f"  {'회복 판정':24s} {'감액':>4s} {'>=3손':>5s} {'>=5손':>5s} {'>=8손':>5s} "
      f"{'30d uplift':>12s} {'OOS48':>12s} {'78d':>12s} {'PF78':>6s} {'MDD78':>7s} "
      f"{'0806':>6s} {'동시양수':>6s}")
print("  " + "-" * 130)
RES = {}
for nm, fn in RECOVER.items():
    r = run(fn)
    RES[nm] = (r, fn)
    a, b, c = ev(r, W30), ev(r, OOS), ev(r, D)
    t0806 = [x for x in r if (x["date"], x["entry_time"]) == KEY0806][0]
    lab = "유지" if not t0806["weak"] else "감액"
    hit = "OK" if (a["up"] > 0 and b["up"] > 0) else ""
    print(f"  {nm:24s} {c['cut']:4d} {c['h3']:5d} {c['h5']:5d} {c['h8']:5d} "
          f"{a['up']:+12,.0f} {b['up']:+12,.0f} {c['up']:+12,.0f} {c['pf']:6.3f} "
          f"{c['mdd']:7.2f} {lab:>6s} {hit:>6s}")

print("\n  거래수 동일성 확인: " + " / ".join(
    f"{tag} {ev(RES['V1 마지막2분 ETF > 0'][0], W)['n']}=={sum(1 for t in BASE if t['date'] in set(W))}"
    for W, tag in ((W30, "30일"), (OOS, "OOS48"), (D, "78일"))))

print("\n" + "=" * 112)
print("weak MFE>=3% 3건이 각 규칙에서 어떻게 분류되나")
print("=" * 112)
print(f"  {'날짜':9s} {'진입':5s} {'창':4s} {'MFE':>5s} {'KRW':>10s} {'끝2분%':>7s} "
      f"{'방향일치':>5s} {'회복폭':>6s} " + " ".join(f"{n.split()[0]:>5s}" for n in RECOVER))
for t in sorted([t for t in R.WEAK if t["mfe"] >= 3], key=lambda z: z["date"]):
    b = BMAP[(t["date"], t["entry_time"])]
    w = "IS" if t["date"] in set(W30) else "OOS"
    cls = " ".join(f"{('유지' if fn(t) else '감액'):>5s}" for fn in RECOVER.values())
    print(f"  {t['date']:9s} {t['entry_time'][11:16]:5s} {w:4s} {t['mfe']:5.2f} "
          f"{b['pnl_krw']:10,.0f} {f(t,'ETF_마지막2분%',0):7.2f} "
          f"{f(t,'마지막2분_방향일치',0):5.0f} {f(t,'회복폭%(저가기준)',0):6.2f} {cls}")

print("\n" + "=" * 112)
print("bootstrap 10,000회 / Top1·3·5 제거")
print("=" * 112)
for nm in ("V1 마지막2분 ETF > 0", "V2 마지막2분 방향일치", "V3 회복폭(저가) >= 0.5%",
           "(대조) 회복 판정 없음"):
    r, _ = RES[nm]
    print(f"\n  ── {nm} ──")
    for tag, W in (("30일", W30), ("OOS48", OOS), ("78일", D)):
        m = ev(r, W)
        da, db = K.daily_krw(BASE, W), K.daily_krw(r, W)
        v = np.array([db[d] - da[d] for d in W])
        bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
        print(f"    {tag:6s} uplift {m['up']:+11,.0f}  PF {m['pf']:6.3f}  MDD {m['mdd']:6.2f}  "
              f"boot P(>0)={(bt>0).mean()*100:6.2f}%  "
              f"-Top1Δ {m['t1']:+10,.0f}  -Top3Δ {m['t3']:+10,.0f}  -Top5Δ {m['t5']:+10,.0f}")
