#!/usr/bin/env python
"""2026-08-17 사용자 요청 (continuation): 시간대 x 방향 x quality score x
당일 플래그 순번 x 당일 진입 순번별로 TRAIN/VAL 성과를 분해해서, TRAIN과 VAL
"양쪽 모두"에서 나쁜 조건만 진입-제외 후보로 삼기 위한 분석. VAL에서만 나쁘다는
이유로 조건을 사후 제거하지 않는다 (요청사항 원칙).

Strictly read-only research, no production code touched. Reuses
scripts/tw_gate_relaxed_optimization.py's day-cache loader, EntryParams,
evaluate_relaxed_entry (the parametrized "게이트 전체 완화" gate) unchanged.
The only new code here is a lightly-extended copy of that script's
``simulate()`` that additionally records, per closed trade:
  - flag_seq_of_day: 1-based rank of this trade's flag among ALL confirmed
    MACD flags that day (both directions), whether or not each flag actually
    became an entry -- i.e. "was this the Nth signal of the day".
  - entry_seq_of_day: 1-based rank among entries actually TAKEN that day
    (same semantics as the original script's Trade.flag_seq_of_day field,
    renamed here to avoid confusion with the above).
Everything else (position management ladder, forced liquidation, opposite-
signal flip, cost engine) is identical to the original script.

Breakdown uses the FULL "게이트 전체 완화" baseline (quality>=2,
require_gap_expansion=True, flag_interval=9min, morning<=3/afternoon<=2,
daily<=5, no blocked windows) as the maximal trade universe -- so entry_seq
and flag_seq values up to 5(+) are all observable, not truncated by whatever
cap a later candidate might use.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.trading.macd2 import config, time_window_filter as twf  # noqa: E402
from app.trading.macd2 import time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
import scripts.tw_gate_relaxed_optimization as base  # noqa: E402

KST = config.KST
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"


@dataclass
class BTrade:
    trading_date: str
    direction: str
    window: str
    session: str
    quality_score: int
    flag_seq_of_day: int
    entry_seq_of_day: int
    entry_time: str
    exit_reason: Optional[str] = None
    net_return_pct: Optional[float] = None
    legs: list = field(default_factory=list)


@dataclass
class _OpenPos:
    symbol: str
    entry_idx: int
    entry_price: float
    session: str
    tp1_done: bool = False
    peak_net_return: float = 0.0
    remaining_fraction: float = 1.0
    trade: BTrade = None


def simulate_breakdown(date: str, hynix_bars_3m, flags, etf_close, *, entry_params: base.EntryParams, start_idx: int = 0) -> list[BTrade]:
    trades: list[BTrade] = []
    position: Optional[_OpenPos] = None
    flags_by_idx = dict(flags)
    pending = None
    morning_count = 0
    afternoon_count = 0
    daily_entry_seq = 0
    flag_rank_by_idx: dict[int, int] = {}
    flag_rank_counter = 0

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
                    pm = twpm.evaluate_morning_position(net_return_pct=net, tp1_done=position.tp1_done, peak_net_return=position.peak_net_return)
                else:
                    pm = twpm.evaluate_afternoon_position(net_return_pct=net, peak_net_return=position.peak_net_return)
                position.peak_net_return = pm.peak_net_return
                position.tp1_done = pm.tp1_done
                if pm.exit_reason is not None:
                    if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                        abs_frac = position.remaining_fraction * pm.sell_fraction
                        position.trade.legs.append((abs_frac, close, pm.exit_reason))
                        position.remaining_fraction -= abs_frac
                    else:
                        _close(position, exit_price=close, reason=pm.exit_reason)
                        trades.append(position.trade)
                        position = None

        if position is not None and bar_dt.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                _close(position, exit_price=close, reason=config.EXIT_FORCED_LIQUIDATION)
                trades.append(position.trade)
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
                    hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at, params=entry_params,
                    position_direction=position_direction(), morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                if approved:
                    target = base._target_symbol(p_direction)
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position.symbol != target:
                            close_now = etf_close[position.symbol].get(bar_ts, position.entry_price)
                            _close(position, exit_price=close_now, reason=config.EXIT_OPPOSITE_SIGNAL)
                            trades.append(position.trade)
                            position = None
                        if position is None:
                            session = info["session"]
                            if session == "MORNING":
                                morning_count += 1
                            else:
                                afternoon_count += 1
                            daily_entry_seq += 1
                            new_trade = BTrade(
                                trading_date=date, direction=p_direction.value, window=info["window"], session=session,
                                quality_score=info["quality_score"], flag_seq_of_day=flag_rank_by_idx[p_idx],
                                entry_seq_of_day=daily_entry_seq, entry_time=bar_dt.isoformat(),
                            )
                            position = _OpenPos(symbol=target, entry_idx=idx, entry_price=fill, session=session, trade=new_trade)

    if position is not None:
        close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[-1], position.entry_price)
        _close(position, exit_price=close, reason="END_OF_DATA")
        trades.append(position.trade)
    return trades


def _close(position: _OpenPos, *, exit_price: float, reason: str) -> None:
    trade = position.trade
    trade.exit_reason = reason
    if position.remaining_fraction > 1e-9:
        trade.legs.append((position.remaining_fraction, exit_price, reason))
    trade.net_return_pct = sum(frac * base._net_pct(position.symbol, position.entry_price, price) for frac, price, _r in trade.legs)


def run_breakdown(cache: list[dict], entry_params: base.EntryParams) -> list[BTrade]:
    out = []
    for day in cache:
        out.extend(simulate_breakdown(day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"], entry_params=entry_params, start_idx=day["start_idx"]))
    return out


STOP_REASONS = {
    config.EXIT_TW_STOP_LOSS, config.EXIT_TW_AFTER_TP1_STOP, config.EXIT_TW_TRAILING_STOP,
    config.EXIT_TW_BREAKEVEN_STOP, config.EXIT_TW_PROFIT_LOCK_STOP,
}


def group_metrics(trades: list[BTrade]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate_pct": None, "avg_return_pct": None, "cum_return_pct": None, "pf": None, "stop_rate_pct": None}
    wins = [t for t in trades if t.net_return_pct > 0]
    losses = [t for t in trades if t.net_return_pct <= 0]
    cum = sum(t.net_return_pct for t in trades)
    gross_win = sum(t.net_return_pct for t in wins)
    gross_loss = abs(sum(t.net_return_pct for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))
    stops = [t for t in trades if t.exit_reason in STOP_REASONS]
    return {
        "n": n,
        "win_rate_pct": round(len(wins) / n * 100.0, 2),
        "avg_return_pct": round(cum / n, 4),
        "cum_return_pct": round(cum, 4),
        "pf": (round(pf, 4) if isinstance(pf, float) and pf != float("inf") else pf),
        "stop_rate_pct": round(len(stops) / n * 100.0, 2),
    }


def breakdown_by(train: list[BTrade], val: list[BTrade], keyfn, label: str) -> dict[str, Any]:
    keys = sorted({keyfn(t) for t in train + val}, key=lambda k: (str(type(k)), k))
    result = {}
    for k in keys:
        tr = [t for t in train if keyfn(t) == k]
        va = [t for t in val if keyfn(t) == k]
        result[str(k)] = {"train": group_metrics(tr), "val": group_metrics(va)}
    return {label: result}


def print_table(label: str, table: dict) -> None:
    print(f"\n=== {label} ===")
    header = f"{'group':<28} {'TR_n':>5} {'TR_win%':>8} {'TR_avg%':>8} {'TR_cum%':>8} {'TR_PF':>7} {'TR_stop%':>9} | {'VA_n':>5} {'VA_win%':>8} {'VA_avg%':>8} {'VA_cum%':>8} {'VA_PF':>7} {'VA_stop%':>9}"
    print(header)
    for k, v in table.items():
        tr, va = v["train"], v["val"]
        def fmt(d, key, width=8):
            val = d.get(key)
            if val is None:
                return " " * (width - 1) + "-"
            return f"{val:>{width}.2f}" if isinstance(val, float) else f"{val:>{width}}"
        print(
            f"{k:<28} {fmt(tr,'n',5)} {fmt(tr,'win_rate_pct')} {fmt(tr,'avg_return_pct')} {fmt(tr,'cum_return_pct')} "
            f"{fmt(tr,'pf',7)} {fmt(tr,'stop_rate_pct',9)} | {fmt(va,'n',5)} {fmt(va,'win_rate_pct')} {fmt(va,'avg_return_pct')} "
            f"{fmt(va,'cum_return_pct')} {fmt(va,'pf',7)} {fmt(va,'stop_rate_pct',9)}"
        )


if __name__ == "__main__":
    print("Loading TRAIN/VAL day caches (baseline full-relaxation gate)...")
    train_cache, train_notes = base._prepare_day_cache(base.TRAIN_DATES)
    val_cache, val_notes = base._prepare_day_cache(base.VAL_DATES)
    print(f"TRAIN={len(train_cache)} days, VAL={len(val_cache)} days")
    for n in train_notes + val_notes:
        print("  note:", n)

    FULL_BASELINE = base.EntryParams(
        quality_threshold=2, require_gap_expansion=True, min_flag_interval_minutes=9,
        max_morning_entries=3, max_afternoon_entries=2, max_daily_entries=5,
    )

    train_trades = run_breakdown(train_cache, FULL_BASELINE)
    val_trades = run_breakdown(val_cache, FULL_BASELINE)
    print(f"\nFull-universe baseline (quality>=2, no window blocks, daily<=5): TRAIN {len(train_trades)} trades / {len(train_cache)}d, VAL {len(val_trades)} trades / {len(val_cache)}d")

    overall_train = group_metrics(train_trades)
    overall_val = group_metrics(val_trades)
    print("Overall TRAIN:", overall_train)
    print("Overall VAL:  ", overall_val)

    tables = {}
    tables.update(breakdown_by(train_trades, val_trades, lambda t: t.window, "window"))
    tables.update(breakdown_by(train_trades, val_trades, lambda t: t.direction, "direction"))
    tables.update(breakdown_by(train_trades, val_trades, lambda t: t.quality_score, "quality_score"))
    tables.update(breakdown_by(train_trades, val_trades, lambda t: t.entry_seq_of_day, "entry_seq_of_day"))
    tables.update(breakdown_by(train_trades, val_trades, lambda t: t.flag_seq_of_day, "flag_seq_of_day"))
    tables.update(breakdown_by(train_trades, val_trades, lambda t: f"{t.window}|{t.direction}", "window_x_direction"))
    tables.update(breakdown_by(train_trades, val_trades, lambda t: f"{t.window}|q{t.quality_score}", "window_x_quality"))
    tables.update(breakdown_by(train_trades, val_trades, lambda t: f"session={t.session}|entry_seq={t.entry_seq_of_day}", "session_x_entryseq"))
    tables.update(breakdown_by(train_trades, val_trades, lambda t: f"{t.direction}|q{t.quality_score}", "direction_x_quality"))

    for label, table in tables.items():
        print_table(label, table)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dump = {
        "overall": {"train": overall_train, "val": overall_val},
        "breakdowns": tables,
        "raw_trades": {
            "train": [vars(t) | {"legs": list(t.legs)} for t in train_trades],
            "val": [vars(t) | {"legs": list(t.legs)} for t in val_trades],
        },
    }
    out_path = OUTPUT_DIR / "entry_breakdown_train_val.json"
    out_path.write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved -> {out_path}")
