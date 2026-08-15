#!/usr/bin/env python
"""Read-only backtest: A(기존 시간대 필터=sideways_filter) vs B(신규 "시간대별
최적거래 필터") vs C(시간필터 없음, baseline) — docs/MACD2_LOGIC.md §18-24.

Uses REAL recorded 1-minute bars for 000660 (signal source) and the actually
traded ETFs 0193T0/0197X0 (실제 체결가 기준, NOT a 2x proxy) for the most
recent 20 real trading days with full same-day coverage of all three symbols:
2026-07-10 through 2026-08-07 (data/cache/replay_YYYYMMDD_{hynix,long,inverse}
_1m.csv). If any date's ETF file were missing this script would fall back to
a 2x-hynix-return proxy for that date ONLY and label it "2x proxy 사용" in the
summary — as of this run every date in the window has real ETF data, so no
proxy fallback is exercised (checked and reported below).

Strictly read-only: never touches the operational runtime state/ledgers/
broker; only reads data/cache/*.csv and writes report artifacts under
data/validation/time_window_filter/.

Shares the SAME entry/exit decision functions the live Worker uses — no
duplicated strategy logic (docs §17/§26):
  - signal_engine.resample_completed_3m / calculate_macd / evaluate_macd_crossover
  - sideways_filter.evaluate_sideways_flag                      (policy A)
  - time_window_filter.evaluate_time_window_entry                (policy B)
  - time_window_position_manager.evaluate_position                (policy B)
  - risk_exit.check_stop_loss                                    (policy A/C)
  - TradeCostEngine.compute_net_pnl / worker._net_return_pct      (all)

Usage:
    python scripts/backtest_time_window_filter.py
    python scripts/backtest_time_window_filter.py --sweep
    python scripts/backtest_time_window_filter.py --split
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.trading.macd2 import config, risk_exit, sideways_filter, time_window_filter as twf  # noqa: E402
from app.trading.macd2 import time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m  # noqa: E402
from app.trading.macd2.worker import _net_return_pct  # noqa: E402
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402

KST = config.KST
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "time_window_filter"

# Most recent 20 real trading days with full 000660/0193T0/0197X0 1-minute
# coverage in data/cache/replay_*.csv (verified 2026-08-15; see report §10).
DATES = [
    "20260710", "20260713", "20260714", "20260715", "20260716", "20260720",
    "20260721", "20260722", "20260723", "20260724", "20260727", "20260728",
    "20260729", "20260730", "20260731", "20260803", "20260804", "20260805",
    "20260806", "20260807",
]

SESSION_END = dtime(15, 30)
COST_ENGINE = TradeCostEngine()


# ── data loading ────────────────────────────────────────────────────────────
def _all_cached_hynix_dates() -> list[str]:
    dates = set()
    for path in CACHE_DIR.glob("replay_*_hynix_1m.csv"):
        stem = path.stem  # replay_YYYYMMDD_hynix_1m
        parts = stem.split("_")
        if len(parts) >= 3 and parts[1].isdigit() and len(parts[1]) == 8:
            dates.add(parts[1])
    return sorted(dates)


_ALL_HYNIX_DATES = _all_cached_hynix_dates()


def _prior_trading_date(date: str) -> Optional[str]:
    """Most recent cached trading date strictly before ``date`` (used ONLY
    for EMA warm-up, matching production's own '전일 데이터는 EMA warm-up에만
    사용한다' rule) — never used for ETF entry/exit prices."""
    earlier = [d for d in _ALL_HYNIX_DATES if d < date]
    return earlier[-1] if earlier else None


def _load_1m(date: str, tag: str) -> Optional[pd.DataFrame]:
    path = CACHE_DIR / f"replay_{date}_{tag}_1m.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize(KST)
    return df.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)


def _load_hynix_with_warmup(date: str) -> tuple[pd.DataFrame, Optional[str]]:
    """Current day's 000660 1m bars, prefixed with the prior cached trading
    day's bars for EMA(12/26/9) warm-up ONLY (docs: '전일 데이터는 EMA
    warm-up에만 사용한다') -- without this, calculate_macd needs >=26
    completed 3m bars before it returns anything at all, so a same-day-only
    build can never produce a flag before ~10:18 KST every single day,
    artificially zeroing the whole 09:00-10:20 window. Returns
    (frame, prior_date_used_or_None)."""
    current = _load_1m(date, "hynix")
    if current is None:
        return current, None
    prior_date = _prior_trading_date(date)
    if prior_date is None:
        return current, None
    prior = _load_1m(prior_date, "hynix")
    if prior is None:
        return current, None
    combined = pd.concat([prior, current], ignore_index=True)
    combined = combined.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)
    return combined, prior_date


def _session_end_dt(date: str) -> datetime:
    day = datetime.strptime(date, "%Y%m%d").replace(tzinfo=KST)
    return day.replace(hour=SESSION_END.hour, minute=SESSION_END.minute)


# ── shared confirmed-flag detection (identical for all 3 policies) ─────────
def detect_confirmed_flags(bars_3m: pd.DataFrame, current_date: str) -> list[tuple[int, Direction]]:
    """Mirrors worker._advance_confirmed_primary / scripts/macd2_validate_
    major_filter.py's detect_confirmed_flags exactly, including its
    PER-CALENDAR-DAY baseline reset: walk completed 3m bars (which may span
    a prior warm-up day plus the current day) in order; the first bar of
    EVERY new calendar date sets direction baseline only (never a flag, and
    never counted toward same-direction suppression) -- this is what lets a
    genuine reversal right at today's open still fire normally while an
    overnight EMA gap never masquerades as an intraday crossover. Only
    flags whose bar actually falls on ``current_date`` are returned (prior
    warm-up-day bars only ever seed EMA state, never become tradeable
    flags in this backtest)."""
    flags: list[tuple[int, Direction]] = []
    previous_direction: Optional[Direction] = None
    last_bar_date: Optional[str] = None
    for i in range(len(bars_3m)):
        snap = calculate_macd(bars_3m.iloc[: i + 1])
        if snap is None:
            continue
        bar_date = pd.Timestamp(bars_3m["datetime"].iloc[i]).astimezone(KST).strftime("%Y%m%d")
        is_first_of_day = last_bar_date is None or bar_date != last_bar_date
        last_bar_date = bar_date
        if is_first_of_day:
            continue
        direction = evaluate_macd_crossover(snap, previous_direction)
        if direction in (Direction.UP_RED, Direction.DOWN_BLUE):
            previous_direction = direction
            if bar_date == current_date:
                flags.append((i, direction))
    return flags


def _target_symbol(direction: Direction) -> str:
    return config.LONG_SYMBOL if direction == Direction.UP_RED else config.INVERSE_SYMBOL


def _direction_for_symbol(symbol: str) -> Optional[Direction]:
    if symbol == config.LONG_SYMBOL:
        return Direction.UP_RED
    if symbol == config.INVERSE_SYMBOL:
        return Direction.DOWN_BLUE
    return None


# ── trade record ────────────────────────────────────────────────────────────
@dataclass
class Trade:
    trading_date: str
    policy: str
    flag_seq_overall: int
    direction: str
    flag_time: str
    confirm_time: str
    entry_time: str
    entry_symbol: str
    entry_price: float
    session: str
    session_seq: Optional[int]
    quality_score: Optional[float]
    tp1_hit: bool = False
    tp2_hit: bool = False
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    net_return_pct: Optional[float] = None
    proxy_used: bool = False


@dataclass
class OpenPosition:
    symbol: str
    entry_idx: int
    entry_price: float
    entry_time: datetime
    qty: float
    initial_qty: float
    session: str
    session_seq: Optional[int]
    quality_score: Optional[float]
    flag_seq_overall: int
    flag_time: str
    confirm_time: str
    tp1_done: bool = False
    tp2_hit: bool = False
    peak_net_return: float = 0.0
    trade: Trade = None


def _etf_close_lookup(etf_bars_3m: pd.DataFrame) -> dict[pd.Timestamp, float]:
    return dict(zip(etf_bars_3m["datetime"], etf_bars_3m["close"]))


def _net_pct(symbol: str, entry_price: float, exit_price: float) -> float:
    return _net_return_pct(symbol, entry_price, exit_price, 1)  # qty cancels out in the %; use 1 for a pure rate


def _close_trade(trade: Trade, *, exit_time: datetime, exit_price: float, reason: str, entry_price: float, symbol: str) -> None:
    trade.exit_time = exit_time.isoformat()
    trade.exit_price = exit_price
    trade.exit_reason = reason
    trade.net_return_pct = _net_pct(symbol, entry_price, exit_price)


# ── policy C: no time filter (every confirmed flag has immediate authority) ─
def simulate_baseline(
    date: str, hynix_bars_3m: pd.DataFrame, flags: list[tuple[int, Direction]],
    etf_close: dict[str, dict[pd.Timestamp, float]], proxy_dates: set[str], start_idx: int = 0,
) -> list[Trade]:
    trades: list[Trade] = []
    position: Optional[OpenPosition] = None
    flags_by_idx = dict(flags)

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()

        if position is not None and idx > position.entry_idx:
            close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[idx])
            if close is not None:
                net = _net_pct(position.symbol, position.entry_price, close)
                if risk_exit.check_stop_loss(net):
                    _close_trade(position.trade, exit_time=bar_dt, exit_price=close,
                                 reason=config.EXIT_STOP_LOSS, entry_price=position.entry_price, symbol=position.symbol)
                    trades.append(position.trade)
                    position = None

        if position is not None and bar_dt.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[idx])
            if close is not None:
                _close_trade(position.trade, exit_time=bar_dt, exit_price=close,
                             reason=config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol)
                trades.append(position.trade)
                position = None

        if idx in flags_by_idx and (position is None or bar_dt.astimezone(KST).time() < config.FORCE_LIQUIDATE_AT):
            direction = flags_by_idx[idx]
            target = _target_symbol(direction)
            fill = etf_close[target].get(hynix_bars_3m["datetime"].iloc[idx])
            if fill is None:
                continue
            if position is not None and position.symbol == target:
                continue  # same direction, no pyramiding
            if position is not None and position.symbol != target:
                _close_trade(position.trade, exit_time=bar_dt, exit_price=etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[idx], position.entry_price),
                             reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol)
                trades.append(position.trade)
                position = None
            new_trade = Trade(
                trading_date=date, policy="C", flag_seq_overall=len([f for f in flags if f[0] <= idx]),
                direction=direction.value, flag_time=bar_dt.isoformat(), confirm_time=bar_dt.isoformat(),
                entry_time=bar_dt.isoformat(), entry_symbol=target, entry_price=fill,
                session="MORNING" if bar_dt.astimezone(KST).time() < dtime(12, 30) else "AFTERNOON",
                session_seq=None, quality_score=None, proxy_used=(date in proxy_dates),
            )
            position = OpenPosition(
                symbol=target, entry_idx=idx, entry_price=fill, entry_time=bar_dt, qty=1.0, initial_qty=1.0,
                session=new_trade.session, session_seq=None, quality_score=None,
                flag_seq_overall=new_trade.flag_seq_overall, flag_time=new_trade.flag_time,
                confirm_time=new_trade.confirm_time, trade=new_trade,
            )

    if position is not None:
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime()
        close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[-1], position.entry_price)
        _close_trade(position.trade, exit_time=last_dt, exit_price=close,
                     reason="END_OF_DATA", entry_price=position.entry_price, symbol=position.symbol)
        trades.append(position.trade)
    return trades


# ── policy A: existing time-window filter (sideways_filter, reused as-is) ──
def simulate_sideways(
    date: str, hynix_bars_3m: pd.DataFrame, hynix_1m: pd.DataFrame, flags: list[tuple[int, Direction]],
    etf_close: dict[str, dict[pd.Timestamp, float]], proxy_dates: set[str], start_idx: int = 0,
) -> list[Trade]:
    trades: list[Trade] = []
    position: Optional[OpenPosition] = None
    flags_by_idx = dict(flags)

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()

        if position is not None and idx > position.entry_idx:
            close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[idx])
            if close is not None:
                net = _net_pct(position.symbol, position.entry_price, close)
                if risk_exit.check_stop_loss(net):
                    _close_trade(position.trade, exit_time=bar_dt, exit_price=close,
                                 reason=config.EXIT_STOP_LOSS, entry_price=position.entry_price, symbol=position.symbol)
                    trades.append(position.trade)
                    position = None

        if position is not None and bar_dt.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[idx])
            if close is not None:
                _close_trade(position.trade, exit_time=bar_dt, exit_price=close,
                             reason=config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol)
                trades.append(position.trade)
                position = None

        if idx in flags_by_idx:
            direction = flags_by_idx[idx]
            decision_at = bar_dt + timedelta(minutes=3)
            decision = sideways_filter.evaluate_sideways_flag(
                hynix_bars_3m.iloc[: idx + 1], hynix_1m[hynix_1m["datetime"] <= bar_dt], direction, decision_at,
            )
            target = _target_symbol(direction)
            fill = etf_close[target].get(hynix_bars_3m["datetime"].iloc[idx])
            if not decision.approved:
                # rejected reversal -> sell-only (matches worker._execute_
                # reversal_exit_only_for_filtered_entry for every non-TIME_
                # WINDOW gate_mode); a rejected FLAT entry is simply a no-op.
                # IMPORTANT: exit price must come from the HELD position's
                # own symbol, never `fill` (the new/rejected target's price).
                if position is not None and position.symbol != target:
                    exit_price = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[idx])
                    if exit_price is not None:
                        _close_trade(position.trade, exit_time=bar_dt, exit_price=exit_price,
                                     reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol)
                        trades.append(position.trade)
                        position = None
                continue
            if fill is None:
                continue
            if position is not None and position.symbol == target:
                continue
            if position is not None and position.symbol != target:
                _close_trade(position.trade, exit_time=bar_dt, exit_price=etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[idx], position.entry_price),
                             reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol)
                trades.append(position.trade)
                position = None
            new_trade = Trade(
                trading_date=date, policy="A", flag_seq_overall=len([f for f in flags if f[0] <= idx]),
                direction=direction.value, flag_time=bar_dt.isoformat(), confirm_time=bar_dt.isoformat(),
                entry_time=bar_dt.isoformat(), entry_symbol=target, entry_price=fill,
                session="MORNING" if bar_dt.astimezone(KST).time() < dtime(12, 30) else "AFTERNOON",
                session_seq=None, quality_score=round(float(decision.score), 1), proxy_used=(date in proxy_dates),
            )
            position = OpenPosition(
                symbol=target, entry_idx=idx, entry_price=fill, entry_time=bar_dt, qty=1.0, initial_qty=1.0,
                session=new_trade.session, session_seq=None, quality_score=new_trade.quality_score,
                flag_seq_overall=new_trade.flag_seq_overall, flag_time=new_trade.flag_time,
                confirm_time=new_trade.confirm_time, trade=new_trade,
            )

    if position is not None:
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime()
        close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[-1], position.entry_price)
        _close_trade(position.trade, exit_time=last_dt, exit_price=close,
                     reason="END_OF_DATA", entry_price=position.entry_price, symbol=position.symbol)
        trades.append(position.trade)
    return trades


# ── policy B: new "시간대별 최적거래 필터" (two-bar T->T+3 confirmation) ────
def simulate_time_window(
    date: str, hynix_bars_3m: pd.DataFrame, flags: list[tuple[int, Direction]],
    etf_close: dict[str, dict[pd.Timestamp, float]], proxy_dates: set[str], start_idx: int = 0,
) -> tuple[list[Trade], list[dict[str, Any]]]:
    trades: list[Trade] = []
    rejected: list[dict[str, Any]] = []
    position: Optional[OpenPosition] = None
    flags_by_idx = dict(flags)
    pending: Optional[tuple[Direction, int, pd.Timestamp]] = None
    morning_count = 0
    afternoon_count = 0

    def position_direction() -> Optional[Direction]:
        return _direction_for_symbol(position.symbol) if position is not None else None

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()
        bar_ts = hynix_bars_3m["datetime"].iloc[idx]

        # 1) position management ladder (only for a TW-managed position)
        if position is not None and idx > position.entry_idx:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                net = _net_pct(position.symbol, position.entry_price, close)
                pm = twpm.evaluate_position(
                    session=position.session, net_return_pct=net,
                    tp1_done=position.tp1_done, peak_net_return=position.peak_net_return,
                )
                position.peak_net_return = pm.peak_net_return
                position.tp1_done = pm.tp1_done
                if pm.exit_reason is not None:
                    if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                        position.trade.tp1_hit = True
                        position.qty *= (1.0 - pm.sell_fraction)
                        # partial exit does not close the trade record yet;
                        # log it as a distinct completed leg for visibility
                        # while the position stays open with reduced qty.
                    else:
                        if pm.exit_reason == config.EXIT_TW_TP2_FULL:
                            position.trade.tp2_hit = True
                        _close_trade(position.trade, exit_time=bar_dt, exit_price=close,
                                     reason=pm.exit_reason, entry_price=position.entry_price, symbol=position.symbol)
                        trades.append(position.trade)
                        position = None

        # 2) forced liquidation at/after 15:00
        if position is not None and bar_dt.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                _close_trade(position.trade, exit_time=bar_dt, exit_price=close,
                             reason=config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol)
                trades.append(position.trade)
                position = None
            pending = None

        # 3) a fresh confirmed crossover always becomes (or replaces) the
        #    pending T+3 candidate -- no order authority on this bar itself.
        if idx in flags_by_idx:
            pending = (flags_by_idx[idx], idx, bar_ts)

        # 4) resolve a pending candidate exactly one bar after its flag bar
        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                decision_at = bar_dt + timedelta(minutes=3)
                decision = twf.evaluate_time_window_entry(
                    hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at,
                    position_direction=position_direction(),
                    morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                if not decision.approved:
                    rejected.append({
                        "trading_date": date, "flag_bar_at": flag_bar_dt.isoformat(),
                        "direction": p_direction.value, "decision": decision.decision,
                        "block_reason": decision.block_reason,
                    })
                else:
                    target = _target_symbol(p_direction)
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position.symbol != target:
                            close_now = etf_close[position.symbol].get(bar_ts, position.entry_price)
                            _close_trade(position.trade, exit_time=bar_dt, exit_price=close_now,
                                         reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol)
                            trades.append(position.trade)
                            position = None
                        if position is None:
                            window = decision.metrics.get("window")
                            session = twf.session_for_window(window) or "MORNING"
                            if session == "MORNING":
                                morning_count += 1
                                seq = morning_count
                            else:
                                afternoon_count += 1
                                seq = afternoon_count
                            new_trade = Trade(
                                trading_date=date, policy="B", flag_seq_overall=len([f for f in flags if f[0] <= p_idx]),
                                direction=p_direction.value, flag_time=flag_bar_dt.isoformat(),
                                confirm_time=bar_dt.isoformat(), entry_time=bar_dt.isoformat(),
                                entry_symbol=target, entry_price=fill, session=session, session_seq=seq,
                                quality_score=decision.score, proxy_used=(date in proxy_dates),
                            )
                            position = OpenPosition(
                                symbol=target, entry_idx=idx, entry_price=fill, entry_time=bar_dt,
                                qty=1.0, initial_qty=1.0, session=session, session_seq=seq,
                                quality_score=decision.score, flag_seq_overall=new_trade.flag_seq_overall,
                                flag_time=new_trade.flag_time, confirm_time=new_trade.confirm_time, trade=new_trade,
                            )

    if position is not None:
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime()
        close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[-1], position.entry_price)
        _close_trade(position.trade, exit_time=last_dt, exit_price=close,
                     reason="END_OF_DATA", entry_price=position.entry_price, symbol=position.symbol)
        trades.append(position.trade)
    return trades, rejected


# ── metrics ──────────────────────────────────────────────────────────────
def _metrics(trades: list[Trade], trading_days: int) -> dict[str, Any]:
    closed = [t for t in trades if t.net_return_pct is not None]
    wins = [t for t in closed if t.net_return_pct > 0]
    losses = [t for t in closed if t.net_return_pct <= 0]
    morning = [t for t in closed if t.session == "MORNING"]
    afternoon = [t for t in closed if t.session == "AFTERNOON"]
    total_simple = sum(t.net_return_pct for t in closed)
    compounded = 1.0
    for t in closed:
        compounded *= (1.0 + t.net_return_pct / 100.0)
    compounded_pct = (compounded - 1.0) * 100.0
    gross_win = sum(t.net_return_pct for t in wins)
    gross_loss = abs(sum(t.net_return_pct for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))
    expectancy = (total_simple / len(closed)) if closed else 0.0

    equity = peak = max_dd = 0.0
    max_consec_losses = consec = 0
    for t in closed:
        equity += t.net_return_pct
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if t.net_return_pct <= 0:
            consec += 1
            max_consec_losses = max(max_consec_losses, consec)
        else:
            consec = 0

    return {
        "trading_days": trading_days,
        "total_entries": len(closed),
        "avg_entries_per_day": round(len(closed) / trading_days, 3) if trading_days else 0.0,
        "morning_entries": len(morning),
        "afternoon_entries": len(afternoon),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2) if closed else 0.0,
        "avg_return_pct": round(total_simple / len(closed), 4) if closed else 0.0,
        "avg_win_pct": round(sum(t.net_return_pct for t in wins) / len(wins), 4) if wins else 0.0,
        "avg_loss_pct": round(sum(t.net_return_pct for t in losses) / len(losses), 4) if losses else 0.0,
        "total_simple_cumulative_return_pct": round(total_simple, 4),
        "compounded_cumulative_return_pct": round(compounded_pct, 4),
        "profit_factor": (round(profit_factor, 4) if isinstance(profit_factor, float) and profit_factor != float("inf") else profit_factor),
        "expectancy_pct": round(expectancy, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "max_consecutive_losses": max_consec_losses,
        "morning_tp2_hits": sum(1 for t in closed if t.tp2_hit),
        "morning_tp1_hits": sum(1 for t in closed if t.tp1_hit),
        "afternoon_tp_hits": sum(1 for t in closed if t.exit_reason == config.EXIT_TW_AFTERNOON_TP),
        "stop_loss_exits": sum(1 for t in closed if t.exit_reason in (config.EXIT_STOP_LOSS, config.EXIT_TW_STOP_LOSS)),
        "opposite_flag_exits": sum(1 for t in closed if t.exit_reason == config.EXIT_OPPOSITE_SIGNAL),
    }


TIME_WINDOWS = [
    ("09:00-09:45", dtime(9, 0), dtime(9, 45)),
    ("09:45-10:20", dtime(9, 45), dtime(10, 20)),
    ("10:20-10:50", dtime(10, 20), dtime(10, 50)),
    ("10:50-13:00", dtime(10, 50), dtime(13, 0)),
    ("13:00-14:00", dtime(13, 0), dtime(14, 0)),
    ("14:00-15:00", dtime(14, 0), dtime(15, 0)),
]


def _window_breakdown(trades: list[Trade]) -> dict[str, Any]:
    out = {}
    for label, start, end in TIME_WINDOWS:
        bucket = [
            t for t in trades
            if t.entry_time and start <= datetime.fromisoformat(t.entry_time).astimezone(KST).time() < end
        ]
        closed = [t for t in bucket if t.net_return_pct is not None]
        wins = [t for t in closed if t.net_return_pct > 0]
        losses = [t for t in closed if t.net_return_pct <= 0]
        gross_win = sum(t.net_return_pct for t in wins)
        gross_loss = abs(sum(t.net_return_pct for t in losses))
        holding_minutes = []
        for t in bucket:
            if t.exit_time:
                holding_minutes.append((datetime.fromisoformat(t.exit_time) - datetime.fromisoformat(t.entry_time)).total_seconds() / 60.0)
        out[label] = {
            "candidate_entries": len(bucket),
            "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2) if closed else 0.0,
            "avg_return_pct": round(sum(t.net_return_pct for t in closed) / len(closed), 4) if closed else 0.0,
            "cumulative_return_pct": round(sum(t.net_return_pct for t in closed), 4),
            "profit_factor": (round(gross_win / gross_loss, 4) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))),
            "max_loss_pct": round(min((t.net_return_pct for t in closed), default=0.0), 4),
            "avg_holding_minutes": round(sum(holding_minutes) / len(holding_minutes), 1) if holding_minutes else 0.0,
        }
    return out


def _trade_to_row(t: Trade) -> dict[str, Any]:
    return {
        "trading_date": t.trading_date, "policy": t.policy, "flag_seq_overall": t.flag_seq_overall,
        "selected": True, "direction": t.direction, "flag_time": t.flag_time, "confirm_time": t.confirm_time,
        "entry_time": t.entry_time, "entry_symbol": t.entry_symbol, "entry_price": t.entry_price,
        "session": t.session, "session_seq": t.session_seq, "quality_score": t.quality_score,
        "tp1_hit": t.tp1_hit, "tp2_hit": t.tp2_hit, "exit_time": t.exit_time, "exit_price": t.exit_price,
        "exit_reason": t.exit_reason, "net_return_pct": t.net_return_pct, "proxy_used": t.proxy_used,
    }


def _prepare_day_cache(dates: list[str]) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    """Loads + resamples each date exactly once so parameter sweeps/splits
    can re-simulate policy B repeatedly without re-reading CSVs each time."""
    cache = []
    proxy_dates: set[str] = set()
    data_notes: list[str] = []
    for date in dates:
        hynix_1m_warm, prior_date = _load_hynix_with_warmup(date)
        long_1m = _load_1m(date, "long")
        inverse_1m = _load_1m(date, "inverse")
        if hynix_1m_warm is None:
            data_notes.append(f"{date}: missing hynix 1m data -- skipped entirely")
            continue
        if long_1m is None or inverse_1m is None:
            proxy_dates.add(date)
            data_notes.append(f"{date}: missing ETF 1m data -- date skipped (no proxy fallback implemented)")
            continue
        if prior_date is None:
            data_notes.append(f"{date}: no prior cached trading day for EMA warm-up")

        end = _session_end_dt(date)
        hynix_bars_3m = resample_completed_3m(hynix_1m_warm, now=end)
        long_bars_3m = resample_completed_3m(long_1m, now=end)
        inverse_bars_3m = resample_completed_3m(inverse_1m, now=end)
        etf_close = {
            config.LONG_SYMBOL: _etf_close_lookup(long_bars_3m),
            config.INVERSE_SYMBOL: _etf_close_lookup(inverse_bars_3m),
        }
        current_day_mask = hynix_bars_3m["datetime"].dt.strftime("%Y%m%d") == date
        if not current_day_mask.any():
            data_notes.append(f"{date}: no completed 3m bars resolved for this calendar date -- skipped")
            continue
        start_idx = int(current_day_mask.to_numpy().nonzero()[0][0])
        flags = detect_confirmed_flags(hynix_bars_3m, date)
        cache.append({
            "date": date, "hynix_bars_3m": hynix_bars_3m, "hynix_1m_warm": hynix_1m_warm,
            "flags": flags, "etf_close": etf_close, "start_idx": start_idx,
        })
    return cache, proxy_dates, data_notes


def run_full_backtest(dates: list[str] = DATES) -> dict[str, Any]:
    all_trades = {"A": [], "B": [], "C": []}
    all_flags_rows = []
    rejected_b = []
    proxy_dates: set[str] = set()
    data_notes = []

    for date in dates:
        hynix_1m_warm, prior_date = _load_hynix_with_warmup(date)
        long_1m = _load_1m(date, "long")
        inverse_1m = _load_1m(date, "inverse")
        if hynix_1m_warm is None:
            data_notes.append(f"{date}: missing hynix 1m data -- skipped entirely")
            continue
        if long_1m is None or inverse_1m is None:
            proxy_dates.add(date)
            data_notes.append(f"{date}: missing ETF 1m data -- 2x proxy would be used (not implemented in this pass; date skipped)")
            continue
        if prior_date is None:
            data_notes.append(f"{date}: no prior cached trading day available for EMA warm-up -- flags may be sparse/absent in the first ~78 minutes of this date")

        end = _session_end_dt(date)
        hynix_bars_3m = resample_completed_3m(hynix_1m_warm, now=end)
        long_bars_3m = resample_completed_3m(long_1m, now=end)
        inverse_bars_3m = resample_completed_3m(inverse_1m, now=end)
        etf_close = {
            config.LONG_SYMBOL: _etf_close_lookup(long_bars_3m),
            config.INVERSE_SYMBOL: _etf_close_lookup(inverse_bars_3m),
        }

        current_day_mask = hynix_bars_3m["datetime"].dt.strftime("%Y%m%d") == date
        if not current_day_mask.any():
            data_notes.append(f"{date}: no completed 3m bars resolved for this calendar date -- skipped")
            continue
        start_idx = int(current_day_mask.to_numpy().nonzero()[0][0])

        flags = detect_confirmed_flags(hynix_bars_3m, date)
        for idx, direction in flags:
            all_flags_rows.append({
                "trading_date": date, "flag_bar_at": pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).isoformat(),
                "direction": direction.value,
            })

        trades_c = simulate_baseline(date, hynix_bars_3m, flags, etf_close, proxy_dates, start_idx=start_idx)
        trades_a = simulate_sideways(date, hynix_bars_3m, hynix_1m_warm, flags, etf_close, proxy_dates, start_idx=start_idx)
        trades_b, rejected = simulate_time_window(date, hynix_bars_3m, flags, etf_close, proxy_dates, start_idx=start_idx)
        rejected_b.extend(rejected)

        all_trades["A"].extend(trades_a)
        all_trades["B"].extend(trades_b)
        all_trades["C"].extend(trades_c)

    used_dates = [d for d in dates if d not in {n.split(":")[0] for n in data_notes if "skipped" in n}]
    n_days = len(used_dates)

    summary = {
        "generated_at": "SEE_REPORT",
        "read_only": True,
        "data_window": {"dates": used_dates, "count": n_days},
        "proxy_dates_2x": sorted(proxy_dates),
        "data_notes": data_notes,
        "policy_A_definition": "existing time-window filter reused as-is: sideways_filter.evaluate_sideways_flag (production's only time-of-day entry gate); exits are unchanged production rules (STOP_LOSS -1.5%, immediate opposite-flag switch, 15:00 forced liquidation) -- no partial TP.",
        "policy_B_definition": "new '시간대별 최적거래 필터': two-bar (T->T+3) delayed entry confirmation + windowed quality gates + its own partial-TP/ratcheted-stop position management.",
        "policy_C_definition": "no time filter at all -- every confirmed flag has immediate order authority (production default with all optional filters OFF); same legacy exits as A.",
        "metrics": {p: _metrics(all_trades[p], n_days) for p in ("A", "B", "C")},
        "time_window_breakdown": {p: _window_breakdown(all_trades[p]) for p in ("A", "B", "C")},
    }
    return {"summary": summary, "trades": all_trades, "all_flags": all_flags_rows, "rejected_b": rejected_b}


# ── §22 parameter sensitivity sweep (one parameter varied at a time) ───────
SWEEP_SPECS: list[tuple[str, list[float], Optional[str]]] = [
    ("MIN_FLAG_INTERVAL_MINUTES", [6, 9, 12, 15], None),
    ("MORNING_TP1", [0.020, 0.025, 0.030], "MORNING_TP1_PCT"),
    ("MORNING_TP1_SELL_RATIO", [0.30, 0.50, 0.70], "MORNING_TP1_SELL_RATIO"),
    ("MORNING_TP2", [0.04, 0.05, 0.06], "MORNING_TP2_PCT"),
    ("MORNING_STOP_LOSS", [-0.010, -0.015, -0.020], "MORNING_STOP_LOSS_PCT"),
    ("AFTERNOON_TP", [0.020, 0.025, 0.030], "AFTERNOON_TP_PCT"),
    ("AFTERNOON_STOP_LOSS", [-0.008, -0.012, -0.015], "AFTERNOON_STOP_LOSS_PCT"),
]


def _run_policy_b_over_cache(cache: list[dict[str, Any]], proxy_dates: set[str]) -> list[Trade]:
    all_trades_b: list[Trade] = []
    for day in cache:
        trades_b, _rejected = simulate_time_window(
            day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"], proxy_dates, start_idx=day["start_idx"],
        )
        all_trades_b.extend(trades_b)
    return all_trades_b


def run_sweep(dates: list[str] = DATES) -> dict[str, Any]:
    """§22: vary ONE parameter at a time (others held at spec default),
    never a brute-force grid search over all combinations -- looks for a
    stable neighborhood, not a single best-fit point."""
    cache, proxy_dates, notes = _prepare_day_cache(dates)
    n_days = len(cache)
    results: dict[str, Any] = {}
    for attr, values, twpm_attr in SWEEP_SPECS:
        original_config_value = getattr(config, attr)
        original_twpm_value = getattr(twpm, twpm_attr) if twpm_attr else None
        rows = []
        for value in values:
            setattr(config, attr, value)
            if twpm_attr:
                scale = 100.0 if twpm_attr.endswith("_PCT") else 1.0
                setattr(twpm, twpm_attr, value * scale)
            trades_b = _run_policy_b_over_cache(cache, proxy_dates)
            m = _metrics(trades_b, n_days)
            rows.append({
                "value": value, "entries": m["total_entries"], "win_rate_pct": m["win_rate_pct"],
                "net_simple_cumulative_return_pct": m["total_simple_cumulative_return_pct"],
                "profit_factor": m["profit_factor"], "max_drawdown_pct": m["max_drawdown_pct"],
            })
        setattr(config, attr, original_config_value)
        if twpm_attr:
            setattr(twpm, twpm_attr, original_twpm_value)
        results[attr] = rows
    return {"trading_days": n_days, "data_notes": notes, "sweep": results}


# ── §23 calibration/validation split ────────────────────────────────────────
def run_split(dates: list[str] = DATES) -> dict[str, Any]:
    cache, proxy_dates, notes = _prepare_day_cache(dates)
    n = len(cache)
    half = n // 2
    first_half_dates = [d["date"] for d in cache[:half]]
    second_half_dates = [d["date"] for d in cache[half:]]

    def _metrics_for(subset: list[dict[str, Any]]) -> dict[str, Any]:
        trades_b = _run_policy_b_over_cache(subset, proxy_dates)
        return _metrics(trades_b, len(subset))

    first_metrics = _metrics_for(cache[:half])
    second_metrics = _metrics_for(cache[half:])
    full_metrics = _metrics_for(cache)
    overfit_flag = (
        first_metrics["total_simple_cumulative_return_pct"] > 0
        and second_metrics["total_simple_cumulative_return_pct"] < 0
    ) or (
        first_metrics["profit_factor"] not in (None,)
        and second_metrics["profit_factor"] not in (None,)
        and isinstance(first_metrics["profit_factor"], (int, float))
        and isinstance(second_metrics["profit_factor"], (int, float))
        and first_metrics["profit_factor"] > 1.5 and second_metrics["profit_factor"] < 1.0
    )
    return {
        "first_half_dates": first_half_dates, "second_half_dates": second_half_dates,
        "calibration_first_half": first_metrics, "validation_second_half": second_metrics,
        "full_20_days": full_metrics, "overfitting_risk_flag": bool(overfit_flag),
    }


def write_outputs(result: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for policy in ("A", "B", "C"):
        rows = [_trade_to_row(t) for t in result["trades"][policy]]
        pd.DataFrame(rows).to_csv(OUTPUT_DIR / f"trades_{policy}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(result["all_flags"]).to_csv(OUTPUT_DIR / "all_flags.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(result["rejected_b"]).to_csv(OUTPUT_DIR / "rejected_candidates_B.csv", index=False, encoding="utf-8-sig")
    with open(OUTPUT_DIR / "summary_comparison.json", "w", encoding="utf-8") as fh:
        json.dump(result["summary"], fh, ensure_ascii=False, indent=2, default=str)
    print(f"Wrote outputs to {OUTPUT_DIR}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", nargs="*", default=None)
    parser.add_argument("--sweep", action="store_true", help="run the §22 parameter sensitivity sweep instead of the main A/B/C comparison")
    parser.add_argument("--split", action="store_true", help="run the §23 calibration/validation split instead of the main A/B/C comparison")
    args = parser.parse_args()
    dates = args.dates or DATES

    if args.sweep:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        sweep_result = run_sweep(dates)
        with open(OUTPUT_DIR / "parameter_sweep.json", "w", encoding="utf-8") as fh:
            json.dump(sweep_result, fh, ensure_ascii=False, indent=2, default=str)
        print(f"Wrote {OUTPUT_DIR / 'parameter_sweep.json'}")
        for attr, rows in sweep_result["sweep"].items():
            print(f"-- {attr} --")
            for row in rows:
                print(f"   {row['value']}: entries={row['entries']} win_rate={row['win_rate_pct']}% "
                      f"net_simple={row['net_simple_cumulative_return_pct']}% PF={row['profit_factor']}")
        return 0

    if args.split:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        split_result = run_split(dates)
        with open(OUTPUT_DIR / "calibration_validation_split.json", "w", encoding="utf-8") as fh:
            json.dump(split_result, fh, ensure_ascii=False, indent=2, default=str)
        print(f"Wrote {OUTPUT_DIR / 'calibration_validation_split.json'}")
        print("first half:", split_result["calibration_first_half"])
        print("second half:", split_result["validation_second_half"])
        print("overfitting_risk_flag:", split_result["overfitting_risk_flag"])
        return 0

    result = run_full_backtest(dates)
    write_outputs(result)
    for p in ("A", "B", "C"):
        m = result["summary"]["metrics"][p]
        print(f"{p}: entries={m['total_entries']} win_rate={m['win_rate_pct']}% "
              f"net_simple={m['total_simple_cumulative_return_pct']}% "
              f"compounded={m['compounded_cumulative_return_pct']}% PF={m['profit_factor']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
