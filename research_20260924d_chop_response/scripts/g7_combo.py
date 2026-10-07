"""G7 - 항목8 최종조합. F = best SHORT-EXIT(B3) + best SLOT-CONTROL(D2)."""
import pickle, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")
import rlib as R, axlib as A, hengine5 as H
from common import summarize
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "g3.pkl", "rb"))
D = Z["dates"]; SH = np.load(HERE / "shadow_on.npy")
ctx = R.get_ctx()


def is_ar1(day, rec_at):
    k = rec_at.astimezone(H.KST)
    return (k.strftime("%Y%m%d"), k.strftime("%H:%M")) in R.Z.RELAXED_KEYS


NEW = [("F_B3_D2", {"on": SH, "is_ar1": is_ar1, "log": [],
                    "exit": {"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20},
                    "skip": {"rule": "D2", "params": {}}})]
for tag, g in NEW:
    if tag in Z["runs"]:
        print(tag, "이미 있음"); continue
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    t0 = time.time()
    ts = A.run("N1", ctx, D, ax=R.AX, gx=g)
    m = summarize(ts, D)
    nch = sum(1 for t in ts if t.get("gx_chop"))
    print("%-12s 거래 %3d (CHOP %2d)  복리 %9.4f  PF %.3f  MDD %7.3f  승률 %5.1f%%  (%.0fs)"
          % (tag, m["trades"], nch, m["compound_pct"], m["pf"], m["mdd_pct"],
             m["win_rate_pct"], time.time() - t0), flush=True)
    Z["runs"][tag] = ts
    Z["logs"][tag] = g["log"]
    Z["bat"] = list(Z["bat"]) + [tag]
    pickle.dump(Z, open(HERE / "g3.pkl", "wb"))
print("saved")
