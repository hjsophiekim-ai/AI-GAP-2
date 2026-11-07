"""가설 검증: 추세장에서는 괴리가 클수록 좋고, 왕복장에서는 괴리가 클수록 나쁘다.

분할은 전부 **표본 중앙값**(분위수)으로만 한다 — 임계값 탐색 없음.
레짐 라벨은 T+3 이전 데이터로만 만든 네 가지를 각각 따로 본다.
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
NUM = ("mu_hy_gap", "mu_session_pct", "er", "net_pct", "n_flag_before", "flag_seq",
       "since_opp_min", "prev_er", "r15", "r30", "mfe", "mae")
ROWS = []
for r in csv.DictReader(open("out_regime.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["mu_hy_gap"] is None or r["r30"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    ROWS.append(r)
print(f"모집단: 전체 플래그 중 괴리·사후경로가 모두 있는 {len(ROWS)}건 (10/19~11/06, 15 거래일)")


def rank_corr(g, a="mu_hy_gap", b="r30"):
    """스피어만 순위상관 — 괴리가 클수록 성과가 좋은가(+) 나쁜가(−)."""
    n = len(g)
    if n < 5:
        return None
    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for t in range(i, j + 1):
                rk[order[t]] = avg
            i = j + 1
        return rk
    ra, rb = ranks([r[a] for r in g]), ranks([r[b] for r in g])
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    den = (sum((x - ma) ** 2 for x in ra) * sum((x - mb) ** 2 for x in rb)) ** 0.5
    return None if den == 0 else round(num / den, 3)


def cell(g):
    if not g:
        return "  0건"
    w = sum(1 for r in g if r["win"])
    p = sum(1 for r in g if r["r30"] > 0)
    return (f"{len(g):3d}건 승률 {100*w/len(g):5.1f}% (30분>0 {100*p/len(g):5.1f}%) "
            f"평균30분 {sum(r['r30'] for r in g)/len(g):+6.3f}% "
            f"MFE {sum(r['mfe'] for r in g)/len(g):+5.2f} MAE {sum(r['mae'] for r in g)/len(g):+6.2f}")


def interaction(label, key, hi_is_trend=True, pop=None):
    pop = pop if pop is not None else ROWS
    sub = [r for r in pop if r[key] is not None]
    m = st.median(r[key] for r in sub)
    hi = [r for r in sub if r[key] >= m]
    lo = [r for r in sub if r[key] < m]
    trend, chop = (hi, lo) if hi_is_trend else (lo, hi)
    print(f"\n--- 레짐 라벨: {label}  (중앙 {m:.4g}, n={len(sub)}) ---")
    for rlab, rg in (("추세장 쪽", trend), ("왕복장 쪽", chop)):
        if len(rg) < 4:
            print(f"  {rlab}: {len(rg)}건 — 생략")
            continue
        gm = st.median(r["mu_hy_gap"] for r in rg)
        ghi = [r for r in rg if r["mu_hy_gap"] >= gm]
        glo = [r for r in rg if r["mu_hy_gap"] < gm]
        d = (sum(r["r30"] for r in ghi) / len(ghi)) - (sum(r["r30"] for r in glo) / len(glo))
        print(f"  {rlab} (괴리 중앙 {gm:+.3f})  순위상관(괴리 vs 30분) = {rank_corr(rg)}")
        print(f"    괴리 상위 {cell(ghi)}")
        print(f"    괴리 하위 {cell(glo)}")
        print(f"    => 괴리 상위 − 하위 = {d:+.3f}%p  ({'가설대로' if (d > 0) == (rlab == '추세장 쪽') else '가설과 반대'})")


print("\n" + "=" * 100)
print("가설: 추세장 → 괴리 클수록 좋다 / 왕복장 → 괴리 클수록 나쁘다")
print("=" * 100)
interaction("효율비 ER (09:00~T+3, 클수록 추세)", "er", True)
interaction("T+3 이전 플래그 수 (적을수록 추세)", "n_flag_before", False)
interaction("직전 3거래일 평균 ER (당일 데이터 안 씀)", "prev_er", True)
interaction("당일 방향부호 변화율 net_pct (클수록 이미 추세 추종)", "net_pct", True)

print("\n" + "=" * 100)
print("추세 '초기' vs '진행 중' — 추세장(ER 상위) 안에서만")
print("=" * 100)
sub = [r for r in ROWS if r["er"] is not None]
em = st.median(r["er"] for r in sub)
tr = [r for r in sub if r["er"] >= em]
nm = st.median(r["net_pct"] for r in tr)
for lab, g in ((f"초기 (net_pct < {nm:+.2f} — 아직 그 방향으로 덜 갔다)", [r for r in tr if r["net_pct"] < nm]),
               (f"진행중 (net_pct >= {nm:+.2f} — 이미 그 방향으로 갔다)", [r for r in tr if r["net_pct"] >= nm])):
    if len(g) < 4:
        continue
    gm = st.median(r["mu_hy_gap"] for r in g)
    ghi = [r for r in g if r["mu_hy_gap"] >= gm]
    glo = [r for r in g if r["mu_hy_gap"] < gm]
    d = (sum(r["r30"] for r in ghi) / len(ghi)) - (sum(r["r30"] for r in glo) / len(glo))
    print(f"\n  {lab}  순위상관 {rank_corr(g)}")
    print(f"    괴리 상위 {cell(ghi)}")
    print(f"    괴리 하위 {cell(glo)}")
    print(f"    => 상위 − 하위 = {d:+.3f}%p")

print("\n" + "=" * 100)
print("참고: 레짐 무시하고 괴리만")
print("=" * 100)
gm = st.median(r["mu_hy_gap"] for r in ROWS)
print(f"  괴리 상위 {cell([r for r in ROWS if r['mu_hy_gap'] >= gm])}")
print(f"  괴리 하위 {cell([r for r in ROWS if r['mu_hy_gap'] < gm])}")
print(f"  전체 순위상관(괴리 vs 30분) = {rank_corr(ROWS)}")
