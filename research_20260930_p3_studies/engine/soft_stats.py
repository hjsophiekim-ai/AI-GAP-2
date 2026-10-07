"""B3 SOFT STOP (7차) 9월 집계. READ-ONLY."""
import pickle, random, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj"))
T = {"R0": pickle.load(open(HERE / "out_H30_d83.pkl", "rb"))["trades"], "S1": pickle.load(open(HERE / "out_SOFT1_d83.pkl", "rb"))["trades"]}
anc = pickle.load(open(HERE / "prev" / "out_H30_d83_anchor.pkl", "rb"))["trades"]
SEP = [d for d in list(pickle.load(open(HERE / "_ctx83.pkl", "rb"))["dates"]) if d.startswith("202609")]
pnl = lambda t: float(t["net_pct"]) * float(t["w1a"])
key = lambda t: (t["date"], str(t["entry_time"]), t["direction"])
hm = lambda s: pd.Timestamp(s).strftime("%H:%M:%S") if s else ""
p = lambda *a: print(*a, flush=True)
sel = lambda ts, ds=SEP: sorted((t for t in ts if t["date"] in set(ds)), key=lambda x: str(x["exit_time"]))
def comp(ts, ds=SEP):
    e = 1.0
    for t in sel(ts, ds): e *= 1 + pnl(t) / 100
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
sig = lambda ts: sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]), round(float(t["net_pct"]), 8)) for t in ts)
p("# B3 SOFT STOP (S1) — 2026년 9월\n")
p("READ-ONLY. production/config/UI 무수정. 훅 = lab/patch_soft.py. 적용 = CHOP/B3(승격 전) 포지션만.\n")
p(f"- R0 재현: 83일 앵커 시그니처 일치 **{sig(T['R0']) == sig(anc)}** · 9월 16일(0903~0928) {comp(T['R0'], [d for d in SEP if d <= '20260928']):+.3f} (앵커 +19.755) · 집계 9월 {len(SEP)}영업일\n")
S = {}
p("| 전략 | 9월 복리 | Δ | PF | MDD | CHOP PF | 손실일 | 2%+일 | 거래 | soft 발동 | 회복 | hard 손절 | 봉 판정 손절 |")
p("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for k, ts in T.items():
    s = sel(ts); dd = daily(ts); ch = [t for t in s if t.get("entry_regime") == "CHOP"]
    so = [t for t in s if t.get("soft_result") in ("RECOVER", "BAR_SL", "HARD")]
    p(f"| {k} | {comp(ts):+.3f} | {comp(ts) - comp(T['R0']):+.3f} | {pf(s):.3f} | {mdd(ts):.2f} | {pf(ch):.3f} | {sum(v < 0 for v in dd.values())} | "
      f"{sum(v >= 2 for v in dd.values())} | {len(s)} | {len(so)} | {sum(t['soft_result'] == 'RECOVER' for t in so)} | "
      f"{sum(t['soft_result'] == 'HARD' for t in so)} | {sum(t['soft_result'] == 'BAR_SL' for t in so)} |")
p("")
# -1% 도달 거래 전부 (S1 기록 기준)
A = {key(t): t for t in sel(T["R0"])}; B = {key(t): t for t in sel(T["S1"])}
p("## -1% 도달한 B3 거래 전부")
p("| 날짜 | 진입 | 방향 | -1% 도달 | 직전봉 N1 추세 | MACD gap 반대확대 | 유예 | 봉 판정 추세/반대확대 | R0 청산 net | S1 청산 net | S1 MFE | Δ(가중) |")
p("|---|---|---|---|---|---|---|---|---|---|---|---|")
tri = [t for t in B.values() if t.get("soft_at")]
for t in sorted(tri, key=key):
    a = A.get(key(t)); d = pnl(t) - (pnl(a) if a else 0)
    bar = "" if t.get("soft_bar_trend") is None else f"{'유지' if t['soft_bar_trend'] else '깨짐'}/{'예' if t['soft_bar_against'] else '아니오'}"
    p(f"| {t['date'][4:]} | {hm(t['entry_time'])[:5]} | {t['direction'][:4]} | {hm(t['soft_at'])} | {'유지' if t['soft_trend'] else '깨짐'} | "
      f"{'예' if t['soft_against'] else '아니오'} | {t.get('soft_result') or '-'} | {bar or '-'} | "
      f"{'' if a is None else f'{a[chr(101)+chr(120)+chr(105)+chr(116)+chr(95)+chr(114)+chr(101)+chr(97)+chr(115)+chr(111)+chr(110)]} {float(a[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]):+.3f}'} | "
      f"{t['exit_reason']} {float(t['net_pct']):+.3f} | {float(t['peak_net_pct']):+.2f} | {d:+.3f} |")
# 바뀐 거래 전부 (슬롯 영향 포함)
df = []
for kk in sorted(set(A) | set(B)):
    a, b = A.get(kk), B.get(kk); d = (pnl(b) if b else 0) - (pnl(a) if a else 0)
    if abs(d) > 1e-9: df.append((kk, a, b, d))
p(f"\n바뀐 거래 {len(df)}건: 손실 축소 {sum(x[3] for x in df if x[3] > 0):+.3f} · 손실 확대 {sum(x[3] for x in df if x[3] < 0):+.3f} · 순효과 {sum(x[3] for x in df):+.3f}")
for kk, a, b, d in df:
    p(f"- {kk[0][4:]} {hm(kk[1])[:5]} {kk[2][:4]}: R0 {'없음' if a is None else a['exit_reason'] + f' {float(a[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]):+.3f}'} → "
      f"{'없음' if b is None else b['exit_reason'] + f' {float(b[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]):+.3f}'} (Δ {d:+.3f})")
# Q1: R0 B3 손절 후 30/60분 같은 방향 +2%
p("\n## R0 B3 −1% 손절(GX_SL) → 이후 같은 방향")
cnt = 0
for t in sel(T["R0"]):
    if t["exit_reason"] != "GX_SL":
        continue
    f = pd.read_csv(HERE / "cache83" / f"replay_{t['date']}_{'long' if t['direction'] == 'UP_RED' else 'inverse'}_1m.csv", parse_dates=["datetime"]).set_index("datetime")
    a0 = pd.Timestamp(t["exit_time"]).tz_localize(None); px = float(t["exit_price"])
    w30 = f.loc[(f.index > a0) & (f.index <= a0 + pd.Timedelta(minutes=30)), "high"]
    w60 = f.loc[(f.index > a0) & (f.index <= a0 + pd.Timedelta(minutes=60)), "high"]
    u30 = (w30.max() / px - 1) * 100; u60 = (w60.max() / px - 1) * 100
    hit = u60 >= 2.0; cnt += hit
    s1 = B.get(key(t))
    p(f"- {t['date'][4:]} {hm(t['entry_time'])[:5]} {t['direction'][:4]} 손절 {hm(t['exit_time'])[:5]} {float(t['net_pct']):+.2f} → 30분 +{u30:.2f}% / 60분 +{u60:.2f}%"
      f"{' ← +2% 이상' if hit else ''} | S1: {'없음' if s1 is None else s1['exit_reason'] + f' {float(s1[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]):+.3f}'} ({(s1 or {}).get('soft_result') or '-'})")
p(f"→ +2% 이상 {cnt}건")
# 강건성
D0, D1 = daily(T["R0"]), daily(T["S1"]); dd = {x: D1[x] - D0[x] for x in SEP}
loo = [comp(T["S1"], [x for x in SEP if x != y]) - comp(T["R0"], [x for x in SEP if x != y]) for y in SEP]
top = sorted(SEP, key=lambda x: dd[x], reverse=True)
ex = lambda n: comp(T["S1"], [x for x in SEP if x not in top[:n]]) - comp(T["R0"], [x for x in SEP if x not in top[:n]])
rng = random.Random(20260930); v = list(dd.values()); boot = np.array([sum(rng.choice(v) for _ in v) for _ in range(20000)])
tot = sum(x[3] for x in df); bb = max(df, key=lambda x: abs(x[3])) if df else None
p(f"\n## 강건성 — ⚠ 표본 작음\n- Δ {comp(T['S1']) - comp(T['R0']):+.3f} · 바뀐 날 {sum(abs(x) > 1e-9 for x in v)} · day-LOO 음수 {sum(x < 0 for x in loo)}/{len(SEP)} (범위 {min(loo):+.2f}~{max(loo):+.2f}) · "
  f"Top1 제외 {ex(1):+.3f} · Top3 제외 {ex(3):+.3f} · P(Δ>0) {(boot > 0).mean() * 100:.1f}% · "
  f"최대 단일 {'' if bb is None else f'{bb[0][0][4:]} {hm(bb[0][1])[:5]} {bb[3]:+.3f} ({abs(bb[3]) / abs(tot) * 100 if tot else 0:.0f}%)'}")
