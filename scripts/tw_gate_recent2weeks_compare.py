#!/usr/bin/env python
"""2026-08-18 user request: compare, on the most recent ~2 trading weeks of
real 하이닉스/레버리지/인버스 data (2026-07-31~2026-08-14, 11 trading days --
same window already cached as this session's FINAL OOS split), two versions
of the "시간대별 최적거래 필터":

  1) CURRENT committed HEAD (commit 8591568) -- "게이트 전체 완화": quality_
     score>=2 uniformly across all windows (W1-W6), TW_MORNING_ONLY=False
     (afternoon entries allowed).
  2) The PREVIOUSLY committed version (commit 8591568^ == b914734 ==
     3ae64bc's code, unchanged since) -- "13시까지만 거래": quality_score
     threshold 4 with the old per-window special cases (W1 exempt, W2
     reset-only quality check skipped, W6 EMA-only), TW_MORNING_ONLY=True
     (no new entries after 13:00; existing positions still manage/exit
     normally in the afternoon).

Both scenarios reuse the EXACT SAME simulation scaffolding (T+3 confirmation
timing, TradeCostEngine-based real ETF cost/price accounting, quantity-
weighted partial-TP blending) -- scenario 1 calls the current in-repo
time_window_filter.evaluate_time_window_entry directly; scenario 2 loads
the git-historical time_window_filter.py as a standalone module (so its own
now-removed per-window branches run exactly as they did when committed) and
temporarily monkeypatches config.QUALITY_SCORE_THRESHOLD/TW_MORNING_ONLY to
their old values only for the duration of that one call (restored
immediately after, verified via assertion). No production files are
modified; this is a read-only comparison.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"
SCRATCH_DIR = Path(r"C:\Users\KIMHYU~1\AppData\Local\Temp\claude\G----------------2--Desktop-AI-GAP-2\fa28d346-f8d0-4336-a183-1cf8c29b11db\scratchpad\old_prod")

RECENT_2W_DATES = ["20260731", "20260803", "20260804", "20260805", "20260806", "20260807",
                    "20260810", "20260811", "20260812", "20260813", "20260814"]

OLD_COMMIT = "8591568^"  # last commit BEFORE today's baseline-confirmation production change


def load_old_time_window_filter():
    src_path = SCRATCH_DIR / "time_window_filter_old.py"
    if not src_path.exists():
        content = subprocess.check_output(["git", "show", f"{OLD_COMMIT}:app/trading/macd2/time_window_filter.py"], cwd=PROJECT_ROOT)
        src_path.write_bytes(content)
    spec = importlib.util.spec_from_file_location("old_time_window_filter", src_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OLD_TWF = load_old_time_window_filter()


@contextmanager
def old_config_values():
    orig_threshold = base.config.QUALITY_SCORE_THRESHOLD
    orig_morning_only = base.config.TW_MORNING_ONLY
    try:
        base.config.QUALITY_SCORE_THRESHOLD = 4
        base.config.TW_MORNING_ONLY = True
        yield
    finally:
        base.config.QUALITY_SCORE_THRESHOLD = orig_threshold
        base.config.TW_MORNING_ONLY = orig_morning_only


def evaluate_via(twf_module, bars_3m, flag_direction, flag_bar_dt, decision_at, *,
                  position_direction=None, morning_entry_count=0, afternoon_entry_count=0):
    decision = twf_module.evaluate_time_window_entry(
        bars_3m, flag_direction, flag_bar_dt, decision_at,
        position_direction=position_direction,
        morning_entry_count=morning_entry_count, afternoon_entry_count=afternoon_entry_count,
    )
    if not decision.approved:
        return None, {"reject": decision.block_reason}
    window = decision.metrics.get("window")
    return True, {
        "window": window,
        "session": decision.metrics.get("session") or base.twf.session_for_window(window),
        "quality_score": decision.score,
    }


def simulate_scenario(date, hynix_bars_3m, flags, etf_close, *, twf_module, start_idx=0):
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
                approved, info = evaluate_via(
                    twf_module, hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at,
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


def run_scenario(cache, *, twf_module, use_old_config):
    all_trades = []
    if use_old_config:
        with old_config_values():
            for day in cache:
                all_trades.extend(simulate_scenario(day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"], twf_module=twf_module, start_idx=day["start_idx"]))
    else:
        for day in cache:
            all_trades.extend(simulate_scenario(day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"], twf_module=twf_module, start_idx=day["start_idx"]))
    return all_trades


if __name__ == "__main__":
    print(f"Current config: QUALITY_SCORE_THRESHOLD={base.config.QUALITY_SCORE_THRESHOLD} TW_MORNING_ONLY={base.config.TW_MORNING_ONLY}")
    print("Loading recent-2-weeks day cache (2026-07-31 ~ 2026-08-14, 11 trading days)...")
    cache, notes = base._prepare_day_cache(RECENT_2W_DATES)
    print(f"days={len(cache)}")
    for n in notes:
        print("  note:", n)

    print("\n[1/2] Running CURRENT committed HEAD (게이트 전체 완화, quality>=2, all-day)...")
    trades_current = run_scenario(cache, twf_module=base.twf, use_old_config=False)
    m_current = base.metrics(trades_current, len(cache))
    print(f"Verify config unchanged after run: threshold={base.config.QUALITY_SCORE_THRESHOLD} morning_only={base.config.TW_MORNING_ONLY}")

    print("\n[2/2] Running PREVIOUSLY committed version (13시까지만, quality>=4 + 창별특례, morning-only)...")
    trades_old = run_scenario(cache, twf_module=OLD_TWF, use_old_config=True)
    m_old = base.metrics(trades_old, len(cache))
    print(f"Verify config restored after run: threshold={base.config.QUALITY_SCORE_THRESHOLD} morning_only={base.config.TW_MORNING_ONLY}")
    assert base.config.QUALITY_SCORE_THRESHOLD == 2 and base.config.TW_MORNING_ONLY is False, "config leaked!"

    def show(label, m):
        print(f"\n=== {label} ===")
        print(f"  n={m['total_entries']} entries/day={m['avg_entries_per_day']} win={m['win_rate_pct']}% "
              f"avg_daily={m['avg_daily_return_pct']}% simple={m['total_simple_cumulative_return_pct']}% "
              f"compounded={m['compounded_cumulative_return_pct']}% PF={m['profit_factor']} MDD={m['max_drawdown_pct']} "
              f"maxConsecLoss={m['max_consecutive_losses']} AM/PM={m['morning_entries']}/{m['afternoon_entries']}")

    show("1) 게이트 전체 완화 (CURRENT, commit 8591568)", m_current)
    show("2) 13시까지만 거래 (PREVIOUS, commit 8591568^)", m_old)

    def trade_rows(trades, label):
        rows = []
        for t in sorted(trades, key=lambda x: x.entry_time):
            rows.append({
                "scenario": label, "date": t.trading_date, "direction": "LONG" if t.direction == "UP_RED" else "INV",
                "symbol": t.entry_symbol, "window": t.window, "quality_score": t.quality_score,
                "entry_time": t.entry_time, "entry_price": t.entry_price,
                "exit_time": t.exit_time, "exit_price": t.exit_price, "exit_reason": t.exit_reason,
                "net_return_pct": round(t.net_return_pct, 3) if t.net_return_pct is not None else None,
                "legs": json.dumps(t.legs, ensure_ascii=False),
            })
        return rows

    all_rows = trade_rows(trades_current, "1_게이트전체완화") + trade_rows(trades_old, "2_13시까지만")
    all_rows.sort(key=lambda r: (r["scenario"], r["entry_time"]))

    csv_path = OUTPUT_DIR / "recent2weeks_compare_all_trades.csv"
    fieldnames = ["scenario", "date", "direction", "symbol", "window", "quality_score", "entry_time", "entry_price",
                  "exit_time", "exit_price", "exit_reason", "net_return_pct", "legs"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in all_rows:
            w.writerow(row)
    print(f"\nSaved {len(all_rows)} trades -> {csv_path}")

    summary = {
        "period": {"dates": RECENT_2W_DATES, "trading_days": len(cache)},
        "scenario_1_gate_relaxed_current": m_current,
        "scenario_2_morning_only_previous": m_old,
    }
    (OUTPUT_DIR / "recent2weeks_compare_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Saved summary -> {OUTPUT_DIR / 'recent2weeks_compare_summary.json'}")
