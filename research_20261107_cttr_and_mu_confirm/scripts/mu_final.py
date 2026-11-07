"""기존안 vs 신안 — production 모양 그대로 비교.

기존안 : MU ENTRY_CONFIRM 전부 + ENTRY_WAIT 중 selective 승인(체결강도 MATCH ∧ 반대gap<=400)
신안   : 기존안 ∧ NOT(왕복장 × 큰괴리)
         왕복장 = 최근 60분 하이닉스 효율비 < 0.1223 (표본 중앙값)
         큰괴리 = mu_hy_gap >= 0.0 (자연 경계)
         둘 중 하나라도 값이 없으면 통과(fail-open)

성과는 둘 다 본다 — 순진한 30분 전방수익률과, 손절(-1.6%) 반영값.
"""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
ERCUT, GAPCUT, STOP = 0.1223, 0.0, -1.6

NUM = ("mu_hy_gap", "er_60", "r15", "r30", "mfe", "mae")
ROWS = []
for r in csv.DictReader(open("out_mu.csv", encoding="utf-8-sig")):
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])
    if r["r30"] is None or r["mae"] is None:
        continue
    r["win"] = r["touch"] == "UP"
    r["pnl"] = STOP if r["mae"] <= STOP else r["r30"]
    ROWS.append(r)

aux = {}
for r in csv.DictReader(open("out_wait.csv", encoding="utf-8-sig")):
    aux[(r["day"], r["t3_at"], r["dir"])] = r
for r in ROWS:
    a = aux.get((r["day"], r["t3_at"], r["dir"]), {})
    r["opp_gap"] = None if a.get("opp_gap", "") == "" else float(a["opp_gap"])
    r["cttr_match"] = a.get("cttr_match", "NA")


def base_in(r):
    """기존안 진입 후보인가."""
    if r["mu_status"] == "ENTRY_CONFIRM":
        return True
    return r["cttr_match"] == "MATCH" and r["opp_gap"] is not None and r["opp_gap"] <= 400.0


def chop_block(r):
    if r["er_60"] is None or r["mu_hy_gap"] is None:
        return False
    return r["er_60"] < ERCUT and r["mu_hy_gap"] >= GAPCUT


BASE = [r for r in ROWS if base_in(r)]
NEW = [r for r in BASE if not chop_block(r)]
CUT = [r for r in BASE if chop_block(r)]


def rep(g):
    if not g:
        return " 0건"
    w = sum(1 for r in g if r["win"])
    p = sum(1 for r in g if r["pnl"] > 0)
    return (f"{len(g):2d}건  승률 {100*w/len(g):5.1f}%  실익 {p:2d}/{len(g):<2d}={100*p/len(g):4.1f}%  "
            f"순진30분 {sum(r['r30'] for r in g)/len(g):+6.3f}%  "
            f"손절반영 {sum(r['pnl'] for r in g)/len(g):+6.3f}%  "
            f"합계 {sum(r['pnl'] for r in g):+7.2f}%p")


print(f"MU CONFIRM {sum(1 for r in ROWS if r['mu_status']=='ENTRY_CONFIRM')}건 + "
      f"WAIT {sum(1 for r in ROWS if r['mu_status']=='ENTRY_WAIT')}건 = {len(ROWS)}건 "
      f"(10/19~11/06, 15 거래일)")
print()
print("=== 전체 비교 ===")
print(f"  기존안  {rep(BASE)}")
print(f"  신안    {rep(NEW)}")
print(f"  차단분  {rep(CUT)}")
d_w = (100*sum(1 for r in NEW if r["win"])/len(NEW)) - (100*sum(1 for r in BASE if r["win"])/len(BASE))
d_p = sum(r["pnl"] for r in NEW)/len(NEW) - sum(r["pnl"] for r in BASE)/len(BASE)
print(f"  => 승률 {d_w:+.1f}%p / 건당 손절반영 {d_p:+.3f}%p / 합계 "
      f"{sum(r['pnl'] for r in NEW) - sum(r['pnl'] for r in BASE):+.2f}%p / 진입 {len(BASE)}→{len(NEW)}건")

print()
print("=== MU 상태별 ===")
for stt, lab in (("ENTRY_CONFIRM", "CONFIRM"), ("ENTRY_WAIT", "WAIT(selective 승인분)")):
    b = [r for r in BASE if r["mu_status"] == stt]
    n = [r for r in NEW if r["mu_status"] == stt]
    print(f"  [{lab}]")
    print(f"    기존안 {rep(b)}")
    print(f"    신안   {rep(n)}")

print()
print("=== 차단되는 9건 전수 ===")
print(f"  {'날짜 T+3':16s} {'MU':14s} {'방향':10s} {'er60':>7s} {'괴리':>7s} {'MFE':>6s} "
      f"{'MAE':>7s} {'30분':>7s} {'손절반영':>7s}")
for r in sorted(CUT, key=lambda r: (r["day"], r["t3_at"])):
    print(f"  {r['day'][5:]+' '+r['t3_at']:16s} {r['mu_status']:14s} {r['dir']:10s} "
          f"{r['er_60']:7.4f} {r['mu_hy_gap']:+7.3f} {r['mfe']:+6.2f} {r['mae']:+7.2f} "
          f"{r['r30']:+7.3f} {r['pnl']:+7.3f}")
print(f"  차단분 중 실익(손익>0) 건: {sum(1 for r in CUT if r['pnl'] > 0)}건")

print()
print("=== 날짜 LOO — 신안이 기존안을 이기는가 (건당 손절반영) ===")
days = sorted({r["day"] for r in ROWS})
ok = 0
for d in days:
    b = [r for r in BASE if r["day"] != d]
    n = [r for r in NEW if r["day"] != d]
    a1, a2 = sum(r["pnl"] for r in b)/len(b), sum(r["pnl"] for r in n)/len(n)
    ok += a2 > a1
    print(f"  −{d[5:]}  기존 {a1:+6.3f}%  신안 {a2:+6.3f}%  {'O' if a2 > a1 else 'X'}")
print(f"  => {ok}/{len(days)}")

print()
print("=== 일자별 합계 (손절반영 %p) ===")
print(f"  {'날짜':8s} {'기존 건수':>8s} {'기존 합계':>9s} {'신안 건수':>8s} {'신안 합계':>9s}")
for d in days:
    b = [r for r in BASE if r["day"] == d]
    n = [r for r in NEW if r["day"] == d]
    if not b:
        continue
    print(f"  {d[5:]:8s} {len(b):8d} {sum(r['pnl'] for r in b):+9.2f} "
          f"{len(n):8d} {sum(r['pnl'] for r in n):+9.2f}")
