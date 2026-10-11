"""A11 — D(구조붕괴 청산) / E(약하면 그 자리 이익확정) 전수 격자. 셀 전부 보고."""
import pickle, time
import axlib as A
from common import summarize

c = A.ctx(78); D78 = c.dates; W30 = c.dates[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D78); A.save()
mb = summarize(base, D78); mb30 = summarize([t for t in base if t['date'] in S30], W30)
print(f"N1 78일 {mb['compound_pct']:.2f} / 30일 {mb30['compound_pct']:.2f}\n", flush=True)
OUT = {"N1": base}
key = lambda t: (t["date"], t["entry_time"])
B = {key(t): t for t in base}

hdr = (f"{'규칙':26s} {'n':>4s} {'78d':>8s} {'Δ78':>7s} {'30d':>7s} {'Δ30':>7s} "
       f"{'PF':>6s} {'MDD':>7s} {'-T10':>7s} {'변경':>4s}")
print(hdr, flush=True)

def go(k, spec):
    t0 = time.time()
    ts = A.run("N1", c, D78, cfg=cfg, po=po, q3=q3, h50=h50, ax=spec)
    OUT[k] = ts; A.save()
    m = summarize(ts, D78); m30 = summarize([t for t in ts if t['date'] in S30], W30)
    chg = sum(1 for t in ts if key(t) in B and abs(t["net_pct"] - B[key(t)]["net_pct"]) > 1e-9)
    print(f"{k:26s} {m['trades']:4d} {m['compound_pct']:8.2f} {m['compound_pct']-mb['compound_pct']:+7.2f} "
          f"{m30['compound_pct']:7.2f} {m30['compound_pct']-mb30['compound_pct']:+7.2f} "
          f"{m['pf']:6.3f} {m['mdd_pct']:7.2f} {m['top10_excl_pct']:7.2f} {chg:4d}  {time.time()-t0:.0f}s",
          flush=True)

print("── D: 이익구간에서 구조붕괴 시 잔량청산 ──", flush=True)
for v in ("C1", "C2", "C3", "C4"):
    for arm in (3.0, 3.5, 4.0):
        for mode in (None, "WEAK", "STRONG"):
            go(f"D_{v}_a{arm}_{mode or 'ALL'}",
               {"decide": 3.0, "strong": {}, "weak": {},
                "brk": {"arm": arm, "variant": v, "mode": mode}})

print("\n── E: 판정레벨 도달 완성봉에서 구조가 X 면 그 자리 전량 이익확정 ──", flush=True)
for lv in (3.0, 3.5, 4.0, 5.0):
    go(f"E_weaklock{lv}", {"decide": lv, "strong": {}, "weak": {"close": True}})
    go(f"E_stronglock{lv}", {"decide": lv, "strong": {"close": True}, "weak": {}})

pickle.dump({"trades": OUT, "dates": D78}, open(A.HERE / "ax78_grid2.pkl", "wb"))
print("\n저장: ax78_grid2.pkl")
