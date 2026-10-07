"""hv_stats — 2026-09-30 (2차) 고변동 reverse / partial profit lock 9월 집계. READ-ONLY."""
import pickle
import random
import statistics
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
from app.trading.macd2.worker import _net_return_pct  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
import tregime as tr  # noqa: E402

NAMES = {"R0": "H30", "A": "RG2", "B": "EXO", "C30": "C30", "C50": "C50",
         "A+C30": "A_C30", "A+C50": "A_C50", "B+C30": "B_C30", "B+C50": "B_C50"}
T = {k: pickle.load(open(HERE / f"out_{v}_d83.pkl", "rb"))["trades"] for k, v in NAMES.items()}
CTX = pickle.load(open(HERE / "_ctx83.pkl", "rb"))
BARS = CTX["hynix_bars_3m"].reset_index(drop=True)
ALLD = sorted({t["date"] for t in T["R0"]})
SEP = [d for d in ALLD if d.startswith("202609")]
SEP16 = [d for d in SEP if d <= "20260928"]
SYM = {"UP_RED": "0193T0", "DOWN_BLUE": "0197X0"}
TAGF = {"UP_RED": "long", "DOWN_BLUE": "inverse"}
_etf = {}


def etf(date, direction):
    k = (date, direction)
    if k not in _etf:
        f = HERE / "cache83" / f"replay_{date}_{TAGF[direction]}_1m.csv"
        df = pd.read_csv(f, parse_dates=["datetime"]).set_index("datetime")
        _etf[k] = df
    return _etf[k]


pnl = lambda t: float(t["net_pct"]) * float(t["w1a"])
key = lambda t: (t["date"], str(t["entry_time"]), t["direction"])
hm = lambda s: "" if not s else str(s)[11:16]
is_p3r = lambda t: bool(t.get("b3_rescued") or t.get("b3_ext_prom"))
is_chop = lambda t: str(t.get("entry_regime")) == "CHOP"
p = lambda *a: print(*a, flush=True)


def sel(ts, dates):
    ds = set(dates)
    return sorted((t for t in ts if t["date"] in ds), key=lambda x: str(x["exit_time"]))


def compound(ts, dates):
    eq = 1.0
    for t in sel(ts, dates):
        eq *= 1 + pnl(t) / 100
    return (eq - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts, dates):
    eq = pk = 1.0; m = 0.0
    for t in sel(ts, dates):
        eq *= 1 + pnl(t) / 100; pk = max(pk, eq); m = min(m, (eq / pk - 1) * 100)
    return m


def daily(ts, dates):
    d = {x: 0.0 for x in dates}
    for t in sel(ts, dates):
        d[t["date"]] += pnl(t)
    return d


def diffs(a, b, dates):
    A = {key(t): t for t in sel(a, dates)}; B = {key(t): t for t in sel(b, dates)}
    out = []
    for k in sorted(set(A) | set(B)):
        x, y = A.get(k), B.get(k)
        dx = 0.0 if x is None else pnl(x); dy = 0.0 if y is None else pnl(y)
        if abs(dy - dx) > 1e-9:
            out.append((k, x, y, dy - dx))
    return out


def desc(t):
    if t is None:
        return "없음"
    return f"{hm(t['exit_time'])} {t['exit_reason'][:22]} {float(t['net_pct']):+.3f}×{float(t['w1a']):.2f}"


# ── 0 재현 ──
anc = pickle.load(open(HERE / "prev" / "out_H30_d83_anchor.pkl", "rb"))["trades"]
s1g = pickle.load(open(HERE / "prev2" / "out_RG2_d83_study1.pkl", "rb"))["trades"]
sig = lambda ts: sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                         round(float(t["net_pct"]), 8)) for t in ts)
p("# P3 고변동장 대응 종합 연구 — 2026년 9월 (2026-09-30)\n")
p("READ-ONLY. production/config/UI 무수정. 랩 = lab/ (patch_r1r2.py + patch_exo_plock.py 훅, 모두 opt-in).\n")
p("## 0. R0 재현")
p(f"- R0(83일) == 9/29 앵커: **{sig(T['R0']) == sig(anc)}** ({len(T['R0'])}거래, 복리 {compound(T['R0'], ALLD):.4f} / 앵커 537.5113)")
p(f"- A(R2) == 1차 연구 RG2: **{sig(T['A']) == sig(s1g)}** (새 훅 추가 후에도 불변)")
p(f"- 9월 16일(0903~0928) R0 {compound(T['R0'], SEP16):+.3f} (앵커 +19.755) · 집계창 9월 {len(SEP)}영업일 {SEP[0]}~{SEP[-1]}\n")

# ── 6 성과 ──
p("## 1. 9월 성과")
p("| 전략 | 9월 복리 | Δ | PF | MDD | WR | CHOP PF (n) | 손실일 | 2%+일 | 거래 | 평균 일수익 | 중앙 일수익 |")
p("|---|---|---|---|---|---|---|---|---|---|---|---|")
S = {}
for k, ts in T.items():
    s = sel(ts, SEP); dd = daily(ts, SEP); ch = [t for t in s if is_chop(t)]
    S[k] = dict(c=compound(ts, SEP), pf=pf(s), mdd=mdd(ts, SEP), wr=100 * sum(pnl(t) > 0 for t in s) / len(s),
                cpf=pf(ch), cn=len(ch), ld=sum(v < 0 for v in dd.values()), d2=sum(v >= 2 for v in dd.values()),
                n=len(s), avg=statistics.mean(dd.values()), med=statistics.median(dd.values()))
for k, m in S.items():
    p(f"| {k} | {m['c']:+.3f} | {m['c'] - S['R0']['c']:+.3f} | {m['pf']:.3f} | {m['mdd']:.2f} | {m['wr']:.1f}% | "
      f"{m['cpf']:.3f} ({m['cn']}) | {m['ld']} | {m['d2']} | {m['n']} | {m['avg']:+.3f} | {m['med']:+.3f} |")
p("")


# ── 13 강건성 ──
def robust(k):
    d0, d1 = daily(T["R0"], SEP), daily(T[k], SEP)
    dd = {x: d1[x] - d0[x] for x in SEP}
    tot = compound(T[k], SEP) - compound(T["R0"], SEP)
    loo = [compound(T[k], [x for x in SEP if x != y]) - compound(T["R0"], [x for x in SEP if x != y]) for y in SEP]
    top = sorted(SEP, key=lambda x: dd[x], reverse=True)
    ex = lambda n: (compound(T[k], [x for x in SEP if x not in top[:n]])
                    - compound(T["R0"], [x for x in SEP if x not in top[:n]]))
    rng = random.Random(20260930); vals = list(dd.values())
    boot = np.array([sum(rng.choice(vals) for _ in vals) for _ in range(20000)])
    df = diffs(T["R0"], T[k], SEP)
    s_add = sum(x[3] for x in df)
    big = max(df, key=lambda x: abs(x[3])) if df else None
    share = abs(big[3]) / abs(s_add) * 100 if (big and s_add) else float("nan")
    nz = sum(abs(v) > 1e-9 for v in vals)
    return dict(tot=tot, loo=sum(v < 0 for v in loo), lmin=min(loo), lmax=max(loo), ex1=ex(1), ex3=ex(3),
                P=(boot > 0).mean() * 100, n=len(df), big=big, share=share, days=nz)


p("## 2. 강건성 (9월 17일, R0 대비) — ⚠ 표본 작음: 바뀐 날/거래 수를 함께 볼 것")
p("| 전략 | Δ복리 | 바뀐 날 | 바뀐 거래 | day-LOO 음수 | LOO 범위 | Top1 제외 Δ | Top3 제외 Δ | P(Δ>0) | 최대 단일거래 기여 |")
p("|---|---|---|---|---|---|---|---|---|---|")
RB = {}
for k in list(NAMES)[1:]:
    r = RB[k] = robust(k)
    b = r["big"]
    bs = f"{b[0][0][4:]} {hm(b[0][1])} {b[0][2][:4]} {b[3]:+.2f} ({r['share']:.0f}%)" if b else "-"
    p(f"| {k} | {r['tot']:+.3f} | {r['days']} | {r['n']} | {r['loo']}/{len(SEP)} | [{r['lmin']:+.2f}, {r['lmax']:+.2f}] | "
      f"{r['ex1']:+.3f} | {r['ex3']:+.3f} | {r['P']:.1f}% | {bs} |")
p("")

# ── 거래 단위 변화 (11 결합효과 포함) ──
p("## 3. 후보별 바뀐 거래 (R0 대비, W1a 가중 %)")
for k in list(NAMES)[1:]:
    df = diffs(T["R0"], T[k], SEP)
    p(f"- **{k}** ({len(df)}건, 합 {sum(x[3] for x in df):+.3f})")
    for kk, x, y, d in df:
        p(f"  - {kk[0][4:]} {hm(kk[1])} {kk[2][:4]}: R0 {desc(x)} → {desc(y)} (Δ {d:+.3f})")
p("")
p("### 결합 상호작용 (결합 Δ vs 단일 Δ 합)")
p("| 결합 | 결합 Δ | 단일 합 | 상호작용 |")
p("|---|---|---|---|")
for c, (a, b) in {"A+C30": ("A", "C30"), "A+C50": ("A", "C50"), "B+C30": ("B", "C30"), "B+C50": ("B", "C50")}.items():
    ca = RB[c]["tot"]; sa = RB[a]["tot"] + RB[b]["tot"]
    p(f"| {c} | {ca:+.3f} | {sa:+.3f} | {ca - sa:+.3f} |")
p("")


# ── 7 고변동 반대신호 ──
def held_keep_mfe(t, at):
    df = etf(t["date"], t["direction"])
    a = pd.Timestamp(at).tz_localize(None)
    w = df.loc[(df.index > a) & (df.index <= a + pd.Timedelta(minutes=60)), "high"]
    if w.empty:
        return None
    return max(_net_return_pct(SYM[t["direction"]], float(t["entry_price"]), float(h), 1000) for h in w)


r0d = {t["date"]: [] for t in T["R0"]}
for t in T["R0"]:
    r0d[t["date"]].append(t)
events = []
for t in sel(T["R0"], SEP):
    for o in t.get("opp", []):
        if "opp" not in o:
            continue
        rv = next((x for x in r0d[t["date"]] if str(x["entry_time"]) == o["at"] and x["direction"] == o["opp"]), None)
        events.append((t, o, rv))
hv = [e for e in events if e[1]["range_pct"] is not None and e[1]["range_pct"] > 2.35]
p("## 4. 고변동 반대신호 (확정 시점 60분 range > 2.35%, 보유 종류 무관, R0 기준)")
rev = [e for e in hv if e[2] is not None]
p(f"- 반대신호 {len(hv)}건 · 그중 즉시 reverse {len(rev)}건 · false reverse(반대거래 net≤0) "
  f"{sum(float(e[2]['net_pct']) <= 0 for e in rev)}건 · true reversal {sum(float(e[2]['net_pct']) > 0 for e in rev)}건 · "
  f"reverse 후 10분 내 손절 {sum((e[2]['exit_reason'] in ('GX_SL', 'TIME_WINDOW_STOP_LOSS')) and (pd.Timestamp(e[2]['exit_time']) - pd.Timestamp(e[2]['entry_time'])).total_seconds() <= 600 for e in rev)}건")
p("\n| 날짜 | 확정 | 보유 | 종류 | range | H50 | 판정 | N1(보유) | 보유 net | 보유 유지 시 60분 MFE | 반대거래 | 반대 MFE | R0 그날 | A | B | C30 | C50 |")
p("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
DD = {k: daily(v, SEP) for k, v in T.items()}
for t, o, rv in hv:
    km = held_keep_mfe(t, o["at"])
    res = "H50 보류" if o["h50_hold"] else ("승인→reverse" if rv is not None else ("승인" if o["approved"] else f"거절·청산"))
    p(f"| {t['date'][4:]} | {hm(o['at'])} | {o['held'][:4]} | {o['kind']} | {o['range_pct']:.2f}% | {o['h50_reason']} | {res} | "
      f"{'유지' if o['n1_trend'] else '깨짐'} | {o['net']} | {'' if km is None else f'{km:+.2f}'} | "
      f"{'' if rv is None else desc(rv)} | {'' if rv is None else f'{float(rv['peak_net_pct']):+.2f}'} | "
      f"{DD['R0'][t['date']]:+.3f} | {DD['A'][t['date']] - DD['R0'][t['date']]:+.3f} | {DD['B'][t['date']] - DD['R0'][t['date']]:+.3f} | "
      f"{DD['C30'][t['date']] - DD['R0'][t['date']]:+.3f} | {DD['C50'][t['date']] - DD['R0'][t['date']]:+.3f} |")
p("\n(A/B/C 열 = 그날 합의 R0 대비 Δ)")
rtw = [e for e in events if e[1]["h50_reason"] == "RANGE_TOO_WIDE" and e[1]["approved"]]
p(f"\n### H50 이 RANGE_TOO_WIDE 로 보류 실패한 승인 반대신호: {len(rtw)}건")
for t, o, rv in rtw:
    p(f"- {t['date'][4:]} {hm(o['at'])} 보유 {o['held'][:4]} {o['kind']} range {o['range_pct']:.2f}% N1 {'유지' if o['n1_trend'] else '깨짐'} "
      f"보유 net {o['net']} → 반대거래 {desc(rv)}")
p("")

# ── 8 R2 전용 ──
p("## 5. A (R2) 전용 — P3-RUNNER + 승인 반대신호 + H50 미보류")
tg = [(t, o) for t in sel(T["A"], SEP) if is_p3r(t) for o in t.get("opp", []) if o.get("r2") == "DEFER"]
for t, o in tg:
    aft = next((o2["r2"] for o2 in t["opp"] if o2.get("idx") == o["idx"] + 1 and o2.get("r2") in ("IGNORED", "FORCED")), "-")
    p(f"- {t['date'][4:]} {hm(o['at'])} 보유 {o['held'][:4]} 반대 {o['opp'][:4]} range {o['range_pct']:.2f}% H50 {o['h50_reason']} "
      f"N1 유지 → 1봉 뒤 {'유지(전환 무시)' if aft == 'IGNORED' else '깨짐(1봉 늦게 전환)'} · 그날 Δ {DD['A'][t['date']] - DD['R0'][t['date']]:+.3f}")
dfA = diffs(T["R0"], T["A"], SEP)
p(f"- 좋아진 거래 {sum(x[3] > 0 for x in dfA)}건 (+{sum(x[3] for x in dfA if x[3] > 0):.3f}) · "
  f"나빠진 거래 {sum(x[3] < 0 for x in dfA)}건 ({sum(x[3] for x in dfA if x[3] < 0):+.3f})")
p(f"- false reverse 회피: {sum(1 for t, o in tg if any(o2.get('r2') == 'IGNORED' and o2['idx'] == o['idx'] + 1 for o2 in t['opp']))}건 · "
  f"true reversal 지연: {sum(1 for t, o in tg if any(o2.get('r2') == 'FORCED' and o2['idx'] == o['idx'] + 1 for o2 in t['opp']))}건\n")

# ── 9 EXIT-ONLY ──
p("## 6. B (EXIT-ONLY) — 즉시 reverse 전 사례 진단 (R0, 보유 종류 무관)")
p("1봉 뒤 반대방향 N1 추세(close/EMA20 vs EMA50/slope)로 진입 여부를 본 **해석적 진단**. 실제 후보 B 는 P3-RUNNER 에만 적용(아래 시뮬).")
p("| 날짜 | 확정 | 보유→반대 | 종류 | range | 보유 청산 | 즉시 반대거래 | 1봉 뒤 반대 N1 | 즉시 진입가 | 1봉 뒤 가격 | 판정 |")
p("|---|---|---|---|---|---|---|---|---|---|---|")
allrev = [e for e in events if e[2] is not None]
cntA = cntB = cntC = cntD = 0
approx = 0.0
for t, o, rv in allrev:
    i = int(o["idx"])
    snap = tr.snapshot(BARS.iloc[: i + 2], Direction(o["opp"]))
    df = etf(rv["date"], rv["direction"])
    a2 = pd.Timestamp(o["at"]).tz_localize(None) + pd.Timedelta(minutes=3)
    px2 = float(df["open"].get(a2, np.nan))
    sl = rv["exit_reason"] in ("GX_SL", "TIME_WINDOW_STOP_LOSS")
    cntA += sl
    if not snap.ok:
        verdict = "진입 취소"
        if sl:
            cntB += 1; verdict += " · 손절 회피"
        elif float(rv["net_pct"]) > 0:
            cntC += 1; verdict += " · 좋은 진입 놓침"
        approx -= pnl(rv)
    else:
        worse = px2 > float(rv["entry_price"])
        verdict = "1봉 뒤 진입" + (" · 진입가 불리" if worse else " · 진입가 유리")
        if worse and float(rv["net_pct"]) > 0:
            cntD += 1
        approx -= (px2 / float(rv["entry_price"]) - 1) * 100 * float(rv["w1a"])
    p(f"| {t['date'][4:]} | {hm(o['at'])} | {o['held'][:4]}→{o['opp'][:4]} | {o['kind']} | {o['range_pct'] or 0:.2f}% | "
      f"{desc(t)} | {desc(rv)} | {'확인' if snap.ok else '미확인'} | {float(rv['entry_price']):,.0f} | {px2:,.0f} | {verdict} |")
p(f"\nA. 즉시 reverse 후 손절 {cntA}건 · B. 1봉 기다렸으면 피한 손절 {cntB}건 · C. 기다려서 놓친 좋은 진입 {cntC}건 · "
  f"D. 늦은 진입으로 수익 감소 {cntD}건 · E. 해석적 net 효과(근사, 취소=반대거래 제거 / 진입=가격차만 반영) {approx:+.3f}")
dfB = diffs(T["R0"], T["B"], SEP)
p(f"\n실제 후보 B 시뮬(P3-RUNNER 한정): 바뀐 거래 {len(dfB)}건, 합 {sum(x[3] for x in dfB):+.3f}")
for t in sel(T["B"], SEP):
    if t.get("exo_exit"):
        p(f"- {t['date'][4:]} {hm(t['entry_time'])} {t['direction'][:4]} 청산 {desc(t)} → 반대진입 {t.get('exo_result')}")
p("")

# ── 10 PARTIAL LOCK ──
p("## 7. C30/C50 — P3-RUNNER 중 MFE ≥ +2% 전부")
p("| 날짜 | 진입 | Q2 | MFE | +2% | 보호 실행(C) | R0 net | C30 net | C50 net | R0 청산 | 실현/MFE R0 |")
p("|---|---|---|---|---|---|---|---|---|---|---|")
M = {k: {key(t): t for t in sel(T[k], SEP)} for k in ("R0", "C30", "C50")}
rn = [t for t in M["R0"].values() if is_p3r(t) and t.get("p2_at")]
for t in sorted(rn, key=key):
    c3, c5 = M["C30"].get(key(t)), M["C50"].get(key(t))
    p(f"| {t['date'][4:]} | {hm(t['entry_time'])} | {hm(t.get('b3_rescue_at')) or 'H30'} | {float(t['peak_net_pct']):+.2f} | "
      f"{hm(t.get('p2_at'))} | {hm((c3 or {}).get('plock_at'))} {'' if not (c3 or {}).get('plock_net') else f'({c3['plock_net']:+.2f})'} | "
      f"{float(t['net_pct']):+.3f} | {'' if c3 is None else f'{float(c3['net_pct']):+.3f}'} | "
      f"{'' if c5 is None else f'{float(c5['net_pct']):+.3f}'} | {t['exit_reason']} | "
      f"{float(t['net_pct']) / float(t['peak_net_pct']):.2f} |")
for k in ("C30", "C50"):
    df = diffs(T["R0"], T[k], SEP)
    gain = sum(x[3] for x in df if x[3] > 0); loss = sum(x[3] for x in df if x[3] < 0)
    pres = []
    for th in (3, 5, 8):
        base = [t for t in M["R0"].values() if float(t["peak_net_pct"]) >= th]
        b0 = sum(pnl(t) for t in base); b1 = sum(pnl(M[k][key(t)]) for t in base if key(t) in M[k])
        pres.append(f"MFE≥{th}% {len(base)}건 {b1 / b0 * 100 if b0 else float('nan'):.0f}%")
    p(f"- {k}: 보호 이익 {gain:+.3f} · runner 희생 {loss:+.3f} · 보존율 " + " / ".join(pres))
p("")
