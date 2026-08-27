#!/usr/bin/env python
"""Builds the markdown tables (5-day per-flag detail, 2 target-flag deep
verification, morning-loser breakdown) from the JSON output of
scripts/teg_backtest_60day.py. Read-only, output-only -- no simulation here."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "teg_gate_v2_60day"
V1_OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "teg_gate_60day"
LAST5_DATES = {"20260820", "20260821", "20260824", "20260825", "20260826"}

TARGET_FLAGS = {
    ("20260825", "2026-08-25T12:09:00+09:00", "UP_RED"),
    ("20260826", "2026-08-26T11:06:00+09:00", "UP_RED"),
}


def load(variant: str):
    return json.loads((OUTPUT_DIR / f"flag_log_variant_{variant}.json").read_text(encoding="utf-8"))


def main():
    log_a = load("A")
    log_b = load("B")
    log_c = load("C")
    by_key_b = {(r["date"], r["flag_bar_start"], r["direction"]): r for r in log_b}
    by_key_c = {(r["date"], r["flag_bar_start"], r["direction"]): r for r in log_c}
    trades_a = json.loads((OUTPUT_DIR / "trades_variant_A.json").read_text(encoding="utf-8"))
    trades_b = json.loads((OUTPUT_DIR / "trades_variant_B.json").read_text(encoding="utf-8"))
    trades_c = json.loads((OUTPUT_DIR / "trades_variant_C.json").read_text(encoding="utf-8"))

    def outcome_for(trades, date, entry_time_prefix):
        for t in trades:
            if t["trading_date"] == date and t["entry_time"].startswith(entry_time_prefix):
                return t
        return None

    lines = []
    lines.append("# TEG 5-trading-day per-flag detail (2026-08-20..2026-08-26)\n")
    lines.append("| date | flag_time | dir | TW2(A) | TEG | A action | B action | C action | A outcome |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in sorted(log_a, key=lambda r: (r["date"], r["flag_bar_start"])):
        if row["date"] not in LAST5_DATES:
            continue
        key = (row["date"], row["flag_bar_start"], row["direction"])
        b = by_key_b.get(key, {})
        c = by_key_c.get(key, {})
        tw2 = "APPROVE" if row["tw2_approved"] else f"REJECT:{row['tw2_block_reason']}"
        teg_s = "APPROVE" if row["teg_approved"] else f"REJECT:{','.join(row['teg_reject_reasons'])}"
        a_act = "ENTER" if row["final_approved"] else "skip"
        b_act = "ENTER" if b.get("final_approved") else "skip"
        c_act = "ENTER" if c.get("final_approved") else ("BYPASS_ENTER" if c.get("final_reason") == "COUNT_CAP_BYPASSED_VIA_TEG" else "skip")
        confirm_prefix = row["confirm_bar_start"][:16]
        a_out = ""
        if row["final_approved"]:
            t = outcome_for(trades_a, row["date"], confirm_prefix)
            if t:
                a_out = f"{t['exit_reason']} net={t['net_return_pct']:.2f}%" if t.get("net_return_pct") is not None else "open/partial"
        lines.append(f"| {row['date']} | {row['flag_bar_start'][11:16]} | {row['direction']} | {tw2} | {teg_s} | {a_act} | {b_act} | {c_act} | {a_out} |")

    lines.append("\n\n# Target-flag deep verification\n")
    for row in log_a:
        key = (row["date"], row["flag_bar_start"], row["direction"])
        if key in TARGET_FLAGS:
            lines.append(f"\n## {row['date']} {row['flag_bar_start']} {row['direction']}")
            lines.append(f"- TW2(A): {'APPROVE' if row['tw2_approved'] else 'REJECT:' + row['tw2_block_reason']}")
            lines.append(f"- TEG: {'APPROVE' if row['teg_approved'] else 'REJECT'}")
            lines.append(f"- TEG conditions: {json.dumps(row['teg_conditions'], ensure_ascii=False)}")
            lines.append(f"- TEG metrics: {json.dumps(row['teg_metrics'], ensure_ascii=False, default=str)}")
            lines.append(f"- TEG reject reasons: {row['teg_reject_reasons']}")
            c = by_key_c.get(key, {})
            lines.append(f"- Variant C: {c.get('final_reason', '')}")

    # ── v1-vs-v2 delta callouts (5-day window) ──
    log_a_v1 = json.loads((V1_OUTPUT_DIR / "flag_log_variant_A.json").read_text(encoding="utf-8"))
    v1_by_key = {(r["date"], r["flag_bar_start"], r["direction"]): r for r in log_a_v1}
    lines.append("\n\n# v1 -> v2 TEG decision deltas (last 5 trading days)\n")
    delta_rows = []
    for row in log_a:
        if row["date"] not in LAST5_DATES:
            continue
        key = (row["date"], row["flag_bar_start"], row["direction"])
        v1_row = v1_by_key.get(key)
        if v1_row is None:
            continue
        if bool(v1_row["teg_approved"]) != bool(row["teg_approved"]):
            delta_rows.append((row["date"], row["flag_bar_start"][11:16], row["direction"], v1_row["teg_approved"], row["teg_approved"], row["teg_reject_reasons"], v1_row["teg_reject_reasons"]))
    if delta_rows:
        lines.append("| date | time | dir | v1 TEG | v2 TEG | v1 reject reasons | v2 reject reasons |")
        lines.append("|---|---|---|---|---|---|---|")
        for d, t, dr, v1a, v2a, v2r, v1r in delta_rows:
            lines.append(f"| {d} | {t} | {dr} | {'APPROVE' if v1a else 'reject'} | {'APPROVE' if v2a else 'reject'} | {','.join(v1r) or '-'} | {','.join(v2r) or '-'} |")
    else:
        lines.append("(no flips in the 5-day window)")

    out_path = OUTPUT_DIR / "REPORT_TABLES.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_path}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
