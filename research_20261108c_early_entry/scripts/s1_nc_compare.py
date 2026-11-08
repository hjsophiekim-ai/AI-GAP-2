"""A(현행 T+3) vs B(T+3 재확인 거절 제거, WK_NOCONF=1) — 재생 결과 비교 + 새로 승인된 후보 전수."""
import json, os, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))

def trades(j):
    """당일 체결만 — orders(당일 한정)와 ex(전일 포함)를 순서대로 정합한다."""
    ex = list(j.get("ex") or []); out = []; pos = 0; cur = None
    for o in j.get("orders") or []:
        key = (o["side"], o["sym"], float(o["qty"]), float(o["px"]))
        while pos < len(ex):
            e = ex[pos]; pos += 1
            if (e.get("side"), e.get("symbol"), float(e.get("executed_qty") or 0),
                    float(e.get("executed_price") or 0)) == key:
                if o["side"] == "BUY":
                    if cur: out.append(cur)
                    cur = dict(t=o["t"][11:16], sym=o["sym"], qty=o["qty"], px=o["px"],
                               pnl=0.0, ex=[], act="|".join(o.get("acts") or []))
                elif cur is not None:
                    cur["pnl"] += float(e.get("net_pnl") or 0); cur["ex"].append(e.get("exit_reason") or "")
                break
    if cur: out.append(cur)
    return out

DAYS = sorted(p.stem.split("_")[-1] for p in OUT.glob("NC_B_*.json"))
ta = tb = na = nb = 0; diffs = []
print(f"=== A(현행) vs B(T+3 거절 제거) — {len(DAYS)}일 ===")
print(f"{'날짜':10s} {'A건':>3s} {'A손익':>12s} {'B건':>3s} {'B손익':>12s} {'차이':>12s}  승인/판정")
for d in DAYS:
    A = json.load(open(OUT / f"CG_A_{d}.json", encoding="utf-8"))
    B = json.load(open(OUT / f"NC_B_{d}.json", encoding="utf-8"))
    a, b = trades(A), trades(B)
    sa, sb = sum(x["pnl"] for x in a), sum(x["pnl"] for x in b)
    ta += sa; tb += sb; na += len(a); nb += len(b)
    nc = [x for x in (B.get("nc") or []) if x["t"][:10].replace("-", "") == d]
    app = sum(1 for x in nc if x.get("approved"))
    print(f"{d:10s} {len(a):3d} {sa:12,.0f} {len(b):3d} {sb:12,.0f} {sb-sa:12,.0f}  {app}/{len(nc)}"
          + ("  <<" if abs(sb - sa) > 1 else ""))
    if abs(sb - sa) > 1:
        sat = {x["t"] for x in a}; sbt = {x["t"] for x in b}
        for x in a:
            if x["t"] not in sbt:
                diffs.append(f"  - A {d} {x['t']} {x['sym']} @{x['px']} {x['pnl']:>12,.0f} [{'|'.join(x['ex'])}]")
        for x in b:
            if x["t"] not in sat:
                diffs.append(f"  + B {d} {x['t']} {x['sym']} @{x['px']} {x['pnl']:>12,.0f} [{'|'.join(x['ex'])}] {x['act']}")
print(f"\n{'합계':10s} {na:3d} {ta:12,.0f} {nb:3d} {tb:12,.0f} {tb-ta:12,.0f}")
print("\n=== 달라진 거래 ===")
print("\n".join(diffs) if diffs else "  없음")
print("\n=== 새로 승인/재차단된 후보 전수 ===")
tot = app_ = 0
for d in DAYS:
    B = json.load(open(OUT / f"NC_B_{d}.json", encoding="utf-8"))
    seen = set()
    for x in (B.get("nc") or []):
        if x["t"][:10].replace("-", "") != d: continue
        k = (x["t"][11:16], x["was"])
        if k in seen: continue
        seen.add(k); tot += 1; app_ += bool(x.get("approved"))
        print(f"  {d} {x['t'][11:16]} {x['was']:34s} -> {str(x['now']):30s} "
              f"{'승인' if x.get('approved') else '재차단'}")
print(f"\n  총 {tot}건 중 승인 {app_}건")
