import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
from collections import Counter
D = pickle.load(open("_ctx_B.pkl","rb"))["dates"]
W30 = D[-30:]
TS = K.load(); A = K.size_chain(TS, None)
a30 = [t for t in A if t["date"] in set(W30)]
print("="*78); print("최근 30거래일 BASE (N1+C1, BASE sizing, 30M cap)"); print("="*78)
print(f"  창              {W30[0]} ~ {W30[-1]}  ({len(W30)}영업일)")
print(f"  거래수          {len(a30)}")
print(f"  30d realized    {K.krw_pnl(A, W30):,.0f} KRW")
print(f"  PF              {K.pf(A, W30):.3f}")
print(f"  MDD             {K.mdd_krw(A, W30):.2f}%")
print(f"  승률            {sum(1 for t in a30 if t['pnl_krw']>0)/len(a30)*100:.1f}%")
print(f"  예산사용률      {K.budget_stats(A, W30)['util_pct']:.1f}%")
print(f"  runner(peak>=8) {sum(1 for t in a30 if t['peak_net_pct']>=8.0)}건")
print(f"\n  방향: {dict(Counter(t['direction'] for t in a30))}")
print(f"  청산사유 상위: {Counter(t['exit_reason'] for t in a30).most_common(5)}")
print(f"\n  ※ 데이터 상한은 20260918 이다 — 오늘(20260921) 분봉/원장은 로컬에 없다.")
