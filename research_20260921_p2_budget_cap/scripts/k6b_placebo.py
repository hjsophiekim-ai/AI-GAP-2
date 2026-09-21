# -*- coding: utf-8 -*-
"""§13 Placebo 정밀판 — 5,000회 + 순수배분(front=1.00) 분해. READ-ONLY."""
import sys, pickle
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
TS = K.load(); M, AF = K.MORNING, K.AFTERNOON
rng = np.random.default_rng(777)
A = K.size_chain(TS, None)
keys = [(t["date"], t["entry_time"]) for t in A]
true_t = [k for k, t in zip(keys, A) if t["slot"] == 3 and t["session"] == M]
KN = len(true_t)


def uplift(targets, front):
    tg = set(targets)
    def extra_of(t):
        if (t["date"], t["entry_time"]) in tg:
            return 0.25
        if t["slot"] == 3 and t["session"] == AF:
            return 1.00
        return front
    return K.krw_pnl(K.size_chain(TS, extra_of=extra_of), D) - K.krw_pnl(A, D)


N = 5000
for front, tag in ((1.05, "P2  (front 1.05 = 배분 + 레버리지)"),
                   (1.00, "P0  (front 1.00 = 순수 배분효과만)")):
    real = uplift(true_t, front)
    pl = np.empty(N)
    for i in range(N):
        pick = rng.choice(len(keys), size=KN, replace=False)
        pl[i] = uplift([keys[j] for j in pick], front)
    pct = (pl < real).mean() * 100
    se = (pct * (100 - pct) / N) ** 0.5
    print("=" * 88)
    print(f"§13  {tag}   {N:,}회")
    print("=" * 88)
    print(f"  실제 uplift = {real:+,.0f} KRW")
    print(f"  placebo: 최소 {pl.min():+,.0f} / p25 {np.percentile(pl,25):+,.0f} / "
          f"p50 {np.percentile(pl,50):+,.0f} / p95 {np.percentile(pl,95):+,.0f} / "
          f"최대 {pl.max():+,.0f}")
    print(f"  placebo 평균 {pl.mean():+,.0f} / 표준편차 {pl.std():,.0f}")
    print(f"  실제값 percentile = {pct:.2f}%  (±{1.96*se:.2f} 95%CI)")
    print(f"  단측 p-value = {(pl >= real).mean():.4f}")
    print(f"  판정: {'통과 (>=95)' if pct >= 95 else '미달 (<95) — 과최적화 경고'}\n")
