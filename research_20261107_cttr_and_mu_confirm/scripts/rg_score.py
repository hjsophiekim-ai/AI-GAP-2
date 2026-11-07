"""가산점 체계 검증 — 추세장+초기+큰괴리 = 가산 / 왕복장+큰괴리 = 감점.

입력 3개(판정 시각 기준, 미래 데이터 없음)
  추세 : er   = 09:00~T+3 하이닉스 효율비      (중앙값 분할)
  초기 : nfb  = 그날 이 플래그 이전 플래그 수   (중앙값 분할)
  괴리 : gap  = mu_hy_gap                     (자연 경계 0.0)
세 임계값 모두 표본 중앙값 또는 자연 경계다 — 탐색하지 않았다.
괴리 결측은 점수 0 (fail-open).
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
NUM = ("mu_hy_gap", "er", "n_flag_before", "flag_seq", "r15", "r30", "mfe", "mae")
ROWS = []
for r in csv.DictReader(open("out_regime2.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["r30"] is None or r["er"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    ROWS.append(r)

ERM = st.median(r["er"] for r in ROWS)
NFB = st.median(r["n_flag_before"] for r in ROWS)
print(f"모집단 {len(ROWS)}건 (10/19~11/06, 11/05·11/06 은 production 원장 기준)")
print(f"임계 — 효율비 중앙 {ERM:.4f} / 이전플래그수 중앙 {NFB:g} / 괴리 자연경계 0.0")


def score(r):
    if r["mu_hy_gap"] is None:
        return 0
    trend = r["er"] >= ERM
    early = r["n_flag_before"] < NFB
    big = r["mu_hy_gap"] >= 0.0
    if trend and early and big:
        return +2
    if (not trend) and big:
        return -1
    return 0


for r in ROWS:
    r["s"] = score(r)


def summ(g):
    if not g:
        return "0건"
    w = sum(1 for r in g if r["win"])
    p = sum(1 for r in g if r["r30"] > 0)
    return (f"{len(g):3d}건 승률 {100*w/len(g):5.1f}% (30분>0 {100*p/len(g):5.1f}%) "
            f"평균30분 {sum(r['r30'] for r in g)/len(g):+6.3f}% 평균15분 {sum(r['r15'] for r in g)/len(g):+6.3f}% "
            f"MFE {sum(r['mfe'] for r in g)/len(g):+5.2f} MAE {sum(r['mae'] for r in g)/len(g):+6.2f}")


print()
print("=== 점수별 ===")
for s in (+2, 0, -1):
    lab = {2: "+2 추세장×초기×큰괴리", 0: " 0 그 외", -1: "-1 왕복장×큰괴리"}[s]
    print(f"  {lab:22s} {summ([r for r in ROWS if r['s'] == s])}")
print(f"  {'전체(기준선)':22s} {summ(ROWS)}")

print()
print("=== 단조성 확인 (점수 → 평균 30분) ===")
vals = [(s, [r for r in ROWS if r["s"] == s]) for s in (-1, 0, 2)]
print("  " + "   ".join(f"{s:+d}: {sum(r['r30'] for r in g)/len(g):+.3f}% (n={len(g)})" for s, g in vals))

print()
print("=== 날짜 LOO — +2 가 0 을, 0 이 -1 을 이기는가 ===")
days = sorted({r["day"] for r in ROWS})
ok = 0
for d in days:
    pop = [r for r in ROWS if r["day"] != d]
    m = {s: sum(r["r30"] for r in pop if r["s"] == s) / max(1, len([r for r in pop if r["s"] == s]))
         for s in (+2, 0, -1)}
    good = m[+2] > m[0] > m[-1]
    ok += good
    print(f"  −{d[5:]}  +2 {m[+2]:+6.3f}%  0 {m[0]:+6.3f}%  -1 {m[-1]:+6.3f}%  {'O' if good else 'X'}")
print(f"  => 완전 단조 {ok}/{len(days)}일")

print()
print("=== 2026-11-06 (금) 전수 — production 결과와 대조 ===")
note = {"10:30": "", "10:33": "실진입 → TIME_WINDOW_STOP_LOSS −192,902원",
        "12:03": "LOW_QUALITY 차단(놓친 +3.9%)",
        "13:36": "★ 2번째 승인 → 예수금 부족 미진입"}
print(f"  {'flag':6s} {'T+3':6s} {'방향':10s} {'ER':>7s} {'레짐':5s} {'nfb':>3s} {'초기':4s} "
      f"{'괴리':>7s} {'점수':>4s} {'30분':>7s} {'MAE':>7s}  비고")
for r in sorted((r for r in ROWS if r["day"] == "2026-11-06"), key=lambda r: r["flag_at"]):
    g = "  결측" if r["mu_hy_gap"] is None else f"{r['mu_hy_gap']:+7.3f}"
    print(f"  {r['flag_at']:6s} {r['t3_at']:6s} {r['dir']:10s} {r['er']:7.4f} "
          f"{'추세' if r['er'] >= ERM else '왕복':5s} {int(r['n_flag_before']):3d} "
          f"{'O' if r['n_flag_before'] < NFB else 'X':^4s} {g:>7s} {r['s']:+4d} "
          f"{r['r30']:+7.3f} {r['mae']:+7.2f}  {note.get(r['t3_at'], '')}")
