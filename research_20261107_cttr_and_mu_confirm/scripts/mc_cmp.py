"""MU CONFIRM 손실 후보 vs 나머지 — 피처별 분포 비교. n=28 이라 규칙 탐색이 아니라 기술통계만."""
import csv
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROWS = list(csv.DictReader(open("out_confirm.csv", encoding="utf-8-sig")))
NUM = ("opp_gap", "g_flag", "g_t2", "gap_growth", "mu_post2_bp", "mu_acc2", "mu_pre3_bp",
       "mu_session_pct", "hy_session_pct", "mu_hy_gap", "cttr", "hy_pos_in_day",
       "hy_day_range_pct", "since_opp_min", "flag_seq", "r15", "r30", "mfe", "mae")
for r in ROWS:
    for k in NUM:
        r[k] = None if r[k] in ("", None) else float(r[k])

LOSS = [r for r in ROWS if r["touch"] == "DOWN"]            # -1.5% 선터치 = 손절 발동 대용
WIN = [r for r in ROWS if r["touch"] == "UP"]
OTH = [r for r in ROWS if r["touch"] not in ("UP", "DOWN")]
print(f"MU ENTRY_CONFIRM {len(ROWS)}건 — -1.5% 선터치 {len(LOSS)} / +1.5% 선터치 {len(WIN)} / 무 {len(OTH)}")
print(f"  30분 수익률 음수: {sum(1 for r in ROWS if (r['r30'] or 0) < 0)}건")
print()


def med(rows, k):
    v = [r[k] for r in rows if r[k] is not None]
    return st.median(v) if v else None


print(f"{'피처':18s} {'손실(중앙)':>11s} {'승리(중앙)':>11s} {'차':>9s}   겹침")
for k in NUM:
    a, b = med(LOSS, k), med(WIN, k)
    if a is None or b is None:
        continue
    la = sorted(r[k] for r in LOSS if r[k] is not None)
    lb = sorted(r[k] for r in WIN if r[k] is not None)
    sep = "분리" if (max(la) < min(lb) or min(la) > max(lb)) else ""
    print(f"{k:18s} {a:11.3f} {b:11.3f} {a-b:+9.3f}   {sep}")

print()
print("=== 범주형 ===")
for k in ("session", "dir", "cttr_match"):
    vals = sorted({r[k] for r in ROWS})
    for v in vals:
        sub = [r for r in ROWS if r[k] == v]
        nl = sum(1 for r in sub if r["touch"] == "DOWN")
        print(f"  {k}={v:10s} {len(sub):2d}건  -1.5% 선터치 {nl}건 ({100*nl/len(sub):4.1f}%)  "
              f"평균30분 {sum(r['r30'] or 0 for r in sub)/len(sub):+6.3f}%")

print()
print("=== 손실 후보 전수 ===")
hdr = (f"{'날짜':10s} {'T+3':5s} {'방향':9s} {'opp':>6s} {'MUses':>7s} {'HYses':>7s} {'MU-HY':>7s} "
       f"{'post2':>7s} {'acc2':>7s} {'위치':>5s} {'폭':>5s} {'MFE':>6s} {'MAE':>7s} {'r30':>7s}")
for lab, rows in (("손실(-1.5% 선터치)", LOSS), ("승리(+1.5% 선터치)", WIN), ("무", OTH)):
    print(f"\n[{lab}] {len(rows)}건")
    print("  " + hdr)
    for r in rows:
        f = lambda k, w=7, p=3: ("{:>%d}" % w).format("-") if r[k] is None else ("{:%d.%df}" % (w, p)).format(r[k])
        print(f"  {r['day']:10s} {r['t3_at']:5s} {r['dir']:9s} {f('opp_gap',6,1)} {f('mu_session_pct')} "
              f"{f('hy_session_pct')} {f('mu_hy_gap')} {f('mu_post2_bp')} {f('mu_acc2')} "
              f"{f('hy_pos_in_day',5,2)} {f('hy_day_range_pct',5,1)} {f('mfe',6,2)} {f('mae',7,2)} {f('r30')}")

print()
print("=== MU-HY 괴리 부호별 (방향부호 적용: + = 하이닉스가 MU 에 못 미침) ===")
for lab, sel in (("MU-HY gap > 0 (하이닉스 지연)", lambda r: (r["mu_hy_gap"] or 0) > 0),
                 ("MU-HY gap <= 0 (하이닉스 선행)", lambda r: (r["mu_hy_gap"] or 0) <= 0)):
    sub = [r for r in ROWS if r["mu_hy_gap"] is not None and sel(r)]
    if not sub:
        continue
    nl = sum(1 for r in sub if r["touch"] == "DOWN")
    nw = sum(1 for r in sub if r["touch"] == "UP")
    print(f"  {lab:34s} {len(sub):2d}건  손실 {nl} / 승리 {nw}  "
          f"평균30분 {sum(r['r30'] or 0 for r in sub)/len(sub):+6.3f}%  "
          f"평균MAE {sum(r['mae'] or 0 for r in sub)/len(sub):+6.2f}%")
