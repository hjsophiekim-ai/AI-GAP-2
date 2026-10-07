"""A7 — D계열 전수 격자 (체리피킹 방지: 셀 전부 보고).
variant C1..C4 x arm{3.0,3.5,4.0} x mode{ALL,WEAK,STRONG}."""
import time, pickle
import axlib as A
from common import summarize

c = A.ctx(78); D78 = c.dates; W30 = c.dates[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D78); A.save()
mb = summarize(base, D78); mb30 = summarize([t for t in base if t['date'] in S30], W30)
print(f"N1 78일 {mb['compound_pct']:.2f} / 30일 {mb30['compound_pct']:.2f}\n", flush=True)

OUT = {"N1": base}
print(f"{'variant':10s} {'arm':>4s} {'mode':>7s} | {'n':>4s} {'78d':>8s} {'Δ78':>7s} {'30d':>7s} {'Δ30':>7s} {'PF':>6s} {'-T10':>7s} {'변경':>4s}")
for v in ("C1", "C2", "C3", "C4"):
    for arm in (3.0, 3.5, 4.0):
        for mode in (None, "WEAK", "STRONG"):
            k = f"D_{v}_a{arm}_{mode or 'ALL'}"
            spec = {"decide": 3.0, "strong": {}, "weak": {},
                    "brk": {"arm": arm, "variant": v, "mode": mode}}
            ts = A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50, ax=spec)
            OUT[k] = ts; A.save()
            m = summarize(ts, D78); m30 = summarize([t for t in ts if t['date'] in S30], W30)
            key = lambda t: (t["date"], t["entry_time"])
            b = {key(t): t for t in base}
            chg = sum(1 for t in ts if key(t) in b and abs(t["net_pct"] - b[key(t)]["net_pct"]) > 1e-9)
            print(f"{v:10s} {arm:4.1f} {str(mode or 'ALL'):>7s} | {m['trades']:4d} "
                  f"{m['compound_pct']:8.2f} {m['compound_pct']-mb['compound_pct']:+7.2f} "
                  f"{m30['compound_pct']:7.2f} {m30['compound_pct']-mb30['compound_pct']:+7.2f} "
                  f"{m['pf']:6.3f} {m['top10_excl_pct']:7.2f} {chg:4d}", flush=True)
pickle.dump({"trades": OUT, "dates": D78}, open(A.HERE / "ax78_grid.pkl", "wb"))
