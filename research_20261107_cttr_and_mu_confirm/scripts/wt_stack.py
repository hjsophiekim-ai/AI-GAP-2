"""WAIT: 현행 선택진입 게이트 위에 '왕복장×큰괴리 차단'을 얹을지 / 따로 쓸지 / 안 쓸지.

현행 게이트(production mu_assist.wait_selective_decision):
  ① 체결강도 MATCH (직전 60초 cttr, RED>100 / BLUE<100)
  ② 반대 gap = max(0, −hy_gap_prev) <= 400
  둘 다여야 APPROVED. 그 외는 MU_ASSIST_WAIT_BLOCK.

새 규칙: 최근 60분 효율비 < 0.1223 ∧ mu_hy_gap >= 0 -> 차단 (fail-open).
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
NUM = ("mu_hy_gap", "er_60", "r30", "mae")
ROWS = []
for r in csv.DictReader(open("out_mu.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["mu_status"] != "ENTRY_WAIT" or r["r30"] is None or r["mae"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    r["pnl"] = -1.6 if r["mae"] <= -1.6 else r["r30"]
    ROWS.append(r)

# out_mu.csv 에는 opp_gap/cttr 가 없다 — out_wait.csv 에서 가져온다
aux = {}
for r in csv.DictReader(open("out_wait.csv", encoding="utf-8-sig")):
    aux[(r["day"], r["t3_at"], r["dir"])] = r
for r in ROWS:
    a = aux.get((r["day"], r["t3_at"], r["dir"]), {})
    r["opp_gap"] = None if a.get("opp_gap", "") == "" else float(a["opp_gap"])
    r["cttr_match"] = a.get("cttr_match", "NA")

ERCUT = 0.1223


def approved(r):
    """현행 production WAIT 선택진입 승인 여부."""
    return r["cttr_match"] == "MATCH" and r["opp_gap"] is not None and r["opp_gap"] <= 400.0


def chop_block(r):
    """새 규칙 — 값이 없으면 fail-open."""
    if r["er_60"] is None or r["mu_hy_gap"] is None:
        return False
    return r["er_60"] < ERCUT and r["mu_hy_gap"] >= 0.0


def rep(g):
    if not g:
        return " 0건"
    w = sum(1 for r in g if r["win"])
    p = sum(1 for r in g if r["pnl"] > 0)
    return (f"{len(g):2d}건 승률 {100*w/len(g):5.1f}% 실익 {p:2d}/{len(g):<2d} "
            f"평균 {sum(r['pnl'] for r in g)/len(g):+6.3f}% 합계 {sum(r['pnl'] for r in g):+7.2f}%p")


print(f"MU ENTRY_WAIT {len(ROWS)}건")
print()
print("=== 실제로 '진입하게 되는 집합' 비교 ===")
opts = [
    ("A 현행만 (체결강도 ∧ 반대gap≤400)", [r for r in ROWS if approved(r)]),
    ("B 현행 + 왕복차단 (둘 다 통과)", [r for r in ROWS if approved(r) and not chop_block(r)]),
    ("C 왕복차단만 (현행 게이트 대체)", [r for r in ROWS if not chop_block(r)]),
    ("D 게이트 없음 (WAIT 전부)", ROWS),
    ("E 현행 ∨ 왕복통과 (둘 중 하나)", [r for r in ROWS if approved(r) or not chop_block(r)]),
]
for lab, g in opts:
    print(f"  {lab:34s} {rep(g)}")

print()
print("=== 두 게이트가 같은 걸 거르는가 ===")
a_blk = [r for r in ROWS if not approved(r)]
c_blk = [r for r in ROWS if chop_block(r)]
both = [r for r in ROWS if not approved(r) and chop_block(r)]
print(f"  현행이 거르는 것        {rep(a_blk)}")
print(f"  새 규칙이 거르는 것      {rep(c_blk)}")
print(f"  둘 다 거르는 것(겹침)    {rep(both)}")
print(f"  새 규칙만 거르는 것      {rep([r for r in c_blk if approved(r)])}")
print(f"  현행만 거르는 것        {rep([r for r in a_blk if not chop_block(r)])}")

print()
print("=== 현행 승인 19건 중 새 규칙이 걸러내는 건 ===")
print(f"  {'날짜 T+3':16s} {'방향':10s} {'er60':>7s} {'괴리':>7s} {'반대gap':>7s} {'cttr':>6s} "
      f"{'MAE':>7s} {'30분':>7s} {'손절반영':>7s}")
for r in sorted((r for r in ROWS if approved(r) and chop_block(r)), key=lambda r: (r["day"], r["t3_at"])):
    print(f"  {r['day'][5:]+' '+r['t3_at']:16s} {r['dir']:10s} {r['er_60']:7.4f} "
          f"{r['mu_hy_gap']:+7.3f} {r['opp_gap']:7.1f} {r['cttr_match'][:5]:>6s} "
          f"{r['mae']:+7.2f} {r['r30']:+7.3f} {r['pnl']:+7.3f}")

print()
print("=== 날짜 LOO — B(현행+왕복차단) 가 A(현행만) 를 이기는가 ===")
days = sorted({r["day"] for r in ROWS})
ok = 0
for d in days:
    pop = [r for r in ROWS if r["day"] != d]
    A = [r for r in pop if approved(r)]
    B = [r for r in pop if approved(r) and not chop_block(r)]
    if not A or not B:
        continue
    a, b = sum(r["pnl"] for r in A)/len(A), sum(r["pnl"] for r in B)/len(B)
    ok += b > a
    print(f"  −{d[5:]}  A {a:+6.3f}%  B {b:+6.3f}%  {'O' if b > a else 'X'}")
print(f"  => {ok}/{len(days)}")
