"""HIGH-VOL + EXIT-ONLY (3차) 9월 집계. READ-ONLY. H2=H1 (적용 사건 동일), H3=R0 (적용 0건) 은 사건 분류로 확인."""
import pickle, random, sys
from pathlib import Path
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj"))
R0 = pickle.load(open(HERE / "out_H30_d83.pkl", "rb"))["trades"]
HV = pickle.load(open(HERE / "out_HVB_d83.pkl", "rb"))["trades"]
SEP = [d for d in sorted({t["date"] for t in R0}) if d.startswith("202609")]
pnl = lambda t: float(t["net_pct"]) * float(t["w1a"])
key = lambda t: (t["date"], str(t["entry_time"]), t["direction"])
hm = lambda s: str(s)[11:16] if s else ""
sel = lambda ts: sorted((t for t in ts if t["date"] in SEP), key=lambda x: str(x["exit_time"]))
def comp(ts, ds=SEP):
    e = 1.0
    for t in sorted((t for t in ts if t["date"] in ds), key=lambda x: str(x["exit_time"])): e *= 1 + pnl(t) / 100
    return (e - 1) * 100
def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0); return g / l
def mdd(ts):
    e = pk = 1.0; m = 0.0
    for t in sel(ts): e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, (e / pk - 1) * 100)
    return m
def daily(ts):
    d = {x: 0.0 for x in SEP}
    for t in sel(ts): d[t["date"]] += pnl(t)
    return d
def row(n, ts, applied, avoided, missed, net):
    s = sel(ts); dd = daily(ts); ch = [t for t in s if t.get("entry_regime") == "CHOP"]
    print(f"| {n} | {comp(ts):+.3f} | {comp(ts) - comp(R0):+.3f} | {pf(s):.3f} | {mdd(ts):.2f} | {pf(ch):.3f} | "
          f"{sum(v < 0 for v in dd.values())} | {sum(v >= 2 for v in dd.values())} | {len(s)} | {applied} | {avoided} | {missed} | {net} |")
ex = [(t, o) for t in sel(HV) for o in t.get("opp", []) if o.get("exo") == "EXIT_ONLY"]
r0 = {t["date"]: t for t in []}
A = {key(t): t for t in sel(R0)}; B = {key(t): t for t in sel(HV)}
diff = [(k, A.get(k), B.get(k), (pnl(B[k]) if k in B else 0) - (pnl(A[k]) if k in A else 0)) for k in sorted(set(A) | set(B))]
diff = [x for x in diff if abs(x[3]) > 1e-9]
rev = {k: t for k, t in A.items()}
avoided = sum(1 for k, a, b, d in diff if b is None and a["exit_reason"] in ("GX_SL", "TIME_WINDOW_STOP_LOSS"))
missed = sum(1 for k, a, b, d in diff if b is None and pnl(a) > 0)
print("## HIGH-VOL + EXIT-ONLY — 9월 17영업일\n")
print(f"적용 사건 (HVB 실행에서 EXIT_ONLY 발동): {[(t['date'][4:], hm(o['at']), o['kind'], round(o['range_pct'], 2)) for t, o in ex]}\n")
print("| 전략 | 9월 복리 | Δ | PF | MDD | CHOP PF | 손실일 | 2%+일 | 거래 | 적용 | 피한 손절 | 놓친 좋은 진입 | 순효과(가산) |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
row("R0", R0, "-", "-", "-", "-")
net = sum(x[3] for x in diff)
row("H1+B", HV, len(ex), avoided, missed, f"{net:+.3f}")
row("H2+B (=H1 사건 동일)", HV, len(ex), avoided, missed, f"{net:+.3f}")
row("H3+B (적용 0건 = R0)", R0, 0, 0, 0, "+0.000")
print("\n### 바뀐 거래")
for k, a, b, d in diff:
    print(f"- {k[0][4:]} {hm(k[1])} {k[2][:4]}: R0 {'없음' if a is None else f'{hm(a['exit_time'])} {a['exit_reason']} {float(a['net_pct']):+.3f}'} → "
          f"{'없음(진입 취소)' if b is None else f'{hm(b['exit_time'])} {b['exit_reason']} {float(b['net_pct']):+.3f}'} (Δ {d:+.3f})")
d0, d1 = daily(R0), daily(HV); dd = {x: d1[x] - d0[x] for x in SEP}
loo = [comp(HV, [x for x in SEP if x != y]) - comp(R0, [x for x in SEP if x != y]) for y in SEP]
top = sorted(SEP, key=lambda x: dd[x], reverse=True)
rng = random.Random(20260930); v = list(dd.values())
boot = np.array([sum(rng.choice(v) for _ in v) for _ in range(20000)])
big = max(diff, key=lambda x: abs(x[3]))
print(f"\n### 강건성: 바뀐 날 {sum(abs(x) > 1e-9 for x in v)} · day-LOO 음수 {sum(x < 0 for x in loo)}/17 (범위 {min(loo):+.2f}~{max(loo):+.2f}) · "
      f"Top1일 제외 Δ {comp(HV, [x for x in SEP if x != top[0]]) - comp(R0, [x for x in SEP if x != top[0]]):+.3f} · "
      f"P(Δ>0) {(boot > 0).mean() * 100:.1f}% · 최대 단일거래 {big[0][0][4:]} {hm(big[0][1])} {big[3]:+.3f} ({abs(big[3]) / abs(net) * 100:.0f}%)")
