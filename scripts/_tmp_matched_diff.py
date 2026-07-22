import json
from pathlib import Path

data = json.loads(Path(r"C:\Users\FURSYS\Desktop\AI-GAP 2\data\state\macd_opening_abc_20d_compare.json").read_text(encoding="utf-8"))


def key(t):
    return (t["day"], t["signal_time"], t["direction"], t["symbol"])


for day in ["2026-07-13", "2026-07-16", "2026-07-21"]:
    at = {key(t): t for t in data["A"]["trades"] if t["day"] == day}
    ct = {key(t): t for t in data["C"]["trades"] if t["day"] == day}
    shared = set(at) & set(ct)
    print(f"=== {day} ===")
    print("A trades:", len(at), "C trades:", len(ct))
    for k in sorted(shared):
        a, c = at[k], ct[k]
        dnet = a["net_pnl"] - c["net_pnl"]
        if abs(dnet) > 0.01 or a["exit_reason"] != c["exit_reason"]:
            print(
                f"  MISMATCH {k[1]} net_delta={dnet:.0f} "
                f"A={a['net_pnl']:.0f} C={c['net_pnl']:.0f} "
                f"exit A={a['exit_reason']} C={c['exit_reason']}"
            )
    for k in sorted(set(at) - set(ct)):
        t = at[k]
        print(f"  ONLY A {k[1]} net={t['net_pnl']:.0f} exit={t['exit_reason']} entry={t['entry_time']}")
    for k in sorted(set(ct) - set(at)):
        t = ct[k]
        print(f"  ONLY C {k[1]} net={t['net_pnl']:.0f}")
    print()
