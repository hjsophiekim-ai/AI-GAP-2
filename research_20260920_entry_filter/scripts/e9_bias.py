"""E9 -- solo 측정의 편향 보정: 승인 플래그에서 solo vs 실제 차이를 재본다."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
import axlib as A
E1 = pickle.load(open(A.HERE / "e1.pkl", "rb"))
SOLO = {(r["date"], r["at"]): r for r in pickle.load(open(A.HERE / "e3.pkl", "rb")).values()}
rows = []
for t in E1["trades_n1c1"]:
    k = (t["date"], t["entry_time"]); s = SOLO.get(k)
    if not s or s.get("solo_net") is None:
        continue
    rows.append({"date": k[0], "real": float(t["net_pct"]), "solo": float(s["solo_net"]),
                 "real_reason": t["exit_reason"], "solo_reason": s["solo_reason"]})
R = pd.DataFrame(rows); R["d"] = R["real"] - R["solo"]
print(f"승인거래 {len(R)}건에서 solo 측정 편향")
print(f"  solo 합 {R['solo'].sum():+8.2f} / 실제 합 {R['real'].sum():+8.2f} "
      f"/ 편향 합 {R['d'].sum():+8.2f} (평균 {R['d'].mean():+.3f})")
print(f"  solo 와 실제가 정확히 같은 거래 {int((R['d'].abs()<1e-9).sum())}건 "
      f"/ 다른 거래 {int((R['d'].abs()>=1e-9).sum())}건")
X = R[R["d"].abs() >= 1e-9]
print(f"  다른 거래의 편향 평균 {X['d'].mean():+.3f} / 중앙 {X['d'].median():+.3f}")
print("\n  실제 청산사유별 편향 (solo 가 과대평가하는 경로):")
for k, g in sorted(R.groupby("real_reason"), key=lambda kv: kv[1]["d"].sum()):
    print(f"    {k:34s} n={len(g):3d} 편향합 {g['d'].sum():+8.2f} 평균 {g['d'].mean():+6.3f}")
print("\n  solo 에서 OPPOSITE_SIGNAL 이 사라져 과대평가된 정도:")
o = R[R["real_reason"] == "TIME_WINDOW_OPPOSITE_SIGNAL"]
print(f"    실제 OPPOSITE_SIGNAL 청산 {len(o)}건: solo {o['solo'].sum():+.2f} -> 실제 {o['real'].sum():+.2f}")
