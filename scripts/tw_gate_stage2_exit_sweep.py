#!/usr/bin/env python
"""2026-08-17 stage 2 (TP/SL) sweep -- entry condition is LOCKED at stage 1's
winner (candidate 5 "Q3_dirbonus_maxflag4_noseq4" from
scripts/tw_gate_stage1_candidates.py) and never touched here. Only sweeps
the user's requested exit-ladder items:
  7. AM TP1: +2.0%@30%/50%, +2.5%@30%/50% (TP2 fixed +5.0%, per spec)
  8. AM SL: -1.0 / -1.25 / -1.5%
  9. PM TP: +2.0 / +2.5 / +3.0%
  10. PM SL: -0.8 / -1.0 / -1.2%
Coordinate-descent, not full brute force (per user's "brute force 금지"):
  Pass A sweeps AM TP1 x AM SL (12 combos) with PM held at current default
    (TP 2.5% / SL -1.2%), ranks by TRAIN+VAL PF/MDD, keeps the AM winner.
  Pass B sweeps PM TP x PM SL (9 combos) with AM held at Pass A's winner,
    picks the PM winner.
  Final candidate = Pass A's AM winner + Pass B's PM winner, re-verified
  once on TRAIN+VAL together.
Sweeps by temporarily monkey-patching time_window_position_manager's own
module-level *_PCT constants (same technique the original backtest_time_
window_filter.py script already uses) -- every combo explicitly restores
the ORIGINAL values in a try/finally immediately after, so no state can leak
between combos (this discipline was verified missing in the prior ad-hoc
interactive stage-2 run whose numbers could not be reproduced).
Read-only research; no production code touched; FINAL OOS is NOT run here.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
import scripts.tw_gate_stage1_candidates as st1  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"

LOCKED_ENTRY = st1.ExtParams(
    label="LOCKED_entry_Q3_dirbonus_maxflag4_noseq4",
    base_params=base.EntryParams(
        quality_threshold=3, require_gap_expansion=True, min_flag_interval_minutes=9,
        max_morning_entries=3, max_afternoon_entries=2, max_daily_entries=5,
        direction_quality_bonus={Direction.UP_RED: 1},
    ),
    max_flag_seq_of_day=4,
    excluded_entry_seq_of_day=(4,),
)

_LADDER_ATTRS = [
    "MORNING_TP1_PCT", "MORNING_TP1_SELL_RATIO", "MORNING_TP2_PCT", "MORNING_STOP_LOSS_PCT",
    "AFTERNOON_TP_PCT", "AFTERNOON_STOP_LOSS_PCT",
]


@contextmanager
def ladder_override(**overrides):
    originals = {name: getattr(base.twpm, name) for name in _LADDER_ATTRS}
    try:
        for k, v in overrides.items():
            setattr(base.twpm, k, v)
        yield
    finally:
        for name, val in originals.items():
            setattr(base.twpm, name, val)


def run_combo(train_cache, val_cache, **overrides):
    with ladder_override(**overrides):
        train_trades = st1.run_ext_over_cache(train_cache, LOCKED_ENTRY)
        val_trades = st1.run_ext_over_cache(val_cache, LOCKED_ENTRY)
        tr_m = base.metrics(train_trades, len(train_cache))
        va_m = base.metrics(val_trades, len(val_cache))
    return tr_m, va_m


def score(tr_m, va_m):
    """Rank key matching the user's stated priority: PF first, then MDD
    (lower better), then cumulative return -- computed on TRAIN+VAL jointly
    so a combo can't win by only helping one side; requires both positive."""
    if tr_m["total_simple_cumulative_return_pct"] <= 0 or va_m["total_simple_cumulative_return_pct"] <= 0:
        return (-1, 0, 0)
    tr_pf = tr_m["profit_factor"] if isinstance(tr_m["profit_factor"], (int, float)) else 0
    va_pf = va_m["profit_factor"] if isinstance(va_m["profit_factor"], (int, float)) else 0
    combined_pf = min(tr_pf, va_pf)
    combined_mdd = max(tr_m["max_drawdown_pct"], va_m["max_drawdown_pct"])
    combined_cum = tr_m["total_simple_cumulative_return_pct"] + va_m["total_simple_cumulative_return_pct"]
    return (combined_pf, -combined_mdd, combined_cum)


if __name__ == "__main__":
    print("Loading TRAIN/VAL day caches...")
    train_cache, _ = base._prepare_day_cache(base.TRAIN_DATES)
    val_cache, _ = base._prepare_day_cache(base.VAL_DATES)
    print(f"TRAIN={len(train_cache)}d VAL={len(val_cache)}d")

    DEFAULT_PM_TP, DEFAULT_PM_SL = 2.5, -1.2
    DEFAULT_TP2 = 5.0

    print("\n=== Pass A: AM TP1 x AM SL (PM held at default 2.5%/-1.2%) ===")
    pass_a_results = []
    for tp1_pct in (2.0, 2.5):
        for sell_ratio in (0.30, 0.50):
            for am_sl in (-1.0, -1.25, -1.5):
                overrides = dict(
                    MORNING_TP1_PCT=tp1_pct, MORNING_TP1_SELL_RATIO=sell_ratio, MORNING_TP2_PCT=DEFAULT_TP2,
                    MORNING_STOP_LOSS_PCT=am_sl, AFTERNOON_TP_PCT=DEFAULT_PM_TP, AFTERNOON_STOP_LOSS_PCT=DEFAULT_PM_SL,
                )
                tr_m, va_m = run_combo(train_cache, val_cache, **overrides)
                label = f"AM_TP1={tp1_pct}%@{int(sell_ratio*100)}%_SL={am_sl}%"
                pass_a_results.append({"label": label, "overrides": overrides, "train": tr_m, "val": va_m, "score": score(tr_m, va_m)})
                print(f"{label:<32} TRAIN cum={tr_m['total_simple_cumulative_return_pct']:>7.2f}% PF={tr_m['profit_factor']} MDD={tr_m['max_drawdown_pct']:>6.2f} | VAL cum={va_m['total_simple_cumulative_return_pct']:>7.2f}% PF={va_m['profit_factor']} MDD={va_m['max_drawdown_pct']:>6.2f}")

    pass_a_results.sort(key=lambda r: r["score"], reverse=True)
    am_winner = pass_a_results[0]
    print(f"\nPass A winner: {am_winner['label']}")

    print("\n=== Pass B: PM TP x PM SL (AM held at Pass A winner) ===")
    am_ov = am_winner["overrides"]
    pass_b_results = []
    for pm_tp in (2.0, 2.5, 3.0):
        for pm_sl in (-0.8, -1.0, -1.2):
            overrides = dict(
                MORNING_TP1_PCT=am_ov["MORNING_TP1_PCT"], MORNING_TP1_SELL_RATIO=am_ov["MORNING_TP1_SELL_RATIO"],
                MORNING_TP2_PCT=DEFAULT_TP2, MORNING_STOP_LOSS_PCT=am_ov["MORNING_STOP_LOSS_PCT"],
                AFTERNOON_TP_PCT=pm_tp, AFTERNOON_STOP_LOSS_PCT=pm_sl,
            )
            tr_m, va_m = run_combo(train_cache, val_cache, **overrides)
            label = f"PM_TP={pm_tp}%_SL={pm_sl}%"
            pass_b_results.append({"label": label, "overrides": overrides, "train": tr_m, "val": va_m, "score": score(tr_m, va_m)})
            print(f"{label:<32} TRAIN cum={tr_m['total_simple_cumulative_return_pct']:>7.2f}% PF={tr_m['profit_factor']} MDD={tr_m['max_drawdown_pct']:>6.2f} | VAL cum={va_m['total_simple_cumulative_return_pct']:>7.2f}% PF={va_m['profit_factor']} MDD={va_m['max_drawdown_pct']:>6.2f}")

    pass_b_results.sort(key=lambda r: r["score"], reverse=True)
    pm_winner = pass_b_results[0]
    print(f"\nPass B winner: {pm_winner['label']}")

    print("\n=== Final combined re-verification ===")
    final_overrides = pm_winner["overrides"]
    tr_m, va_m = run_combo(train_cache, val_cache, **final_overrides)
    print(f"FINAL {final_overrides} TRAIN={tr_m} VAL={va_m}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dump = {
        "locked_entry": LOCKED_ENTRY.label,
        "pass_a_all": pass_a_results,
        "pass_a_winner": am_winner,
        "pass_b_all": pass_b_results,
        "pass_b_winner": pm_winner,
        "final_overrides": final_overrides,
        "final_train": tr_m,
        "final_val": va_m,
    }
    (OUTPUT_DIR / "stage2_exit_sweep.json").write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved -> {OUTPUT_DIR / 'stage2_exit_sweep.json'}")
