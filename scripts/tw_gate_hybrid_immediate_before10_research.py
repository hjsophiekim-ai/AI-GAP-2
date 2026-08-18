#!/usr/bin/env python
"""2026-08-18 user request: instead of either "always wait T+3" (current
production) or "always enter immediately at T" (previous research, which
regressed on FINAL OOS), try a hybrid: 09:00-09:59 flags enter immediately
at their own flag bar T (using evaluate_time_window_entry_immediate, no
extra decisive-cross filter -- TRAIN showed that filter only hurts), while
flags confirmed at/after 10:00 keep the current T+3 delayed re-confirmation
(evaluate_time_window_entry, unchanged). The 09:00-10:00 window is exactly
W1(09:00-09:45)+W2(09:45-10:20's first 15min) -- the earliest, most
aggressive entries where the extra 3-minute wait costs the most relative
move; the boundary is a plain clock-time cut at 10:00, independent of the
W1-W6 classification (which still governs quality-score thresholds/reset
rules for every flag exactly as before -- only WHEN the decision is
evaluated changes, never the gates themselves).

Reuses scripts/tw_gate_immediate_entry_research.py's evaluate_baseline/
evaluate_immediate (same production decision functions, not reimplemented)
and scripts/tw_gate_relaxed_optimization.py's TRAIN(34d)/VAL(11d)/OOS(11d)
split + cost-engine simulation scaffolding. Read-only research; no
production files touched (time_window_filter.py's two decision functions
already exist from the prior immediate-entry research, both pure/unchanged
here).
"""
from __future__ import annotations

import json
import sys
from datetime import time as dtime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
import scripts.tw_gate_immediate_entry_research as imm  # noqa: E402
from app.trading.macd2 import config  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"

TRAIN_DATES = base.TRAIN_DATES
VAL_DATES = base.VAL_DATES
OOS_DATES = base.OOS_DATES
FULL_DATES = base.FULL_DATES

IMMEDIATE_CUTOFF = dtime(10, 0)  # flags confirmed before this enter immediately; at/after this, T+3 as before


def simulate_hybrid(date, hynix_bars_3m, flags, etf_close, *, start_idx=0):
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
            flag_time = bar_ts.astimezone(base.KST).time() if hasattr(bar_ts, "astimezone") else pd.Timestamp(bar_ts).tz_convert(base.KST).time()
            use_immediate = flag_time < IMMEDIATE_CUTOFF
            pending = (flags_by_idx[idx], idx, bar_ts, use_immediate)

        if pending is not None:
            p_direction, p_idx, p_bar_ts, use_immediate = pending
            resolve_now = (idx == p_idx) if use_immediate else (idx == p_idx + 1)
            if resolve_now:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                decision_at = bar_dt + pd.Timedelta(minutes=3)
                evaluator = imm.evaluate_immediate if use_immediate else imm.evaluate_baseline
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


def run_hybrid_over_cache(cache):
    all_trades = []
    for day in cache:
        all_trades.extend(simulate_hybrid(day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"], start_idx=day["start_idx"]))
    return all_trades


def show(label, m):
    print(f"  {label}: n={m['total_entries']} entries/day={m['avg_entries_per_day']} win={m['win_rate_pct']}% "
          f"simple={m['total_simple_cumulative_return_pct']}% compounded={m['compounded_cumulative_return_pct']}% "
          f"PF={m['profit_factor']} MDD={m['max_drawdown_pct']} maxConsecLoss={m['max_consecutive_losses']}")


if __name__ == "__main__":
    assert config.TW_IMMEDIATE_MIN_GAP_ATR_RATIO == 0.0, "expected the validated no-filter default"
    print(f"Hybrid rule: flags confirmed before {IMMEDIATE_CUTOFF} -> immediate entry (no extra filter); at/after -> current T+3 delayed re-confirmation")

    print(f"\nLoading TRAIN({len(TRAIN_DATES)}) / VAL({len(VAL_DATES)}) / OOS({len(OOS_DATES)}) day caches...")
    train_cache, tnotes = base._prepare_day_cache(TRAIN_DATES)
    val_cache, vnotes = base._prepare_day_cache(VAL_DATES)
    oos_cache, onotes = base._prepare_day_cache(OOS_DATES)
    full_cache, fnotes = base._prepare_day_cache(FULL_DATES)
    for n in tnotes + vnotes + onotes:
        print("  note:", n)

    print("\n=== Baseline (current production, always T+3) ===")
    m_base = {}
    for label, cache in (("TRAIN", train_cache), ("VAL", val_cache), ("OOS", oos_cache), ("FULL_56D", full_cache)):
        trades = imm.run_over_cache(cache, immediate=False)
        m_base[label] = base.metrics(trades, len(cache))
        show(label, m_base[label])

    print("\n=== Hybrid (09:00-09:59 immediate / 10:00+ T+3 as before) ===")
    m_hybrid = {}
    for label, cache in (("TRAIN", train_cache), ("VAL", val_cache), ("OOS", oos_cache), ("FULL_56D", full_cache)):
        trades = run_hybrid_over_cache(cache)
        m_hybrid[label] = base.metrics(trades, len(cache))
        show(label, m_hybrid[label])

    print("\n=== Side-by-side ===")
    for label in ("TRAIN", "VAL", "OOS", "FULL_56D"):
        b, h = m_base[label], m_hybrid[label]
        print(f"  [{label}] baseline: win={b['win_rate_pct']}% compounded={b['compounded_cumulative_return_pct']}% PF={b['profit_factor']} MDD={b['max_drawdown_pct']}  |  "
              f"hybrid: win={h['win_rate_pct']}% compounded={h['compounded_cumulative_return_pct']}% PF={h['profit_factor']} MDD={h['max_drawdown_pct']}")

    summary = {
        "immediate_cutoff": str(IMMEDIATE_CUTOFF), "train_dates": TRAIN_DATES, "val_dates": VAL_DATES, "oos_dates": OOS_DATES,
        "baseline": m_base, "hybrid": m_hybrid,
    }
    (OUTPUT_DIR / "hybrid_immediate_before10_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    print(f"\nSaved summary -> {OUTPUT_DIR / 'hybrid_immediate_before10_summary.json'}")
