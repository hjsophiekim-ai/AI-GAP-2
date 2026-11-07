"""out_blue.csv 집계 — 지표별 비교, 그룹 비교, 판정이 갈리는 후보 전수."""
import csv
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROWS = [r for r in csv.DictReader(open("out_blue.csv", encoding="utf-8-sig"))]
for r in ROWS:
    for k in ("r15", "r30", "A_cum", "B_prod60", "C180", "F60", "F180", "opp_gap"):
        r[k] = None if r[k] == "" else float(r[k])


def stat(name, rows):
    n = len(rows)
    if not n:
        print(f"{name:34s}  0건")
        return
    win = sum(1 for r in rows if (r["r30"] or 0) > 0)
    a15 = sum(r["r15"] or 0 for r in rows) / n
    a30 = sum(r["r30"] or 0 for r in rows) / n
    up = sum(1 for r in rows if r["touch"] == "UP")
    dn = sum(1 for r in rows if r["touch"] == "DOWN")
    print(f"{name:34s} {n:2d}건  승률(30분) {win}/{n}={100*win/n:5.1f}%  "
          f"평균15분 {a15:+6.3f}%  평균30분 {a30:+6.3f}%  선터치 +1.5%={up} / -1.5%={dn} / 무={n-up-dn}")


print("=== 1. 지표별 비교 (BLUE 기준 <100 = 일치) ===")
for key, lab in (("A_match", "A 누적 체결강도"), ("B_match", "B 60초(prod 누적평균)"),
                 ("C_match", "C 180초(누적평균)"), ("F60_match", "B' 60초 창 체결강도"),
                 ("F180_match", "C' 180초 창 체결강도")):
    for v in ("MATCH", "MISMATCH"):
        stat(f"{lab} {v}", [r for r in ROWS if r[key] == v])
for v in ("TOWARD", "AWAY"):
    stat(f"D 60s vs 180s {v}", [r for r in ROWS if r["D_drift"] == v])

print()
print("=== 2. 그룹 비교 (단기 = 창 체결강도 F60/F180) ===")


def short_match(r):
    return r["F60_match"] == "MATCH" or r["F180_match"] == "MATCH"


g1 = [r for r in ROWS if r["A_match"] == "MISMATCH" and short_match(r)]
g2 = [r for r in ROWS if r["A_match"] == "MISMATCH" and not short_match(r)]
g3 = [r for r in ROWS if r["A_match"] == "MATCH" and r["F60_match"] == "MATCH" and r["F180_match"] == "MATCH"]
g4 = [r for r in ROWS if r["A_match"] == "MATCH" and not (r["F60_match"] == "MATCH" and r["F180_match"] == "MATCH")]
stat("1 누적 불일치 · 단기 일치", g1)
stat("2 누적·단기 모두 불일치", g2)
stat("3 누적·단기 모두 일치", g3)
stat("4 누적 일치 · 단기 불일치(참고)", g4)

print()
print("=== 3. 현행(A)과 판정이 갈리는 후보 전수 ===")
hdr = f"{'날짜':10s} {'T+3':5s} {'MU':14s} {'HY':7s} {'opp':>6s} {'A':>7s} {'F60':>8s} {'F180':>8s} {'D':7s} {'LQ':3s} {'r15':>7s} {'r30':>7s} {'터치':5s} 차단"
for lab, sel in (("A 불일치 → 60초 일치", lambda r: r["A_match"] == "MISMATCH" and r["F60_match"] == "MATCH"),
                 ("A 불일치 → 180초 일치", lambda r: r["A_match"] == "MISMATCH" and r["F180_match"] == "MATCH"),
                 ("A 일치 → 60초 불일치", lambda r: r["A_match"] == "MATCH" and r["F60_match"] == "MISMATCH"),
                 ("A 일치 → 180초 불일치", lambda r: r["A_match"] == "MATCH" and r["F180_match"] == "MISMATCH")):
    sub = [r for r in ROWS if sel(r)]
    print(f"\n[{lab}] {len(sub)}건")
    print("  " + hdr)
    for r in sub:
        print(f"  {r['day']:10s} {r['t3_at']:5s} {r['mu_status']:14s} {r['hy_state']:7s} "
              f"{r['opp_gap'] or 0:6.1f} {r['A_cum']:7.2f} {r['F60']:8.2f} {r['F180']:8.2f} "
              f"{r['D_drift']:7s} {r['low_quality']:3s} {r['r15']:+7.3f} {r['r30']:+7.3f} "
              f"{r['touch']:5s} {r['prod_block'] or '-'}")

print()
print("=== 4. 안정성: 60초 vs 180초 ===")
for key, lab in (("F60_match", "B' 60초"), ("F180_match", "C' 180초")):
    flip = [r for r in ROWS if r[key] != r["A_match"]]
    good = sum(1 for r in flip if (r["r30"] or 0) > 0)
    print(f"{lab}: A와 판정이 다른 후보 {len(flip)}건 중 실제 BLUE 승자 {good}건")
ext = [abs((r["F60"] or 100) - 100) for r in ROWS if r["F60"] is not None]
ext2 = [abs((r["F180"] or 100) - 100) for r in ROWS if r["F180"] is not None]
print(f"중립(100)에서의 평균 이탈폭: 60초 {sum(ext)/len(ext):.1f}  180초 {sum(ext2)/len(ext2):.1f}  (클수록 변동이 심함)")
