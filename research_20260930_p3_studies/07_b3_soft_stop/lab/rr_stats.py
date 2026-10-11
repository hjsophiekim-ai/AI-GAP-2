"""rr_stats — 2026-09-30 R1/R2/R3 9월 집계. READ-ONLY (production 무수정).

입력: out_{H30,RL1,RG2,RC3}_d83.pkl  (R0 = H30 = production Q2+H30)
출력: stdout (markdown) -> ../rr_stats.md
"""
import pickle
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
SFX = "_d83"
NAMES = {"R0": "H30", "R1": "RL1", "R2": "RG2", "R3": "RC3"}
T = {k: pickle.load(open(HERE / f"out_{v}{SFX}.pkl", "rb"))["trades"] for k, v in NAMES.items()}

ALLD = sorted({t["date"] for t in T["R0"]})
SEP = [d for d in ALLD if d.startswith("202609") and d <= "20260929"]
SEP16 = [d for d in SEP if d <= "20260928"]


def pnl(t):
    return float(t["net_pct"]) * float(t["w1a"])


def sel(ts, dates):
    ds = set(dates)
    return sorted((t for t in ts if t["date"] in ds), key=lambda x: str(x["exit_time"]))


def compound(ts, dates):
    eq = 1.0
    for t in sel(ts, dates):
        eq *= 1 + pnl(t) / 100
    return (eq - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0)
    l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts, dates):
    eq, peak, m = 1.0, 1.0, 0.0
    for t in sel(ts, dates):
        eq *= 1 + pnl(t) / 100
        peak = max(peak, eq)
        m = min(m, (eq / peak - 1) * 100)
    return m


def daily(ts, dates):
    d = {x: 0.0 for x in dates}
    for t in sel(ts, dates):
        d[t["date"]] += pnl(t)
    return d


def is_chop(t):
    return str(t.get("entry_regime")) == "CHOP"


def is_p3r(t):
    return bool(t.get("b3_rescued") or t.get("b3_ext_prom"))


def key(t):
    return (t["date"], str(t["entry_time"]), t["direction"])


def hm(s):
    return "" if not s else str(s)[11:16]


def summary(ts, dates):
    s = sel(ts, dates)
    dd = daily(ts, dates)
    ch = [t for t in s if is_chop(t)]
    return {"복리": compound(ts, dates), "PF": pf(s), "MDD": mdd(ts, dates),
            "WR": 100 * sum(pnl(t) > 0 for t in s) / max(1, len(s)),
            "CHOP_PF": pf(ch), "CHOP_n": len(ch),
            "손실일": sum(v < 0 for v in dd.values()), "2%+일": sum(v >= 2.0 for v in dd.values()),
            "거래": len(s)}


def p(*a):
    print(*a, flush=True)


# ── 0. R0 재현 ──────────────────────────────────────────────────────────────
anc = pickle.load(open(HERE / "prev" / "out_H30_d83_anchor.pkl", "rb"))["trades"]
sig = lambda ts: sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                         round(float(t["net_pct"]), 8)) for t in ts)
p("# P3-RUNNER profit lock(R1) · reverse guard(R2) — 2026년 9월\n")
p("READ-ONLY 연구. production/config/UI 무수정. R0 = production P3 (B3 + Q2 + H30 + Y3 + N1/C1/H50/ETP).\n")
p("## 0. R0 재현")
p(f"- 9/29 랩 앵커(0527~0929, 83일) 대비 거래 시그니처 일치: **{sig(anc) == sig(T['R0'])}** "
  f"({len(T['R0'])}거래 / 앵커 {len(anc)}거래), 83일 복리 {compound(T['R0'], ALLD):.4f} (앵커 537.5113)")
p(f"- 9월 16영업일(0903~0928) R0 복리 **{compound(T['R0'], SEP16):+.3f}** (기존 9월 연구 앵커 +19.755)")
p(f"- 집계 창: **9월 {len(SEP)}영업일 {SEP[0]}~{SEP[-1]}** (캐시에 0901·0902 없음, 0924~26 추석). 0930 은 OOS.\n")

# ── 5. 9월 성과 ─────────────────────────────────────────────────────────────
p("## 1. 9월 전체 성과 (R0 대비 Δ)")
p("| 전략 | 9월 복리 | Δ | PF | MDD | WR | CHOP PF (n) | 손실일 | 2%+ 일 | 거래 |")
p("|---|---|---|---|---|---|---|---|---|---|")
S = {k: summary(v, SEP) for k, v in T.items()}
for k in ("R0", "R1", "R2", "R3"):
    m = S[k]
    dlt = m["복리"] - S["R0"]["복리"]
    p(f"| {k} | {m['복리']:+.3f} | {dlt:+.3f} | {m['PF']:.3f} | {m['MDD']:.2f} | {m['WR']:.1f}% | "
      f"{m['CHOP_PF']:.3f} ({m['CHOP_n']}) | {m['손실일']} | {m['2%+일']} | {m['거래']} |")
p("")


# ── 강건성 공통 ─────────────────────────────────────────────────────────────
def robust(k):
    d0, d1 = daily(T["R0"], SEP), daily(T[k], SEP)
    dd = {x: d1[x] - d0[x] for x in SEP}
    tot = compound(T[k], SEP) - compound(T["R0"], SEP)
    loo = [compound(T[k], [x for x in SEP if x != y]) - compound(T["R0"], [x for x in SEP if x != y])
           for y in SEP]
    top = sorted(SEP, key=lambda x: dd[x], reverse=True)
    ex1 = [x for x in SEP if x not in top[:1]]
    ex3 = [x for x in SEP if x not in top[:3]]
    rng = random.Random(20260930)
    vals = list(dd.values())
    boot = []
    for _ in range(20000):
        boot.append(sum(rng.choice(vals) for _ in vals))
    boot = np.array(boot)
    # 거래 단위 기여 (같은 진입키 기준, 사라진/생긴 거래 포함)
    a = {key(t): pnl(t) for t in sel(T["R0"], SEP)}
    b = {key(t): pnl(t) for t in sel(T[k], SEP)}
    diffs = {kk: b.get(kk, 0.0) - a.get(kk, 0.0) for kk in set(a) | set(b)}
    diffs = {kk: v for kk, v in diffs.items() if abs(v) > 1e-9}
    s_add = sum(diffs.values())
    big = max(diffs.items(), key=lambda kv: abs(kv[1])) if diffs else (None, 0.0)
    share = abs(big[1]) / abs(s_add) * 100 if s_add else float("nan")
    return {"tot": tot, "loo_neg": sum(v < 0 for v in loo), "loo_min": min(loo), "loo_max": max(loo),
            "ex1": compound(T[k], ex1) - compound(T["R0"], ex1),
            "ex3": compound(T[k], ex3) - compound(T["R0"], ex3),
            "P": float((boot > 0).mean() * 100), "ci": (np.percentile(boot, 2.5), np.percentile(boot, 97.5)),
            "n_changed": len(diffs), "big": big, "share": share, "sum_add": s_add, "dd": dd}


R = {k: robust(k) for k in ("R1", "R2", "R3")}
p("## 2. 강건성 (9월 일 단위, R0 대비) — ⚠ 표본 작음")
p("| 전략 | Δ복리 | day-LOO 음수 | LOO 범위 | Top1일 제외 Δ | Top3일 제외 Δ | bootstrap P(Δ>0) | 95% CI(가산) | 바뀐 거래 | 최대 단일거래 기여 |")
p("|---|---|---|---|---|---|---|---|---|---|")
for k in ("R1", "R2", "R3"):
    r = R[k]
    bk = r["big"][0]
    bks = f"{bk[0][4:]} {hm(bk[1])} {bk[2][:4]} {r['big'][1]:+.2f} ({r['share']:.0f}%)" if bk else "-"
    p(f"| {k} | {r['tot']:+.3f} | {r['loo_neg']}/{len(SEP)} | [{r['loo_min']:+.2f}, {r['loo_max']:+.2f}] | "
      f"{r['ex1']:+.3f} | {r['ex3']:+.3f} | {r['P']:.1f}% | [{r['ci'][0]:+.2f}, {r['ci'][1]:+.2f}] | "
      f"{r['n_changed']} | {bks} |")
p("")

# ── 6. R1 전용 ──────────────────────────────────────────────────────────────
p("## 3. R1 — P3-RUNNER 거래 (9월)")
r0 = {key(t): t for t in sel(T["R0"], SEP)}
r1 = {key(t): t for t in sel(T["R1"], SEP)}
runners = [t for t in r0.values() if is_p3r(t)]
m2 = [t for t in runners if t.get("p2_at")]
gave = [t for t in m2 if t.get("p2_floor_at")]
fired = [t for t in r1.values() if t.get("r1_fired")]
saved = sum(pnl(r1[key(t)]) - pnl(t) for t in runners if key(t) in r1 and pnl(r1[key(t)]) > pnl(t))
cut = sum(pnl(r1[key(t)]) - pnl(t) for t in runners if key(t) in r1 and pnl(r1[key(t)]) < pnl(t))
p(f"- P3-RUNNER {len(runners)}건 · MFE≥2% 도달 {len(m2)}건 · R1 ARM {len(m2)}건 · "
  f"+2% 후 완성봉 +1% 이하 되밀림 {len(gave)}건 · R1 발동 {len(fired)}건")
p(f"- R1이 살린 이익 {saved:+.3f} · R1 때문에 잘린 runner {cut:+.3f} (W1a 가중 %)")
p(f"- R0 합 {sum(pnl(t) for t in runners):+.3f} → R1 합 {sum(pnl(r1[key(t)]) for t in runners if key(t) in r1):+.3f}")
p("\n| 날짜 | 진입 | 방향 | 부분익절 | MFE | +2% 시각 | +1% floor 시각 | R0 net | R1 net | Δ | R0 청산 | R1 청산 | 실현/MFE(R0) |")
p("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for t in sorted(runners, key=key):
    u = r1.get(key(t))
    kind = "Q2 " + hm(t.get("b3_rescue_at")) if t.get("b3_rescued") else "H30"
    ratio = (float(t["net_pct"]) / float(t["peak_net_pct"])) if float(t["peak_net_pct"]) > 0 else float("nan")
    p(f"| {t['date'][4:]} | {hm(t['entry_time'])} | {t['direction'][:4]} | {kind} | {float(t['peak_net_pct']):+.2f} | "
      f"{hm(t.get('p2_at'))} | {hm(t.get('p2_floor_at'))} | {float(t['net_pct']):+.3f} | "
      f"{'' if u is None else f'{float(u['net_pct']):+.3f}'} | "
      f"{'' if u is None else f'{float(u['net_pct']) - float(t['net_pct']):+.3f}'} | {t['exit_reason']} | "
      f"{'' if u is None else u['exit_reason']} | {ratio:.2f} |")
big5 = [t for t in r0.values() if float(t["peak_net_pct"]) >= 5.0]
b5_0 = sum(pnl(t) for t in big5)
b5_1 = sum(pnl(r1[key(t)]) for t in big5 if key(t) in r1)
p(f"\n- MFE≥5% 거래 {len(big5)}건 이익 보존율 R1/R0 = "
  f"{(b5_1 / b5_0 * 100) if b5_0 else float('nan'):.1f}% ({b5_0:+.2f} → {b5_1:+.2f})")
# 오늘 패턴 (+2% → 거의 본전) 전체/러너
near = lambda t: t.get("p2_at") and float(t["net_pct"]) <= 0.5
allpos = sel(T["R0"], SEP)
p(f"- **'+2% 찍고 거의 본전(최종 net ≤ +0.5%)' 사례: P3-RUNNER {sum(bool(near(t)) for t in runners)}건 / "
  f"전체 포지션 {sum(bool(near(t)) for t in allpos)}건** (MFE≥2% 전체 {sum(bool(t.get('p2_at')) for t in allpos)}건)")
for t in allpos:
    if near(t):
        p(f"  - {t['date'][4:]} {hm(t['entry_time'])} {t['direction'][:4]} {t.get('entry_regime')} "
          f"{'P3-RUNNER' if is_p3r(t) else ('Y3' if t.get('b3_y3') else ('B3' if t.get('b3_on') else 'BASE'))} "
          f"MFE {float(t['peak_net_pct']):+.2f} → net {float(t['net_pct']):+.3f} ({t['exit_reason']})")
p("")

# ── 7. R2 전용 ──────────────────────────────────────────────────────────────
p("## 4. R2 — P3-RUNNER 보유 중 반대 플래그 (9월)")
opp_r = [(t, o) for t in runners for o in t.get("opp", []) if "opp" in o]
h50h = [x for x in opp_r if x[1]["h50_hold"]]
noh = [x for x in opp_r if not x[1]["h50_hold"]]
appr = [x for x in noh if x[1]["approved"]]
tr_ok = [x for x in appr if x[1]["n1_trend"]]
g2 = {key(t): t for t in sel(T["R2"], SEP)}
p(f"- 반대 플래그 {len(opp_r)}건 · H50 보류 {len(h50h)}건 · H50 미보류 {len(noh)}건 "
  f"(그중 승인 {len(appr)}건) · 승인+N1 상위추세 유지(=R2 유예 대상) {len(tr_ok)}건")
ign = [(t, o) for t in g2.values() if is_p3r(t) for o in t.get("opp", []) if o.get("r2") == "IGNORED"]
frc = [(t, o) for t in g2.values() if is_p3r(t) for o in t.get("opp", []) if o.get("r2") == "FORCED"]
p(f"- R2 실행: 1봉 뒤에도 추세 유지 → 반대 전환 무시 {len(ign)}건 · 추세 깨짐 → 1봉 늦게 전환 {len(frc)}건")
p("\n| 날짜 | 반대플래그 확정 | 보유 | 60분 range | H50 | N1 추세 | 1봉 뒤 | 판정 | R0 그날 합 | R2 그날 합 | Δ |")
p("|---|---|---|---|---|---|---|---|---|---|---|")
d0, d2 = daily(T["R0"], SEP), daily(T["R2"], SEP)
for t, o in opp_r:
    after = ""
    tt = g2.get(key(t))
    if tt is not None:
        for o2 in tt.get("opp", []):
            if o2.get("idx") == o["idx"] + 1 and o2.get("r2"):
                after = "유지" if o2["r2"] == "IGNORED" else "깨짐"
    res = "H50 보류" if o["h50_hold"] else ("승인·전환" if o["approved"] else f"거절({o['reason'][:18]})")
    rng_ = "-" if o["range_pct"] is None else f"{o['range_pct']:.2f}%"
    p(f"| {t['date'][4:]} | {hm(o['at'])} | {o['held'][:4]} | {rng_} | {o['h50_reason']} | "
      f"{'유지' if o['n1_trend'] else '깨짐'} | {after or '-'} | {res} | {d0[t['date']]:+.3f} | "
      f"{d2[t['date']]:+.3f} | {d2[t['date']] - d0[t['date']]:+.3f} |")

# 오늘 09:48 패턴 — 전 포지션 진단 (R0)
p("\n### 오늘 09:48 패턴 전수 (R0, 보유 종류 무관 · 진단용)")
p("조건: 반대 플래그 승인 → H50 미보류 사유가 RANGE_TOO_WIDE (= 보유방향 EMA20/50 정렬은 통과) → 전환")
nxt = {}
allr0 = sel(T["R0"], ALLD)
for t in allr0:
    nxt.setdefault(t["date"], []).append(t)
cnt = 0
p("| 날짜 | 확정 | 보유 | 종류 | range | N1 추세 | 보유 net | 전환 거래 결과 |")
p("|---|---|---|---|---|---|---|---|")
for t in sel(T["R0"], SEP):
    for o in t.get("opp", []):
        if "opp" not in o or not o["approved"] or o["h50_hold"] or o["h50_reason"] != "RANGE_TOO_WIDE":
            continue
        cnt += 1
        nt = [x for x in nxt[t["date"]] if str(x["entry_time"]) >= str(o["at"]) and x is not t]
        nr = f"{hm(nt[0]['entry_time'])} {nt[0]['direction'][:4]} {float(nt[0]['net_pct']):+.3f} ({nt[0]['exit_reason']})" if nt else "-"
        p(f"| {t['date'][4:]} | {hm(o['at'])} | {o['held'][:4]} | {o['kind']} | {o['range_pct']:.2f}% | "
          f"{'유지' if o['n1_trend'] else '깨짐'} | {o['net']} | {nr} |")
p(f"\n→ 9월 해당 {cnt}건")
