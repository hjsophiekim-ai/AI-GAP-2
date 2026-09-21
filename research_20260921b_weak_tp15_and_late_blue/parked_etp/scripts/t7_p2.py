# -*- coding: utf-8 -*-
"""§14 P2 결합 독립성 · Q4 이익원천 분해 · §6 발동거래 전량. READ-ONLY."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, r1_core as R, t2_regime as RG, t3_tp15 as T

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
TS = K.load()
P2 = dict(front=1.05, mo3=0.25, af3=1.00)
sim3 = T.simulate(TS, RG.R3, 1.50)

print("=" * 108)
print("§14  P2 sizing 과의 독립성 (진입집합 4개 모두 동일)")
print("=" * 108)
runs = {
    "A  N1+C1 (BASE sizing)":        R.chain(TS,   front=1.0, mo3=1.0, af3=1.0),
    "B  + WEAK-TP15(R3)":            R.chain(sim3, front=1.0, mo3=1.0, af3=1.0),
    "C  + P2 sizing":                R.chain(TS,   **P2),
    "D  + P2 + WEAK-TP15(R3)":       R.chain(sim3, **P2),
}
a = K.krw_pnl(runs["A  N1+C1 (BASE sizing)"], D)
print(f"{'구성':28s} {'n':>4s} {'30d':>11s} {'70d':>12s} {'78d':>12s} {'vs A':>11s} "
      f"{'PF':>6s} {'MDD%':>6s} {'사용률':>7s}")
print("-" * 108)
for n, r in runs.items():
    b = K.budget_stats(r, D)
    print(f"{n:28s} {len(r):4d} {K.krw_pnl(r, D[-30:]):11,.0f} {K.krw_pnl(r, D[-70:]):12,.0f} "
          f"{K.krw_pnl(r, D):12,.0f} {K.krw_pnl(r, D)-a:+11,.0f} {K.pf(r, D):6.3f} "
          f"{K.mdd_krw(r, D):6.2f} {b['util_pct']:6.1f}%")
tp_base = K.krw_pnl(runs["B  + WEAK-TP15(R3)"], D) - a
p2_only = K.krw_pnl(runs["C  + P2 sizing"], D) - a
both = K.krw_pnl(runs["D  + P2 + WEAK-TP15(R3)"], D) - a
print("-" * 108)
print(f"  TP15 단독 {tp_base:+,.0f} / P2 단독 {p2_only:+,.0f} / 단순합 {tp_base+p2_only:+,.0f} "
      f"/ 실제 결합 {both:+,.0f} / 상호작용 {both-tp_base-p2_only:+,.0f}")
print(f"  P2 위에서의 TP15 증분 = {both - p2_only:+,.0f} KRW  (BASE 위에서는 {tp_base:+,.0f})")
ks = lambda r: [(x["date"], x["entry_time"]) for x in r]
print(f"  진입집합 4개 전부 동일 = {all(ks(r) == ks(runs['A  N1+C1 (BASE sizing)']) for r in runs.values())}")

print()
print("=" * 108)
print("Q4  이익이 '반납 방지' 에서 오는가, 단순히 보유시간 단축인가 (R3, thr 1.50%)")
print("=" * 108)
fired = [x for x in sim3 if x["tp15_fired"]]
gain = [x for x in fired if x["net_pct"] > x["base_net_pct"]]
loss = [x for x in fired if x["net_pct"] < x["base_net_pct"]]
print(f"  발동 {len(fired)}건 = 개선 {len(gain)}건 / 악화 {len(loss)}건")
print(f"    개선 합 {sum(x['net_pct']-x['base_net_pct'] for x in gain):+.2f}%p "
      f"(기존청산이 1.5% 아래로 반납한 건)")
print(f"    악화 합 {sum(x['net_pct']-x['base_net_pct'] for x in loss):+.2f}%p "
      f"(기존청산이 1.5% 위로 더 간 건)")
print(f"    순     {sum(x['net_pct']-x['base_net_pct'] for x in fired):+.2f}%p")
import statistics as st
hb = [x["hold_minutes"] for x in fired]
print(f"  보유시간: 기존 평균 {sum(hb)/len(hb):.1f}분 -> TP15 는 도달 즉시 종료")

print()
print("=" * 108)
print("§6  TP15 발동 거래 전량 (R3, thr 1.50%)")
print("=" * 108)
print(f"{'날짜':9s} {'종목':7s} {'방향':10s} {'진입':6s} {'진입가':>8s} {'도달시각':6s} "
      f"{'도달가':>8s} {'체결가':>8s} {'기존청산':6s} {'기존사유':22s} {'기존%':>7s} "
      f"{'TP15%':>7s} {'차이':>7s} {'runner':>6s}")
print("-" * 108)
RUN8 = {(t["date"], t["entry_time"]) for t in TS if t["peak_net_pct"] >= 8.0}
for x in fired:
    print(f"{x['date']:9s} {x['symbol']:7s} {x['direction']:10s} {x['entry_time'][11:16]:6s} "
          f"{x['entry_price']:8,.0f} {x['tp15_touch_time'][11:16]:6s} {x['tp15_touch_px']:8,.0f} "
          f"{x['tp15_fill_px']:8,.0f} {x['base_exit_time'][11:16]:6s} {x['base_exit_reason']:22s} "
          f"{x['base_net_pct']:7.2f} {x['net_pct']:7.2f} {x['net_pct']-x['base_net_pct']:+7.2f} "
          f"{'Y' if (x['date'], x['entry_time']) in RUN8 else '-':>6s}")
