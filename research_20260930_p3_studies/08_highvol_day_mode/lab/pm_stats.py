"""pm_stats — 프리마켓 bias 연구 분석 (out_BASE / out_PM1 / out_PM3 + PM2 해석적)."""
import copy
import pickle
import sys
from collections import defaultdict
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
meta = pickle.load(open(HERE / "pm_meta.pkl", "rb"))
PM, FIRST = meta["PM"], meta["FIRST"]
DATES = list(pickle.load(open(HERE / "_ctx80.pkl", "rb"))["dates"])
R30 = DATES[-30:]
MON = {m: [d for d in DATES if d[:6] == m] for m in ("202607", "202608", "202609")}


def load(n): return pickle.load(open(HERE / f"out_{n}.pkl", "rb"))["trades"]


def hm(i_or_ts): return pd.Timestamp(i_or_ts).strftime("%H:%M")


BASE = load("BASE")
PM1 = load("PM1"); PM3 = load("PM3")


def first_time(d):
    return pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {FIRST[d][2]}").time() if d in FIRST else None


def targeted(d, cut=dtime(9, 30)):
    if d not in FIRST: return False
    pmd, st, _ = PM[d]
    t = first_time(d); dv = FIRST[d][1]
    return (st == "STRONG" and t < cut
            and ((pmd == "PM_BLUE" and dv == "UP_RED") or (pmd == "PM_RED" and dv == "DOWN_BLUE")))


def first_trade(ts, d, allow_defer=False):
    """그날 첫 정규장 플래그에서 나온 진입 (decision_idx = flag_idx+1, PM1 지연이면 +2)."""
    if d not in FIRST: return None
    fi = FIRST[d][0]
    ok = {fi + 1} | ({fi + 2} if allow_defer else set())
    xs = [t for t in ts if t["date"] == d and t["decision_idx"] in ok]
    return xs[0] if xs else None


# PM2 = BASE 에서 대상 첫 거래만 비중 x0.5 (청산 판정은 수량 무관 -> 해석적으로 정확)
PM2 = copy.deepcopy(BASE)
pm2_hit = []
for d in DATES:
    if targeted(d):
        t = first_trade(PM2, d)
        if t is not None:
            t["w1a"] = t["w1a"] * 0.5; pm2_hit.append(d)
RUNS = {"R0": BASE, "PM1": PM1, "PM2": PM2, "PM3": PM3}


def pnl(t): return float(t["net_pct"]) * float(t["w1a"])


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


def wr(ts): return 100 * np.mean([pnl(t) > 0 for t in ts]) if ts else float("nan")


def excl_top(ts, k):
    top = set(map(id, sorted(ts, key=pnl, reverse=True)[:k]))
    return comp([t for t in ts if id(t) not in top], DATES)


def P(*a): print(*a, flush=True)


P("데이터: 프리마켓(08:00~) 봉이 있는 날 %d/80 → PM 판정 가능일. 나머지는 PM_MIXED(no data)."
  % sum(1 for d in DATES if PM[d][2].get("reason") != "no_premarket_bars"))
P("PM 분포: " + str(pd.Series([PM[d][1] + ":" + PM[d][0] for d in DATES]).value_counts().to_dict()))
P("적용대상(PM STRONG ∧ 첫플래그<09:30 ∧ 반대방향): %s" % [d for d in DATES if targeted(d)])
P("PM2 에서 x0.5 된 첫 거래: %s" % pm2_hit)
# 노출상한 확인 — PM2 로 늘어난 여유가 이후 거래 w1a 를 바꿨을 수 있는가
for d in pm2_hit:
    day = sorted((t for t in BASE if t["date"] == d), key=lambda t: t["entry_time"])
    P(f"   {d} 당일 w1a 합 {sum(t['w1a'] for t in day):.2f} (상한 3.0) → "
      + ("상한 무관(해석적 정확)" if sum(t['w1a'] for t in day) < 3.0 - 1e-9 else "⚠ 상한 도달 — 근사"))

P("\n" + "=" * 100)
P("표 1 — 80영업일 %s~%s" % (DATES[0], DATES[-1]))
P("=" * 100)
P("%-5s %4s %9s %8s %8s %8s %8s %6s %7s %6s | %8s" % ("전략", "n", "80일", "최근30", "9월", "7월", "8월",
                                                        "PF", "MDD", "WR", "Δ80"))
S = {}
for k, ts in RUNS.items():
    S[k] = dict(c80=comp(ts, DATES), r30=comp(ts, R30), sep=comp(ts, MON["202609"]),
                jul=comp(ts, MON["202607"]), aug=comp(ts, MON["202608"]), pf=pf(ts), mdd=mdd(ts), wr=wr(ts), n=len(ts))
    s = S[k]
    P("%-5s %4d %9.3f %8.3f %8.3f %8.3f %8.3f %6.3f %7.3f %6.2f | %+8.3f" % (
        k, s["n"], s["c80"], s["r30"], s["sep"], s["jul"], s["aug"], s["pf"], s["mdd"], s["wr"],
        s["c80"] - S["R0"]["c80"]))

P("\n09:30 이전 첫 플래그에서 나온 거래 (전략별)")
for k, ts in RUNS.items():
    xs = [first_trade(ts, d, allow_defer=(k == "PM1")) for d in DATES
          if d in FIRST and first_time(d) < dtime(9, 30)]
    xs = [x for x in xs if x is not None]
    P(f"  {k}: n={len(xs)} WR {wr(xs):.1f}% avg net {np.mean([x['net_pct'] for x in xs]):+.3f} "
      f"avg 가중 {np.mean([pnl(x) for x in xs]):+.3f} PF {pf(xs):.3f}")


def group_stats(label, xs):
    if not xs:
        P(f"  {label:34s} n=0"); return
    stop = np.mean(["STOP_LOSS" in (x["exit_reason"] or "") for x in xs]) * 100
    h50 = np.mean([bool(x["h50_held"]) for x in xs]) * 100
    P(f"  {label:34s} n={len(xs):2d} WR {wr(xs):5.1f}% avg {np.mean([x['net_pct'] for x in xs]):+6.3f} "
      f"PF {pf(xs):6.3f} MFE {np.mean([x['peak_net_pct'] for x in xs]):5.2f} "
      f"MAE {np.mean([x['mae_net_pct'] for x in xs]):+6.2f} STOP {stop:4.0f}% H50 {h50:4.0f}%")


def groups(cut, label):
    A, B, C1, C2 = [], [], [], []
    for d in DATES:
        if d not in FIRST or first_time(d) >= cut: continue
        x = first_trade(BASE, d)
        if x is None: continue
        pmd, st, det = PM[d]
        if det.get("reason") == "no_premarket_bars":
            C2.append(x)
        elif st == "STRONG":
            (B if targeted(d, cut) else A).append(x)
        else:
            C1.append(x)
    P(f"\n[{label}] BASE 첫 플래그 거래 그룹")
    group_stats("A PM STRONG 동일방향", A)
    group_stats("B PM STRONG 반대방향", B)
    group_stats("C WEAK/MIXED (프리마켓 데이터 있음)", C1)
    group_stats("C' 프리마켓 데이터 없음", C2)
    return A, B


P("\n" + "=" * 100)
P("표 2 / §8 — 09:30 이전 첫 플래그 (BASE 경로)")
P("=" * 100)
A30, B30 = groups(dtime(9, 30), "09:30")
P("\n" + "=" * 100)
P("표 3 / §6 — 시간컷 민감도 (기술통계만)")
P("=" * 100)
for c, lab in ((dtime(9, 20), "09:20"), (dtime(10, 0), "10:00")):
    groups(c, lab)

P("\n" + "=" * 100)
P("표 4 / §9 — PM STRONG 반대방향 첫 플래그 전량 (09:30) · 후보별 처리")
P("=" * 100)
for d in DATES:
    if not targeted(d): continue
    x = first_trade(BASE, d)
    pmd, st, det = PM[d]
    y1 = first_trade(PM1, d, allow_defer=True)
    y3 = first_trade(PM3, d)
    if x is None:
        P(f"  {d} flag {FIRST[d][2]} {FIRST[d][1][:4]} PM {pmd} — BASE 도 진입 안 함"); continue
    strong = x["peak_net_pct"] >= 3.0
    s1 = ("놓침(skip)" if y1 is None else
          f"{'늦춤' if y1['decision_idx'] != x['decision_idx'] else '동일'} {hm(y1['entry_time'])} {y1['net_pct']:+.3f}")
    P(f"  {d} flag {FIRST[d][2]} {FIRST[d][1][:4]} PM {pmd}(B{det['blue_votes']}/R{det['red_votes']}) "
      f"entry {hm(x['entry_time'])} BASE {x['net_pct']:+.3f} MFE {x['peak_net_pct']:.2f} MAE {x['mae_net_pct']:+.2f} "
      f"{x['exit_reason'][:22]}{'  ★강한 reversal' if strong else ''} | PM1 {s1} | "
      f"PM2 size50% → 가중 {pnl(x)*0.5:+.3f} (BASE {pnl(x):+.3f}) | PM3 {'놓침' if y3 is None else 'entry?'}")
    # 그날 나머지 거래 변화
    for k, ts in (("PM1", PM1), ("PM3", PM3)):
        b = sum(pnl(t) for t in BASE if t["date"] == d); v = sum(pnl(t) for t in ts if t["date"] == d)
        P(f"      {k}: 당일 가중합 {v:+.3f} vs BASE {b:+.3f} (Δ {v-b:+.3f}), 거래 {sum(t['date']==d for t in ts)} vs {sum(t['date']==d for t in BASE)}")

P("\n" + "=" * 100)
P("§12 강건성 vs R0")
P("=" * 100)


def dayfac(ts):
    f = defaultdict(lambda: 1.0)
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        f[t["date"]] *= 1 + pnl(t) / 100
    return np.array([f[d] for d in DATES])


rng = np.random.default_rng(20260928)
IDX = rng.integers(0, len(DATES), size=(10000, len(DATES)))
F0 = dayfac(BASE)
for k in ("PM1", "PM2", "PM3"):
    Fk = dayfac(RUNS[k])
    loo = [(np.prod(Fk) / Fk[i] - np.prod(F0) / F0[i]) * 100 for i in range(len(DATES))]
    bs = (np.prod(Fk[IDX], axis=1) - np.prod(F0[IDX], axis=1)) * 100
    diff = [DATES[i] for i in range(len(DATES)) if abs(Fk[i] - F0[i]) > 1e-12]
    dd = {DATES[i]: (Fk[i] - F0[i]) * 100 for i in range(len(DATES)) if abs(Fk[i] - F0[i]) > 1e-12}
    tot = sum(dd.values())
    top = max(dd.items(), key=lambda kv: abs(kv[1])) if dd else (None, 0)
    P(f"  {k}: Δ80 {S[k]['c80']-S['R0']['c80']:+.3f} | LOO 음수 {sum(x < 0 for x in loo)}/80 min {min(loo):+.3f} | "
      f"Top5x {excl_top(RUNS[k],5)-excl_top(BASE,5):+.3f} Top10x {excl_top(RUNS[k],10)-excl_top(BASE,10):+.3f} | "
      f"bootstrap {bs.mean():+.3f} [{np.percentile(bs,2.5):+.3f},{np.percentile(bs,97.5):+.3f}] "
      f"P(>0) {100*(bs>0).mean():.1f}% P(<0) {100*(bs<0).mean():.1f}%")
    P(f"       손익 다른 날 {len(diff)}일: " + ", ".join(f"{d} {v:+.2f}" for d, v in dd.items())
      + (f" | 최대 단일일 비중 {100*abs(top[1])/max(1e-9, sum(abs(v) for v in dd.values())):.0f}%" if dd else ""))
