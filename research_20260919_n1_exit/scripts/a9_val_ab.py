"""A9 — A/B 계열 채택조건 배터리 + 변경거래 명세."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import axval as V

D = pickle.load(open(HERE / "ax78.pkl", "rb"))
TR, dates = D["trades"], D["dates"]
base = TR["N1"]
rows = []
for k, ts in TR.items():
    if k == "N1":
        continue
    rows.append(V.report(k, ts, base, dates))
rows.sort(key=lambda r: -(r["78d_d"] + r["30d_d"]))

print(f"{'규칙':20s} {'Δ78':>7s} {'Δ30':>7s} {'ΔPF78':>7s} {'ΔT10':>7s} {'ΔT10_30':>8s} "
      f"{'앞/뒤':>15s} {'WF승':>5s} {'변경':>4s} {'개선':>4s} {'악화':>4s} {'Σ차':>7s} {'Σ-상위3':>8s}")
for r in rows:
    h = f"{r['half'][0]:+6.2f}/{r['half'][1]:+6.2f}"
    print(f"{r['name']:20s} {r['78d_d']:+7.2f} {r['30d_d']:+7.2f} "
          f"{(r['78d_pf_a'] or 0)-(r['78d_pf_b'] or 0):+7.3f} {r['top10_d']:+7.2f} {r['top10_d30']:+8.2f} "
          f"{h:>15s} {r['wf6_win']}/{6-r['wf6_win']-sum(1 for x in r['wf6'] if abs(x)<1e-9):>3s} "
          if False else
          f"{r['name']:20s} {r['78d_d']:+7.2f} {r['30d_d']:+7.2f} "
          f"{(r['78d_pf_a'] or 0)-(r['78d_pf_b'] or 0):+7.3f} {r['top10_d']:+7.2f} {r['top10_d30']:+8.2f} "
          f"{h:>15s} {r['wf6_win']:2d}/{r['wf6_lose']:<2d} {r['chg']:4d} {r['up']:4d} {r['dn']:4d} "
          f"{r['sum_d']:+7.2f} {r['sum_d_ex3']:+8.2f}")

print("\n\n=== 상위 후보 상세 ===")
for r in rows[:4]:
    print(f"\n--- {r['name']} ---")
    print("  WF6:", " ".join(f"{x:+.2f}" for x in r["wf6"]),
          f"  (승 {r['wf6_win']} / 패 {r['wf6_lose']})")
    print("  5분할:", " ".join(f"{x:+.2f}" for x in r["k5"]))
    print("  거래별 차이(w1a 반영, 큰 것부터):")
    for (d, et), x in r["diffs"][:14]:
        print(f"    {d} {str(et)[11:16]}  {x:+7.3f}")
    c = V.verdict(r)
    print("  채택조건:", "  ".join(f"{'O' if v else 'X'}{k}" for k, v in c.items()))
