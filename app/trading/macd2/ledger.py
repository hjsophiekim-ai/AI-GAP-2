"""MACD2 ledgers — signal_ledger + execution_ledger.

Entirely separate files from MACD v1's macd_hynix_execution_ledger.csv /
macd_hynix_signal_ledger.csv (docs §13/§17). Append-only, atomic header
init, file lock, dedup by signal_id (signal ledger) / order_id (execution
ledger). Statistics functions never raise on an empty or missing ledger —
the UI must keep rendering (docs §17).
"""
from __future__ import annotations

import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config
from app.trading.macd2.models import Direction
from app.utils.data_paths import LOGS_DIR

_VALID_DIRECTION_VALUES = {d.value for d in Direction}

SIGNAL_LEDGER_COLUMNS = [
    "trading_date", "completed_bar_at", "signal_id", "signal_type", "direction",
    "macd", "signal", "hist_last3", "detected_at", "order_requested_at",
    "order_result", "block_reason",
    "signal_bar_at", "signal_confirmed_at", "baseline_completed_bar_at",
    "strategy_name", "strategy_version", "signal_rule", "worker_code_sha",
    "worker_instance_id", "session_started_at",
    "forming_bar_start", "forming_bar_end",
    "previous_macd", "previous_signal", "previous_diff",
    "provisional_macd", "provisional_signal", "provisional_diff",
    "confirmed_macd", "confirmed_signal", "confirmed_diff",
    "provisional_direction", "confirmed_direction",
    "quote_ages", "position_reconcile", "executor_called", "order_requested_at_trace",
    "broker_called", "broker_order_id", "broker_rt_cd", "broker_msg_cd", "broker_msg1",
    "orderable_cash", "nrcvb_buy_amt", "nrcvb_buy_qty", "psbl_qty_calc_unpr",
    "ask1", "order_price", "order_type", "usable_cash", "limit_buyable_qty",
    "budget_qty", "final_qty", "sizing_price", "requested_qty", "expected_amount",
    "sizing_rt_cd", "sizing_msg_cd", "sizing_msg1",
    "filled_qty", "fill_poll_result", "balance_qty", "failure_stage",
    "final_result",
    # Optional Hybrid MAJOR_FLAG filter fields (appended; never rename/delete older cols)
    "major_filter_enabled", "major_filter_version",
    "major_score", "major_required_score", "major_approved", "major_decision",
    "major_block_reason", "major_is_reversal", "major_fast_reversal",
    "major_component_scores",
    "hist_impulse_atr", "breakout", "price_impulse_atr", "body_atr", "volume_ratio",
    "ema10_ok", "ema20_or_vwap_ok", "recent_range_ratio", "ema_spread_ratio",
    "daily_major_entry_count", "last_major_entry_at",
    # Optional 추세전환장(sideways/whipsaw) entry filter fields (appended
    # 2026-08-04; never rename/delete older cols). Shares the same generic
    # hist_impulse_atr/body_atr/volume_ratio/... metric columns above — those
    # are populated by whichever gate (major or sideways) actually judged
    # this signal, never both, since the two toggles are mutually exclusive.
    "sideways_filter_enabled", "sideways_filter_version",
    "sideways_score", "sideways_required_score", "sideways_approved", "sideways_decision",
    "sideways_block_reason", "sideways_component_scores",
    "daily_sideways_entry_count", "last_sideways_entry_at",
]

EXECUTION_LEDGER_COLUMNS = [
    "order_id", "signal_id", "timestamp", "mode", "symbol", "side",
    "requested_qty", "executed_qty", "requested_price", "executed_price",
    "position_before", "position_after", "gross_pnl", "fee", "slippage",
    "net_pnl", "exit_reason", "broker_response",
    # Profit Lock — MACD convergence early exit diagnostic snapshot (appended
    # 2026-08-05; never rename/delete older cols). order_executor._record_leg
    # never populates these — they're patched in afterward, ONLY for the
    # exit_reason == config.EXIT_PROFIT_LOCK_MACD_CONVERGENCE row, by
    # record_profit_lock_convergence_fields() below; empty for every other row.
    "profit_lock_enabled", "profit_lock_peak_return_pct", "profit_lock_max_support_gap",
    "profit_lock_current_support_gap", "profit_lock_gap_ratio", "profit_lock_contraction_count",
    "profit_lock_drawdown_pct",
]

LOGS_DIR_PATH: Path = LOGS_DIR
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
    """Append one row, keyed strictly by column NAME — never by position.

    ``columns`` is the current code's canonical column list, but its ORDER
    can change across versions (a field inserted in the middle, not only
    appended at the end). A file already on disk keeps whatever order it was
    first written with; if this function blindly wrote new rows using
    ``columns``' (possibly different) order, every value would land one or
    more columns off from the on-disk header — the exact 2026-07-27 incident
    where a forming_bar_start value ended up in the strategy_name column.
    Reading back the REAL on-disk header (after _ensure_columns has merged
    in any genuinely missing columns) and using THAT as fieldnames guarantees
    new rows always align with whatever is actually on disk.
    """
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


PROFIT_LOCK_LEDGER_COLUMNS = [
    "profit_lock_enabled", "profit_lock_peak_return_pct", "profit_lock_max_support_gap",
    "profit_lock_current_support_gap", "profit_lock_gap_ratio", "profit_lock_contraction_count",
    "profit_lock_drawdown_pct",
]


def record_profit_lock_convergence_fields(order_id: str, fields: dict[str, Any]) -> bool:
    """Patch the just-written execution-ledger row for a
    PROFIT_LOCK_MACD_CONVERGENCE exit with its diagnostic snapshot (docs §10
    2026-08-05 spec) — additive columns only (see EXECUTION_LEDGER_COLUMNS),
    never touches order_id/signal_id/side/qty/price/pnl/exit_reason or any
    other field order_executor._record_leg already wrote for that row (주문
    수량·체결·잔고 처리 미변경). Caller must pass the SAME order_id
    ``execute_exit``'s returned ``outcome.sell_result.order_id`` already
    recorded via the normal append_execution() call. No-op (returns False,
    never raises) if the ledger file or that order_id doesn't exist yet —
    a missing diagnostic snapshot must never affect the already-confirmed
    exit itself.
    """
    if not order_id:
        return False
    with _EXECUTION_LOCK:
        if not EXECUTION_LEDGER_PATH.exists():
            return False
        _ensure_columns(EXECUTION_LEDGER_PATH, EXECUTION_LEDGER_COLUMNS)
        with open(EXECUTION_LEDGER_PATH, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        found = False
        for row in rows:
            if str(row.get("order_id") or "") == str(order_id):
                for col in PROFIT_LOCK_LEDGER_COLUMNS:
                    if col in fieldnames:
                        row[col] = fields.get(col, "")
                found = True
                break
        if not found:
            return False
        with open(EXECUTION_LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in fieldnames})
        return True


def _upsert_broker_direct_execution(row: dict[str, Any]) -> str:
    order_id = str(row.get("order_id") or "")
    if not order_id:
        raise ValueError("_upsert_broker_direct_execution: row is missing order_id")
    with _EXECUTION_LOCK:
        rows = _load_rows(EXECUTION_LEDGER_PATH, limit=0)
        for existing in rows:
            if existing.get("order_id") != order_id:
                continue
            if str(existing.get("signal_id") or "") != "BROKER_DIRECT":
                return "skipped"
            existing.update({col: row.get(col, "") for col in EXECUTION_LEDGER_COLUMNS})
            _ensure_columns(EXECUTION_LEDGER_PATH, EXECUTION_LEDGER_COLUMNS)
            fieldnames = _read_header(EXECUTION_LEDGER_PATH) or EXECUTION_LEDGER_COLUMNS
            with open(EXECUTION_LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                for item in rows:
                    writer.writerow({col: item.get(col, "") for col in fieldnames})
            return "updated"
        _append_row(EXECUTION_LEDGER_PATH, EXECUTION_LEDGER_COLUMNS, row)
        return "inserted"


def append_broker_direct_execution(order_result: Any) -> bool:
    """Record a successful direct KIS broker order in the MACD2 execution ledger.

    MACD2's normal order executor writes richer rows after fill/balance
    reconciliation. This helper is only for broker calls made outside that
    executor path, such as manual verification orders, so they still appear in
    the UI trade ledger.
    """
    if not bool(getattr(order_result, "success", False)):
        return False
    order_id = str(getattr(order_result, "order_id", "") or "")
    symbol = str(getattr(order_result, "symbol", "") or "")
    if not order_id or symbol not in config.TRADE_SYMBOLS:
        return False

    side = str(getattr(order_result, "side", "") or "").upper()
    qty = _int(getattr(order_result, "quantity", 0), 0)
    price = _float(getattr(order_result, "price", 0.0), 0.0)
    raw = getattr(order_result, "raw", {}) or {}
    try:
        broker_response = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    except TypeError:
        broker_response = str(raw)

    timestamp = _normalize_execution_timestamp(getattr(order_result, "timestamp", "") or "")

    status = _upsert_broker_direct_execution({
        "order_id": order_id,
        "signal_id": "BROKER_DIRECT",
        "timestamp": timestamp,
        "mode": str(getattr(order_result, "mode", "") or ""),
        "symbol": symbol,
        "side": side,
        "requested_qty": qty,
        "executed_qty": qty,
        "requested_price": price,
        "executed_price": price,
        "position_before": "",
        "position_after": "",
        "gross_pnl": 0.0,
        "fee": 0.0,
        "slippage": 0.0,
        "net_pnl": 0.0,
        "exit_reason": "BROKER_DIRECT",
        "broker_response": broker_response,
    })
    return status in ("inserted", "updated")


def _normalize_execution_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now(config.KST).isoformat()
    if len(text) == 14 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=config.KST).isoformat()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=config.KST).isoformat()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return datetime.now(config.KST).isoformat()
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        parsed = parsed.replace(tzinfo=config.KST)
    return parsed.astimezone(config.KST).isoformat()


def execution_row_trading_date(row: dict[str, Any]) -> str:
    """Return YYYYMMDD for an execution row timestamp.

    Execution rows are normally written as KST ISO strings
    (``2026-07-31T09:03:00+09:00``), while some older tests/rows used compact
    timestamps. Daily UI/stats must treat both forms as the same trading date.
    """
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
        parsed = parsed.replace(tzinfo=config.KST)
    return parsed.astimezone(config.KST).strftime("%Y%m%d")


def filter_execution_rows_by_trading_date(rows: list[dict[str, Any]], trading_date: str) -> list[dict[str, Any]]:
    expected = "".join(ch for ch in str(trading_date or "") if ch.isdigit())[:8]
    if len(expected) != 8:
        return []
    return [r for r in rows if execution_row_trading_date(r) == expected]


def append_broker_direct_fill(fill: dict[str, Any], *, mode: str) -> bool:
    order_id = str(fill.get("order_id") or fill.get("odno") or "")
    symbol = str(fill.get("symbol") or fill.get("pdno") or "")
    if not order_id or symbol not in config.TRADE_SYMBOLS:
        return False
    side = str(fill.get("side") or "").upper()
    qty = _int(fill.get("quantity") or fill.get("qty") or fill.get("tot_ccld_qty"), 0)
    price = _float(fill.get("price") or fill.get("avg_price") or fill.get("avg_prvs"), 0.0)
    try:
        broker_response = json.dumps(fill, ensure_ascii=False, sort_keys=True)
    except TypeError:
        broker_response = str(fill)
    status = _upsert_broker_direct_execution({
        "order_id": order_id,
        "signal_id": "BROKER_DIRECT",
        "timestamp": _normalize_execution_timestamp(fill.get("timestamp") or fill.get("ordered_at") or ""),
        "mode": mode,
        "symbol": symbol,
        "side": side,
        "requested_qty": qty,
        "executed_qty": qty,
        "requested_price": price,
        "executed_price": price,
        "position_before": "",
        "position_after": "",
        "gross_pnl": 0.0,
        "fee": 0.0,
        "slippage": 0.0,
        "net_pnl": 0.0,
        "exit_reason": "BROKER_DIRECT_FILL_BACKFILL",
        "broker_response": broker_response,
    })
    return status in ("inserted", "updated")


def backfill_broker_direct_fills(fills: list[dict[str, Any]], *, mode: str) -> dict[str, int]:
    scanned = 0
    written = 0
    skipped = 0
    for fill in fills:
        if not isinstance(fill, dict):
            skipped += 1
            continue
        scanned += 1
        try:
            if append_broker_direct_fill(fill, mode=mode):
                written += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    return {"scanned": scanned, "written": written, "skipped": skipped}


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


def _row_schema_ok(row: dict[str, Any]) -> bool:
    """Sanity-check a small set of fields whose VALUES are known to be one of
    a fixed set — a column-order mismatch between an old on-disk header and
    the current code (2026-07-27 incident: forming_bar_start landing in the
    strategy_name column) reliably produces an out-of-domain value here, even
    though the row's OTHER fields (strategy_version, signal_rule, ...) may
    look superficially plausible. Empty/missing values are never flagged —
    only a genuinely wrong, non-empty value counts as malformed, so legacy
    rows predating a given column are still handled by OLD_STRATEGY/
    LEGACY_INVALID instead."""
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
    detected_at = str(row.get("detected_at") or "")
    if not detected_at:
        return False
    try:
        return datetime.fromisoformat(detected_at) < datetime.fromisoformat(str(session_started_at))
    except ValueError:
        return False


def _current_strategy_rows(
    rows: list[dict[str, Any]],
    *,
    strategy_version: Optional[str] = None,
    signal_rule: Optional[str] = None,
    session_started_at: Optional[str] = None,
    session_baseline_bar_ts: Optional[str] = None,
    worker_code_sha: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        # docs §2: a row written by a DIFFERENT deployed code SHA (a redeploy
        # happened mid-session, or this is a leftover row from a prior day's
        # code) never counts toward "current" stats — only the query filter
        # changes here, the on-disk row itself is never touched/rewritten.
        if keep and worker_code_sha and str(row.get("worker_code_sha") or "") != worker_code_sha:
            keep, reason = False, "OLD_WORKER_SHA"
        if keep and _is_pre_session_row(row, session_started_at):
            keep, reason = False, "PRE_SESSION_ROW"
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
    worker_code_sha: Optional[str] = None,
) -> dict[str, Any]:
    """docs §16 stats: today's UP_RED/DOWN_BLUE counts + unexecuted signals+reason.

    Never raises on an empty/missing ledger.
    """
    all_rows = [r for r in load_signal_ledger() if r.get("trading_date") == trading_date]
    rows, excluded = _current_strategy_rows(
        all_rows, strategy_version=strategy_version, signal_rule=signal_rule,
        session_started_at=session_started_at, session_baseline_bar_ts=session_baseline_bar_ts,
        worker_code_sha=worker_code_sha,
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
    rows = filter_execution_rows_by_trading_date(load_execution_ledger(), trading_date)
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
