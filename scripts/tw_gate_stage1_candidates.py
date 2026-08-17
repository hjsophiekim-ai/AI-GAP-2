#!/usr/bin/env python
"""2026-08-17 stage 1 (entry-condition) candidate test, driven by the
window x direction x quality x flag_seq x entry_seq breakdown produced by
scripts/tw_gate_entry_breakdown.py. TP/SL untouched -- current production
ladder (MORNING_TP1/TP2/STOP_LOSS, AFTERNOON_TP/STOP_LOSS module-level
constants) stays exactly as-is throughout this file; only the entry gate
(EntryParams) varies across candidates. Read-only research; no production
code touched.

Data-driven findings from the breakdown (bad in BOTH TRAIN and VAL, not just
VAL -- see data/validation/tw_gate_relaxed_optimization/entry_breakdown_train_val.json):
  - quality_score == 2 is bad in both TRAIN (cum -3.84%, PF 0.87) and VAL
    (cum -3.36%, PF 0.64) -> raise quality_threshold 2 -> 3.
  - direction UP_RED (long leverage) is bad in both TRAIN (cum -7.09%, PF
    0.91) and VAL (cum -5.17%, PF 0.74) while DOWN_BLUE (inverse) is good in
    both (TRAIN +14.68% PF 1.24, VAL +15.78% PF 2.23) -> apply a +1 quality
    bonus requirement to UP_RED only (asymmetric leverage/inverse entry bar).
  - flag_seq_of_day >= 5 (the day's 5th+ raw MACD crossover, i.e. an already-
    choppy day) is bad in both TRAIN (cum -10.08%/-1.26%, PF 0.43/0.77) and
    VAL (cum -3.69%/-3.52%, PF 0.07/0.00) -> cap max_flag_seq_of_day = 4.
  - entry_seq_of_day == 4 (the FIRST afternoon slot on days that already
    used all 3 morning slots) is bad in both TRAIN (cum -14.90%, PF 0.51)
    and VAL (cum -4.21%, PF 0.46) while entry_seq 5 (the day's second
    afternoon slot) is fine in both -> exclude entry_seq_of_day == 4
    specifically (not a blunt max_daily cut, which would also remove the
    good entry_seq==5 trades).
Explicitly NOT removed despite one side looking bad, because the OTHER side
disagreed (would be VAL-only post-hoc overfitting): W5_EARLY_AFTERNOON_A_GRADE
window (TRAIN bad / VAL good), quality_score 4 and 5 (mixed sign across
TRAIN/VAL), flag_seq 1 and 3 (mixed sign).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
import pandas as pd  # noqa: E402
from datetime import timedelta  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"


@dataclass
class ExtParams:
    label: str
    base_params: base.EntryParams
    max_flag_seq_of_day: Optional[int] = None
    excluded_entry_seq_of_day: tuple = ()


def simulate_ext(date, hynix_bars_3m, flags, etf_close, *, ext: ExtParams, start_idx: int = 0):
    trades = []
    position = None
    flags_by_idx = dict(flags)
    pending = None
    morning_count = 0
    afternoon_count = 0
    daily_entry_seq = 0
    flag_rank_by_idx = {}
    flag_rank_counter = 0

    def position_direction():
        return base._direction_for_symbol(position["symbol"]) if position is not None else None

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()
        bar_ts = hynix_bars_3m["datetime"].iloc[idx]

        if position is not None and idx > position["entry_idx"]:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                net = base._net_pct(position["symbol"], position["entry_price"], close)
                if position["session"] == "MORNING":
                    pm = base.twpm.evaluate_morning_position(net_return_pct=net, tp1_done=position["tp1_done"], peak_net_return=position["peak_net_return"])
                else:
                    pm = base.twpm.evaluate_afternoon_position(net_return_pct=net, peak_net_return=position["peak_net_return"])
                position["peak_net_return"] = pm.peak_net_return
                position["tp1_done"] = pm.tp1_done
                if pm.exit_reason is not None:
                    if pm.exit_reason == base.config.EXIT_TW_TP1_PARTIAL:
                        base._record_partial_leg(_as_openpos(position), qty_fraction=pm.sell_fraction, price=close, reason=pm.exit_reason)
                    else:
                        if pm.exit_reason == base.config.EXIT_TW_TP2_FULL:
                            position["trade"].tp2_hit = True
                        base._close_trade(position["trade"], exit_time=bar_dt, exit_price=close, reason=pm.exit_reason, entry_price=position["entry_price"], symbol=position["symbol"], remaining_fraction=position["remaining_fraction"])
                        trades.append(position["trade"])
                        position = None

        if position is not None and bar_dt.astimezone(base.KST).time() >= base.config.FORCE_LIQUIDATE_AT:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                base._close_trade(position["trade"], exit_time=bar_dt, exit_price=close, reason=base.config.EXIT_FORCED_LIQUIDATION, entry_price=position["entry_price"], symbol=position["symbol"], remaining_fraction=position["remaining_fraction"])
                trades.append(position["trade"])
                position = None
            pending = None

        if idx in flags_by_idx:
            pending = (flags_by_idx[idx], idx, bar_ts)
            flag_rank_counter += 1
            flag_rank_by_idx[idx] = flag_rank_counter

        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                decision_at = bar_dt + timedelta(minutes=3)
                approved, info = base.evaluate_relaxed_entry(
                    hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at, params=ext.base_params,
                    position_direction=position_direction(), morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                if approved:
                    flag_seq = flag_rank_by_idx[p_idx]
                    prospective_entry_seq = morning_count + afternoon_count + 1
                    if ext.max_flag_seq_of_day is not None and flag_seq > ext.max_flag_seq_of_day:
                        approved = False
                    elif prospective_entry_seq in ext.excluded_entry_seq_of_day:
                        approved = False
                if approved:
                    target = base._target_symbol(p_direction)
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position["symbol"] != target:
                            close_now = etf_close[position["symbol"]].get(bar_ts, position["entry_price"])
                            base._close_trade(position["trade"], exit_time=bar_dt, exit_price=close_now, reason=base.config.EXIT_OPPOSITE_SIGNAL, entry_price=position["entry_price"], symbol=position["symbol"], remaining_fraction=position["remaining_fraction"])
                            trades.append(position["trade"])
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
                            position = {
                                "symbol": target, "entry_idx": idx, "entry_price": fill, "session": session,
                                "tp1_done": False, "peak_net_return": 0.0, "remaining_fraction": 1.0, "trade": new_trade,
                            }

    if position is not None:
        close = etf_close[position["symbol"]].get(hynix_bars_3m["datetime"].iloc[-1], position["entry_price"])
        base._close_trade(position["trade"], exit_time=pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime(), exit_price=close, reason="END_OF_DATA", entry_price=position["entry_price"], symbol=position["symbol"], remaining_fraction=position["remaining_fraction"])
        trades.append(position["trade"])
    return trades


class _PositionViaDict:
    """Adapts our plain-dict position record to the attribute interface
    base._record_partial_leg expects (position.remaining_fraction, .trade),
    mutating the underlying dict in place so the simulate_ext loop's own
    ``position["remaining_fraction"]`` reads see the update."""

    def __init__(self, d):
        self._d = d

    @property
    def remaining_fraction(self):
        return self._d["remaining_fraction"]

    @remaining_fraction.setter
    def remaining_fraction(self, v):
        self._d["remaining_fraction"] = v

    @property
    def trade(self):
        return self._d["trade"]


def _as_openpos(position_dict):
    return _PositionViaDict(position_dict)


def run_ext_over_cache(cache, ext: ExtParams):
    all_trades = []
    for day in cache:
        all_trades.extend(simulate_ext(day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"], ext=ext, start_idx=day["start_idx"]))
    return all_trades


if __name__ == "__main__":
    print("Loading TRAIN/VAL day caches...")
    train_cache, train_notes = base._prepare_day_cache(base.TRAIN_DATES)
    val_cache, val_notes = base._prepare_day_cache(base.VAL_DATES)
    print(f"TRAIN={len(train_cache)}d VAL={len(val_cache)}d")

    CANDIDATES = [
        ExtParams(
            label="1_Q2_maxdaily3 (anchor: reconstructs prior #4 simple_md3_noDirExcl)",
            base_params=base.EntryParams(quality_threshold=2, require_gap_expansion=True, min_flag_interval_minutes=9, max_morning_entries=3, max_afternoon_entries=2, max_daily_entries=3),
        ),
        ExtParams(
            label="2_Q3_baseline (anchor: reconstructs prior #5 qt3_stricter)",
            base_params=base.EntryParams(quality_threshold=3, require_gap_expansion=True, min_flag_interval_minutes=9, max_morning_entries=3, max_afternoon_entries=2, max_daily_entries=5),
        ),
        ExtParams(
            label="3_Q3_dirbonus_upred (Q3 + UP_RED needs q>=4)",
            base_params=base.EntryParams(quality_threshold=3, require_gap_expansion=True, min_flag_interval_minutes=9, max_morning_entries=3, max_afternoon_entries=2, max_daily_entries=5, direction_quality_bonus={Direction.UP_RED: 1}),
        ),
        ExtParams(
            label="4_Q3_dirbonus_maxflag4 (3 + exclude day's 5th+ raw flag)",
            base_params=base.EntryParams(quality_threshold=3, require_gap_expansion=True, min_flag_interval_minutes=9, max_morning_entries=3, max_afternoon_entries=2, max_daily_entries=5, direction_quality_bonus={Direction.UP_RED: 1}),
            max_flag_seq_of_day=4,
        ),
        ExtParams(
            label="5_Q3_dirbonus_maxflag4_noseq4 (4 + exclude entry_seq==4)",
            base_params=base.EntryParams(quality_threshold=3, require_gap_expansion=True, min_flag_interval_minutes=9, max_morning_entries=3, max_afternoon_entries=2, max_daily_entries=5, direction_quality_bonus={Direction.UP_RED: 1}),
            max_flag_seq_of_day=4,
            excluded_entry_seq_of_day=(4,),
        ),
    ]

    results = {}
    lines = []
    for ext in CANDIDATES:
        train_trades = run_ext_over_cache(train_cache, ext)
        val_trades = run_ext_over_cache(val_cache, ext)
        tr_m = base.metrics(train_trades, len(train_cache))
        va_m = base.metrics(val_trades, len(val_cache))
        results[ext.label] = {"train": tr_m, "val": va_m}
        line1 = f"{ext.label}"
        line2 = f"  TRAIN({tr_m['trading_days']}d): entries/day={tr_m['avg_entries_per_day']} win={tr_m['win_rate_pct']:.1f}% simple={tr_m['total_simple_cumulative_return_pct']:.2f}% compounded={tr_m['compounded_cumulative_return_pct']:.2f}% PF={tr_m['profit_factor']} maxDD={tr_m['max_drawdown_pct']:.2f} maxConsecLoss={tr_m['max_consecutive_losses']} (AM={tr_m['morning_entries']}/PM={tr_m['afternoon_entries']})"
        line3 = f"  VAL  ({va_m['trading_days']}d): entries/day={va_m['avg_entries_per_day']} win={va_m['win_rate_pct']:.1f}% simple={va_m['total_simple_cumulative_return_pct']:.2f}% compounded={va_m['compounded_cumulative_return_pct']:.2f}% PF={va_m['profit_factor']} maxDD={va_m['max_drawdown_pct']:.2f} maxConsecLoss={va_m['max_consecutive_losses']} (AM={va_m['morning_entries']}/PM={va_m['afternoon_entries']})"
        for l in (line1, line2, line3):
            print(l)
            lines.append(l)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "stage1_entry_candidates_train_val.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUTPUT_DIR / "stage1_entry_candidates_train_val.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\nSaved stage1_entry_candidates_train_val.{json,txt}")
