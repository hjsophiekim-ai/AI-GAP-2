"""손절 후 동일방향 1회 재진입 (5차) 9월 집계. READ-ONLY."""
import pickle, random, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
from app.trading.macd2.worker import _net_return_pct  # noqa: E402
NAMES = {"R0": "H30", "R50": "RE50", "R70": "RE70", "R100": "RE100"}
T = {k: pickle.load(open(HERE / f"out_{v}_d83.pkl", "rb"))["trades"] for k, v in NAMES.items()}
anc = pickle.load(open(HERE / "prev" / "out_H30_d83_anchor.pkl", "rb"))["trades"]
SEP = [d for d in sorted({t["date"] for t in T["R0"]}) if d.startswith("202609")]
SEP16 = [d for d in SEP if d <= "20260928"]
SLR = ("GX_SL", "TIME_WINDOW_STOP_LOSS")
pnl = lambda t: float(t["net_pct"]) * float(t["w1a"])
key = lambda t: (t["date"], str(t["entry_time"]), t["direction"])
hm = lambda s: str(s)[11:16] if s else ""
p = lambda *a: print(*a, flush=True)
sel = lambda ts, ds=SEP: sorted((t for t in ts if t["date"] in ds), key=lambda x: str(x["exit_time"]))
def comp(ts, ds=SEP):
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
    d = {x: 0.0 for x in SEP}
    for t in sel(ts): d[t["date"]] += pnl(t)
    return d
sig = lambda ts: sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]), round(float(t["net_pct"]), 8)) for t in ts)

p("# 손절 후 동일방향 1회 재진입 — 2026년 9월 (2026-09-30)\n")
p("READ-ONLY. production/config/UI 무수정. 훅 = lab/patch_reentry.py (opt-in b3['reent']).\n")
p(f"## 0. R0 재현: 83일 앵커 시그니처 일치 **{sig(T['R0']) == sig(anc)}** ({len(T['R0'])}거래, 83일 복리 {comp(T['R0'], sorted({t['date'] for t in T['R0']})):.4f}) · 9월 16일 {comp(T['R0'], SEP16):+.3f} (앵커 +19.755) · 집계 9월 {len(SEP)}영업일\n")

p("## 1. 9월 성과")
p("| 전략 | 9월 복리 | Δ | PF | MDD | CHOP PF | 손실일 | 2%+일 | 거래 | 재진입 |")
p("|---|---|---|---|---|---|---|---|---|---|")
for k, ts in T.items():
    s = sel(ts); dd = daily(ts); ch = [t for t in s if t.get("entry_regime") == "CHOP"]
    p(f"| {k} | {comp(ts):+.3f} | {comp(ts) - comp(T['R0']):+.3f} | {pf(s):.3f} | {mdd(ts):.2f} | {pf(ch):.3f} | "
      f"{sum(v < 0 for v in dd.values())} | {sum(v >= 2 for v in dd.values())} | {len(s)} | {sum(bool(t.get('reentry')) for t in s)} |")
p("")

# Q1: 손절 후 60분 같은 방향 +2%
p("## 2. 9월 R0 손절 거래 → 이후 60분 같은 방향")
etf = {}
def etfdf(date, d):
    k = (date, d)
    if k not in etf:
        etf[k] = pd.read_csv(HERE / "cache83" / f"replay_{date}_{'long' if d == 'UP_RED' else 'inverse'}_1m.csv", parse_dates=["datetime"]).set_index("datetime")
    return etf[k]
big = []
for t in sel(T["R0"]):
    if t["exit_reason"] not in SLR:
        continue
    f = etfdf(t["date"], t["direction"]); a = pd.Timestamp(t["exit_time"]).tz_localize(None)
    w = f.loc[(f.index > a) & (f.index <= a + pd.Timedelta(minutes=60))]
    up = (w["high"].max() / float(t["exit_price"]) - 1) * 100 if len(w) else float("nan")
    if up >= 2.0:
        big.append(key(t))
    p(f"- {t['date'][4:]} {hm(t['entry_time'])} {t['direction'][:4]} {t.get('entry_regime')} {t['exit_reason']} {hm(t['exit_time'])} net {float(t['net_pct']):+.2f} → 60분 최대 +{up:.2f}%{' ← +2% 이상' if up >= 2 else ''}")
p(f"→ 손절 {sum(1 for t in sel(T['R0']) if t['exit_reason'] in SLR)}건 중 60분 안 같은 방향 +2% 이상 **{len(big)}건**\n")

# 재진입 상세
p("## 3. 재진입 거래 상세")
p("| 후보 | 날짜 | 최초 진입 | 방향 | 손절 | 손절 net | 재진입(=조건충족) | 재진입가 | MFE | MAE | 재진입 net | ×W1a | 청산 | R0 하루 | 후보 하루 | Δ |")
p("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
D0 = daily(T["R0"])
for k in ("R50", "R70", "R100"):
    Dk = daily(T[k])
    for t in sel(T[k]):
        if not t.get("reentry"):
            continue
        p(f"| {k} | {t['date'][4:]} | {hm(t.get('reent_orig_entry'))} | {t['direction'][:4]} | {hm(t.get('reent_sl_at'))} | {t.get('reent_sl_net'):+.2f} | "
          f"{hm(t['entry_time'])} | {float(t['entry_price']):,.0f} | {float(t['peak_net_pct']):+.2f} | {float(t['mae_net_pct']):+.2f} | "
          f"{float(t['net_pct']):+.3f} | {float(t['w1a']):.2f} | {t['exit_reason']} | {D0[t['date']]:+.3f} | {Dk[t['date']]:+.3f} | {Dk[t['date']] - D0[t['date']]:+.3f} |")
p("")

# 성공/실패, 바뀐 거래(슬롯 밀림 포함)
p("## 4. 효과 분해 (R0 대비, W1a 가중 %)")
for k in ("R50", "R70", "R100"):
    A = {key(t): t for t in sel(T["R0"])}; B = {key(t): t for t in sel(T[k])}
    df = [(kk, A.get(kk), B.get(kk), (pnl(B[kk]) if kk in B else 0) - (pnl(A[kk]) if kk in A else 0)) for kk in sorted(set(A) | set(B))]
    df = [x for x in df if abs(x[3]) > 1e-9]
    re = [x for x in df if x[2] is not None and x[2].get("reentry")]
    other = [x for x in df if x not in re]
    g = sum(x[3] for x in re if x[3] > 0); l = sum(x[3] for x in re if x[3] < 0)
    p(f"- **{k}**: 재진입 {len(re)}건 (성공 {sum(x[3] > 0 for x in re)} / 실패 {sum(x[3] < 0 for x in re)}) · gain {g:+.3f} · loss {l:+.3f} · "
      f"재진입 외 바뀐 거래 {len(other)}건 {sum(x[3] for x in other):+.3f} · 순효과 {sum(x[3] for x in df):+.3f}")
    for kk, a, b, d in other:
        p(f"  - 슬롯/경로 변화: {kk[0][4:]} {hm(kk[1])} {kk[2][:4]}: R0 {'없음' if a is None else f'{a['exit_reason']} {float(a['net_pct']):+.3f}'} → {'없음' if b is None else f'{b['exit_reason']} {float(b['net_pct']):+.3f}'} (Δ {d:+.3f})")
p("")

# 안전성
p("## 5. 안전성")
for k in ("R50", "R70", "R100"):
    s = sel(T[k]); byday = defaultdict(list)
    for t in s: byday[t["date"]].append(t)
    maxn = max(len(v) for v in byday.values()); exp = max(sum(float(t["w1a"]) for t in v) for v in byday.values())
    dup = max(sum(bool(t.get("reentry")) for t in v) for v in byday.values())
    p(f"- {k}: 하루 최대 진입 {maxn}건 (cap 3) · 하루 최대 노출(W1a 합) {exp:.2f} (cap 3.0) · 하루 재진입 최대 {dup}건")
p("")

# 강건성
p("## 6. 강건성 (9월, R0 대비) — ⚠ 재진입 표본 작음")
p("| 후보 | Δ | 바뀐 날 | day-LOO 음수 | Top1 제외 Δ | Top3 제외 Δ | P(Δ>0) | 최대 단일거래 기여 |")
p("|---|---|---|---|---|---|---|---|")
for k in ("R50", "R70", "R100"):
    d1 = daily(T[k]); dd = {x: d1[x] - D0[x] for x in SEP}
    loo = [comp(T[k], [x for x in SEP if x != y]) - comp(T["R0"], [x for x in SEP if x != y]) for y in SEP]
    top = sorted(SEP, key=lambda x: dd[x], reverse=True)
    ex = lambda n: comp(T[k], [x for x in SEP if x not in top[:n]]) - comp(T["R0"], [x for x in SEP if x not in top[:n]])
    rng = random.Random(20260930); v = list(dd.values())
    boot = np.array([sum(rng.choice(v) for _ in v) for _ in range(20000)])
    A = {key(t): t for t in sel(T["R0"])}; B = {key(t): t for t in sel(T[k])}
    df = [((pnl(B[kk]) if kk in B else 0) - (pnl(A[kk]) if kk in A else 0), kk) for kk in set(A) | set(B)]
    df = [x for x in df if abs(x[0]) > 1e-9]; tot = sum(x[0] for x in df)
    bb = max(df, key=lambda x: abs(x[0])) if df else (0, None)
    p(f"| {k} | {comp(T[k]) - comp(T['R0']):+.3f} | {sum(abs(x) > 1e-9 for x in v)} | {sum(x < 0 for x in loo)}/{len(SEP)} | {ex(1):+.3f} | {ex(3):+.3f} | "
      f"{(boot > 0).mean() * 100:.1f}% | {'' if bb[1] is None else f'{bb[1][0][4:]} {hm(bb[1][1])} {bb[0]:+.2f} ({abs(bb[0]) / abs(tot) * 100:.0f}%)'} |")
