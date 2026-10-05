"""플래그 점수화 4단계 — train 에서만 고른 단순 3변수 점수. test 는 마지막 한 번. READ-ONLY."""
import os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
rng = np.random.default_rng(11)
ROOT = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(ROOT + "/score_features.csv")
df["day"] = df["day"].astype(str)
ALL = [c for c in df.columns if c not in ("day", "entry", "krw", "net", "rs", "y_run", "y_sl")]
tr = df[df.day < "20260801"].reset_index(drop=True)
te = df[df.day >= "20260801"].reset_index(drop=True)


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    if y.sum() == 0 or y.sum() == len(y):
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    n1, n0 = y.sum(), len(y) - y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


# ── train 에서만 특징 3개 선택 ────────────────────────────────────────
rank = sorted(((abs(auc(tr.y_run, tr[c]) - .5), c, auc(tr.y_run, tr[c])) for c in ALL), reverse=True)
print("## train 기준 러너 판별력 상위 8개 (선택은 여기서만)")
for v, c, a in rank[:8]:
    print(f"  {c:10s} train AUC {a:.3f}  (|Δ| {v:.3f})")
PICK = [(c, 1.0 if a > .5 else -1.0) for v, c, a in rank[:3]]
print(f"\n  선택: {[(c, '+' if s>0 else '-') for c, s in PICK]}")

mu, sd = tr[[c for c, _ in PICK]].mean(), tr[[c for c, _ in PICK]].std().replace(0, 1)


def score(d):
    z = (d[[c for c, _ in PICK]] - mu) / sd
    return sum(s * z[c] for c, s in PICK).to_numpy()


s_tr, s_te = score(tr), score(te)
a_tr, a_te = auc(tr.y_run, s_tr), auc(te.y_run, s_te)
y = te.y_run.to_numpy(float)
null = np.array([auc(rng.permutation(y), s_te) for _ in range(5000)])
p = (np.abs(null - .5) >= abs(a_te - .5)).mean()
print(f"\n## 단순 점수(z합)의 러너 판별력")
print(f"  train AUC {a_tr:.3f} → **test AUC {a_te:.3f}** · 순열 p = **{p:.4f}**")
a_sl = auc(te.y_sl, s_te)
print(f"  (같은 점수로 손절 판별: test AUC {a_sl:.3f} — 낮을수록 '손절을 못 피한다'는 뜻)")

print(f"\n## 점수 5분위별 결과 (test {len(te)}건, 기준손익 {te.krw.sum():+,.0f}원)")
te2 = te.copy(); te2["sc"] = s_te
te2["q"] = pd.qcut(te2.sc, 5, labels=[1, 2, 3, 4, 5])
g = te2.groupby("q", observed=True).agg(건수=("krw", "size"), 손익=("krw", "sum"), 거래당=("krw", "mean"),
                                        러너=("y_run", "sum"), 손절=("y_sl", "sum"))
print(g.round(0).to_string())
print("\n## 같은 분위를 train 에서 보면 (안정성 확인)")
tr2 = tr.copy(); tr2["sc"] = s_tr
tr2["q"] = pd.qcut(tr2.sc, 5, labels=[1, 2, 3, 4, 5])
print(tr2.groupby("q", observed=True).agg(건수=("krw", "size"), 손익=("krw", "sum"), 거래당=("krw", "mean"),
                                          러너=("y_run", "sum"), 손절=("y_sl", "sum")).round(0).to_string())

print("\n## 경제성 (1차 근사 — 채택 전 전체 replay 필요)")
base = te.krw.sum()
print("| 규칙 | 대상 | 대상손익 | 러너 | Δ(test) |")
print("|---|---|---|---|---|")
for q, mult in ((5, 1.5), (5, 2.0), (4, 1.25)):
    z = te2[te2.q == q]
    print(f"| {q}분위 ×{mult} | {len(z)}건 | {z.krw.sum():+,.0f} | {int(z.y_run.sum())} | {z.krw.sum()*(mult-1):+,.0f} |")
for q, mult in ((1, 0.5), (1, 0.0)):
    z = te2[te2.q == q]
    print(f"| {q}분위 ×{mult} | {len(z)}건 | {z.krw.sum():+,.0f} | {int(z.y_run.sum())} | {z.krw.sum()*(mult-1):+,.0f} |")
te2[["day", "entry", "sc", "q", "krw", "y_run", "y_sl", "rs"]].to_csv(ROOT + "/score_test_quintiles.csv", index=False, encoding="utf-8")
