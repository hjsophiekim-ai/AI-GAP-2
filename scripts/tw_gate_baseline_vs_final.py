#!/usr/bin/env python
"""2026-08-17 user request: compare the ORIGINAL, UNMODIFIED "게이트 전체
완화" baseline (quality>=2, gap expansion required, 9min flag interval,
morning<=3/afternoon<=2/daily<=5, no window blocks, no direction bonus,
NO extra flag_seq/entry_seq restrictions -- and the CURRENT/default TP-SL
ladder, i.e. no override at all: "현재 익절·손절 래더 유지" per the original
spec) against the FINAL strategy locked in scripts/tw_gate_stage1_candidates
.py + scripts/tw_gate_stage2_exit_sweep.py, on the exact same TRAIN(34d)/
VAL(11d)/FINAL OOS(11d) split, using the identical simulate_ext code path
(same cost engine, same real ETF prices, same T+3 confirmation) for both --
so the only difference between the two runs is the entry-gate params and
ladder overrides.

STRICT NO-REOPTIMIZATION: neither side's parameters are touched here. This
script only runs both, exactly as already defined, and reports/compares.
Every individual trade (entry time/price, exit time/price, return%) from
BOTH strategies across ALL THREE periods is dumped in full.
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
import scripts.tw_gate_stage1_candidates as st1  # noqa: E402
import scripts.tw_gate_stage2_exit_sweep as st2  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"

BASELINE = st1.ExtParams(
    label="BASELINE_게이트전체완화 (quality>=2, default TP/SL ladder, unmodified)",
    base_params=base.EntryParams(
        quality_threshold=2, require_gap_expansion=True, min_flag_interval_minutes=9,
        max_morning_entries=3, max_afternoon_entries=2, max_daily_entries=5,
    ),
    max_flag_seq_of_day=None, excluded_entry_seq_of_day=(),
)
BASELINE_OVERRIDES = None  # None == leave twpm module constants completely untouched (current defaults)

FINAL = st2.LOCKED_ENTRY
FINAL_OVERRIDES = None  # filled in from stage2_exit_sweep.json below


def trade_to_row(strategy: str, period: str, t: base.Trade) -> dict:
    d = asdict(t)
    d["strategy"] = strategy
    d["period"] = period
    d["legs"] = json.dumps(d["legs"], ensure_ascii=False)
    return d


def run_strategy(strategy_label: str, ext: st1.ExtParams, overrides: dict | None, caches: dict) -> dict:
    out = {}
    for period, cache in caches.items():
        if overrides:
            with st2.ladder_override(**overrides):
                trades = st1.run_ext_over_cache(cache, ext)
        else:
            trades = st1.run_ext_over_cache(cache, ext)
        m = base.metrics(trades, len(cache))
        out[period] = {"metrics": m, "trades": trades}
    return out


if __name__ == "__main__":
    stage2 = json.loads((OUTPUT_DIR / "stage2_exit_sweep.json").read_text(encoding="utf-8"))
    FINAL_OVERRIDES = stage2["final_overrides"]

    print("Loading TRAIN/VAL/OOS day caches (shared across both strategies)...")
    train_cache, _ = base._prepare_day_cache(base.TRAIN_DATES)
    val_cache, _ = base._prepare_day_cache(base.VAL_DATES)
    oos_cache, _ = base._prepare_day_cache(base.OOS_DATES)
    caches = {"TRAIN": train_cache, "VAL": val_cache, "OOS": oos_cache}
    print(f"TRAIN={len(train_cache)}d VAL={len(val_cache)}d OOS={len(oos_cache)}d")

    print("\nRunning BASELINE (게이트 전체 완화, unmodified)...")
    baseline_result = run_strategy("BASELINE", BASELINE, BASELINE_OVERRIDES, caches)

    print("Running FINAL strategy (already locked)...")
    final_result = run_strategy("FINAL", FINAL, FINAL_OVERRIDES, caches)

    def show(label, result):
        print(f"\n=== {label} ===")
        for period in ("TRAIN", "VAL", "OOS"):
            m = result[period]["metrics"]
            print(f"  {period:5s}: n={m['total_entries']:>3d} entries/day={m['avg_entries_per_day']:<6} win={m['win_rate_pct']:>6.2f}% "
                  f"avg_daily={m['avg_daily_return_pct']:>7.4f}% simple={m['total_simple_cumulative_return_pct']:>8.2f}% "
                  f"compounded={m['compounded_cumulative_return_pct']:>8.2f}% PF={m['profit_factor']} MDD={m['max_drawdown_pct']:>6.2f} "
                  f"maxConsecLoss={m['max_consecutive_losses']} AM/PM={m['morning_entries']}/{m['afternoon_entries']}")

    show("BASELINE (게이트 전체 완화, entry+ladder UNCHANGED)", baseline_result)
    show("FINAL (stage1+stage2에서 확정된 전략)", final_result)

    # full period (TRAIN+VAL+OOS chained) compounded, for the "실제 최선의 결과 몇 %" honesty check
    def chained_compounded(result):
        factor = 1.0
        for period in ("TRAIN", "VAL", "OOS"):
            factor *= (1.0 + result[period]["metrics"]["compounded_cumulative_return_pct"] / 100.0)
        return (factor - 1.0) * 100.0

    print(f"\nBASELINE full-span (TRAIN->VAL->OOS chained) compounded: {chained_compounded(baseline_result):.2f}%")
    print(f"FINAL    full-span (TRAIN->VAL->OOS chained) compounded: {chained_compounded(final_result):.2f}%")

    # ---- dump every individual trade from both strategies, all periods ----
    all_rows = []
    for period in ("TRAIN", "VAL", "OOS"):
        for t in baseline_result[period]["trades"]:
            all_rows.append(trade_to_row("BASELINE", period, t))
        for t in final_result[period]["trades"]:
            all_rows.append(trade_to_row("FINAL", period, t))
    all_rows.sort(key=lambda r: (r["strategy"], r["period"], r["entry_time"]))

    fieldnames = ["strategy", "period", "trading_date", "direction", "entry_symbol", "window", "quality_score",
                  "flag_seq_of_day", "entry_time", "entry_price", "exit_time", "exit_price", "exit_reason",
                  "tp1_hit", "tp2_hit", "net_return_pct", "legs"]
    csv_path = OUTPUT_DIR / "baseline_vs_final_all_trades.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in all_rows:
            w.writerow({k: row.get(k) for k in fieldnames})
    print(f"\nSaved ALL {len(all_rows)} trades -> {csv_path}")

    summary = {
        "baseline": {p: baseline_result[p]["metrics"] for p in ("TRAIN", "VAL", "OOS")},
        "final": {p: final_result[p]["metrics"] for p in ("TRAIN", "VAL", "OOS")},
        "baseline_chained_compounded_pct": chained_compounded(baseline_result),
        "final_chained_compounded_pct": chained_compounded(final_result),
    }
    (OUTPUT_DIR / "baseline_vs_final_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Saved summary -> {OUTPUT_DIR / 'baseline_vs_final_summary.json'}")
