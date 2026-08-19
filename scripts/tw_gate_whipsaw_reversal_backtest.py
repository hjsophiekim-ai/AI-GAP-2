#!/usr/bin/env python
"""Canonical TRAIN/VAL/OOS backtest for MACD2's "휩쏘-내성" T+3 반대신호
재확인 (2026-08-19 production feature -- app/trading/macd2/config.py's
TW_WHIPSAW_REJECT_REASONS + worker.py's _resolve_time_window_candidate whipsaw
branch). Kept as a permanent, non-throwaway script (unlike the various
scripts/_tmp_*.py research scripts from the same investigation) because this
IS the parity reference for that production change going forward -- re-run
this after any future change to time_window_filter.evaluate_time_window_entry
or the SL/TP1/TP2/trailing ladder to confirm the backtest and worker.py still
agree.

Ordering fidelity (2026-08-19 fix -- this was a real bug in the very first
version of this research): a fresh confirmed flag landing on the SAME bar an
existing pending candidate is due to resolve used to silently overwrite the
pending slot BEFORE the old candidate's resolution check ran, dropping it
entirely. Real app/trading/macd2/worker.py always resolves an EXISTING
pending candidate FIRST (_resolve_time_window_candidate, called at both
worker.py's held-position branch and its flat/new-entry branch) and only
registers a fresh flag as the new pending candidate afterward -- and only
when nothing executed (switch/sell-only) this same tick, since an executed
outcome causes an early `return result` in worker.py that skips new-flag
registration for that tick. simulate_variant() below mirrors that exact order
so a day's candidate sequence here is never a subset/superset of what the
real worker would have seen.

A) predicate_a — 현재(즉시 반대신호 청산): a rejected reversal ALWAYS sells,
   regardless of reject reason (pre-2026-08-19 production behavior).
B) predicate_b — 휩쏘-내성(T+3 재확인, 2026-08-19 production behavior): a
   rejected reversal sells UNLESS decision.block_reason is exactly
   config.TW_WHIPSAW_REJECT_REASONS (imported straight from production
   config, never redefined here) -- MACD/Signal relationship didn't hold 3
   minutes later, or the gap didn't expand -- in which case the position is
   left completely untouched (WHIPSAW_HOLD). No gap-magnitude or time-of-day
   threshold of any kind (2026-08-19 사용자 요청: 그런 추가 조건은 넣지 않음
   -- a 2026-08-19 gap/time concentration analysis found no consistent
   evidence supporting one; see chat history / scripts/_tmp_option_b_
   corrected_and_gap_analysis.py for that exploratory work).

Both variants use the exact same time_window_filter.evaluate_time_window_entry
call and the exact same config.TW_WHIPSAW_REJECT_REASONS constant the real
worker.py uses -- no duplicated classification logic. SL(-1.7%)/TP1/TP2/
trailing-stop/15:00 forced liquidation are completely untouched by either
variant (handled entirely by time_window_position_manager.evaluate_morning_
position/evaluate_afternoon_position, called unconditionally every bar).

Strictly read-only research; never touches production state/ledger.
"""
from __future__ import annotations

import csv
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base
from app.trading.macd2 import config, time_window_filter as twf

KST = base.KST
WHIPSAW_REASONS = config.TW_WHIPSAW_REJECT_REASONS  # shared straight from production config
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def simulate_variant(date, hynix_bars_3m, flags, etf_close, *, hold_predicate, start_idx: int = 0):
    """CORRECTED ordering (see module docstring): resolve any existing
    pending candidate FIRST; only register a fresh confirmed flag as the new
    pending candidate if nothing executed (switch/sell-only) this same tick.
    ``hold_predicate(approved, block_reason) -> bool`` decides, for a
    REVERSAL candidate only (opposite direction vs a currently-held
    position), whether to hold instead of acting on the gate's decision.
    """
    trades: list[base.Trade] = []
    position: Optional[base.OpenPosition] = None
    flags_by_idx = dict(flags)
    pending = None
    morning_count = 0
    afternoon_count = 0
    daily_entry_seq = 0
    whipsaw_holds = 0

    def position_direction():
        return base._direction_for_symbol(position.symbol) if position is not None else None

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()
        bar_ts = hynix_bars_3m["datetime"].iloc[idx]

        # 1) position management ladder (TP1/TP2/SL -- unaffected by this feature)
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

        # 2) forced liquidation at/after 15:00
        if position is not None and bar_dt.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                base._close_trade(position.trade, exit_time=bar_dt, exit_price=close, reason=config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                trades.append(position.trade)
                position = None
            pending = None

        executed_this_tick = False

        # 3) RESOLVE an existing pending candidate FIRST
        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                decision_at = bar_dt + timedelta(minutes=3)
                decision = twf.evaluate_time_window_entry(
                    hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at,
                    position_direction=position_direction(), morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                approved = bool(decision.approved)
                block_reason = decision.block_reason or ""
                is_reversal = position is not None and base._target_symbol(p_direction) != position.symbol

                do_hold = is_reversal and hold_predicate(approved=approved, block_reason=block_reason)
                if do_hold:
                    whipsaw_holds += 1
                    pass  # leave the held position completely untouched
                elif approved:
                    target = base._target_symbol(p_direction)
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position.symbol != target:
                            close_now = etf_close[position.symbol].get(bar_ts, position.entry_price)
                            base._close_trade(position.trade, exit_time=bar_dt, exit_price=close_now, reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                            trades.append(position.trade)
                            position = None
                            executed_this_tick = True
                        if position is None:
                            session = (decision.metrics or {}).get("session") or twf.session_for_window((decision.metrics or {}).get("window"))
                            if session == "MORNING":
                                morning_count += 1
                            else:
                                afternoon_count += 1
                            daily_entry_seq += 1
                            new_trade = base.Trade(
                                trading_date=date, direction=p_direction.value, flag_time=flag_bar_dt.isoformat(),
                                entry_time=bar_dt.isoformat(), entry_symbol=target, entry_price=fill,
                                window=(decision.metrics or {}).get("window"), quality_score=decision.score, flag_seq_of_day=daily_entry_seq,
                            )
                            position = base.OpenPosition(symbol=target, entry_idx=idx, entry_price=fill, entry_time=bar_dt, session=session, trade=new_trade)
                            executed_this_tick = True
                else:
                    if position is not None and base._target_symbol(p_direction) != position.symbol:
                        close_now = etf_close[position.symbol].get(bar_ts, position.entry_price)
                        base._close_trade(position.trade, exit_time=bar_dt, exit_price=close_now, reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                        trades.append(position.trade)
                        position = None
                        executed_this_tick = True
                    # else: same-direction / no-position rejection -- no-op (matches FILTERED_OUT)

        # 4) THEN register a fresh confirmed flag as the new pending candidate
        # -- but only if nothing executed this same tick.
        if not executed_this_tick and idx in flags_by_idx:
            pending = (flags_by_idx[idx], idx, bar_ts)

    if position is not None:
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime()
        close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[-1], position.entry_price)
        base._close_trade(position.trade, exit_time=last_dt, exit_price=close, reason="END_OF_DATA", entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
        trades.append(position.trade)
    return trades, whipsaw_holds


def run_over_cache(cache, *, hold_predicate):
    all_trades = []
    total_whipsaw_holds = 0
    for day in cache:
        trades, holds = simulate_variant(
            day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"],
            hold_predicate=hold_predicate, start_idx=day["start_idx"],
        )
        all_trades.extend(trades)
        total_whipsaw_holds += holds
    return all_trades, total_whipsaw_holds


def predicate_a(**kw):
    return False


def predicate_b(*, approved, block_reason):
    return (not approved) and (block_reason in WHIPSAW_REASONS)


def _write_trades_csv(path: Path, trades: list[base.Trade]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["trading_date", "direction", "flag_time", "entry_time", "entry_symbol", "entry_price",
                    "window", "quality_score", "tp1_hit", "tp2_hit", "exit_time", "exit_price", "exit_reason",
                    "net_return_pct"])
        for t in trades:
            w.writerow([t.trading_date, t.direction, t.flag_time, t.entry_time, t.entry_symbol, t.entry_price,
                        t.window, t.quality_score, t.tp1_hit, t.tp2_hit, t.exit_time, t.exit_price, t.exit_reason,
                        round(t.net_return_pct, 4) if t.net_return_pct is not None else None])


def print_metrics_row(label, m):
    print(f"  {label:<34} n={m['total_entries']:>3} win={m['win_rate_pct']:>6}% compounded={m['compounded_cumulative_return_pct']:>8}% PF={str(m['profit_factor']):>6} MDD={m['max_drawdown_pct']:>6}")


if __name__ == "__main__":
    print("Loading TRAIN/VAL/OOS day caches...")
    cache, notes = base._prepare_day_cache(base.FULL_DATES)
    print(f"days loaded: {len(cache)}")
    for n in notes:
        print("  note:", n)
    day_by_date = {d["date"]: d for d in cache}

    for label, dates in [("TRAIN", base.TRAIN_DATES), ("VAL", base.VAL_DATES), ("OOS", base.OOS_DATES)]:
        split_cache = [day_by_date[d] for d in dates if d in day_by_date]

        trades_a, _ = run_over_cache(split_cache, hold_predicate=predicate_a)
        m_a = base.metrics(trades_a, len(split_cache))

        trades_b, whipsaw_holds = run_over_cache(split_cache, hold_predicate=predicate_b)
        m_b = base.metrics(trades_b, len(split_cache))

        print(f"\n=== {label} ({len(split_cache)} days) ===")
        print_metrics_row("A) 현재(즉시 반대신호 청산):", m_a)
        print_metrics_row("B) 휩쏘-내성(T+3 재확인, PRODUCTION):", m_b)
        print(f"  whipsaw HOLD: {whipsaw_holds}건")

        _write_trades_csv(OUTPUT_DIR / f"whipsaw_backtest_trades_A_{label}.csv", trades_a)
        _write_trades_csv(OUTPUT_DIR / f"whipsaw_backtest_trades_B_{label}.csv", trades_b)

    print(f"\nCSV 저장 -> {OUTPUT_DIR}")
