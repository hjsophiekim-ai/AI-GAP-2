# -*- coding: utf-8 -*-
"""P2 효과 분해 — '배분' 과 '단순히 돈을 더 썼다' 를 완전히 분리. READ-ONLY."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W30, W70 = D[-30:], D[-70:]
TS = K.load()
M, AF = K.MORNING, K.AFTERNOON
A = K.size_chain(TS, None)
A78 = K.krw_pnl(A, D)
bA = K.budget_stats(A, D)


def row(tag, fn, note=""):
    r = K.size_chain(TS, fn) if not callable(getattr(fn, "trade", None)) else None
    b = K.budget_stats(r, D)
    print(f"{tag:34s} {K.krw_pnl(r, D)-A78:+12,.0f} {K.krw_pnl(r, W70)-K.krw_pnl(A, W70):+12,.0f} "
          f"{K.krw_pnl(r, W30)-K.krw_pnl(A, W30):+11,.0f} {b['util_pct']:6.1f}% "
          f"{b['util_pct']-bA['util_pct']:+6.1f}p {K.pf(r, D):6.3f} {K.mdd_krw(r, D):7.2f}  {note}")
    return K.krw_pnl(r, D) - A78


print("=" * 122)
print("P2 효과 분해 — 기준 A 대비 (DAILY_CAPITAL 3,000만원 cap 항상 적용)")
print("=" * 122)
print(f"  기준 A: 78d {A78:,.0f} KRW / 예산사용률 {bA['util_pct']:.1f}% / "
      f"PF {K.pf(A, D):.3f} / MDD {K.mdd_krw(A, D):.2f}%")
print()
print(f"{'변형':34s} {'78d Δ':>12s} {'70d Δ':>12s} {'30d Δ':>11s} {'사용률':>7s} "
      f"{'Δ사용률':>7s} {'PF':>6s} {'MDD%':>7s}")
print("-" * 122)
alloc = row("① 배분만 (오전s3 0.25, front 1.00)", K.make_extra(1.00, 0.25, 1.00),
            "<- 돈을 더 쓰지 않는다")
lev = row("② 레버리지만 (front 1.05, s3 무변경)", K.make_extra(1.05, 1.00, 1.00),
          "<- slot3 안 건드림")
both = row("③ P2 = ①+② (오전s3 0.25, front 1.05)", K.make_extra(1.05, 0.25, 1.00))
print("-" * 122)
print(f"  ①+② 단순합 = {alloc+lev:+,.0f} / 실제 ③ = {both:+,.0f} / 상호작용 = {both-alloc-lev:+,.0f}")
print(f"  ③ 중 레버리지 기여 = {lev/both*100:.1f}%   배분 기여 = {alloc/both*100:.1f}%")
print(f"  ② 가 이미 주는 것을 빼면 P2 의 순수 배분 기여 = {both-lev:+,.0f} KRW")

print()
print("=" * 122)
print("레버리지를 뺀 비교 — 같은 예산사용률에서 배분만 바꿨을 때")
print("=" * 122)
print(f"{'변형':34s} {'78d Δ':>12s} {'70d Δ':>12s} {'30d Δ':>11s} {'사용률':>7s} "
      f"{'Δ사용률':>7s} {'PF':>6s} {'MDD%':>7s}")
print("-" * 122)
row("오전s3 0.25 만 (front 1.00)", K.make_extra(1.00, 0.25, 1.00), "= P0")
row("오전s3 0.25 + 오후s3 최대흡수", K.make_extra(1.00, 0.25, 1.50),
    "남는 예산을 오후s3 로")
row("오전s3 0.25 + front1.05 + 오후s3흡수", K.make_extra(1.05, 0.25, 1.50))
row("오후s3 만 최대흡수 (s3오전 무변경)", K.make_extra(1.00, 1.00, 1.50),
    "<- 대조: 오전 안 건드림")

print()
print("=" * 122)
print("오전 slot3 를 '건너뛰기'(0) 로 두면? — production MIN 0.25 때문에 불가하지만 참고")
print("=" * 122)
old = K.MIN_MULT
K.MIN_MULT = 0.0
print(f"{'변형':34s} {'78d Δ':>12s} {'70d Δ':>12s} {'30d Δ':>11s} {'사용률':>7s} "
      f"{'Δ사용률':>7s} {'PF':>6s} {'MDD%':>7s}")
print("-" * 122)
row("오전s3 = 0.00 (front 1.00)", K.make_extra(1.00, 0.00, 1.00), "** production 불가 **")
row("오전s3 = 0.00 (front 1.05)", K.make_extra(1.05, 0.00, 1.00), "** production 불가 **")
K.MIN_MULT = old
