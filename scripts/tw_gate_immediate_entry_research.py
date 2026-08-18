#!/usr/bin/env python
"""2026-08-18 user request: the time-window filter currently waits a full
extra 3-minute bar after a flag confirms (T) before evaluating order
authority (T+3, requiring the MACD-Signal gap to have widened further) --
user's complaint: this makes entries too late and hurts win rate. This
script backtests the new evaluate_time_window_entry_immediate() (app/
trading/macd2/time_window_filter.py) -- enters right at the flag bar T's own
close, replacing the "gap widened one bar later" confirmation (structurally
unavailable before T+1 exists) with a "decisive cross" filter: |gap at T| /
ATR14 at T >= TW_IMMEDIATE_MIN_GAP_ATR_RATIO.

Sweeps that ATR-ratio threshold on TRAIN (34 days), confirms the winner on
VAL (11 days), then reports OOS (11 days) ONCE for the winning threshold plus
the current production baseline for comparison -- same TRAIN/VAL/OOS split
already established in tw_gate_relaxed_optimization.py, to avoid overfitting
to any one window (this repo's own prior threshold-tuning mistake, per
config.py's 2026-08-15 tuning-history comment).

Read-only research; entry TIME_WINDOW/quality-count/pyramiding gates and the
exit ladder (TP1/TP2/SL) are completely unchanged from current production --
only WHEN the entry decision is evaluated (T vs T+3) and HOW weak crosses are
filtered (gap-widened-next-bar vs decisive-cross-vs-ATR) are varied.
"""
from __future__ import annotations

import csv
import json
import sys
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
from app.trading.macd2 import config, time_window_filter as twf  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"

TRAIN_DATES = base.TRAIN_DATES
VAL_DATES = base.VAL_DATES
OOS_DATES = base.OOS_DATES

ATR_RATIO_SWEEP = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40]


@contextmanager
def atr_ratio_override(ratio):
    orig = config.TW_IMMEDIATE_MIN_GAP_ATR_RATIO
    try:
        config.TW_IMMEDIATE_MIN_GAP_ATR_RATIO = ratio
        yield
    finally:
        config.TW_IMMEDIATE_MIN_GAP_ATR_RATIO = orig


def evaluate_baseline(hynix_bars_3m, direction, flag_bar_dt, decision_at, *, position_direction, morning_entry_count, afternoon_entry_count):
    decision = twf.evaluate_time_window_entry(
        hynix_bars_3m, direction, flag_bar_dt, decision_at,
        position_direction=position_direction, morning_entry_count=morning_entry_count, afternoon_entry_count=afternoon_entry_count,
    )
    if not decision.approved:
        return None, {"reject": decision.block_reason}
    window = decision.metrics.get("window")
    return True, {"window": window, "session": decision.metrics.get("session") or twf.session_for_window(window), "quality_score": decision.score}


def evaluate_immediate(hynix_bars_3m, direction, flag_bar_dt, decision_at, *, position_direction, morning_entry_count, afternoon_entry_count):
    decision = twf.evaluate_time_window_entry_immediate(
        hynix_bars_3m, direction, flag_bar_dt, decision_at,
        position_direction=position_direction, morning_entry_count=morning_entry_count, afternoon_entry_count=afternoon_entry_count,
    )
    if not decision.approved:
        return None, {"reject": decision.block_reason}
    window = decision.metrics.get("window")
    return True, {"window": window, "session": decision.metrics.get("session") or twf.session_for_window(window), "quality_score": decision.score}


def simulate(date, hynix_bars_3m, flags, etf_close, *, immediate: bool, start_idx=0):
    """immediate=False replays current production timing (resolve at T+1,
    baseline evaluator). immediate=True resolves AT the flag bar T itself
    (no pending wait), using evaluate_time_window_entry_immediate. Exit
    ladder/forced-liquidation/reversal mechanics are identical in both."""
    trades = []
    position = None
    flags_by_idx = dict(flags)
    pending = None if not immediate else None
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
                    if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                        position.trade.tp1_hit = True
                        base._record_partial_leg(position, qty_fraction=pm.sell_fraction, price=close, reason=pm.exit_reason)
                    else:
                        if pm.exit_reason == config.EXIT_TW_TP2_FULL:
                            position.trade.tp2_hit = True
                        base._close_trade(position.trade, exit_time=bar_dt, exit_price=close, reason=pm.exit_reason, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                        trades.append(position.trade)
                        position = None

        if position is not None and bar_dt.astimezone(base.KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                base._close_trade(position.trade, exit_time=bar_dt, exit_price=close, reason=config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                trades.append(position.trade)
                position = None
            pending = None

        if idx in flags_by_idx:
            pending = (flags_by_idx[idx], idx, bar_ts)

        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            resolve_now = (idx == p_idx) if immediate else (idx == p_idx + 1)
            if resolve_now:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                decision_at = bar_dt + pd.Timedelta(minutes=3)
                evaluator = evaluate_immediate if immediate else evaluate_baseline
                approved, info = evaluator(
                    hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at,
                    position_direction=position_direction(), morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                if approved:
                    target = base._target_symbol(p_direction)
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position.symbol != target:
                            close_now = etf_close[position.symbol].get(bar_ts, position.entry_price)
                            base._close_trade(position.trade, exit_time=bar_dt, exit_price=close_now, reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
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


def run_over_cache(cache, *, immediate: bool):
    all_trades = []
    for day in cache:
        all_trades.extend(simulate(day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"], immediate=immediate, start_idx=day["start_idx"]))
    return all_trades


if __name__ == "__main__":
    print(f"Current production entry gate: QUALITY_SCORE_THRESHOLD={config.QUALITY_SCORE_THRESHOLD} TW_MORNING_ONLY={config.TW_MORNING_ONLY} MIN_FLAG_INTERVAL={config.MIN_FLAG_INTERVAL_MINUTES}min")

    print(f"\nLoading TRAIN({len(TRAIN_DATES)}) / VAL({len(VAL_DATES)}) / OOS({len(OOS_DATES)}) day caches...")
    train_cache, tnotes = base._prepare_day_cache(TRAIN_DATES)
    val_cache, vnotes = base._prepare_day_cache(VAL_DATES)
    oos_cache, onotes = base._prepare_day_cache(OOS_DATES)
    print(f"train days={len(train_cache)} val days={len(val_cache)} oos days={len(oos_cache)}")
    for n in tnotes + vnotes + onotes:
        print("  note:", n)

    print("\n=== Baseline (current production, T+3 delayed confirmation) ===")
    base_train = run_over_cache(train_cache, immediate=False)
    m_base_train = base.metrics(base_train, len(train_cache))
    print(f"  TRAIN: n={m_base_train['total_entries']} win={m_base_train['win_rate_pct']}% simple={m_base_train['total_simple_cumulative_return_pct']}% compounded={m_base_train['compounded_cumulative_return_pct']}% PF={m_base_train['profit_factor']} MDD={m_base_train['max_drawdown_pct']}")

    print("\n=== Immediate-entry ATR-ratio sweep on TRAIN ===")
    sweep_results = {}
    for ratio in ATR_RATIO_SWEEP:
        with atr_ratio_override(ratio):
            trades = run_over_cache(train_cache, immediate=True)
        m = base.metrics(trades, len(train_cache))
        sweep_results[ratio] = m
        print(f"  ratio={ratio:<5} n={m['total_entries']:>3} win={m['win_rate_pct']:>6}% simple={m['total_simple_cumulative_return_pct']:>8}% compounded={m['compounded_cumulative_return_pct']:>8}% PF={str(m['profit_factor']):>6} MDD={m['max_drawdown_pct']:>6} maxConsecLoss={m['max_consecutive_losses']}")

    def rank_key(item):
        ratio, m = item
        if m["total_simple_cumulative_return_pct"] <= 0:
            return (-1, 0, 0)
        return (1, m["profit_factor"] if isinstance(m["profit_factor"], (int, float)) else 0, -m["max_drawdown_pct"])

    best_ratio, best_train_m = max(sweep_results.items(), key=rank_key)
    print(f"\nBest TRAIN ratio (rank by PF, then lower MDD, positive-return only): {best_ratio}")

    print(f"\n=== Confirm winner (ratio={best_ratio}) on VAL, vs baseline ===")
    base_val = run_over_cache(val_cache, immediate=False)
    m_base_val = base.metrics(base_val, len(val_cache))
    with atr_ratio_override(best_ratio):
        imm_val = run_over_cache(val_cache, immediate=True)
    m_imm_val = base.metrics(imm_val, len(val_cache))
    print(f"  baseline VAL: n={m_base_val['total_entries']} win={m_base_val['win_rate_pct']}% simple={m_base_val['total_simple_cumulative_return_pct']}% compounded={m_base_val['compounded_cumulative_return_pct']}% PF={m_base_val['profit_factor']} MDD={m_base_val['max_drawdown_pct']}")
    print(f"  immediate VAL: n={m_imm_val['total_entries']} win={m_imm_val['win_rate_pct']}% simple={m_imm_val['total_simple_cumulative_return_pct']}% compounded={m_imm_val['compounded_cumulative_return_pct']}% PF={m_imm_val['profit_factor']} MDD={m_imm_val['max_drawdown_pct']}")

    print(f"\n=== FINAL OOS (ratio={best_ratio}, run ONCE) ===")
    base_oos = run_over_cache(oos_cache, immediate=False)
    m_base_oos = base.metrics(base_oos, len(oos_cache))
    with atr_ratio_override(best_ratio):
        imm_oos = run_over_cache(oos_cache, immediate=True)
    m_imm_oos = base.metrics(imm_oos, len(oos_cache))
    print(f"  baseline OOS: n={m_base_oos['total_entries']} win={m_base_oos['win_rate_pct']}% simple={m_base_oos['total_simple_cumulative_return_pct']}% compounded={m_base_oos['compounded_cumulative_return_pct']}% PF={m_base_oos['profit_factor']} MDD={m_base_oos['max_drawdown_pct']}")
    print(f"  immediate OOS: n={m_imm_oos['total_entries']} win={m_imm_oos['win_rate_pct']}% simple={m_imm_oos['total_simple_cumulative_return_pct']}% compounded={m_imm_oos['compounded_cumulative_return_pct']}% PF={m_imm_oos['profit_factor']} MDD={m_imm_oos['max_drawdown_pct']}")

    assert config.TW_IMMEDIATE_MIN_GAP_ATR_RATIO == 0.10, "config leaked!"

    # also report naive immediate entry (ratio=0.0, i.e. no decisive-cross filter
    # at all beyond the structural gates) on TRAIN+VAL+OOS combined, since the
    # user specifically suspected "enter immediately with no filter" would hurt
    # win rate -- this confirms/refutes that directly.
    full_cache = train_cache + val_cache + oos_cache
    with atr_ratio_override(0.0):
        naive_trades = run_over_cache(full_cache, immediate=True)
    m_naive = base.metrics(naive_trades, len(full_cache))
    base_full = run_over_cache(full_cache, immediate=False)
    m_base_full = base.metrics(base_full, len(full_cache))
    with atr_ratio_override(best_ratio):
        best_full = run_over_cache(full_cache, immediate=True)
    m_best_full = base.metrics(best_full, len(full_cache))

    print("\n=== Full 56-day summary (TRAIN+VAL+OOS combined) ===")
    print(f"  1) baseline (T+3 delayed):            n={m_base_full['total_entries']} win={m_base_full['win_rate_pct']}% simple={m_base_full['total_simple_cumulative_return_pct']}% compounded={m_base_full['compounded_cumulative_return_pct']}% PF={m_base_full['profit_factor']} MDD={m_base_full['max_drawdown_pct']} maxConsecLoss={m_base_full['max_consecutive_losses']}")
    print(f"  2) immediate, NO decisive-cross filter (ratio=0.0): n={m_naive['total_entries']} win={m_naive['win_rate_pct']}% simple={m_naive['total_simple_cumulative_return_pct']}% compounded={m_naive['compounded_cumulative_return_pct']}% PF={m_naive['profit_factor']} MDD={m_naive['max_drawdown_pct']} maxConsecLoss={m_naive['max_consecutive_losses']}")
    print(f"  3) immediate, ratio={best_ratio} (TRAIN-selected): n={m_best_full['total_entries']} win={m_best_full['win_rate_pct']}% simple={m_best_full['total_simple_cumulative_return_pct']}% compounded={m_best_full['compounded_cumulative_return_pct']}% PF={m_best_full['profit_factor']} MDD={m_best_full['max_drawdown_pct']} maxConsecLoss={m_best_full['max_consecutive_losses']}")

    summary = {
        "train_dates": TRAIN_DATES, "val_dates": VAL_DATES, "oos_dates": OOS_DATES,
        "atr_ratio_sweep_train": {str(k): v for k, v in sweep_results.items()},
        "best_ratio": best_ratio,
        "val": {"baseline": m_base_val, "immediate": m_imm_val},
        "oos": {"baseline": m_base_oos, "immediate": m_imm_oos},
        "full_56d": {"baseline": m_base_full, "immediate_no_filter": m_naive, "immediate_best": m_best_full},
    }
    (OUTPUT_DIR / "immediate_entry_research_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    print(f"\nSaved summary -> {OUTPUT_DIR / 'immediate_entry_research_summary.json'}")
