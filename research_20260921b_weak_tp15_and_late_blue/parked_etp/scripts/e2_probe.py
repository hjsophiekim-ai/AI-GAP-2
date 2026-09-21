import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
from collections import Counter
S = pickle.load(open("_strats.pkl","rb"))
n1 = [t for t in S["N1"] if t.get("net_pct") is not None]
TS = K.load()
print(f"N1 단독 엔진 {len(n1)}건 / N1+C1 원장 {len(TS)}건")
c1 = Counter(t["exit_reason"] for t in n1)
print("  N1 단독 ETP =", c1.get("EARLY_TAKE_PROFIT", 0),
      "| N1+C1 원장 ETP =", sum(1 for t in TS if t["exit_reason"]=="EARLY_TAKE_PROFIT"))
k = lambda t: (t["date"], t["entry_time"])
a = {k(t) for t in n1 if t["exit_reason"]=="EARLY_TAKE_PROFIT"}
b = {k(t) for t in TS if t["exit_reason"]=="EARLY_TAKE_PROFIT"}
print(f"  ETP 거래 집합 동일 = {a == b}  (교집합 {len(a&b)} / N1만 {len(a-b)} / 원장만 {len(b-a)})")
print()
print("  N1+C1 의 ETP 6건 상세 (원장):")
print(f"  {'날짜':9s} {'진입':6s} {'slot':>4s} {'세션':4s} {'chop':>5s} {'MFE':>7s} "
      f"{'net':>7s} {'반납':>7s} {'보유분':>6s} {'청산':6s}")
for t in sorted((x for x in TS if x["exit_reason"]=="EARLY_TAKE_PROFIT"),
                key=lambda x: x["date"]):
    print(f"  {t['date']:9s} {t['entry_time'][11:16]:6s} {t['slot']:4d} "
          f"{('오전' if t['session']=='MORNING' else '오후'):4s} {str(t['entry_chop']):>5s} "
          f"{t['peak_net_pct']:7.3f} {t['net_pct']:7.3f} "
          f"{t['peak_net_pct']-t['net_pct']:7.3f} {t['hold_minutes']:6.0f} "
          f"{t['exit_time'][11:16]:6s}")
print()
print("  C1 는 MFE>=5.0% 에서 arm 한다 -> ETP 6건의 MFE 는 전부",
      f"{max(t['peak_net_pct'] for t in TS if t['exit_reason']=='EARLY_TAKE_PROFIT'):.2f}% 이하")
