# -*- coding: utf-8 -*-
"""[연구 2] 13:30 이후 DOWN_BLUE — 최근 30거래일. READ-ONLY.

진입집합/청산 불변, BASE sizing 위에서 **주문금액 배수만** 바꾼다.
(사이징은 net_pct·청산시각·진입집합에 영향이 없음을 앞선 연구에서 코드로 증명했다)
"""
from __future__ import annotations
import sys, pickle, statistics as st
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W30 = D[-30:]
S30 = set(W30)
TS = K.load()
A = K.size_chain(TS, None)
A30 = K.krw_pnl(A, W30)
rng = np.random.default_rng(20260921)
hhmm = lambda t: t["entry_time"][11:16]
is_blue = lambda t: t["direction"] == "DOWN_BLUE"


def grp(rows, pred):
    return [t for t in rows if t["date"] in S30 and pred(t)]


def stats(tag, g):
    if not g:
        print(f"  {tag:22s} (없음)"); return
    n = [t["net_pct"] for t in g]
    print(f"  {tag:22s} {len(g):3d} {sum(n)/len(n):+8.3f} {st.median(n):+8.3f} "
          f"{sum(1 for x in n if x > 0)/len(n)*100:6.1f}% "
          f"{(lambda w, l: (w/l if l else float('inf')))(sum(x for x in n if x>0), -sum(x for x in n if x<0)):6.2f} "
          f"{sum(t['peak_net_pct'] for t in g)/len(g):7.3f} "
          f"{sum(t['mae_net_pct'] for t in g)/len(g):7.3f} "
          f"{sum(t['pnl_krw'] for t in g):12,.0f} "
          f"{sum(t['hold_minutes'] for t in g)/len(g):7.1f} "
          f"{'/'.join(str(sum(1 for t in g if t['slot']==i)) for i in (1,2,3)):>8s} "
          f"{sum(1 for t in g if t['peak_net_pct']>=8.0)/len(g)*100:6.1f}%")


CUT = "13:30"
print("=" * 126)
print(f"[연구 2] 13:30 이후 DOWN_BLUE — 30거래일 {W30[0]}~{W30[-1]}")
print("=" * 126)
print(f"  BASE: {A30:,.0f} KRW / PF {K.pf(A, W30):.3f} / MDD {K.mdd_krw(A, W30):.2f}%")
print()
print(f"  {'그룹':22s} {'n':>3s} {'평균net':>8s} {'중앙net':>8s} {'승률':>7s} {'PF':>6s} "
      f"{'평균MFE':>7s} {'평균MAE':>7s} {'실현KRW':>12s} {'보유분':>7s} {'slot1/2/3':>8s} {'runner':>7s}")
print("  " + "-" * 122)
stats("BLUE 13:30 이전", grp(A, lambda t: is_blue(t) and hhmm(t) < CUT))
stats("BLUE 13:30 이후", grp(A, lambda t: is_blue(t) and hhmm(t) >= CUT))
stats("(참고) BLUE 전체", grp(A, is_blue))
stats("(참고) RED 13:30 이전", grp(A, lambda t: not is_blue(t) and hhmm(t) < CUT))
stats("(참고) RED 13:30 이후", grp(A, lambda t: not is_blue(t) and hhmm(t) >= CUT))
stats("(참고) 전체", grp(A, lambda t: True))

late = grp(A, lambda t: is_blue(t) and hhmm(t) >= CUT)
print(f"\n  13:30 이후 BLUE 전량 ({len(late)}건)")
if late:
    print(f"    {'날짜':9s} {'진입':6s} {'sl':>2s} {'net%':>7s} {'MFE':>7s} {'MAE':>7s} "
          f"{'보유분':>6s} {'주문KRW':>11s} {'손익KRW':>11s} {'청산사유':24s}")
    for t in sorted(late, key=lambda x: x["date"]):
        print(f"    {t['date']:9s} {hhmm(t):6s} {t['slot']:2d} {t['net_pct']:7.2f} "
              f"{t['peak_net_pct']:7.2f} {t['mae_net_pct']:7.2f} {t['hold_minutes']:6.0f} "
              f"{t['actual_krw']:11,.0f} {t['pnl_krw']:11,.0f} {t['exit_reason']:24s}")


# ── 배수 비교 ───────────────────────────────────────────────────────────
def boost(mult, cut=CUT):
    keys = {(t["date"], t["entry_time"]) for t in TS
            if t["direction"] == "DOWN_BLUE" and t["entry_time"][11:16] >= cut}
    return K.size_chain(TS, extra_of=lambda t: mult if (t["date"], t["entry_time"]) in keys else 1.0)


print()
print("=" * 112)
print(f"{CUT} 이후 BLUE 배수 비교 (daily cap 30M 유지, 진입집합 불변)")
print("=" * 112)
print(f"  {'배수':>6s} {'30d P/L':>12s} {'uplift':>11s} {'PF':>6s} {'MDD%':>6s} {'사용률':>7s} "
      f"{'-Top1':>11s} {'-Top3':>11s} {'cap발동':>7s}")
print("  " + "-" * 108)
RUNS = {}
for m in (1.00, 1.10, 1.20, 1.30):
    r = boost(m)
    RUNS[m] = r
    b = K.budget_stats(r, W30)
    print(f"  {m:6.2f} {K.krw_pnl(r, W30):12,.0f} {K.krw_pnl(r, W30)-A30:+11,.0f} "
          f"{K.pf(r, W30):6.3f} {K.mdd_krw(r, W30):6.2f} {b['util_pct']:6.1f}% "
          f"{K.excl_top_krw(r, W30, 1)-K.excl_top_krw(A, W30, 1):+11,.0f} "
          f"{K.excl_top_krw(r, W30, 3)-K.excl_top_krw(A, W30, 3):+11,.0f} {b['cap_hits']:7d}")

print()
print("=" * 112)
print("부트스트랩 10,000회 (일단위, 30일) — 배수 1.30 기준")
print("=" * 112)
da = K.daily_krw(A, W30)
for m in (1.10, 1.20, 1.30):
    db = K.daily_krw(RUNS[m], W30)
    v = np.array([db[d] - da[d] for d in W30])
    bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
    print(f"  배수 {m:.2f}  P(>0)={(bt>0).mean()*100:6.2f}%  p5={np.percentile(bt,5):+11,.0f}  "
          f"중앙={np.percentile(bt,50):+11,.0f}  p95={np.percentile(bt,95):+11,.0f}  "
          f"영향일수={int((v!=0).sum())}")

print()
print("=" * 112)
print("시간 기준 민감도 (배수 1.20 고정)")
print("=" * 112)
print(f"  {'기준시각':>8s} {'해당 BLUE':>9s} {'평균net':>8s} {'30d uplift':>12s} {'PF':>6s} {'MDD%':>6s}")
print("  " + "-" * 60)
for cut in ("13:00", "13:30", "14:00"):
    g = grp(A, lambda t, c=cut: is_blue(t) and hhmm(t) >= c)
    r = boost(1.20, cut)
    mn = (sum(t["net_pct"] for t in g) / len(g)) if g else 0.0
    print(f"  {cut:>8s} {len(g):9d} {mn:+8.3f} {K.krw_pnl(r, W30)-A30:+12,.0f} "
          f"{K.pf(r, W30):6.3f} {K.mdd_krw(r, W30):6.2f}")

print()
print("=" * 112)
print("표본 안정성 — 78일 전체에서도 같은 우위가 있는가")
print("=" * 112)
S78 = set(D)
g78l = [t for t in A if t["date"] in S78 and is_blue(t) and hhmm(t) >= CUT]
g78e = [t for t in A if t["date"] in S78 and is_blue(t) and hhmm(t) < CUT]
for tag, g in (("78일 BLUE 13:30 이전", g78e), ("78일 BLUE 13:30 이후", g78l)):
    n = [t["net_pct"] for t in g]
    print(f"  {tag:22s} n={len(g):3d} 평균 {sum(n)/len(n):+.3f}% 중앙 {st.median(n):+.3f}% "
          f"승률 {sum(1 for x in n if x>0)/len(n)*100:.1f}% KRW {sum(t['pnl_krw'] for t in g):+,.0f}")
mn_l = np.array([t["net_pct"] for t in g78l]); mn_e = np.array([t["net_pct"] for t in g78e])
bl = mn_l[rng.integers(0, len(mn_l), size=(10000, len(mn_l)))].mean(axis=1)
be = mn_e[rng.integers(0, len(mn_e), size=(10000, len(mn_e)))].mean(axis=1)
print(f"  거래단위 부트스트랩 78일: P(13:30이후 평균 > 이전 평균) = {(bl > be).mean()*100:.2f}%")
