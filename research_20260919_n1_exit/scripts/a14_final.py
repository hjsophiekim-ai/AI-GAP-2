"""A14 — 최종 비교표: A.H50 / B.N1 / C.N1-Safe / D.Adaptive(상위3) 를 창별로."""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize, compound, excl_topn, pnl
import axval as V

c = A.ctx(78); D = c.dates
TR = {}
for f in ("ax78.pkl", "ax78_d.pkl", "ax78_grid2.pkl", "ax78_e35.pkl"):
    d = pickle.load(open(A.HERE / f, "rb"))
    for k, v in d["trades"].items():
        TR.setdefault(k, v)
# 기준 4전략
for k in ("H50", "N1_Safe", "X2lite_W1"):
    TR[k] = A.run(k, c, D)
A.save()

WINS = {
    "78일(0527~0918)": D,
    "30일(0805~0918)": D[-30:],
    "8월이후(0803~)":  [x for x in D if x >= "20260801"],
    "앞39일":          D[:39],
    "뒤39일":          D[39:],
    "6~7월":           [x for x in D if "20260601" <= x <= "20260731"],
    "8~9월":           [x for x in D if x >= "20260801"],
}
SHOW = ["H50", "N1", "N1_Safe", "E_weaklock3.5", "D_C3_a3.5_WEAK", "D_C3_a3.0_WEAK"]
NAME = {"H50": "A. H50", "N1": "B. N1(기준)", "N1_Safe": "C. N1-Safe",
        "E_weaklock3.5": "D1. AX-E3.5", "D_C3_a3.5_WEAK": "D2. AX-C3@3.5",
        "D_C3_a3.0_WEAK": "D3. AX-C3@3.0"}
for wn, W in WINS.items():
    print(f"\n{'='*96}\n{wn}  ({len(W)}영업일)")
    print(f"{'전략':16s} {'n':>4s} {'복리%':>9s} {'ΔN1':>8s} {'PF':>6s} {'MDD':>7s} "
          f"{'승률':>5s} {'일승률':>6s} {'-T1':>8s} {'-T3':>8s} {'-T10':>8s}")
    bn = compound(TR["N1"], W)
    for k in SHOW:
        ts = TR[k]; m = summarize([t for t in ts if t["date"] in set(W)], W)
        if not m.get("trades"):
            continue
        print(f"{NAME[k]:16s} {m['trades']:4d} {m['compound_pct']:9.2f} "
              f"{m['compound_pct']-bn:+8.2f} {m['pf']:6.3f} {m['mdd_pct']:7.2f} "
              f"{m['win_rate_pct']:5.1f} {m['day_win_rate_pct']:6.1f} "
              f"{excl_topn(ts,W,1):8.2f} {excl_topn(ts,W,3):8.2f} {excl_topn(ts,W,10):8.2f}")

print("\n\n=== AX-E3.5 가 살린 거래 / 망친 거래 (N1 대비, w1a 반영) ===")
r = V.report("E3.5", TR["E_weaklock3.5"], TR["N1"], D)
kb = {(t["date"], t["entry_time"]): t for t in TR["N1"]}
ka = {(t["date"], t["entry_time"]): t for t in TR["E_weaklock3.5"]}
print(f"{'일자':10s} {'진입':6s} {'N1 net':>8s} {'N1 사유':26s} {'AX net':>8s} {'AX 사유':22s} "
      f"{'peak':>6s} {'Δ':>7s}")
for k, x in r["diffs"]:
    b, a = kb[k], ka[k]
    print(f"{k[0]:10s} {str(k[1])[11:16]:6s} {b['net_pct']:8.3f} {str(b['exit_reason'])[:26]:26s} "
          f"{a['net_pct']:8.3f} {str(a['exit_reason'])[:22]:22s} {b['peak_net_pct']:6.2f} {x:+7.3f}")
