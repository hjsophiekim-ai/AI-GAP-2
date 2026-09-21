"""floor 상한 탐색 + 나머지 변형 통합. READ-ONLY."""
import sys, time, pickle
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A, hengine5 as H
c = A.ctx(78); D = c.dates
out = {}
for name, po in (("L  floor 1.40", dict(etp_floor_pct=1.40)),
                 ("M  floor 1.50 (=trigger)", dict(etp_floor_pct=1.50)),
                 ("N  floor 1.10", dict(etp_floor_pct=1.10))):
    t0 = time.time()
    bp = {"trail_stop_pct": 1.5, "aft_tp_pct": 4.0}; bp.update(po)
    ts = A.run("N1", c, D, po=bp, cfg=dict(A.NB), q3=True, h50=True)
    m = H.metrics(ts, D)
    ne = sum(1 for t in ts if (t.exit_reason if not isinstance(t, dict) else t["exit_reason"]) == "EARLY_TAKE_PROFIT")
    print(f"{name:26s} n={len(ts):4d} ETP={ne:3d} 78d복리={m['compound_pct']:9.4f} {time.time()-t0:.0f}s", flush=True)
    out[name] = [dict(vars(t)) if not isinstance(t, dict) else dict(t) for t in ts]
pickle.dump(out, open("_etp_runs3.pkl", "wb"))
print("saved", flush=True)
