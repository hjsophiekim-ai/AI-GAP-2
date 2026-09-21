# -*- coding: utf-8 -*-
"""[P4] R1/R2 강건성 — 단일거래 의존성을 양쪽 창 모두에서 대칭 검사."""
import sys; sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import numpy as np, k1_core as K
import p2_rules as R
A, D, W30, OOS, BASE = R.A, R.D, R.W30, R.OOS, R.BASE
f = R.f
rng = np.random.default_rng(20260922)
CAND = {"R1 ETF역행>=3봉": lambda t: f(t, "ETF_역행봉수", 0) >= 3,
        "R2 ETF추종<=0%":  lambda t: f(t, "ETF_추종%", 1) <= 0.0,
        "R1&R2":          lambda t: f(t, "ETF_역행봉수", 0) >= 3 and f(t, "ETF_추종%", 1) <= 0.0}
for nm, fn in CAND.items():
    r = R.build(fn)
    print("=" * 112); print(nm); print("=" * 112)
    for tag, W in (("30일 IS", W30), ("OOS48", OOS), ("78일", D)):
        m = R.ev(r, W, fn)
        da, db = K.daily_krw(BASE, W), K.daily_krw(r, W)
        v = np.array([db[d] - da[d] for d in W])
        bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
        loo = [v.sum() - v[i] for i in range(len(v))]
        sgn_keep = sum(1 for z in loo if (z > 0) == (v.sum() > 0))
        big = sorted(zip(W, v), key=lambda z: -abs(z[1]))[:3]
        print(f"  {tag:8s} uplift {m['up']:+11,.0f}  PF {m['pf']:6.3f}  MDD {m['mdd']:6.2f}  "
              f"boot P(>0)={(bt>0).mean()*100:6.2f}%  LOO 부호유지 {sgn_keep}/{len(loo)}일")
        print(f"           영향 큰 날: " + ", ".join(f"{d}({x:+,.0f})" for d, x in big)
              + f"   -Top1Δ {m['t1']:+,.0f} / -Top3Δ {m['t3']:+,.0f}")
    # 거래단위 LOO: 제외된 거래 1건씩 되살려도 부호가 유지되나
    keys = {(x["date"], x["entry_time"]) for x in r}
    dropped = [t for t in A if (t["date"], t["entry_time"]) not in keys]
    for tag, W in (("30일 IS", W30), ("OOS48", OOS)):
        SW = set(W)
        sub = [t for t in dropped if t["date"] in SW]
        base_up = R.ev(r, W, fn)["up"]
        worst = []
        for t in sub:
            fn2 = lambda z, tt=t: fn(z) and not (z["date"] == tt["date"]
                                                 and z["entry_time"] == tt["entry_time"])
            worst.append((R.ev(R.build(fn2), W, fn2)["up"], t))
        worst.sort()
        flip = sum(1 for u, _ in worst if (u > 0) != (base_up > 0))
        print(f"  {tag} 거래단위 LOO: 제외 {len(sub)}건 중 1건 되살려 부호 반전 {flip}건 / "
              f"uplift 범위 {worst[0][0]:+,.0f} ~ {worst[-1][0]:+,.0f}")
        if flip:
            for u, t in worst:
                if (u > 0) != (base_up > 0):
                    print(f"      -> {t['date']} {t['entry_time'][11:16]} MFE {t['mfe']:.2f} "
                          f"KRW {t['pnl_krw']:+,.0f} 되살리면 uplift {u:+,.0f}")
    print()
