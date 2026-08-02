"""TSLA_AUTO ledgers — signal_ledger + execution_ledger. Entirely separate
files/paths from MACD2's macd2_signal_ledger.csv/macd2_execution_ledger.csv
(docs §3). Append-only, atomic header init, file lock, dedup by signal_id
(signal ledger) / order_id (execution ledger). Never raises on an empty or
missing ledger. Structure mirrors app/trading/macd2/ledger.py
(TSLA_AUTO_COPY_MAP.md — COPY_WITH_US_MARKET_CHANGE), re-implemented here
independently.
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.tsla_auto import config
from app.trading.tsla_auto.models import Direction
from app.utils.data_paths import data_path

_VALID_DIRECTION_VALUES = {d.value for d in Direction}

SIGNAL_LEDGER_COLUMNS = [
    "trading_date", "completed_bar_at", "signal_id", "signal_type", "direction", "origin",
    "macd", "signal", "hist_last3",
    "bar_start_at_et", "bar_start_at_kst", "bar_end_at_et", "bar_end_at_kst",
    "evaluated_at_et", "evaluated_at_kst", "detected_at_et", "detected_at_kst",
    "order_requested_at_et", "order_requested_at_kst",
    "order_result", "block_reason",
    "strategy_name", "strategy_version", "signal_rule", "worker_code_sha",
    "worker_instance_id", "session_started_at",
    "previous_macd", "previous_signal", "previous_diff",
    "confirmed_macd", "confirmed_signal", "confirmed_diff", "confirmed_direction",
    "quote_ages", "position_reconcile", "executor_called", "broker_called",
    "broker_order_id", "broker_rt_cd", "broker_msg_cd", "broker_msg1",
    "available_usd", "usable_usd", "bid1", "ask1", "order_price",
    "budget_qty", "available_qty", "final_qty", "expected_notional_usd", "expected_fee_usd",
    "filled_qty", "fill_poll_result", "balance_qty", "failure_stage", "final_result",
    # Optional Hybrid strong-flag filter fields (never rename/delete older cols)
    "strong_filter_enabled", "strong_filter_version",
    "strong_score", "strong_required_score", "strong_approved", "strong_decision",
    "strong_block_reason", "strong_is_reversal", "strong_fast_reversal", "strong_component_scores",
    "strong_metrics",
    "market_regime", "daily_entry_count", "last_entry_at",
    # (신규) 손절 재진입 쿨다운 필드 (docs §12 — MACD2에 없음)
    "stop_loss_reentry_cooldown_active", "stop_loss_reentry_override_used",
    "last_stop_loss_at", "cooldown_end_at", "elapsed_minutes_after_stop_loss",
]

EXECUTION_LEDGER_COLUMNS = [
    "order_id", "signal_id", "timestamp", "mode", "symbol", "side",
    "requested_qty", "executed_qty", "requested_price", "executed_price",
    "position_before", "position_after", "gross_pnl_usd",
    "buy_fee_usd", "sell_fee_usd", "slippage_usd", "fx_cost_usd",
    "sec_fee_usd", "finra_taf_usd", "total_cost_usd", "fee_usd", "net_pnl_usd",
    "exit_reason", "broker_response",
]

LOGS_DIR_PATH: Path = data_path("ledger", "tsla_auto")
SIGNAL_LEDGER_PATH: Path = LOGS_DIR_PATH / config.SIGNAL_LEDGER_FILENAME
EXECUTION_LEDGER_PATH: Path = LOGS_DIR_PATH / config.EXECUTION_LEDGER_FILENAME

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


def _append_row(path: Path, columns: list[str], row: dict[str, Any]) -> None:
    """Append one row, keyed strictly by column NAME — never by position
    (avoids MACD2's 2026-07-27 column-shift incident class of bug)."""
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


def execution_row_trading_date(row: dict[str, Any]) -> str:
    text = str(row.get("timestamp") or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        parsed = parsed.replace(tzinfo=config.ET)
    return parsed.astimezone(config.ET).strftime("%Y%m%d")


def filter_execution_rows_by_trading_date(rows: list[dict[str, Any]], trading_date: str) -> list[dict[str, Any]]:
    expected = "".join(ch for ch in str(trading_date or "") if ch.isdigit())[:8]
    if len(expected) != 8:
        return []
    return [r for r in rows if execution_row_trading_date(r) == expected]


def append_signal(row: dict[str, Any]) -> bool:
    """Append one signal-ledger row. Returns False (no write) if signal_id
    was already recorded — signal_id dedup (at most one lifetime record)."""
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
    """Append one execution-ledger row. Returns False (no write) if order_id
    was already recorded — order_id dedup."""
    order_id = str(row.get("order_id") or "")
    if not order_id:
        raise ValueError("append_execution: row is missing order_id")
    with _EXECUTION_LOCK:
        for existing in _load_rows(EXECUTION_LEDGER_PATH):
            if existing.get("order_id") == order_id:
                return False
        _append_row(EXECUTION_LEDGER_PATH, EXECUTION_LEDGER_COLUMNS, row)
        return True


def _row_schema_ok(row: dict[str, Any]) -> bool:
    strategy_name = str(row.get("strategy_name") or "")
    if strategy_name and strategy_name != config.STRATEGY_NAME:
        return False
    direction = str(row.get("direction") or "")
    if direction and direction not in _VALID_DIRECTION_VALUES:
        return False
    return True


def _is_pre_session_row(row: dict[str, Any], session_started_at: Optional[str]) -> bool:
    if not session_started_at:
        return False
    detected_at = str(row.get("detected_at_et") or "")
    if not detected_at:
        return False
    try:
        return datetime.fromisoformat(detected_at) < datetime.fromisoformat(str(session_started_at))
    except ValueError:
        return False


def _current_strategy_rows(
    rows: list[dict[str, Any]], *, strategy_version: Optional[str] = None, signal_rule: Optional[str] = None,
    session_started_at: Optional[str] = None, worker_code_sha: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """docs §7/§2(MACD2 이식) — 현재 strategy_version/signal_rule/worker_code_sha
    /세션 이후의 confirmed 신호만 "current"로 남기고, 나머지는 제외 사유와 함께
    별도 목록으로 반환한다. 원장 원본은 변경하지 않는다(조회 필터만)."""
    current: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        keep = True
        reason = ""
        if not _row_schema_ok(row):
            keep, reason = False, "MALFORMED_SCHEMA"
        if keep and strategy_version and row.get("strategy_version") != strategy_version:
            keep, reason = False, "OLD_STRATEGY"
        if keep and signal_rule and row.get("signal_rule") != signal_rule:
            keep, reason = False, "LEGACY_INVALID"
        if keep and worker_code_sha and str(row.get("worker_code_sha") or "") != worker_code_sha:
            keep, reason = False, "OLD_WORKER_SHA"
        if keep and str(row.get("origin") or "") == config.ORIGIN_HISTORICAL_REPLAY_ONLY:
            keep, reason = False, "HISTORICAL_REPLAY_ONLY"
        if keep and _is_pre_session_row(row, session_started_at):
            keep, reason = False, "PRE_SESSION_ROW"
        if keep:
            current.append(row)
        else:
            copy = dict(row)
            copy["excluded_reason"] = reason
            excluded.append(copy)
    return current, excluded


def summarize_signals(
    trading_date: str, *, strategy_version: Optional[str] = None, signal_rule: Optional[str] = None,
    session_started_at: Optional[str] = None, worker_code_sha: Optional[str] = None,
) -> dict[str, Any]:
    """docs §UI stats: today's UP_RED/DOWN_BLUE counts + unexecuted
    signals+reason. Never raises on an empty/missing ledger."""
    all_rows = [r for r in load_signal_ledger() if r.get("trading_date") == trading_date]
    rows, excluded = _current_strategy_rows(
        all_rows, strategy_version=strategy_version, signal_rule=signal_rule,
        session_started_at=session_started_at, worker_code_sha=worker_code_sha,
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
        if not str(r.get("order_result") or "").strip()
        or str(r.get("order_result")).upper() in ("BLOCKED", "FAILED", "WAITING")
    ]
    return {
        "trading_date": trading_date, "red_count": red_count, "blue_count": blue_count,
        "signal_count": len(onset_rows), "unexecuted_signals": unexecuted, "excluded_signals": excluded,
        "latest_signal_id": onset_rows[-1].get("signal_id") if onset_rows else None,
        "current_signal_ids": [r.get("signal_id") for r in onset_rows if r.get("signal_id")],
        "onset_signals": onset_rows,
    }


def summarize_daily_trading(
    trading_date: str, budget_usd: float = None, *, signal_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """docs §UI/§비용·손익 stats: buys/sells, round trips, Gross/Net USD.
    Never raises on an empty or missing execution ledger."""
    budget_usd = budget_usd if budget_usd is not None else config.DEFAULT_BUDGET_USD
    rows = filter_execution_rows_by_trading_date(load_execution_ledger(), trading_date)
    if signal_ids is not None:
        rows = [r for r in rows if str(r.get("signal_id") or "") in signal_ids]
    budget_f = float(budget_usd or config.DEFAULT_BUDGET_USD)

    empty: dict[str, Any] = {
        "trading_date": trading_date, "has_data": False, "buy_count": 0, "sell_count": 0,
        "round_trip_count": 0, "gross_pnl_usd": 0.0, "total_commission_usd": 0.0,
        "total_slippage_usd": 0.0, "total_fx_cost_usd": 0.0, "total_cost_usd": 0.0, "net_pnl_usd": 0.0,
        "return_pct": 0.0, "win_rate_pct": 0.0, "budget_usd": budget_f,
    }
    if not rows:
        return empty

    buys = [r for r in rows if str(r.get("side") or "").upper() == "BUY"]
    sells = [r for r in rows if str(r.get("side") or "").upper() == "SELL"]
    gross = sum(float(r.get("gross_pnl_usd") or 0.0) for r in sells)
    commission = sum(float(r.get("buy_fee_usd") or 0.0) + float(r.get("sell_fee_usd") or 0.0) for r in sells)
    slippage = sum(float(r.get("slippage_usd") or 0.0) for r in sells)
    fx_cost = sum(float(r.get("fx_cost_usd") or 0.0) for r in sells)
    total_cost = sum(float(r.get("total_cost_usd") or r.get("fee_usd") or 0.0) for r in sells)
    net = sum(float(r.get("net_pnl_usd") or 0.0) for r in sells)
    wins = sum(1 for r in sells if float(r.get("net_pnl_usd") or 0.0) > 0)
    round_trips = len(sells)
    return {
        "trading_date": trading_date, "has_data": True, "buy_count": len(buys), "sell_count": len(sells),
        "round_trip_count": round_trips, "gross_pnl_usd": round(gross, 4),
        "total_commission_usd": round(commission, 4), "total_slippage_usd": round(slippage, 4),
        "total_fx_cost_usd": round(fx_cost, 4), "total_cost_usd": round(total_cost, 4),
        "net_pnl_usd": round(net, 4), "return_pct": round((net / budget_f) * 100.0, 4) if budget_f else 0.0,
        "win_rate_pct": round((wins / round_trips) * 100.0, 2) if round_trips else 0.0, "budget_usd": budget_f,
    }
