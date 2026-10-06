"""플래그 점수화 3단계 — 모형선택을 train 안에서만 하고 test 는 단 한 번 쓴다. READ-ONLY.

2단계에서 λ 를 test AUC 로 고른 것은 test 누수다. 여기서는
 (1) λ 를 train 내부 그룹 CV 로만 고르고
 (2) 특징집합은 기존 연구 가설(러너 = 역정렬 진입)로 사전 등록하며
 (3) test 는 마지막에 한 번만 본다.
"""
import os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
rng = np.random.default_rng(7)
ROOT = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(ROOT + "/score_features.csv")
df["day"] = df["day"].astype(str)
ALL = [c for c in df.columns if c not in ("day", "entry", "krw", "net", "rs", "y_run", "y_sl")]
# 사전 등록 특징집합: 2026-09-30 연구 '러너는 역정렬·저품질 진입에서 나온다' 가설 기반
PRE = ["e20_e50", "c_e50", "c_e20", "ret60", "ret30", "gap", "flag_rng", "conf_body", "pos_in_day"]
tr = df[df.day < "20260801"].reset_index(drop=True)
te = df[df.day >= "20260801"].reset_index(drop=True)


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    if y.sum() == 0 or y.sum() == len(y):
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    n1, n0 = y.sum(), len(y) - y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def fit_logit(X, y, lam=1.0, iters=4000, lr=0.1):
    X = np.c_[np.ones(len(X)), X]
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) / len(y)
        g[1:] += lam * w[1:] / len(y)
        w -= lr * g
    return w


def pred(w, X):
    return 1 / (1 + np.exp(-(np.c_[np.ones(len(X)), X] @ w)))


def cv_auc(d, feats, lab, lam, k=5):
    days = sorted(d.day.unique())
    folds = [days[i::k] for i in range(k)]
    out = []
    for f in folds:
        a, b = d[~d.day.isin(f)], d[d.day.isin(f)]
        if b[lab].sum() == 0 or b[lab].sum() == len(b) or len(a) < 10:
            continue
        mu, sd = a[feats].mean(), a[feats].std().replace(0, 1)
        w = fit_logit(((a[feats] - mu) / sd).to_numpy(), a[lab].to_numpy(float), lam=lam)
        out.append(auc(b[lab], pred(w, ((b[feats] - mu) / sd).to_numpy())))
    return np.nanmean(out) if out else np.nan


print(f"# train {len(tr)}건 러너 {int(tr.y_run.sum())} / test {len(te)}건 러너 {int(te.y_run.sum())}")
print("\n## 1. train 내부 그룹 CV 로 λ·특징집합 선택 (test 미사용)")
print("| 라벨 | 특징집합 | λ | train CV AUC |")
print("|---|---|---|---|")
best = {}
for lab in ("y_run", "y_sl"):
    for name, feats in (("사전등록 9개", PRE), ("전체 27개", ALL)):
        for lam in (1.0, 10.0, 50.0, 200.0):
            a = cv_auc(tr, feats, lab, lam)
            print(f"| {lab} | {name} | {lam} | {a:.3f} |")
            if lab not in best or a > best[lab][0]:
                best[lab] = (a, name, feats, lam)

print("\n## 2. 선택된 모형을 test 에 **한 번** 적용")
print("| 라벨 | 선택 | train CV AUC | **test AUC** | 순열 p |")
print("|---|---|---|---|---|")
RES = {}
for lab in ("y_run", "y_sl"):
    cvA, name, feats, lam = best[lab]
    mu, sd = tr[feats].mean(), tr[feats].std().replace(0, 1)
    w = fit_logit(((tr[feats] - mu) / sd).to_numpy(), tr[lab].to_numpy(float), lam=lam)
    s = pred(w, ((te[feats] - mu) / sd).to_numpy())
    a = auc(te[lab], s)
    y = te[lab].to_numpy(float)
    null = np.array([auc(rng.permutation(y), s) for _ in range(2000)])
    p = (np.abs(null - .5) >= abs(a - .5)).mean()
    print(f"| {lab} | {name} λ={lam} | {cvA:.3f} | **{a:.3f}** | {p:.4f} |")
    RES[lab] = (s, w, feats, mu, sd)

print("\n## 3. 단변량 안정성 — train 과 test 에서 같은 방향으로 러너를 가리키는 특징")
print("| 특징 | train AUC | test AUC | 방향 |")
print("|---|---|---|---|")
for c in ALL:
    a1, a2 = auc(tr.y_run, tr[c]), auc(te.y_run, te[c])
    if (a1 - .5) * (a2 - .5) > 0 and min(abs(a1 - .5), abs(a2 - .5)) >= 0.10:
        print(f"| {c} | {a1:.3f} | {a2:.3f} | {'높을수록 러너' if a1 > .5 else '**낮을수록 러너**'} |")

print("\n## 4. 경제성 — 러너 점수로 '비중을 키우면' test 구간이 어떻게 되나")
print("  (1차 근사: 해당 거래 손익 × 배수. 노출상한/슬롯 연쇄 미반영 → 채택 전 전체 replay 필요)")
s = RES["y_run"][0]
te2 = te.copy(); te2["rs_score"] = s
base = te2.krw.sum()
print(f"\n  test 기준손익 {base:+,.0f}원 ({len(te2)}건, 러너 {int(te2.y_run.sum())}건)")
print("| 규칙 | 대상 | 대상 손익 | 그중 러너 | 조정 후 | Δ |")
print("|---|---|---|---|---|---|")
for k, mult in ((0.2, 1.5), (0.3, 1.5), (0.2, 2.0), (0.3, 1.25)):
    th = np.quantile(s, 1 - k)
    up = te2[te2.rs_score >= th]
    adj = base + up.krw.sum() * (mult - 1)
    print(f"| 상위 {int(k*100)}% ×{mult} | {len(up)}건 | {up.krw.sum():+,.0f} | {int(up.y_run.sum())} | {adj:+,.0f} | {adj-base:+,.0f} |")
print("\n  (대조) 하위 점수 거래를 줄이면")
for k, mult in ((0.3, 0.5), (0.5, 0.5)):
    th = np.quantile(s, k)
    dn = te2[te2.rs_score <= th]
    adj = base + dn.krw.sum() * (mult - 1)
    print(f"  하위 {int(k*100)}% ×{mult}: 대상 {len(dn)}건 {dn.krw.sum():+,.0f}원 · 조정 후 {adj:+,.0f} · Δ {adj-base:+,.0f}")
