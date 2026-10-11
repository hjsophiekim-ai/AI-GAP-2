"""grace_stats — Y3 grace (20분 시점 ±0.2% 면 10분 유예) 9월 전용 + 0929 OOS 참고 (2026-09-29)."""
import pickle
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0, "proj"); sys.path.insert(0, "proj/scripts")
L = lambda n: pickle.load(open(f"out_{n}_d83.pkl", "rb"))["trades"]
D = list(pickle.load(open("_ctx83.pkl", "rb"))["dates"])
SEP = [d for d in D if "20260901" <= d <= "20260928"]
OOS = "20260929"
ALL = {"R0": L("H30"), "G10": L("G10"), "G10_STRUCT": L("G10S")}
S = {k: [t for t in v if t["date"] in set(SEP)] for k, v in ALL.items()}
pnl = lambda t: t["net_pct"] * t["w1a"]
key = lambda t: (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])
P = lambda *a: print(*a, flush=True)
hm = lambda x: pd.Timestamp(x).strftime("%H:%M") if x else "-"

prev = pickle.load(open("prev/out_H30_d83_prev.pkl", "rb"))["trades"]
sig = lambda ts: sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]), round(float(t["net_pct"]), 8)) for t in ts)
P(f"R0 parity (진단필드 추가 엔진 vs 직전 83일 R0): {'IDENTICAL' if sig(ALL['R0']) == sig(prev) else 'DIFF'}")


def fac(ts, days):
    f = defaultdict(lambda: 1.0)
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        f[t["date"]] *= 1 + pnl(t) / 100
    return np.array([f[d] for d in days])


comp = lambda ts: (np.prod(fac(ts, SEP)) - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts):
    e = pk = 1.0; m = 0.0
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, e / pk - 1)
    return m * 100


chop = lambda ts: [t for t in ts if t.get("entry_regime") == "CHOP"]
P(f"9월 {len(SEP)}영업일 ({SEP[0]}~{SEP[-1]}), 0929 제외")
P("\n## 9월 결과표")
P("| 전략 | 거래 | 9월 복리 | PF | MDD | CHOP PF | 손실일 | ≥2%일 | Δ vs R0 |")
P("|---|---|---|---|---|---|---|---|---|")
for k, ts in S.items():
    dd = (fac(ts, SEP) - 1) * 100
    P(f"| {k} | {len(ts)} | {comp(ts):+.3f}% | {pf(ts):.3f} | {mdd(ts):.3f}% | {pf(chop(ts)):.3f} | "
      f"{(dd < 0).sum()} | {(dd >= 2).sum()} | {comp(ts) - comp(S['R0']):+.3f} |")


def table(days, title):
    R = {key(t): t for t in ALL["R0"] if t["date"] in days}
    V = {k: {key(t): t for t in ALL[k] if t["date"] in days} for k in ("G10", "G10_STRUCT")}
    cand = sorted(kk for kk, t in R.items() if t["exit_reason"] == "GX_MAXHOLD" and t.get("mh20_net") is not None
                  and -0.2 - 1e-12 <= t["mh20_net"] <= 0.2 + 1e-12)
    P(f"\n## {title}: grace 대상 후보(R0 20분 max-hold 청산 & 20분 net ±0.2%) {len(cand)}건")
    P("| 날짜 | 진입 | 방향 | 20분 net | MACD gap 확대 | ETF 추종 | R0 net | G10 net | G10 결과 | G10_STRUCT net | STRUCT 발동 | MFE(G10) | Δ G10 | Δ STRUCT |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for kk in cand:
        r = R[kk]; g = V["G10"].get(kk); s = V["G10_STRUCT"].get(kk)
        res = "-"
        if g:
            res = ("Y3 승격" if g.get("b3_y3") else ("+1% TP" if g["exit_reason"] == "GX_TP" else str(g["exit_reason"])[:14]))
        P(f"| {kk[0]} | {kk[1][11:16]} | {kk[2][:4]} | {r['mh20_net']:+.3f} | {'Y' if r['mh20_gap'] else 'N'} | "
          f"{'Y' if r['mh20_etf'] else 'N'} | {r['net_pct']:+.3f} | {g['net_pct']:+.3f} | {res} | {s['net_pct']:+.3f} | "
          f"{'Y' if s.get('grace_on') else 'N'} | {g['peak_net_pct']:+.3f} | {g['net_pct'] - r['net_pct']:+.3f} | "
          f"{s['net_pct'] - r['net_pct']:+.3f} |")
    return R, V


R, V = table(set(SEP), "9월")
P("\n## grace 요약 (9월, W1a 가중 %p)")
for k in ("G10", "G10_STRUCT"):
    ts = S[k]; K = {key(t): t for t in ts}
    gr = [t for t in ts if t.get("grace_on")]
    d = [(t, pnl(t) - pnl(R[key(t)])) for t in gr if key(t) in R]
    saved = [t for t, _ in d if t.get("b3_y3") or t["exit_reason"] == "GX_TP"]
    runner = [t for t, _ in d if t["peak_net_pct"] >= 3.0]
    gain = sum(x for _, x in d if x > 0); loss = sum(x for _, x in d if x < 0)
    other = [kk for kk in set(R) | set(K) if kk not in {key(t) for t in gr} and (
        kk not in R or kk not in K or abs(R[kk]["net_pct"] - K[kk]["net_pct"]) > 1e-9)]
    P(f"- {k}: grace 발동 {len(gr)}건 | 살아난 거래(유예 뒤 Y3 승격/+1% TP) {len(saved)}건 | "
      f"MFE≥3% runner 된 거래 {len(runner)}건 | 손해 {sum(1 for _, x in d if x < 0)}건 | "
      f"gain {gain:+.3f} | loss {loss:+.3f} | 순효과 {gain + loss:+.3f} | grace 밖 차이 {len(other)}건")

rng = np.random.default_rng(20260929); IDX = rng.integers(0, len(SEP), size=(20000, len(SEP)))
F0 = fac(S["R0"], SEP)
P("\n## 강건성 (9월, ⚠ 표본 작음)")
for k in ("G10", "G10_STRUCT"):
    Fk = fac(S[k], SEP)
    loo = [(np.prod(Fk) / Fk[i] - np.prod(F0) / F0[i]) * 100 for i in range(len(SEP))]
    bs = (np.prod(Fk[IDX], axis=1) - np.prod(F0[IDX], axis=1)) * 100
    P(f"- {k}: day-LOO 음수 {sum(x < 0 for x in loo)}/{len(SEP)} (min {min(loo):+.3f}) | "
      f"bootstrap P(Δ>0) {100 * (bs > 0).mean():.1f}% [95% {np.percentile(bs, 2.5):+.3f}, {np.percentile(bs, 97.5):+.3f}]")

table({OOS}, "0929 OOS 참고 (집계 제외)")
for k in ("R0", "G10", "G10_STRUCT"):
    ts = sorted([t for t in ALL[k] if t["date"] == OOS], key=lambda t: t["entry_time"])
    P(f"- {k} 0929: " + " / ".join(f"{hm(t['entry_time'])} {t['direction'][:4]} -> {hm(t['exit_time'])} "
                                  f"{t['exit_reason'][:16]} {t['net_pct']:+.3f}" for t in ts))
