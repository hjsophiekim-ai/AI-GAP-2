"""MU ENTRY_WAIT 승자/패자 — 피처 비교 + 레짐×괴리 적용.

성과는 둘 다 본다:
  r30  = T+3 후 30분 전방 수익률 (순진)
  pnl  = 손절 반영 (MAE <= -1.6% 면 -1.6, 아니면 r30) — production 은 손절이 먼저 맞는다
분할은 전부 표본 중앙값 또는 자연 경계(0.0). 탐색 없음.
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
NUM = ("mu_hy_gap", "mu_session_pct", "hy_session_pct", "er", "net_pct", "n_flag_before",
       "flag_seq", "since_opp_min", "opp_gap", "cttr", "r15", "r30", "mfe", "mae")
ROWS = []
for r in csv.DictReader(open("out_wait.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["r30"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    r["pnl"] = -1.6 if (r["mae"] is not None and r["mae"] <= -1.6) else r["r30"]
    ROWS.append(r)

L = [r for r in ROWS if r["touch"] == "DOWN"]
W_ = [r for r in ROWS if r["touch"] == "UP"]
O = [r for r in ROWS if r["touch"] not in ("UP", "DOWN")]
print(f"MU ENTRY_WAIT {len(ROWS)}건 — +1.5% 선터치 {len(W_)} / -1.5% 선터치 {len(L)} / 무 {len(O)}")
print(f"  순진한 평균30분 {sum(r['r30'] for r in ROWS)/len(ROWS):+.3f}%  "
      f"손절 반영 평균 {sum(r['pnl'] for r in ROWS)/len(ROWS):+.3f}%  "
      f"손절 선행 {sum(1 for r in ROWS if r['pnl'] == -1.6)}건")
print()


def med(g, k):
    v = [r[k] for r in g if r[k] is not None]
    return st.median(v) if v else None


print("=== ① 피처 중앙값: 패자 vs 승자 ===")
print(f"{'피처':18s} {'패자':>10s} {'승자':>10s} {'차':>9s}")
for k in ("mu_hy_gap", "mu_session_pct", "hy_session_pct", "er", "n_flag_before", "flag_seq",
          "since_opp_min", "opp_gap", "cttr", "mfe", "mae"):
    a, b = med(L, k), med(W_, k)
    if a is None or b is None:
        continue
    print(f"{k:18s} {a:10.3f} {b:10.3f} {a-b:+9.3f}")

print()
print("=== ② 범주형 ===")
for k in ("session", "dir", "cttr_match"):
    for v in sorted({r[k] for r in ROWS}):
        g = [r for r in ROWS if r[k] == v]
        nl = sum(1 for r in g if r["touch"] == "DOWN")
        print(f"  {k}={v:10s} {len(g):2d}건 패 {nl} ({100*nl/len(g):4.1f}%)  "
              f"순진 {sum(r['r30'] for r in g)/len(g):+6.3f}%  손절반영 {sum(r['pnl'] for r in g)/len(g):+6.3f}%")


def cell(g):
    if not g:
        return "  0건"
    w = sum(1 for r in g if r["win"])
    return (f"{len(g):2d}건 승률 {100*w/len(g):5.1f}% 순진 {sum(r['r30'] for r in g)/len(g):+6.3f}% "
            f"손절반영 {sum(r['pnl'] for r in g)/len(g):+6.3f}% MAE {sum(r['mae'] for r in g)/len(g):+6.2f}")


print()
print("=== ③ 레짐 × 괴리 2×2 (WAIT 안에서) ===")
have = [r for r in ROWS if r["mu_hy_gap"] is not None and r["er"] is not None]
print(f"  괴리·ER 모두 있는 {len(have)}건")
for key, lab, hit in (("er", "효율비 ER", True), ("n_flag_before", "이전 플래그 수", False)):
    m = st.median(r[key] for r in have)
    hi = [r for r in have if r[key] >= m]
    lo = [r for r in have if r[key] < m]
    trend, chop = (hi, lo) if hit else (lo, hi)
    print(f"\n  [{lab}] 중앙 {m:g}")
    for rlab, rg in (("추세장", trend), ("왕복장", chop)):
        if len(rg) < 4:
            print(f"    {rlab}: {len(rg)}건 — 생략")
            continue
        gm = st.median(r["mu_hy_gap"] for r in rg)
        ghi = [r for r in rg if r["mu_hy_gap"] >= gm]
        glo = [r for r in rg if r["mu_hy_gap"] < gm]
        d30 = sum(r["r30"] for r in ghi)/len(ghi) - sum(r["r30"] for r in glo)/len(glo)
        dp = sum(r["pnl"] for r in ghi)/len(ghi) - sum(r["pnl"] for r in glo)/len(glo)
        print(f"    {rlab} 괴리상위 {cell(ghi)}")
        print(f"    {rlab} 괴리하위 {cell(glo)}")
        print(f"    => 상위−하위  순진 {d30:+.3f}%p / 손절반영 {dp:+.3f}%p")

print()
print("=== ④ 점수 체계(+2 / 0 / -1) 를 WAIT 에 적용 ===")
ERM = st.median(r["er"] for r in have)
NFB = st.median(r["n_flag_before"] for r in have)
print(f"  (WAIT 표본 기준 임계: ER {ERM:.4f} / nfb {NFB:g} / 괴리 0.0)")


def score(r):
    if r["mu_hy_gap"] is None or r["er"] is None:
        return 0
    if r["er"] >= ERM and r["n_flag_before"] < NFB and r["mu_hy_gap"] >= 0.0:
        return 2
    if r["er"] < ERM and r["mu_hy_gap"] >= 0.0:
        return -1
    return 0


for r in ROWS:
    r["s"] = score(r)
for s in (2, 0, -1):
    print(f"  점수 {s:+d}: {cell([r for r in ROWS if r['s'] == s])}")

print()
print("=== ⑤ 전수 ===")
print(f"  {'날짜 T+3':16s} {'방향':10s} {'ER':>7s} {'nfb':>3s} {'괴리':>7s} {'반대gap':>7s} "
      f"{'cttr':>7s} {'점수':>3s} {'MFE':>6s} {'MAE':>7s} {'30분':>7s} {'손절반영':>7s} 터치")
for r in sorted(ROWS, key=lambda r: (r["day"], r["t3_at"])):
    f = lambda k, w=7, p=3: (" " * (w - 2) + "-") if r[k] is None else ("{:%d.%df}" % (w, p)).format(r[k])
    print(f"  {r['day'][5:]+' '+r['t3_at']:16s} {r['dir']:10s} {f('er',7,4)} "
          f"{int(r['n_flag_before']):3d} {f('mu_hy_gap')} {f('opp_gap',7,1)} {f('cttr',7,1)} "
          f"{r['s']:+3d} {f('mfe',6,2)} {f('mae',7,2)} {f('r30')} {r['pnl']:+7.3f} {r['touch']}")
