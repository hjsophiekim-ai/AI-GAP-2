"""규칙 B 로 달라진 거래 전수 — 개선/악화를 손절 거래와 2차 효과로 나눠 보인다."""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUTD = Path(os.environ.get("ARM3_OUT",
            r"C:/Users/Kim Hyunjoo/AppData/Local/Temp/claude/arm3_sp/out"))
FEE = 0.000036
FROM = "20260803"
SYM = {"0193T0": "레버", "0197X0": "인버스"}


def episodes(js, day):
    eps, cur = [], None
    for o in js["orders"]:
        if o["t"][:10].replace("-", "") != day:
            continue
        if o["side"] == "BUY":
            if cur is not None and cur["left"] > 0:
                cur["buys"].append(o); cur["left"] += o["qty"]; continue
            cur = dict(sym=o["sym"], buys=[o], sells=[], left=o["qty"]); eps.append(cur)
        elif cur is not None:
            cur["sells"].append(o); cur["left"] -= o["qty"]
    out = []
    for ep in eps:
        b = ep["buys"][0]; e = float(b["px"])
        last = ep["sells"][-1] if ep["sells"] else None
        out.append(dict(
            t=b["t"][11:19], sym=ep["sym"], e=e, qty=b["qty"],
            pnl=sum(s["qty"] * (s["px"] - e) - FEE * s["qty"] * (s["px"] + e) for s in ep["sells"]),
            xt=last["t"][11:19] if last else "", xp=float(last["px"]) if last else 0.0,
            net=((last["px"] / e - 1) * 100 - FEE * 100 * (1 + last["px"] / e)) if last else 0.0,
            why=";".join(sorted({a.split(":")[0] for s in ep["sells"]
                                 for a in (s.get("acts") or [])}))))
    return out


def mins(hms):
    return int(hms[:2]) * 60 + int(hms[3:5]) if hms else 0


days = sorted(f.stem.split("_")[-1] for f in OUTD.glob("L85_A_2026*.json")
              if f.stem.split("_")[-1] >= FROM)
imp, wor, sec = [], [], []
for d in days:
    A = episodes(json.loads((OUTD / f"L85_A_{d}.json").read_text(encoding="utf-8")), d)
    f = OUTD / f"CHK_SLH_ON_{d}.json"
    B = episodes(json.loads((f if f.exists() else OUTD / f"L85_A_{d}.json").read_text(encoding="utf-8")), d)
    kb = {(x["t"], x["sym"]): x for x in B}
    ka = {(x["t"], x["sym"]): x for x in A}
    for a in A:
        b = kb.get((a["t"], a["sym"]))
        dl = (b["pnl"] if b else 0.0) - a["pnl"]
        if abs(dl) < 0.5:
            continue
        if "TIME_WINDOW_STOP_LOSS" in a["why"]:
            (imp if dl > 0 else wor).append((d, a, b, dl))
        else:
            sec.append((d, a, b, dl))
    for b in B:
        if (b["t"], b["sym"]) not in ka:
            sec.append((d, None, b, b["pnl"]))


def table(rows, title):
    print(f"\n{title} — {len(rows)}건 / 합계 {sum(r[3] for r in rows):+,.0f}\n")
    h = (f"{'날짜':8s} {'진입':8s} {'종목':4s} {'수량':>5s} {'진입가':>7s} | {'A 청산':8s} {'A가':>7s} "
         f"{'A%':>6s} {'A손익':>10s} | {'B 청산':8s} {'B가':>7s} {'B%':>6s} {'B손익':>10s} | {'Δ':>10s} {'연장':>4s}")
    print(h); print("-" * len(h))
    for d, a, b, dl in sorted(rows, key=lambda r: -abs(r[3])):
        print(f"{d} {a['t']:8s} {SYM[a['sym']]:4s} {a['qty']:>5,} {a['e']:>7,.0f} | "
              f"{a['xt']:8s} {a['xp']:>7,.0f} {a['net']:>+6.2f} {a['pnl']:>+10,.0f} | "
              f"{b['xt']:8s} {b['xp']:>7,.0f} {b['net']:>+6.2f} {b['pnl']:>+10,.0f} | "
              f"{dl:>+10,.0f} {mins(b['xt']) - mins(a['xt']):>3d}분")
        print(f"{'':31s}| A {a['why'][:30]:30s} | B {b['why'][:30]:30s} | 수량 {a['qty']:,}->{b['qty']:,}")


print("규칙 B (초기 30분 손절 2봉 확인) 로 달라진 거래 전수")
table(imp, "[개선된 손절 거래]")
table(wor, "[악화된 손절 거래]")
print(f"\n[2차 효과] — {len(sec)}건 / 합계 {sum(r[3] for r in sec):+,.0f}\n")
for d, a, b, dl in sec:
    print(f"  {d} {SYM[(a or b)['sym']]:4s} A {(a['why'][:24] if a else '진입없음'):24s} "
          f"{(a['pnl'] if a else 0):+10,.0f} ({a['qty'] if a else 0:,}주) -> "
          f"B {(b['why'][:24] if b else '진입없음'):24s} {(b['pnl'] if b else 0):+10,.0f} "
          f"({b['qty'] if b else 0:,}주)  Δ {dl:+10,.0f}")
print(f"\n손절 순효과 {sum(r[3] for r in imp + wor):+,.0f} + 2차 {sum(r[3] for r in sec):+,.0f} "
      f"= 총계 {sum(r[3] for r in imp + wor + sec):+,.0f}")
