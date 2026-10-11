"""V7 — 구간 안정성(앞26/중26/뒤26, 앞39/뒤39, 월별) + walk-forward 6/9fold.
arm/give 는 어느 fold 에서도 재최적화하지 않는다(고정 5.0/1.5)."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import compound, excl_topn
import hengine as H

V1 = pickle.load(open(A.HERE / "v1.pkl", "rb"))
V6 = pickle.load(open(A.HERE / "v6.pkl", "rb"))
base, c1, D = V1["base"], V1["c1"], V1["dates"]
key = lambda t: (t["date"], t["entry_time"])
B = {key(t): t for t in base}; K = {key(t): t for t in c1}
FIRE = {k for k in K if K[k].get("pp_fired")}


def seg(name, W):
    ws = set(W)
    cb, cc = compound(base, W), compound(c1, W)
    f = [k for k in FIRE if k[0] in ws]
    d = [(k, (K[k]["net_pct"] - B[k]["net_pct"]) * B[k]["w1a"]) for k in f]
    up = sum(1 for _, x in d if x > 1e-9); dn = sum(1 for _, x in d if x < -1e-9)
    print(f"{name:18s} {len(W):3d}일  N1 {cb:8.2f}  C1 {cc:8.2f}  uplift {cc-cb:+8.2f}  "
          f"발동 {len(f):2d}건 (개선 {up} / 악화 {dn})  단순합 {sum(x for _, x in d):+6.2f}"
          + ("   " + ", ".join(f"{k[0][4:]}{x:+.2f}" for k, x in sorted(d, key=lambda y: -y[1])) if d else ""))


print("=== 7. 시간구간 안정성 ===")
n = len(D)
print("\n[3분할 26/26/26]")
for i, lab in enumerate(("앞26", "중26", "뒤26")):
    seg(lab, D[i * 26:(i + 1) * 26])
print("\n[2분할 39/39]")
seg("앞39", D[:39]); seg("뒤39", D[39:])
print("\n[월별]")
for m, lab in (("202605", "2026-05(3일)"), ("202606", "2026-06"), ("202607", "2026-07"),
               ("202608", "2026-08"), ("202609", "2026-09")):
    W = [x for x in D if x.startswith(m)]
    if W:
        seg(lab, W)
print("\n[참고 창]")
seg("최근30일", D[-30:]); seg("8월이후", [x for x in D if x >= "20260801"]); seg("전체78일", D)

print("\n=== 8. Walk-forward (arm/give 고정 5.0/1.5, fold별 재최적화 없음) ===")
for kf in (6, 9, 13):
    sz = len(D) // kf
    wins = loses = zero = 0
    print(f"\n  [{kf}-fold, 각 {sz}일]")
    for i in range(kf):
        W = D[i * sz:(i + 1) * sz if i < kf - 1 else len(D)]
        u = compound(c1, W) - compound(base, W)
        f = [k for k in FIRE if k[0] in set(W)]
        tag = "승" if u > 1e-9 else ("패" if u < -1e-9 else "무")
        wins += u > 1e-9; loses += u < -1e-9; zero += abs(u) <= 1e-9
        print(f"    fold{i+1:2d} {W[0]}~{W[-1]} ({len(W):2d}일) uplift {u:+8.2f} [{tag}] "
              f"발동 {len(f)}건 " + (", ".join(k[0][4:] for k in sorted(f)) if f else ""))
    print(f"    → {wins}승 {loses}패 {zero}무")

print("\n=== 기타 지표 (78일) ===")
mb, mc = H.metrics(base, D), H.metrics(c1, D)
for lab, a, b in (("복리%", mb["compound_pct"], mc["compound_pct"]),
                  ("PF", mb["pf"], mc["pf"]), ("MDD%", mb["mdd_pct"], mc["mdd_pct"]),
                  ("승률%", mb["win_rate_pct"], mc["win_rate_pct"]),
                  ("일승률%", mb["day_win_rate_pct"], mc["day_win_rate_pct"]),
                  ("-Top1", excl_topn(base, D, 1), excl_topn(c1, D, 1)),
                  ("-Top3", excl_topn(base, D, 3), excl_topn(c1, D, 3)),
                  ("-Top10", excl_topn(base, D, 10), excl_topn(c1, D, 10)),
                  ("최대연속손실일", mb["max_loss_streak"], mc["max_loss_streak"]),
                  ("거래수", mb["trades"], mc["trades"])):
    print(f"  {lab:14s} N1 {a:10.3f}  →  C1 {b:10.3f}   Δ {b-a:+8.3f}")

print("\n=== 13. 09-19 이후 신규데이터 ===")
print(f"  ctx 마지막 영업일 = {D[-1]}. 캐시·재수신본 모두 0918 까지이며,")
print("  ETF(0193T0/0197X0) 과거 분봉 조회 한도로 창을 더 넓힐 수 없다.")
print("  → OOS 구간 없음. 신규 데이터 수신은 별도 요청 시 수행(브로커 API 호출).")
