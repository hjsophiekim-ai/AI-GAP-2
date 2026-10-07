"""b3stats — B3 TP 민감도 분석. 엔진 재실행 없음(out_*.pkl 만 읽는다)."""
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
SUF = sys.argv[1] if len(sys.argv) > 1 else ""


def load(n):
    p = HERE / f"out_{n}{SUF if n != 'BASE' else ''}.pkl"
    return pickle.load(open(p, "rb"))["trades"] if p.exists() else None


R = {n: load(n) for n in ["BASE", "T10", "T11", "T12", "P3_T10", "P3A_T11", "P3A_T12",
                          "P3B_T11", "P3B_T12"]}
R = {k: v for k, v in R.items() if v is not None}
BASE = R["BASE"]
DATES = sorted({t["date"] for t in BASE})
import datetime as _dt
# 80영업일 = ctx 날짜. 거래 없는 날도 포함해야 하므로 ctx 에서 읽는다.
_ctx = pickle.load(open(HERE / "_ctx80.pkl", "rb"))
DATES = list(_ctx["dates"])
RECENT30 = DATES[-30:]
SEP = [d for d in DATES if d.startswith("202609")]
NONSEP = [d for d in DATES if not d.startswith("202609")]

# BASE 의 진입 regime (섀도우 = BASE 자기 자신)
import bisect
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


def pnl(t):
    return float(t["net_pct"]) * float(t["w1a"])


def key(t):
    return (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])


def comp(ts, days):
    ds = set(days); eq = 1.0
    for t in sorted((x for x in ts if x["date"] in ds), key=lambda x: x["exit_time"]):
        eq *= 1 + pnl(t) / 100
    return (eq - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts):
    eq = 1.0; pk = 1.0; m = 0.0
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        eq *= 1 + pnl(t) / 100; pk = max(pk, eq); m = min(m, eq / pk - 1)
    return m * 100


def excl_top(ts, k):
    top = set(map(id, sorted(ts, key=pnl, reverse=True)[:k]))
    return comp([t for t in ts if id(t) not in top], DATES)


def dayfac(ts):
    f = defaultdict(lambda: 1.0)
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        f[t["date"]] *= 1 + pnl(t) / 100
    return np.array([f[d] for d in DATES])


def chop(ts):
    return [t for t in ts if t.get("entry_regime") == "CHOP"]


def summ(ts):
    return dict(n=len(ts), c80=comp(ts, DATES), r30=comp(ts, RECENT30), sep=comp(ts, SEP),
                nonsep=comp(ts, NONSEP), pf=pf(ts), mdd=mdd(ts),
                wr=100 * sum(pnl(t) > 0 for t in ts) / len(ts),
                avg=np.mean([pnl(t) for t in ts]), avgraw=np.mean([t["net_pct"] for t in ts]),
                chop_pnl=sum(pnl(t) for t in chop(ts)), chop_pf=pf(chop(ts)), chop_n=len(chop(ts)),
                t5=excl_top(ts, 5), t10=excl_top(ts, 10))


def P(*a):
    print(*a, flush=True)


S = {k: summ(v) for k, v in R.items()}

P("=" * 100)
P("§1 B3 단독 비교 (80영업일 %s~%s)" % (DATES[0], DATES[-1]))
P("=" * 100)
hdr = "%-8s %4s %9s %8s %8s %9s %6s %7s %6s %7s %7s | %6s %8s %6s"
P(hdr % ("전략", "n", "80일", "최근30", "9월", "비9월", "PF", "MDD", "WR", "avg", "avgRaw",
         "CHOPn", "CHOP손익", "CHOPPF"))
for k in R:
    s = S[k]
    P("%-8s %4d %9.3f %8.3f %8.3f %9.3f %6.3f %7.3f %6.2f %7.4f %7.4f | %6d %8.3f %6.3f" % (
        k, s["n"], s["c80"], s["r30"], s["sep"], s["nonsep"], s["pf"], s["mdd"], s["wr"],
        s["avg"], s["avgraw"], s["chop_n"], s["chop_pnl"], s["chop_pf"]))

P("\n월별 복리")
months = sorted({d[:6] for d in DATES})
P("%-8s " % "전략" + " ".join("%9s" % m for m in months))
for k in R:
    P("%-8s " % k + " ".join("%9.3f" % comp(R[k], [d for d in DATES if d.startswith(m)]) for m in months))

# ── §2 ─────────────────────────────────────────────────────────────────
BK = {key(t): t for t in BASE}
TPV = {"T10": 1.0, "T11": 1.1, "T12": 1.2}
P("\n" + "=" * 100)
P("§2 runner / 손실 교환관계 (B3 관리 거래 = CHOP 진입)")
P("=" * 100)
for k, tp in TPV.items():
    if k not in R:
        continue
    b3 = [t for t in R[k] if t.get("b3_on")]
    n = len(b3)
    tp_hit = [t for t in b3 if t["exit_reason"] == "GX_TP"]
    sl = [t for t in b3 if t["exit_reason"] == "GX_SL"]
    mh = [t for t in b3 if t["exit_reason"] == "GX_MAXHOLD"]
    oth = [t for t in b3 if t["exit_reason"] not in ("GX_TP", "GX_SL", "GX_MAXHOLD")]
    more = [t for t in tp_hit if key(t) in BK and BK[key(t)]["net_pct"] > t["net_pct"]]
    forgone = sum(pnl(BK[key(t)]) - pnl(t) for t in more)
    miss_loss = [t for t in b3 if t["exit_reason"] != "GX_TP" and t["net_pct"] < 0]
    P(f"{k}: B3관리 {n}건 | TP도달 {len(tp_hit)} ({100*len(tp_hit)/n:.1f}%) | TP전 SL {len(sl)} | "
      f"TP전 max-hold {len(mh)} (그중 손실 {sum(t['net_pct']<0 for t in mh)}) | 기타청산 {len(oth)} "
      f"{dict(pd.Series([t['exit_reason'] for t in oth]).value_counts()) if oth else ''}")
    P(f"      TP후 BASE가 더 간 거래 {len(more)}/{len(tp_hit)} (포기이익 가중 {forgone:+.3f}%p) | "
      f"TP 미도달 후 손실 종료 {len(miss_loss)}건 (가중 {sum(pnl(t) for t in miss_loss):+.3f}%p)")

P("\nMFE 구간별 (BASE CHOP 진입 거래의 MFE 기준, 거래단위 매칭, 가중 net 합)")
bins = [(-99, 1.0, "<1%"), (1.0, 1.1, "1.0~1.1"), (1.1, 1.2, "1.1~1.2"), (1.2, 1.5, "1.2~1.5"),
        (1.5, 3.0, "1.5~3"), (3.0, 999, ">=3%")]
VK = {k: {key(t): t for t in R[k]} for k in R}
bc = chop(BASE)
P("%-8s %4s %9s %9s %9s %9s   (매칭실패 T10/T11/T12)" % ("MFE", "n", "BASE", "T10", "T11", "T12"))
for lo, hi, lab in bins:
    g = [t for t in bc if lo <= t["peak_net_pct"] < hi]
    row = [sum(pnl(t) for t in g)]
    miss = []
    for k in TPV:
        m = [VK[k].get(key(t)) for t in g]
        row.append(sum(pnl(x) for x in m if x is not None)); miss.append(sum(x is None for x in m))
    P("%-8s %4d %9.3f %9.3f %9.3f %9.3f   %s" % (lab, len(g), *row, miss))
tot = [sum(pnl(t) for t in bc)] + [sum(pnl(t) for t in chop(R[k])) for k in TPV]
P("%-8s %4s %9.3f %9.3f %9.3f %9.3f   ← CHOP 전체(매칭무관, 변형 자체의 CHOP 거래)" % ("CHOP계", len(bc), *tot))
for k in TPV:
    new = [t for t in chop(R[k]) if key(t) not in BK]
    P(f"   {k}: BASE에 없는 신규 CHOP 거래 {len(new)}건 가중 {sum(pnl(t) for t in new):+.3f}  "
      f"/ BASE CHOP 중 변형에서 사라진 거래 {sum(key(t) not in VK[k] for t in bc)}건")

# ── §3 ─────────────────────────────────────────────────────────────────
P("\n" + "=" * 100)
P("§3 CHOP runner 보존 (BASE CHOP 진입, MFE 기준)")
P("=" * 100)


def runner_tbl(keys, base_list):
    for thr in (3.0, 5.0, 8.0):
        rs = [t for t in base_list if t["peak_net_pct"] >= thr]
        if not rs:
            P(f"  MFE>={thr:.0f}%: 0건"); continue
        bp = sum(pnl(t) for t in rs)
        line = f"  MFE>={thr:.0f}%: n={len(rs)} BASE가중 {bp:+.3f} |"
        for k in keys:
            m = [(t, VK[k].get(key(t))) for t in rs]
            kept = [x for t, x in m if x is not None and x["net_pct"] >= t["net_pct"] - 0.5]
            cut = [(t, x) for t, x in m if x is None or x["net_pct"] < t["net_pct"] - 0.5]
            vp = sum(pnl(x) for t, x in m if x is not None)
            cl = sum(pnl(t) - (pnl(x) if x is not None else 0.0) for t, x in cut)
            line += (f" {k}: 보존 {len(kept)}/{len(rs)} 이익보존 {100*vp/bp:.1f}% 잘림 {len(cut)} "
                     f"잘린손실 {cl:.3f} |")
        P(line)


runner_tbl(["T10", "T11", "T12"], bc)
P("\n9월 runner 날짜 전량 (BASE MFE>=3% 거래, regime 무관 표기)")
for d in ["20260907", "20260908", "20260909", "20260916", "20260922"]:
    for t in [x for x in BASE if x["date"] == d and x["peak_net_pct"] >= 3.0]:
        s = (f"  {d} {pd.Timestamp(t['entry_time']).strftime('%H:%M')} {t['direction'][:4]} "
             f"regime={t['entry_regime']:6s} MFE {t['peak_net_pct']:.2f} | BASE {t['net_pct']:+.3f} "
             f"({t['exit_reason']})")
        for k in ["T10", "T11", "T12"]:
            x = VK[k].get(key(t))
            if x is None:
                s += f" | {k} (진입소멸)"
            else:
                s += (f" | {k} {x['net_pct']:+.3f} {x['exit_reason']}"
                      f"{' TPhit' if x['exit_reason']=='GX_TP' else ''}"
                      f"{' MAXHOLD' if x['exit_reason']=='GX_MAXHOLD' else ''}")
        P(s)

# ── §4 ─────────────────────────────────────────────────────────────────
P("\n" + "=" * 100)
P("§4 손실 방어 (BASE 대비, 매칭 거래 가중)")
P("=" * 100)
for k in ["T10", "T11", "T12"]:
    red = enl = 0.0; w2l = l2w = 0
    for t in BASE:
        x = VK[k].get(key(t))
        if x is None:
            continue
        b, v = pnl(t), pnl(x)
        if b < 0 and v > b:
            red += min(v, 0) - b if v < 0 else -b
        if v < 0 and v < b:
            enl += (b - v) if b < 0 else -v
        if b > 0 and v < 0:
            w2l += 1
        if b < 0 and v > 0:
            l2w += 1
    worst = min(R[k], key=pnl)
    P(f"{k}: 줄인 손실 {red:.3f}%p | 커진(새로 생긴) 손실 {enl:.3f}%p | 승→패 {w2l} | 패→승 {l2w} | "
      f"최악거래 {worst['date']} {pd.Timestamp(worst['entry_time']).strftime('%H:%M')} "
      f"{worst['net_pct']:+.3f} ({worst['exit_reason']}, w1a {worst['w1a']})")
wb = min(BASE, key=pnl)
P(f"BASE 최악거래 {wb['date']} {pd.Timestamp(wb['entry_time']).strftime('%H:%M')} {wb['net_pct']:+.3f} ({wb['exit_reason']})")

# ── §5 ─────────────────────────────────────────────────────────────────
P("\n" + "=" * 100)
P("§5 강건성")
P("=" * 100)
F = {k: dayfac(v) for k, v in R.items()}
rng = np.random.default_rng(20260928)
IDX = rng.integers(0, len(DATES), size=(10000, len(DATES)))


def boot(a, b):
    ca = np.prod(F[a][IDX], axis=1); cb = np.prod(F[b][IDX], axis=1)
    d = (ca - cb) * 100
    return d.mean(), np.percentile(d, 2.5), np.percentile(d, 97.5), 100 * (d > 0).mean()


def loo(a, b):
    tot_a, tot_b = np.prod(F[a]), np.prod(F[b])
    d = [(tot_a / F[a][i] - tot_b / F[b][i]) * 100 for i in range(len(DATES))]
    return min(d), float(np.median(d)), max(d), sum(x <= 0 for x in d)


def rob(pairs):
    P("%-18s %9s %9s %9s | %8s %8s %8s %5s | %8s %8s %8s %6s" % (
        "비교", "Δ80", "ΔTop5x", "ΔTop10x", "LOOmin", "LOOmed", "LOOmax", "≤0일", "BSmean", "CI2.5", "CI97.5", "P(>0)"))
    for a, b in pairs:
        if a not in R or b not in R:
            continue
        l = loo(a, b); bs = boot(a, b)
        P("%-18s %+9.3f %+9.3f %+9.3f | %+8.3f %+8.3f %+8.3f %5d | %+8.3f %+8.3f %+8.3f %5.1f%%" % (
            f"{a} vs {b}", S[a]["c80"] - S[b]["c80"], S[a]["t5"] - S[b]["t5"], S[a]["t10"] - S[b]["t10"],
            *l, *bs))


P("절대값 Top5 제외 / Top10 제외: " + " | ".join(f"{k} {S[k]['t5']:.2f}/{S[k]['t10']:.2f}" for k in R))
rob([("T10", "BASE"), ("T11", "BASE"), ("T12", "BASE"), ("T11", "T10"), ("T12", "T10"), ("T12", "T11")])

# ── §6 ─────────────────────────────────────────────────────────────────
if "P3_T10" in R:
    P("\n" + "=" * 100)
    P("§6 P3 stack 적용")
    P("=" * 100)
    for k in ["P3_T10", "P3A_T11", "P3A_T12", "P3B_T11", "P3B_T12"]:
        if k not in R:
            continue
        ts = R[k]; s = S[k]
        b3 = [t for t in ts if t.get("b3_on")]
        resc = [t for t in b3 if t.get("b3_rescued")]
        y3 = [t for t in b3 if t.get("b3_y3")]
        pure = {"P3_T10": "T10", "P3A_T11": "T11", "P3A_T12": "T12", "P3B_T11": "T11", "P3B_T12": "T12"}[k]
        fp = []
        for t in resc + y3:
            x = VK[pure].get(key(t))
            if x is not None and x["net_pct"] > t["net_pct"]:
                fp.append((t, x))
        P(f"{k}: n={s['n']} 80일 {s['c80']:.3f} 최근30 {s['r30']:.3f} 9월 {s['sep']:.3f} PF {s['pf']:.3f} "
          f"MDD {s['mdd']:.3f} WR {s['wr']:.2f} CHOPPF {s['chop_pf']:.3f} Top10x {s['t10']:.2f} | "
          f"rescue {len(resc)} Y3 {len(y3)} false-promotion {len(fp)} "
          f"(손해 {sum(pnl(x)-pnl(t) for t,x in fp):.3f}%p)")
    P("\nrunner 보존 (P3 계열)")
    runner_tbl([k for k in ["P3_T10", "P3A_T11", "P3A_T12", "P3B_T11", "P3B_T12"] if k in R], bc)
    rob([("P3A_T11", "P3_T10"), ("P3A_T12", "P3_T10"), ("P3B_T11", "P3_T10"), ("P3B_T12", "P3_T10"),
         ("P3_T10", "BASE")])
    P("\n9월 runner (P3 계열)")
    for d in ["20260907", "20260908", "20260909", "20260916", "20260922"]:
        for t in [x for x in BASE if x["date"] == d and x["peak_net_pct"] >= 3.0]:
            s_ = f"  {d} {pd.Timestamp(t['entry_time']).strftime('%H:%M')} BASE {t['net_pct']:+.3f}"
            for k in ["P3_T10", "P3A_T11", "P3A_T12", "P3B_T11", "P3B_T12"]:
                x = VK.get(k, {}).get(key(t))
                if k in R:
                    s_ += f" | {k} " + ("소멸" if x is None else
                                        f"{x['net_pct']:+.3f} {x['exit_reason']}{' R' if x.get('b3_rescued') else ''}{' Y' if x.get('b3_y3') else ''}")
            P(s_)

pickle.dump(S, open(HERE / f"stats{SUF}.pkl", "wb"))
