"""손실 9건의 공통점 확인 — acc2 부호, 그날 CONFIRM 순번, 초기 실패 여부."""
import csv
import sys
from math import comb

sys.stdout.reconfigure(encoding="utf-8")
ROWS = list(csv.DictReader(open("out_confirm.csv", encoding="utf-8-sig")))
for r in ROWS:
    for k in ("mu_acc2", "mu_post2_bp", "mu_pre3_bp", "mfe", "mae", "r30", "g_flag"):
        r[k] = None if r[k] in ("", None) else float(r[k])
    r["loss"] = r["touch"] == "DOWN"

# 그날 몇 번째 CONFIRM 후보인가
seen = {}
for r in ROWS:
    seen[r["day"]] = seen.get(r["day"], 0) + 1
    r["confirm_ord"] = seen[r["day"]]

L = [r for r in ROWS if r["loss"]]
print(f"손실(-1.5% 선터치) {len(L)}건 / 전체 {len(ROWS)}건")
print()

print("=== ① MU 가속 acc2 의 부호 ===")
for lab, sel in (("acc2 < 0 (가속이 플래그 前에 몰림)", lambda r: r["mu_acc2"] < 0),
                 ("acc2 >= 0 (가속이 플래그 後에 몰림)", lambda r: r["mu_acc2"] >= 0)):
    g = [r for r in ROWS if sel(r)]
    nl = sum(1 for r in g if r["loss"])
    print(f"  {lab:34s} {len(g):2d}건  손실 {nl} ({100*nl/len(g):4.1f}%)  "
          f"평균30분 {sum(r['r30'] for r in g)/len(g):+6.3f}%  "
          f"평균MAE {sum(r['mae'] for r in g)/len(g):+6.2f}%  "
          f"평균MFE {sum(r['mfe'] for r in g)/len(g):+5.2f}%")
n, k, d = len(ROWS), len(L), sum(1 for r in ROWS if r["mu_acc2"] < 0)
p = comb(n - k, d) / comb(n, d)
print(f"  (우연일 확률: {d}건을 뽑아 손실 0건일 초기하 확률 = {p:.3f} — 피처 13개를 본 뒤의 값이라 과신 금물)")

print()
print("=== ② 그날 몇 번째 MU CONFIRM 후보인가 ===")
for o in sorted({r["confirm_ord"] for r in ROWS}):
    g = [r for r in ROWS if r["confirm_ord"] == o]
    nl = sum(1 for r in g if r["loss"])
    print(f"  {o}번째 {len(g):2d}건  손실 {nl} ({100*nl/len(g):4.1f}%)  "
          f"평균30분 {sum(r['r30'] for r in g)/len(g):+6.3f}%")

print()
print("=== ③ 손실은 '곧바로' 실패한다 (MFE = 진입 후 최대 유리폭) ===")
for lab, g in (("손실", L), ("승리", [r for r in ROWS if r["touch"] == "UP"])):
    small = sum(1 for r in g if r["mfe"] < 2.0)
    print(f"  {lab} {len(g):2d}건 중 MFE < +2.0% 인 건 {small}건 ({100*small/len(g):4.1f}%)  "
          f"중앙 MFE {sorted(r['mfe'] for r in g)[len(g)//2]:+5.2f}%")

print()
print("=== 손실 9건의 공통 조건 체크 ===")
print(f"  {'날짜 T+3':16s} {'방향':9s} {'acc2':>7s} {'post2':>7s} {'gflag':>7s} {'MFE':>6s} {'MAE':>7s}  acc2>=0  gflag>=224")
for r in L:
    print(f"  {r['day'][5:]+' '+r['t3_at']:16s} {r['dir']:9s} {r['mu_acc2']:7.1f} {r['mu_post2_bp']:7.1f} "
          f"{r['g_flag']:7.1f} {r['mfe']:6.2f} {r['mae']:7.2f}  "
          f"{'O' if r['mu_acc2'] >= 0 else 'X':^7s}  {'O' if r['g_flag'] >= 224 else 'X':^9s}")
