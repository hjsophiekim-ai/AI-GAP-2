"""재생 A 가 실제 production 체결을 재현하는가 — 11/05·11/06 실체결 원장과 대조."""
import csv, json, os, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
LED = Path(r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2"
           r"/b8fc9310-d582-4463-b146-e0f3b55feb3b/scratchpad/led")
for day, ymd in (("2026-11-05", "20261105"), ("2026-11-06", "20261106")):
    p = OUT / f"CG_A_{ymd}.json"
    if not p.exists():
        print(f"{day}: 재생 결과 없음"); continue
    j = json.load(open(p, encoding="utf-8"))
    print(f"=== {day} ===")
    print("  [production 실체결]")
    tot = 0.0
    with open(LED / ymd / "macd2_execution_ledger.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            tot += float(r["net_pnl"] or 0)
            print(f"    {r['timestamp'][11:19]} {r['side']:4s} {r['symbol']:7s} "
                  f"qty={r['executed_qty']:>5s} px={r['executed_price']:>10s} "
                  f"net={float(r['net_pnl'] or 0):>12,.0f} {r['exit_reason']}")
    print(f"    합계 {tot:,.0f}")
    print("  [재생 A]")
    rt = 0.0
    for o in j.get("orders") or []:
        acts = "|".join(o.get("acts") or [])
        print(f"    {o['t'][11:19]} {o['side']:4s} {o['sym']:7s} qty={o['qty']:>5} px={o['px']:>10} {acts}")
    for e in j.get("ex") or []:
        rt += float(e.get("net_pnl") or 0)
    print(f"    합계 {rt:,.0f}")
    print()
