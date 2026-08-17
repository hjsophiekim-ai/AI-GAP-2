#!/usr/bin/env python
"""2026-08-18 user request: run the PREVIOUSLY committed "13시까지만 거래"
version (commit 8591568^ -- quality_score threshold 4 with the old per-
window special cases, TW_MORNING_ONLY=True) completely unmodified, on the
exact same TRAIN(34d)/VAL(11d)/FINAL OOS(11d) 56-day split already used
throughout this session, and compare it side by side against the CURRENT
committed "게이트 전체 완화" baseline. No condition is changed on either
side -- this is a straight side-by-side re-run for comparison only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
import scripts.tw_gate_recent2weeks_compare as cmp  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"

if __name__ == "__main__":
    print(f"Current config: QUALITY_SCORE_THRESHOLD={base.config.QUALITY_SCORE_THRESHOLD} TW_MORNING_ONLY={base.config.TW_MORNING_ONLY}")

    print("\nLoading TRAIN/VAL/OOS day caches (56 trading days total)...")
    train_cache, _ = base._prepare_day_cache(base.TRAIN_DATES)
    val_cache, _ = base._prepare_day_cache(base.VAL_DATES)
    oos_cache, _ = base._prepare_day_cache(base.OOS_DATES)
    print(f"TRAIN={len(train_cache)}d VAL={len(val_cache)}d OOS={len(oos_cache)}d")

    periods = {"TRAIN": train_cache, "VAL": val_cache, "OOS": oos_cache}

    print("\n=== Running CURRENT (게이트 전체 완화) ===")
    current_results = {}
    for name, cache in periods.items():
        trades = cmp.run_scenario(cache, twf_module=base.twf, use_old_config=False)
        current_results[name] = base.metrics(trades, len(cache))

    print("=== Running PREVIOUS COMMIT (13시까지만, unmodified) ===")
    old_results = {}
    for name, cache in periods.items():
        trades = cmp.run_scenario(cache, twf_module=cmp.OLD_TWF, use_old_config=True)
        old_results[name] = base.metrics(trades, len(cache))

    assert base.config.QUALITY_SCORE_THRESHOLD == 2 and base.config.TW_MORNING_ONLY is False, "config leaked!"
    print(f"\nConfig after run (must be unchanged): threshold={base.config.QUALITY_SCORE_THRESHOLD} morning_only={base.config.TW_MORNING_ONLY}")

    print("\n=== Side-by-side comparison ===")
    header = f"{'period':<7} {'strategy':<28} {'entries/day':>11} {'win%':>6} {'simple%':>8} {'compounded%':>11} {'PF':>7} {'MDD':>7} {'maxConsecLoss':>13}"
    print(header)
    for name in ("TRAIN", "VAL", "OOS"):
        for label, m in (("1_게이트전체완화(현재)", current_results[name]), ("2_13시까지만(직전커밋)", old_results[name])):
            print(f"{name:<7} {label:<28} {m['avg_entries_per_day']:>11} {m['win_rate_pct']:>6} {m['total_simple_cumulative_return_pct']:>8} "
                  f"{m['compounded_cumulative_return_pct']:>11} {str(m['profit_factor']):>7} {m['max_drawdown_pct']:>7} {m['max_consecutive_losses']:>13}")

    def chained(results):
        factor = 1.0
        for name in ("TRAIN", "VAL", "OOS"):
            factor *= (1.0 + results[name]["compounded_cumulative_return_pct"] / 100.0)
        return (factor - 1.0) * 100.0

    print(f"\nFull-span chained compounded: 게이트전체완화={chained(current_results):.2f}%  13시까지만={chained(old_results):.2f}%")

    dump = {
        "current_gate_relaxed": current_results, "previous_morning_only": old_results,
        "chained_compounded": {"gate_relaxed": chained(current_results), "morning_only": chained(old_results)},
    }
    (OUTPUT_DIR / "prev_commit_vs_current_full_split.json").write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved -> {OUTPUT_DIR / 'prev_commit_vs_current_full_split.json'}")
