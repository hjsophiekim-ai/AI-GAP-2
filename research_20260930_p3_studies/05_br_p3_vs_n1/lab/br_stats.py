"""B(EXIT-ONLY) + B진입 손절 후 1회 재진입 — P3 vs N1 적용 비교, 최근 80영업일. READ-ONLY."""
import pickle, random, sys
from pathlib import Path
import numpy as np
import pandas as pd
TS = lambda x: pd.Timestamp(x).strftime("%Y%m%d%H%M")
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj"))
NAMES = {"R0": "H30", "P3-BR": "P3BR", "N1-BR": "N1BR", "P3-BR+N1-BR": "BOTHBR"}
T = {k: pickle.load(open(HERE / f"out_{v}_d83.pkl", "rb"))["trades"] for k, v in NAMES.items()}
anc = pickle.load(open(HERE / "prev" / "out_H30_d83_anchor.pkl", "rb"))["trades"]
ALL = sorted({t["date"] for t in T["R0"]})
CTXD = list(pickle.load(open(HERE / "_ctx83.pkl", "rb"))["dates"])
W = CTXD[-80:]
pnl = lambda t: float(t["net_pct"]) * float(t["w1a"])
key = lambda t: (t["date"], str(t["entry_time"]), t["direction"])
hm = lambda s: str(s)[11:16] if s else ""
p = lambda *a: print(*a, flush=True)
sel = lambda ts, ds=W: sorted((t for t in ts if t["date"] in set(ds)), key=lambda x: str(x["exit_time"]))
def comp(ts, ds=W):
    e = 1.0
    for t in sel(ts, ds): e *= 1 + pnl(t) / 100
    return (e - 1) * 100
def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0); return g / l if l else float("inf")
def mdd(ts):
    e = pk = 1.0; m = 0.0
    for t in sel(ts): e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, (e / pk - 1) * 100)
    return m
def daily(ts):
    d = {x: 0.0 for x in W}
    for t in sel(ts): d[t["date"]] += pnl(t)
    return d
sig = lambda ts: sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]), round(float(t["net_pct"]), 8)) for t in ts)


def classify(k):
    A = {key(t): t for t in sel(T["R0"])}; B = {key(t): t for t in sel(T[k])}
    bex = {TS(t["exit_time"]) for t in sel(T[k]) if t.get("exo_exit")}
    out = []
    for kk in sorted(set(A) | set(B)):
        a, b = A.get(kk), B.get(kk)
        d = (pnl(b) if b else 0.0) - (pnl(a) if a else 0.0)
        if abs(d) < 1e-9:
            continue
        if b is not None and b.get("reentry"):
            c = "재진입"
        elif b is not None and b.get("exo_entry"):
            c = "B 지연진입"
        elif b is None and a is not None and TS(a["entry_time"]) in bex:
            c = "B 취소"
        else:
            c = "슬롯/경로 밀림"
        out.append((c, kk, a, b, d))
    return out


p("# B(EXIT-ONLY) + 1회 재진입 — P3 vs N1 적용 비교 (최근 80영업일)\n")
p("READ-ONLY. production/config/UI 무수정. 재진입은 **B 로 들어간 반대 ETF 가 손절된 경우에만**(요청 흐름 그대로), 수량 50%, 하루 1회.\n")
p(f"- 창: {W[0]}~{W[-1]} ({len(W)}영업일, 83일 런에서 최근 80일; 엔진은 일 단위 독립)")
p(f"- R0 재현: 83일 앵커 시그니처 일치 **{sig(T['R0']) == sig(anc)}** ({len(T['R0'])}거래, 83일 {comp(T['R0'], ALL):.4f})\n")
p("## 1. 80일 성과")
p("| 전략 | 복리 | Δ | PF | MDD | WR | 손실일 | 2%+일 | 거래 | B 발동 | B 지연진입 | 재진입 | 재진입 성공/실패 | 재진입 gain / loss | 슬롯 밀림 |")
p("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
C = {}
for k, ts in T.items():
    s = sel(ts); dd = daily(ts)
    C[k] = classify(k) if k != "R0" else []
    re = [t for t in s if t.get("reentry")]
    slot = sum(x[4] for x in C[k] if x[0] == "슬롯/경로 밀림")
    p(f"| {k} | {comp(ts):+.3f} | {comp(ts) - comp(T['R0']):+.3f} | {pf(s):.3f} | {mdd(ts):.2f} | {100 * sum(pnl(t) > 0 for t in s) / len(s):.1f}% | "
      f"{sum(v < 0 for v in dd.values())} | {sum(v >= 2 for v in dd.values())} | {len(s)} | {sum(bool(t.get('exo_exit')) for t in s)} | "
      f"{sum(bool(t.get('exo_entry')) for t in s)} | {len(re)} | {sum(pnl(t) > 0 for t in re)}/{sum(pnl(t) <= 0 for t in re)} | "
      f"{sum(pnl(t) for t in re if pnl(t) > 0):+.3f} / {sum(pnl(t) for t in re if pnl(t) <= 0):+.3f} | {slot:+.3f} |")
p("")
p("## 2. 효과 분해 (R0 대비 거래 단위 가산, W1a 가중 %)")
for k in list(NAMES)[1:]:
    grp = {}
    for c, *_r, d in C[k]:
        grp.setdefault(c, []).append(d)
    p(f"- **{k}**: " + " · ".join(f"{c} {len(v)}건 {sum(v):+.3f}" for c, v in grp.items()) + f" · 합 {sum(x[4] for x in C[k]):+.3f}")
p("")
p("## 3. 바뀐 거래 전부")
for k in list(NAMES)[1:]:
    p(f"### {k}")
    for c, kk, a, b, d in C[k]:
        da = "없음" if a is None else f"{hm(a['exit_time'])} {a['exit_reason'][:20]} {float(a['net_pct']):+.3f}×{float(a['w1a']):.2f}"
        db = "없음" if b is None else f"{hm(b['exit_time'])} {b['exit_reason'][:20]} {float(b['net_pct']):+.3f}×{float(b['w1a']):.2f}"
        p(f"- [{c}] {kk[0][4:]} {hm(kk[1])} {kk[2][:4]}: R0 {da} → {db} (Δ {d:+.3f})")
    p(f"- B 발동 상세: " + "; ".join(f"{t['date'][4:]} {hm(t['exit_time'])} {t['direction'][:4]}→{t.get('exo_result')}" for t in sel(T[k]) if t.get("exo_exit")))
p("")
p("## 4. 강건성 (80일, R0 대비) — ⚠ 바뀐 날 수를 함께 볼 것")
p("| 전략 | Δ | 바뀐 날 | day-LOO 음수 | LOO 범위 | Top1 제외 Δ | Top3 제외 Δ | P(Δ>0) | 최대 단일거래 기여 |")
p("|---|---|---|---|---|---|---|---|---|")
D0 = daily(T["R0"])
for k in list(NAMES)[1:]:
    d1 = daily(T[k]); dd = {x: d1[x] - D0[x] for x in W}
    loo = [comp(T[k], [x for x in W if x != y]) - comp(T["R0"], [x for x in W if x != y]) for y in W]
    top = sorted(W, key=lambda x: dd[x], reverse=True)
    ex = lambda n: comp(T[k], [x for x in W if x not in top[:n]]) - comp(T["R0"], [x for x in W if x not in top[:n]])
    rng = random.Random(20260930); v = list(dd.values())
    boot = np.array([sum(rng.choice(v) for _ in v) for _ in range(20000)])
    tot = sum(x[4] for x in C[k]); bb = max(C[k], key=lambda x: abs(x[4])) if C[k] else None
    p(f"| {k} | {comp(T[k]) - comp(T['R0']):+.3f} | {sum(abs(x) > 1e-9 for x in v)} | {sum(x < 0 for x in loo)}/{len(W)} | "
      f"[{min(loo):+.2f}, {max(loo):+.2f}] | {ex(1):+.3f} | {ex(3):+.3f} | {(boot > 0).mean() * 100:.1f}% | "
      f"{'' if bb is None else f'{bb[1][0][4:]} {hm(bb[1][1])} [{bb[0]}] {bb[4]:+.2f} ({(abs(bb[4]) / abs(tot) * 100) if tot else 0:.0f}%)'} |")
dP = comp(T["P3-BR"]) - comp(T["R0"]); dN = comp(T["N1-BR"]) - comp(T["R0"]); dB = comp(T["P3-BR+N1-BR"]) - comp(T["R0"])
p(f"\n## 5. 상호작용: 결합 Δ {dB:+.3f} vs 단일 합 {dP + dN:+.3f} → {dB - dP - dN:+.3f}")
