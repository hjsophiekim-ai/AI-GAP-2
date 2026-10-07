"""A13 — 최종 채택조건 배터리. 모든 후보 pickle 을 모아 한 표로."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import axval as V
from common import pnl

TR = {}
for f in ("ax78.pkl", "ax78_d.pkl", "ax78_grid2.pkl", "ax78_e35.pkl"):
    p = HERE / f
    if not p.exists():
        print(f"(없음: {f})"); continue
    d = pickle.load(open(p, "rb"))
    for k, v in d["trades"].items():
        if k not in TR:
            TR[k] = v
    dates = d["dates"]
base = TR["N1"]
rows = [V.report(k, ts, base, dates) for k, ts in TR.items() if k != "N1"]
rows.sort(key=lambda r: -(r["78d_d"] + 3 * r["30d_d"]))

print(f"후보 {len(rows)}개  (탐색공간 전체를 그대로 보고한다)\n")
print(f"{'규칙':26s} {'Δ78':>8s} {'Δ30':>7s} {'ΔT10':>7s} {'ΔT10_30':>8s} "
      f"{'앞':>7s} {'뒤':>7s} {'WF승/패':>8s} {'변경':>4s} {'개↑':>3s} {'악↓':>3s} {'Σ단순':>7s} {'Σ-3':>7s}")
for r in rows[:28]:
    print(f"{r['name']:26s} {r['78d_d']:+8.2f} {r['30d_d']:+7.2f} {r['top10_d']:+7.2f} {r['top10_d30']:+8.2f} "
          f"{r['half'][0]:+7.2f} {r['half'][1]:+7.2f} {r['wf6_win']:4d}/{r['wf6_lose']:<3d} "
          f"{r['chg']:4d} {r['up']:3d} {r['dn']:3d} {r['sum_d']:+7.2f} {r['sum_d_ex3']:+7.2f}")

TOP = [r for r in rows if r["78d_d"] > 0 and r["30d_d"] > 0][:5]
print("\n\n" + "=" * 100)
print("30일·78일 모두 개선인 후보 상세")
print("=" * 100)
for r in TOP:
    print(f"\n### {r['name']}")
    print(f"  78일 {r['78d_b']:.2f} -> {r['78d_a']:.2f} ({r['78d_d']:+.2f}%p)   "
          f"30일 {r['30d_b']:.2f} -> {r['30d_a']:.2f} ({r['30d_d']:+.2f}%p)")
    print(f"  PF {r['78d_pf_b']:.3f}->{r['78d_pf_a']:.3f} (78) / {r['30d_pf_b']:.3f}->{r['30d_pf_a']:.3f} (30)   "
          f"MDD {r['78d_mdd_b']:.2f}->{r['78d_mdd_a']:.2f} / {r['30d_mdd_b']:.2f}->{r['30d_mdd_a']:.2f}")
    print(f"  -Top1/3/10(78) {r['top1_d']:+.2f} / {r['top3_d']:+.2f} / {r['top10_d']:+.2f}   "
          f"(30) {r['top1_d30']:+.2f} / {r['top3_d30']:+.2f} / {r['top10_d30']:+.2f}")
    print(f"  앞/뒤 39일: {r['half'][0]:+.2f} / {r['half'][1]:+.2f}")
    print(f"  5분할 : " + "  ".join(f"{x:+.2f}" for x in r["k5"]))
    print(f"  WF 6분할: " + "  ".join(f"{x:+.2f}" for x in r["wf6"]) +
          f"   → 승 {r['wf6_win']} / 패 {r['wf6_lose']} / 무 {6-r['wf6_win']-r['wf6_lose']}")
    print(f"  변경거래 {r['chg']}건 (개선 {r['up']} / 악화 {r['dn']}), 단순합 {r['sum_d']:+.2f}%p, "
          f"상위1/2/3 개선거래 제외시 {r['sum_d_ex1']:+.2f} / {r['sum_d_ex2']:+.2f} / {r['sum_d_ex3']:+.2f}")
    if r["only_a"] or r["only_b"]:
        print(f"  ** 진입집합 변화: 후보에만 {r['only_a']}건, N1 에만 {r['only_b']}건")
    print("  거래별 차이:")
    for (d, et), x in r["diffs"]:
        print(f"    {d} {str(et)[11:16]}  {x:+7.3f}")
    c = V.verdict(r)
    print("  채택조건 " + " | ".join(f"{'PASS' if v else 'FAIL'} {k}" for k, v in c.items()))
    print(f"  => {'채택' if all(c.values()) else '기각'} (미충족 "
          f"{[k for k, v in c.items() if not v]})")
