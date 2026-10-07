"""t12_stats — B3 TP 분리(+1.0 판단 / +1.2 전량익절) 9월 전용 (2026-09-29).

R0 = production P3 (Q2 20% + H30) = 랩 변형 "H30".
T12 = R0 + 승격 전 B3 의 **전량익절 기준만** +1.2 (tp_exit). Q2 rescue(6분 내 +1.0) 와
H30 연장 중 부분익절(+1.0) 은 그대로 +1.0.
"""
import pickle
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0, "proj"); sys.path.insert(0, "proj/scripts")
SFX = sys.argv[1] if len(sys.argv) > 1 else "_d82"
L = lambda n: pickle.load(open(f"out_{n}{SFX}.pkl", "rb"))["trades"]
D = list(pickle.load(open(f"_ctx{SFX[2:] if SFX else '80'}.pkl", "rb"))["dates"])
SEP = [d for d in D if "20260901" <= d <= "20260928"]
R0all, T12all = L("H30"), L("T12")
S = {"R0": [t for t in R0all if t["date"] in set(SEP)], "T12": [t for t in T12all if t["date"] in set(SEP)]}
pnl = lambda t: t["net_pct"] * t["w1a"]
key = lambda t: (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])
P = lambda *a: print(*a, flush=True)
hm = lambda x: pd.Timestamp(x).strftime("%H:%M:%S") if x else "-"


def fac(ts):
    f = defaultdict(lambda: 1.0)
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        f[t["date"]] *= 1 + pnl(t) / 100
    return np.array([f[d] for d in SEP])


comp = lambda ts: (np.prod(fac(ts)) - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts):
    e = pk = 1.0; m = 0.0
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, e / pk - 1)
    return m * 100


chop = lambda ts: [t for t in ts if t.get("entry_regime") == "CHOP"]
M = {}
P(f"9월 영업일 {len(SEP)}일: {', '.join(SEP)}")
P("\n## 9월 결과표")
P("| 전략 | 거래 | 9월 복리 | PF | MDD | WR | CHOP n | CHOP PF | 손실일 | ≥2%일 |")
P("|---|---|---|---|---|---|---|---|---|---|")
for k, ts in S.items():
    dd = (fac(ts) - 1) * 100; c = chop(ts)
    M[k] = dict(comp=comp(ts), pf=pf(ts), mdd=mdd(ts), cpf=pf(c), loss=int((dd < 0).sum()), big=int((dd >= 2).sum()))
    P(f"| {k} | {len(ts)} | {M[k]['comp']:+.3f}% | {M[k]['pf']:.3f} | {M[k]['mdd']:.3f}% | "
      f"{100*np.mean([pnl(t) > 0 for t in ts]):.1f}% | {len(c)} | {M[k]['cpf']:.3f} | {M[k]['loss']} | {M[k]['big']} |")

K0 = {key(t): t for t in S["R0"]}; K1 = {key(t): t for t in S["T12"]}
# 영향 거래: R0 에서 승격 전 B3 가 +1.0 전량익절(GX_TP) 한 거래 = T12 가 바꾸는 유일한 지점
AFF = sorted([kk for kk, t in K0.items() if t["exit_reason"] == "GX_TP"])
P(f"\n## T12 영향 거래 (R0 에서 B3 +1.0 전량익절 = GX_TP) : {len(AFF)}건")
other = [kk for kk in set(K0) | set(K1) if kk not in AFF and (
    kk not in K0 or kk not in K1 or abs(K0[kk]["net_pct"] - K1[kk]["net_pct"]) > 1e-9
    or str(K0[kk]["exit_time"]) != str(K1[kk]["exit_time"]))]
P(f"- 영향 거래 밖의 차이: {len(other)}건" + ("" if not other else " -> " + "; ".join(
    f"{d[0]} {d[1][11:16]} {d[2][:4]} " + (f"R0 {K0[d]['net_pct']:+.3f} / T12 {K1[d]['net_pct']:+.3f}" if d in K0 and d in K1
                                          else ("R0에만" if d in K0 else "T12에만(새 진입)"))
    for d in sorted(other))))


def group(r0, t1):
    if t1 is None:
        return "?"
    if t1.get("b3_y3") or t1.get("b3_ext_prom") or t1.get("b3_rescued"):
        return "G4"
    if t1["exit_reason"] == "GX_TP":
        return "G1"
    return "G2" if t1["net_pct"] >= r0["net_pct"] - 1e-9 else "G3"


P("\n| 날짜 | 진입 | 방향 | +1.0 도달 | 경과(분) | +1.2 도달 | 도달소요(분) | +1.0 이후 최고 | +1.0 이후 최저 | MFE(T12) | R0 net | T12 net | T12 청산 | Δ | 그룹 |")
P("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
rows = []
for kk in AFF:
    r0 = K0[kk]; t1 = K1.get(kk); g = group(r0, t1)
    trig = t1.get("b3_first_trig_at") if t1 else None
    ex12 = t1.get("b3_exit_trig_at") if t1 else None
    ttime = (pd.Timestamp(ex12) - pd.Timestamp(trig)).total_seconds() / 60 if (trig and ex12) else None
    d = (pnl(t1) - pnl(r0)) if t1 else float("nan")
    rows.append(dict(k=kk, g=g, r0=r0, t1=t1, ttime=ttime, d=d))
    P(f"| {kk[0]} | {kk[1][11:16]} | {kk[2]} | {hm(trig)} | {t1.get('b3_trig_el') if t1 else '-'} | "
      f"{'YES' if g == 'G1' else 'NO'} | {'-' if ttime is None else f'{ttime:.1f}'} | "
      f"{(t1.get('b3_post_trig_max') or 0):+.3f} | {(t1.get('b3_post_trig_min') or 0):+.3f} | {t1['peak_net_pct']:+.3f} | "
      f"{r0['net_pct']:+.3f} | {t1['net_pct']:+.3f} | {str(t1['exit_reason'])[:18]} | {d:+.3f} | {g} |")

P("\n## 그룹별")
P("| 그룹 | 정의 | n | 평균 MFE | 평균 final net (T12) | 평균 Δ(가중) | Δ 합 |")
P("|---|---|---|---|---|---|---|")
DEF = {"G1": "+1.0 → +1.2 도달", "G2": "+1.2 미도달, R0 이상 유지", "G3": "+1.2 미도달, 되밀려 R0보다 손해",
       "G4": "+1.0 후 Y3/H30 승격 → runner"}
for g in ("G1", "G2", "G3", "G4"):
    rs = [r for r in rows if r["g"] == g]
    if not rs:
        P(f"| {g} | {DEF[g]} | 0 | - | - | - | - |"); continue
    P(f"| {g} | {DEF[g]} | {len(rs)} | {np.mean([r['t1']['peak_net_pct'] for r in rs]):+.3f} | "
      f"{np.mean([r['t1']['net_pct'] for r in rs]):+.3f} | {np.mean([r['d'] for r in rs]):+.3f} | {sum(r['d'] for r in rs):+.3f} |")

n = len(rows); g1 = [r for r in rows if r["g"] == "G1"]; nonreach = [r for r in rows if r["g"] in ("G2", "G3")]
gain = sum(r["d"] for r in rows if r["d"] > 0); lost = sum(r["d"] for r in rows if r["d"] < 0)
tt = [r["ttime"] for r in g1 if r["ttime"] is not None]
P("\n## 핵심 질문")
P(f"1. +1.0 도달(=R0 GX_TP) {n}건 중 +1.2 도달 {len(g1)}건 ({100*len(g1)/max(n,1):.0f}%)")
P(f"2. +1.0 찍고 +1.2 못 간 뒤 0% 이하로 끝난 거래: {sum(1 for r in rows if r['g'] != 'G1' and r['t1']['net_pct'] <= 0)}건 "
  f"(미도달 {len(nonreach)}건 + 승격 {sum(1 for r in rows if r['g']=='G4')}건 중)")
P(f"3. T12 가 추가로 먹은 수익 합(Δ>0 거래, W1a 가중 %p): {gain:+.3f}")
P(f"4. T12 때문에 놓친 +1.0 익절 합(Δ<0 거래): {lost:+.3f}")
P(f"5. 순효과(영향 거래 합): {gain + lost:+.3f}  | 영향 밖 차이 합: "
  f"{sum(pnl(K1[d]) - pnl(K0[d]) for d in other if d in K0 and d in K1):+.3f}")
P(f"6. +1.0 → +1.2 소요시간: median {np.median(tt):.1f}분 / max {max(tt):.1f}분" if tt else "6. +1.2 도달 없음")
P(f"7. +1.2 기다리다 max-hold/Y3/H30 으로 넘어간 거래: "
  f"{sum(1 for r in rows if r['g'] != 'G1' and (r['t1']['exit_reason'] in ('GX_MAXHOLD',) or r['t1'].get('b3_y3') or r['t1'].get('b3_ext')))}건 "
  f"(GX_MAXHOLD {sum(1 for r in rows if r['t1']['exit_reason']=='GX_MAXHOLD')} / Y3 승격 {sum(1 for r in rows if r['t1'].get('b3_y3'))} / "
  f"H30 연장 {sum(1 for r in rows if r['t1'].get('b3_ext'))})")

P("\n## runner 생성/손상 (9월 전 거래, MFE 기준)")
for thr in (3.0, 5.0, 8.0):
    a = {kk for kk, t in K0.items() if t["peak_net_pct"] >= thr}; b = {kk for kk, t in K1.items() if t["peak_net_pct"] >= thr}
    P(f"- MFE≥{thr:.0f}%: R0 {len(a)}건 / T12 {len(b)}건 | 새로 생김 {sorted(b - a)} | 사라짐 {sorted(a - b)}")
chg = [kk for kk in set(K0) & set(K1) if (K0[kk].get("b3_rescued") or K0[kk].get("b3_y3") or K1[kk].get("b3_rescued") or K1[kk].get("b3_y3"))
       and abs(K0[kk]["net_pct"] - K1[kk]["net_pct"]) > 1e-9]
P(f"- 기존 Q2/Y3 runner 중 결과가 달라진 거래: {len(chg)}건 {sorted(chg)}")

rng = np.random.default_rng(20260929); IDX = rng.integers(0, len(SEP), size=(20000, len(SEP)))
F0, F1 = fac(S["R0"]), fac(S["T12"])
loo = [(np.prod(F1) / F1[i] - np.prod(F0) / F0[i]) * 100 for i in range(len(SEP))]
bs = (np.prod(F1[IDX], axis=1) - np.prod(F0[IDX], axis=1)) * 100
dd = {SEP[i]: (F1[i] - F0[i]) * 100 for i in range(len(SEP)) if abs(F1[i] - F0[i]) > 1e-12}
tot = sum(abs(v) for v in dd.values()); big = max(dd.items(), key=lambda kv: abs(kv[1])) if dd else ("-", 0.0)
td = {kk: pnl(K1[kk]) - pnl(K0[kk]) for kk in set(K0) & set(K1)}
ttot = sum(abs(v) for v in td.values()); tbig = max(td.items(), key=lambda kv: abs(kv[1])) if td else (("-", "-", "-"), 0.0)


def excl(ts, k):
    top = set(map(id, sorted(ts, key=pnl, reverse=True)[:k])); return comp([t for t in ts if id(t) not in top])


P("\n## 강건성 (T12 − R0, 9월) — ⚠ 표본 작음")
P(f"- Δ9월 복리 {M['T12']['comp'] - M['R0']['comp']:+.3f}%p")
P(f"- day-LOO: 음수 {sum(x < 0 for x in loo)}/{len(SEP)}, min {min(loo):+.3f}, max {max(loo):+.3f}")
P(f"- Top1 제외 Δ {excl(S['T12'], 1) - excl(S['R0'], 1):+.3f} / Top3 제외 Δ {excl(S['T12'], 3) - excl(S['R0'], 3):+.3f}")
P(f"- bootstrap 20,000: 평균 {bs.mean():+.3f} [95% {np.percentile(bs, 2.5):+.3f}, {np.percentile(bs, 97.5):+.3f}] P(Δ>0) {100*(bs > 0).mean():.1f}%")
P(f"- 바뀐 날 {len(dd)}일, 최대 단일일 {big[0]} {big[1]:+.3f} ({100*abs(big[1])/max(tot,1e-9):.0f}%)")
P(f"- 최대 단일거래 기여 {tbig[0][0]} {str(tbig[0][1])[11:16]} {tbig[1]:+.3f} ({100*abs(tbig[1])/max(ttot,1e-9):.0f}%)")

P("\n## 성공 기준")
crit = [
    ("9월 복리 > R0", M["T12"]["comp"] > M["R0"]["comp"]),
    ("PF >= R0", M["T12"]["pf"] >= M["R0"]["pf"] - 1e-9),
    ("MDD 악화 없음", M["T12"]["mdd"] >= M["R0"]["mdd"] - 1e-9),
    ("CHOP PF >= R0", M["T12"]["cpf"] >= M["R0"]["cpf"] - 1e-9),
    ("손실일 증가 없음", M["T12"]["loss"] <= M["R0"]["loss"]),
    ("미도달 손실 <= 추가 수익", -lost <= gain + 1e-9),
    ("bootstrap P(Δ>0) >= 80%", (bs > 0).mean() >= 0.80),
    ("단일거래 기여율 <= 50%", abs(tbig[1]) / max(ttot, 1e-9) <= 0.50),
]
for name, ok in crit:
    P(f"- [{'PASS' if ok else 'FAIL'}] {name}")
P(f"\n통과 {sum(ok for _, ok in crit)}/{len(crit)}")
