"""syn_stats — CHOP 손실쪽 가설 (B3 SL 0.8/1.2, B3 본전보호 BE05) vs R0. 83일 창, 인샘플 = ~0928, 0929 = OOS 참고."""
import pickle
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0, "proj"); sys.path.insert(0, "proj/scripts")
L = lambda n: pickle.load(open(f"out_{n}_d83.pkl", "rb"))["trades"]
ALLD = list(pickle.load(open("_ctx83.pkl", "rb"))["dates"])
D = [d for d in ALLD if d <= "20260928"]
NAMES = ["R0", "SL08", "SL12", "BE05"]
RUN = {"R0": L("H30"), "SL08": L("SL08"), "SL12": L("SL12"), "BE05": L("BE05")}
IN = {k: [t for t in v if t["date"] in set(D)] for k, v in RUN.items()}
pnl = lambda t: t["net_pct"] * t["w1a"]
key = lambda t: (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])
P = lambda *a: print(*a, flush=True)
CHOP = lambda ts: [t for t in ts if t.get("entry_regime") == "CHOP"]
CD = sorted({t["date"] for t in CHOP(IN["R0"])})


def fac(ts, days):
    f = defaultdict(lambda: 1.0)
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        f[t["date"]] *= 1 + pnl(t) / 100
    return np.array([f[d] for d in days])


comp = lambda ts, days: (np.prod(fac(ts, days)) - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts):
    e = pk = 1.0; m = 0.0
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, e / pk - 1)
    return m * 100


P(f"인샘플 {len(D)}일 ({D[0]}~{D[-1]}), CHOP 진입일 {len(CD)}일")
P("\n## 전체 / CHOP 성과 (인샘플)")
P("| 전략 | 83일 복리 | 9월 복리 | PF | MDD | CHOP n | CHOP 합 | CHOP PF | CHOP일 손실일 | CHOP일 최저 | CHOP일 σ | CHOP일 ≥1% | 비CHOP 차이 |")
P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
SEP = [d for d in D if d[:6] == "202609"]
K0 = {key(t): t for t in IN["R0"]}
for k in NAMES:
    ts = IN[k]; c = CHOP(ts); dd = (fac(c, CD) - 1) * 100
    K = {key(t): t for t in ts}
    nonchop_diff = sum(1 for kk in set(K0) | set(K) if (K0.get(kk, K.get(kk)) or {}).get("entry_regime") != "CHOP" and (
        kk not in K0 or kk not in K or abs(K0[kk]["net_pct"] - K[kk]["net_pct"]) > 1e-9))
    P(f"| {k} | {comp(ts, D):+.2f} | {comp(ts, SEP):+.2f} | {pf(ts):.3f} | {mdd(ts):.2f} | {len(c)} | {sum(pnl(t) for t in c):+.2f} | "
      f"{pf(c):.3f} | {(dd < 0).sum()}/{len(CD)} | {dd.min():+.2f} | {dd.std():.2f} | {(dd >= 1).sum()} | {nonchop_diff} |")

P("\n## CHOP 청산 경로 (합, W1a 가중)")
for k in NAMES:
    g = defaultdict(list)
    for t in CHOP(IN[k]):
        r = "Q2" if t.get("b3_rescued") else ("Y3" if t.get("b3_y3") else ("H30" if t.get("b3_ext_prom") else t["exit_reason"]))
        g[r].append(pnl(t))
    P(f"- {k}: " + " | ".join(f"{r} {len(v)}건 {sum(v):+.2f}" for r, v in sorted(g.items())))

rng = np.random.default_rng(20260929)
IDX = rng.integers(0, len(CD), size=(20000, len(CD)))
F0 = fac(CHOP(IN["R0"]), CD)
half = len(CD) // 2
P("\n## 강건성 (CHOP 진입일 기준, R0 대비) — ⚠ CHOP 18일/40건, 사실상 9월 한 episode")
P("| 전략 | ΔCHOP 복리 | day-LOO 음수 | 전반 9일 Δ | 후반 9일 Δ | bootstrap P(Δ>0) | 95% CI | 바뀐 거래 | 최대 단일거래 기여 |")
P("|---|---|---|---|---|---|---|---|---|")
for k in NAMES[1:]:
    Fk = fac(CHOP(IN[k]), CD)
    loo = [(np.prod(Fk) / Fk[i] - np.prod(F0) / F0[i]) * 100 for i in range(len(CD))]
    bs = (np.prod(Fk[IDX], axis=1) - np.prod(F0[IDX], axis=1)) * 100
    e = (np.prod(Fk[:half]) - np.prod(F0[:half])) * 100; l = (np.prod(Fk[half:]) - np.prod(F0[half:])) * 100
    K = {key(t): t for t in IN[k]}
    td = {kk: pnl(K[kk]) - pnl(K0[kk]) for kk in set(K0) & set(K) if abs(pnl(K[kk]) - pnl(K0[kk])) > 1e-9}
    tot = sum(abs(v) for v in td.values()); big = max(td.items(), key=lambda kv: abs(kv[1])) if td else (("-", "-", "-"), 0.0)
    P(f"| {k} | {(np.prod(Fk) - np.prod(F0)) * 100:+.3f} | {sum(x < 0 for x in loo)}/{len(CD)} | {e:+.3f} | {l:+.3f} | "
      f"{100 * (bs > 0).mean():.1f}% | [{np.percentile(bs, 2.5):+.2f}, {np.percentile(bs, 97.5):+.2f}] | {len(td)} | "
      f"{big[0][0]} {str(big[0][1])[11:16]} {big[1]:+.2f} ({100 * abs(big[1]) / max(tot, 1e-9):.0f}%) |")

P("\n## 바뀐 CHOP 거래 상세")
for k in NAMES[1:]:
    K = {key(t): t for t in IN[k]}
    rows = [(kk, K0[kk], K[kk]) for kk in sorted(set(K0) & set(K)) if abs(K0[kk]["net_pct"] - K[kk]["net_pct"]) > 1e-9]
    P(f"- {k}: " + "; ".join(f"{a[0][4:]} {a[1][11:16]} {a[2][:4]} R0 {b['net_pct']:+.2f}({b['exit_reason'][:9]}) → {c['net_pct']:+.2f}({c['exit_reason'][:9]})"
                            for a, b, c in rows))

P("\n## 0929 OOS 참고 (집계 제외)")
for k in NAMES:
    tt = sorted([t for t in RUN[k] if t["date"] == "20260929"], key=lambda t: t["entry_time"])
    P(f"- {k}: " + " / ".join(f"{pd.Timestamp(t['entry_time']).strftime('%H:%M')} {t['direction'][:4]} → "
                              f"{pd.Timestamp(t['exit_time']).strftime('%H:%M')} {t['exit_reason'][:10]} {t['net_pct']:+.3f}" for t in tt))
