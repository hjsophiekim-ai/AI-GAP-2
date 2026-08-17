#!/usr/bin/env python
"""2026-08-18 REGRESSION CHECK for the revert-to-previous-commit: confirm
that PRODUCTION's real time_window_filter.evaluate_time_window_entry (now
reverted -- quality_threshold=4, TW_MORNING_ONLY=True, old per-window
special cases restored) reproduces, over the full TRAIN(34d)/VAL(11d)/
FINAL OOS(11d) split, exactly the numbers already recorded for "13시까지만"
in data/validation/tw_gate_relaxed_optimization/
prev_commit_vs_current_full_split.json's "previous_morning_only" section
(which was computed by loading that same code from git history + temporary
config monkeypatch, BEFORE the revert). If production's real function now
gives identical numbers with no monkeypatching needed at all, the revert is
byte-for-byte correct.
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

KEYS = ["total_entries", "avg_entries_per_day", "morning_entries", "afternoon_entries",
        "win_rate_pct", "total_simple_cumulative_return_pct", "compounded_cumulative_return_pct",
        "profit_factor", "max_drawdown_pct", "max_consecutive_losses"]

if __name__ == "__main__":
    print(f"config.QUALITY_SCORE_THRESHOLD = {base.config.QUALITY_SCORE_THRESHOLD} (expect 4)")
    print(f"config.TW_MORNING_ONLY = {base.config.TW_MORNING_ONLY} (expect True)")
    assert base.config.QUALITY_SCORE_THRESHOLD == 4
    assert base.config.TW_MORNING_ONLY is True

    train_cache, _ = base._prepare_day_cache(base.TRAIN_DATES)
    val_cache, _ = base._prepare_day_cache(base.VAL_DATES)
    oos_cache, _ = base._prepare_day_cache(base.OOS_DATES)
    periods = {"TRAIN": train_cache, "VAL": val_cache, "OOS": oos_cache}

    print("\nRunning PRODUCTION's real (now-reverted) evaluate_time_window_entry() directly, no monkeypatch...")
    prod_results = {}
    for name, cache in periods.items():
        trades = cmp.run_scenario(cache, twf_module=base.twf, use_old_config=False)  # no monkeypatch needed anymore
        prod_results[name] = base.metrics(trades, len(cache))

    recorded = json.loads((OUTPUT_DIR / "prev_commit_vs_current_full_split.json").read_text(encoding="utf-8"))["previous_morning_only"]

    all_match = True
    mismatches = []
    for name in ("TRAIN", "VAL", "OOS"):
        print(f"\n{name}:")
        for k in KEYS:
            pv, rv = prod_results[name].get(k), recorded[name].get(k)
            close = (pv == rv) if not isinstance(pv, float) else (rv is not None and abs(pv - rv) < 0.05)
            if not close:
                all_match = False
                mismatches.append((name, k, pv, rv))
            print(f"  {k:<38} production={pv!s:<12} recorded={rv!s:<12} [{'OK' if close else 'MISMATCH'}]")

    print(f"\n{'ALL MATCH -- revert confirmed byte-for-byte.' if all_match else 'MISMATCH FOUND'}")
    for m in mismatches:
        print("  MISMATCH:", m)

    (OUTPUT_DIR / "revert_regression_check.json").write_text(
        json.dumps({"production": prod_results, "recorded_previous": recorded, "all_match": all_match, "mismatches": mismatches}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nSaved -> {OUTPUT_DIR / 'revert_regression_check.json'}")
    sys.exit(0 if all_match else 1)
