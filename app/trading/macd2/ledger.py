"""MACD2 ledgers — signal_ledger + execution_ledger.

Entirely separate files from MACD v1's macd_hynix_execution_ledger.csv /
macd_hynix_signal_ledger.csv (docs §13/§17). Append-only, atomic header
init, file lock, dedup by signal_id (signal ledger) / order_id (execution
ledger). Statistics functions never raise on an empty or missing ledger —
the UI must keep rendering (docs §17).
"""
from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config
from app.utils.data_paths import LOGS_DIR

SIGNAL_LEDGER_COLUMNS = [
    "trading_date", "completed_bar_at", "signal_id", "signal_type", "direction",
    "macd", "signal", "hist_last3", "detected_at", "order_requested_at",
    "order_result", "block_reason",
    "signal_bar_at", "signal_confirmed_at", "baseline_completed_bar_at",
    "strategy_name", "strategy_version", "signal_rule", "worker_code_sha",
    "worker_instance_id", "session_started_at",
]

EXECUTION_LEDGER_COLUMNS = [
    "order_id", "signal_id", "timestamp", "mode", "symbol", "side",
    "requested_qty", "executed_qty", "requested_price", "executed_price",
    "position_before", "position_after", "gross_pnl", "fee", "slippage",
    "net_pnl", "exit_reason", "broker_response",
]

LOGS_DIR_PATH: Path = LOGS_DIR
SIGNAL_LEDGER_PATH: Path = LOGS_DIR_PATH / config.SIGNAL_LEDGER_FILENAME
EXECUTION_LEDGER_PATH: Path = LOGS_DIR_PATH / config.EXECUTION_LEDGER_FILENAME

_SIGNAL_LOCK = threading.RLock()
_EXECUTION_LOCK = threading.RLock()


def ensure_paths() -> None:
    LOGS_DIR_PATH.mkdir(parents=True, exist_ok=True)


def _append_row(path: Path, columns: list[str], row: dict[str, Any]) -> None:
    ensure_paths()
    if path.exists() and path.stat().st_size > 0:
        _ensure_columns(path, columns)
    is_new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        if is_new:
            writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in columns})


def _ensure_columns(path: Path, columns: list[str]) -> None:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        old_columns = list(reader.fieldnames or [])
        if all(col in old_columns for col in columns):
            return
        rows = list(reader)
    merged_columns = list(old_columns)
    for col in columns:
        if col not in merged_columns:
            merged_columns.append(col)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=merged_columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in merged_columns})


def _load_rows(path: Path, limit: int = 10_000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows[-limit:] if limit else rows


def load_signal_ledger(limit: int = 500) -> list[dict[str, Any]]:
    return _load_rows(SIGNAL_LEDGER_PATH, limit=limit)


def load_execution_ledger(limit: int = 500) -> list[dict[str, Any]]:
    return _load_rows(EXECUTION_LEDGER_PATH, limit=limit)


def append_signal(row: dict[str, Any]) -> bool:
    """Append one signal-ledger row. Returns False (no write) if signal_id was
    already recorded — signal_id dedup (docs §6: at most one lifetime record).
    """
    signal_id = str(row.get("signal_id") or "")
    if not signal_id:
        raise ValueError("append_signal: row is missing signal_id")
    with _SIGNAL_LOCK:
        for existing in _load_rows(SIGNAL_LEDGER_PATH):
            if existing.get("signal_id") == signal_id:
                return False
        _append_row(SIGNAL_LEDGER_PATH, SIGNAL_LEDGER_COLUMNS, row)
        return True


def append_execution(row: dict[str, Any]) -> bool:
    """Append one execution-ledger row. Returns False (no write) if order_id was
    already recorded — order_id dedup. Callers must only invoke this after KIS
    execution confirmation + position reconciliation succeeded (docs §17) —
    this function itself does not gate on that, it only prevents duplicates.
    """
    order_id = str(row.get("order_id") or "")
    if not order_id:
        raise ValueError("append_execution: row is missing order_id")
    with _EXECUTION_LOCK:
        for existing in _load_rows(EXECUTION_LEDGER_PATH):
            if existing.get("order_id") == order_id:
                return False
        _append_row(EXECUTION_LEDGER_PATH, EXECUTION_LEDGER_COLUMNS, row)
        return True


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _current_strategy_rows(
    rows: list[dict[str, Any]],
    *,
    strategy_version: Optional[str] = None,
    signal_rule: Optional[str] = None,
    session_started_at: Optional[str] = None,
    session_baseline_bar_ts: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        keep = True
        reason = ""
        if strategy_version and row.get("strategy_version") != strategy_version:
            keep, reason = False, "OLD_STRATEGY"
        if keep and signal_rule and row.get("signal_rule") != signal_rule:
            keep, reason = False, "LEGACY_INVALID"
        if keep and session_baseline_bar_ts:
            completed_at = str(row.get("completed_bar_at") or "")
            baseline_hms = session_baseline_bar_ts[11:19].replace(":", "")
            if len(completed_at) == 6 and len(baseline_hms) == 6:
                if completed_at <= baseline_hms:
                    keep, reason = False, "PRE_SESSION_SIGNAL"
        if keep:
            current.append(row)
        else:
            copy = dict(row)
            copy["excluded_reason"] = reason
            excluded.append(copy)
    return current, excluded


def summarize_signals(
    trading_date: str,
    *,
    strategy_version: Optional[str] = None,
    signal_rule: Optional[str] = None,
    session_started_at: Optional[str] = None,
    session_baseline_bar_ts: Optional[str] = None,
) -> dict[str, Any]:
    """docs §16 stats: today's UP_RED/DOWN_BLUE counts + unexecuted signals+reason.

    Never raises on an empty/missing ledger.
    """
    all_rows = [r for r in load_signal_ledger() if r.get("trading_date") == trading_date]
    rows, excluded = _current_strategy_rows(
        all_rows, strategy_version=strategy_version, signal_rule=signal_rule,
        session_started_at=session_started_at, session_baseline_bar_ts=session_baseline_bar_ts,
    )
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(str(row.get("signal_id") or ""), row)
    rows = sorted([row for sid, row in unique.items() if sid], key=lambda r: str(r.get("completed_bar_at") or ""))
    onset_rows: list[dict[str, Any]] = []
    last_direction = ""
    for row in rows:
        direction = str(row.get("direction") or "")
        if direction and direction != last_direction:
            onset_rows.append(row)
            last_direction = direction
    red_count = sum(1 for r in onset_rows if r.get("direction") == "UP_RED")
    blue_count = sum(1 for r in onset_rows if r.get("direction") == "DOWN_BLUE")
    unexecuted = [
        {"signal_id": r.get("signal_id"), "direction": r.get("direction"), "reason": r.get("block_reason")}
        for r in rows
        if not str(r.get("order_result") or "").strip() or str(r.get("order_result")).upper() in ("BLOCKED", "FAILED")
    ]
    return {
        "trading_date": trading_date,
        "red_count": red_count,
        "blue_count": blue_count,
        "signal_count": len(onset_rows),
        "unexecuted_signals": unexecuted,
        "excluded_signals": excluded,
        "latest_signal_id": onset_rows[-1].get("signal_id") if onset_rows else None,
        "current_signal_ids": [r.get("signal_id") for r in onset_rows if r.get("signal_id")],
        "onset_signals": onset_rows,
    }


def summarize_daily_trading(
    trading_date: str,
    budget: float = config.DEFAULT_BUDGET,
    *,
    signal_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """docs §16/§17 stats: buys/sells, completed round trips, gross/cost/net,
    return%, win rate, profit factor, max drawdown. Never raises on an empty
    or missing execution ledger — an empty ledger produces a well-formed
    zeroed result (UI must keep rendering).
    """
    rows = [r for r in load_execution_ledger() if str(r.get("timestamp") or "").startswith(trading_date)]
    if signal_ids is not None:
        rows = [r for r in rows if str(r.get("signal_id") or "") in signal_ids]
    budget_f = float(budget or config.DEFAULT_BUDGET)

    empty: dict[str, Any] = {
        "trading_date": trading_date,
        "has_data": False,
        "buy_count": 0,
        "sell_count": 0,
        "round_trip_count": 0,
        "gross_pnl": 0.0,
        "total_cost": 0.0,
        "net_pnl": 0.0,
        "return_pct": 0.0,
        "win_rate_pct": 0.0,
        "profit_factor": None,
        "max_drawdown": 0.0,
        "budget": budget_f,
    }
    if not rows:
        return empty

    buy_rows = [r for r in rows if str(r.get("side") or "").upper() == "BUY"]
    sell_rows = [r for r in rows if str(r.get("side") or "").upper() == "SELL"]
    net_values = [_float(r.get("net_pnl")) for r in sell_rows]  # PnL realizes on SELL rows

    gross_pnl = sum(_float(r.get("gross_pnl")) for r in rows)
    total_cost = sum(_float(r.get("fee")) for r in rows)
    net_pnl = sum(net_values)
    wins = [v for v in net_values if v > 0]
    losses = [v for v in net_values if v < 0]
    win_rate = (len(wins) / len(net_values) * 100.0) if net_values else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (None if not wins else float("inf"))

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for v in net_values:
        equity += v
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "trading_date": trading_date,
        "has_data": True,
        "buy_count": len(buy_rows),
        "sell_count": len(sell_rows),
        "round_trip_count": len(sell_rows),
        "gross_pnl": round(gross_pnl, 2),
        "total_cost": round(total_cost, 2),
        "net_pnl": round(net_pnl, 2),
        "return_pct": round((net_pnl / budget_f * 100.0) if budget_f else 0.0, 4),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4) if isinstance(profit_factor, float) and profit_factor not in (float("inf"),) else profit_factor,
        "max_drawdown": round(max_dd, 2),
        "budget": budget_f,
    }
