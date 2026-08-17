#!/usr/bin/env python
"""One-off helper: convert baseline_vs_final_all_trades.csv + summary json
into a compact JS-embeddable JSON file for the trade-ledger artifact page.
Not part of the research pipeline; safe to delete after the artifact is built."""
import csv
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "data" / "validation" / "tw_gate_relaxed_optimization"
csv_path = BASE / "baseline_vs_final_all_trades.csv"
summary_path = BASE / "baseline_vs_final_summary.json"

rows = []
with csv_path.open(encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        legs = json.loads(r["legs"])
        legs_summary = " -> ".join(f"{frac*100:.0f}%@{price:,.0f}({reason.replace('TIME_WINDOW_', '')})" for frac, price, reason in legs)
        rows.append({
            "s": r["strategy"], "p": r["period"], "d": r["trading_date"],
            "dir": "LONG" if r["direction"] == "UP_RED" else "INV",
            "sym": r["entry_symbol"], "win": r["window"].split("_", 1)[1].replace("_", " ").title(),
            "q": int(r["quality_score"]), "fseq": int(r["flag_seq_of_day"]),
            "et": r["entry_time"][11:16], "ep": round(float(r["entry_price"]), 1),
            "xt": r["exit_time"][11:16], "xp": round(float(r["exit_price"]), 1),
            "reason": r["exit_reason"].replace("TIME_WINDOW_", ""),
            "ret": round(float(r["net_return_pct"]), 3),
            "legs": legs_summary,
        })

summary = json.loads(summary_path.read_text(encoding="utf-8"))

out = {"trades": rows, "summary": summary}
out_path = BASE / "_trades_artifact_data.json"
out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"rows={len(rows)} -> {out_path} ({out_path.stat().st_size} bytes)")
