# -*- coding: utf-8 -*-
"""§6 민감도 격자 — 오전 slot3 배수 x slot1/2 증액. DAILY_CAPITAL cap 항상 적용."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W30, W70 = D[-30:], D[-70:]
TS = K.load()
A = K.size_chain(TS, None)
A78, A30, A70 = K.krw_pnl(A, D), K.krw_pnl(A, W30), K.krw_pnl(A, W70)
AT10 = K.excl_top_krw(A, D, 10)

MO3 = [0.00, 0.10, 0.25, 0.40, 0.50, 0.75, 1.00]
FRONT = [1.00, 1.02, 1.05, 1.08, 1.10]


def grid(min_mult):
    K.MIN_MULT = min_mult
    print(f"\n{'=' * 118}")
    tag = ("production 그대로 (MIN_MULT=0.25)" if min_mult == 0.25
           else f"MIN_MULT 완화 = {min_mult}  ** production 상수 아님, 참고용 **")
    print(f"§6  민감도 격자 — {tag}")
    print("=" * 118)
    print(f"{'오전s3':>6s} {'front':>6s} {'78d Δ':>11s} {'70d Δ':>11s} {'30d Δ':>10s} "
          f"{'78d KRW':>12s} {'PF':>6s} {'MDD%':>6s} {'-T10 Δ':>11s} {'cap':>4s} {'사용률':>6s}")
    print("-" * 118)
    best = None
    cells = {}
    for m in MO3:
        for f in FRONT:
            r = K.size_chain(TS, K.make_extra(f, m, 1.00))
            b = K.budget_stats(r, D)
            d78 = K.krw_pnl(r, D) - A78
            cells[(m, f)] = d78
            mark = ""
            if abs(m - 0.25) < 1e-9 and abs(f - 1.05) < 1e-9:
                mark = "  <== P2"
            print(f"{m:6.2f} {f:6.2f} {d78:+11,.0f} {K.krw_pnl(r, W70)-A70:+11,.0f} "
                  f"{K.krw_pnl(r, W30)-A30:+10,.0f} {K.krw_pnl(r, D):12,.0f} "
                  f"{K.pf(r, D):6.3f} {K.mdd_krw(r, D):6.2f} "
                  f"{K.excl_top_krw(r, D, 10)-AT10:+11,.0f} {b['cap_hits']:4d} "
                  f"{b['util_pct']:5.1f}%{mark}")
            if best is None or d78 > best[0]:
                best = (d78, m, f)
        print()
    print(f"  격자 최고점: 오전s3={best[1]:.2f} front={best[2]:.2f}  Δ={best[0]:+,.0f} KRW")
    return cells


cells = grid(0.25)

print()
print("=" * 118)
print("§6-plateau  0.25 / 1.05 주변이 고원인가 (production MIN_MULT=0.25 기준)")
print("=" * 118)
p2 = cells[(0.25, 1.05)]
nb = [(m, f) for m in (0.10, 0.25, 0.40) for f in (1.02, 1.05, 1.08)]
print(f"  P2 (0.25/1.05) = {p2:+,.0f} KRW")
print(f"  {'이웃':14s} {'Δ KRW':>12s} {'P2 대비':>11s}")
print("  " + "-" * 40)
for m, f in nb:
    v = cells[(m, f)]
    print(f"  {m:.2f}/{f:.2f}      {v:+12,.0f} {v-p2:+11,.0f}")
vals = [cells[(m, f)] for m, f in nb]
print(f"\n  이웃 9칸 전부 양수: {all(v > 0 for v in vals)}")
print(f"  이웃 9칸 최소 {min(vals):+,.0f} / 최대 {max(vals):+,.0f} / 폭 {max(vals)-min(vals):,.0f}")
print(f"  P2 가 이웃 대비 튀는가(최대값이면서 2위와 격차 큰가): "
      f"P2 순위 {sorted(vals, reverse=True).index(p2)+1}/9")

# 오전 slot3 축만 놓고 단조성 확인 (front=1.05 고정)
print(f"\n  front=1.05 고정, 오전 slot3 축:")
for m in MO3:
    print(f"    {m:.2f} -> {cells[(m, 1.05)]:+12,.0f} KRW")

grid(0.05)
K.MIN_MULT = 0.25
