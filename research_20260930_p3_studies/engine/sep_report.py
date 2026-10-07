"""9/1~9/23 P3 ON 거래표. 수량 = floor(1,000만원 × w1a / 진입가), 원화손익 = Σ 레그수량 × 진입가 × 레그net/100."""
import math
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
BUDGET = 10_000_000
NAME = {"0193T0": "KODEX레버리지", "0197X0": "SOL인버스2X"}
START, END = "20260901", "20260923"


def load(n):
    return pickle.load(open(HERE / f"out_{n}.pkl", "rb"))["trades"]


def window(ts):
    return sorted((t for t in ts if START <= t["date"] <= END), key=lambda t: t["entry_time"])


def krw(t):
    qty = math.floor(BUDGET * t["w1a"] / t["entry_price"])
    return qty, qty * t["entry_price"] * t["net_pct"] / 100.0   # net_pct 는 부분청산 실현분 포함


def mode(t):
    if not t.get("b3_on"):
        return "BASE"
    if t.get("b3_rescued"):
        return "P3-RUNNER"
    if t.get("b3_y3"):
        return "Y3-RUNNER"
    return "B3"


def comp(ts):
    eq = 1.0
    for t in sorted(ts, key=lambda x: x["exit_time"]):
        eq *= 1 + t["net_pct"] * t["w1a"] / 100
    return (eq - 1) * 100


P3 = window(load("P3_T10_d81"))
BASE = window(load("BASE_d81"))
rows = []
for i, t in enumerate(P3, 1):
    qty, won = krw(t)
    et = pd.Timestamp(t["entry_time"])
    legs = " / ".join(f"{pd.Timestamp(l[0]).strftime('%H:%M')} {l[1]:,.0f}원×{l[2]*100:.0f}% {l[3]}" for l in t["legs"])
    rows.append(dict(no=i, date=t["date"], entry=et.strftime("%H:%M"), dir=t["direction"],
                     sym=NAME.get(t["entry_symbol"], t["entry_symbol"]), regime=t.get("entry_regime", ""),
                     mode=mode(t), entry_px=t["entry_price"], qty=qty, w1a=t["w1a"],
                     exit=pd.Timestamp(t["exit_time"]).strftime("%H:%M"), exit_px=t["exit_price"],
                     reason=t["exit_reason"], legs=legs, net=t["net_pct"], won=won))
df = pd.DataFrame(rows)
df.to_csv(HERE / "sep_p3_trades.csv", index=False, encoding="utf-8-sig")
for r in rows:
    print(f"{r['no']:2d} {r['date']} {r['entry']} {r['dir'][:4]} {r['sym']} {r['regime']:6s} {r['mode']:9s} "
          f"진입 {r['entry_px']:,.0f}×{r['qty']}주(w{r['w1a']}) → {r['exit']} {r['exit_px']:,.0f} {r['reason']} "
          f"| {r['net']:+.3f}% {r['won']:+,.0f}원 || {r['legs']}")
w = sum(r["won"] > 0 for r in rows)
print(f"\nP3 ON : {len(rows)}거래 {w}승 {len(rows)-w}패 승률 {100*w/len(rows):.1f}% | 원화합 {df.won.sum():+,.0f}원 | "
      f"복리 {comp(P3):+.3f}% | 단순합(net×w1a) {sum(t['net_pct']*t['w1a'] for t in P3):+.3f}%p")
bw = [krw(t)[1] for t in BASE]
print(f"BASE  : {len(BASE)}거래 {sum(x>0 for x in bw)}승 | 원화합 {sum(bw):+,.0f}원 | 복리 {comp(BASE):+.3f}%")
for d in sorted({r['date'] for r in rows} | {t['date'] for t in BASE}):
    a = df[df.date == d].won.sum(); b = sum(krw(t)[1] for t in BASE if t["date"] == d)
    print(f"  {d}: P3 {a:+12,.0f}  BASE {b:+12,.0f}  Δ {a-b:+11,.0f}")
# 80일 런과 9/1~9/22 일치 확인
old = [t for t in load("P3_T10") if START <= t["date"] <= "20260922"]
new = [t for t in P3 if t["date"] <= "20260922"]
sg = lambda ts: sorted((t["entry_time"], t["exit_time"], round(t["net_pct"], 6)) for t in ts)
print("\n80일 런 대비 9/1~9/22 거래 일치:", sg(old) == sg(new), len(old), len(new))
