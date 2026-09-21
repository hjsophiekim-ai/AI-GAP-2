# -*- coding: utf-8 -*-
"""§16-5 cap 발동 거래 전량 + §2 진입원장 표본 + §3 복리. READ-ONLY."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
TS = K.load()
M = K.MORNING
RUNS = {
    "A 현행":  K.size_chain(TS, None),
    "D P0":   K.size_chain(TS, K.make_extra(1.00, 0.25, 1.00)),
    "E P1":   K.size_chain(TS, K.make_extra(1.02, 0.25, 1.00)),
    "C P2":   K.size_chain(TS, K.make_extra(1.05, 0.25, 1.00)),
    "F P3":   K.size_chain(TS, K.make_extra(1.10, 0.25, 1.00)),
}

print("=" * 112)
print("§16-5  예산 cap 이 실제로 주문을 깎은 거래 전량")
print("=" * 112)
for name, r in RUNS.items():
    hits = [t for t in r if t["budget_capped"]]
    print(f"\n[{name}]  cap 발동 {len(hits)}건 / 총 {len(r)}건   "
          f"잘린 금액 합계 {sum(t['cut_krw'] for t in hits):,.0f} KRW")
    if not hits:
        continue
    print(f"  {'날짜':9s} {'시각':6s} {'sl':>2s} {'세션':4s} {'전략계산':>12s} {'남은예산':>12s} "
          f"{'실제주문':>12s} {'잘림':>11s} {'net%':>7s} {'손익KRW':>11s}")
    for t in hits:
        print(f"  {t['date']:9s} {t['entry_time'][11:16]:6s} {t['slot']:2d} "
              f"{('오전' if t['session']==M else '오후'):4s} {t['strategy_krw']:12,.0f} "
              f"{t['remaining_krw']:12,.0f} {t['actual_krw']:12,.0f} {-t['cut_krw']:+11,.0f} "
              f"{t['net_pct']:7.2f} {t['pnl_krw']:11,.0f}")

print()
print("=" * 112)
print("§17  하루 예산 3,000만원 초과 여부 (절대 위반 0 이어야 함)")
print("=" * 112)
for name, r in RUNS.items():
    worst = 0.0; bad = 0
    for d in D:
        u = sum(t["actual_krw"] for t in r if t["date"] == d)
        worst = max(worst, u)
        if u > K.DAILY_CAPITAL + 1e-6:
            bad += 1
    print(f"  {name:8s} 최대 일사용액 {worst:12,.0f} KRW   초과일수 {bad}")

print()
print("=" * 112)
print("§2  진입원장 표본 (P2, 예산이 깎였거나 오전 slot3 인 거래)")
print("=" * 112)
r = RUNS["C P2"]
sel = [t for t in r if t["budget_capped"] or (t["slot"] == 3 and t["session"] == M)]
print(f"{'날짜':9s} {'시각':6s} {'sl':>2s} {'세션':4s} {'배수':>5s} {'W1a':>5s} {'적용':>5s} "
      f"{'전략금액':>11s} {'잔여':>11s} {'실주문':>11s} {'잘림':>9s} {'수량':>5s} "
      f"{'진입가':>8s} {'net%':>6s} {'손익KRW':>10s} {'방향':>9s}")
print("-" * 112)
for t in sel:
    print(f"{t['date']:9s} {t['entry_time'][11:16]:6s} {t['slot']:2d} "
          f"{('오전' if t['session']==M else '오후'):4s} {t['w1a_extra']:5.2f} "
          f"{t['w1a_rules']:5.2f} {t['w1a_clipped']:5.2f} {t['strategy_krw']:11,.0f} "
          f"{t['remaining_krw']:11,.0f} {t['actual_krw']:11,.0f} {-t['cut_krw']:+9,.0f} "
          f"{t['qty']:5d} {t['entry_price']:8,.0f} {t['net_pct']:6.2f} "
          f"{t['pnl_krw']:10,.0f} {t['direction']:>9s}")

print()
print("=" * 112)
print("§3  복리 수익률 (자기자본이 늘면 기준금액도 같이 늘린다고 볼 때)")
print("=" * 112)
print(f"{'전략':10s} {'78d 단리%':>10s} {'78d 복리%':>10s} {'월환산(21일)':>13s} {'엔진식 복리%':>13s}")
print("-" * 112)
for name, r in RUNS.items():
    simple = K.krw_pnl(r, D) / K.DAILY_CAPITAL * 100
    comp = K.krw_compound(r, D)
    mth = ((1 + comp / 100) ** (21 / len(D)) - 1) * 100
    print(f"{name:10s} {simple:9.2f}% {comp:9.2f}% {mth:12.2f}% {K.compound_pct(r, D):12.2f}%")
print()
print("  주: '엔진식 복리' 는 기존 연구가 쓰던 정의 (노출배수를 자기자본 비율로 간주).")
print("      실제 3,000만원 고정예산 계약에서는 '단리%' 가 현실이고, '복리%' 는")
print("      번 돈을 같은 비율로 재투자했을 때다.")
