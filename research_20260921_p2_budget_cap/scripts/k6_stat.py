# -*- coding: utf-8 -*-
"""§12 부트스트랩, §13 Placebo. 전부 KRW + 3,000만원 cap. READ-ONLY."""
import sys, pickle
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
TS = K.load()
M = K.MORNING
rng = np.random.default_rng(20260921)

A = K.size_chain(TS, None)
P2 = K.size_chain(TS, K.make_extra(1.05, 0.25, 1.00))
REAL = K.krw_pnl(P2, D) - K.krw_pnl(A, D)
print(f"실제 P2 uplift (78d) = {REAL:+,.0f} KRW\n")

# ── §12-a 일단위 부트스트랩 ──────────────────────────────────────────────
dA, dB = K.daily_krw(A, D), K.daily_krw(P2, D)
diff = np.array([dB[d] - dA[d] for d in D])
n = len(diff)
N = 10_000
idx = rng.integers(0, n, size=(N, n))
boot = diff[idx].sum(axis=1)
print("=" * 88)
print(f"§12-a  일단위 부트스트랩 {N:,}회  (78일 재표집)")
print("=" * 88)
print(f"  P(P2 > A)        = {(boot > 0).mean()*100:.2f}%")
print(f"  P(uplift <= 0)   = {(boot <= 0).mean()*100:.2f}%")
print(f"  uplift  p5       = {np.percentile(boot, 5):+,.0f} KRW")
print(f"          중앙     = {np.percentile(boot, 50):+,.0f} KRW")
print(f"          p95      = {np.percentile(boot, 95):+,.0f} KRW")
print(f"  채택기준 P>=95% & p5>=0 : "
      f"{'통과' if (boot > 0).mean() >= 0.95 and np.percentile(boot, 5) >= 0 else '실패'}")
print(f"  (일별 차이 양수 {int((diff>0).sum())}일 / 음수 {int((diff<0).sum())}일 / "
      f"0 {int((diff==0).sum())}일)")

# ── §12-b 거래단위 부트스트랩 ────────────────────────────────────────────
tdiff = np.array([b["pnl_krw"] - a["pnl_krw"] for a, b in zip(A, P2)])
m = len(tdiff)
bt = tdiff[rng.integers(0, m, size=(N, m))].sum(axis=1)
print()
print("=" * 88)
print(f"§12-b  거래단위 부트스트랩 {N:,}회  ({m}거래 재표집)")
print("=" * 88)
print(f"  P(P2 > A)        = {(bt > 0).mean()*100:.2f}%")
print(f"  P(uplift <= 0)   = {(bt <= 0).mean()*100:.2f}%")
print(f"  uplift  p5       = {np.percentile(bt, 5):+,.0f} KRW")
print(f"          중앙     = {np.percentile(bt, 50):+,.0f} KRW")
print(f"          p95      = {np.percentile(bt, 95):+,.0f} KRW")
print(f"  (거래별 차이 양수 {int((tdiff>0).sum())} / 음수 {int((tdiff<0).sum())} / "
      f"0 {int((tdiff==0).sum())})")

# ── §13 Placebo ─────────────────────────────────────────────────────────
print()
print("=" * 88)
print("§13  Placebo — 감액대상 8건을 임의 8거래로 치환 (front 1.05 는 그대로), 1,000회")
print("=" * 88)
keys = [(t["date"], t["entry_time"]) for t in A]
true_targets = {(t["date"], t["entry_time"]) for t in A
                if t["slot"] == 3 and t["session"] == M}
k_targets = len(true_targets)
print(f"  실제 감액대상 {k_targets}건")


def run_with(targets):
    tg = set(targets)

    def extra_of(t):
        if (t["date"], t["entry_time"]) in tg:
            return 0.25
        if t["slot"] == 3 and t["session"] == K.AFTERNOON:
            return 1.00
        return 1.05
    r = K.size_chain(TS, extra_of=extra_of)
    return K.krw_pnl(r, D) - K.krw_pnl(A, D)


NP = 1000
pl = np.empty(NP)
for i in range(NP):
    pick = rng.choice(len(keys), size=k_targets, replace=False)
    pl[i] = run_with([keys[j] for j in pick])
pct = (pl < REAL).mean() * 100
print(f"  placebo 분포: 최소 {pl.min():+,.0f} / p50 {np.percentile(pl,50):+,.0f} / "
      f"p95 {np.percentile(pl,95):+,.0f} / 최대 {pl.max():+,.0f}")
print(f"  실제 P2 uplift {REAL:+,.0f} KRW 의 placebo percentile = {pct:.1f}%")
print(f"  판정: {'통과 (>=95%)' if pct >= 95 else '*** 95 percentile 미만 — 과최적화 경고 ***'}")

# 참고: 감액대상을 '오전 slot3' 이 아닌 '임의 slot3' 로만 제한한 placebo
s3 = [k for k, t in zip(keys, A) if t["slot"] == 3]
pl2 = np.empty(NP)
for i in range(NP):
    pick = rng.choice(len(s3), size=k_targets, replace=False)
    pl2[i] = run_with([s3[j] for j in pick])
print(f"\n  (참고) slot3 안에서만 임의 8건: p50 {np.percentile(pl2,50):+,.0f} / "
      f"p95 {np.percentile(pl2,95):+,.0f} / 실제 percentile = {(pl2 < REAL).mean()*100:.1f}%")
