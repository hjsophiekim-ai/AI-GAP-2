import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
from collections import Counter
D = pickle.load(open("_ctx_B.pkl","rb"))["dates"]
TS = K.load(); A = K.size_chain(TS, None); b = K.budget_stats(A, D)
exp = [("거래수", len(A), 158), ("78d realized P/L", round(K.krw_pnl(A,D)), 17_641_769),
       ("PF", round(K.pf(A,D),3), 2.658), ("MDD%", round(K.mdd_krw(A,D),2), -3.08),
       ("평균 예산사용률%", round(b["util_pct"],1), 67.2),
       ("runner(peak>=8%)", sum(1 for t in A if t["peak_net_pct"]>=8.0), 9)]
ok = True
print("=" * 72); print("§0 BASE 앵커"); print("=" * 72)
for n,g,w in exp:
    hit = abs(g-w) < (0.051 if isinstance(w,float) else 1); ok &= hit
    print(f"  {n:20s} {g:>13} / {w:<13} {'OK' if hit else '*** 불일치 ***'}")
print(f"  ==> {'일치' if ok else '불일치 — 중단'}")
print()
print("=" * 72); print("청산사유 분포 (158건)"); print("=" * 72)
for r, c in Counter(t["exit_reason"] for t in A).most_common():
    g = [t for t in A if t["exit_reason"] == r]
    print(f"  {r:34s} {c:3d}건  평균net {sum(x['net_pct'] for x in g)/c:+7.3f}%  "
          f"KRW {sum(x['pnl_krw'] for x in g):+12,.0f}")
etp = [t for t in A if t["exit_reason"] == "EARLY_TAKE_PROFIT"]
print()
print(f"  ** ETP = {len(etp)}건 / 158 = {len(etp)/158*100:.1f}%  "
      f"KRW 기여 {sum(t['pnl_krw'] for t in etp):+,.0f} "
      f"({sum(t['pnl_krw'] for t in etp)/K.krw_pnl(A,D)*100:+.2f}% of BASE) **")
