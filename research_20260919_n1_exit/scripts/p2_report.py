"""P2 — MFE 구간 분석 + 채택조건 배터리 + 지정거래 점검."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pandas as pd
import axval as V
from common import compound, excl_topn, pnl

D = pickle.load(open(HERE / "pp78.pkl", "rb"))
TR, dates = D["trades"], D["dates"]
base = TR["N1"]; W30 = dates[-30:]
key = lambda t: (t["date"], t["entry_time"])
B = {key(t): t for t in base}

# ── 0. N1 의 MFE 구간별 반납 구조 ──────────────────────────────────────────
df = pd.DataFrame(base)
print("=" * 100)
print("0. N1 78일 — MFE(peak) 구간별 반납")
print("=" * 100)
for lv in (3.0, 3.5, 4.0):
    h = df[df.peak_net_pct >= lv]
    gb = h.peak_net_pct - h.net_pct
    bad = h[h.net_pct < lv - 1.0]
    print(f"  MFE>={lv}: {len(h):3d}거래  평균peak={h.peak_net_pct.mean():5.2f} "
          f"평균net={h.net_pct.mean():+5.2f}  평균반납={gb.mean():4.2f}  최대반납={gb.max():4.2f}  "
          f"반납>1.0%p {int((gb>1.0).sum()):2d}건(합 {gb[gb>1.0].sum():5.2f}%p)  "
          f"net<{lv-1.0:.1f} {len(bad):2d}건")
print(f"  TP2 8% 도달(runner): {int((df.peak_net_pct>=8.0).sum())}거래, "
      f"평균net={df[df.peak_net_pct>=8.0].net_pct.mean():.3f}")

rows = [V.report(k, ts, base, dates) for k, ts in TR.items() if k != "N1"]
rows.sort(key=lambda r: -(r["78d_d"] + 3 * r["30d_d"]))

print("\n" + "=" * 130)
print("1. 후보 전량 (사후선택 없음)")
print("=" * 130)
print(f"{'후보':20s} {'Δ78':>8s} {'Δ30':>7s} {'ΔPF78':>7s} {'ΔMDD':>6s} {'ΔT10':>7s} {'ΔT10_30':>8s} "
      f"{'앞39':>7s} {'뒤39':>7s} {'WF':>6s} {'변경':>4s} {'개↑':>3s} {'악↓':>3s} {'Σ단순':>7s} {'Σ-3':>7s}")
for r in rows:
    print(f"{r['name']:20s} {r['78d_d']:+8.2f} {r['30d_d']:+7.2f} "
          f"{(r['78d_pf_a'] or 0)-(r['78d_pf_b'] or 0):+7.3f} "
          f"{r['78d_mdd_a']-r['78d_mdd_b']:+6.2f} {r['top10_d']:+7.2f} {r['top10_d30']:+8.2f} "
          f"{r['half'][0]:+7.2f} {r['half'][1]:+7.2f} {r['wf6_win']:2d}/{r['wf6_lose']:<2d} "
          f"{r['chg']:4d} {r['up']:3d} {r['dn']:3d} {r['sum_d']:+7.2f} {r['sum_d_ex3']:+7.2f}")

# ── 지정거래 점검 ──────────────────────────────────────────────────────────
RESCUE = ["20260803", "20260827", "20260831"]
KEEP = ["20260527", "20260624", "20260703", "20260706", "20260714", "20260731"]
RUN8 = sorted(k for k, t in B.items() if t["peak_net_pct"] >= 8.0)

print("\n" + "=" * 130)
print("2. 지정거래 점검 — 살려야 할 3건 / 자르면 안 되는 6건 / runner 9건")
print("=" * 130)
print(f"{'후보':20s} {'살림3 Δ합':>10s} {'살림 건수':>8s} | {'유지6 Δ합':>10s} {'잘린 건수':>8s} | "
      f"{'runner9 Δ합':>11s} {'훼손 건수':>8s} | {'전체 Δ단순':>10s}")
for r in rows:
    ts = TR[r["name"]]; ka = {key(t): t for t in ts}
    def sub(keys):
        return [(k, ka[k]["net_pct"] - B[k]["net_pct"]) for k in keys if k in ka]
    rk = [k for k in B if k[0] in RESCUE]
    kk = [k for k in B if k[0] in KEEP]
    a = sub(rk); b = sub(kk); cc = sub(RUN8)
    print(f"{r['name']:20s} {sum(x for _, x in a):+10.2f} {sum(1 for _, x in a if x>0.01):8d} | "
          f"{sum(x for _, x in b):+10.2f} {sum(1 for _, x in b if x<-0.01):8d} | "
          f"{sum(x for _, x in cc):+11.2f} {sum(1 for _, x in cc if x<-0.01):8d} | "
          f"{r['sum_d']:+10.2f}")

# ── 채택조건 ───────────────────────────────────────────────────────────────
def verdict2(r, ts):
    ka = {key(t): t for t in ts}
    c = dict(V.verdict(r))
    c.pop("상위3 개선거래 제외 후 양수")
    c["상위3 개선 제외 uplift>=0"] = r["sum_d_ex3"] >= 0
    c["변경거래 >=10"] = r["chg"] >= 10
    dmg = sum(ka[k]["net_pct"] - B[k]["net_pct"] for k in RUN8 if k in ka)
    c["runner 손상 없음"] = dmg >= -1e-9
    return c, dmg

print("\n" + "=" * 130)
print("3. 채택조건 (30/78 모두 비악화인 후보 상세)")
print("=" * 130)
TOP = [r for r in rows if r["78d_d"] >= 0 and r["30d_d"] >= 0]
if not TOP:
    TOP = rows[:3]
    print("(30·78 동시 비악화 후보 없음 — 상위 3개를 대신 보고)")
for r in TOP[:5]:
    ts = TR[r["name"]]; ka = {key(t): t for t in ts}
    c, dmg = verdict2(r, ts)
    print(f"\n### {r['name']}")
    print(f"  78일 {r['78d_b']:.2f} -> {r['78d_a']:.2f} ({r['78d_d']:+.2f})   "
          f"30일 {r['30d_b']:.2f} -> {r['30d_a']:.2f} ({r['30d_d']:+.2f})")
    print(f"  PF {r['78d_pf_b']:.3f}->{r['78d_pf_a']:.3f} / {r['30d_pf_b']:.3f}->{r['30d_pf_a']:.3f}  "
          f"MDD {r['78d_mdd_b']:.2f}->{r['78d_mdd_a']:.2f} / {r['30d_mdd_b']:.2f}->{r['30d_mdd_a']:.2f}")
    print(f"  -Top1/3/10 (78) {r['top1_d']:+.2f}/{r['top3_d']:+.2f}/{r['top10_d']:+.2f}   "
          f"(30) {r['top1_d30']:+.2f}/{r['top3_d30']:+.2f}/{r['top10_d30']:+.2f}")
    print(f"  앞39/뒤39 {r['half'][0]:+.2f} / {r['half'][1]:+.2f}   "
          f"5분할 " + " ".join(f"{x:+.2f}" for x in r["k5"]))
    print(f"  WF6 " + " ".join(f"{x:+.2f}" for x in r["wf6"]) +
          f"  → {r['wf6_win']}승 {r['wf6_lose']}패 {6-r['wf6_win']-r['wf6_lose']}무")
    print(f"  변경 {r['chg']}건 (개선 {r['up']}/악화 {r['dn']}) 단순합 {r['sum_d']:+.2f} "
          f"상위1/2/3 제외 {r['sum_d_ex1']:+.2f}/{r['sum_d_ex2']:+.2f}/{r['sum_d_ex3']:+.2f}  "
          f"runner 손상 {dmg:+.2f}")
    if r["only_a"] or r["only_b"]:
        print(f"  ** 진입집합 변화: 후보에만 {r['only_a']}, N1 에만 {r['only_b']}")
    print("  개선/악화 거래 전량:")
    for k, x in r["diffs"]:
        b_, a_ = B[k], ka[k]
        print(f"    {k[0]} {str(k[1])[11:16]}  peak={b_['peak_net_pct']:5.2f}  "
              f"N1 {b_['net_pct']:+7.3f} [{str(b_['exit_reason'])[:24]:24s}] -> "
              f"{a_['net_pct']:+7.3f} [{str(a_['exit_reason'])[:16]:16s}]  Δ{x:+7.3f}")
    print("  " + " | ".join(f"{'PASS' if v else 'FAIL'} {kk}" for kk, v in c.items()))
    print(f"  => {'채택' if all(c.values()) else '기각'}  미충족={[kk for kk,v in c.items() if not v]}")
