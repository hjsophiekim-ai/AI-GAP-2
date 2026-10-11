"""R5b — 강건성. bootstrap 은 **일 단위 복원추출의 중복을 살려** 계산한다
(set() 로 접으면 복원추출이 아니게 된다)."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "r3.pkl", "rb"))
D, RUNS = Z["dates"], Z["runs"]
TAGS = sys.argv[1:] or Z["bat"]
SEP = [d for d in D if d >= "20260901"]


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    return d


DF = {t: prep(RUNS[t]) for t in RUNS}
DAY = {t: {k: g.pnl.values for k, g in DF[t].groupby("date")} for t in RUNS}


def comp_days(t, days):
    p = np.concatenate([DAY[t][d] for d in days if d in DAY[t]]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


def full(t, d=None):
    g = DF[t] if d is None else DF[t][DF[t].date.isin(set(d))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


B = "P0_BASE"
rng = np.random.default_rng(20260924)
BOOT = [list(rng.choice(D, len(D), replace=True)) for _ in range(10000)]
print("=" * 150)
print("9. 강건성 — BASE 대비 복리 델타 (bootstrap 10,000 · 일 단위 복원추출)")
print("=" * 150)
rows = []
for t in TAGS:
    d = DF[t]
    p = d.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[d.net_pct > 0], p[d.net_pct <= 0]
    r = {"후보": t, "거래": len(d),
         "승률": round(100 * float((d.net_pct > 0).mean()), 1),
         "PF": round(float(w.sum() / -l.sum()), 3),
         "MDD": round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
         "전체": round(full(t), 2), "Δ전체": round(full(t) - full(B), 2),
         "9월": round(full(t, SEP), 2), "Δ9월": round(full(t, SEP) - full(B, SEP), 2),
         "뒤28": round(full(t, D[50:]), 2), "Δ뒤28": round(full(t, D[50:]) - full(B, D[50:]), 2)}
    for n in (1, 3, 5):
        cv = float(((1 + d.drop(d.nlargest(n, "pnl").index).sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        bb = DF[B]
        bv = float(((1 + bb.drop(bb.nlargest(n, "pnl").index).sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        r["Δtop%d제외" % n] = round(cv - bv, 2)
    loo = np.array([comp_days(t, [x for x in D if x != day]) - comp_days(B, [x for x in D if x != day]) for day in D])
    r["LOO최소"] = round(float(loo.min()), 2); r["LOO중앙"] = round(float(np.median(loo)), 2)
    r["LOO<=0"] = int((loo <= 0).sum())
    bs = np.array([comp_days(t, s) - comp_days(B, s) for s in BOOT])
    r["bs평균"] = round(float(bs.mean()), 2)
    r["bsCI"] = "[%.1f, %.1f]" % (np.percentile(bs, 2.5), np.percentile(bs, 97.5))
    r["P(>0)%"] = round(100 * float((bs > 0).mean()), 1)
    r["MFE3"] = int((d.mfe >= 3).sum()); r["MFE5"] = int((d.mfe >= 5).sum()); r["MFE8"] = int((d.mfe >= 8).sum())
    r["일평균자본"] = round(d.groupby("date").w1a.sum().mean(), 3)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))
