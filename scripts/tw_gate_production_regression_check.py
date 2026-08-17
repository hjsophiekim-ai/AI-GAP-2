#!/usr/bin/env python
"""2026-08-18 REGRESSION CHECK, per user instruction: after unifying
time_window_filter.evaluate_time_window_entry's per-window quality gate
(removing the W1-exempt/W2-reset-only/W6-EMA-only special cases) and
reverting config.QUALITY_SCORE_THRESHOLD (4->2) / config.TW_MORNING_ONLY
(True->False) to match the validated "게이트 전체 완화" baseline, confirm
that PRODUCTION'S OWN entry-decision function -- time_window_filter.
evaluate_time_window_entry(), the exact function app/trading/macd2/
worker.py's _resolve_time_window_candidate calls -- reproduces the TRAIN/
VAL/OOS numbers already recorded in data/validation/tw_gate_relaxed_
optimization/baseline_vs_final_summary.json's "baseline" section.

Two things are checked, on purpose, separately:
1) ENTRY-GATE PARITY (does production approve the exact same flags as the
   validated baseline?) -- total_entries / entries-per-day / morning-
   afternoon split must match EXACTLY (these are deterministic integer/
   count facts, not affected by any P&L accounting choice).
2) P&L PARITY, computed with the SAME blended-partial-exit accounting the
   recorded baseline used (scripts/tw_gate_relaxed_optimization.py's
   _record_partial_leg/_close_trade -- a TP1 partial sell's own price is
   quantity-weighted into the trade's final net_return_pct, matching what
   worker.py's real position tracking does). This script does NOT reuse
   scripts/backtest_time_window_filter.py's own simulate_time_window() for
   the P&L, because that script's _close_trade uses a naive entry->final
   -price %% that silently ignores the TP1 partial's own (usually better)
   price -- a known, PRE-EXISTING simplification in that older script,
   unrelated to today's entry-gate change, and not how live P&L actually
   works. Using it would make an apples-to-oranges comparison and hide the
   one number that actually matters here.

Both checks call time_window_filter.evaluate_time_window_entry directly
(via a thin adapter converting its MajorFlagDecision into the (approved,
info) shape scripts/tw_gate_relaxed_optimization.py's simulate() loop
already consumes) -- no gate logic is reimplemented, only the loop
scaffolding (T+3 timing, position ladder, forced liquidation, opposite-
signal handling) is reused unchanged from that script.
"""
from __future__ import annotations

import sys
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
import scripts.backtest_time_window_filter as prod_bt  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"


def evaluate_via_production(bars_3m, flag_direction, flag_bar_dt, decision_at, *,
                             position_direction=None, morning_entry_count=0, afternoon_entry_count=0, **_ignored):
    """Adapter: calls PRODUCTION's real evaluate_time_window_entry and
    reshapes its MajorFlagDecision into the (approved, info) tuple
    base.simulate()'s loop already expects -- no gate logic duplicated."""
    decision = base.twf.evaluate_time_window_entry(
        bars_3m, flag_direction, flag_bar_dt, decision_at,
        position_direction=position_direction,
        morning_entry_count=morning_entry_count, afternoon_entry_count=afternoon_entry_count,
    )
    if not decision.approved:
        return None, {"reject": decision.block_reason}
    return True, {
        "window": decision.metrics.get("window"),
        "session": decision.metrics.get("session") or base.twf.session_for_window(decision.metrics.get("window")),
        "quality_score": decision.score,
    }


def simulate_with_production_gate(date, hynix_bars_3m, flags, etf_close, *, start_idx=0):
    """Exact copy of base.simulate()'s loop scaffolding (T+3 timing,
    partial-leg-blended position ladder, forced liquidation, opposite-signal
    handling) with the ONLY change being which gate function decides entry:
    evaluate_via_production() above instead of base.evaluate_relaxed_entry()."""
    trades = []
    position = None
    flags_by_idx = dict(flags)
    pending = None
    morning_count = 0
    afternoon_count = 0
    daily_entry_seq = 0

    def position_direction():
        return base._direction_for_symbol(position.symbol) if position is not None else None

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()
        bar_ts = hynix_bars_3m["datetime"].iloc[idx]

        if position is not None and idx > position.entry_idx:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                net = base._net_pct(position.symbol, position.entry_price, close)
                if position.session == "MORNING":
                    pm = base.twpm.evaluate_morning_position(net_return_pct=net, tp1_done=position.tp1_done, peak_net_return=position.peak_net_return)
                else:
                    pm = base.twpm.evaluate_afternoon_position(net_return_pct=net, peak_net_return=position.peak_net_return)
                position.peak_net_return = pm.peak_net_return
                position.tp1_done = pm.tp1_done
                if pm.exit_reason is not None:
                    if pm.exit_reason == base.config.EXIT_TW_TP1_PARTIAL:
                        position.trade.tp1_hit = True
                        base._record_partial_leg(position, qty_fraction=pm.sell_fraction, price=close, reason=pm.exit_reason)
                    else:
                        if pm.exit_reason == base.config.EXIT_TW_TP2_FULL:
                            position.trade.tp2_hit = True
                        base._close_trade(position.trade, exit_time=bar_dt, exit_price=close, reason=pm.exit_reason, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                        trades.append(position.trade)
                        position = None

        if position is not None and bar_dt.astimezone(base.KST).time() >= base.config.FORCE_LIQUIDATE_AT:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                base._close_trade(position.trade, exit_time=bar_dt, exit_price=close, reason=base.config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                trades.append(position.trade)
                position = None
            pending = None

        if idx in flags_by_idx:
            pending = (flags_by_idx[idx], idx, bar_ts)

        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                decision_at = bar_dt + timedelta(minutes=3)
                approved, info = evaluate_via_production(
                    hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at,
                    position_direction=position_direction(), morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                if approved:
                    target = base._target_symbol(p_direction)
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position.symbol != target:
                            close_now = etf_close[position.symbol].get(bar_ts, position.entry_price)
                            base._close_trade(position.trade, exit_time=bar_dt, exit_price=close_now, reason=base.config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                            trades.append(position.trade)
                            position = None
                        if position is None:
                            session = info["session"]
                            if session == "MORNING":
                                morning_count += 1
                            else:
                                afternoon_count += 1
                            daily_entry_seq += 1
                            new_trade = base.Trade(
                                trading_date=date, direction=p_direction.value, flag_time=flag_bar_dt.isoformat(),
                                entry_time=bar_dt.isoformat(), entry_symbol=target, entry_price=fill,
                                window=info["window"], quality_score=info["quality_score"], flag_seq_of_day=daily_entry_seq,
                            )
                            position = base.OpenPosition(symbol=target, entry_idx=idx, entry_price=fill, entry_time=bar_dt, session=session, trade=new_trade)

    if position is not None:
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime()
        close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[-1], position.entry_price)
        base._close_trade(position.trade, exit_time=last_dt, exit_price=close, reason="END_OF_DATA", entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
        trades.append(position.trade)
    return trades


def run_period(cache):
    all_trades = []
    for day in cache:
        all_trades.extend(simulate_with_production_gate(day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"], start_idx=day["start_idx"]))
    return base.metrics(all_trades, len(cache))


KEYS_TO_COMPARE = [
    "total_entries", "avg_entries_per_day", "morning_entries", "afternoon_entries",
    "win_rate_pct", "total_simple_cumulative_return_pct", "compounded_cumulative_return_pct",
    "profit_factor", "max_drawdown_pct", "max_consecutive_losses",
]


if __name__ == "__main__":
    print(f"config.QUALITY_SCORE_THRESHOLD = {base.config.QUALITY_SCORE_THRESHOLD} (expect 2)")
    print(f"config.TW_MORNING_ONLY = {base.config.TW_MORNING_ONLY} (expect False)")
    print(f"config.TW_ALLOW_ENTRY_1050_1300 = {base.config.TW_ALLOW_ENTRY_1050_1300} (expect True)")
    assert base.config.QUALITY_SCORE_THRESHOLD == 2, "QUALITY_SCORE_THRESHOLD not reverted to 2"
    assert base.config.TW_MORNING_ONLY is False, "TW_MORNING_ONLY not reverted to False"

    print("\nLoading TRAIN/VAL/OOS day caches...")
    train_cache, _ = base._prepare_day_cache(base.TRAIN_DATES)
    val_cache, _ = base._prepare_day_cache(base.VAL_DATES)
    oos_cache, _ = base._prepare_day_cache(base.OOS_DATES)
    print(f"TRAIN={len(train_cache)}d VAL={len(val_cache)}d OOS={len(oos_cache)}d")

    print("\nRunning PRODUCTION's real time_window_filter.evaluate_time_window_entry() (blended P&L accounting)...")
    prod_results = {"TRAIN": run_period(train_cache), "VAL": run_period(val_cache), "OOS": run_period(oos_cache)}

    recorded = json.loads((OUTPUT_DIR / "baseline_vs_final_summary.json").read_text(encoding="utf-8"))["baseline"]

    print("\n=== Production (real evaluate_time_window_entry, blended P&L) vs recorded baseline ===")
    all_match = True
    mismatches = []
    for period in ("TRAIN", "VAL", "OOS"):
        prod_m, rec_m = prod_results[period], recorded[period]
        print(f"\n{period}:")
        for k in KEYS_TO_COMPARE:
            pv, rv = prod_m.get(k), rec_m.get(k)
            close = (pv == rv) if not isinstance(pv, float) else (rv is not None and abs(pv - rv) < 0.05)
            flag = "OK" if close else "MISMATCH"
            if not close:
                all_match = False
                mismatches.append((period, k, pv, rv))
            print(f"  {k:<38} production={pv!s:<12} recorded={rv!s:<12} [{flag}]")

    print(f"\n{'ALL MATCH -- production entry function + P&L now exactly reproduces the validated baseline.' if all_match else 'MISMATCHES FOUND -- see list below.'}")
    for m in mismatches:
        print("  MISMATCH:", m)

    (OUTPUT_DIR / "production_regression_check.json").write_text(
        json.dumps({"production": prod_results, "recorded_baseline": recorded, "all_match": all_match, "mismatches": mismatches}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nSaved -> {OUTPUT_DIR / 'production_regression_check.json'}")
    sys.exit(0 if all_match else 1)
