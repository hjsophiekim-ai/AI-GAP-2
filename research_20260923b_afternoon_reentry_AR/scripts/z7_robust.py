"""Z7 — AR 변형 강건성 (LOO / excl-top / bootstrap / 반기분할). 엔진 재실행 없음."""
import sys, pickle; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import numpy as np, pandas as pd
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import compound, pnl, summarize
tr = pickle.load(open(HERE / "z5_trades.pkl", "rb"))
import hengine5 as H
dates = sorted({t["date"] for v in tr.values() for t in v})
allday = pickle.load(open(HERE / "z1.pkl", "rb"))["dates"]

def comp(ts, ds): return compound(ts, ds)

print("=== 전체 (78일) ===")
rows = []
for k in ("BASE", "AR0", "AR1", "PLB"):
    m = summarize(tr[k], allday)
    rows.append(dict(variant=k, n=m["trades"], compound=m["compound_pct"], pf=m["pf"], mdd=m["mdd_pct"],
                     win=m["win_rate_pct"], top5excl=m["top5_excl_pct"], top10excl=m["top10_excl_pct"],
                     excl_top1d=m["excl_top1d"], excl_top3d=m["excl_top3d"], month=m["monthly_pct"]))
R = pd.DataFrame(rows); pd.set_option("display.width", 260)
print(R.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
b = R[R["variant"] == "BASE"].iloc[0]
print("\n델타 vs BASE:")
for _, r in R[R["variant"] != "BASE"].iterrows():
    print("  %-4s 복리 %+8.3f%%p | top5제외 %+8.3f%%p | top10제외 %+8.3f%%p | 최고일제외 %+8.3f%%p | 상위3일제외 %+8.3f%%p | MDD %+.3f"
          % (r["variant"], r.compound - b.compound, r.top5excl - b.top5excl, r.top10excl - b.top10excl,
             r.excl_top1d - b.excl_top1d, r.excl_top3d - b.excl_top3d, r.mdd - b.mdd))

print("\n=== LOO (하루씩 제외했을 때 AR1-BASE 복리 델타) ===")
for k in ("AR0", "AR1", "PLB"):
    ds = []
    for d in allday:
        sub = [x for x in allday if x != d]
        ds.append(comp(tr[k], sub) - comp(tr["BASE"], sub))
    ds = np.array(ds)
    print("  %-4s min %+8.3f  median %+8.3f  max %+8.3f  | 델타<=0 인 날 %d/%d"
          % (k, ds.min(), np.median(ds), ds.max(), (ds <= 0).sum(), len(ds)))

print("\n=== 반기 분할 (앞 39일 / 뒤 39일) ===")
h1, h2 = allday[:39], allday[39:]
for k in ("BASE", "AR0", "AR1", "PLB"):
    print("  %-4s H1(%s~%s) %8.3f   H2(%s~%s) %8.3f"
          % (k, h1[0], h1[-1], comp(tr[k], h1), h2[0], h2[-1], comp(tr[k], h2)))

print("\n=== bootstrap (일 단위 복원추출 2000회, AR-BASE 복리 델타) ===")
rng = np.random.default_rng(11)
for k in ("AR0", "AR1", "PLB"):
    out = []
    for _ in range(2000):
        s = list(rng.choice(allday, len(allday), replace=True))
        out.append(comp(tr[k], s) - comp(tr["BASE"], s))
    out = np.array(out)
    print("  %-4s mean %+8.3f  95%%CI [%+8.3f, %+8.3f]  P(>0)=%.1f%%"
          % (k, out.mean(), np.percentile(out, 2.5), np.percentile(out, 97.5), (out > 0).mean() * 100))

print("\n=== AR1 추가거래 9건 자체 강건성 ===")
def key(t): return (t["date"], t["entry_time"], t["direction"])
bs = {key(t) for t in tr["BASE"]}
add = [t for t in tr["AR1"] if key(t) not in bs]
v = np.array(sorted([t["net_pct"] for t in add], reverse=True))
print("  net합 %+.3f%%p  승%d/패%d" % (v.sum(), (v > 0).sum(), (v <= 0).sum()))
for k in (1, 2, 3):
    print("   top%d 제외: 합 %+.3f%%p  평균 %+.3f%%" % (k, v[k:].sum(), v[k:].mean()))
