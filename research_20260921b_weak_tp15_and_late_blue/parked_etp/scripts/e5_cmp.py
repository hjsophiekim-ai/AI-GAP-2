# -*- coding: utf-8 -*-
"""§4 NO-ETP 대조 · §5/§8 변형·민감도 · §10 runner · §11 의존성. READ-ONLY."""
import sys, pickle, itertools, statistics as st
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W30, W70 = D[-30:], D[-70:]
RUNS = pickle.load(open("_etp_runs.pkl", "rb"))
rng = np.random.default_rng(20260921)
wins = lambda ds, k: [ds[i*(len(ds)//k):(i+1)*(len(ds)//k) if i < k-1 else len(ds)] for i in range(k)]


def rows(ts):
    out = [{
        "date": t["date"], "session": t["session"], "slot": t["slot_number"],
        "direction": t["direction"], "entry_time": t["entry_time"],
        "exit_time": t["exit_time"], "entry_price": t["entry_price"],
        "exit_price": t["exit_price"], "exit_reason": t["exit_reason"],
        "net_pct": t["net_pct"], "w1a_ref": t["w1a"], "peak_net_pct": t["peak_net_pct"],
        "mae_net_pct": t["mae_net_pct"], "entry_chop": t["entry_chop"],
        "tp1_hit": t["tp1_hit"], "hold_minutes": t["hold_minutes"],
        "symbol": "0193T0" if t["direction"] == "UP_RED" else "0197X0",
    } for t in ts if t.get("net_pct") is not None]
    out.sort(key=lambda t: (t["date"], t["entry_time"]))
    return out


SZ = {n: K.size_chain(rows(v), None) for n, v in RUNS.items()}
BASE_K = "A  BASE (chop 1.5/0.8)"
A = SZ[BASE_K]
A78 = K.krw_pnl(A, D)
akeys = [(t["date"], t["entry_time"]) for t in A]
R8A = sum(1 for t in A if t["peak_net_pct"] >= 8.0)

print("=" * 150)
print("§4·§5  ETP 변형 비교 — N1 엔진 (ETP 거래집합이 N1+C1 과 동일함을 확인함), BASE sizing, 30M cap")
print("=" * 150)
print(f"  기준 A: 78d {A78:,.0f} KRW / PF {K.pf(A, D):.3f} / MDD {K.mdd_krw(A, D):.2f}% / runner {R8A}건")
print()
h = (f"{'변형':24s} {'n':>4s} {'ETP':>4s} {'진입diff':>8s} {'30d':>11s} {'70d':>12s} {'78d':>12s} "
     f"{'uplift':>11s} {'PF':>6s} {'MDD%':>6s} {'승률':>6s} {'평균/건':>9s} "
     f"{'-T1':>11s} {'-T3':>11s} {'-T10':>11s} {'run':>4s}")
print(h); print("-" * 150)
for n, r in SZ.items():
    ks = [(t["date"], t["entry_time"]) for t in r]
    diff = len(set(ks) ^ set(akeys))
    ne = sum(1 for t in r if t["exit_reason"] == "EARLY_TAKE_PROFIT")
    print(f"{n:24s} {len(r):4d} {ne:4d} {diff:8d} {K.krw_pnl(r, W30):11,.0f} "
          f"{K.krw_pnl(r, W70):12,.0f} {K.krw_pnl(r, D):12,.0f} {K.krw_pnl(r, D)-A78:+11,.0f} "
          f"{K.pf(r, D):6.3f} {K.mdd_krw(r, D):6.2f} "
          f"{sum(1 for t in r if t['pnl_krw']>0)/len(r)*100:5.1f}% {K.krw_pnl(r, D)/len(r):9,.0f} "
          + " ".join(f"{K.excl_top_krw(r, D, k):11,.0f}" for k in (1, 3, 10))
          + f" {sum(1 for t in r if t['peak_net_pct']>=8.0):4d}")

print()
print("=" * 110)
print("§4b  기간 분할 (BASE 대비 uplift KRW)")
print("=" * 110)
print(f"{'변형':24s} {'앞39':>11s} {'뒤39':>11s} {'5분할':>7s} {'WF6':>6s} "
      f"{'06제외':>11s} {'07제외':>11s} {'08제외':>11s} {'09제외':>11s}")
print("-" * 110)
for n, r in SZ.items():
    if n == BASE_K:
        continue
    u = lambda W: K.krw_pnl(r, W) - K.krw_pnl(A, W)
    hh = wins(D, 2); k5 = [u(w) for w in wins(D, 5)]; w6 = [u(w) for w in wins(D, 6)]
    mo = [u([d for d in D if not d.startswith(m)]) for m in ("202606","202607","202608","202609")]
    print(f"{n:24s} {u(hh[0]):+11,.0f} {u(hh[1]):+11,.0f} "
          f"{sum(1 for x in k5 if x>0)}/5    {sum(1 for x in w6 if x>0)}/6   "
          + " ".join(f"{x:+11,.0f}" for x in mo))

print()
print("=" * 110)
print("§10  runner 영향 · 청산사유 변화")
print("=" * 110)
from collections import Counter
cb = Counter(t["exit_reason"] for t in A)
print(f"{'변형':24s} {'runner':>6s} {'Δrunner':>8s} {'ETP':>4s} {'SL':>4s} {'TP2':>4s} "
      f"{'OPP':>4s} {'trail':>5s} {'기타':>5s}")
print("-" * 110)
for n, r in SZ.items():
    c = Counter(t["exit_reason"] for t in r)
    rk = sum(1 for t in r if t["peak_net_pct"] >= 8.0)
    oth = len(r) - sum(c.get(x, 0) for x in ("EARLY_TAKE_PROFIT", "TIME_WINDOW_STOP_LOSS",
                                             "TIME_WINDOW_TP2_FULL", "OPPOSITE_SIGNAL",
                                             "TIME_WINDOW_TRAILING_STOP"))
    print(f"{n:24s} {rk:6d} {rk-R8A:+8d} {c.get('EARLY_TAKE_PROFIT',0):4d} "
          f"{c.get('TIME_WINDOW_STOP_LOSS',0):4d} {c.get('TIME_WINDOW_TP2_FULL',0):4d} "
          f"{c.get('OPPOSITE_SIGNAL',0):4d} {c.get('TIME_WINDOW_TRAILING_STOP',0):5d} {oth:5d}")

# ── 최우수 후보 심층 ────────────────────────────────────────────────────
cands = {n: K.krw_pnl(r, D) - A78 for n, r in SZ.items() if n != BASE_K}
best = max(cands, key=cands.get)
print()
print("=" * 110)
print(f"§11·§13  최우수 후보 심층 — {best}  (uplift {cands[best]:+,.0f} KRW)")
print("=" * 110)
B = SZ[best]
common = set(akeys) & {(t["date"], t["entry_time"]) for t in B}
am = {(t["date"], t["entry_time"]): t for t in A}
bm = {(t["date"], t["entry_time"]): t for t in B}
d = sorted((bm[k]["pnl_krw"] - am[k]["pnl_krw"] for k in common
            if abs(bm[k]["pnl_krw"] - am[k]["pnl_krw"]) > 1e-6), reverse=True)
if not d:
    print("  변경된 거래 없음")
else:
    tot = sum(d)
    print(f"  손익 달라진 거래 {len(d)} / 순 {tot:+,.0f} | Top1 {d[0]/tot*100:.1f}% "
          f"Top3 {sum(d[:3])/tot*100:.1f}%")
    for k in (1, 3, 5):
        print(f"  -Top{k} 후 {tot-sum(d[:k]):+,.0f}")
    da, db = K.daily_krw(A, D), K.daily_krw(B, D)
    v = np.array([db[x] - da[x] for x in D])
    bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
    print(f"  부트스트랩(일단위) P(>0)={(bt>0).mean()*100:.2f}% p5={np.percentile(bt,5):+,.0f} "
          f"중앙={np.percentile(bt,50):+,.0f} p95={np.percentile(bt,95):+,.0f}")
