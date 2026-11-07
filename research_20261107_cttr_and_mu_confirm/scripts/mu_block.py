"""'왕복장 × 괴리≥0 차단' 의 견고성 — 후보 기준을 고른 것 자체가 선택편향이므로 검증한다.

날짜 LOO : 하루를 빼고 중앙값·차단집합을 다시 만들어도 통과분이 기준선을 이기는가
치환검정 : 날짜 안에서 손익만 섞어 차단집합 평균이 우연히 이만큼 나쁠 확률
적용     : CONFIRM / WAIT 별 효과, 11/06 13:30 포함 여부
"""
import csv
import statistics as st
import sys
import random

sys.stdout.reconfigure(encoding="utf-8")
random.seed(20261108)
NUM = ("mu_hy_gap", "er", "er_60", "n_flag_before", "flag_rate", "range_pos",
       "since_opp_min", "r30", "mfe", "mae")
ROWS = []
for r in csv.DictReader(open("out_mu.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["r30"] is None or r["mae"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    r["pnl"] = -1.6 if r["mae"] <= -1.6 else r["r30"]
    ROWS.append(r)
DAYS = sorted({r["day"] for r in ROWS})
CANDS = [("er_60", "C2 최근 60분 효율비", False), ("n_flag_before", "C3 이전 플래그 수", True),
         ("range_pos", "C6 순변화/고저폭", False), ("er", "C1 당일 효율비", False)]


def blocker(pop, key, hi_chop):
    vals = [r[key] for r in pop if r[key] is not None]
    m = st.median(vals)

    def f(r):
        if r[key] is None or r["mu_hy_gap"] is None:
            return False
        return (r[key] >= m if hi_chop else r[key] < m) and r["mu_hy_gap"] >= 0.0
    return f, m


def rep(g):
    if not g:
        return "0건"
    w = sum(1 for r in g if r["win"])
    p = sum(1 for r in g if r["pnl"] > 0)
    return (f"{len(g):2d}건 승률 {100*w/len(g):5.1f}% 실익 {p:2d}/{len(g):<2d} "
            f"평균 {sum(r['pnl'] for r in g)/len(g):+6.3f}% 합계 {sum(r['pnl'] for r in g):+7.2f}%p")


print(f"기준선 {rep(ROWS)}")
print()
print("=== ① 날짜 LOO — 하루 빼고 중앙값·차단집합 재계산 ===")
for key, lab, h in CANDS:
    ok = 0
    deltas = []
    for d in DAYS:
        pop = [r for r in ROWS if r["day"] != d]
        f, _ = blocker(pop, key, h)
        keep = [r for r in pop if not f(r)]
        a = sum(r["pnl"] for r in keep) / len(keep)
        b = sum(r["pnl"] for r in pop) / len(pop)
        deltas.append(a - b)
        ok += a > b
    print(f"  {lab:22s} {ok}/{len(DAYS)} 일 우위  평균 개선 {sum(deltas)/len(deltas):+.3f}%p  "
          f"최악 {min(deltas):+.3f}%p")

print()
print("=== ② 치환검정 (날짜 안에서 손익만 섞음, 2000회) — 차단집합 평균이 이만큼 낮을 확률 ===")
byd = {}
for r in ROWS:
    byd.setdefault(r["day"], []).append(r)
for key, lab, h in CANDS:
    f, _ = blocker(ROWS, key, h)
    cut = [r for r in ROWS if f(r)]
    obs = sum(r["pnl"] for r in cut) / len(cut)
    null = []
    for _ in range(2000):
        for d, g in byd.items():
            v = [r["pnl"] for r in g]
            random.shuffle(v)
            for r, x in zip(g, v):
                r["_p"] = x
        null.append(sum(r["_p"] for r in cut) / len(cut))
    p = sum(1 for x in null if x <= obs) / len(null)
    print(f"  {lab:22s} 차단 {len(cut):2d}건 관측 {obs:+.3f}%  귀무중앙 {st.median(null):+.3f}  p = {p:.4f}")

print()
print("=== ③ C2 기준 적용 — MU 상태별 ===")
f2, m2 = blocker(ROWS, "er_60", False)
print(f"  왕복 판정: 최근 60분 효율비 < {m2:.4f} (표본 중앙값)")
for stt in ("ENTRY_CONFIRM", "ENTRY_WAIT"):
    g = [r for r in ROWS if r["mu_status"] == stt]
    cut = [r for r in g if f2(r)]
    keep = [r for r in g if not f2(r)]
    print(f"  {stt:14s} 기준선 {rep(g)}")
    print(f"  {'':14s} 차단   {rep(cut)}")
    print(f"  {'':14s} 통과   {rep(keep)}")

print()
print("=== ④ 차단되는 15건 전수 ===")
print(f"  {'날짜 T+3':16s} {'MU':14s} {'방향':10s} {'er60':>7s} {'괴리':>7s} {'MAE':>7s} "
      f"{'30분':>7s} {'손절반영':>7s}")
for r in sorted((r for r in ROWS if f2(r)), key=lambda r: (r["day"], r["t3_at"])):
    print(f"  {r['day'][5:]+' '+r['t3_at']:16s} {r['mu_status']:14s} {r['dir']:10s} "
          f"{r['er_60']:7.4f} {r['mu_hy_gap']:+7.3f} {r['mae']:+7.2f} {r['r30']:+7.3f} {r['pnl']:+7.3f}")

print()
print("=== ⑤ 11/06(금) 전수에 적용 ===")
for r in sorted((r for r in ROWS if r["day"] == "2026-11-06"), key=lambda r: r["t3_at"]):
    g = "결측" if r["mu_hy_gap"] is None else f"{r['mu_hy_gap']:+.3f}"
    e = "-" if r["er_60"] is None else f"{r['er_60']:.4f}"
    print(f"  {r['t3_at']} {r['mu_status']:14s} {r['dir']:10s} er60={e:>7s} "
          f"{'왕복' if (r['er_60'] is not None and r['er_60'] < m2) else '추세':4s} 괴리 {g:>8s} "
          f"=> {'차단' if f2(r) else '통과'}   손절반영 {r['pnl']:+7.3f}")
