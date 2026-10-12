import json, os, sys
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
RP = os.path.dirname(os.path.abspath(__file__)); WT = os.path.join(os.path.dirname(RP), "wt_int")
sys.path.insert(0, os.path.join(WT, "research_20261008b_box_breakout_gate", "scripts"))
from feat import trades
P85 = [l.strip() for l in open(os.path.join(WT, "research_20261012_prod_verification", "p3_days.txt"))]
ALLD = sorted({l.split()[1] for l in open(os.path.join(RP, "jobs.txt"))})
D23 = [d for d in ALLD if "20260903" <= d <= "20261008"]
L = lambda t, d: json.load(open(os.path.join(RP, "out", f"REAL_{t}_{d}.json"), encoding="utf-8"))
def lab(d):
    s, c = L("SLOW", d), L("CHOP", d)
    ts, tc = trades(s), trades(c)
    ks = {t["entry"].strftime("%H:%M") for t in ts}; kc = {t["entry"].strftime("%H:%M") for t in tc}
    labs = set()
    if any(x["block_reason"] == "CHOP_MORNING_NO_TRADE" for x in c["sig"]): labs.add("MC")
    if any(e["k"] == "B3STOP" and e["res"] == "DEFER" for e in c["prod"]): labs.add("STRUCT")
    if len(ts) >= 3 and len(tc) < len(ts) and any(x["block_reason"] == "TW2_3SLOT_REJECT_DAILY_SLOT_CAP" for x in c["sig"]): labs.add("2SLOT")
    if ks != kc and not labs: labs.add("진입경로변화")
    dv = sum(t["krw"] for t in tc) - sum(t["krw"] for t in ts)
    return "+".join(sorted(labs)) or "-", dv
for nm, ds in (("85일", P85), ("9~10월 23일", D23)):
    agg = defaultdict(lambda: [0, 0.0, 0, 0])
    for d in ds:
        k, v = lab(d)
        if abs(v) < 0.5 and k == "-": continue
        a = agg[k]; a[0] += 1; a[1] += v; a[2] += v > 0; a[3] += v < 0
    print(f"\n[{nm}] 방어 3종 효과 = CHOP - (P3+SLOW), 발동 조합별")
    for k, (n, v, w, l) in sorted(agg.items(), key=lambda x: x[1][1]):
        print(f"  {k:22s} {n:3d}일  Δ {v:+12,.0f}  (개선 {w} / 악화 {l})")
    print(f"  합계 {sum(a[1] for a in agg.values()):+,.0f}")
