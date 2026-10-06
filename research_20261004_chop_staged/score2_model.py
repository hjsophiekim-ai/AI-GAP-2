"""플래그 점수화 연구 2단계 — 예측력이 실재하는가. 엄격한 시간분할 OOS. READ-ONLY.

train = 2026-05-27 ~ 07-31, test = 08-01 ~ 10-01 (시간순, 섞지 않음).
sklearn 미설치 환경이라 AUC/로지스틱 회귀를 직접 구현한다.
"""
import os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
rng = np.random.default_rng(0)
ROOT = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(ROOT + "/score_features.csv")
df["day"] = df["day"].astype(str)
FEATS = [c for c in df.columns if c not in ("day", "entry", "krw", "net", "rs", "y_run", "y_sl")]
tr = df[df.day < "20260801"].reset_index(drop=True)
te = df[df.day >= "20260801"].reset_index(drop=True)
print(f"# train {len(tr)}건 ({tr.day.min()}~{tr.day.max()}) 러너 {int(tr.y_run.sum())} 손절 {int(tr.y_sl.sum())} · 손익 {tr.krw.sum():+,.0f}")
print(f"# test  {len(te)}건 ({te.day.min()}~{te.day.max()}) 러너 {int(te.y_run.sum())} 손절 {int(te.y_sl.sum())} · 손익 {te.krw.sum():+,.0f}")
print(f"# 특징 {len(FEATS)}개")


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    if y.sum() == 0 or y.sum() == len(y):
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    n1, n0 = y.sum(), len(y) - y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def auc_ci(y, s, n=2000):
    y = np.asarray(y); s = np.asarray(s)
    out = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        if 0 < np.asarray(y)[i].sum() < len(i):
            out.append(auc(y[i], s[i]))
    return (np.percentile(out, 2.5), np.percentile(out, 97.5)) if out else (np.nan, np.nan)


print("\n## 1. 단변량 판별력 (train 에서 학습 → test 에서 확인)")
print("| 특징 | train AUC(손절) | test AUC(손절) | train AUC(러너) | test AUC(러너) |")
print("|---|---|---|---|---|")
rows = []
for c in FEATS:
    a_tr, a_te = auc(tr.y_sl, tr[c]), auc(te.y_sl, te[c])
    r_tr, r_te = auc(tr.y_run, tr[c]), auc(te.y_run, te[c])
    rows.append((c, a_tr, a_te, r_tr, r_te))
rows.sort(key=lambda r: -abs((r[1] or .5) - .5))
for c, a1, a2, r1, r2 in rows[:12]:
    print(f"| {c} | {a1:.3f} | {a2:.3f} | {r1:.3f} | {r2:.3f} |")
sg = [r for r in rows if abs(r[1] - .5) >= .10]
print(f"\n  train 에서 |AUC-0.5| >= 0.10 인 특징 {len(sg)}개 → 그중 test 에서 **같은 방향**으로 유지된 것: "
      f"{sum(1 for r in sg if (r[1]-.5)*(r[2]-.5) > 0)}개")

# ── 로지스틱 회귀 (L2, 표준화) ────────────────────────────────────────
def fit_logit(X, y, lam=1.0, iters=4000, lr=0.1):
    X = np.c_[np.ones(len(X)), X]
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) / len(y)
        g[1:] += lam * w[1:] / len(y)
        w -= lr * g
    return w


def predict(w, X):
    return 1 / (1 + np.exp(-(np.c_[np.ones(len(X)), X] @ w)))


mu, sd = tr[FEATS].mean(), tr[FEATS].std().replace(0, 1)
Xtr = ((tr[FEATS] - mu) / sd).to_numpy()
Xte = ((te[FEATS] - mu) / sd).to_numpy()
print("\n## 2. 다변량 점수모형 (L2 로지스틱, 표준화, train 전용 학습)")
print("| 라벨 | 정규화 | train AUC | **test AUC** | test 95% CI |")
print("|---|---|---|---|---|")
best = {}
for lab in ("y_sl", "y_run"):
    for lam in (1.0, 10.0, 50.0):
        w = fit_logit(Xtr, tr[lab].to_numpy(float), lam=lam)
        a_tr, a_te = auc(tr[lab], predict(w, Xtr)), auc(te[lab], predict(w, Xte))
        lo, hi = auc_ci(te[lab].to_numpy(), predict(w, Xte))
        print(f"| {lab} | λ={lam} | {a_tr:.3f} | **{a_te:.3f}** | [{lo:.3f}, {hi:.3f}] |")
        if lab not in best or a_te > best[lab][0]:
            best[lab] = (a_te, w, lam)

print("\n## 3. 순열검정 — test AUC 가 우연보다 나은가 (라벨 섞기 1000회)")
for lab in ("y_sl", "y_run"):
    a_te, w, lam = best[lab]
    yte = te[lab].to_numpy(float)
    s = predict(w, Xte)
    null = np.array([auc(rng.permutation(yte), s) for _ in range(1000)])
    p = (np.abs(null - .5) >= abs(a_te - .5)).mean()
    print(f"  {lab}: test AUC {a_te:.3f} · 귀무분포 평균 {null.mean():.3f} · **p = {p:.3f}**")

print("\n## 4. 경제성 검정 — 점수 하위 K% 진입을 막으면 test 구간 손익이 어떻게 되나")
print("  (1차 근사: 그 거래만 제거. 슬롯 연쇄 미반영 → 실제 채택 전 전체 replay 필요)")
a_te, w, lam = best["y_sl"]
s = predict(w, Xte)                      # 손절 확률 (높을수록 나쁨)
te2 = te.copy(); te2["score"] = s
base = te2.krw.sum()
print(f"\n| 차단 비율 | 차단 거래 | 차단 손익 | 그중 러너 | 잔여 손익 | Δ |")
print("|---|---|---|---|---|---|")
for k in (0.1, 0.2, 0.3, 0.4):
    th = np.quantile(s, 1 - k)
    cut = te2[te2.score >= th]
    keep = te2[te2.score < th]
    print(f"| 상위 {int(k*100)}% (손절확률 높은 쪽) | {len(cut)} | {cut.krw.sum():+,.0f} | "
          f"{int(cut.y_run.sum())} | {keep.krw.sum():+,.0f} | {keep.krw.sum()-base:+,.0f} |")

print("\n## 5. 참고 — train 구간에서 같은 규칙을 적용하면 (인샘플, 과적합 확인용)")
s_tr = predict(w, Xtr)
tr2 = tr.copy(); tr2["score"] = s_tr
for k in (0.2, 0.3):
    th = np.quantile(s_tr, 1 - k)
    keep = tr2[tr2.score < th]
    print(f"  상위 {int(k*100)}% 차단 → train Δ {keep.krw.sum()-tr.krw.sum():+,.0f}원 "
          f"(test Δ 와 비교: 과적합 크기)")
