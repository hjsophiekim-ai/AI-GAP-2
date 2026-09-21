"""A~F 재실행 (엔진 패치 후 동일성 확인 포함). READ-ONLY."""
import sys, time, pickle
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A, hengine5 as H
c = A.ctx(78); D = c.dates
VAR = {
    "A  BASE (chop 1.5/0.8)": {},
    "B  NO-ETP":              dict(etp_scope="none"),
    "C  floor 0.4 (늦게)":     dict(etp_floor_pct=0.4),
    "D  floor 0.0 (더늦게)":   dict(etp_floor_pct=0.0),
    "E  floor 1.0 (빨리)":     dict(etp_floor_pct=1.0),
    "F  floor 1.25 (더빨리)":  dict(etp_floor_pct=1.25),
}
out = {}
for name, po in VAR.items():
    t0 = time.time()
    base_po = {"trail_stop_pct": 1.5, "aft_tp_pct": 4.0}; base_po.update(po)
    ts = A.run("N1", c, D, po=base_po, cfg=dict(A.NB), q3=True, h50=True)
    m = H.metrics(ts, D)
    ne = sum(1 for t in ts if (t.exit_reason if not isinstance(t, dict) else t["exit_reason"]) == "EARLY_TAKE_PROFIT")
    print(f"{name:24s} n={len(ts):4d} ETP={ne:3d} 78d복리={m['compound_pct']:9.4f} {time.time()-t0:.0f}s", flush=True)
    out[name] = [dict(vars(t)) if not isinstance(t, dict) else dict(t) for t in ts]
pickle.dump(out, open("_etp_runs.pkl", "wb"))
print("saved _etp_runs.pkl", flush=True)
