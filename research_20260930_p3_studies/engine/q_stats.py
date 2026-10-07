"""q_stats — P3 CHOP 개선 후보(Q1=M2 / Q2 / Q3) vs R0. CHOP 성과·runner·episode 분리."""
import bisect
import pickle
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0, "proj"); sys.path.insert(0, "proj/scripts")
L = lambda n: pickle.load(open(f"out_{n}.pkl", "rb"))["trades"]
D = list(pickle.load(open("_ctx80.pkl", "rb"))["dates"]); R30 = D[-30:]
SEP = [d for d in D if d[:6] == "202609"]
NAMES = {"R0": "R0", "Q1": "M2", "Q2": "Q2", "Q3": "Q3"}
RUNS = {k: L(v) for k, v in NAMES.items()}
B = L("BASE")
pnl = lambda t: t["net_pct"] * t["w1a"]
key = lambda t: (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])
rows = sorted(((pd.Timestamp(t["exit_time"]), t["h50_held"], t["tp1_hit"]) for t in B), key=lambda r: r[0])
ex = [r[0] for r in rows]


def rg(ts):
    k = bisect.bisect_left(ex, pd.Timestamp(ts))
    if k < 10: return "WARMUP"
    r = rows[k - 10:k]
    return "CHOP" if sum(z[1] for z in r) >= 4 and sum(z[2] for z in r) <= 2 else "TREND"


for t in B: t["entry_regime"] = rg(t["entry_time"])


def comp(ts, ds):
    ds = set(ds); e = 1.0
    for t in sorted((x for x in ts if x["date"] in ds), key=lambda x: x["exit_time"]): e *= 1 + pnl(t) / 100
    return (e - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts):
    e = pk = 1.0; m = 0
    for t in sorted(ts, key=lambda x: x["exit_time"]): e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, e / pk - 1)
    return m * 100


def fac(ts):
    f = defaultdict(lambda: 1.0)
    for t in sorted(ts, key=lambda x: x["exit_time"]): f[t["date"]] *= 1 + pnl(t) / 100
    return np.array([f[d] for d in D])


def excl(ts, k):
    top = set(map(id, sorted(ts, key=pnl, reverse=True)[:k])); return comp([t for t in ts if id(t) not in top], D)


chop = lambda ts: [t for t in ts if t.get("entry_regime") == "CHOP"]
P = lambda *a: print(*a, flush=True)

P("%-3s %4s %9s %8s %8s %6s %7s %6s | %6s %6s %6s %7s | %4s %5s | %8s" % (
    "전략", "n", "80일", "최근30", "9월", "PF", "MDD", "WR", "CHOPn", "CHOPWR", "CHOPPF", "CHOP합", "손실일", "≥2%일", "Δ80"))
for k, ts in RUNS.items():
    c = chop(ts); dd = (fac(ts) - 1) * 100
    P("%-3s %4d %9.3f %8.3f %8.3f %6.3f %7.3f %6.2f | %6d %5.1f%% %6.3f %+7.3f | %4d %5d | %+8.3f" % (
        k, len(ts), comp(ts, D), comp(ts, R30), comp(ts, SEP), pf(ts), mdd(ts), 100 * np.mean([pnl(t) > 0 for t in ts]),
        len(c), 100 * np.mean([pnl(t) > 0 for t in c]), pf(c), sum(pnl(t) for t in c), (dd < 0).sum(), (dd >= 2).sum(),
        comp(ts, D) - comp(RUNS["R0"], D)))

P("\nCHOP runner 보존 (BASE CHOP 진입, 보존 = net >= BASE−0.5)")
bc = chop(B)
for thr in (3.0, 5.0):
    rs = [t for t in bc if t["peak_net_pct"] >= thr]; bp = sum(pnl(t) for t in rs)
    line = f"  MFE≥{thr:.0f} n={len(rs)} BASE {bp:+.3f} |"
    for k, ts in RUNS.items():
        K = {key(t): t for t in ts}
        kept = sum(1 for t in rs if key(t) in K and K[key(t)]["net_pct"] >= t["net_pct"] - 0.5)
        vp = sum(pnl(K[key(t)]) for t in rs if key(t) in K)
        line += f" {k} {kept}/{len(rs)} 이익 {100*vp/bp:.1f}% |"
    P(line)
P("  runner 별:")
for t in sorted([t for t in bc if t["peak_net_pct"] >= 3.0], key=lambda t: t["entry_time"]):
    s = f"   {t['date']} {pd.Timestamp(t['entry_time']).strftime('%H:%M')} BASE {t['net_pct']:+.3f}"
    for k, ts in RUNS.items():
        x = {key(z): z for z in ts}.get(key(t))
        s += f" | {k} " + ("소멸" if x is None else f"{x['net_pct']:+.3f}{' R' if x.get('b3_rescued') else ''}{' Y' if x.get('b3_y3') else ''}")
    P(s)

P("\nCHOP episode 분리 (CHOP 진입 거래 가중합, 월별)")
for k, ts in RUNS.items():
    c = chop(ts); m = defaultdict(float)
    for t in c: m[t["date"][:6]] += pnl(t)
    P(f"  {k}: " + " ".join(f"{mm} {v:+.3f}" for mm, v in sorted(m.items())))

rng = np.random.default_rng(20260928); IDX = rng.integers(0, len(D), size=(20000, len(D)))
F0 = fac(RUNS["R0"]); K0 = {key(t): t for t in RUNS["R0"]}
for k in ("Q1", "Q2", "Q3"):
    ts = RUNS[k]; Fk = fac(ts)
    loo = [(np.prod(Fk) / Fk[i] - np.prod(F0) / F0[i]) * 100 for i in range(len(D))]
    bs = (np.prod(Fk[IDX], axis=1) - np.prod(F0[IDX], axis=1)) * 100
    dd = {D[i]: (Fk[i] - F0[i]) * 100 for i in range(len(D)) if abs(Fk[i] - F0[i]) > 1e-12}
    tot = sum(abs(v) for v in dd.values()); big = max(dd.items(), key=lambda kv: abs(kv[1])) if dd else ("-", 0)
    P(f"\n[{k}] Δ80 {comp(ts,D)-comp(RUNS['R0'],D):+.3f} | LOO 음수 {sum(x<0 for x in loo)}/80 min {min(loo):+.3f} | "
      + " ".join(f"Top{n}x {excl(ts,n)-excl(RUNS['R0'],n):+.2f}" for n in (1, 3, 5, 10))
      + f" | bootstrap {bs.mean():+.3f} [{np.percentile(bs,2.5):+.3f},{np.percentile(bs,97.5):+.3f}] P(>0) {100*(bs>0).mean():.1f}%"
      + f" | 다른날 {len(dd)} 최대 {big[0]} {big[1]:+.2f} ({100*abs(big[1])/max(tot,1e-9):.0f}%)")
    VK = {key(t): t for t in ts}
    for kk in sorted(set(K0) | set(VK)):
        a, b = K0.get(kk), VK.get(kk)
        if a is None or b is None or abs(a["net_pct"] - b["net_pct"]) > 1e-9:
            fa = "-" if a is None else f"{a['net_pct']:+.3f} {a['exit_reason'][:14]}"
            fb = "-" if b is None else f"{b['net_pct']:+.3f} {b['exit_reason'][:14]}{' R' if b.get('b3_rescued') else ''}{' Y' if b.get('b3_y3') else ''}"
            P(f"    {kk[0]} {kk[1][11:16]} {kk[2][:4]} | R0 {fa} | {k} {fb}")
