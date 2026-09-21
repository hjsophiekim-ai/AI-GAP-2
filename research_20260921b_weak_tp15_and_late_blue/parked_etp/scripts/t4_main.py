# -*- coding: utf-8 -*-
"""§0 앵커 · §4 weak/strong 분해 · §9 성과표. READ-ONLY."""
import sys, pickle, statistics as st
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P, t2_regime as RG, t3_tp15 as T

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W30, W70 = D[-30:], D[-70:]
TS = K.load()
A = K.size_chain(TS, None)
b = K.budget_stats(A, D)

print("=" * 78); print("§0  BASE (N1+C1) 앵커"); print("=" * 78)
exp = [("거래수", len(A), 158), ("78d realized P/L", round(K.krw_pnl(A, D)), 17_641_769),
       ("PF", round(K.pf(A, D), 3), 2.658), ("MDD%", round(K.mdd_krw(A, D), 2), -3.08),
       ("평균 예산사용률%", round(b["util_pct"], 1), 67.2),
       ("runner(peak>=8%)", sum(1 for t in A if t["peak_net_pct"] >= 8.0), 9)]
ok = True
for n, g, w in exp:
    hit = abs(g - w) < (0.051 if isinstance(w, float) else 1); ok &= hit
    print(f"  {n:20s} {g:>14} / {w:<14} {'OK' if hit else '*** 불일치 ***'}")
er = {}
for t in A:
    er[t["exit_reason"]] = er.get(t["exit_reason"], 0) + 1
print(f"  청산사유: PP_EXIT(C1)={er.get('PP_EXIT',0)} TP2_FULL={er.get('TIME_WINDOW_TP2_FULL',0)} "
      f"STOP_LOSS={er.get('TIME_WINDOW_STOP_LOSS',0)} OPPOSITE={er.get('OPPOSITE_SIGNAL',0)}")
print(f"  ==> {'앵커 일치 — 연구 진행' if ok else '앵커 불일치 — 중단'}")
if not ok:
    sys.exit(1)

THR = 1.50
print(); print("=" * 118); print(f"§4  weak / strong 분해 (thr={THR}%)"); print("=" * 118)
hdr = (f"{'regime':14s} {'그룹':6s} {'n':>4s} {'평균net':>8s} {'중앙net':>8s} {'승률':>6s} "
       f"{'평균MFE':>8s} {'중앙MFE':>8s} {'평균MAE':>8s} {'도달률':>7s} {'도달후<=0':>8s} "
       f"{'도달후더오름':>10s} {'추가상승':>8s} {'반납':>7s}")
print(hdr); print("-" * 118)
ROWS = {}
for rn, rf in RG.REGIMES.items():
    for tag, sel in (("weak", True), ("strong", False)):
        g = [t for t in TS if bool(rf(t)) == sel]
        if not g:
            continue
        nets = [t["net_pct"] for t in g]
        mfes = [P.mfe_high(t) for t in g]
        maes = [t["mae_net_pct"] for t in g]
        stats = [T.after_touch_stats(t, THR) for t in g]
        hit = [s for s in stats if s]
        ROWS[(rn, tag)] = (g, hit)
        print(f"{rn:14s} {tag:6s} {len(g):4d} {sum(nets)/len(nets):8.3f} {st.median(nets):8.3f} "
              f"{sum(1 for x in nets if x>0)/len(nets)*100:5.1f}% {sum(mfes)/len(mfes):8.3f} "
              f"{st.median(mfes):8.3f} {sum(maes)/len(maes):8.3f} "
              f"{len(hit)/len(g)*100:6.1f}% "
              f"{(sum(1 for s in hit if s['ended_below_0'])/len(hit)*100 if hit else 0):7.1f}% "
              f"{(sum(1 for s in hit if not s['ended_below_thr'])/len(hit)*100 if hit else 0):9.1f}% "
              f"{(sum(s['extra_up'] for s in hit)/len(hit) if hit else 0):8.3f} "
              f"{(sum(s['giveback'] for s in hit)/len(hit) if hit else 0):7.3f}")
    print()
