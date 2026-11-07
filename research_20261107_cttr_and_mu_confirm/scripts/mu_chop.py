"""왕복장 판정 기준 비교 — 후보 6종을 같은 방식으로 재고, '왕복장 × 큰 괴리 차단' 효과를 본다.

모집단 : MU CONFIRM + WAIT 70건 (10/19~11/06)
성과    : pnl = 손절 반영 (MAE <= -1.6% 면 -1.6, 아니면 30분 수익률)
분할    : 전부 표본 중앙값. 괴리는 자연 경계 0.0. 탐색 없음.
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
NUM = ("mu_hy_gap", "er", "er_60", "n_flag_before", "flag_rate", "range_pos",
       "since_opp_min", "elapsed_min", "r15", "r30", "mfe", "mae")
ROWS = []
for r in csv.DictReader(open("out_mu.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["r30"] is None or r["mae"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    r["pnl"] = -1.6 if r["mae"] <= -1.6 else r["r30"]
    ROWS.append(r)

HAVE = [r for r in ROWS if r["mu_hy_gap"] is not None]
print(f"모집단 {len(ROWS)}건 (CONFIRM {sum(1 for r in ROWS if r['mu_status']=='ENTRY_CONFIRM')} / "
      f"WAIT {sum(1 for r in ROWS if r['mu_status']=='ENTRY_WAIT')}), 괴리값 있음 {len(HAVE)}건")
print(f"기준선: 승률 {100*sum(1 for r in ROWS if r['win'])/len(ROWS):.1f}%  "
      f"실익 {sum(1 for r in ROWS if r['pnl']>0)}/{len(ROWS)}  "
      f"평균 손절반영 {sum(r['pnl'] for r in ROWS)/len(ROWS):+.3f}%  "
      f"합계 {sum(r['pnl'] for r in ROWS):+.2f}%p")

CANDS = [("er",          "C1 당일 효율비",        False),   # 낮을수록 왕복
         ("er_60",       "C2 최근 60분 효율비",    False),
         ("n_flag_before", "C3 이전 플래그 수",    True),    # 높을수록 왕복
         ("flag_rate",   "C4 시간당 플래그 수",    True),
         ("since_opp_min", "C5 직전 반대플래그 경과분", False),
         ("range_pos",   "C6 순변화/고저폭",       False)]


def line(g):
    if not g:
        return "0건"
    w = sum(1 for r in g if r["win"])
    p = sum(1 for r in g if r["pnl"] > 0)
    return (f"{len(g):2d}건 승률 {100*w/len(g):5.1f}% 실익 {p:2d} "
            f"평균 {sum(r['pnl'] for r in g)/len(g):+6.3f}% 합계 {sum(r['pnl'] for r in g):+7.2f}%p")


print()
print("=== ① 후보별: 왕복장 안에서 괴리 상위가 더 나쁜가 (차단 전제) ===")
for key, lab, hi_chop in CANDS:
    sub = [r for r in HAVE if r[key] is not None]
    if len(sub) < 20:
        print(f"  {lab:22s} 표본 {len(sub)}건 — 생략")
        continue
    m = st.median(r[key] for r in sub)
    chop = [r for r in sub if (r[key] >= m if hi_chop else r[key] < m)]
    trend = [r for r in sub if r not in chop]
    cg = st.median(r["mu_hy_gap"] for r in chop)
    ch = [r for r in chop if r["mu_hy_gap"] >= 0.0]
    cl = [r for r in chop if r["mu_hy_gap"] < 0.0]
    tg_h = [r for r in trend if r["mu_hy_gap"] >= 0.0]
    tg_l = [r for r in trend if r["mu_hy_gap"] < 0.0]
    d_chop = (sum(r["pnl"] for r in ch)/len(ch) - sum(r["pnl"] for r in cl)/len(cl)) if ch and cl else None
    d_tr = (sum(r["pnl"] for r in tg_h)/len(tg_h) - sum(r["pnl"] for r in tg_l)/len(tg_l)) if tg_h and tg_l else None
    print(f"\n  {lab}  (중앙 {m:g}, 왕복 {len(chop)}건 / 추세 {len(trend)}건)")
    print(f"    왕복×괴리≥0 {line(ch)}")
    print(f"    왕복×괴리<0 {line(cl)}")
    print(f"    추세×괴리≥0 {line(tg_h)}")
    print(f"    추세×괴리<0 {line(tg_l)}")
    s = "" if (d_chop is None or d_tr is None) else \
        f"  [왕복 괴리효과 {d_chop:+.3f}%p / 추세 {d_tr:+.3f}%p  상호작용 {d_tr-d_chop:+.3f}%p]"
    print(f"   {s}")

print()
print("=== ② '왕복장 × 괴리≥0 차단' 의 효과 (후보별) ===")
print(f"  {'기준':24s} {'차단':>5s}  {'차단분 평균':>10s}  {'통과 승률':>9s} {'통과 실익':>9s} "
      f"{'통과 평균':>9s} {'통과 합계':>9s}")
for key, lab, hi_chop in CANDS:
    sub = [r for r in HAVE if r[key] is not None]
    if len(sub) < 20:
        continue
    m = st.median(r[key] for r in sub)
    def blk(r):
        if r[key] is None or r["mu_hy_gap"] is None:
            return False                     # fail-open
        chop = r[key] >= m if hi_chop else r[key] < m
        return chop and r["mu_hy_gap"] >= 0.0
    cut = [r for r in ROWS if blk(r)]
    keep = [r for r in ROWS if not blk(r)]
    w = sum(1 for r in keep if r["win"])
    p = sum(1 for r in keep if r["pnl"] > 0)
    print(f"  {lab:24s} {len(cut):3d}건  {sum(r['pnl'] for r in cut)/len(cut):+9.3f}%  "
          f"{100*w/len(keep):8.1f}% {p:4d}/{len(keep):<4d} "
          f"{sum(r['pnl'] for r in keep)/len(keep):+8.3f}% {sum(r['pnl'] for r in keep):+8.2f}%p")

print()
print("=== ③ 왕복장 지표끼리 얼마나 같은 날을 가리키나 (일치율) ===")
labs = [(k, l, h) for k, l, h in CANDS if len([r for r in HAVE if r[k] is not None]) >= 20]
meds = {}
for k, l, h in labs:
    meds[k] = st.median(r[k] for r in HAVE if r[k] is not None)
def ischop(r, k, h):
    return None if r[k] is None else (r[k] >= meds[k] if h else r[k] < meds[k])
print("      " + " ".join(f"{l.split()[0]:>6s}" for _, l, _ in labs))
for k1, l1, h1 in labs:
    cells = []
    for k2, l2, h2 in labs:
        both = [r for r in HAVE if ischop(r, k1, h1) is not None and ischop(r, k2, h2) is not None]
        agree = sum(1 for r in both if ischop(r, k1, h1) == ischop(r, k2, h2))
        cells.append(f"{100*agree/len(both):5.0f}%")
    print(f"  {l1.split()[0]:4s} " + " ".join(f"{c:>6s}" for c in cells))
