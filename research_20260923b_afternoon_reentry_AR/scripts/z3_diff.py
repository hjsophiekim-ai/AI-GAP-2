import sys, pickle; sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
tr = pickle.load(open(HERE / "z2_trades.pkl", "rb"))
def key(t): return (t["date"], t["entry_time"], t["direction"])
base = {key(t): t for t in tr["BASE"]}
for name in ("R0_all", "R2_extreme"):
    v = {key(t): t for t in tr[name]}
    add = [v[k] for k in v if k not in base]
    rem = [base[k] for k in base if k not in v]
    print("=== %s : 추가 %d건 / 사라짐 %d건 ===" % (name, len(add), len(rem)))
    if add:
        D = pd.DataFrame([dict(date=t["date"], entry=pd.Timestamp(t["entry_time"]).strftime("%H:%M"),
                               dir=t["direction"], slot=t["slot_number"],
                               exit=pd.Timestamp(t["exit_time"]).strftime("%m-%d %H:%M"),
                               reason=t["exit_reason"], net=t["net_pct"],
                               peak=t["peak_net_pct"], mae=t["mae_net_pct"]) for t in add])
        pd.set_option("display.width", 220)
        print(D.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
        print("  합계 net %.3f%%p  승 %d / 패 %d" % (D.net.sum(), (D.net > 0).sum(), (D.net <= 0).sum()))
    if rem:
        print("  사라진 거래:", [(t["date"], pd.Timestamp(t["entry_time"]).strftime("%H:%M")) for t in rem])
    print()
