"""R5 — 최종후보 강건성: Top1/3/5 제거 · LOO · bootstrap 10,000 · 자본사용률.

사용: python r5_robust.py TAG1 TAG2 ...   (생략 시 전 후보)
엔진 재실행 없음 — r3.pkl 의 거래집합만 쓴다.
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 320)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "r3.pkl", "rb"))
D, RUNS = Z["dates"], Z["runs"]
TAGS = sys.argv[1:] or Z["bat"]
SEP = [d for d in D if d >= "20260901"]


def df_of(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    return d


DF = {t: df_of(RUNS[t]) for t in Z["bat"]}


def comp(d, dates=None):
    g = d if dates is None else d[d.date.isin(set(dates))]
    g = g.sort_values("exit_time")
    return float(((1 + g.pnl.values / 100).prod() - 1) * 100) if len(g) else 0.0


B = DF["P0_BASE"]
print("=" * 124); print("9. 강건성 — BASE 대비 복리 델타 기준"); print("=" * 124)
rng = np.random.default_rng(20260924)
rows = []
for t in TAGS:
    d = DF[t]
    r = {"후보": t, "거래": len(d), "전체": round(comp(d), 2),
         "Δ전체": round(comp(d) - comp(B), 2),
         "9월": round(comp(d, SEP), 2), "Δ9월": round(comp(d, SEP) - comp(B, SEP), 2)}
    for n in (1, 3, 5):
        top = d.nlargest(n, "pnl").index
        rest = d.drop(top).sort_values("exit_time")
        bt = B.nlargest(n, "pnl").index
        brest = B.drop(bt).sort_values("exit_time")
        cv = float(((1 + rest.pnl.values / 100).prod() - 1) * 100)
        bv = float(((1 + brest.pnl.values / 100).prod() - 1) * 100)
        r["Δtop%d제외" % n] = round(cv - bv, 2)
    loo = np.array([comp(d, [x for x in D if x != day]) - comp(B, [x for x in D if x != day])
                    for day in D])
    r["LOO최소"] = round(float(loo.min()), 2)
    r["LOO중앙"] = round(float(np.median(loo)), 2)
    r["LOO<=0일"] = int((loo <= 0).sum())
    bs = np.empty(10000)
    for i in range(10000):
        s = list(rng.choice(D, len(D), replace=True))
        bs[i] = comp(d, s) - comp(B, s)
    r["bs평균"] = round(float(bs.mean()), 2)
    r["bs_CI2.5"] = round(float(np.percentile(bs, 2.5)), 2)
    r["bs_CI97.5"] = round(float(np.percentile(bs, 97.5)), 2)
    r["P(>0)"] = round(100 * float((bs > 0).mean()), 1)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))
