"""진입시점 feature 예측력 스크리닝 (2026-10-17). READ-ONLY.

목적: full replay 를 돌리기 전에 후보를 줄인다.
  1) 최근 10일 손실 16 vs 이익 9 의 진입시점 차이
  2) 각 feature 의 AUC — 전체 / train(5~7월) / test(8~10월) + 날짜 bootstrap
  3) 기존 조건끼리의 중복 (상관)
  4) CHOP 라벨 진입만 따로

결과변수 둘:
  win    = 실현손익 > 0
  notimm = '들어가자마자 역행'이 아님 (mfe_b >= 0.3). 최근 손실의 62%가 immrev 라서
           이쪽이 이번 연구의 직접 목표다.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
pd.set_option("display.width", 250)

# a priori 부호: +1 = 값이 클수록 좋은 진입이라는 가설
FEATS = {
    "ema_stack": +1, "px_ema10": +1, "px_ema20": +1, "ema_sep": +1,
    "ema10_slope": +1, "ema20_slope": +1,
    "hist": +1, "hist_slope": +1, "t3_gain": +1, "t3_ratio": +1,
    "vwap_pos": +1, "atr_pct": 0, "vol_rel": 0,
    "pe30": +1, "pe60": +1, "xc60": -1,
    "rev15": -1, "rev30": -1, "rev60": -1, "nflag30": -1, "nflag60": -1,
}


def auc(y, s):
    m = ~(np.isnan(s) | np.isnan(y))
    y, s = y[m], s[m]
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return np.nan
    r = pd.Series(np.r_[pos, neg]).rank().to_numpy()
    return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def main():
    d = pd.read_csv(OUT + "/f1_features.csv", dtype={"day": str})
    d["notimm"] = 1 - d.immrev
    days = sorted(d.day.unique())
    r10 = d[d.day.isin(days[-10:])]

    print(f"전체 {len(d)}건 / 최근10일 {len(r10)}건 (이익 {int(r10.win.sum())} 손실 {int((1 - r10.win).sum())})\n")
    print("=" * 108)
    print("1) 최근 10영업일 — 손실 16 vs 이익 9 의 진입시점 feature")
    print("=" * 108)
    L, Wn = r10[r10.win == 0], r10[r10.win == 1]
    print(f"  {'feature':12s} {'손실16 평균':>12s} {'이익9 평균':>12s} {'차이':>9s} {'손실 중앙':>10s} {'이익 중앙':>10s}")
    for f in FEATS:
        print(f"  {f:12s} {L[f].mean():12.3f} {Wn[f].mean():12.3f} "
              f"{Wn[f].mean() - L[f].mean():+9.3f} {L[f].median():10.3f} {Wn[f].median():10.3f}")

    print("\n" + "=" * 108)
    print("2) 예측력 (AUC). 부호는 사전 가설로 정렬 — 0.5 초과면 가설대로")
    print("=" * 108)
    rng = np.random.default_rng(0)
    alld = d.day.to_numpy()
    uniq = d.day.unique()
    for yname in ("win", "notimm"):
        print(f"\n  [결과변수 = {yname}]  n={len(d)}  (양성 {int(d[yname].sum())})")
        print(f"  {'feature':12s} {'AUC':>6s} {'95% CI':>16s} {'train5-7':>9s} {'test8-10':>9s} {'최근10일':>9s}")
        y = d[yname].to_numpy(float)
        tr = (d.day < "20260801").to_numpy()
        r10m = d.day.isin(days[-10:]).to_numpy()
        res = []
        for f, sgn in FEATS.items():
            s = (sgn if sgn else 1) * d[f].astype(float).to_numpy()
            a = auc(y, s)
            bs = []
            for _ in range(800):
                pick = rng.choice(uniq, len(uniq))
                idx = np.concatenate([np.where(alld == k)[0] for k in pick])
                bs.append(auc(y[idx], s[idx]))
            lo, hi = np.nanpercentile(bs, [2.5, 97.5])
            res.append((f, sgn, a, lo, hi, auc(y[tr], s[tr]), auc(y[~tr], s[~tr]), auc(y[r10m], s[r10m])))
        for f, sgn, a, lo, hi, atr_, ate, a10 in sorted(res, key=lambda x: -(x[2] if x[2] == x[2] else 0)):
            mark = "  <<<" if (lo > 0.5 or hi < 0.5) else ""
            print(f"  {('+' if sgn >= 0 else '-') + f:12s} {a:6.3f} [{lo:.3f},{hi:.3f}] "
                  f"{atr_:9.3f} {ate:9.3f} {a10:9.3f}{mark}")

    print("\n" + "=" * 108)
    print("3) 기존 조건끼리 중복인가 (상관, |rho|>=0.6 만)")
    print("=" * 108)
    cols = [c for c in FEATS if d[c].notna().sum() > 100]
    cm = d[cols].rank().corr()
    seen = set()
    for i in cols:
        for j in cols:
            if i >= j or (i, j) in seen:
                continue
            seen.add((i, j))
            if abs(cm.loc[i, j]) >= 0.6:
                print(f"  {i:12s} ~ {j:12s} rho {cm.loc[i, j]:+.2f}")

    print("\n" + "=" * 108)
    print("4) CHOP 라벨 진입만 (B3 청산 경로를 탄 거래)")
    print("=" * 108)
    c = d[d.chop == 1]
    print(f"  n {len(c)} / 합 {c.krw.sum():+,.0f} / 승률 {c.win.mean() * 100:.0f}% "
          f"| 8~10월 n {len(c[c.day >= '20260801'])} 합 {c[c.day >= '20260801'].krw.sum():+,.0f}")
    y = c.win.to_numpy(float)
    print(f"  {'feature':12s} {'AUC(win)':>9s} {'AUC(notimm)':>12s}")
    yn = c.notimm.to_numpy(float)
    for f, sgn in FEATS.items():
        s = (sgn if sgn else 1) * c[f].astype(float).to_numpy()
        print(f"  {('+' if sgn >= 0 else '-') + f:12s} {auc(y, s):9.3f} {auc(yn, s):12.3f}")

    print("\n" + "=" * 108)
    print("5) ema_stack(TEG 조건) 이 실제로 무엇을 가르는가")
    print("=" * 108)
    for lab, g in (("전체", d), ("8~10월", d[d.day >= "20260801"]), ("최근10일", r10),
                   ("CHOP 라벨", d[d.chop == 1])):
        for v in (0, 1):
            x = g[g.ema_stack == v]
            if not len(x):
                continue
            print(f"  {lab:8s} ema_stack={v}  n {len(x):3d}  승률 {x.win.mean() * 100:5.1f}%  "
                  f"합 {x.krw.sum():+11,.0f}  평균 {x.krw.mean():+9,.0f}  즉시역행 {x.immrev.mean() * 100:4.0f}%")


if __name__ == "__main__":
    main()
