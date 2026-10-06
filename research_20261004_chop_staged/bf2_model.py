"""BOX-FAIL SCORE 2단계 — 로지스틱 회귀 1개로 결합, train 고정 후 test 1회. READ-ONLY.

가중치·표준화·방향은 전부 train(05-27~07-31)에서만 결정하고 test(08-01~10-01)에는 그대로 적용.
임계 탐색 없음. 10/02 는 학습·선택에 일절 쓰지 않고 마지막 참고로만 본다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
rng = np.random.default_rng(17)
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
F = pd.read_csv(ROOT + "/bf_features.csv")
F["day"] = F["day"].astype(str)
F["entry"] = pd.to_datetime(F["entry"])
FEATS = ["box_pos", "brk_fail", "ext_atr", "rev", "persist", "reentry"]
tr = F[F.day < "20260801"].reset_index(drop=True)
te = F[(F.day >= "20260801") & (F.day <= "20261001")].reset_index(drop=True)
d02 = F[F.day == "20261002"].reset_index(drop=True)
print(f"# train {len(tr)}건 (손절 {int(tr.y_sl.sum())} / runner {int(tr.y_run.sum())}) "
      f"· test {len(te)}건 (손절 {int(te.y_sl.sum())} / runner {int(te.y_run.sum())})")


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    if y.sum() in (0, len(y)):
        return np.nan
    r = pd.Series(s).rank().to_numpy(); n1 = y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


def spear(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 5 or np.std(a) == 0:
        return np.nan
    return float(np.corrcoef(pd.Series(a).rank(), pd.Series(b).rank())[0, 1])


def fit(X, y, lam=5.0, iters=6000, lr=0.1):
    X = np.c_[np.ones(len(X)), X]
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) / len(y)
        g[1:] += lam * w[1:] / len(y)
        w -= lr * g
    return w


mu, sd = tr[FEATS].mean(), tr[FEATS].std().replace(0, 1)
Z = lambda d: ((d[FEATS] - mu) / sd).to_numpy()
W = fit(Z(tr), tr.y_sl.to_numpy(float))
score = lambda d: 100.0 / (1 + np.exp(-(np.c_[np.ones(len(d)), Z(d)] @ W)))
for d in (tr, te, d02):
    d["score"] = score(d)
print("\n## 모형 (train 전용 학습, λ=5 고정)")
print("| feature | 계수 | 해석 |")
print("|---|---|---|")
for c, w in zip(FEATS, W[1:]):
    print(f"| {c} | {w:+.3f} | {'높을수록 손절↑' if w > 0 else '낮을수록 손절↑'} |")
print(f"  절편 {W[0]:+.3f}")

print("\n## 1~3. 판별력")
print("| 구간 | 손절 AUC | runner AUC | 순위상관(score, 거래손익) |")
print("|---|---|---|---|")
for lab, d in (("train", tr), ("test", te)):
    print(f"| {lab} | {auc(d.y_sl, d.score):.3f} | {auc(d.y_run, d.score):.3f} | {spear(d.score, d.krw):+.3f} |")
a = auc(te.y_sl, te.score)
null = np.array([auc(rng.permutation(te.y_sl.to_numpy(float)), te.score.to_numpy()) for _ in range(3000)])
print(f"  test 손절 AUC 순열검정 p = {(np.abs(null - .5) >= abs(a - .5)).mean():.4f}")

print("\n## 4. score 구간별 (train 분위로 고정)")
BINS = [0, 40, 60, 75, 101]
LAB = ["0-39", "40-59", "60-74", "75-100"]
for lab, d in (("train", tr), ("test", te)):
    d["bucket"] = pd.cut(d.score, BINS, right=False, labels=LAB)
    print(f"\n### {lab} ({len(d)}건, {d.krw.sum():+,.0f}원)")
    print("| score | 거래수 | 평균손익 | 승률% | 손절률% | runner율% |")
    print("|---|---|---|---|---|---|")
    for b in LAB:
        y = d[d.bucket == b]
        if not len(y):
            print(f"| {b} | 0 | — | — | — | — |"); continue
        print(f"| {b} | {len(y)} | {y.krw.mean():+,.0f} | {(y.krw>0).mean()*100:.1f} | "
              f"{y.sl_pct if False else y.y_sl.mean()*100:.1f} | {y.y_run.mean()*100:.1f} |")

print("\n## 4b. 상위 20% (train 분위 기준)")
th = np.quantile(tr.score, 0.80)
print(f"  임계 = train 80퍼센타일 {th:.1f}")
print("| 구간 | 상위20% 건수 | 평균손익 | 손절률% | runner율% | 나머지 평균손익 |")
print("|---|---|---|---|---|---|")
for lab, d in (("train", tr), ("test", te)):
    hi, lo = d[d.score >= th], d[d.score < th]
    print(f"| {lab} | {len(hi)} | {hi.krw.mean() if len(hi) else 0:+,.0f} | "
          f"{hi.y_sl.mean()*100 if len(hi) else 0:.1f} | {hi.y_run.mean()*100 if len(hi) else 0:.1f} | "
          f"{lo.krw.mean():+,.0f} |")

print("\n## 5. train/test 방향 일치")
for c in FEATS:
    a1, a2 = auc(tr.y_sl, tr[c]), auc(te.y_sl, te[c])
    ok = "일치" if (a1 - .5) * (a2 - .5) > 0 else "**불일치**"
    print(f"  {c:9s} train {a1:.3f} / test {a2:.3f}  {ok}")
a1, a2 = auc(tr.y_sl, tr.score), auc(te.y_sl, te.score)
print(f"  score     train {a1:.3f} / test {a2:.3f}  {'일치' if (a1-.5)*(a2-.5)>0 else '**불일치**'}")

print("\n## 6. 10/02 세 signal")
print("| 진입 | score | box_pos | brk_fail | ext_atr | rev | persist | reentry | 결과 |")
print("|---|---|---|---|---|---|---|---|---|")
for _, r in d02.sort_values("entry").iterrows():
    print(f"| {r['entry']:%H:%M} | **{r['score']:.0f}** | {r['box_pos']:.2f} | {int(r['brk_fail'])} | "
          f"{r['ext_atr']:+.2f} | {r['rev']:+.2f} | {int(r['persist'])} | {int(r['reentry'])} | "
          f"{r['rs']} {r['krw']:+,.0f} |")

print("\n## 7. 정상 추세일 false alert (train+test)")
A = pd.concat([tr, te])
dd = A.groupby("day").agg(krw=("krw", "sum"), sl=("y_sl", "sum"), run=("y_run", "sum"),
                          mx=("score", "max"), n=("score", "size"), nhi=("score", lambda s: int((s >= th).sum())))
norm = dd[(dd.krw > 0) & (dd.sl == 0)]
chop = dd[(dd.krw < 0) & (dd.sl >= 2)]
print(f"  정상 추세일(일손익>0 ∧ 손절 0건) {len(norm)}일 중 상위20% 진입 발생 **{int((norm.nhi>0).sum())}일** "
      f"({(norm.nhi>0).mean()*100:.0f}%)")
print(f"  왕복 손실일(일손익<0 ∧ 손절≥2건) {len(chop)}일 중 발생 **{int((chop.nhi>0).sum())}일** "
      f"({(chop.nhi>0).mean()*100:.0f}%)")
print(f"  러너일 {int((dd.run>=1).sum())}일 중 발생 {int((dd[dd.run>=1].nhi>0).sum())}일")
print(f"  일 최고 score 평균: 정상일 {norm.mx.mean():.1f} · 왕복손실일 {chop.mx.mean():.1f}")

print("\n## 8. 10/01 · 10/02 오전 진입 시점 score")
for d in ("20261001", "20261002"):
    z = F[(F.day == d)].sort_values("entry")
    z = z.assign(score=score(z))
    for _, r in z.iterrows():
        print(f"  {d} {r['entry']:%H:%M} score **{r['score']:.0f}** · {r['rs']} {r['krw']:+,.0f}")
am = A[A.entry.dt.hour < 12]
print(f"\n  오전 진입 전체 평균 score {am.score.mean():.1f} "
      f"(손절건 {am[am.y_sl==1].score.mean():.1f} / 비손절 {am[am.y_sl==0].score.mean():.1f})")
F2 = pd.concat([tr, te, d02])
F2.to_csv(ROOT + "/bf_scored.csv", index=False, encoding="utf-8")
