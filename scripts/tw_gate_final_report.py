#!/usr/bin/env python
"""2026-08-17 final step: consolidate every TRAIN/VAL result gathered across
stage1 (scripts/tw_gate_stage1_candidates.py) and stage2
(scripts/tw_gate_stage2_exit_sweep.py) into a single ranked table, confirm
the winner, and run FINAL OOS (11 days, 20%) EXACTLY ONCE against that one
locked strategy. Per the user's explicit instruction, no parameter is
changed after this OOS result is seen -- this script only reports it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
import scripts.tw_gate_stage1_candidates as st1  # noqa: E402
import scripts.tw_gate_stage2_exit_sweep as st2  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"


def score(tr_m, va_m):
    if tr_m["total_simple_cumulative_return_pct"] <= 0 or va_m["total_simple_cumulative_return_pct"] <= 0:
        return (-1, 0, 0)
    tr_pf = tr_m["profit_factor"] if isinstance(tr_m["profit_factor"], (int, float)) else 0
    va_pf = va_m["profit_factor"] if isinstance(va_m["profit_factor"], (int, float)) else 0
    combined_pf = min(tr_pf, va_pf)
    combined_mdd = max(tr_m["max_drawdown_pct"], va_m["max_drawdown_pct"])
    combined_cum = tr_m["total_simple_cumulative_return_pct"] + va_m["total_simple_cumulative_return_pct"]
    return (combined_pf, -combined_mdd, combined_cum)


if __name__ == "__main__":
    stage1 = json.loads((OUTPUT_DIR / "stage1_entry_candidates_train_val.json").read_text(encoding="utf-8"))
    stage2 = json.loads((OUTPUT_DIR / "stage2_exit_sweep.json").read_text(encoding="utf-8"))

    pool = []
    default_ladder_desc = "AM_TP1=2.5%@50%/SL=-1.5%(default), PM_TP=2.5%/SL=-1.2%(default)"
    for label, m in stage1.items():
        pool.append({"entry": label, "exit": default_ladder_desc, "train": m["train"], "val": m["val"]})
    for r in stage2["pass_a_all"]:
        pool.append({"entry": st2.LOCKED_ENTRY.label, "exit": r["label"] + " + PM=default(2.5%/-1.2%)", "train": r["train"], "val": r["val"]})
    for r in stage2["pass_b_all"]:
        pool.append({"entry": st2.LOCKED_ENTRY.label, "exit": stage2["pass_a_winner"]["label"] + " + " + r["label"], "train": r["train"], "val": r["val"]})

    for row in pool:
        row["score"] = score(row["train"], row["val"])
    pool.sort(key=lambda r: r["score"], reverse=True)

    print("=== TOP 5 (ranked by min(TRAIN PF, VAL PF), then MDD, then combined cum, TRAIN&VAL both required positive) ===")
    for i, row in enumerate(pool[:5], 1):
        tr, va = row["train"], row["val"]
        print(f"\n#{i}  entry={row['entry']}\n    exit ={row['exit']}")
        print(f"    TRAIN: entries/day={tr['avg_entries_per_day']} win={tr['win_rate_pct']}% simple={tr['total_simple_cumulative_return_pct']}% compounded={tr['compounded_cumulative_return_pct']}% PF={tr['profit_factor']} MDD={tr['max_drawdown_pct']} maxConsecLoss={tr['max_consecutive_losses']} AM/PM={tr['morning_entries']}/{tr['afternoon_entries']}")
        print(f"    VAL  : entries/day={va['avg_entries_per_day']} win={va['win_rate_pct']}% simple={va['total_simple_cumulative_return_pct']}% compounded={va['compounded_cumulative_return_pct']}% PF={va['profit_factor']} MDD={va['max_drawdown_pct']} maxConsecLoss={va['max_consecutive_losses']} AM/PM={va['morning_entries']}/{va['afternoon_entries']}")

    winner = pool[0]
    print(f"\n=== WINNER selected for FINAL OOS: entry={winner['entry']} | exit={winner['exit']} ===")

    print("\nLoading OOS cache (11 days, 2026-07-31~08-14) -- FIRST AND ONLY TOUCH...")
    oos_cache, oos_notes = base._prepare_day_cache(base.OOS_DATES)
    print(f"OOS={len(oos_cache)}d")
    for n in oos_notes:
        print("  note:", n)

    final_overrides = stage2["final_overrides"]
    with st2.ladder_override(**final_overrides):
        oos_trades = st1.run_ext_over_cache(oos_cache, st2.LOCKED_ENTRY)
        oos_m = base.metrics(oos_trades, len(oos_cache))

    print("\n=== FINAL STRATEGY -- TRAIN / VAL / FINAL OOS (fully separated) ===")
    tr_m, va_m = stage2["final_train"], stage2["final_val"]
    for name, m in (("TRAIN (34d, 05/27-07/14)", tr_m), ("VAL   (11d, 07/15-07/30)", va_m), ("FINAL OOS (11d, 07/31-08/14)", oos_m)):
        print(f"{name}: entries/day={m['avg_entries_per_day']} win={m['win_rate_pct']}% avg_daily={m['avg_daily_return_pct']}% "
              f"simple_cum={m['total_simple_cumulative_return_pct']}% compounded={m['compounded_cumulative_return_pct']}% "
              f"PF={m['profit_factor']} MDD={m['max_drawdown_pct']} maxConsecLoss={m['max_consecutive_losses']} AM/PM={m['morning_entries']}/{m['afternoon_entries']}")

    dump = {
        "top5": pool[:5],
        "winner": {"entry": winner["entry"], "exit": winner["exit"], "overrides": final_overrides},
        "final_train": tr_m, "final_val": va_m, "final_oos": oos_m,
        "oos_notes": oos_notes,
    }
    (OUTPUT_DIR / "FINAL_report.json").write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved -> {OUTPUT_DIR / 'FINAL_report.json'}")
