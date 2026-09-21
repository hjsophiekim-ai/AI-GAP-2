"""나머지 ETP 변형 (delay / trigger / scope). READ-ONLY."""
import sys, time, pickle
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A, hengine5 as H
c = A.ctx(78); D = c.dates
out = pickle.load(open("_etp_runs_partial.pkl", "rb")) if False else {}
VAR = [
    ("G  delay +1봉",  {}, 1),
    ("H  delay +2봉",  {}, 2),
    ("I  trigger 1.0", dict(etp_trigger_pct=1.0), 0),
    ("J  trigger 2.0", dict(etp_trigger_pct=2.0), 0),
    ("K  scope all",   dict(etp_scope="all"), 0),
]
for name, po, dly in VAR:
    t0 = time.time()
    H.ETP_DELAY_BARS = dly
    base_po = {"trail_stop_pct": 1.5, "aft_tp_pct": 4.0}; base_po.update(po)
    ts = A.run("N1", c, D, po=base_po, cfg=dict(A.NB), q3=True, h50=True)
    H.ETP_DELAY_BARS = 0
    m = H.metrics(ts, D)
    ne = sum(1 for t in ts if (t.exit_reason if not isinstance(t, dict) else t["exit_reason"]) == "EARLY_TAKE_PROFIT")
    print(f"{name:24s} n={len(ts):4d} ETP={ne:3d} 78d복리={m['compound_pct']:9.4f} {time.time()-t0:.0f}s", flush=True)
    out[name] = [dict(vars(t)) if not isinstance(t, dict) else dict(t) for t in ts]
pickle.dump(out, open("_etp_runs2.pkl", "wb"))
print("saved _etp_runs2.pkl", flush=True)
