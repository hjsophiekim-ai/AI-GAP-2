# -*- coding: utf-8 -*-
"""§9 성과표 · §5 threshold 민감도 · §7 충돌분석. READ-ONLY."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t2_regime as RG, t3_tp15 as T

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W30, W70 = D[-30:], D[-70:]
TS = K.load()
A = K.size_chain(TS, None)
A78 = K.krw_pnl(A, D)
RUN8 = {(t["date"], t["entry_time"]) for t in A if t["peak_net_pct"] >= 8.0}

ALL = lambda t: True
CANDS = dict(RG.REGIMES); CANDS["(대조) 전거래"] = ALL

def build(rf, thr=1.50, **kw):
    sim = T.simulate(TS, rf, thr, **kw)
    return sim, K.size_chain(sim, None)

print("=" * 150)
print("§9  핵심 성과표 — WEAK-TP15 (thr 1.50%), BASE sizing, DAILY_CAPITAL 30,000,000")
print("=" * 150)
h = (f"{'regime':14s} {'weak일':>6s} {'weak거래':>7s} {'발동':>4s} {'30d':>11s} {'70d':>12s} "
     f"{'78d':>12s} {'uplift':>11s} {'PF':>6s} {'MDD%':>6s} {'승률':>6s} {'평균/거래':>9s} "
     f"{'-T1':>11s} {'-T3':>11s} {'-T5':>11s} {'-T10':>11s} {'runner손상':>9s}")
print(h); print("-" * 150)
BEST = {}
for name, rf in CANDS.items():
    sim, r = build(rf, 1.50)
    fired = [x for x in sim if x["tp15_fired"]]
    wk = [x for x in sim if x["regime_weak"]]
    rd = sum(1 for x in fired if (x["date"], x["entry_time"]) in RUN8)
    up = K.krw_pnl(r, D) - A78
    BEST[name] = (sim, r, up)
    print(f"{name:14s} {len({x['date'] for x in wk}):6d} {len(wk):7d} {len(fired):4d} "
          f"{K.krw_pnl(r, W30):11,.0f} {K.krw_pnl(r, W70):12,.0f} {K.krw_pnl(r, D):12,.0f} "
          f"{up:+11,.0f} {K.pf(r, D):6.3f} {K.mdd_krw(r, D):6.2f} "
          f"{sum(1 for x in r if x['pnl_krw']>0)/len(r)*100:5.1f}% {K.krw_pnl(r, D)/len(r):9,.0f} "
          + " ".join(f"{K.excl_top_krw(r, D, k):11,.0f}" for k in (1,3,5,10))
          + f" {rd:9d}")

print()
print("=" * 120)
print("§5  threshold 민감도 (plateau 확인용, 최고점 탐색 아님)")
print("=" * 120)
print(f"{'regime':14s} " + " ".join(f"{t:>13s}" for t in ("+1.25%","+1.50%","+1.75%","+2.00%")))
print("-" * 120)
for name, rf in CANDS.items():
    vals = []
    for thr in (1.25, 1.50, 1.75, 2.00):
        _, r = build(rf, thr)
        vals.append(K.krw_pnl(r, D) - A78)
    print(f"{name:14s} " + " ".join(f"{v:+13,.0f}" for v in vals))

print()
print("=" * 120)
print("§7  기존 N1/C1 청산 대체 분석 (thr 1.50%)")
print("=" * 120)
print(f"{'regime':14s} {'발동':>4s} {'TP2대체':>7s} {'C1(PP)대체':>9s} {'손절전익절':>9s} "
      f"{'반대신호대체':>10s} {'기타':>5s} {'runner손상':>9s} {'runner포기손익':>13s}")
print("-" * 120)
for name, rf in CANDS.items():
    sim, _, _ = BEST[name]
    fired = [x for x in sim if x["tp15_fired"]]
    cat = lambda rs: sum(1 for x in fired if x["base_exit_reason"] == rs)
    rd = [x for x in fired if (x["date"], x["entry_time"]) in RUN8]
    lost = sum(x["base_net_pct"] - x["net_pct"] for x in rd)
    other = len(fired) - cat("TIME_WINDOW_TP2_FULL") - cat("PP_EXIT") - cat("TIME_WINDOW_STOP_LOSS") - cat("OPPOSITE_SIGNAL")
    print(f"{name:14s} {len(fired):4d} {cat('TIME_WINDOW_TP2_FULL'):7d} {cat('PP_EXIT'):9d} "
          f"{cat('TIME_WINDOW_STOP_LOSS'):9d} {cat('OPPOSITE_SIGNAL'):10d} {other:5d} "
          f"{len(rd):9d} {lost:12.2f}%p")
