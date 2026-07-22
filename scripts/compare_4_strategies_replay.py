"""4-strategy read-only replay comparison on today's KIS 1-min bars.

Strategy A: Production weighted RANGE (as in replay_today_weighted_range.py)
Strategy B: MACD 3-min crossover only
Strategy C: MACD + Williams %R 3-min confirmation
Strategy D: Price-action early entry + MACD/Williams confirmation scale-in

All strategies share:
  - 09:00–14:50 new entry, 15:15 force-close
  - Initial cash 10,000,000 KRW
  - Next-minute-open fill + 0.05% slippage (conservative)
  - Round-trip cost via TradeCostEngine
  - No future data, no incomplete 3-min candles
  - No duplicate episode entry
  - Stress: 15s/30s delay + 0.10% slippage scenarios

Usage:
    python scripts/compare_4_strategies_replay.py
"""
from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading import early_trend_detector as etd  # noqa: E402
from app.trading.etf_entry_confirmation import (  # noqa: E402
    compute_etf_breakouts,
    compute_etf_vwap,
    is_swing_structure_broken_against,
    resolve_window_directions,
    trade_aligned_window_directions,
)
from app.trading.hynix_fast_trend import compute_fast_trend_signal  # noqa: E402
from app.trading.hynix_symbols import (  # noqa: E402
    LONG_SYMBOL,
    SHORT_SYMBOL as INVERSE_SYMBOL,
    SIGNAL_SYMBOL,
)
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed  # noqa: E402
from app.trading.range_weighted_optimize import (  # noqa: E402
    classify_intraday_regime,
    get_range_weighted_config,
    load_optimized_config,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402

INITIAL_CASH = 10_000_000.0
TODAY = datetime.now().strftime("%Y-%m-%d")

# ── shared helpers ──

def _price_at(df: pd.DataFrame, ts: datetime) -> Optional[float]:
    t0 = ts.replace(second=0, microsecond=0)
    t1 = t0 + timedelta(minutes=1)
    r0 = df[df["datetime"] == t0]
    r1 = df[df["datetime"] == t1]
    if r0.empty:
        return float(r1.iloc[0]["open"]) if not r1.empty else None
    if r1.empty:
        return float(r0.iloc[0]["close"])
    frac = ts.second / 60.0
    return float(r0.iloc[0]["close"]) * (1 - frac) + float(r1.iloc[0]["open"]) * frac


def _next_min_open(df: pd.DataFrame, ts: datetime) -> Optional[float]:
    minute = ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
    row = df[df["datetime"] == minute]
    return float(row.iloc[0]["open"]) if not row.empty else None


def _conservative_fill(df: pd.DataFrame, ts: datetime, side: str, slip_pct: float = 0.05) -> Optional[float]:
    base = _next_min_open(df, ts)
    if base is None:
        return None
    s = slip_pct / 100.0
    return base * (1.0 + s) if side == "BUY" else base * (1.0 - s)


def _delayed_fill(df: pd.DataFrame, ts: datetime, side: str, delay_sec: int, slip_pct: float) -> Optional[float]:
    delayed_ts = ts + timedelta(seconds=delay_sec)
    return _conservative_fill(df, delayed_ts, side, slip_pct)


def _resample_3m(df_1m: pd.DataFrame) -> pd.DataFrame:
    return (
        df_1m.set_index("datetime")
        .resample("3min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["close"])
        .reset_index()
    )


def _completed_3m_bars(df_3m: pd.DataFrame, ts: datetime) -> pd.DataFrame:
    """Only bars whose 3-min window is fully closed (3min boundary <= ts)."""
    cutoff = ts.replace(second=0, microsecond=0)
    return df_3m[df_3m["datetime"] + timedelta(minutes=3) <= cutoff]


def _macd_signal(closes: pd.Series) -> tuple[float, float, float]:
    """Return (macd_line, signal_line, histogram) for latest bar."""
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    return float(macd.iloc[-1]), float(sig.iloc[-1]), float(macd.iloc[-1] - sig.iloc[-1])


def _williams_r(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> Optional[float]:
    if len(closes) < period:
        return None
    hh = highs.rolling(period).max()
    ll = lows.rolling(period).min()
    span = (hh - ll).replace(0.0, float("nan"))
    wr = ((hh - closes) / span * -100.0).dropna()
    return float(wr.iloc[-1]) if len(wr) > 0 else None


def _fast_direction(hynix_1m: pd.DataFrame, ts: datetime) -> str:
    h_slice = hynix_1m[hynix_1m["datetime"] <= ts].tail(30)
    if len(h_slice) < 5:
        return "FLAT"
    sig = compute_fast_trend_signal(h_slice, now=ts)
    return sig.get("direction") or "FLAT"


# ── Trade record ──

@dataclass
class Trade:
    strategy: str
    symbol: str
    direction: str
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    qty: int = 0
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    cost: float = 0.0
    exit_reason: str = ""
    entry_path: str = ""
    signal_time: Optional[datetime] = None
    cons_entry_price: Optional[float] = None
    cons_exit_price: Optional[float] = None
    cons_net_pnl: float = 0.0
    held_seconds: float = 0.0
    wrong_direction: bool = False
    episode_id: str = ""


@dataclass
class StrategyResult:
    name: str
    trades: list[Trade] = field(default_factory=list)
    duplicate_blocked: int = 0


# ── Strategy A: Production weighted RANGE ──

def _run_strategy_a(
    hynix_1m: pd.DataFrame, long_1m: pd.DataFrame, inverse_1m: pd.DataFrame,
    slip_pct: float = 0.05, delay_sec: int = 0,
) -> StrategyResult:
    """Delegate to existing replay code, extract trades."""
    from scripts.replay_today_weighted_range import run_replay
    load_optimized_config()
    result = run_replay(hynix_1m, long_1m, inverse_1m)
    sr = StrategyResult(name="A: weighted RANGE")
    cost_engine = TradeCostEngine()

    buys = [e for e in result["events"] if e["action"] == "매수"]
    sells = [e for e in result["events"] if e["action"] == "매도"]

    # parse conservative trades
    for tc in result.get("trades_conservative", []):
        if tc["side"] != "SELL":
            continue
        t = Trade(
            strategy="A",
            symbol=tc.get("symbol", ""),
            direction="UP" if tc.get("symbol") == LONG_SYMBOL else "DOWN",
            entry_time=datetime.now(),
            entry_price=0.0,
            exit_time=tc["time"],
            exit_price=tc["price"],
            qty=tc["qty"],
            net_pnl=tc.get("net_pnl", 0.0),
            held_seconds=tc.get("held_seconds", 0.0),
            exit_reason=tc.get("reason", ""),
        )
        sr.trades.append(t)

    sr.duplicate_blocked = result.get("duplicate_episode", 0)
    return sr, result


# ── Strategy B: MACD 3-min crossover only ──

def _run_strategy_b(
    hynix_1m: pd.DataFrame, long_1m: pd.DataFrame, inverse_1m: pd.DataFrame,
    slip_pct: float = 0.05, delay_sec: int = 0,
) -> StrategyResult:
    sr = StrategyResult(name="B: MACD 3min only")
    hynix_3m = _resample_3m(hynix_1m)
    cost_engine = TradeCostEngine()

    cash = INITIAL_CASH
    position = None
    episode_ids: set[str] = set()
    prev_hist = None

    for i in range(26, len(hynix_3m)):
        bar = hynix_3m.iloc[i]
        ts = bar["datetime"] + timedelta(minutes=3)  # bar close time
        if ts.hour < 9 or (ts.hour == 14 and ts.minute > 50) or ts.hour >= 15:
            if position and ts.hour >= 15 and ts.minute >= 15:
                # force close
                exit_price = _conservative_fill(
                    long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m,
                    ts, "SELL", slip_pct,
                )
                if exit_price is None:
                    exit_price = _price_at(long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m, ts) or position["entry_price"]
                qty = position["qty"]
                gross = (exit_price - position["entry_price"]) * qty
                cost_pct = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0
                cost_val = cost_pct * position["entry_price"] * qty
                net = gross - cost_val
                cash += qty * exit_price
                sr.trades.append(Trade(
                    strategy="B", symbol=position["symbol"],
                    direction=position["direction"],
                    entry_time=position["entry_time"], entry_price=position["entry_price"],
                    exit_time=ts, exit_price=exit_price, qty=qty,
                    net_pnl=net, gross_pnl=gross, cost=cost_val,
                    exit_reason="15:15_FORCE_CLOSE",
                    held_seconds=(ts - position["entry_time"]).total_seconds(),
                    signal_time=position.get("signal_time"),
                    episode_id=position.get("episode_id", ""),
                    cons_entry_price=position.get("cons_entry_price"),
                    cons_exit_price=exit_price,
                    cons_net_pnl=net,
                ))
                position = None
            continue

        completed = _completed_3m_bars(hynix_3m, ts)
        if len(completed) < 26:
            continue
        closes = pd.to_numeric(completed["close"], errors="coerce").dropna()
        if len(closes) < 26:
            continue
        _, _, hist = _macd_signal(closes)
        if prev_hist is None:
            prev_hist = hist
            continue

        direction = _fast_direction(hynix_1m, ts)
        if direction == "FLAT":
            prev_hist = hist
            continue

        # MACD crossover: histogram sign change
        crossover_up = prev_hist <= 0 and hist > 0
        crossover_down = prev_hist >= 0 and hist < 0

        # Exit check
        if position is not None:
            exit_needed = False
            exit_reason = ""
            etf_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
            held_price = _price_at(etf_df, ts) or position["entry_price"]
            net_ret = (held_price / position["entry_price"] - 1.0) * 100.0

            if net_ret <= -0.5:
                exit_needed = True
                exit_reason = "MACD_STOP_LOSS"
            elif position["direction"] == "UP" and crossover_down:
                exit_needed = True
                exit_reason = "MACD_CROSS_DOWN"
            elif position["direction"] == "DOWN" and crossover_up:
                exit_needed = True
                exit_reason = "MACD_CROSS_UP"
            elif net_ret >= 1.35:
                exit_needed = True
                exit_reason = "MACD_TAKE_PROFIT"

            if exit_needed:
                exit_price = _conservative_fill(etf_df, ts, "SELL", slip_pct)
                if delay_sec:
                    exit_price = _delayed_fill(etf_df, ts, "SELL", delay_sec, slip_pct) or exit_price
                if exit_price is None:
                    exit_price = held_price
                qty = position["qty"]
                gross = (exit_price - position["entry_price"]) * qty
                cost_pct = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0
                cost_val = cost_pct * position["entry_price"] * qty
                net = gross - cost_val
                cash += qty * exit_price
                true_dir = "UP" if float(hynix_1m["close"].iloc[-1]) > float(hynix_1m["close"].iloc[0]) else "DOWN"
                sr.trades.append(Trade(
                    strategy="B", symbol=position["symbol"],
                    direction=position["direction"],
                    entry_time=position["entry_time"], entry_price=position["entry_price"],
                    exit_time=ts, exit_price=exit_price, qty=qty,
                    net_pnl=net, gross_pnl=gross, cost=cost_val,
                    exit_reason=exit_reason,
                    held_seconds=(ts - position["entry_time"]).total_seconds(),
                    signal_time=position.get("signal_time"),
                    episode_id=position.get("episode_id", ""),
                    cons_entry_price=position.get("cons_entry_price"),
                    cons_exit_price=exit_price,
                    cons_net_pnl=net,
                    wrong_direction=position["direction"] != true_dir,
                ))
                position = None

        # Entry check
        if position is None and (crossover_up or crossover_down):
            trade_dir = "UP" if crossover_up else "DOWN"
            symbol = LONG_SYMBOL if trade_dir == "UP" else INVERSE_SYMBOL
            ep_id = f"B:{trade_dir}:{ts.strftime('%H%M')}"
            if ep_id in episode_ids:
                sr.duplicate_blocked += 1
                prev_hist = hist
                continue
            episode_ids.add(ep_id)
            etf_df = long_1m if symbol == LONG_SYMBOL else inverse_1m
            entry_price = _conservative_fill(etf_df, ts, "BUY", slip_pct)
            if delay_sec:
                entry_price = _delayed_fill(etf_df, ts, "BUY", delay_sec, slip_pct) or entry_price
            if entry_price is None:
                prev_hist = hist
                continue
            qty = max(1, int(cash * 0.40 / entry_price))
            if qty * entry_price > cash:
                prev_hist = hist
                continue
            cash -= qty * entry_price
            position = {
                "symbol": symbol, "direction": trade_dir, "qty": qty,
                "entry_price": entry_price, "entry_time": ts,
                "signal_time": ts, "episode_id": ep_id,
                "cons_entry_price": entry_price,
            }

        prev_hist = hist

    # Force close at end
    if position:
        ts_end = hynix_1m["datetime"].iloc[-1]
        etf_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
        exit_price = _price_at(etf_df, ts_end) or position["entry_price"]
        qty = position["qty"]
        gross = (exit_price - position["entry_price"]) * qty
        cost_pct = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0
        cost_val = cost_pct * position["entry_price"] * qty
        net = gross - cost_val
        sr.trades.append(Trade(
            strategy="B", symbol=position["symbol"], direction=position["direction"],
            entry_time=position["entry_time"], entry_price=position["entry_price"],
            exit_time=ts_end, exit_price=exit_price, qty=qty,
            net_pnl=net, gross_pnl=gross, cost=cost_val,
            exit_reason="END_OF_DAY", held_seconds=(ts_end - position["entry_time"]).total_seconds(),
            cons_entry_price=position.get("cons_entry_price"), cons_exit_price=exit_price, cons_net_pnl=net,
        ))
    return sr


# ── Strategy C: MACD + Williams %R 3-min ──

def _run_strategy_c(
    hynix_1m: pd.DataFrame, long_1m: pd.DataFrame, inverse_1m: pd.DataFrame,
    slip_pct: float = 0.05, delay_sec: int = 0,
) -> StrategyResult:
    sr = StrategyResult(name="C: MACD+Williams 3min")
    hynix_3m = _resample_3m(hynix_1m)
    cost_engine = TradeCostEngine()
    cash = INITIAL_CASH
    position = None
    episode_ids: set[str] = set()
    prev_hist = None

    for i in range(26, len(hynix_3m)):
        bar = hynix_3m.iloc[i]
        ts = bar["datetime"] + timedelta(minutes=3)
        if ts.hour < 9 or (ts.hour == 14 and ts.minute > 50) or ts.hour >= 15:
            if position and ts.hour >= 15 and ts.minute >= 15:
                etf_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
                exit_price = _conservative_fill(etf_df, ts, "SELL", slip_pct) or _price_at(etf_df, ts) or position["entry_price"]
                qty = position["qty"]
                gross = (exit_price - position["entry_price"]) * qty
                cost_val = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
                net = gross - cost_val
                cash += qty * exit_price
                sr.trades.append(Trade(
                    strategy="C", symbol=position["symbol"], direction=position["direction"],
                    entry_time=position["entry_time"], entry_price=position["entry_price"],
                    exit_time=ts, exit_price=exit_price, qty=qty,
                    net_pnl=net, gross_pnl=gross, cost=cost_val, exit_reason="15:15_FORCE_CLOSE",
                    held_seconds=(ts - position["entry_time"]).total_seconds(),
                    cons_entry_price=position.get("cons_entry_price"), cons_exit_price=exit_price, cons_net_pnl=net,
                ))
                position = None
            continue

        completed = _completed_3m_bars(hynix_3m, ts)
        if len(completed) < 26:
            continue
        closes = pd.to_numeric(completed["close"], errors="coerce").dropna()
        highs = pd.to_numeric(completed["high"], errors="coerce").dropna()
        lows = pd.to_numeric(completed["low"], errors="coerce").dropna()
        if len(closes) < 26:
            continue
        _, _, hist = _macd_signal(closes)
        wr = _williams_r(highs, lows, closes)
        if prev_hist is None:
            prev_hist = hist
            continue

        direction = _fast_direction(hynix_1m, ts)
        crossover_up = prev_hist <= 0 and hist > 0
        crossover_down = prev_hist >= 0 and hist < 0

        # Exit
        if position is not None:
            exit_needed = False
            exit_reason = ""
            etf_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
            held_price = _price_at(etf_df, ts) or position["entry_price"]
            net_ret = (held_price / position["entry_price"] - 1.0) * 100.0

            if net_ret <= -0.5:
                exit_needed, exit_reason = True, "STOP_LOSS"
            elif position["direction"] == "UP" and crossover_down and wr is not None and wr > -20:
                exit_needed, exit_reason = True, "MACD_DOWN+WR_OVERBOUGHT"
            elif position["direction"] == "DOWN" and crossover_up and wr is not None and wr < -80:
                exit_needed, exit_reason = True, "MACD_UP+WR_OVERSOLD"
            elif position["direction"] == "UP" and crossover_down:
                exit_needed, exit_reason = True, "MACD_CROSS_DOWN"
            elif position["direction"] == "DOWN" and crossover_up:
                exit_needed, exit_reason = True, "MACD_CROSS_UP"
            elif net_ret >= 1.35:
                exit_needed, exit_reason = True, "TAKE_PROFIT"

            if exit_needed:
                exit_price = _conservative_fill(etf_df, ts, "SELL", slip_pct)
                if delay_sec:
                    exit_price = _delayed_fill(etf_df, ts, "SELL", delay_sec, slip_pct) or exit_price
                if exit_price is None:
                    exit_price = held_price
                qty = position["qty"]
                gross = (exit_price - position["entry_price"]) * qty
                cost_val = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
                net = gross - cost_val
                cash += qty * exit_price
                true_dir = "UP" if float(hynix_1m["close"].iloc[-1]) > float(hynix_1m["close"].iloc[0]) else "DOWN"
                sr.trades.append(Trade(
                    strategy="C", symbol=position["symbol"], direction=position["direction"],
                    entry_time=position["entry_time"], entry_price=position["entry_price"],
                    exit_time=ts, exit_price=exit_price, qty=qty,
                    net_pnl=net, gross_pnl=gross, cost=cost_val, exit_reason=exit_reason,
                    held_seconds=(ts - position["entry_time"]).total_seconds(),
                    cons_entry_price=position.get("cons_entry_price"), cons_exit_price=exit_price, cons_net_pnl=net,
                    wrong_direction=position["direction"] != true_dir,
                ))
                position = None

        # Entry: MACD cross + Williams confirmation
        if position is None and (crossover_up or crossover_down):
            trade_dir = "UP" if crossover_up else "DOWN"
            wr_ok = False
            if trade_dir == "UP" and wr is not None and wr < -50:
                wr_ok = True
            if trade_dir == "DOWN" and wr is not None and wr > -50:
                wr_ok = True
            if not wr_ok:
                prev_hist = hist
                continue

            symbol = LONG_SYMBOL if trade_dir == "UP" else INVERSE_SYMBOL
            ep_id = f"C:{trade_dir}:{ts.strftime('%H%M')}"
            if ep_id in episode_ids:
                sr.duplicate_blocked += 1
                prev_hist = hist
                continue
            episode_ids.add(ep_id)
            etf_df = long_1m if symbol == LONG_SYMBOL else inverse_1m
            entry_price = _conservative_fill(etf_df, ts, "BUY", slip_pct)
            if delay_sec:
                entry_price = _delayed_fill(etf_df, ts, "BUY", delay_sec, slip_pct) or entry_price
            if entry_price is None:
                prev_hist = hist
                continue
            qty = max(1, int(cash * 0.40 / entry_price))
            if qty * entry_price > cash:
                prev_hist = hist
                continue
            cash -= qty * entry_price
            position = {
                "symbol": symbol, "direction": trade_dir, "qty": qty,
                "entry_price": entry_price, "entry_time": ts,
                "signal_time": ts, "episode_id": ep_id,
                "cons_entry_price": entry_price,
            }

        prev_hist = hist

    # Force close
    if position:
        ts_end = hynix_1m["datetime"].iloc[-1]
        etf_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
        exit_price = _price_at(etf_df, ts_end) or position["entry_price"]
        qty = position["qty"]
        gross = (exit_price - position["entry_price"]) * qty
        cost_val = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
        net = gross - cost_val
        sr.trades.append(Trade(
            strategy="C", symbol=position["symbol"], direction=position["direction"],
            entry_time=position["entry_time"], entry_price=position["entry_price"],
            exit_time=ts_end, exit_price=exit_price, qty=qty,
            net_pnl=net, gross_pnl=gross, cost=cost_val, exit_reason="END_OF_DAY",
            held_seconds=(ts_end - position["entry_time"]).total_seconds(),
            cons_entry_price=position.get("cons_entry_price"), cons_exit_price=exit_price, cons_net_pnl=net,
        ))
    return sr


# ── Strategy D: Price-action early entry + MACD/Williams confirmation ──

def _run_strategy_d(
    hynix_1m: pd.DataFrame, long_1m: pd.DataFrame, inverse_1m: pd.DataFrame,
    slip_pct: float = 0.05, delay_sec: int = 0,
) -> StrategyResult:
    sr = StrategyResult(name="D: PriceAction+MACD/WR")
    hynix_3m = _resample_3m(hynix_1m)
    cost_engine = TradeCostEngine()
    cash = INITIAL_CASH
    position = None
    episode_ids: set[str] = set()
    scale_in_done = False
    start = max(hynix_1m["datetime"].min(), long_1m["datetime"].min(), inverse_1m["datetime"].min())
    start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = min(hynix_1m["datetime"].max(), long_1m["datetime"].max(), inverse_1m["datetime"].max())
    end = end.replace(second=0, microsecond=0)

    ts = start
    while ts <= end:
        if ts.hour < 9 or (ts.hour == 14 and ts.minute > 50 and position is None):
            ts += timedelta(seconds=5)
            continue
        if ts.hour >= 15 and ts.minute >= 15 and position is not None:
            # force close
            etf_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
            exit_price = _conservative_fill(etf_df, ts, "SELL", slip_pct) or _price_at(etf_df, ts) or position["entry_price"]
            qty = position["qty"]
            gross = (exit_price - position["entry_price"]) * qty
            cost_val = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
            net = gross - cost_val
            cash += qty * exit_price
            sr.trades.append(Trade(
                strategy="D", symbol=position["symbol"], direction=position["direction"],
                entry_time=position["entry_time"], entry_price=position["entry_price"],
                exit_time=ts, exit_price=exit_price, qty=qty,
                net_pnl=net, gross_pnl=gross, cost=cost_val, exit_reason="15:15_FORCE_CLOSE",
                held_seconds=(ts - position["entry_time"]).total_seconds(),
                cons_entry_price=position.get("cons_entry_price"), cons_exit_price=exit_price, cons_net_pnl=net,
            ))
            position = None
            break
        if ts.hour >= 15 and ts.minute >= 15:
            break

        h_slice = hynix_1m[hynix_1m["datetime"] <= ts].tail(30)
        if len(h_slice) < 5:
            ts += timedelta(seconds=5)
            continue
        fast = compute_fast_trend_signal(h_slice, now=ts)
        live_dir = fast.get("direction") or "FLAT"
        if live_dir == "FLAT":
            ts += timedelta(seconds=5)
            continue

        desired_symbol = LONG_SYMBOL if live_dir == "UP" else INVERSE_SYMBOL
        etf_df = long_1m if desired_symbol == LONG_SYMBOL else inverse_1m
        current_price = _price_at(etf_df, ts)
        if current_price is None:
            ts += timedelta(seconds=5)
            continue

        # Gather price-action evidence
        etf_slice = etf_df[etf_df["datetime"] <= ts].tail(30)
        if len(etf_slice) < 5:
            ts += timedelta(seconds=5)
            continue
        breakouts = compute_etf_breakouts(etf_slice, current_price, live_dir)
        vwap_ok = bool(breakouts.get("vwap_breakout"))
        structure_ok = bool(breakouts.get("structure_breakout"))

        # 5/10s slope via fast signal votes
        slope_aligned = fast.get("up_votes", 0) >= 3 if live_dir == "UP" else fast.get("down_votes", 0) >= 3

        # Opposite ETF weakness
        opp_df = inverse_1m if desired_symbol == LONG_SYMBOL else long_1m
        opp_price = _price_at(opp_df, ts)
        opp_weak = False
        if opp_price:
            opp_slice = opp_df[opp_df["datetime"] <= ts].tail(5)
            if len(opp_slice) >= 2:
                opp_ret = (float(opp_slice["close"].iloc[-1]) / float(opp_slice["close"].iloc[0]) - 1.0) * 100.0
                opp_weak = opp_ret <= 0.0

        evidence_count = sum([slope_aligned, vwap_ok, structure_ok, opp_weak])

        # Exit logic for held position
        if position is not None:
            held_etf_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
            held_price = _price_at(held_etf_df, ts) or position["entry_price"]
            net_ret = (held_price / position["entry_price"] - 1.0) * 100.0

            exit_needed = False
            exit_reason = ""

            # Hard stop
            if net_ret <= -0.5:
                exit_needed, exit_reason = True, "HARD_STOP"
            # Price structure invalidation
            elif position["direction"] == "UP" and live_dir == "DOWN" and not vwap_ok:
                exit_needed, exit_reason = True, "STRUCTURE_INVALIDATED"
            elif position["direction"] == "DOWN" and live_dir == "UP" and not vwap_ok:
                exit_needed, exit_reason = True, "STRUCTURE_INVALIDATED"
            else:
                # MACD + Williams simultaneous opposite
                completed = _completed_3m_bars(hynix_3m, ts)
                if len(completed) >= 26:
                    closes_3m = pd.to_numeric(completed["close"], errors="coerce").dropna()
                    highs_3m = pd.to_numeric(completed["high"], errors="coerce").dropna()
                    lows_3m = pd.to_numeric(completed["low"], errors="coerce").dropna()
                    if len(closes_3m) >= 26:
                        _, _, hist = _macd_signal(closes_3m)
                        wr = _williams_r(highs_3m, lows_3m, closes_3m)
                        if position["direction"] == "UP" and hist < 0 and wr is not None and wr > -20:
                            exit_needed, exit_reason = True, "MACD_WR_OPPOSITE"
                        elif position["direction"] == "DOWN" and hist > 0 and wr is not None and wr < -80:
                            exit_needed, exit_reason = True, "MACD_WR_OPPOSITE"

            if net_ret >= 1.35:
                exit_needed, exit_reason = True, "TAKE_PROFIT"

            if exit_needed:
                exit_price = _conservative_fill(held_etf_df, ts, "SELL", slip_pct)
                if delay_sec:
                    exit_price = _delayed_fill(held_etf_df, ts, "SELL", delay_sec, slip_pct) or exit_price
                if exit_price is None:
                    exit_price = held_price
                qty = position["qty"]
                gross = (exit_price - position["entry_price"]) * qty
                cost_val = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
                net = gross - cost_val
                cash += qty * exit_price
                true_dir = "UP" if float(hynix_1m["close"].iloc[-1]) > float(hynix_1m["close"].iloc[0]) else "DOWN"
                sr.trades.append(Trade(
                    strategy="D", symbol=position["symbol"], direction=position["direction"],
                    entry_time=position["entry_time"], entry_price=position["entry_price"],
                    exit_time=ts, exit_price=exit_price, qty=qty,
                    net_pnl=net, gross_pnl=gross, cost=cost_val, exit_reason=exit_reason,
                    held_seconds=(ts - position["entry_time"]).total_seconds(),
                    cons_entry_price=position.get("cons_entry_price"), cons_exit_price=exit_price, cons_net_pnl=net,
                    wrong_direction=position["direction"] != true_dir,
                ))
                position = None
                scale_in_done = False

            # MACD/Williams confirmation scale-in
            if position is not None and not scale_in_done:
                completed = _completed_3m_bars(hynix_3m, ts)
                if len(completed) >= 26:
                    closes_3m = pd.to_numeric(completed["close"], errors="coerce").dropna()
                    highs_3m = pd.to_numeric(completed["high"], errors="coerce").dropna()
                    lows_3m = pd.to_numeric(completed["low"], errors="coerce").dropna()
                    if len(closes_3m) >= 26:
                        _, _, hist = _macd_signal(closes_3m)
                        wr = _williams_r(highs_3m, lows_3m, closes_3m)
                        macd_ok = (position["direction"] == "UP" and hist > 0) or (position["direction"] == "DOWN" and hist < 0)
                        wr_ok = wr is not None and (
                            (position["direction"] == "UP" and wr > -80)
                            or (position["direction"] == "DOWN" and wr < -20)
                        )
                        if macd_ok and wr_ok:
                            add_pct = 0.20
                            add_qty = max(1, int(cash * add_pct / current_price))
                            if add_qty * current_price <= cash:
                                ep = _conservative_fill(etf_df, ts, "BUY", slip_pct) or current_price
                                cash -= add_qty * ep
                                # Adjust position weighted average
                                total_cost_basis = position["entry_price"] * position["qty"] + ep * add_qty
                                position["qty"] += add_qty
                                position["entry_price"] = total_cost_basis / position["qty"]
                                scale_in_done = True

        # New entry: price-action probe
        if position is None and evidence_count >= 3:
            if ts.hour == 14 and ts.minute > 50:
                ts += timedelta(seconds=5)
                continue
            ep_id = f"D:{live_dir}:{ts.strftime('%H%M')}"
            if ep_id in episode_ids:
                sr.duplicate_blocked += 1
                ts += timedelta(seconds=5)
                continue
            episode_ids.add(ep_id)
            entry_price = _conservative_fill(etf_df, ts, "BUY", slip_pct)
            if delay_sec:
                entry_price = _delayed_fill(etf_df, ts, "BUY", delay_sec, slip_pct) or entry_price
            if entry_price is None:
                ts += timedelta(seconds=5)
                continue
            qty = max(1, int(cash * 0.25 / entry_price))
            if qty * entry_price > cash:
                ts += timedelta(seconds=5)
                continue
            cash -= qty * entry_price
            position = {
                "symbol": desired_symbol, "direction": live_dir, "qty": qty,
                "entry_price": entry_price, "entry_time": ts,
                "signal_time": ts, "episode_id": ep_id,
                "cons_entry_price": entry_price,
            }
            scale_in_done = False

        ts += timedelta(seconds=5)

    # Force close remaining
    if position:
        ts_end = hynix_1m["datetime"].iloc[-1]
        etf_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
        exit_price = _price_at(etf_df, ts_end) or position["entry_price"]
        qty = position["qty"]
        gross = (exit_price - position["entry_price"]) * qty
        cost_val = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
        net = gross - cost_val
        sr.trades.append(Trade(
            strategy="D", symbol=position["symbol"], direction=position["direction"],
            entry_time=position["entry_time"], entry_price=position["entry_price"],
            exit_time=ts_end, exit_price=exit_price, qty=qty,
            net_pnl=net, gross_pnl=gross, cost=cost_val, exit_reason="END_OF_DAY",
            held_seconds=(ts_end - position["entry_time"]).total_seconds(),
            cons_entry_price=position.get("cons_entry_price"), cons_exit_price=exit_price, cons_net_pnl=net,
        ))
    return sr


# ── Metrics calculation ──

def _compute_metrics(sr: StrategyResult) -> dict:
    trades = sr.trades
    if not trades:
        return {
            "name": sr.name, "entries": 0, "round_trips": 0, "lev_trades": 0, "inv_trades": 0,
            "win_rate": 0.0, "pf": 0.0, "net_pnl": 0.0, "return_pct": 0.0, "mdd_pct": 0.0,
            "total_cost": 0.0, "cost_gross_ratio": 0.0, "avg_hold_sec": 0.0, "median_hold_sec": 0.0,
            "sub20_trips": 0, "wrong_direction_trades": [], "duplicate_blocked": sr.duplicate_blocked,
        }

    n = len(trades)
    lev = sum(1 for t in trades if t.symbol == LONG_SYMBOL)
    inv = n - lev
    wins = sum(1 for t in trades if t.net_pnl > 0)
    gross_profit = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gross_loss = abs(sum(t.gross_pnl for t in trades if t.gross_pnl < 0))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    net_pnl = sum(t.net_pnl for t in trades)
    total_cost = sum(t.cost for t in trades)
    cost_ratio = (total_cost / gross_profit * 100.0) if gross_profit > 0 else 0.0

    hold_secs = [t.held_seconds for t in trades]
    sub20 = sum(1 for s in hold_secs if s < 20.0)

    # MDD
    equity = INITIAL_CASH
    peak = equity
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_time or x.entry_time):
        equity += t.net_pnl
        peak = max(peak, equity)
        dd = (equity / peak - 1.0) * 100.0
        max_dd = min(max_dd, dd)

    wrong_trades = [t for t in trades if t.wrong_direction]

    return {
        "name": sr.name,
        "entries": n,
        "round_trips": n,
        "lev_trades": lev,
        "inv_trades": inv,
        "win_rate": (wins / n * 100.0) if n else 0.0,
        "pf": pf,
        "net_pnl": net_pnl,
        "return_pct": (net_pnl / INITIAL_CASH * 100.0),
        "mdd_pct": max_dd,
        "total_cost": total_cost,
        "cost_gross_ratio": cost_ratio,
        "avg_hold_sec": statistics.mean(hold_secs) if hold_secs else 0.0,
        "median_hold_sec": statistics.median(hold_secs) if hold_secs else 0.0,
        "sub20_trips": sub20,
        "wrong_direction_trades": wrong_trades,
        "duplicate_blocked": sr.duplicate_blocked,
        "trades": trades,
    }


# ── Output formatting ──

def _print_strategy_report(m: dict) -> None:
    print(f"\n{'─' * 72}")
    print(f"  {m['name']}")
    print(f"{'─' * 72}")
    print(f"  진입/왕복: {m['entries']}  (레버리지 {m['lev_trades']}, 인버스 {m['inv_trades']})")
    pf_txt = f"{m['pf']:.2f}" if m['pf'] != float("inf") else "∞"
    print(f"  승률: {m['win_rate']:.1f}%   PF: {pf_txt}   Net PnL: {m['net_pnl']:+,.0f} KRW   수익률: {m['return_pct']:+.3f}%")
    print(f"  MDD: {m['mdd_pct']:.3f}%   총비용: {m['total_cost']:,.0f} KRW   비용/Gross: {m['cost_gross_ratio']:.1f}%")
    print(f"  평균보유: {m['avg_hold_sec']:.0f}s   중앙보유: {m['median_hold_sec']:.0f}s   20초미만: {m['sub20_trips']}")
    print(f"  중복진입 차단: {m['duplicate_blocked']}")

    if m.get("wrong_direction_trades"):
        print(f"  잘못된 방향 진입 ({len(m['wrong_direction_trades'])}건):")
        for t in m["wrong_direction_trades"]:
            print(f"    {t.entry_time.strftime('%H:%M:%S')} {t.direction} {t.symbol} → PnL {t.net_pnl:+,.0f}")
    else:
        print("  잘못된 방향 진입: 0건")

    # Trade detail
    print(f"  {'신호시각':>10s} {'주문시각':>10s} {'청산시각':>10s} {'방향':>4s} {'종목':>8s} {'수량':>5s} {'NetPnL':>10s} {'청산사유'}")
    for t in m.get("trades", []):
        sig = t.signal_time.strftime("%H:%M:%S") if t.signal_time else "—"
        ent = t.entry_time.strftime("%H:%M:%S")
        ext = t.exit_time.strftime("%H:%M:%S") if t.exit_time else "—"
        sym = "레버" if t.symbol == LONG_SYMBOL else "인버"
        print(f"  {sig:>10s} {ent:>10s} {ext:>10s} {t.direction:>4s} {sym:>8s} {t.qty:>5d} {t.net_pnl:>+10,.0f} {t.exit_reason}")


def _print_intraday_analysis(m: dict, hynix_1m: pd.DataFrame) -> None:
    """Print analysis for key intraday periods."""
    trades = m.get("trades", [])
    periods = [
        ("12:51 이후 하락", datetime(2026, 7, 21, 12, 51), datetime(2026, 7, 21, 13, 22)),
        ("13:22~14:17 인버스 구간", datetime(2026, 7, 21, 13, 22), datetime(2026, 7, 21, 14, 17)),
        ("14:18 이후 반등", datetime(2026, 7, 21, 14, 18), datetime(2026, 7, 21, 15, 0)),
        ("마지막 30분", datetime(2026, 7, 21, 15, 0), datetime(2026, 7, 21, 15, 30)),
    ]
    for label, t0, t1 in periods:
        period_trades = [t for t in trades if t.entry_time >= t0 and t.entry_time < t1]
        pnl = sum(t.net_pnl for t in period_trades)
        cnt = len(period_trades)
        print(f"    {label}: {cnt}건, PnL {pnl:+,.0f}")


def _load_1m_data():
    """Load 1-min bars from existing replay infrastructure."""
    from scripts.replay_today_weighted_range import fetch_full_day_1min
    print("KIS에서 1분봉 수집 중...")
    hynix_1m = fetch_full_day_1min(SIGNAL_SYMBOL)
    long_1m = fetch_full_day_1min(LONG_SYMBOL)
    inverse_1m = fetch_full_day_1min(INVERSE_SYMBOL)
    print(f"  하이닉스: {len(hynix_1m)}봉, 레버리지: {len(long_1m)}봉, 인버스: {len(inverse_1m)}봉")
    return hynix_1m, long_1m, inverse_1m


def main() -> int:
    load_optimized_config()
    print("=" * 72)
    print(f"4-Strategy Replay Comparison — {TODAY}")
    print("=" * 72)

    hynix_1m, long_1m, inverse_1m = _load_1m_data()
    regime = classify_intraday_regime(hynix_1m)
    print(f"당일 regime: {regime}")

    hynix_open = float(hynix_1m["close"].iloc[0])
    hynix_close = float(hynix_1m["close"].iloc[-1])
    hynix_ret = (hynix_close / hynix_open - 1.0) * 100.0
    print(f"하이닉스 당일 수익률: {hynix_ret:+.2f}% ({hynix_open:,.0f} → {hynix_close:,.0f})")

    # ── Base scenario: 0.05% slip, 0 delay ──
    scenarios = [
        ("기본 (0s delay, 0.05% slip)", 0.05, 0),
        ("15s delay, 0.10% slip", 0.10, 15),
        ("30s delay, 0.10% slip", 0.10, 30),
    ]

    all_results = {}
    for scenario_name, slip, delay in scenarios:
        print(f"\n{'=' * 72}")
        print(f"시나리오: {scenario_name}")
        print(f"{'=' * 72}")

        results = {}

        # Strategy A
        print("  전략 A 실행 중...")
        sr_a, raw_a = _run_strategy_a(hynix_1m, long_1m, inverse_1m, slip, delay)
        # For A, use raw replay conservative data
        m_a = {
            "name": "A: weighted RANGE (프로덕션)",
            "entries": raw_a.get("entries", 0),
            "round_trips": raw_a.get("round_trips", 0),
            "lev_trades": sum(1 for e in raw_a.get("events", []) if e.get("action") == "매수" and e.get("symbol") == "레버리지"),
            "inv_trades": sum(1 for e in raw_a.get("events", []) if e.get("action") == "매수" and e.get("symbol") == "인버스"),
            "win_rate": 0.0,
            "pf": raw_a.get("profit_factor_conservative", raw_a.get("profit_factor", 0.0)),
            "net_pnl": raw_a.get("net_pnl_conservative_krw", raw_a.get("net_pnl_krw", 0.0)),
            "return_pct": raw_a.get("return_pct_conservative", raw_a.get("return_pct", 0.0)),
            "mdd_pct": raw_a.get("max_intraday_dd_pct_conservative", 0.0),
            "total_cost": 0.0,
            "cost_gross_ratio": 0.0,
            "avg_hold_sec": 0.0,
            "median_hold_sec": 0.0,
            "sub20_trips": raw_a.get("sub20_round_trips", 0),
            "wrong_direction_trades": [],
            "duplicate_blocked": raw_a.get("duplicate_episode", 0),
            "trades": sr_a.trades,
            "day_regime": raw_a.get("day_regime"),
        }
        # compute cost/win from conservative trades
        cons_trades = raw_a.get("trades_conservative", [])
        sell_trades = [t for t in cons_trades if t.get("side") == "SELL"]
        if sell_trades:
            wins_a = sum(1 for t in sell_trades if t.get("net_pnl", 0) > 0)
            m_a["win_rate"] = wins_a / len(sell_trades) * 100.0
            gross_p = sum(t.get("net_pnl", 0) for t in sell_trades if t.get("net_pnl", 0) > 0)
            gross_l = abs(sum(t.get("net_pnl", 0) for t in sell_trades if t.get("net_pnl", 0) < 0))
            m_a["pf"] = (gross_p / gross_l) if gross_l > 0 else (float("inf") if gross_p > 0 else 0.0)
            hold_secs = [t.get("held_seconds", 0) for t in sell_trades]
            m_a["avg_hold_sec"] = statistics.mean(hold_secs) if hold_secs else 0.0
            m_a["median_hold_sec"] = statistics.median(hold_secs) if hold_secs else 0.0
        results["A"] = m_a

        # Strategy B
        print("  전략 B 실행 중...")
        sr_b = _run_strategy_b(hynix_1m, long_1m, inverse_1m, slip, delay)
        results["B"] = _compute_metrics(sr_b)

        # Strategy C
        print("  전략 C 실행 중...")
        sr_c = _run_strategy_c(hynix_1m, long_1m, inverse_1m, slip, delay)
        results["C"] = _compute_metrics(sr_c)

        # Strategy D
        print("  전략 D 실행 중...")
        sr_d = _run_strategy_d(hynix_1m, long_1m, inverse_1m, slip, delay)
        results["D"] = _compute_metrics(sr_d)

        for key in ("A", "B", "C", "D"):
            _print_strategy_report(results[key])
            print("  구간별 분석:")
            _print_intraday_analysis(results[key], hynix_1m)

        all_results[scenario_name] = results

    # ── Summary comparison table ──
    print(f"\n{'=' * 72}")
    print("최종 비교 요약")
    print(f"{'=' * 72}")
    base = all_results[scenarios[0][0]]
    stress15 = all_results[scenarios[1][0]]
    stress30 = all_results[scenarios[2][0]]

    header = f"{'지표':<25s}"
    for key in ("A", "B", "C", "D"):
        header += f"  {base[key]['name'][:20]:>20s}"
    print(header)
    print("-" * (25 + 22 * 4))

    def _row(label, key_fn):
        line = f"{label:<25s}"
        for k in ("A", "B", "C", "D"):
            val = key_fn(base[k])
            line += f"  {val:>20s}"
        return line

    print(_row("보수적 Net PnL", lambda m: f"{m['net_pnl']:+,.0f}"))
    print(_row("수익률 (%)", lambda m: f"{m['return_pct']:+.3f}%"))
    pf_fmt = lambda m: f"{m['pf']:.2f}" if m['pf'] != float("inf") else "∞"
    print(_row("PF", lambda m: pf_fmt(m)))
    print(_row("MDD (%)", lambda m: f"{m['mdd_pct']:.3f}%"))
    print(_row("승률 (%)", lambda m: f"{m['win_rate']:.1f}%"))
    print(_row("진입/왕복", lambda m: f"{m['entries']}/{m['round_trips']}"))
    print(_row("비용/Gross (%)", lambda m: f"{m['cost_gross_ratio']:.1f}%"))
    print(_row("방향오류", lambda m: f"{len(m['wrong_direction_trades'])}건"))
    print(_row("20초미만 왕복", lambda m: f"{m['sub20_trips']}"))
    print(_row("평균보유 (s)", lambda m: f"{m['avg_hold_sec']:.0f}"))
    print(_row("중앙보유 (s)", lambda m: f"{m['median_hold_sec']:.0f}"))

    # Stress sensitivity
    print(f"\n{'─' * 72}")
    print("체결 지연 민감도 (Net PnL 변화)")
    print(f"{'─' * 72}")
    for key in ("A", "B", "C", "D"):
        base_pnl = base[key]["net_pnl"]
        s15_pnl = stress15[key]["net_pnl"]
        s30_pnl = stress30[key]["net_pnl"]
        d15 = s15_pnl - base_pnl
        d30 = s30_pnl - base_pnl
        print(f"  {base[key]['name'][:30]:<32s} 기본: {base_pnl:+10,.0f}  15s: {s15_pnl:+10,.0f} ({d15:+,.0f})  30s: {s30_pnl:+10,.0f} ({d30:+,.0f})")

    # ── Final recommendation ──
    print(f"\n{'=' * 72}")
    print("최종 추천")
    print(f"{'=' * 72}")

    # Score each strategy
    scores = {}
    for key in ("A", "B", "C", "D"):
        m = base[key]
        s15 = stress15[key]
        s30 = stress30[key]
        # 1. Net PnL (normalized)
        net_score = m["net_pnl"] / INITIAL_CASH * 100.0
        # 2. PF (cap at 5)
        pf_val = min(m["pf"], 5.0) if m["pf"] != float("inf") else 5.0
        # 3. MDD (less negative = better)
        mdd_score = m["mdd_pct"]  # negative
        # 4. Direction errors
        dir_score = -len(m["wrong_direction_trades"]) * 10
        # 5. Cost/overtrading
        cost_score = -m["cost_gross_ratio"] * 0.5
        # 6. Delay sensitivity (smaller degradation = better)
        delay_deg = abs(s30["net_pnl"] - m["net_pnl"]) / max(abs(m["net_pnl"]), 1.0) * 100.0
        delay_score = -delay_deg * 0.3

        total = net_score * 3.0 + pf_val * 2.0 + mdd_score * 1.5 + dir_score + cost_score + delay_score
        scores[key] = total
        print(f"  {m['name'][:30]:<32s} 종합점수: {total:+.2f}")
        print(f"    수익({net_score*3:.1f}) + PF({pf_val*2:.1f}) + MDD({mdd_score*1.5:.1f}) + 방향({dir_score:.0f}) + 비용({cost_score:.1f}) + 지연({delay_score:.1f})")

    best_key = max(scores, key=scores.get)
    print(f"\n  → 추천: {base[best_key]['name']}")
    print(f"    근거: 보수적 Net PnL, PF, MDD, 방향오류, 비용, 지연민감도 종합 최고")
    print(f"{'=' * 72}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
