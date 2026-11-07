"""피처를 반으로 갈랐을 때 손실률이 어떻게 갈리는지 — n=28 이므로 기술통계일 뿐, 임계값 제안이 아니다."""
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
    # 방향 정규화: 추격도 = 당일 범위에서 진입이 이미 간 쪽에 얼마나 붙었나
    p = r["hy_pos_in_day"]
    r["chase"] = None if p is None else (p if r["dir"] == "UP_RED" else 1 - p)
    r["mu_abs"] = None if r["mu_session_pct"] is None else abs(r["mu_session_pct"])
    r["loss"] = r["touch"] == "DOWN"


def split(k, label):
    sub = [r for r in ROWS if r[k] is not None]
    if len(sub) < 10:
        return
    m = st.median(r[k] for r in sub)
    hi = [r for r in sub if r[k] >= m]
    lo = [r for r in sub if r[k] < m]
    out = []
    for nm, g in (("상위", hi), ("하위", lo)):
        nl = sum(1 for r in g if r["loss"])
        out.append(f"{nm} {len(g):2d}건 손실 {nl} ({100*nl/len(g):4.1f}%) "
                   f"평균30분 {sum(r['r30'] or 0 for r in g)/len(g):+6.3f}% "
                   f"평균MAE {sum(r['mae'] or 0 for r in g)/len(g):+6.2f}%")
    print(f"{label:28s} (중앙 {m:8.2f})  " + " | ".join(out))


print("=== 피처 중앙값 기준 반 가르기 ===")
for k, lab in (("mu_acc2", "MU 가속 acc2"), ("mu_post2_bp", "MU T+2 상승 post2"),
               ("mu_pre3_bp", "MU 플래그前 3분 pre3"), ("g_flag", "플래그봉 gap"),
               ("g_t2", "T+2 gap"), ("gap_growth", "gap 성장폭"), ("opp_gap", "반대 gap"),
               ("mu_abs", "MU 세션 이동폭 |%|"), ("mu_hy_gap", "MU-HY 괴리"),
               ("chase", "추격도(당일범위)"), ("hy_day_range_pct", "당일 변동폭"),
               ("since_opp_min", "직전 반대플래그 경과분"), ("cttr", "누적 체결강도")):
    split(k, lab)

print()
print("=== MU 확인강도 상위권 (post2 + acc2 둘 다 상위 절반) ===")
mp = st.median(r["mu_post2_bp"] for r in ROWS if r["mu_post2_bp"] is not None)
ma = st.median(r["mu_acc2"] for r in ROWS if r["mu_acc2"] is not None)
strong = [r for r in ROWS if (r["mu_post2_bp"] or 0) >= mp and (r["mu_acc2"] or 0) >= ma]
weak = [r for r in ROWS if r not in strong]
for nm, g in (("강한 확인", strong), ("약한 확인", weak)):
    nl = sum(1 for r in g if r["loss"])
    print(f"  {nm} {len(g):2d}건  손실 {nl} ({100*nl/len(g):4.1f}%)  "
          f"평균30분 {sum(r['r30'] or 0 for r in g)/len(g):+6.3f}%  "
          f"평균MFE {sum(r['mfe'] or 0 for r in g)/len(g):+5.2f}%  "
          f"평균MAE {sum(r['mae'] or 0 for r in g)/len(g):+6.2f}%")
print("  강한 확인 명단: " + ", ".join(f"{r['day'][5:]} {r['t3_at']}{'(손실)' if r['loss'] else ''}" for r in strong))

print()
print("=== post2 / acc2 순위표 (큰 쪽부터) ===")
rk = sorted((r for r in ROWS if r["mu_acc2"] is not None), key=lambda r: -(r["mu_acc2"]))
print(f"  {'순':>2s} {'날짜 T+3':16s} {'방향':9s} {'post2':>7s} {'acc2':>7s} {'MAE':>7s} {'r30':>7s}  결과")
for i, r in enumerate(rk, 1):
    print(f"  {i:2d} {r['day'][5:]+' '+r['t3_at']:16s} {r['dir']:9s} {r['mu_post2_bp']:7.1f} "
          f"{r['mu_acc2']:7.1f} {r['mae']:7.2f} {r['r30']:7.3f}  {'손실' if r['loss'] else ('승리' if r['touch']=='UP' else '무')}")
