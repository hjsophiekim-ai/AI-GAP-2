#!/usr/bin/env python
"""Read-only Hybrid MAJOR_FLAG 3-day trade simulation (2026-07-28~30).

- Collects real KIS 1m bars for 000660 / 0193T0 / 0197X0 (plus 000660 prior-day warm-up)
- Scores every confirmed MACD crossover with MAJOR_FILTER_HYBRID_V1
- Simulates entries/exits with MACD2 SL / Profit Lock / 14:55 cutoff / 15:00 force exit
- Fill price = next 1m open after signal confirmation (completed 3m bar close)
- Never touches operational state / ledger / cache, never calls order APIs
- Does not modify macd2 operational modules

Usage:
  python scripts/simulate_major_filter_3days.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.kis_client import create_kis_client
from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import apply_major_trade_gates, evaluate_major_flag
from app.trading.macd2.models import Direction
from app.trading.macd2.risk_exit import evaluate_position_exits
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m
from app.trading.trading_cost_engine import TradeCostEngine

KST = config.KST
WATCH = config.WATCH_SYMBOL
LONG = config.LONG_SYMBOL
INV = config.INVERSE_SYMBOL
SYMBOLS = (WATCH, LONG, INV)

DATES = ("20260728", "20260729", "20260730")
BUDGET = 10_000_000.0
KIS_PAGE_SIZE = 120
KIS_MAX_PAGES = 20
PAGE_PACING_SEC = float(getattr(config, "KIS_PAGE_FETCH_PACING_SEC", 0.4))
RETRY = int(getattr(config, "PRIOR_DAY_FETCH_RETRIES", 5))
RETRY_DELAY = float(getattr(config, "PRIOR_DAY_FETCH_RETRY_DELAY_SEC", 2.0))

# Reporting-only KIS chart labels (not approval truth).
KIS_REFERENCE_FLAGS = (
    ("20260728", "11:06", "UP_RED"),
    ("20260728", "13:09", "DOWN_BLUE"),
    ("20260728", "13:42", "UP_RED"),
    ("20260728", "14:18", "DOWN_BLUE"),
    ("20260729", "09:27", "DOWN_BLUE"),
    ("20260729", "12:39", "UP_RED"),
    ("20260730", "09:54", "UP_RED"),
    ("20260730", "11:00", "DOWN_BLUE"),
    ("20260730", "12:27", "UP_RED"),
    ("20260730", "13:09", "DOWN_BLUE"),
)


def _out_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    rows = []
    for c in candles or []:
        date_raw = str(c.get("date") or "").strip()
        time_raw = str(c.get("time") or "").strip().replace(":", "")
        if len(date_raw) != 8 or len(time_raw) < 6:
            continue
        try:
            dt = datetime.strptime(f"{date_raw}{time_raw[:6]}", "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            continue
        rows.append({
            "datetime": dt,
            "open": float(c.get("open") or 0),
            "high": float(c.get("high") or 0),
            "low": float(c.get("low") or 0),
            "close": float(c.get("close") or 0),
            "volume": float(c.get("volume") or 0),
        })
    if not rows:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    return df


def fetch_day_1m(client, symbol: str, date_ymd: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Page through 주식일별분봉조회 for one symbol/date. Read-only quotes API."""
    pages: list[pd.DataFrame] = []
    page_diags: list[dict[str, Any]] = []
    hour1 = ""
    prev_count = 0
    for page_i in range(KIS_MAX_PAGES):
        if page_i > 0:
            time.sleep(PAGE_PACING_SEC)
        part = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        err = None
        for retry_i in range(RETRY):
            try:
                candles = client.get_minute_candles_for_date(
                    symbol, date_ymd, period_min=1, count=KIS_PAGE_SIZE, hour1=hour1,
                ) or []
                part = _candles_to_df(candles)
                err = None
                if not part.empty or retry_i == RETRY - 1:
                    break
            except Exception as exc:
                err = repr(exc)
                part = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
            if retry_i < RETRY - 1:
                time.sleep(RETRY_DELAY)
        page_diags.append({
            "page": page_i + 1, "hour1": hour1 or "LATEST",
            "received": int(len(part)), "error": err,
        })
        if part.empty:
            break
        pages.append(part)
        merged = (
            pd.concat(pages, ignore_index=True)
            .drop_duplicates("datetime", keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        # Keep only requested date
        merged = merged[merged["datetime"].dt.strftime("%Y%m%d") == date_ymd].reset_index(drop=True)
        if len(merged) <= prev_count:
            break
        prev_count = len(merged)
        oldest = merged["datetime"].iloc[0]
        next_hour1 = (oldest - timedelta(minutes=1)).strftime("%H%M%S")
        if next_hour1 == hour1:
            break
        hour1 = next_hour1
        pages = [merged]

    df = pages[-1] if pages else pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    if not df.empty:
        df = df[df["datetime"].dt.strftime("%Y%m%d") == date_ymd].reset_index(drop=True)
    return df, {"symbol": symbol, "date": date_ymd, "rows": int(len(df)), "pages": page_diags}


def prior_weekday_candidates(before_ymd: str, n: int = 8) -> list[str]:
    d = datetime.strptime(before_ymd, "%Y%m%d").date()
    out: list[str] = []
    guard = 0
    while len(out) < n and guard < n * 3:
        d = d - timedelta(days=1)
        guard += 1
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return out


def collect_all(client, raw_dir: Path) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Collect required bars. Writes only under validation raw_dir."""
    _out_dir(raw_dir)
    data: dict[str, pd.DataFrame] = {}
    report: dict[str, Any] = {"fetched": [], "missing": [], "warmup_date": None}
    # Warm-up for 000660: first prior trading day with data before 20260728
    warmup_df = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    for cand in prior_weekday_candidates(DATES[0]):
        print(f"[collect] warm-up probe {WATCH} {cand} ...")
        df, diag = fetch_day_1m(client, WATCH, cand)
        report["fetched"].append(diag)
        path = raw_dir / f"{WATCH}_{cand}_1m.csv"
        if not df.empty:
            df.to_csv(path, index=False)
            warmup_df = df
            report["warmup_date"] = cand
            print(f"  -> {len(df)} rows (warmup={cand})")
            break
        print(f"  -> empty")
        report["missing"].append({"symbol": WATCH, "date": cand, "role": "warmup_candidate"})
    if warmup_df.empty:
        report["missing"].append({"symbol": WATCH, "date": f"<{DATES[0]}", "role": "warmup"})

    for date in DATES:
        for symbol in SYMBOLS:
            print(f"[collect] {symbol} {date} ...")
            df, diag = fetch_day_1m(client, symbol, date)
            report["fetched"].append(diag)
            path = raw_dir / f"{symbol}_{date}_1m.csv"
            if df.empty:
                report["missing"].append({"symbol": symbol, "date": date, "role": "session"})
                print(f"  -> EMPTY")
            else:
                df.to_csv(path, index=False)
                data[f"{symbol}_{date}"] = df
                print(f"  -> {len(df)} rows saved {path.name}")
            time.sleep(PAGE_PACING_SEC)

    if not warmup_df.empty:
        data[f"{WATCH}_warmup"] = warmup_df
    return data, report


def load_from_raw(raw_dir: Path) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    data: dict[str, pd.DataFrame] = {}
    report: dict[str, Any] = {"fetched": [], "missing": [], "warmup_date": None, "source": "disk"}
    for date in DATES:
        for symbol in SYMBOLS:
            path = raw_dir / f"{symbol}_{date}_1m.csv"
            if not path.exists():
                report["missing"].append({"symbol": symbol, "date": date, "role": "session"})
                continue
            df = pd.read_csv(path)
            df["datetime"] = pd.to_datetime(df["datetime"], utc=False)
            if df["datetime"].dt.tz is None:
                df["datetime"] = df["datetime"].dt.tz_localize(KST)
            else:
                df["datetime"] = df["datetime"].dt.tz_convert(KST)
            data[f"{symbol}_{date}"] = df
            report["fetched"].append({"symbol": symbol, "date": date, "rows": len(df), "path": str(path)})
    # warmup: any prior watch file
    warm_files = sorted(raw_dir.glob(f"{WATCH}_*_1m.csv"))
    warm_files = [p for p in warm_files if p.stem.split("_")[1] < DATES[0]]
    if warm_files:
        p = warm_files[-1]
        df = pd.read_csv(p)
        df["datetime"] = pd.to_datetime(df["datetime"], utc=False)
        if df["datetime"].dt.tz is None:
            df["datetime"] = df["datetime"].dt.tz_localize(KST)
        else:
            df["datetime"] = df["datetime"].dt.tz_convert(KST)
        data[f"{WATCH}_warmup"] = df
        report["warmup_date"] = p.stem.split("_")[1]
    else:
        report["missing"].append({"symbol": WATCH, "date": f"<{DATES[0]}", "role": "warmup"})
    return data, report


def _target_symbol(direction: Direction) -> str:
    return LONG if direction == Direction.UP_RED else INV


def _direction_for_symbol(symbol: str) -> Optional[Direction]:
    if symbol == LONG:
        return Direction.UP_RED
    if symbol == INV:
        return Direction.DOWN_BLUE
    return None


def _next_1m_open(etf_1m: pd.DataFrame, at_or_after: datetime) -> Optional[tuple[datetime, float]]:
    """First 1m bar with datetime >= at_or_after; return (bar_dt, open)."""
    if etf_1m is None or etf_1m.empty:
        return None
    sub = etf_1m[etf_1m["datetime"] >= at_or_after]
    if sub.empty:
        return None
    row = sub.iloc[0]
    return pd.Timestamp(row["datetime"]).to_pydatetime(), float(row["open"])


def _etf_price_at(etf_1m: pd.DataFrame, when: datetime, field: str = "close") -> Optional[float]:
    sub = etf_1m[etf_1m["datetime"] <= when]
    if sub.empty:
        return None
    return float(sub.iloc[-1][field])


def _qty_for_budget(price: float, budget: float = BUDGET) -> int:
    if price <= 0:
        return 0
    # Mirror MACD2-ish safety: leave ~0.5% for fees
    return int((budget * 0.995) // price)


def _net_return_pct(engine: TradeCostEngine, symbol: str, entry: float, current: float, qty: int) -> float:
    if entry <= 0 or qty <= 0 or current <= 0:
        return 0.0
    cost = engine.compute_net_pnl(symbol, entry, current, qty, buy_order_type="market", sell_order_type="market")
    return float(cost["net_pnl"]) / (entry * qty) * 100.0


@dataclass
class Position:
    symbol: str
    direction: Direction
    qty: int
    entry_price: float
    entry_at: datetime
    signal_bar_at: datetime
    order_at: datetime
    peak_net: float = 0.0
    profit_lock_active: bool = False


@dataclass
class SimState:
    position: Optional[Position] = None
    daily_entries: int = 0
    last_entry_at: Optional[datetime] = None
    last_exit_at: Optional[datetime] = None
    last_exit_direction: Optional[Direction] = None
    last_detected: Optional[Direction] = None
    equity_curve: list[float] = field(default_factory=list)
    realized_net_cum: float = 0.0


def simulate(data: dict[str, pd.DataFrame]) -> tuple[list[dict], list[dict], dict[str, Any]]:
    engine = TradeCostEngine()
    cost_cfg = dict(engine._cfg)

    # Build continuous 000660 series: warmup + each day
    watch_parts = []
    if f"{WATCH}_warmup" in data:
        watch_parts.append(data[f"{WATCH}_warmup"])
    for date in DATES:
        key = f"{WATCH}_{date}"
        if key not in data:
            continue
        watch_parts.append(data[key])
    if not watch_parts:
        return [], [], {"error": "no_watch_bars", "cost_config": cost_cfg}

    watch_all = (
        pd.concat(watch_parts, ignore_index=True)
        .drop_duplicates("datetime", keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )

    # ETF maps by date
    etf_by_date: dict[str, dict[str, pd.DataFrame]] = {}
    for date in DATES:
        etf_by_date[date] = {}
        for sym in (LONG, INV):
            key = f"{sym}_{date}"
            if key in data:
                etf_by_date[date][sym] = data[key]

    flag_rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    state = SimState()

    # Walk day by day so baseline resets correctly and we never peek next day
    for date in DATES:
        key = f"{WATCH}_{date}"
        if key not in data:
            continue
        day_watch = data[key]
        # History available up to end of this day only (+ warmup + prior sim days)
        hist_end = day_watch["datetime"].iloc[-1].to_pydatetime()
        # Include bars through this day from watch_all
        hist = watch_all[watch_all["datetime"] <= hist_end].copy()
        now_end = hist_end.replace(hour=15, minute=33, second=0, microsecond=0)
        bars_3m = resample_completed_3m(hist, now=now_end)
        if bars_3m.empty:
            continue

        # Indices belonging to this trading date
        day_mask = bars_3m["datetime"].dt.tz_convert(KST).dt.strftime("%Y%m%d") == date
        day_indices = list(bars_3m.index[day_mask])
        if not day_indices:
            continue

        state.daily_entries = 0
        state.last_detected = None
        first_idx = day_indices[0]

        # Also need 1m ETF series for the day for mark-to-market between signals
        long_1m = etf_by_date[date].get(LONG, pd.DataFrame())
        inv_1m = etf_by_date[date].get(INV, pd.DataFrame())

        def etf_df(symbol: str) -> pd.DataFrame:
            return long_1m if symbol == LONG else inv_1m

        # Build timeline of completed bar confirmations for this day
        events: list[tuple[datetime, str, Any]] = []
        for i in day_indices:
            bar_dt = pd.Timestamp(bars_3m.loc[i, "datetime"]).to_pydatetime().astimezone(KST)
            confirm_at = bar_dt + timedelta(minutes=3)
            events.append((confirm_at, "bar", i))

        # 1m marks for open position management (after each ETF minute)
        # Use LONG union INV datetimes for the day
        mark_times = set()
        for df in (long_1m, inv_1m):
            if not df.empty:
                for t in df["datetime"]:
                    mark_times.add(pd.Timestamp(t).to_pydatetime().astimezone(KST))
        for t in sorted(mark_times):
            events.append((t, "mark", None))
        events.append((datetime.strptime(date + "150000", "%Y%m%d%H%M%S").replace(tzinfo=KST), "force", None))
        events.sort(key=lambda x: (x[0], 0 if x[1] == "bar" else 1 if x[1] == "mark" else 2))

        processed_bar_idxs: set[int] = set()

        def close_position(exit_at: datetime, exit_price: float, reason: str, related_flag: str = "") -> None:
            nonlocal state
            pos = state.position
            if pos is None:
                return
            pnl = engine.compute_net_pnl(
                pos.symbol, pos.entry_price, exit_price, pos.qty,
                buy_order_type="market", sell_order_type="market",
            )
            ret_pct = float(pnl["net_pnl"]) / BUDGET * 100.0
            trades.append({
                "trading_date": date,
                "direction": pos.direction.value,
                "symbol": pos.symbol,
                "signal_bar_at": pos.signal_bar_at.isoformat(),
                "order_at": pos.order_at.isoformat(),
                "entry_price": pos.entry_price,
                "exit_at": exit_at.isoformat(),
                "exit_price": exit_price,
                "quantity": pos.qty,
                "exit_reason": reason,
                "gross_pnl": pnl["gross_pnl"],
                "total_cost": pnl["total_cost"],
                "net_pnl": pnl["net_pnl"],
                "return_pct_budget": round(ret_pct, 4),
                "related_flag": related_flag,
                "buy_fee": pnl["buy_fee"],
                "sell_fee": pnl["sell_fee"],
                "slippage": pnl["slippage"],
                "transaction_tax": pnl["transaction_tax"],
            })
            state.realized_net_cum += float(pnl["net_pnl"])
            state.equity_curve.append(state.realized_net_cum)
            state.last_exit_at = exit_at
            state.last_exit_direction = pos.direction
            state.position = None

        for when, kind, payload in events:
            # Force liquidation
            if kind == "force":
                if state.position is not None:
                    px = _etf_price_at(etf_df(state.position.symbol), when, "close")
                    if px is None:
                        fill = _next_1m_open(etf_df(state.position.symbol), when - timedelta(minutes=1))
                        px = fill[1] if fill else None
                    if px is not None:
                        close_position(when, px, config.EXIT_FORCED_LIQUIDATION)
                continue

            if kind == "mark" and state.position is not None:
                pos = state.position
                # Only mark with bars after entry
                if when <= pos.order_at:
                    continue
                px = _etf_price_at(etf_df(pos.symbol), when, "close")
                if px is None:
                    continue
                net = _net_return_pct(engine, pos.symbol, pos.entry_price, px, pos.qty)
                decision = evaluate_position_exits(
                    current_net_return=net,
                    peak_net_return=pos.peak_net,
                    profit_lock_active=pos.profit_lock_active,
                )
                pos.peak_net = decision.peak_net_return
                pos.profit_lock_active = decision.profit_lock_active
                if decision.exit_reason == config.EXIT_STOP_LOSS:
                    close_position(when, px, config.EXIT_STOP_LOSS)
                elif decision.exit_reason == config.EXIT_PROFIT_LOCK:
                    close_position(when, px, config.EXIT_PROFIT_LOCK)
                continue

            if kind != "bar":
                continue
            i = int(payload)
            if i in processed_bar_idxs:
                continue
            processed_bar_idxs.add(i)

            # Bars available ONLY up to index i (no future)
            bars_upto = bars_3m.iloc[: i + 1].copy()
            snap = calculate_macd(bars_upto)
            if snap is None:
                continue
            bar_dt = snap.bar_dt.astimezone(KST)
            confirm_at = bar_dt + timedelta(minutes=3)
            # Sanity: confirm_at should match event time
            if i == first_idx:
                # baseline — no signal
                state.last_detected = None
                continue

            direction = evaluate_macd_crossover(snap, state.last_detected)
            if direction == Direction.HOLD:
                continue
            state.last_detected = direction

            pos_dir = state.position.direction if state.position else None
            last_entry = state.position.entry_at if state.position else state.last_entry_at
            decision = evaluate_major_flag(
                bars_upto, direction, pos_dir, last_entry, state.daily_entries, confirm_at,
            )
            same_exit = state.last_exit_at if state.last_exit_direction == direction else None
            decision = apply_major_trade_gates(
                decision,
                flag_direction=direction,
                position_direction=pos_dir,
                last_entry_at=last_entry,
                last_same_direction_exit_at=same_exit,
                daily_major_entry_count=state.daily_entries,
                now=confirm_at,
            )

            comps = dict(decision.component_scores or {})
            metrics = dict(decision.metrics or {})
            flag_row = {
                "trading_date": date,
                "flag_time": bar_dt.strftime("%H:%M"),
                "flag_bar_at": bar_dt.isoformat(),
                "confirmed_at": confirm_at.isoformat(),
                "direction": direction.value,
                "score": decision.score,
                "required_score": decision.required_score,
                "approved": bool(decision.approved),
                "decision": decision.decision,
                "block_reason": decision.block_reason or "",
                "reasons": "|".join(decision.reasons),
                "is_reversal": decision.is_reversal,
                "fast_reversal": decision.fast_reversal,
                "sim_position_before": pos_dir.value if pos_dir else "",
                "daily_entries_before": state.daily_entries,
                **{f"score_{k}": v for k, v in comps.items()},
                **{f"m_{k}": metrics.get(k) for k in (
                    "hist_impulse_atr", "breakout", "price_impulse_atr", "body_atr",
                    "volume_ratio", "ema10_ok", "ema20_or_vwap_ok", "recent_range_ratio",
                    "ema_spread_ratio", "atr14", "macd", "signal", "hist", "close",
                )},
            }

            # Entry window
            entry_cutoff = datetime.strptime(date + "145500", "%Y%m%d%H%M%S").replace(tzinfo=KST)
            can_enter = confirm_at < entry_cutoff

            acted = ""
            if decision.approved and can_enter:
                target = _target_symbol(direction)
                # Same-direction already gated; opposite or flat
                if state.position is not None and state.position.direction != direction:
                    # Exit current at next 1m open of held symbol
                    fill_exit = _next_1m_open(etf_df(state.position.symbol), confirm_at)
                    if fill_exit is None:
                        flag_row["trade_action"] = "APPROVED_BUT_NO_EXIT_FILL"
                        flag_rows.append(flag_row)
                        continue
                    close_position(fill_exit[0], fill_exit[1], config.EXIT_OPPOSITE_SIGNAL,
                                   related_flag=f"{date}_{bar_dt.strftime('%H%M')}_{direction.value}")

                if state.position is None:
                    fill = _next_1m_open(etf_df(target), confirm_at)
                    if fill is None:
                        flag_row["trade_action"] = "APPROVED_BUT_NO_ENTRY_FILL"
                        flag_rows.append(flag_row)
                        continue
                    order_at, entry_px = fill
                    # Apply buy slippage to effective entry for costing via engine at exit;
                    # store raw open as entry_price (engine adds costs on round-trip).
                    qty = _qty_for_budget(entry_px)
                    if qty <= 0:
                        flag_row["trade_action"] = "APPROVED_BUT_QTY0"
                        flag_rows.append(flag_row)
                        continue
                    state.position = Position(
                        symbol=target,
                        direction=direction,
                        qty=qty,
                        entry_price=entry_px,
                        entry_at=order_at,
                        signal_bar_at=bar_dt,
                        order_at=order_at,
                    )
                    state.daily_entries += 1
                    state.last_entry_at = order_at
                    acted = f"ENTER {target} qty={qty} @{entry_px} at {order_at.strftime('%H:%M')}"
            elif decision.approved and not can_enter:
                acted = "APPROVED_BUT_AFTER_1455"
            else:
                acted = "FILTERED_OUT"

            flag_row["trade_action"] = acted
            flag_rows.append(flag_row)

        # End of day force if still holding (safety)
        if state.position is not None:
            force_at = datetime.strptime(date + "150000", "%Y%m%d%H%M%S").replace(tzinfo=KST)
            px = _etf_price_at(etf_df(state.position.symbol), force_at, "close")
            if px is not None:
                close_position(force_at, px, config.EXIT_FORCED_LIQUIDATION)

    # Max drawdown on equity curve
    peak = 0.0
    max_dd = 0.0
    for v in state.equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)

    # Daily stats
    by_date: dict[str, Any] = {}
    trades_df = pd.DataFrame(trades)
    for date in DATES:
        sub = trades_df[trades_df["trading_date"] == date] if not trades_df.empty else pd.DataFrame()
        if sub.empty:
            by_date[date] = {"trades": 0, "wins": 0, "win_rate_pct": 0.0, "net_pnl": 0.0, "return_pct_budget": 0.0}
            continue
        wins = int((sub["net_pnl"] > 0).sum())
        net = float(sub["net_pnl"].sum())
        by_date[date] = {
            "trades": int(len(sub)),
            "wins": wins,
            "win_rate_pct": round(wins / len(sub) * 100.0, 2),
            "net_pnl": round(net, 2),
            "return_pct_budget": round(net / BUDGET * 100.0, 4),
        }

    # Flag vs KIS reference
    flags_df = pd.DataFrame(flag_rows)
    ref_cmp = []
    prog_keys = set()
    if not flags_df.empty:
        for _, r in flags_df.iterrows():
            prog_keys.add((r["trading_date"], r["flag_time"], r["direction"]))
    for d, hm, direction in KIS_REFERENCE_FLAGS:
        key = (d, hm, direction)
        if key in prog_keys:
            row = flags_df[(flags_df["trading_date"] == d) & (flags_df["flag_time"] == hm) & (flags_df["direction"] == direction)].iloc[0]
            ref_cmp.append({
                "trading_date": d, "flag_time": hm, "direction": direction,
                "in_program": True,
                "approved": bool(row["approved"]),
                "score": float(row["score"]),
                "block_reason": row.get("block_reason") or "",
            })
        else:
            ref_cmp.append({
                "trading_date": d, "flag_time": hm, "direction": direction,
                "in_program": False, "approved": False, "score": None, "block_reason": "NOT_IN_PROGRAM_FLAGS",
            })
    extra_prog = []
    ref_set = {(d, hm, direction) for d, hm, direction in KIS_REFERENCE_FLAGS}
    if not flags_df.empty:
        for _, r in flags_df.iterrows():
            key = (r["trading_date"], r["flag_time"], r["direction"])
            if key not in ref_set:
                extra_prog.append({
                    "trading_date": r["trading_date"], "flag_time": r["flag_time"],
                    "direction": r["direction"], "approved": bool(r["approved"]),
                    "score": float(r["score"]), "block_reason": r.get("block_reason") or "",
                })

    summary = {
        "filter_version": config.MAJOR_FILTER_VERSION,
        "dates": list(DATES),
        "budget": BUDGET,
        "cost_config": cost_cfg,
        "confirmed_flags": int(len(flag_rows)),
        "approved_flags": int(sum(1 for r in flag_rows if r["approved"])),
        "trades": int(len(trades)),
        "by_date": by_date,
        "total_gross_pnl": round(float(trades_df["gross_pnl"].sum()), 2) if not trades_df.empty else 0.0,
        "total_cost": round(float(trades_df["total_cost"].sum()), 2) if not trades_df.empty else 0.0,
        "total_net_pnl": round(float(trades_df["net_pnl"].sum()), 2) if not trades_df.empty else 0.0,
        "total_return_pct_budget": round(float(trades_df["net_pnl"].sum()) / BUDGET * 100.0, 4) if not trades_df.empty else 0.0,
        "max_drawdown_krw": round(max_dd, 2),
        "kis_reference_comparison": ref_cmp,
        "program_flags_not_in_kis_reference": extra_prog,
        "rules": {
            "entry_score": config.MAJOR_ENTRY_SCORE_MIN,
            "reversal_score": config.MAJOR_REVERSAL_SCORE_MIN,
            "fast_reversal_score": config.MAJOR_FAST_REVERSAL_SCORE_MIN,
            "max_daily_entries": config.MAJOR_MAX_DAILY_ENTRIES,
            "stop_loss_pct": config.STOP_LOSS_NET_PCT,
            "profit_lock_activate": config.PROFIT_LOCK_ACTIVATE_NET_PCT,
            "profit_lock_giveback": config.PROFIT_LOCK_GIVEBACK_PP,
            "entry_cutoff": str(config.NEW_ENTRY_CUTOFF),
            "force_liquidate": str(config.FORCE_LIQUIDATE_AT),
            "fill": "next_1m_open_after_3m_confirm",
        },
    }
    return flag_rows, trades, summary


def print_tables(flags: list[dict], trades: list[dict], summary: dict[str, Any], collect_report: dict[str, Any]) -> None:
    print("\n========== DATA COLLECTION ==========")
    print(f"warmup_date={collect_report.get('warmup_date')}")
    if collect_report.get("missing"):
        print("MISSING:")
        for m in collect_report["missing"]:
            print(f"  - {m}")
    else:
        print("missing: none")

    print("\n========== 1. ALL FLAGS (score / pass-fail) ==========")
    if not flags:
        print("(no flags)")
    else:
        cols = ["trading_date", "flag_time", "direction", "score", "required_score", "approved", "block_reason", "trade_action"]
        df = pd.DataFrame(flags)
        print(df[cols].to_string(index=False))

    print("\n========== 2. TRADES ==========")
    if not trades:
        print("(no trades)")
    else:
        cols = [
            "trading_date", "direction", "symbol", "signal_bar_at", "order_at",
            "entry_price", "exit_at", "exit_price", "quantity", "exit_reason",
            "gross_pnl", "total_cost", "net_pnl", "return_pct_budget",
        ]
        df = pd.DataFrame(trades)
        print(df[cols].to_string(index=False))

    print("\n========== 3-5. DAILY / TOTAL / MDD ==========")
    print(json.dumps(summary.get("by_date"), ensure_ascii=False, indent=2))
    print(
        f"total_gross={summary.get('total_gross_pnl')} "
        f"total_cost={summary.get('total_cost')} "
        f"total_net={summary.get('total_net_pnl')} "
        f"return%={summary.get('total_return_pct_budget')} "
        f"max_dd={summary.get('max_drawdown_krw')}"
    )

    print("\n========== 6. KIS vs PROGRAM FLAGS ==========")
    print("reference (KIS chart labels) vs program:")
    print(json.dumps(summary.get("kis_reference_comparison"), ensure_ascii=False, indent=2))
    print("program flags not in reference:")
    print(json.dumps(summary.get("program_flags_not_in_kis_reference"), ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="data/validation/major_filter_3days")
    ap.add_argument("--skip-fetch", action="store_true", help="Use already-fetched CSVs under output-dir/raw")
    ap.add_argument("--kis-mode", default="real", help="KIS client mode for date-scoped 1m API (default real)")
    args = ap.parse_args()

    out_dir = _out_dir(Path(args.output_dir))
    raw_dir = _out_dir(out_dir / "raw")

    # Guard: do not touch operational paths
    forbidden_touch = [
        ROOT / "data" / "state" / "macd2_runtime.json",
        ROOT / "data" / "logs" / config.SIGNAL_LEDGER_FILENAME,
        ROOT / "data" / "logs" / config.EXECUTION_LEDGER_FILENAME,
    ]
    before_mtime = {p: (p.stat().st_mtime if p.exists() else None) for p in forbidden_touch}

    if args.skip_fetch:
        data, collect_report = load_from_raw(raw_dir)
    else:
        client = create_kis_client(args.kis_mode)
        if client is None:
            print(f"ERROR: create_kis_client({args.kis_mode!r}) returned None — cannot fetch KIS 1m bars.")
            print("Provide credentials or re-run with --skip-fetch after placing CSVs in raw/.")
            return 2
        data, collect_report = collect_all(client, raw_dir)

    # Require all session days for all symbols
    missing_session = [m for m in collect_report.get("missing", []) if m.get("role") == "session"]
    if missing_session:
        print("ERROR: insufficient data — will not invent prices.")
        print(json.dumps(missing_session, ensure_ascii=False, indent=2))
        (out_dir / "summary.json").write_text(
            json.dumps({"error": "insufficient_data", "missing": missing_session, "collect": collect_report},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print_tables([], [], {"by_date": {}, "kis_reference_comparison": [], "program_flags_not_in_kis_reference": []}, collect_report)
        return 2

    if f"{WATCH}_warmup" not in data:
        print("ERROR: 000660 prior-day warm-up missing — EMA seed would be wrong; refuse to estimate.")
        (out_dir / "summary.json").write_text(
            json.dumps({"error": "warmup_missing", "collect": collect_report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 2

    flags, trades, summary = simulate(data)
    summary["collect"] = collect_report

    pd.DataFrame(flags).to_csv(out_dir / "all_flags.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(trades).to_csv(out_dir / "trades.csv", index=False, encoding="utf-8-sig")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print_tables(flags, trades, summary, collect_report)

    after_mtime = {p: (p.stat().st_mtime if p.exists() else None) for p in forbidden_touch}
    if before_mtime != after_mtime:
        print("WARNING: operational file mtime changed — unexpected")
        return 3
    print("\n[ok] operational state/ledger untouched; outputs under", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
