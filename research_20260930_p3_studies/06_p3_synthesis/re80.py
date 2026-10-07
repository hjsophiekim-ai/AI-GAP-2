"""종합 연구: 손절 후 동일방향 1회 재진입(RE50/70/100) 80영업일 강건성·장세별·기간분할. READ-ONLY."""
import pickle, random, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
RL = Path(sys.argv[1]); sys.path.insert(0, str(RL / "proj"))
NAMES = {"R0": "H30", "R50": "RE50", "R70": "RE70", "R100": "RE100"}
T = {k: pickle.load(open(RL / f"out_{v}_d83.pkl", "rb"))["trades"] for k, v in NAMES.items()}
W = list(pickle.load(open(RL / "_ctx83.pkl", "rb"))["dates"])[-80:]
pnl = lambda t: float(t["net_pct"]) * float(t["w1a"])
key = lambda t: (t["date"], str(t["entry_time"]), t["direction"])
hm = lambda s: pd.Timestamp(s).strftime("%H:%M") if s else ""
p = lambda *a: print(*a, flush=True)
sel = lambda ts, ds=W: sorted((t for t in ts if t["date"] in set(ds)), key=lambda x: str(x["exit_time"]))
def comp(ts, ds=W):
    e = 1.0
    for t in sel(ts, ds): e *= 1 + pnl(t) / 100
    return (e - 1) * 100
def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0); return g / l
def mdd(ts, ds=W):
    e = pk = 1.0; m = 0.0
    for t in sel(ts, ds): e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, (e / pk - 1) * 100)
    return m
def daily(ts, ds=W):
    d = {x: 0.0 for x in ds}
    for t in sel(ts, ds): d[t["date"]] += pnl(t)
    return d
D0 = daily(T["R0"])
p(f"# 손절 후 동일방향 1회 재진입 — 80영업일 {W[0]}~{W[-1]}\n")
p("| 후보 | 복리 | Δ | PF | MDD | 손실일 | 최악일 | 2%+일 | 거래 | 재진입 | 성공/실패 |")
p("|---|---|---|---|---|---|---|---|---|---|---|")
for k, ts in T.items():
    s = sel(ts); dd = daily(ts); re = [t for t in s if t.get("reentry")]
    p(f"| {k} | {comp(ts):+.2f} | {comp(ts) - comp(T['R0']):+.2f} | {pf(s):.3f} | {mdd(ts):.2f} | {sum(v < 0 for v in dd.values())} | "
      f"{min(dd.values()):+.2f} | {sum(v >= 2 for v in dd.values())} | {len(s)} | {len(re)} | {sum(pnl(t) > 0 for t in re)}/{sum(pnl(t) <= 0 for t in re)} |")
p("")
for k in ("R50", "R70", "R100"):
    A = {key(t): t for t in sel(T["R0"])}; B = {key(t): t for t in sel(T[k])}
    df = []
    for kk in sorted(set(A) | set(B)):
        a, b = A.get(kk), B.get(kk); d = (pnl(b) if b else 0) - (pnl(a) if a else 0)
        if abs(d) > 1e-9: df.append(("RE" if b is not None and b.get("reentry") else "SLOT", kk, a, b, d))
    re = [x for x in df if x[0] == "RE"]; sl = [x for x in df if x[0] == "SLOT"]
    d1 = daily(T[k]); dd = {x: d1[x] - D0[x] for x in W}
    loo = [comp(T[k], [x for x in W if x != y]) - comp(T["R0"], [x for x in W if x != y]) for y in W]
    top = sorted(W, key=lambda x: dd[x], reverse=True)
    ex = lambda n: comp(T[k], [x for x in W if x not in top[:n]]) - comp(T["R0"], [x for x in W if x not in top[:n]])
    rng = random.Random(20260930); v = list(dd.values()); boot = np.array([sum(rng.choice(v) for _ in v) for _ in range(20000)])
    tot = sum(x[4] for x in df); bb = max(df, key=lambda x: abs(x[4]))
    h1, h2 = W[:40], W[40:]
    p(f"## {k}")
    p(f"- 분해(가산): 재진입 {len(re)}건 {sum(x[4] for x in re):+.2f} (gain {sum(x[4] for x in re if x[4] > 0):+.2f} / loss {sum(x[4] for x in re if x[4] < 0):+.2f}) · 슬롯/경로 {len(sl)}건 {sum(x[4] for x in sl):+.2f}")
    p(f"- 강건성: 바뀐 날 {sum(abs(x) > 1e-9 for x in v)} · day-LOO 음수 {sum(x < 0 for x in loo)}/80 (최소 {min(loo):+.2f}) · Top1 제외 {ex(1):+.2f} · Top3 제외 {ex(3):+.2f} · Top5 제외 {ex(5):+.2f} · "
      f"P(Δ>0) {(boot > 0).mean() * 100:.1f}% · 최대 단일 {bb[1][0][4:]} {hm(bb[1][1])} [{bb[0]}] {bb[4]:+.2f} ({abs(bb[4]) / tot * 100:.0f}%)")
    p(f"- 기간 분할: 전반40일({h1[0]}~{h1[-1]}) Δ {comp(T[k], h1) - comp(T['R0'], h1):+.2f} / 후반40일({h2[0]}~{h2[-1]}) Δ {comp(T[k], h2) - comp(T['R0'], h2):+.2f} · "
      f"MDD 전반 {mdd(T['R0'], h1):.2f}→{mdd(T[k], h1):.2f} 후반 {mdd(T['R0'], h2):.2f}→{mdd(T[k], h2):.2f}")
    byreg = {}
    for x in re:
        b = x[3]; g = str(b.get("entry_regime")); byreg.setdefault(g, []).append(x[4])
    p("- 재진입 regime별: " + " · ".join(f"{g} {len(vv)}건 {sum(vv):+.2f} (승 {sum(z > 0 for z in vv)})" for g, vv in byreg.items()))
    gap = [((pd.Timestamp(x[3]["entry_time"]) - pd.Timestamp(x[3]["reent_sl_at"])).total_seconds() / 60, x[4]) for x in re]
    near = [z for z in gap if z[0] <= 30]; far = [z for z in gap if z[0] > 30]
    p(f"- 손절→재진입 간격: ≤30분 {len(near)}건 {sum(z[1] for z in near):+.2f} (승 {sum(z[1] > 0 for z in near)}) / >30분 {len(far)}건 {sum(z[1] for z in far):+.2f} (승 {sum(z[1] > 0 for z in far)})  [진단용, 새 파라미터 아님]")
    if k == "R50":
        p("\n| 날짜 | 원 진입 | 방향 | 손절 | 손절 net | 재진입 | 간격(분) | regime | 청산 | 재진입 net | ×W1a | Δ |")
        p("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for x in re:
            b = x[3]
            p(f"| {b['date'][4:]} | {hm(b['reent_orig_entry'])} | {b['direction'][:4]} | {hm(b['reent_sl_at'])} | {b['reent_sl_net']:+.2f} | {hm(b['entry_time'])} | "
              f"{(pd.Timestamp(b['entry_time']) - pd.Timestamp(b['reent_sl_at'])).total_seconds() / 60:.0f} | {b.get('entry_regime')} | {b['exit_reason'][:22]} | {float(b['net_pct']):+.3f} | {float(b['w1a']):.2f} | {x[4]:+.3f} |")
        p("\n슬롯/경로 변화:")
        for x in sl:
            a, b = x[2], x[3]
            p(f"- {x[1][0][4:]} {hm(x[1][1])} {x[1][2][:4]}: R0 {'없음' if a is None else a['exit_reason'][:20] + f' {float(a[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]):+.3f}'} → {'없음' if b is None else b['exit_reason'][:20] + f' {float(b[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]):+.3f}'} (Δ {x[4]:+.3f})")
    p("")
