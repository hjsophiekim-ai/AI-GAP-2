"""h_stats — Q2 + H50-active 연장(H30/H40/H60) vs Q2. 80일 + 09/28 OOS 분리."""
import bisect
import pickle
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0, "proj"); sys.path.insert(0, "proj/scripts")
L = lambda n: pickle.load(open(f"out_{n}.pkl", "rb"))["trades"]
D = list(pickle.load(open("_ctx80.pkl", "rb"))["dates"]); R30 = D[-30:]; SEP = [d for d in D if d[:6] == "202609"]
NAMES = ["R0", "Q2", "H30", "H40", "H60"]
RUNS = {k: L(k) for k in NAMES}
B = L("BASE")
pnl = lambda t: t["net_pct"] * t["w1a"]
key = lambda t: (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])
hm = lambda x: pd.Timestamp(x).strftime("%H:%M") if x else "-"
rows = sorted(((pd.Timestamp(t["exit_time"]), t["h50_held"], t["tp1_hit"]) for t in B), key=lambda r: r[0]); ex = [r[0] for r in rows]


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
VK = {k: {key(t): t for t in v} for k, v in RUNS.items()}

ext_all = {k: [t for t in RUNS[k] if t.get("b3_ext")] for k in ("H30", "H40", "H60")}
P(f"■ 표본: 80일 동안 'CHOP ∧ 20분 max-hold ∧ H50 active' 연장 발동 = "
  + ", ".join(f"{k} {len(v)}건" for k, v in ext_all.items()))

P("\n표 1 — 80영업일 (09/28 제외)")
P("%-4s %4s %9s %8s %8s %6s %7s %6s | %6s %8s | %4s %5s %6s %6s | %8s" % (
    "전략", "n", "80일", "최근30", "9월", "PF", "MDD", "WR", "CHOPPF", "CHOPavg", "손실일", "≥2%일", "일평균", "일중앙", "ΔvsQ2"))
q2c = comp(RUNS["Q2"], D)
for k, ts in RUNS.items():
    c = chop(ts); dd = (fac(ts) - 1) * 100
    P("%-4s %4d %9.3f %8.3f %8.3f %6.3f %7.3f %6.2f | %6.3f %+8.4f | %4d %5d %6.2f %6.2f | %+8.3f" % (
        k, len(ts), comp(ts, D), comp(ts, R30), comp(ts, SEP), pf(ts), mdd(ts), 100 * np.mean([pnl(t) > 0 for t in ts]),
        pf(c), np.mean([t["net_pct"] for t in c]), (dd < 0).sum(), (dd >= 2).sum(), dd.mean(), np.median(dd), comp(ts, D) - q2c))

P("\n§7 / 표 2 — 연장 거래 전량 (80일)")
BK = {key(t): t for t in B}
for k, ext in ext_all.items():
    tr = fp = 0; gain = loss = 0.0
    P(f"  [{k}] extension {len(ext)}건")
    for t in ext:
        q = VK["Q2"].get(key(t)); r0 = VK["R0"].get(key(t)); b = BK.get(key(t))
        d = pnl(t) - (pnl(q) if q else 0.0)
        is_fp = q is not None and t["net_pct"] < q["net_pct"]
        tr += int(d > 0 and t["peak_net_pct"] >= 3.0); fp += int(is_fp)
        if d > 0: gain += d
        else: loss += d
        dur = (pd.Timestamp(t["exit_time"]) - pd.Timestamp(t["entry_time"])).total_seconds() / 60
        P(f"     {t['date']} {hm(t['entry_time'])} {t['direction'][:4]} 20분 net {t.get('b3_ext_net20'):+.3f} H50 {hm(t.get('b3_ext_h50_at'))} | "
          f"MFE {t['peak_net_pct']:.2f} MAE {t['mae_net_pct']:+.2f} | R0 {r0['net_pct'] if r0 else float('nan'):+.3f} "
          f"Q2 {q['net_pct'] if q else float('nan'):+.3f} {k} {t['net_pct']:+.3f} {t['exit_reason']} {dur:.0f}분"
          f"{' (+1%→runner)' if t.get('b3_ext_prom') else ''} | BASE {b['net_pct'] if b else float('nan'):+.3f}"
          f"{'  FALSE' if is_fp else ''}")
    prec = 100 * (len(ext) - fp) / len(ext) if ext else float("nan")
    A = Bc = Cc = 0.0; ek = {key(t) for t in ext}
    for t in RUNS[k]:
        q = VK["Q2"].get(key(t))
        if q is None: continue
        d = pnl(t) - pnl(q)
        if key(t) in ek:
            if d > 0: Bc += d
            else: Cc += d
        else:
            A += d
    Dd = sum(pnl(t) for t in RUNS[k] if key(t) not in VK["Q2"]) - sum(pnl(q) for kk, q in VK["Q2"].items() if kk not in VK[k])
    P(f"     → true runner {tr} / false {fp} / precision {prec:.0f}% | rescue gain {gain:+.3f} false loss {loss:+.3f} 순 {gain+loss:+.3f}")
    P(f"     → 분해: A 같은거래 {A:+.3f} | B rescue {Bc:+.3f} | C false {Cc:+.3f} | 직접 {A+Bc+Cc:+.3f} | D 재배치 {Dd:+.3f}"
      + ("  ⚠ 재배치로만 개선" if (A + Bc + Cc) <= 0 < (A + Bc + Cc + Dd) else ""))

P("\n표 3 — CHOP runner 보존 (BASE CHOP, 보존 = net >= BASE−0.5)")
bc = chop(B)
for thr in (3.0, 5.0, 8.0):
    rs = [t for t in bc if t["peak_net_pct"] >= thr]
    if not rs: P(f"  MFE≥{thr:.0f}: 0건"); continue
    bp = sum(pnl(t) for t in rs); line = f"  MFE≥{thr:.0f} n={len(rs)} |"
    for k in NAMES:
        kept = sum(1 for t in rs if key(t) in VK[k] and VK[k][key(t)]["net_pct"] >= t["net_pct"] - 0.5)
        vp = sum(pnl(VK[k][key(t)]) for t in rs if key(t) in VK[k])
        line += f" {k} {kept}/{len(rs)} 이익 {100*vp/bp:.1f}% |"
    P(line)

P("\n§14 강건성 vs Q2 (bootstrap 20,000)")
rng = np.random.default_rng(20260928); IDX = rng.integers(0, len(D), size=(20000, len(D)))
F0 = fac(RUNS["Q2"])
for k in ("H30", "H40", "H60"):
    ts = RUNS[k]; Fk = fac(ts)
    loo = [(np.prod(Fk) / Fk[i] - np.prod(F0) / F0[i]) * 100 for i in range(len(D))]
    bs = (np.prod(Fk[IDX], axis=1) - np.prod(F0[IDX], axis=1)) * 100
    dd = {D[i]: (Fk[i] - F0[i]) * 100 for i in range(len(D)) if abs(Fk[i] - F0[i]) > 1e-12}
    P(f"  {k}: Δ80 {comp(ts,D)-q2c:+.3f} | LOO 음수 {sum(x<0 for x in loo)}/80 | "
      + " ".join(f"Top{n}x {excl(ts,n)-excl(RUNS['Q2'],n):+.2f}" for n in (1, 3, 5, 10))
      + f" | P(>0) {100*(bs>0).mean():.1f}% P(<0) {100*(bs<0).mean():.1f}% | 다른 날 {dd}")

# ── 09/28 OOS ──────────────────────────────────────────────────────────
P("\n표 4 — 09/28 OOS (82일 런, 80일 복리에 미포함)")
try:
    B82 = pickle.load(open("out_BASE_d82.pkl", "rb"))["trades"]
    bt = next(t for t in B82 if t["date"] == "20260928" and hm(t["entry_time"]) == "09:09")
    P(f"  BASE 09:09 인버스 @{bt['entry_price']:,.0f} → {hm(bt['exit_time'])} {bt['exit_reason']} net {bt['net_pct']:+.3f} "
      f"MFE {bt['peak_net_pct']:.2f} (peak {hm(bt.get('peak_at'))})")
    for k in NAMES:
        ts = pickle.load(open(f"out_{k}_d82.pkl", "rb"))["trades"]
        t = next((x for x in ts if key(x) == key(bt)), None)
        day = [x for x in ts if x["date"] == "20260928"]
        if t is None:
            P(f"  {k}: 같은 진입 없음"); continue
        dur = (pd.Timestamp(t["exit_time"]) - pd.Timestamp(t["entry_time"])).total_seconds() / 60
        P(f"  {k:4s}: net {t['net_pct']:+.3f} | {t['exit_reason']} {hm(t['exit_time'])} ({dur:.0f}분) | 연장 {t.get('b3_ext', False)} "
          f"20분net {t.get('b3_ext_net20')} +1%→runner {t.get('b3_ext_prom', False)} | 당일 {len(day)}거래 가중합 {sum(pnl(x) for x in day):+.3f}")
except FileNotFoundError as e:
    P("  (82일 결과 없음)", e)
