"""§11 용 타 전략 거래목록 생성 (엔진 런). READ-ONLY."""
import sys, time, pickle, json
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
import hengine5 as H

c = A.ctx(78); D = c.dates
out = {}
ANCHOR = {"X2lite_W1": None, "H50": 230.6116, "N1": 401.0853, "N1_Safe": None}
for name in ("X2lite_W1", "H50", "N1", "N1_Safe"):
    t0 = time.time()
    ts = A.run(name, c, D)
    m = H.metrics(ts, D)
    a = ANCHOR.get(name)
    print(f"{name:12s} n={len(ts):4d} 78d={m['compound_pct']:9.4f} "
          f"{'앵커 %.4f' % a if a else ''} {time.time()-t0:.0f}s", flush=True)
    out[name] = [dict(vars(t)) if not isinstance(t, dict) else dict(t) for t in ts]
with open("_strats.pkl", "wb") as fh:
    pickle.dump(out, fh)
print("saved _strats.pkl", flush=True)
