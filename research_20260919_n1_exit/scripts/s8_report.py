"""S8 — 최종 후보 3개 확정표 + 변경거래 전량 + 창별 비교."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize, compound, excl_topn
import axval as V

c = A.ctx(78); D = c.dates
TR = {}
for f in ("s3.pkl", "s4.pkl", "s5.pkl", "s6.pkl", "s7.pkl"):
    d = pickle.load(open(A.HERE / f, "rb"))
    for k, v in d["trades"].items():
        TR.setdefault(k, v)
base = TR["N1"]; key = lambda t: (t["date"], t["entry_time"])
B = {key(t): t for t in base}
RUN8 = sorted(x for x, t in B.items() if t["peak_net_pct"] >= 8.0)

FINAL = {
  "PP5.0 (C1)":     "PPonly_a5.0_g1.5",
  "PP4.5 (C2)":     "PPonly_a4.5_g1.5",
  "PP5.0+E3.5 (C3)": "E35+PP_a5.0",
}
print("=" * 126)
print("최종 후보 — 채택조건 전체")
print("=" * 126)
for lbl, k in FINAL.items():
    ts = TR[k]; r = V.report(k, ts, base, D); m = summarize(ts, D)
    ka = {key(t): t for t in ts}
    dmg = sum(ka[x]["net_pct"]-B[x]["net_pct"] for x in RUN8 if x in ka)
    cond = {
      "30일 비악화": r["30d_d"] >= -1e-9, "78일 비악화": r["78d_d"] >= -1e-9,
      "PF 비악화(78)": (r["78d_pf_a"] or 0) >= (r["78d_pf_b"] or 0)-1e-9,
      "PF 비악화(30)": (r["30d_pf_a"] or 0) >= (r["30d_pf_b"] or 0)-1e-9,
      "MDD 비악화(78)": r["78d_mdd_a"] >= r["78d_mdd_b"]-1e-9,
      "MDD 비악화(30)": r["30d_mdd_a"] >= r["30d_mdd_b"]-1e-9,
      "-Top10 비악화(78)": r["top10_d"] >= -1e-9,
      "-Top10 비악화(30)": r["top10_d30"] >= -1e-9,
      "앞39/뒤39 둘다 비악화": all(x >= -1e-9 for x in r["half"]),
      "WF 4/6 이상(패 없음)": r["wf6_win"] >= 4 and r["wf6_lose"] == 0,
      "상위3 개선 제외 uplift>=0": r["sum_d_ex3"] >= 0,
      "runner(TP2 8%) 손상 없음": dmg >= -1e-9,
      "악화거래 없음": r["dn"] == 0,
      "진입집합 무변경": r["only_a"] == 0 and r["only_b"] == 0,
    }
    print(f"\n### {lbl}  [{k}]")
    print(f"  78일 {r['78d_b']:.2f} → {r['78d_a']:.2f} ({r['78d_d']:+.2f}%p)    "
          f"30일 {r['30d_b']:.2f} → {r['30d_a']:.2f} ({r['30d_d']:+.2f}%p)   n={m['trades']}")
    print(f"  PF {r['78d_pf_b']:.4f}→{r['78d_pf_a']:.4f} (78) / {r['30d_pf_b']:.4f}→{r['30d_pf_a']:.4f} (30)")
    print(f"  MDD {r['78d_mdd_b']:.2f}→{r['78d_mdd_a']:.2f} / {r['30d_mdd_b']:.2f}→{r['30d_mdd_a']:.2f}   "
          f"수익/MDD {abs(r['78d_b']/r['78d_mdd_b']):.2f}→{abs(r['78d_a']/r['78d_mdd_a']):.2f}")
    print(f"  -Top1/3/10 (78) {r['top1_d']:+.2f}/{r['top3_d']:+.2f}/{r['top10_d']:+.2f}   "
          f"(30) {r['top1_d30']:+.2f}/{r['top3_d30']:+.2f}/{r['top10_d30']:+.2f}")
    print(f"  앞39/뒤39 {r['half'][0]:+.2f} / {r['half'][1]:+.2f}")
    print(f"  5분할  " + "  ".join(f"{x:+.2f}" for x in r["k5"]))
    print(f"  WF6    " + "  ".join(f"{x:+.2f}" for x in r["wf6"]) +
          f"   → {r['wf6_win']}승 {r['wf6_lose']}패 {6-r['wf6_win']-r['wf6_lose']}무")
    print(f"  변경 {r['chg']}건 ({r['up']}↑/{r['dn']}↓) 단순합 {r['sum_d']:+.2f}%p  "
          f"상위1/2/3 제외 {r['sum_d_ex1']:+.2f}/{r['sum_d_ex2']:+.2f}/{r['sum_d_ex3']:+.2f}  "
          f"runner {dmg:+.2f}")
    for kk, x in r["diffs"]:
        b_, a_ = B[kk], ka[kk]
        print(f"      {kk[0]} {str(kk[1])[11:16]} peak={b_['peak_net_pct']:5.2f}  "
              f"N1 {b_['net_pct']:+7.3f}[{str(b_['exit_reason'])[:22]:22s}] → "
              f"{a_['net_pct']:+7.3f}[{str(a_['exit_reason'])[:8]:8s}] Δ{x:+7.3f}")
    print("  " + " | ".join(f"{'PASS' if v else 'FAIL'} {kk}" for kk, v in cond.items()))
    print(f"  ==> {'채택' if all(cond.values()) else '기각'}"
          + (f"  미충족={[kk for kk,v in cond.items() if not v]}" if not all(cond.values()) else ""))

print("\n" + "=" * 126)
print("창별 비교 — A.N1 / B.N1-Safe / C.후보")
print("=" * 126)
WINS = {"78일": D, "30일": D[-30:], "8월이후": [x for x in D if x >= "20260801"],
        "앞39일": D[:39], "뒤39일": D[39:],
        "5월말~6월": [x for x in D if x < "20260701"],
        "7월": [x for x in D if "20260701" <= x < "20260801"],
        "8월": [x for x in D if "20260801" <= x < "20260901"],
        "9월": [x for x in D if x >= "20260901"]}
KEYS = [("A. N1", "N1"), ("B. N1-Safe", "N1_Safe"), ("C1. N1+PP5.0", "PPonly_a5.0_g1.5"),
        ("C2. N1+PP4.5", "PPonly_a4.5_g1.5"), ("C3. N1+PP5.0+E3.5", "E35+PP_a5.0")]
print(f"{'창':12s} " + " ".join(f"{n:>18s}" for n, _ in KEYS))
for wn, W in WINS.items():
    row = []
    bn = compound(TR["N1"], W)
    for n, k in KEYS:
        v = compound(TR[k], W)
        row.append(f"{v:9.2f}({v-bn:+7.2f})")
    print(f"{wn:12s} " + " ".join(f"{x:>18s}" for x in row))
print(f"\n{'':12s} " + " ".join(f"{n:>18s}" for n, _ in KEYS))
for lbl, fn in (("PF(78)", lambda k: summarize(TR[k], D)["pf"]),
                ("MDD(78)", lambda k: summarize(TR[k], D)["mdd_pct"]),
                ("-Top10(78)", lambda k: excl_topn(TR[k], D, 10)),
                ("승률", lambda k: summarize(TR[k], D)["win_rate_pct"]),
                ("일승률", lambda k: summarize(TR[k], D)["day_win_rate_pct"]),
                ("거래수", lambda k: summarize(TR[k], D)["trades"])):
    print(f"{lbl:12s} " + " ".join(f"{fn(k):>18.3f}" for _, k in KEYS))
pickle.dump({"trades": TR, "dates": D}, open(A.HERE / "RESULTS" / "FINAL_TRADES.pkl", "wb"))
