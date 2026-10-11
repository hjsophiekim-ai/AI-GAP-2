"""r_stats — P3 late-promotion(R1/R2/R3) vs R0 분석. out_*.pkl 만 읽는다."""
import bisect
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
SFX = sys.argv[1] if len(sys.argv) > 1 else ""   # "" = 80일, "_d81" 등


def load(n):
    return pickle.load(open(HERE / f"out_{n}{SFX}.pkl", "rb"))["trades"]


NAMES = {"A_BASE": "BASE", "B_B3": "T10", "C_R0": "R0", "D_R1": "R1", "E_R2": "R2", "F_R3": "R3"}
R = {}
for lab, n in NAMES.items():
    try:
        R[lab] = load(n)
    except FileNotFoundError:
        pass
_ctx = pickle.load(open(HERE / f"_ctx{80 if not SFX else SFX.replace('_d', '')}.pkl", "rb"))
DATES = [d for d in _ctx["dates"] if d <= "20260922"]      # 80일 창 (OOS 는 별도)
RECENT30 = DATES[-30:]
SEP = [d for d in DATES if d.startswith("202609")]
NONSEP = [d for d in DATES if not d.startswith("202609")]
for k in R:
    R[k] = [t for t in R[k] if t["date"] in set(DATES)]
BASE = R["A_BASE"]
_rows = sorted(((pd.Timestamp(t["exit_time"]), bool(t["h50_held"]), bool(t["tp1_hit"])) for t in BASE),
               key=lambda r: r[0])
_ex = [r[0] for r in _rows]


def regime_at(ts):
    k = bisect.bisect_left(_ex, pd.Timestamp(ts))
    if k < 10:
        return "WARMUP"
    rec = _rows[k - 10:k]
    return "CHOP" if (sum(r[1] for r in rec) >= 4 and sum(r[2] for r in rec) <= 2) else "TREND"


for t in BASE:
    t["entry_regime"] = regime_at(t["entry_time"])


def pnl(t): return float(t["net_pct"]) * float(t["w1a"])
def key(t): return (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])


def comp(ts, days):
    ds = set(days); eq = 1.0
    for t in sorted((x for x in ts if x["date"] in ds), key=lambda x: x["exit_time"]):
        eq *= 1 + pnl(t) / 100
    return (eq - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts):
    eq = pk = 1.0; m = 0.0
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        eq *= 1 + pnl(t) / 100; pk = max(pk, eq); m = min(m, eq / pk - 1)
    return m * 100


def excl_top(ts, k):
    top = set(map(id, sorted(ts, key=pnl, reverse=True)[:k]))
    return comp([t for t in ts if id(t) not in top], DATES)


def chop(ts): return [t for t in ts if t.get("entry_regime") == "CHOP"]


def P(*a): print(*a, flush=True)


VK = {k: {key(t): t for t in v} for k, v in R.items()}
S = {}
for k, ts in R.items():
    c = chop(ts)
    S[k] = dict(n=len(ts), c80=comp(ts, DATES), r30=comp(ts, RECENT30), sep=comp(ts, SEP),
                nonsep=comp(ts, NONSEP), pf=pf(ts), mdd=mdd(ts),
                wr=100 * sum(pnl(t) > 0 for t in ts) / len(ts),
                chop_pf=pf(c), chop_avg=np.mean([t["net_pct"] for t in c]) if c else float("nan"),
                chop_n=len(c))

P("=" * 110)
P(f"§9 성과 (80영업일 {DATES[0]}~{DATES[-1]})")
P("=" * 110)
P("%-8s %4s %9s %8s %8s %9s %6s %7s %6s %6s %8s | %8s %8s %8s" % (
    "전략", "n", "80일", "최근30", "9월", "비9월", "PF", "MDD", "WR", "CHOPPF", "CHOPavg",
    "Δ80vsR0", "Δ30", "Δ9월"))
r0 = S.get("C_R0")
for k in R:
    s = S[k]
    P("%-8s %4d %9.3f %8.3f %8.3f %9.3f %6.3f %7.3f %6.2f %6.3f %8.4f | %+8.3f %+8.3f %+8.3f" % (
        NAMES[k], s["n"], s["c80"], s["r30"], s["sep"], s["nonsep"], s["pf"], s["mdd"], s["wr"],
        s["chop_pf"], s["chop_avg"], s["c80"] - r0["c80"], s["r30"] - r0["r30"], s["sep"] - r0["sep"]))

# ── §10 TREND 보존 ───────────────────────────────────────────────────────
P("\n§10 TREND/WARMUP 진입 거래 R0 대비 diff")
sig = lambda t: (key(t), str(t["exit_time"]), round(t["net_pct"], 8), t["exit_reason"])
r0_tr = {sig(t) for t in R["C_R0"] if t.get("entry_regime") != "CHOP"}
for k in ("D_R1", "E_R2", "F_R3"):
    if k not in R: continue
    tr = {sig(t) for t in R[k] if t.get("entry_regime") != "CHOP"}
    P(f"  {NAMES[k]}: R0에만 {len(r0_tr - tr)}건 / {NAMES[k]}에만 {len(tr - r0_tr)}건"
      + ("  → diff 0" if r0_tr == tr else ""))
    for x in sorted(r0_tr ^ tr)[:10]:
        P("     ", x)

# ── §12 조건별 진단 ─────────────────────────────────────────────────────
P("\n" + "=" * 110)
P("§12 R0 의 CHOP 거래 중 +1% 최초 도달이 6분 초과인 거래 — 조건별 (MFE/최종net 은 BASE 경로 기준)")
P("=" * 110)
BK = VK["A_BASE"]
late = [t for t in R["C_R0"] if t.get("b3_on") and t.get("b3_trig_el") is not None
        and t["b3_trig_el"] > 6.0 + 1e-9 and t.get("b3_trig_diag")]
rows = []
for t in late:
    d = t["b3_trig_diag"]; b = BK.get(key(t))
    rows.append(dict(t=t, d=d, b=b, g1=d["c3"], g2=d["c3"] and d["slope_ok"],
                     g3=d["c3"] and d["slope_ok"] and (d["gap_ok"] or d["etf_ok"])))


def grp(label, rs):
    bb = [r for r in rs if r["b"] is not None]
    if not rs:
        P(f"  {label:26s} n=0"); return
    mfe = [r["b"]["peak_net_pct"] for r in bb]; net = [r["b"]["net_pct"] for r in bb]
    P(f"  {label:26s} n={len(rs):2d} (BASE매칭 {len(bb)}) 평균MFE {np.mean(mfe) if mfe else float('nan'):5.2f} "
      f"BASE평균net {np.mean(net) if net else float('nan'):+6.3f} R0평균net {np.mean([r['t']['net_pct'] for r in rs]):+6.3f} "
      f"MFE>=3 {100*np.mean([m>=3 for m in mfe]) if mfe else 0:5.1f}% MFE>=5 {100*np.mean([m>=5 for m in mfe]) if mfe else 0:5.1f}%")


grp("전체(6분초과 +1%)", rows)
grp("G1 3라인정렬", [r for r in rows if r["g1"]])
grp("G1 아님", [r for r in rows if not r["g1"]])
grp("G2 3라인+slope", [r for r in rows if r["g2"]])
grp("G3 G2+(gap|ETF)", [r for r in rows if r["g3"]])
grp("G2 아님", [r for r in rows if not r["g2"]])
P("\n  거래 전량:")
for r in sorted(rows, key=lambda r: r["t"]["entry_time"]):
    t, d, b = r["t"], r["d"], r["b"]
    P(f"   {t['date']} {pd.Timestamp(t['entry_time']).strftime('%H:%M')} {t['direction'][:4]} "
      f"+1%@{pd.Timestamp(t['b3_first_trig_at']).strftime('%H:%M')}({t['b3_trig_el']:.0f}분) "
      f"c3={int(d['c3'])} slope={int(d['slope_ok'])} gap={int(d['gap_ok'])} etf={int(d['etf_ok'])} "
      f"G1/2/3={int(r['g1'])}{int(r['g2'])}{int(r['g3'])} | R0 {t['net_pct']:+.3f} {t['exit_reason']} | "
      f"BASE {'-' if b is None else f'{b['net_pct']:+.3f} MFE {b['peak_net_pct']:.2f} {b['exit_reason']}'}")

# ── §6 runner / §7 false promotion / §8 분해 ────────────────────────────
bc = chop(BASE)
P("\n" + "=" * 110)
P("§6 CHOP runner 보존 (BASE CHOP 진입, 보존 = 변형 net >= BASE net − 0.5)")
P("=" * 110)
RUNKEYS = [k for k in ("B_B3", "C_R0", "D_R1", "E_R2", "F_R3") if k in R]
for thr in (3.0, 5.0, 8.0):
    rs = [t for t in bc if t["peak_net_pct"] >= thr]
    if not rs:
        P(f"  MFE>={thr:.0f}%: 0건"); continue
    bp = sum(pnl(t) for t in rs)
    line = f"  MFE>={thr:.0f}%: n={len(rs)} BASE {bp:+.3f} |"
    for k in RUNKEYS:
        m = [(t, VK[k].get(key(t))) for t in rs]
        kept = sum(1 for t, x in m if x is not None and x["net_pct"] >= t["net_pct"] - 0.5)
        vp = sum(pnl(x) for t, x in m if x is not None)
        line += f" {NAMES[k]} {kept}/{len(rs)} ({100*kept/len(rs):.0f}%) 이익 {100*vp/bp:.1f}% |"
    P(line)
r3 = [t for t in bc if t["peak_net_pct"] >= 3.0]
P("  R0 대비 추가로 살린 runner / 추가 rescue 이익(가중, MFE>=3):")
for k in ("D_R1", "E_R2", "F_R3"):
    if k not in R: continue
    add = 0; gain = 0.0
    for t in r3:
        a, b = VK["C_R0"].get(key(t)), VK[k].get(key(t))
        ka = a is not None and a["net_pct"] >= t["net_pct"] - 0.5
        kb = b is not None and b["net_pct"] >= t["net_pct"] - 0.5
        add += int(kb and not ka)
        gain += (pnl(b) if b else 0) - (pnl(a) if a else 0)
    P(f"    {NAMES[k]}: 추가 보존 {add}건, runner 이익 변화 {gain:+.3f}%p")
P("  9월 runner 전량:")
for d in ["20260907", "20260908", "20260909", "20260916", "20260922"]:
    for t in [x for x in BASE if x["date"] == d and x["peak_net_pct"] >= 3.0]:
        s = f"   {d} {pd.Timestamp(t['entry_time']).strftime('%H:%M')} BASE {t['net_pct']:+.3f}"
        for k in RUNKEYS:
            x = VK[k].get(key(t))
            s += (f" | {NAMES[k]} " + ("소멸" if x is None else
                  f"{x['net_pct']:+.3f} {x['exit_reason'][:14]}"
                  f"{' R' if x.get('b3_rescued') else ''}{' L' if x.get('b3_late') else ''}{' Y' if x.get('b3_y3') else ''}"))
        P(s)

P("\n" + "=" * 110)
P("§7 false promotion / §8 R0 대비 delta 분해 (가중 net 단순합, %p)")
P("=" * 110)
R0K = VK["C_R0"]
for k in ("D_R1", "E_R2", "F_R3"):
    if k not in R: continue
    ts = R[k]
    prom = [t for t in ts if t.get("b3_late")]
    true_r = fp = 0; gain = loss = 0.0
    P(f"  [{NAMES[k]}] 신규 승격 {len(prom)}건")
    for t in prom:
        a = R0K.get(key(t)); dlt = pnl(t) - (pnl(a) if a else 0.0)
        is_fp = (t["peak_net_pct"] < 1.5) or (a is not None and pnl(t) < pnl(a))
        true_r += int(t["peak_net_pct"] >= 3.0)
        fp += int(is_fp)
        if dlt > 0: gain += dlt
        else: loss += dlt
        P(f"     {t['date']} {pd.Timestamp(t['entry_time']).strftime('%H:%M')} {t['direction'][:4]} "
          f"MFE {t['peak_net_pct']:.2f} | {NAMES[k]} {t['net_pct']:+.3f} {t['exit_reason']} | "
          f"R0 {'-' if a is None else f'{a['net_pct']:+.3f} {a['exit_reason']}'} | Δ {dlt:+.3f}"
          f"{'  FALSE' if is_fp else ''}")
    prec = 100 * (len(prom) - fp) / len(prom) if prom else float("nan")
    # §8 분해
    A = B = C = 0.0
    pk = {key(t) for t in prom}
    for t in ts:
        a = R0K.get(key(t))
        if a is None: continue
        d = pnl(t) - pnl(a)
        if key(t) in pk:
            if d > 0: B += d
            else: C += d
        else:
            A += d
    D = sum(pnl(t) for t in ts if key(t) not in R0K) - sum(pnl(a) for kk, a in R0K.items() if kk not in VK[k])
    P(f"     → promotion {len(prom)} / true runner(MFE>=3) {true_r} / false {fp} / precision {prec:.1f}% | "
      f"rescue gain {gain:+.3f} false loss {loss:+.3f} 순효과 {gain+loss:+.3f}")
    P(f"     → 분해: A 같은거래 exit {A:+.3f} | B rescue {B:+.3f} | C false {C:+.3f} | "
      f"D 재배치 {D:+.3f} | 직접(A+B+C) {A+B+C:+.3f} | 합 {A+B+C+D:+.3f}  "
      f"(복리 Δ {S[k]['c80']-S['C_R0']['c80']:+.3f})"
      + ("  ⚠ 재배치로만 개선" if (A + B + C) <= 0 < (A + B + C + D) else ""))

# ── §11 강건성 ──────────────────────────────────────────────────────────
P("\n" + "=" * 110)
P("§11 강건성 vs R0")
P("=" * 110)


def dayfac(ts):
    f = defaultdict(lambda: 1.0)
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        f[t["date"]] *= 1 + pnl(t) / 100
    return np.array([f[d] for d in DATES])


rng = np.random.default_rng(20260928)
IDX = rng.integers(0, len(DATES), size=(10000, len(DATES)))
F0 = dayfac(R["C_R0"])
for k in ("D_R1", "E_R2", "F_R3"):
    if k not in R: continue
    Fk = dayfac(R[k])
    loo = [(np.prod(Fk) / Fk[i] - np.prod(F0) / F0[i]) * 100 for i in range(len(DATES))]
    bs = (np.prod(Fk[IDX], axis=1) - np.prod(F0[IDX], axis=1)) * 100
    tops = " ".join(f"Top{n}x {excl_top(R[k], n) - excl_top(R['C_R0'], n):+.3f}" for n in (1, 3, 5, 10))
    diffdays = sum(abs(Fk[i] - F0[i]) > 1e-12 for i in range(len(DATES)))
    P(f"  {NAMES[k]}: Δ80 {S[k]['c80']-S['C_R0']['c80']:+.3f} | LOO 음수일 {sum(x < 0 for x in loo)}/80 "
      f"≤0일 {sum(x <= 1e-12 for x in loo)} min {min(loo):+.3f} | {tops} | "
      f"bootstrap 평균 {bs.mean():+.3f} [{np.percentile(bs,2.5):+.3f},{np.percentile(bs,97.5):+.3f}] "
      f"P(>0) {100*(bs>0).mean():.1f}% P(<0) {100*(bs<0).mean():.1f}% | R0와 손익 다른 날 {diffdays}일")
pickle.dump(S, open(HERE / f"r_stats{SFX}.pkl", "wb"))
