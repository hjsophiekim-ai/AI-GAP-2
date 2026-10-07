"""09/28 OOS — 오늘 09:06 블루(09:03 플래그 → 09:09 인버스) 가 R0/M2/Q2/Q3 에서 어떻게 관리됐나."""
import bisect
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
DAY = "20260928"


def load(n):
    p = HERE / f"out_{n}_d82.pkl"
    return pickle.load(open(p, "rb"))["trades"] if p.exists() else None


B = load("BASE")
RUNS = {k: load(k) for k in ("R0", "M2", "Q2", "Q3", "Q4", "Q5")}
RUNS = {k: v for k, v in RUNS.items() if v is not None}
hm = lambda x: pd.Timestamp(x).strftime("%H:%M") if x else "-"
key = lambda t: (t["date"], str(pd.Timestamp(t["entry_time"])), t["direction"])

# 1) 80일 구간 일관성
for k in ["BASE"] + list(RUNS):
    a = load(k); b = pickle.load(open(HERE / f"out_{k}.pkl", "rb"))["trades"]
    sa = sorted((key(t), round(t["net_pct"], 8)) for t in a if t["date"] <= "20260922")
    sb = sorted((key(t), round(t["net_pct"], 8)) for t in b)
    print(f"[일관성] {k}: 0527~0922 거래 82일런 == 80일런 → {sa == sb} ({len(sa)} vs {len(sb)})")

# 2) 09/28 진입 regime (섀도우 = BASE_d82)
rows = sorted(((pd.Timestamp(t["exit_time"]), bool(t["h50_held"]), bool(t["tp1_hit"])) for t in B), key=lambda r: r[0])
ex = [r[0] for r in rows]


def regime(ts):
    k = bisect.bisect_left(ex, pd.Timestamp(ts))
    if k < 10:
        return "WARMUP", None
    r = rows[k - 10:k]
    h, tp = sum(z[1] for z in r) / 10, sum(z[2] for z in r) / 10
    return ("CHOP" if h >= 0.4 and tp <= 0.2 else "TREND"), (h, tp)


print("\n[09/28 BASE 거래]")
for t in sorted((t for t in B if t["date"] == DAY), key=lambda t: t["entry_time"]):
    rg, hr = regime(t["entry_time"])
    print(f"  {hm(t['entry_time'])} {t['direction']} {t['entry_symbol']} @{t['entry_price']:,.0f} → {hm(t['exit_time'])} "
          f"@{t['exit_price']:,.0f} {t['exit_reason']} net {t['net_pct']:+.3f} MFE {t['peak_net_pct']:.2f} "
          f"(peak {hm(t.get('peak_at'))}) MAE {t['mae_net_pct']:+.2f} h50={t['h50_held']} | 진입 regime {rg} {hr}")

print("\n[09/28 전략별 거래]")
for k, ts in RUNS.items():
    day = sorted((t for t in ts if t["date"] == DAY), key=lambda t: t["entry_time"])
    print(f" {k}: {len(day)}거래, 당일 가중합 {sum(t['net_pct']*t['w1a'] for t in day):+.3f}")
    for t in day:
        mode = ("P3-RESCUE" if t.get("b3_rescued") else "Y3" if t.get("b3_y3") else
                "B3" if t.get("b3_on") else "BASE")
        legs = " / ".join(f"{hm(l[0])} {l[1]:,.0f}×{l[2]*100:.0f}% {l[3]}({l[4]:+.2f})" for l in t["legs"])
        print(f"   {hm(t['entry_time'])} {t['direction'][:4]} regime={t.get('entry_regime')} mode={mode} "
              f"+1%최초 {hm(t.get('b3_first_trig_at'))} ({t.get('b3_trig_el')}분) → {hm(t['exit_time'])} "
              f"{t['exit_reason']} net {t['net_pct']:+.3f} MFE {t['peak_net_pct']:.2f} | {legs}")

# 3) 첫 거래 요약 판정
print("\n[판정] 09:09 인버스 첫 거래")
bt = next((t for t in B if t["date"] == DAY and hm(t["entry_time"]) in ("09:09", "09:06", "09:12")), None)
for k, ts in RUNS.items():
    t = next((x for x in ts if x["date"] == DAY and bt is not None and key(x) == key(bt)), None)
    if t is None:
        print(f"  {k}: (같은 진입 없음)"); continue
    print(f"  {k}: net {t['net_pct']:+.3f} vs BASE {bt['net_pct']:+.3f} | runner 보존 "
          f"{100*t['net_pct']/bt['net_pct']:.0f}%" if bt and bt["net_pct"] > 0 else f"  {k}: net {t['net_pct']:+.3f}")
