"""V2 — arm x give 격자 (plateau 확인용, 최고값 탐색 아님). 전 셀 보고."""
import pickle, sys, time
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_V2.pkl"
A._Q3_PATH = A.HERE / "_q3base_V2.pkl"
from common import summarize
import axval as V

c = A.ctx(78); D = c.dates
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D); A.save()
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
RUN8 = sorted(k for k, t in B.items() if t["peak_net_pct"] >= 8.0)
OUT = {"N1": base}

print(f"{'arm':>4s} {'give':>5s} | {'n':>4s} {'Δ78':>8s} {'Δ30':>7s} {'PF78':>6s} {'PF30':>6s} "
      f"{'MDD':>6s} {'ΔT10':>7s} {'ΔT10_30':>8s} {'변경':>4s} {'↑':>3s} {'↓':>3s} {'run8':>6s}", flush=True)
for arm in (4.0, 4.5, 5.0, 5.5, 6.0, 6.5):
    for give in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
        k = f"a{arm}_g{give}"
        t0 = time.time()
        ts = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50,
                   ax={"decide": 99.0, "strong": {}, "weak": {},
                       "pp": {"arm": arm, "give": give, "cond": "gap_neg"}})
        OUT[k] = ts; A.save()
        r = V.report(k, ts, base, D); m = summarize(ts, D)
        ka = {key(t): t for t in ts}
        dmg = sum(ka[x]["net_pct"] - B[x]["net_pct"] for x in RUN8 if x in ka)
        print(f"{arm:4.1f} {give:5.2f} | {m['trades']:4d} {r['78d_d']:+8.2f} {r['30d_d']:+7.2f} "
              f"{m['pf']:6.3f} {r['30d_pf_a']:6.3f} {m['mdd_pct']:6.2f} {r['top10_d']:+7.2f} "
              f"{r['top10_d30']:+8.2f} {r['chg']:4d} {r['up']:3d} {r['dn']:3d} {dmg:+6.2f}"
              f"   {time.time()-t0:.0f}s", flush=True)
pickle.dump({"trades": OUT, "dates": D}, open(A.HERE / "v2.pkl", "wb"))
print("\n저장: v2.pkl")
