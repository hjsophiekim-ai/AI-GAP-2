"""MU_MACD ledgers — mu_macd_signal_ledger.csv + mu_macd_execution_ledger.csv
ONLY. Entirely separate files from macd2's/tsla_auto's ledgers (own
filenames from config.py, same LOGS_DIR root). Append-only, atomic header
init, file lock, dedup by signal_id (signal ledger) / order_id (execution
ledger) — same technique as app.trading.macd2.ledger.
"""
from __future__ import annotations

import csv
import os
import threading
from pathlib import Path
from typing import Any, Optional

from app.trading.mu_macd import config
from app.utils.data_paths import LOGS_DIR

SIGNAL_LEDGER_COLUMNS = [
    "trading_date", "bar_start_at", "confirmed_at", "signal_id", "signal_type", "direction",
    "macd", "signal", "hist", "detected_at",
    "order_requested_at", "order_result", "block_reason",
    "strategy_name", "strategy_version", "signal_rule",
    "worker_instance_id", "session_started_at",
    "ws_connected", "ws_last_tick_at", "ws_last_error",
    "warmup_bars_3m_count", "warmup_ready",
    "position_reconcile", "executor_called",
    "broker_called", "broker_order_id", "broker_rt_cd", "broker_msg_cd", "broker_msg1",
    "final_qty", "final_result",
    # Optional "시간대별 최적거래 필터" fields (appended 2026-08-15; never
    # rename/delete older cols).
    "time_window_filter_enabled", "time_window_score", "time_window_decision",
    "time_window_block_reason",
]

EXECUTION_LEDGER_COLUMNS = [
    "timestamp", "signal_id", "order_id", "symbol", "side", "requested_qty", "executed_qty",
    "requested_price", "executed_price", "success", "exit_reason",
    "gross_pnl", "net_pnl", "fee", "tax",
    "position_before", "position_after", "strategy_name", "strategy_version",
]

LOGS_DIR_PATH: Path = LOGS_DIR
SIGNAL_LEDGER_PATH: Path = LOGS_DIR_PATH / config.SIGNAL_LEDGER_FILENAME
EXECUTION_LEDGER_PATH: Path = LOGS_DIR_PATH / config.EXECUTION_LEDGER_FILENAME

# Frozen at import time -- see app.trading.macd2.ledger's identical pattern
# and its own docstring for the 2026-08-19 incident (an ad-hoc macd2 replay
# script wrote synthetic rows into the real production ledger) this guards
# against, ported here for the same risk in MU_MACD.
_DEFAULT_SIGNAL_LEDGER_PATH: Path = SIGNAL_LEDGER_PATH
_DEFAULT_EXECUTION_LEDGER_PATH: Path = EXECUTION_LEDGER_PATH
LIVE_WORKER_MARKER_ENV = "MU_MACD_LIVE_WORKER_PID"


def _assert_safe_to_write_ledger() -> None:
    paths_untouched = (
        SIGNAL_LEDGER_PATH == _DEFAULT_SIGNAL_LEDGER_PATH
        and EXECUTION_LEDGER_PATH == _DEFAULT_EXECUTION_LEDGER_PATH
    )
    if not paths_untouched:
        return  # already redirected elsewhere (pytest conftest, an isolated script) -- safe
    if os.environ.get(LIVE_WORKER_MARKER_ENV) == str(os.getpid()):
        return  # this process IS the genuine live MU_MACD service process -- safe
    raise RuntimeError(
        "REFUSING to write to the production MU_MACD ledger "
        f"({_DEFAULT_SIGNAL_LEDGER_PATH} / {_DEFAULT_EXECUTION_LEDGER_PATH}). "
        "This looks like an ad-hoc/replay script calling append_signal/append_execution "
        "(directly or via worker.run_once()) without isolating the ledger path first. "
        "Redirect ledger.SIGNAL_LEDGER_PATH and ledger.EXECUTION_LEDGER_PATH to a tmp "
        "directory BEFORE calling run_once(). If this genuinely is the live service "
        f"process, set os.environ['{LIVE_WORKER_MARKER_ENV}'] = str(os.getpid()) once at "
        "startup instead (see app.trading.mu_macd.service.MUMacdService)."
    )


_SIGNAL_LOCK = threading.RLock()
_EXECUTION_LOCK = threading.RLock()


def ensure_paths() -> None:
    LOGS_DIR_PATH.mkdir(parents=True, exist_ok=True)


def _read_header(path: Path) -> Optional[list[str]]:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path, newline="", encoding="utf-8") as fh:
        try:
            return next(csv.reader(fh))
        except StopIteration:
            return None


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


def _append_row(path: Path, columns: list[str], row: dict[str, Any]) -> None:
    ensure_paths()
    is_new = not path.exists() or path.stat().st_size == 0
    if not is_new:
        _ensure_columns(path, columns)
    fieldnames = columns if is_new else (_read_header(path) or columns)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in fieldnames})


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
    signal_id = str(row.get("signal_id") or "")
    if not signal_id:
        raise ValueError("append_signal: row is missing signal_id")
    _assert_safe_to_write_ledger()
    with _SIGNAL_LOCK:
        for existing in _load_rows(SIGNAL_LEDGER_PATH):
            if existing.get("signal_id") == signal_id:
                return False
        _append_row(SIGNAL_LEDGER_PATH, SIGNAL_LEDGER_COLUMNS, row)
        return True


def append_execution(row: dict[str, Any]) -> bool:
    order_id = str(row.get("order_id") or "")
    if not order_id:
        raise ValueError("append_execution: row is missing order_id")
    _assert_safe_to_write_ledger()
    with _EXECUTION_LOCK:
        for existing in _load_rows(EXECUTION_LEDGER_PATH):
            if existing.get("order_id") == order_id:
                return False
        _append_row(EXECUTION_LEDGER_PATH, EXECUTION_LEDGER_COLUMNS, row)
        return True
