"""상호작용의 견고성 — 날짜 LOO, 교란 점검, 교란 없는 '초기 vs 진행중' 재검.

101건이 15 거래일에서 나왔다(날짜 군집). 하루를 통째로 빼도 부호가 유지되는지가 핵심이다.
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
NUM = ("mu_hy_gap", "mu_session_pct", "hy_session_pct", "er", "net_pct",
       "n_flag_before", "flag_seq", "since_opp_min", "prev_er", "r15", "r30", "mfe", "mae")
ROWS = []
for r in csv.DictReader(open("out_regime.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["mu_hy_gap"] is None or r["r30"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    r["hhmm"] = int(r["t3_at"][:2]) * 60 + int(r["t3_at"][3:])
    ROWS.append(r)


def gap_effect(g):
    """그 집합 안에서 괴리 상위 − 하위의 평균 30분 차(%p). 표본이 작으면 None."""
    if len(g) < 8:
        return None
    m = st.median(r["mu_hy_gap"] for r in g)
    hi = [r for r in g if r["mu_hy_gap"] >= m]
    lo = [r for r in g if r["mu_hy_gap"] < m]
    if not hi or not lo:
        return None
    return (sum(r["r30"] for r in hi) / len(hi)) - (sum(r["r30"] for r in lo) / len(lo))


def split(pop, key, hi_is_trend):
    sub = [r for r in pop if r[key] is not None]
    m = st.median(r[key] for r in sub)
    hi = [r for r in sub if r[key] >= m]
    lo = [r for r in sub if r[key] < m]
    return (hi, lo) if hi_is_trend else (lo, hi)


print("=== ① 교란 점검: 레짐 라벨이 괴리 자체와 묶여 있나 (두 반쪽의 괴리 중앙값) ===")
for key, lab, hit in (("er", "효율비 ER", True), ("n_flag_before", "T+3 이전 플래그 수", False),
                      ("net_pct", "당일 방향부호 변화율", True), ("prev_er", "직전 3일 ER", True)):
    t, c = split(ROWS, key, hit)
    mt = st.median(r["mu_hy_gap"] for r in t)
    mc = st.median(r["mu_hy_gap"] for r in c)
    flag = "  <= 괴리와 엉켜 있음(라벨로 부적격)" if abs(mt - mc) > 1.0 else ""
    print(f"  {lab:22s} 추세쪽 괴리중앙 {mt:+7.3f} / 왕복쪽 {mc:+7.3f}  차 {mt-mc:+7.3f}{flag}")

print()
print("=== ② 날짜 LOO — 하루를 통째로 빼도 부호가 유지되는가 ===")
days = sorted({r["day"] for r in ROWS})
for key, lab, hit in (("er", "효율비 ER", True), ("n_flag_before", "T+3 이전 플래그 수", False)):
    print(f"\n  [{lab}]")
    base_t, base_c = split(ROWS, key, hit)
    print(f"    전체      추세장 {gap_effect(base_t):+7.3f}%p   왕복장 {gap_effect(base_c):+7.3f}%p")
    ok = 0
    for d in days:
        pop = [r for r in ROWS if r["day"] != d]
        t, c = split(pop, key, hit)
        et, ec = gap_effect(t), gap_effect(c)
        if et is None or ec is None:
            continue
        good = et > 0 > ec
        ok += good
        print(f"    −{d[5:]}   추세장 {et:+7.3f}%p   왕복장 {ec:+7.3f}%p   {'부호유지' if good else '** 깨짐 **'}")
    print(f"    => {ok}/{len(days)} 일에서 부호 유지")

print()
print("=== ③ '추세 초기 vs 진행중' 재검 — 괴리와 엉키지 않는 대리변수로 ===")
tr, _ = split(ROWS, "er", True)
for key, lab in (("flag_seq", "그날 몇 번째 플래그"), ("hhmm", "T+3 시각")):
    sub = [r for r in tr if r[key] is not None]
    m = st.median(r[key] for r in sub)
    early = [r for r in sub if r[key] < m]
    late = [r for r in sub if r[key] >= m]
    me = st.median(r["mu_hy_gap"] for r in early)
    ml = st.median(r["mu_hy_gap"] for r in late)
    print(f"\n  [{lab}] 중앙 {m:g}  (괴리중앙: 초기 {me:+.3f} / 후기 {ml:+.3f})")
    for nm, g in (("초기", early), ("후기", late)):
        e = gap_effect(g)
        if e is None:
            print(f"    {nm} {len(g):2d}건 — 표본 부족")
            continue
        gm = st.median(r["mu_hy_gap"] for r in g)
        hi = [r for r in g if r["mu_hy_gap"] >= gm]
        lo = [r for r in g if r["mu_hy_gap"] < gm]
        print(f"    {nm} {len(g):2d}건  괴리상위 {len(hi):2d}건 평균30분 "
              f"{sum(r['r30'] for r in hi)/len(hi):+6.3f}% / 하위 {len(lo):2d}건 "
              f"{sum(r['r30'] for r in lo)/len(lo):+6.3f}%  => {e:+.3f}%p")

print()
print("=== ④ 2×2 요약 (ER 라벨, 평균 30분 / 건수) ===")
t, c = split(ROWS, "er", True)
print(f"  {'':12s} {'괴리 하위':>16s} {'괴리 상위':>16s}")
for nm, g in (("추세장", t), ("왕복장", c)):
    m = st.median(r["mu_hy_gap"] for r in g)
    hi = [r for r in g if r["mu_hy_gap"] >= m]
    lo = [r for r in g if r["mu_hy_gap"] < m]
    print(f"  {nm:12s} {sum(r['r30'] for r in lo)/len(lo):+10.3f}% ({len(lo):2d}) "
          f"{sum(r['r30'] for r in hi)/len(hi):+10.3f}% ({len(hi):2d})")
