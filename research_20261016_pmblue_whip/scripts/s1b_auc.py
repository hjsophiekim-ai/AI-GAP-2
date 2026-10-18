"""연구1 보강 — 임계값 없이 본 판별력 (2026-10-16). READ-ONLY.

threshold 세밀탐색을 하지 말라는 지시에 맞춰, 임계값을 고르지 않고
연속값 그대로의 AUC/순위상관으로 본다. train = 5~7월, test = 8~10월.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"


def auc(y, s):
    m = ~(np.isnan(s) | np.isnan(y))
    y, s = y[m], s[m]
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return np.nan
    r = pd.Series(np.r_[pos, neg]).rank().to_numpy()
    return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def main():
    d = pd.read_csv(OUT + "/s1_pmblue.csv", dtype={"day": str})
    y = (d.ret > 0).astype(float).to_numpy()
    tr = (d.day < "20260801").to_numpy()
    rng = np.random.default_rng(0)
    days = d.day.unique()
    print(f"13:30~14:30 DOWN_BLUE {len(d)}건 / {d.day.nunique()}일 "
          f"(train 5~7월 {int(tr.sum())} / test 8~10월 {int((~tr).sum())})")
    print("AUC > 0.5 = '그 값이 클수록 BLUE 가 맞았다'\n")
    for c in ("am_peak_pct", "pullback_pct", "am_close_pct", "am_low_pct"):
        s = d[c].astype(float).to_numpy()
        a, a_tr, a_te = auc(y, s), auc(y[tr], s[tr]), auc(y[~tr], s[~tr])
        bs = []
        for _ in range(2000):
            pick = rng.choice(days, len(days))
            idx = np.concatenate([np.where(d.day.to_numpy() == k)[0] for k in pick])
            bs.append(auc(y[idx], s[idx]))
        lo, hi = np.nanpercentile(bs, [2.5, 97.5])
        print(f"  {c:14s} AUC {a:.3f} [{lo:.3f},{hi:.3f}]  train {a_tr:.3f}  test {a_te:.3f}")

    print("\n수익(ret) 과의 Spearman 순위상관")
    for c in ("am_peak_pct", "pullback_pct"):
        for lab, m in (("전체", np.ones(len(d), bool)), ("5~7월", tr), ("8~10월", ~tr)):
            x = d[m]
            rho = x[c].rank().corr(x.ret.rank())        # scipy 없이 Spearman = 순위 Pearson
            print(f"  {c:14s} {lab:6s} rho {rho:+.3f}  n {len(x)}")

    print("\n오전고점 사분위별 PM-BLUE 성적 (임계값 탐색 아님, 분위수 고정)")
    d["q"] = pd.qcut(d.am_peak_pct, 4, labels=["Q1 낮음", "Q2", "Q3", "Q4 높음"])
    for lab, m in (("전체", np.ones(len(d), bool)), ("5~7월", tr), ("8~10월", ~tr)):
        print(f"  [{lab}]")
        for q, g in d[m].groupby("q", observed=True):
            print(f"    {q:8s} n {len(g):3d} 승률 {(g.ret > 0).mean() * 100:5.1f}% "
                  f"평균 {g.ret.mean():+6.2f} 합 {g.ret.sum():+7.1f} "
                  f"(고점 {g.am_peak_pct.min():+.1f}~{g.am_peak_pct.max():+.1f}%)")


if __name__ == "__main__":
    main()
