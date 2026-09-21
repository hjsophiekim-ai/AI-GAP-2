import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
from e5_cmp import SZ, BASE_K, D, A, A78, rows, RUNS
wins = lambda ds,k: [ds[i*(len(ds)//k):(i+1)*(len(ds)//k) if i<k-1 else len(ds)] for i in range(k)]
F = SZ["F  floor 1.25 (더빨리)"]
u = lambda W: K.krw_pnl(F, W) - K.krw_pnl(A, W)
print("F (floor 1.25) 세부")
print(f"  5분할 {', '.join(f'{u(w):+,.0f}' for w in wins(D,5))}")
print(f"  WF6   {', '.join(f'{u(w):+,.0f}' for w in wins(D,6))}")
k5 = [u(w) for w in wins(D,5)]; w6 = [u(w) for w in wins(D,6)]
print(f"  5분할: 양수 {sum(1 for x in k5 if x>0)} / 0 {sum(1 for x in k5 if x==0)} / 음수 {sum(1 for x in k5 if x<0)}")
print(f"  WF6  : 양수 {sum(1 for x in w6 if x>0)} / 0 {sum(1 for x in w6 if x==0)} / 음수 {sum(1 for x in w6 if x<0)}")
am = {(t["date"],t["entry_time"]):t for t in A}
fm = {(t["date"],t["entry_time"]):t for t in F}
print(f"\n  변경 거래 전량:")
print(f"  {'날짜':9s} {'진입':6s} {'A사유':24s} {'A net':>7s} {'F사유':24s} {'F net':>7s} {'ΔKRW':>10s}")
for k in sorted(am):
    if abs(am[k]["pnl_krw"]-fm[k]["pnl_krw"]) > 1e-6:
        print(f"  {k[0]:9s} {k[1][11:16]:6s} {am[k]['exit_reason']:24s} {am[k]['net_pct']:7.3f} "
              f"{fm[k]['exit_reason']:24s} {fm[k]['net_pct']:7.3f} {fm[k]['pnl_krw']-am[k]['pnl_krw']:+10,.0f}")
