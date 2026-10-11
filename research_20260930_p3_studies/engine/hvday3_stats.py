"""HIGH-VOL DAY MODE (8차) 집계: 최근 80영업일 + 9월. READ-ONLY."""
import pickle, random, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE / "proj"))
NAMES = {"R0": "H30", "D3b 보호": "BP", "D3b 지연": "BD", "D3b 둘다": "BPD", "D3a 보호": "AP", "D3a 지연": "AD", "D3a 둘다": "APD"}
T = {k: pickle.load(open(HERE / f"out_{v}_d83.pkl", "rb"))["trades"] for k, v in NAMES.items()}
anc = pickle.load(open(HERE / "prev" / "out_H30_d83_anchor.pkl", "rb"))["trades"]
HVON = pickle.load(open(HERE / "hv_on3b.pkl", "rb")); HVA = pickle.load(open(HERE / "hv_on3a.pkl", "rb"))
CD = list(pickle.load(open(HERE / "_ctx83.pkl", "rb"))["dates"]); W80 = CD[-80:]; SEP = [d for d in CD if d.startswith("202609")]
pnl = lambda t: float(t["net_pct"]) * float(t["w1a"]); key = lambda t: (t["date"], str(t["entry_time"]), t["direction"])
hm = lambda s: pd.Timestamp(s).strftime("%H:%M") if s else ""
p = lambda *a: print(*a, flush=True)
sel = lambda ts, ds: sorted((t for t in ts if t["date"] in set(ds)), key=lambda x: str(x["exit_time"]))
def comp(ts, ds):
    e = 1.0
    for t in sel(ts, ds): e *= 1 + pnl(t) / 100
    return (e - 1) * 100
def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0); return g / l
def mdd(ts, ds):
    e = pk = 1.0; m = 0.0
    for t in sel(ts, ds): e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, (e / pk - 1) * 100)
    return m
def daily(ts, ds):
    d = {x: 0.0 for x in ds}
    for t in sel(ts, ds): d[t["date"]] += pnl(t)
    return d
sig = lambda ts: sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]), round(float(t["net_pct"]), 8)) for t in ts)
p("# HIGH-VOL DAY MODE — 최근 80영업일 + 9월\n")
p(f"- R0 재현: 83일 앵커 일치 **{sig(T['R0']) == sig(anc)}**")
p(f"- 탐지기 D1(하이닉스 1분 |수익률| 09:00~t 누적평균 > 직전 20일 같은 시각 80백분위, 09:15~): 80일 중 {sum(d in HVON for d in W80)}일 ON, 9월 {sum(d in HVON for d in SEP)}일 ON {[d[4:] for d in SEP if d in HVON]}, 0930 OFF")
p("- 참고 D2(H50 60분 range>2.35%): 80일 중 78일 ON → 탐지기로 무의미\n")
for tag, ds in (("80영업일", W80), ("9월", SEP)):
    p(f"## {tag} ({ds[0]}~{ds[-1]}, {len(ds)}일)")
    p("| 전략 | 복리 | Δ | PF | MDD | WR | CHOP PF | 손실일 | 2%+일 | 거래 | HV 보호 청산 | 반대진입 지연 | 지연 후 진입/취소 |")
    p("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k, ts in T.items():
        s = sel(ts, ds); dd = daily(ts, ds); ch = [t for t in s if t.get("entry_regime") == "CHOP"]
        ex = [t for t in s if t.get("exo_exit")]
        p(f"| {k} | {comp(ts, ds):+.2f} | {comp(ts, ds) - comp(T['R0'], ds):+.2f} | {pf(s):.3f} | {mdd(ts, ds):.2f} | {100 * sum(pnl(t) > 0 for t in s) / len(s):.1f}% | "
          f"{pf(ch):.3f} | {sum(v < 0 for v in dd.values())} | {sum(v >= 2 for v in dd.values())} | {len(s)} | {sum(t['exit_reason'] == 'HV_PROTECT' for t in s)} | "
          f"{len(ex)} | {sum(t.get('exo_result') == 'ENTERED' for t in ex)}/{sum(t.get('exo_result') == 'CANCELLED' for t in ex)} |")
    D0 = daily(T["R0"], ds)
    p("\n| 전략 | Δ | 바뀐 날 | day-LOO 음수 | Top1 제외 | Top3 제외 | P(Δ>0) | 최대 단일 | HV일 Δ합 | 비HV일 Δ합 |")
    p("|---|---|---|---|---|---|---|---|---|---|")
    for k in [x for x in NAMES if x != "R0"]:
        d1 = daily(T[k], ds); dd = {x: d1[x] - D0[x] for x in ds}
        loo = [comp(T[k], [x for x in ds if x != y]) - comp(T["R0"], [x for x in ds if x != y]) for y in ds]
        top = sorted(ds, key=lambda x: dd[x], reverse=True)
        ex_ = lambda n: comp(T[k], [x for x in ds if x not in top[:n]]) - comp(T["R0"], [x for x in ds if x not in top[:n]])
        rng = random.Random(20260930); v = list(dd.values()); boot = np.array([sum(rng.choice(v) for _ in v) for _ in range(20000)])
        A = {key(t): t for t in sel(T["R0"], ds)}; B = {key(t): t for t in sel(T[k], ds)}
        df = [((pnl(B[kk]) if kk in B else 0) - (pnl(A[kk]) if kk in A else 0), kk) for kk in set(A) | set(B)]
        df = [x for x in df if abs(x[0]) > 1e-9]; tot = sum(x[0] for x in df); bb = max(df, key=lambda x: abs(x[0])) if df else (0, ("", "", ""))
        p(f"| {k} | {comp(T[k], ds) - comp(T['R0'], ds):+.2f} | {sum(abs(x) > 1e-9 for x in v)} | {sum(x < 0 for x in loo)}/{len(ds)} | {ex_(1):+.2f} | {ex_(3):+.2f} | "
          f"{(boot > 0).mean() * 100:.1f}% | {bb[1][0][4:]} {hm(bb[1][1])} {bb[0]:+.2f} ({abs(bb[0]) / abs(tot) * 100 if tot else 0:.0f}%) | "
          f"{sum(dd[x] for x in ds if x in (HVON if k.startswith('D3b') else HVA)):+.2f} | {sum(dd[x] for x in ds if x not in (HVON if k.startswith('D3b') else HVA)):+.2f} |")
    p("")
p("## 80일 바뀐 거래 (D3b 보호 / D3b 지연)")
for VK in ("D3b 보호", "D3b 지연"):
  p(f"### {VK}")
  A = {key(t): t for t in sel(T["R0"], W80)}; B = {key(t): t for t in sel(T[VK], W80)}
  for kk in sorted(set(A) | set(B)):
    a, b = A.get(kk), B.get(kk); d = (pnl(b) if b else 0) - (pnl(a) if a else 0)
    if abs(d) > 1e-9:
        f = lambda t: "없음" if t is None else f"{hm(t['exit_time'])} {t['exit_reason'][:22]} {float(t['net_pct']):+.3f}×{float(t['w1a']):.2f} MFE {float(t['peak_net_pct']):+.2f}"
        p(f"  - {kk[0][4:]} {hm(kk[1])} {kk[2][:4]}: R0 {f(a)} → {f(b)} (Δ {d:+.3f})")
