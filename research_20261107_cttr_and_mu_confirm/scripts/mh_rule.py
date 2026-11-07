"""'오전이면 MU-HY 괴리 >= 2.5 만 진입' 규칙을 걸었을 때 승률이 어떻게 변하는지.

2.5 는 표본을 본 뒤 고른 값이다 — 탐색 결과가 아니라 사용자가 지정한 값으로 그대로 적용만 한다.
결측(MU 세션 기준봉 없음)은 fail-open / fail-closed 양쪽을 다 보여준다.
"""
import csv
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROWS = list(csv.DictReader(open("out_confirm.csv", encoding="utf-8-sig")))
for r in ROWS:
    for k in ("mu_acc2", "g_flag", "mu_hy_gap", "r15", "r30", "mfe", "mae"):
        r[k] = None if r[k] in ("", None) else float(r[k])
    r["win"] = r["touch"] == "UP"

CUT = 2.5
GRP = [r for r in ROWS if r["mu_acc2"] >= 0 and r["g_flag"] >= 224]


def keep(r, miss_passes):
    if r["session"] != "AM":
        return True                     # 오후는 규칙 대상 아님
    if r["mu_hy_gap"] is None:
        return miss_passes
    return r["mu_hy_gap"] >= CUT


def line(lab, g):
    if not g:
        print(f"  {lab:34s}  0건")
        return
    w = sum(1 for r in g if r["win"])
    p = sum(1 for r in g if r["r30"] > 0)
    print(f"  {lab:34s} {len(g):2d}건  승률(+1.5선터치) {w}/{len(g)}={100*w/len(g):5.1f}%  "
          f"(30분>0) {p}/{len(g)}={100*p/len(g):5.1f}%  "
          f"평균30분 {sum(r['r30'] for r in g)/len(g):+6.3f}%  "
          f"평균MFE {sum(r['mfe'] for r in g)/len(g):+5.2f}%  "
          f"평균MAE {sum(r['mae'] for r in g)/len(g):+6.2f}%")


for pop_lab, POP in (("전체 MU CONFIRM", ROWS), ("acc2>=0 ∧ gflag>=224 그룹", GRP)):
    print(f"=== {pop_lab} ===")
    line("현행(규칙 없음)", POP)
    for mp, mlab in ((True, "결측 통과(fail-open)"), (False, "결측 차단(fail-closed)")):
        line(f"오전 괴리>={CUT} 한정 — {mlab}", [r for r in POP if keep(r, mp)])
    am = [r for r in POP if r["session"] == "AM"]
    pm = [r for r in POP if r["session"] == "PM"]
    line("(참고) 오전 전체", am)
    line("(참고) 오후 전체 — 규칙 무관", pm)
    print()

print("=== 규칙이 잘라내는 후보 전수 (오전 · 괴리 < 2.5 또는 결측) ===")
cutlist = [r for r in ROWS if r["session"] == "AM" and not keep(r, True)]
misslist = [r for r in ROWS if r["session"] == "AM" and r["mu_hy_gap"] is None]
print(f"  {'날짜 T+3':16s} {'방향':9s} {'MU-HY':>7s} {'그룹':4s} {'MFE':>6s} {'MAE':>7s} {'30분':>7s} 결과")
for r in cutlist + misslist:
    g = "O" if r in GRP else "-"
    gap = "결측" if r["mu_hy_gap"] is None else f"{r['mu_hy_gap']:+7.3f}"
    print(f"  {r['day'][5:]+' '+r['t3_at']:16s} {r['dir']:9s} {gap:>7s} {g:^4s} "
          f"{r['mfe']:+6.2f} {r['mae']:+7.2f} {r['r30']:+7.3f} {'승리' if r['win'] else ('손실' if r['touch']=='DOWN' else '무')}")

print()
print("=== 임계값이 얼마나 자의적인가 — 오전 후보를 괴리 순으로 ===")
am = sorted((r for r in ROWS if r["session"] == "AM" and r["mu_hy_gap"] is not None),
            key=lambda r: -r["mu_hy_gap"])
for r in am:
    print(f"  {r['day'][5:]} {r['t3_at']}  gap {r['mu_hy_gap']:+6.3f}  "
          f"30분 {r['r30']:+7.3f}  {'승리' if r['win'] else ('손실' if r['touch']=='DOWN' else '무')}"
          f"{'   <- 2.5 경계' if r['mu_hy_gap'] < CUT and am.index(r) > 0 and am[am.index(r)-1]['mu_hy_gap'] >= CUT else ''}")
