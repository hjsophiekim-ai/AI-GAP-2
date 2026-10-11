"""s_stats — TREND 첫 거래 손실크기 연구 (S1/S2 엔진, S3/S4 해석적 size 75%)."""
import copy
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
D = list(pickle.load(open(HERE / "_ctx80.pkl", "rb"))["dates"])
R30 = D[-30:]
MON = {m: [d for d in D if d[:6] == m] for m in ("202607", "202608", "202609")}


def load(n): return pickle.load(open(HERE / f"out_{n}.pkl", "rb"))["trades"]


def pnl(t): return float(t["net_pct"]) * float(t["w1a"])
def key(t): return (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])
def hm(x): return pd.Timestamp(x).strftime("%H:%M")


S0 = load("S0"); S1 = load("S1"); S2 = load("S2")
R0 = load("R0")
same = sorted((key(t), round(t["net_pct"], 8)) for t in S0) == sorted((key(t), round(t["net_pct"], 8)) for t in R0)


def is_first_trend(t): return int(t.get("day_seq", 0)) == 1 and t.get("entry_regime") == "TREND"


def sized(ts, pred):
    out = copy.deepcopy(ts)
    for t in out:
        if pred(t):
            t["w1a"] = t["w1a"] * 0.75
            t["_sized"] = True
    return out


S3 = sized(S0, is_first_trend)
S4 = sized(S2, is_first_trend)
RUNS = {"R0": S0, "S1": S1, "S2": S2, "S3": S3, "S4": S4}


def comp(ts, days):
    ds = set(days); e = 1.0
    for t in sorted((x for x in ts if x["date"] in ds), key=lambda x: x["exit_time"]):
        e *= 1 + pnl(t) / 100
    return (e - 1) * 100


def pf(ts):
    g = sum(pnl(t) for t in ts if pnl(t) > 0); l = -sum(pnl(t) for t in ts if pnl(t) < 0)
    return g / l if l > 0 else float("inf")


def mdd(ts):
    e = pk = 1.0; m = 0.0
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        e *= 1 + pnl(t) / 100; pk = max(pk, e); m = min(m, e / pk - 1)
    return m * 100


def fac(ts):
    f = defaultdict(lambda: 1.0)
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        f[t["date"]] *= 1 + pnl(t) / 100
    return np.array([f[d] for d in D])


def daily(ts): return (fac(ts) - 1) * 100


def excl(ts, k):
    top = set(map(id, sorted(ts, key=pnl, reverse=True)[:k]))
    return comp([t for t in ts if id(t) not in top], D)


def P(*a): print(*a, flush=True)


P(f"R0 재현: S0 == 기존 R0 거래집합 {same} ({len(S0)}거래, {comp(S0, D):.4f})")
tgt = [t for t in S0 if is_first_trend(t)]
wu = [t for t in S0 if int(t.get("day_seq", 0)) == 1 and t.get("entry_regime") == "WARMUP"]
ch = [t for t in S0 if int(t.get("day_seq", 0)) == 1 and t.get("entry_regime") == "CHOP"]
P(f"대상(당일 첫 실제진입 ∧ TREND) {len(tgt)}일 / 첫거래 WARMUP {len(wu)} / CHOP {len(ch)}(제외) / 거래일 {len({t['date'] for t in S0})}")
cap = [sum(t["w1a"] for t in S0 if t["date"] == d) for d in {t["date"] for t in tgt}]
P(f"S3/S4 노출상한 확인: 대상일 당일 w1a 합 최대 {max(cap):.2f} (상한 3.0) → "
  + ("상한 무관, 해석적 정확" if max(cap) < 3.0 - 1e-9 else "⚠ 상한 도달일 있음"))
s1_t = [t for t in S1 if t.get("fs_target")]; s2_t = [t for t in S2 if t.get("fs_target")]
P(f"S1/S2 엔진 대상 표시: S1 {len(s1_t)} / S2 {len(s2_t)} (S0 정의 {len(tgt)})")

P("\n" + "=" * 110)
P("표 1 / 표 2 — 80영업일 %s~%s" % (D[0], D[-1]))
P("=" * 110)
P("%-3s %4s %9s %8s %8s %8s %8s %6s %7s %6s | %4s %5s %6s %6s | %8s" % (
    "전략", "n", "80일", "최근30", "9월", "7월", "8월", "PF", "MDD", "WR", "손실일", "≥2%일", "일평균", "일중앙", "Δ80"))
ST = {}
for k, ts in RUNS.items():
    dd = daily(ts); has = np.array([any(t["date"] == d for t in ts) for d in D])
    ST[k] = dict(c80=comp(ts, D), r30=comp(ts, R30), sep=comp(ts, MON["202609"]), jul=comp(ts, MON["202607"]),
                 aug=comp(ts, MON["202608"]), pf=pf(ts), mdd=mdd(ts), wr=100 * np.mean([pnl(t) > 0 for t in ts]),
                 n=len(ts), loss=int((dd < 0).sum()), ge2=int((dd >= 2).sum()), mean=dd.mean(), med=np.median(dd),
                 std=dd.std())
    s = ST[k]
    P("%-3s %4d %9.3f %8.3f %8.3f %8.3f %8.3f %6.3f %7.3f %6.2f | %4d %5d %6.2f %6.2f | %+8.3f" % (
        k, s["n"], s["c80"], s["r30"], s["sep"], s["jul"], s["aug"], s["pf"], s["mdd"], s["wr"],
        s["loss"], s["ge2"], s["mean"], s["med"], s["c80"] - ST["R0"]["c80"]))

P("\n" + "=" * 110)
P("§6 TREND 첫 거래 성과")
P("=" * 110)
for k, ts in RUNS.items():
    x = [t for t in ts if is_first_trend(t)]
    P(f"  {k}: n={len(x)} WR {100*np.mean([pnl(t)>0 for t in x]):.1f}% avg net {np.mean([t['net_pct'] for t in x]):+.3f} "
      f"avg 가중 {np.mean([pnl(t) for t in x]):+.3f} PF {pf(x):.3f} MFE {np.mean([t['peak_net_pct'] for t in x]):.2f} "
      f"MAE {np.mean([t['mae_net_pct'] for t in x]):+.2f} stop {sum(t['exit_reason']=='TIME_WINDOW_STOP_LOSS' for t in x)} "
      f"runner≥3/5/8 {sum(t['peak_net_pct']>=3 for t in x)}/{sum(t['peak_net_pct']>=5 for t in x)}/{sum(t['peak_net_pct']>=8 for t in x)}")
P("  (참고) WARMUP 첫 거래: n=%d avg net %+.3f WR %.1f%% — 이번 후보는 적용하지 않음"
  % (len(wu), np.mean([t["net_pct"] for t in wu]) if wu else float("nan"),
     100 * np.mean([pnl(t) > 0 for t in wu]) if wu else float("nan")))

P("\n" + "=" * 110)
P("§7 R0 손실일 전량 — 일 복리 %")
P("=" * 110)
DD = {k: dict(zip(D, daily(v))) for k, v in RUNS.items()}
first0 = {t["date"]: t for t in S0 if int(t.get("day_seq", 0)) == 1}
P("  %-8s %-6s %8s %8s | %7s %7s %7s %7s" % ("date", "첫regime", "R0", "첫거래", "S1", "S2", "S3", "S4"))
for d in D:
    if DD["R0"][d] < 0:
        f = first0.get(d)
        P("  %-8s %-6s %+8.2f %+8.2f | %+7.2f %+7.2f %+7.2f %+7.2f" % (
            d, (f or {}).get("entry_regime", "-"), DD["R0"][d], pnl(f) if f else 0,
            DD["S1"][d], DD["S2"][d], DD["S3"][d], DD["S4"][d]))
for k in ("S1", "S2", "S3", "S4"):
    l2p = sum(1 for d in D if DD["R0"][d] < 0 <= DD[k][d])
    red = sum(1 for d in D if DD["R0"][d] < 0 and DD[k][d] > DD["R0"][d] + 1e-9)
    p2l = sum(1 for d in D if DD["R0"][d] >= 0 > DD[k][d])
    ge2lost = sum(1 for d in D if DD["R0"][d] >= 2 > DD[k][d]); ge2gain = sum(1 for d in D if DD["R0"][d] < 2 <= DD[k][d])
    P(f"  {k}: 손실일→흑자 {l2p} | 손실 감소 {red} | 흑자→손실 {p2l} | ≥2% 잃은 날 {ge2lost} / 새로 ≥2% {ge2gain}")

P("\n" + "=" * 110)
P("§8 / 표 3 — runner 손상 (TREND 첫 거래, R0 기준 MFE)")
P("=" * 110)
K = {k: {key(t): t for t in v} for k, v in RUNS.items()}
for thr in (3.0, 5.0, 8.0):
    rs = [t for t in tgt if t["peak_net_pct"] >= thr]
    if not rs:
        P(f"  MFE≥{thr:.0f}: 0건"); continue
    bp = sum(pnl(t) for t in rs)
    line = f"  MFE≥{thr:.0f}: n={len(rs)} R0 가중 {bp:+.3f} |"
    for k in ("S1", "S2", "S3", "S4"):
        m = [(t, K[k].get(key(t))) for t in rs]
        kept = sum(1 for t, x in m if x is not None and x["net_pct"] >= t["net_pct"] - 0.5)
        vp = sum(pnl(x) for t, x in m if x is not None)
        line += f" {k} 보존 {kept}/{len(rs)} ({100*kept/len(rs):.0f}%) 이익 {100*vp/bp:.1f}% 손상 {vp-bp:+.2f} |"
    P(line)
P("  runner 전량 (MFE≥3):")
for t in sorted([t for t in tgt if t["peak_net_pct"] >= 3.0], key=lambda t: t["entry_time"]):
    s = f"   {t['date']} {hm(t['entry_time'])} {t['direction'][:4]} MFE {t['peak_net_pct']:.2f} MAE {t['mae_net_pct']:+.2f} R0 {t['net_pct']:+.3f}"
    for k in ("S1", "S2"):
        x = K[k].get(key(t))
        s += f" | {k} " + ("소멸" if x is None else f"{x['net_pct']:+.3f} {x['exit_reason'][:16]}")
    P(s)

P("\n" + "=" * 110)
P("§9 / 표 4 — 첫 거래 stop 으로 달라진 거래 전량")
P("=" * 110)
for k in ("S1", "S2"):
    ch_ = []
    for kk in sorted(set(K["R0"]) | set(K[k])):
        a, b = K["R0"].get(kk), K[k].get(kk)
        if a is None or b is None or abs(a["net_pct"] - b["net_pct"]) > 1e-9 or a["exit_time"] != b["exit_time"]:
            ch_.append((kk, a, b))
    saved = cut = 0; sv = ct = 0.0
    P(f"  [{k}] 달라진 거래 {len(ch_)}건")
    for kk, a, b in ch_:
        tag = ""
        if a is not None and b is not None and b.get("fs_target"):
            dlt = pnl(b) - pnl(a)
            if dlt >= 0: saved += 1; sv += dlt; tag = "손실축소" if a["net_pct"] < 0 else "개선"
            else: cut += 1; ct += dlt; tag = "RUNNER 절단" if a["peak_net_pct"] >= 3 else "악화"
        elif a is None: tag = "신규(재배치)"
        elif b is None: tag = "소멸(재배치)"
        else: tag = "후속거래 변화(재배치)"
        P(f"     {kk[0]} {kk[1][11:16]} {kk[2][:4]} | R0 {'-' if a is None else f'{a[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]:+.3f} MFE {a[chr(112)+chr(101)+chr(97)+chr(107)+chr(95)+chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]:.2f} MAE {a[chr(109)+chr(97)+chr(101)+chr(95)+chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]:+.2f} {a[chr(101)+chr(120)+chr(105)+chr(116)+chr(95)+chr(114)+chr(101)+chr(97)+chr(115)+chr(111)+chr(110)][:16]}'} "
          f"| {k} {'-' if b is None else f'{b[chr(110)+chr(101)+chr(116)+chr(95)+chr(112)+chr(99)+chr(116)]:+.3f} {b[chr(101)+chr(120)+chr(105)+chr(116)+chr(95)+chr(114)+chr(101)+chr(97)+chr(115)+chr(111)+chr(110)][:16]}'} | {tag}")
    P(f"     → 손실축소/개선 {saved}건 {sv:+.3f} | 악화/runner 절단 {cut}건 {ct:+.3f}")

P("\n" + "=" * 110)
P("§10 size-only (S3)")
P("=" * 110)
diff_path = [t for t in S3 if not t.get("_sized") and (key(t) not in K["R0"] or abs(K["R0"][key(t)]["w1a"] - t["w1a"]) > 1e-12)]
P(f"  경로 동일성: 비대상 거래 w1a/집합 변화 {len(diff_path)}건 (0 이어야 정상)")
lossred = sum(-0.25 * pnl(t) for t in S0 if is_first_trend(t) and pnl(t) < 0)
gainred = sum(-0.25 * pnl(t) for t in S0 if is_first_trend(t) and pnl(t) > 0)
P(f"  손실 감소 {lossred:+.3f}%p | 이익 감소 {gainred:+.3f}%p | 순효과(단순합) {lossred+gainred:+.3f} | "
  f"MDD {ST['R0']['mdd']:.3f} → {ST['S3']['mdd']:.3f} | 일별 표준편차 {ST['R0']['std']:.3f} → {ST['S3']['std']:.3f}")

P("\n" + "=" * 110)
P("§11 S4 분해 (80일 복리, R0 대비)")
P("=" * 110)
A = ST["S2"]["c80"] - ST["R0"]["c80"]; B = ST["S3"]["c80"] - ST["R0"]["c80"]; T = ST["S4"]["c80"] - ST["R0"]["c80"]
P(f"  A stop(S2) {A:+.3f} | B size(S3) {B:+.3f} | C 상호작용 {T-A-B:+.3f} | S4 합 {T:+.3f}")

P("\n" + "=" * 110)
P("§12 강건성 vs R0 (bootstrap 20,000)")
P("=" * 110)
rng = np.random.default_rng(20260928)
IDX = rng.integers(0, len(D), size=(20000, len(D)))
F0 = fac(S0)
for k in ("S1", "S2", "S3", "S4"):
    Fk = fac(RUNS[k])
    loo = [(np.prod(Fk) / Fk[i] - np.prod(F0) / F0[i]) * 100 for i in range(len(D))]
    bs = (np.prod(Fk[IDX], axis=1) - np.prod(F0[IDX], axis=1)) * 100
    tops = " ".join(f"Top{n}x {excl(RUNS[k], n) - excl(S0, n):+.2f}" for n in (1, 3, 5, 10))
    dd = {D[i]: (Fk[i] - F0[i]) * 100 for i in range(len(D)) if abs(Fk[i] - F0[i]) > 1e-12}
    tot = sum(abs(v) for v in dd.values())
    big = max(dd.items(), key=lambda kv: abs(kv[1])) if dd else ("-", 0)
    P(f"  {k}: Δ80 {ST[k]['c80']-ST['R0']['c80']:+.3f} | LOO 음수 {sum(x < 0 for x in loo)}/80 min {min(loo):+.3f} | {tops} | "
      f"bootstrap {bs.mean():+.3f} [{np.percentile(bs,2.5):+.3f},{np.percentile(bs,97.5):+.3f}] P(>0) {100*(bs>0).mean():.1f}% | "
      f"다른 날 {len(dd)}일, 최대 단일일 {big[0]} {big[1]:+.2f} ({100*abs(big[1])/max(tot,1e-9):.0f}%)")

P("\n" + "=" * 110)
P("§13 목표 지표")
P("=" * 110)
r5 = [t for t in S0 if t["peak_net_pct"] >= 5.0]
P("  %-3s %6s %5s %6s %6s | %10s %10s" % ("전략", "≥2%일", "손실일", "일평균", "일중앙", "MFE≥5보존", "이익보존"))
for k, ts in RUNS.items():
    kk = {key(t): t for t in ts}
    kept = sum(1 for t in r5 if key(t) in kk and kk[key(t)]["net_pct"] >= t["net_pct"] - 0.5)
    prof = sum(pnl(kk[key(t)]) for t in r5 if key(t) in kk) / sum(pnl(t) for t in r5) * 100
    s = ST[k]
    P("  %-3s %6d %5d %6.2f %6.2f | %4d/%-2d %3.0f%% %9.1f%%" % (k, s["ge2"], s["loss"], s["mean"], s["med"], kept, len(r5), 100 * kept / len(r5), prof))
pickle.dump(ST, open(HERE / "s_stats.pkl", "wb"))
