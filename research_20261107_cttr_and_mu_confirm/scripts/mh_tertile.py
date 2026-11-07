"""acc2>=0 AND gflag>=224 그룹 안에서 MU-HY 괴리 3등분 — 분위수로만 자른다(임계값 탐색 없음).

분위수 경계는 그 그룹의 표본에서 나온 값이지 조정한 값이 아니다.
그룹 11건 중 MU 세션 기준봉(08:00)이 없는 10/26 2건은 production 도 값이 없어 제외한다.
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROWS = list(csv.DictReader(open("out_confirm.csv", encoding="utf-8-sig")))
NUM = ("mu_acc2", "g_flag", "mu_hy_gap", "mu_session_pct", "hy_session_pct",
       "r15", "r30", "mfe", "mae")
for r in ROWS:
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])

GRP = [r for r in ROWS if r["mu_acc2"] >= 0 and r["g_flag"] >= 224]
HAVE = [r for r in GRP if r["mu_hy_gap"] is not None]
MISS = [r for r in GRP if r["mu_hy_gap"] is None]
print(f"그룹(acc2>=0 ∧ gflag>=224) {len(GRP)}건 / MU-HY 값 있음 {len(HAVE)}건 / 결측 {len(MISS)}건")
for r in MISS:
    print(f"  결측: {r['day'][5:]} {r['t3_at']} {r['dir']} — MU 세션 기준봉 없음(MU 1분봉 09:00 시작)")
print()

HAVE.sort(key=lambda r: r["mu_hy_gap"])
n = len(HAVE)
k = n // 3
buckets = [("하 (하이닉스가 MU 를 앞섬/비슷)", HAVE[:k]),
           ("중", HAVE[k:n - k]),
           ("상 (하이닉스 지연 큼)", HAVE[n - k:])]
q = [HAVE[k - 1]["mu_hy_gap"], HAVE[n - k]["mu_hy_gap"]]
print(f"3등분 경계(표본 분위수): {q[0]:+.3f} / {q[1]:+.3f}")
print()

print(f"{'구간':26s} {'n':>2s} {'승률(+1.5선터치)':>15s} {'승률(30분>0)':>12s} "
      f"{'평균MFE':>8s} {'평균MAE':>8s} {'평균30분':>9s} {'중앙30분':>9s}")
for lab, g in buckets:
    if not g:
        continue
    w = sum(1 for r in g if r["touch"] == "UP")
    p = sum(1 for r in g if r["r30"] > 0)
    print(f"{lab:26s} {len(g):2d} {w}/{len(g)}={100*w/len(g):8.1f}%   "
          f"{p}/{len(g)}={100*p/len(g):6.1f}%  "
          f"{sum(r['mfe'] for r in g)/len(g):+8.2f} {sum(r['mae'] for r in g)/len(g):+8.2f} "
          f"{sum(r['r30'] for r in g)/len(g):+9.3f} {st.median(r['r30'] for r in g):+9.3f}")

print()
print("=== 구간별 명단 ===")
for lab, g in buckets:
    print(f"\n[{lab}] {len(g)}건")
    print(f"  {'날짜 T+3':16s} {'방향':9s} {'MU-HY':>7s} {'MUses':>7s} {'HYses':>7s} "
          f"{'acc2':>6s} {'gflag':>7s} {'MFE':>6s} {'MAE':>7s} {'15분':>7s} {'30분':>7s} 터치")
    for r in g:
        print(f"  {r['day'][5:]+' '+r['t3_at']:16s} {r['dir']:9s} {r['mu_hy_gap']:+7.3f} "
              f"{r['mu_session_pct']:+7.3f} {r['hy_session_pct']:+7.3f} {r['mu_acc2']:6.1f} "
              f"{r['g_flag']:7.1f} {r['mfe']:+6.2f} {r['mae']:+7.2f} {r['r15']:+7.3f} "
              f"{r['r30']:+7.3f} {r['touch']}")

print()
print("=== 대조: 그룹 밖(나머지 MU CONFIRM)에서 같은 3등분 ===")
OUT = [r for r in ROWS if r not in GRP and r["mu_hy_gap"] is not None]
OUT.sort(key=lambda r: r["mu_hy_gap"])
m = len(OUT)
j = m // 3
for lab, g in (("하", OUT[:j]), ("중", OUT[j:m - j]), ("상", OUT[m - j:])):
    if not g:
        continue
    w = sum(1 for r in g if r["touch"] == "UP")
    print(f"  {lab:3s} {len(g):2d}건  승률(+1.5선터치) {w}/{len(g)}  "
          f"평균MFE {sum(r['mfe'] for r in g)/len(g):+5.2f}  "
          f"평균MAE {sum(r['mae'] for r in g)/len(g):+6.2f}  "
          f"평균30분 {sum(r['r30'] for r in g)/len(g):+6.3f}%")
