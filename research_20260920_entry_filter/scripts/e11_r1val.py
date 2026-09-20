"""E11 — R1(1.0/1.1) 정밀검증: 제거거래 전량, 일단위 부트스트랩, 위약, 월별."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
import axlib as A
from common import summarize, compound
import axval as V

E10 = pickle.load(open(A.HERE / "e10.pkl", "rb"))
OUT, D = E10["out"], E10["dates"]
BASE = OUT["base"]
key = lambda t: (t["date"], t["entry_time"])
BK = {key(t): t for t in BASE}
mB = summarize(BASE, D)
E1 = pickle.load(open(A.HERE / "e1.pkl", "rb"))
FL = {(r["date"], r["at"]): r for r in E1["N1C1"]}
rng = np.random.default_rng(20260920)

for thr in (1.0, 1.1):
    ts = OUT[thr]; ka = {key(t): t for t in ts}
    m = summarize(ts, D)
    gone = sorted(set(BK) - set(ka)); new = sorted(set(ka) - set(BK))
    print("\n" + "=" * 132)
    print(f"R1 임계값 {thr}: 78d {m['compound_pct']:.2f} (ΔC1 {m['compound_pct']-mB['compound_pct']:+.2f}) "
          f"/ 제거 {len(gone)}건 / 신규 {len(new)}건")
    print("=" * 132)
    print(f"{'제거된 거래':10s} {'시각':6s} {'방향':10s} {'e20-e50':>8s} {'net':>8s} {'peak':>6s} {'청산사유'}")
    gs = 0.0
    for k in gone:
        t = BK[k]; f = FL.get(k, {})
        gs += float(t["net_pct"])
        print(f"{k[0]:10s} {str(k[1])[11:16]:6s} {t['direction']:10s} "
              f"{(f.get('e20_e50_pct') or 0):8.3f} {t['net_pct']:+8.3f} {t['peak_net_pct']:6.2f} "
              f"{t['exit_reason']}")
    print(f"  제거거래 단순합 {gs:+.2f}%p (평균 {gs/max(1,len(gone)):+.2f})")
    if new:
        print(f"\n{'신규 거래':10s} {'시각':6s} {'방향':10s} {'net':>8s} {'청산사유'}")
        ns = 0.0
        for k in new:
            t = ka[k]; ns += float(t["net_pct"])
            print(f"{k[0]:10s} {str(k[1])[11:16]:6s} {t['direction']:10s} {t['net_pct']:+8.3f} {t['exit_reason']}")
        print(f"  신규거래 단순합 {ns:+.2f}%p")

    # 제거거래 1건씩 되살리면 (LOO)
    print(f"\n  LOO — 제거거래 1건을 되살렸다고 보고 단순합 재계산:")
    for k in sorted(gone, key=lambda x: float(BK[x]["net_pct"]))[:5]:
        print(f"    {k[0]} {str(k[1])[11:16]} net {BK[k]['net_pct']:+7.3f} -> 이 1건 남기면 제거합 {gs-float(BK[k]['net_pct']):+.2f}")

    # 일단위 부트스트랩 (78일 재표집 10000회)
    days = np.array(D)
    ua = {d: compound([t for t in ts if t["date"] == d], [d]) for d in D}
    ub = {d: compound([t for t in BASE if t["date"] == d], [d]) for d in D}
    da = np.array([ua[d] - ub[d] for d in D])
    idx = rng.integers(0, len(D), size=(10000, len(D)))
    bs = da[idx].mean(axis=1)
    print(f"\n  일단위 부트스트랩 10000회: 평균 {bs.mean():+.4f} / >0 비율 {100*(bs>0).mean():.1f}% "
          f"/ 5%분위 {np.percentile(bs,5):+.4f} / 95%분위 {np.percentile(bs,95):+.4f}")

    # 위약 — 같은 수의 거래를 무작위 제거
    n_rm = len(gone)
    pl = []
    allk = list(BK)
    for _ in range(1000):
        rm = set(rng.choice(len(allk), size=n_rm, replace=False))
        keep = [BK[allk[i]] for i in range(len(allk)) if i not in rm]
        pl.append(compound(keep, D))
    pl = np.array(pl)
    real = m["compound_pct"]
    print(f"  위약 1000회(같은 건수 무작위 제거, 신규진입 없음): 평균 {pl.mean():.1f} "
          f"/ 실제 {real:.1f} 를 넘은 위약 {int((pl>=real).sum())}건 ({100*(pl>=real).mean():.1f}%)")

    # 월별
    mm = " ".join(f"{k}={compound(ts,[x for x in D if x.startswith(k)])-compound(BASE,[x for x in D if x.startswith(k)]):+.1f}"
                  for k in ("202605", "202606", "202607", "202608", "202609"))
    print(f"  월별ΔC1: {mm}")
