"""G2 - 축A/C 용 단일 feature 판별력. 표본 25건(train 4)이라 결론이 아니라 진단이다."""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 320)
HERE = Path(__file__).resolve().parent
F = pd.read_csv(HERE / "g1_chop_features.csv")
print("CHOP ON 진입 %d건 (train 비9월 %d / OOS 9월 %d)"
      % (len(F), int((~F.is_sep).sum()), int(F.is_sep.sum())))
print("!! train 표본 %d건 — feature ranking 학습 불가 수준\n" % int((~F.is_sep).sum()))

FEATS = ["quality", "teg_pass", "gap_s2_dir", "gap_dir", "e20_50_dir", "e50_slope_dir",
         "vwap_dir", "etf_conf", "cross_cnt", "opp_flag_cnt", "flag_interval", "cum_move",
         "e10_20", "tmin", "slot", "flag_ord"]
rows = []
for c in FEATS:
    if c not in F:
        continue
    v = pd.to_numeric(F[c], errors="coerce")
    if v.notna().sum() < 8:
        rows.append(dict(feature=c, 결측=int(v.isna().sum()), 비고="표본부족")); continue
    lo, hi = v[F.loss], v[~F.loss]
    # AUC (loss 를 양성으로)
    ok = v.notna()
    r = v[ok].rank()
    n1, n0 = int(F.loss[ok].sum()), int((~F.loss[ok]).sum())
    auc = (r[F.loss[ok]].sum() - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else np.nan
    # 3분위 버킷별 평균 net (단조성)
    try:
        b = pd.qcut(v, 3, labels=["하", "중", "상"], duplicates="drop")
        g = F.groupby(b, observed=True).net.mean().round(3).to_dict()
    except Exception:
        g = {}
    rows.append(dict(feature=c, 결측=int(v.isna().sum()),
                     손실평균=round(float(lo.mean()), 3), 이익평균=round(float(hi.mean()), 3),
                     차=round(float(hi.mean() - lo.mean()), 3), AUC=round(float(auc), 3),
                     버킷net=str(g)))
R = pd.DataFrame(rows)
print(R.to_string(index=False))
print("\n[train(비9월 4건) vs OOS(9월 21건) 부호 일치 확인]")
rows = []
for c in FEATS:
    if c not in F:
        continue
    v = pd.to_numeric(F[c], errors="coerce")
    tr, oo = ~F.is_sep, F.is_sep
    def diff(m):
        a, b = v[m & F.loss], v[m & ~F.loss]
        return float(b.mean() - a.mean()) if a.notna().sum() and b.notna().sum() else np.nan
    dt_, do_ = diff(tr), diff(oo)
    rows.append(dict(feature=c, train차=round(dt_, 3) if dt_ == dt_ else None,
                     OOS차=round(do_, 3) if do_ == do_ else None,
                     부호일치=(None if (dt_ != dt_ or do_ != do_) else bool(np.sign(dt_) == np.sign(do_)))))
print(pd.DataFrame(rows).to_string(index=False))
