"""게이트 후보의 효과 — 판정 시각에 production 이 가질 수 있는 값만 쓴다.

입력 3개
  nfb  : 그날 이 플래그 이전에 난 플래그 수      (worker 가 이미 아는 값)
  er   : 09:00~T+3 하이닉스 효율비              (df_1m 로 계산 가능, 새 계산)
  gap  : mu_hy_gap                            (mu_catchup.evaluate 가 이미 계산)
gap 이 없는 플래그(MU/HY 08:00 기준봉 결측)는 **fail-open** — 기존 그대로 통과.

임계값은 최적화하지 않는다. 기본값은 (ⓐ 표본 중앙값, ⓑ 자연 경계 0.0) 둘만 보고,
주변값은 '고원이 있는가'를 확인하는 용도로만 찍는다.
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
NUM = ("mu_hy_gap", "er", "n_flag_before", "r15", "r30", "mfe", "mae")
ROWS = []
for r in csv.DictReader(open("out_regime.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["r30"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    ROWS.append(r)

HAVE = [r for r in ROWS if r["mu_hy_gap"] is not None]
GM = st.median(r["mu_hy_gap"] for r in HAVE)
NFB = st.median(r["n_flag_before"] for r in HAVE)
ERM = st.median(r["er"] for r in HAVE)
print(f"전체 플래그 {len(ROWS)}건 / 괴리값 있음 {len(HAVE)}건 (결측 {len(ROWS)-len(HAVE)}건은 fail-open)")
print(f"표본 중앙값 — 괴리 {GM:+.3f}  이전플래그수 {NFB:g}  효율비 {ERM:.4f}")
print()


def blocked(r, gcut, ncut, mode):
    """True = 차단. gap 결측이면 항상 통과(fail-open)."""
    if r["mu_hy_gap"] is None:
        return False
    chop = r["n_flag_before"] >= ncut
    hi = r["mu_hy_gap"] >= gcut
    if mode == "V1":
        return chop and hi                       # 왕복장 고괴리만 차단
    if mode == "V2":
        return (not chop) and (not hi)           # 추세장 저괴리만 차단
    if mode == "V3":
        return (chop and hi) or ((not chop) and (not hi))   # 둘 다
    return False


def summ(g):
    if not g:
        return "0건"
    w = sum(1 for r in g if r["win"])
    return (f"{len(g):3d}건 승률 {100*w/len(g):5.1f}% 평균30분 "
            f"{sum(r['r30'] for r in g)/len(g):+6.3f}% 평균15분 "
            f"{sum(r['r15'] for r in g)/len(g):+6.3f}% MAE {sum(r['mae'] for r in g)/len(g):+6.2f}")


print("=== 기준선 (게이트 없음) ===")
print("  " + summ(ROWS))
print()

for gcut, glab in ((GM, f"괴리 >= 표본중앙 {GM:+.3f}"), (0.0, "괴리 >= 0.0 (자연 경계)")):
    print(f"=== 임계: {glab},  왕복장 = 이전플래그수 >= {NFB:g} ===")
    for mode, mlab in (("V1", "V1 왕복장×고괴리 차단"),
                       ("V2", "V2 추세장×저괴리 차단"),
                       ("V3", "V3 둘 다 차단")):
        keep = [r for r in ROWS if not blocked(r, gcut, NFB, mode)]
        cut = [r for r in ROWS if blocked(r, gcut, NFB, mode)]
        print(f"  {mlab:22s} 통과 {summ(keep)}")
        print(f"  {'':22s} 차단 {summ(cut)}")
    print()

print("=== 고원 확인 (최적값 선택 아님 — V3, 통과분 평균 30분) ===")
print(f"  {'괴리컷':>8s} " + " ".join(f"{'nfb>=%d' % n:>10s}" for n in (3, 4, 5)))
for gc in (-1.0, -0.5, 0.0, 0.5, 1.0):
    cells = []
    for n in (3, 4, 5):
        keep = [r for r in ROWS if not blocked(r, gc, n, "V3")]
        cells.append(f"{sum(r['r30'] for r in keep)/len(keep):+9.3f}%")
    print(f"  {gc:+8.1f} " + " ".join(f"{c:>10s}" for c in cells))

print()
print("=== 날짜 LOO (V3, 괴리 >= 0.0, nfb >= 4) — 통과분이 기준선을 이기는가 ===")
days = sorted({r["day"] for r in ROWS})
ok = 0
for d in days:
    pop = [r for r in ROWS if r["day"] != d]
    keep = [r for r in pop if not blocked(r, 0.0, NFB, "V3")]
    a = sum(r["r30"] for r in keep) / len(keep)
    b = sum(r["r30"] for r in pop) / len(pop)
    ok += a > b
    print(f"  −{d[5:]}  통과 {a:+6.3f}% vs 기준선 {b:+6.3f}%  {'O' if a > b else 'X'}")
print(f"  => {ok}/{len(days)}")

print()
print("=== 하루 평균 몇 건이 막히나 (슬롯 경쟁 영향 가늠) ===")
for mode in ("V1", "V2", "V3"):
    cut = [r for r in ROWS if blocked(r, 0.0, NFB, mode)]
    byday = {}
    for r in cut:
        byday[r["day"]] = byday.get(r["day"], 0) + 1
    print(f"  {mode}: 총 {len(cut)}건 / {len(days)}일 = 하루 {len(cut)/len(days):.1f}건 차단")
