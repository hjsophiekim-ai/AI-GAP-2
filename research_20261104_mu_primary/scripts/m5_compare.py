"""A(현행 production) vs MU 원신호 변형 재생 결과 비교 (research only).

    python m5_compare.py A=L85_A M1=MUPRI_M1_S2 M2=MUPRI_M2_S2
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
FEE = 0.000036
DAYS = ["20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]
SYM = {"0193T0": "RED", "0197X0": "BLUE"}


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
        pnl = sum(s["qty"] * (s["px"] - e) - FEE * s["qty"] * (s["px"] + e) for s in ep["sells"])
        qty = sum(x["qty"] for x in ep["buys"])
        out.append(dict(day=day, t=b["t"][11:19], sym=ep["sym"], dir=SYM.get(ep["sym"], "?"),
                        px=e, qty=qty, pnl=pnl, net=100.0 * pnl / (e * qty) if qty else 0.0,
                        exit=ep["sells"][-1]["t"][11:19] if ep["sells"] else "",
                        why=";".join(sorted({a.split(":")[0] for s in ep["sells"]
                                             for a in (s.get("acts") or [])}))))
    return out


def load(tag):
    rows = {}
    for d in DAYS:
        f = OUT / f"{tag}_{d}.json"
        rows[d] = episodes(json.loads(f.read_text(encoding="utf-8")), d) if f.exists() else []
    return rows


def metrics(rows):
    tr = [x for d in DAYS for x in rows[d]]
    daily = [sum(x["pnl"] for x in rows[d]) for d in DAYS]
    cum = peak = mdd = 0.0
    for v in daily:
        cum += v; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    g = sum(x["pnl"] for x in tr if x["pnl"] > 0)
    l = -sum(x["pnl"] for x in tr if x["pnl"] < 0)
    top = sorted((x["pnl"] for x in tr), reverse=True)
    tot = sum(x["pnl"] for x in tr)
    return dict(n=len(tr), total=tot, pf=(g / l if l else float("inf")), mdd=mdd,
                worst=min(daily) if daily else 0.0,
                worst_day=DAYS[daily.index(min(daily))] if daily else "",
                win=100.0 * sum(1 for x in tr if x["pnl"] > 0) / max(len(tr), 1),
                avg=tot / max(len(tr), 1), loss_days=sum(1 for v in daily if v < 0),
                ex1=tot - sum(top[:1]), ex2=tot - sum(top[:2]))


ROWS = {}
for arg in sys.argv[1:]:
    k, v = arg.split("=", 1)
    ROWS[k] = load(v)
M = {k: metrics(v) for k, v in ROWS.items()}
keys = list(ROWS)

print(f"기간 {DAYS[0]}~{DAYS[-1]} ({len(DAYS)}영업일, MU 1분봉이 production 스키마로 수집된 구간)\n")
hdr = "".join(f"{k:>16s}" for k in keys)
print(f"{'':14s}{hdr}")
for key, lab, f in (("n", "거래수", "{:16,.0f}"), ("total", "총손익", "{:+16,.0f}"),
                    ("pf", "PF", "{:16.3f}"), ("mdd", "MDD", "{:16,.0f}"),
                    ("worst", "최악일", "{:+16,.0f}"), ("win", "승률%", "{:16.1f}"),
                    ("avg", "거래당평균", "{:+16,.0f}"), ("loss_days", "손실일수", "{:16,.0f}"),
                    ("ex1", "상위1제외", "{:+16,.0f}"), ("ex2", "상위2제외", "{:+16,.0f}")):
    print(f"{lab:14s}" + "".join(f.format(M[k][key]) for k in keys))
print(f"{'최악일자':14s}" + "".join(f"{M[k]['worst_day']:>16s}" for k in keys))

print(f"\n{'=' * 100}\n일자별 손익\n{'=' * 100}")
print(f"{'일자':10s}" + "".join(f"{k:>16s}" for k in keys))
for d in DAYS:
    print(f"{d:10s}" + "".join(f"{sum(x['pnl'] for x in ROWS[k][d]):+16,.0f}" for k in keys))

print(f"\n{'=' * 100}\n방향별\n{'=' * 100}")
for k in keys:
    tr = [x for d in DAYS for x in ROWS[k][d]]
    print(f"  [{k}]")
    for dr in ("RED", "BLUE"):
        s = [x for x in tr if x["dir"] == dr]
        if not s:
            print(f"    {dr:5s}  0건"); continue
        print(f"    {dr:5s} {len(s):3d}건  손익 {sum(x['pnl'] for x in s):+12,.0f}  "
              f"승률 {100 * sum(1 for x in s if x['pnl'] > 0) / len(s):5.1f}%  "
              f"평균 {sum(x['pnl'] for x in s) / len(s):+10,.0f}")

print(f"\n{'=' * 100}\n거래 전수\n{'=' * 100}")
for k in keys:
    print(f"\n  [{k}]")
    for d in DAYS:
        for x in ROWS[k][d]:
            print(f"   {d} {x['t']} {x['dir']:4s} @{x['px']:>7,.0f} x{x['qty']:>5,d} -> "
                  f"{x['exit']}  {x['net']:+6.2f}%  {x['pnl']:+11,.0f}  {x['why'][:38]}")

if len(keys) >= 2:
    a, b = keys[0], keys[1]
    print(f"\n{'=' * 100}\n{a} 가 잡고 {b} 가 놓친 / {b} 가 새로 잡은 거래 (|손익| >= 200,000)\n{'=' * 100}")
    for d in DAYS:
        ka = {(x["t"], x["sym"]) for x in ROWS[a][d]}
        kb = {(x["t"], x["sym"]) for x in ROWS[b][d]}
        for x in ROWS[a][d]:
            if (x["t"], x["sym"]) not in kb and abs(x["pnl"]) >= 200000:
                print(f"  놓침 {d} {x['t']} {x['dir']:4s} {x['pnl']:+12,.0f}  {x['why'][:30]}")
        for x in ROWS[b][d]:
            if (x["t"], x["sym"]) not in ka and abs(x["pnl"]) >= 200000:
                tag = "새승자" if x["pnl"] > 0 else "새패자"
                print(f"  {tag} {d} {x['t']} {x['dir']:4s} {x['pnl']:+12,.0f}  {x['why'][:30]}")
