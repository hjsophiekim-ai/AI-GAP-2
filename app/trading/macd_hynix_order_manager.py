"""Isolated MACD Hynix order manager: sell-confirm-then-buy, own ledger, locks.

Shared only: KIS broker via create_broker + exit_order_coordinator serialization.
Does not write Enhanced ledger / state / episode files.
"""
from __future__ import annotations

import csv
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.logger import logger
from app.trading import exit_order_coordinator as order_coord
from app.trading.macd_hynix_strategy import (
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SYMBOL_NAME,
    TRADE_SYMBOLS,
    opposite_symbol,
    target_symbol_for_direction,
)
from app.trading.trading_cost_engine import TradeCostEngine
from app.utils.data_paths import LOGS_DIR, STATE_DIR

STRATEGY_NAME = "MACD_HYNIX_3M"
SIGNAL_SOURCE = "MACD_HIST_3M_B"
STATE_PATH = STATE_DIR / "macd_hynix_state.json"
MUTEX_PATH = STATE_DIR / "macd_hynix_mutex.json"
LEDGER_PATH = LOGS_DIR / "macd_hynix_execution_ledger.csv"
STATE_LOCK_PATH = STATE_DIR / "macd_hynix_state.lock"

_FILE_LOCK = threading.RLock()
_ORDER_PROCESS_LOCK = threading.RLock()  # process-wide MACD order lock

MAX_ORDER_ATTEMPTS = 3
CONFIRM_ATTEMPTS = 5
CONFIRM_DELAY_SEC = 1.0
QUOTE_STALE_SEC = 30.0

LEDGER_COLUMNS = [
    "trade_id", "timestamp", "mode", "macd_signal", "action", "symbol",
    "requested_qty", "executed_qty", "order_price", "executed_price", "order_id",
    "hold_seconds", "gross_pnl", "cost", "net_pnl", "exit_reason", "success",
    "position_confirmed", "signal_id", "idempotency_key", "pipeline_stage",
    "git_sha", "message",
]

PIPELINE_STAGES = [
    "Signal",
    "Sell Requested",
    "Sell Executed",
    "Buy Requested",
    "Buy Executed",
    "Position Confirmed",
    "Ledger Recorded",
]


def _git_sha() -> str:
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return out.strip()
    except Exception:
        return ""


def default_state() -> dict[str, Any]:
    return {
        "auto_trade_on": False,
        "mode": "mock",
        "budget": 10_000_000,
        "stopped": False,
        "stopped_reason": None,
        "display_direction": "HOLD",
        "last_signal_direction": None,
        "last_signal_bar_ts": None,
        "last_signal_id": None,
        "last_signal_at": None,
        "pending_signal_id": None,
        "pending_signal_direction": None,
        "pending_signal_at": None,
        "order_requested_at": None,
        "broker_executed_at": None,
        "last_order_at": None,
        "position": {
            "symbol": None,
            "quantity": 0,
            "avg_price": 0.0,
            "entry_at": None,
            "signal_id": None,
        },
        "prices": {
            "hynix": None,
            "long": None,
            "inverse": None,
            "updated_at": None,
        },
        "macd": {
            "macd": None,
            "signal": None,
            "hist": None,
            "hist_last3": [],
            "hist_deltas": [],
            "reason": None,
        },
        "pipeline": {stage: {"ok": None, "at": None, "message": ""} for stage in PIPELINE_STAGES},
        "order_block_reason": None,
        "next_action": "대기",
        "processed_signal_ids": [],
        "worker": {
            "alive": False,
            "last_tick_at": None,
            "tick_intervals": [],
            "avg_interval": None,
            "p95_interval": None,
            "signal_detected_at": None,
            "order_requested_at": None,
            "broker_executed_at": None,
        },
        "force_liquidate_pending": False,
        "force_liquidate_done_date": None,
        "real_confirm_ok": False,
        "masked_account": "",
        "updated_at": None,
        "git_sha": _git_sha(),
    }


def ensure_paths() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, Any]:
    ensure_paths()
    with _FILE_LOCK:
        if not STATE_PATH.exists():
            state = default_state()
            _write_state_unlocked(state)
            return state
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            base = default_state()
            base.update(raw if isinstance(raw, dict) else {})
            if not isinstance(base.get("pipeline"), dict):
                base["pipeline"] = default_state()["pipeline"]
            if not isinstance(base.get("position"), dict):
                base["position"] = default_state()["position"]
            if not isinstance(base.get("worker"), dict):
                base["worker"] = default_state()["worker"]
            return base
        except Exception as exc:
            logger.error("[MACDHynix] state load failed: %s", exc)
            return default_state()


def _write_state_unlocked(state: dict[str, Any]) -> None:
    ensure_paths()
    state = dict(state)
    state["updated_at"] = datetime.now().isoformat()
    state["git_sha"] = _git_sha()
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def save_state(state: dict[str, Any]) -> dict[str, Any]:
    with _FILE_LOCK:
        _write_state_unlocked(state)
        return state


def update_state(**kwargs) -> dict[str, Any]:
    with _FILE_LOCK:
        state = load_state()
        state.update(kwargs)
        _write_state_unlocked(state)
        return state


def set_pipeline_stage(state: dict[str, Any], stage: str, ok: bool, message: str = "") -> None:
    pipe = state.setdefault("pipeline", {})
    pipe[stage] = {
        "ok": bool(ok),
        "at": datetime.now().isoformat(),
        "message": str(message or ""),
    }


def write_mutex(*, macd_on: bool, mode: str, reason: str = "") -> None:
    ensure_paths()
    payload = {
        "macd_auto_trade_on": bool(macd_on),
        "mode": mode,
        "updated_at": datetime.now().isoformat(),
        "reason": reason,
        "note": (
            "Old Enhanced UI cannot yet read this file without a one-line patch. "
            "MACD module already blocks start when old auto_trade_on is true."
        ),
    }
    MUTEX_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_old_auto_trade_on() -> tuple[bool, str]:
    """Read-only peek at Enhanced auto_trade_on for mutual exclusion (no writes)."""
    candidates = [
        STATE_DIR / "hynix_strategy_profile_common.json",
        STATE_DIR / "hynix_auto_state_mock.json",
        STATE_DIR / "hynix_auto_state_real.json",
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and bool(data.get("auto_trade_on")):
                return True, str(path.name)
        except Exception:
            continue
    return False, ""


def can_start_macd(mode: str = "mock") -> tuple[bool, str]:
    old_on, src = read_old_auto_trade_on()
    if old_on:
        return False, f"기존 하이닉스 자동매매가 ON 상태입니다 ({src}). 중지 후 시작하세요."
    state = load_state()
    if state.get("force_liquidate_pending"):
        return False, "15:00 강제청산 진행 중입니다."
    return True, ""


def get_ledger_path() -> Path:
    ensure_paths()
    return LEDGER_PATH


def _append_ledger(row: dict[str, Any]) -> str:
    ensure_paths()
    trade_id = row.get("trade_id") or uuid.uuid4().hex[:16]
    row = dict(row)
    row["trade_id"] = trade_id
    is_new = not LEDGER_PATH.exists()
    with _FILE_LOCK:
        with LEDGER_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in LEDGER_COLUMNS})
    return trade_id


def load_ledger(limit: int = 200) -> list[dict[str, Any]]:
    path = get_ledger_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return rows[-limit:]
    except Exception as exc:
        logger.error("[MACDHynix] ledger load failed: %s", exc)
        return []


def _pos_qty(pos: Any, symbol: str) -> int:
    pos_symbol = pos.get("symbol") if isinstance(pos, dict) else getattr(pos, "symbol", None)
    if pos_symbol != symbol:
        return 0
    qty = pos.get("quantity") if isinstance(pos, dict) else getattr(pos, "quantity", 0)
    try:
        return int(qty or 0)
    except Exception:
        return 0


def _pos_avg(pos: Any) -> float:
    val = pos.get("avg_price") if isinstance(pos, dict) else getattr(pos, "avg_price", 0)
    try:
        return float(val or 0)
    except Exception:
        return 0.0


def get_held_quantity(broker, symbol: str) -> Optional[int]:
    try:
        positions = broker.get_positions()
    except Exception:
        return None
    total = 0
    found = False
    for pos in positions or []:
        q = _pos_qty(pos, symbol)
        if q > 0 or (isinstance(pos, dict) and pos.get("symbol") == symbol):
            found = True
            total += q
        elif getattr(pos, "symbol", None) == symbol:
            found = True
            total += q
    return total if found else 0


def get_position_snapshot(broker, symbol: str) -> dict[str, Any]:
    qty = get_held_quantity(broker, symbol)
    if qty is None:
        return {"ok": False, "quantity": None, "avg_price": None}
    avg = 0.0
    try:
        for pos in broker.get_positions() or []:
            if _pos_qty(pos, symbol) > 0 or (
                (pos.get("symbol") if isinstance(pos, dict) else getattr(pos, "symbol", None)) == symbol
            ):
                avg = _pos_avg(pos)
                break
    except Exception:
        pass
    return {"ok": True, "quantity": int(qty or 0), "avg_price": avg}


def confirm_quantity(
    broker,
    symbol: str,
    *,
    retry_while_qty_equals: Optional[int] = None,
    attempts: int = CONFIRM_ATTEMPTS,
    delay_seconds: float = CONFIRM_DELAY_SEC,
) -> dict[str, Any]:
    last_error = None
    for idx in range(max(1, attempts)):
        try:
            snap = get_position_snapshot(broker, symbol)
            if not snap.get("ok"):
                raise RuntimeError("broker position query failed")
            qty = int(snap["quantity"] or 0)
            if (
                retry_while_qty_equals is not None
                and qty == retry_while_qty_equals
                and idx < attempts - 1
            ):
                time.sleep(delay_seconds)
                continue
            return {
                "ok": True,
                "quantity": qty,
                "avg_price": snap.get("avg_price"),
                "attempts": idx + 1,
            }
        except Exception as exc:
            last_error = str(exc)
            if idx < attempts - 1:
                time.sleep(delay_seconds)
    return {"ok": False, "quantity": None, "avg_price": None, "error": last_error}


def validate_etf_quotes(quotes: dict[str, Any]) -> tuple[bool, str]:
    """Safety checks on ETF quotes. Does NOT flip MACD direction — only blocks orders."""
    now = datetime.now()
    for key, symbol in (("long", LONG_SYMBOL), ("inverse", INVERSE_SYMBOL)):
        q = quotes.get(key) or {}
        price = q.get("price")
        try:
            price_f = float(price)
        except Exception:
            return False, f"ORDER_DATA_INVALID: {symbol} price missing"
        if price_f <= 0:
            return False, f"ORDER_DATA_INVALID: {symbol} price={price_f}"
        updated = q.get("updated_at")
        if updated:
            try:
                ts = datetime.fromisoformat(str(updated))
                if (now - ts).total_seconds() > QUOTE_STALE_SEC:
                    return False, f"ORDER_DATA_INVALID: {symbol} stale quote"
            except Exception:
                pass
        if q.get("bid") is not None and q.get("ask") is not None:
            try:
                if float(q["bid"]) <= 0 or float(q["ask"]) <= 0:
                    return False, f"ORDER_DATA_INVALID: {symbol} bad bid/ask"
            except Exception:
                return False, f"ORDER_DATA_INVALID: {symbol} bid/ask parse"
    # Both ETFs abnormal same-direction spike
    try:
        long_chg = float((quotes.get("long") or {}).get("change_pct") or 0)
        inv_chg = float((quotes.get("inverse") or {}).get("change_pct") or 0)
        if abs(long_chg) >= 3.0 and abs(inv_chg) >= 3.0 and (long_chg * inv_chg) > 0:
            return False, "ORDER_DATA_INVALID: both ETFs abnormal same-direction move"
    except Exception:
        pass
    return True, ""


def create_macd_broker(mode: str, *, real_confirm_text: str = "", real_ready: bool = False):
    from app.trading.broker_factory import create_broker

    mode = "real" if mode == "real" else "mock"
    if mode == "real":
        return create_broker(
            mode="real",
            confirm_text=real_confirm_text,
            runtime_real_mode=bool(real_ready),
            runtime_enable_real_buy=bool(real_ready),
            runtime_enable_real_sell=bool(real_ready),
        )
    return create_broker(mode="mock")


def _order_to_dict(order: Any) -> dict:
    if hasattr(order, "to_dict"):
        return order.to_dict()
    if isinstance(order, dict):
        return dict(order)
    return {
        "success": bool(getattr(order, "success", False)),
        "order_id": getattr(order, "order_id", ""),
        "message": getattr(order, "message", ""),
        "price": getattr(order, "price", None),
        "quantity": getattr(order, "quantity", None),
    }


def _record_fill(
    *,
    mode: str,
    macd_signal: str,
    action: str,
    symbol: str,
    requested_qty: int,
    executed_qty: int,
    order_price: float,
    executed_price: float,
    order_id: str,
    success: bool,
    position_confirmed: bool,
    signal_id: str,
    idempotency_key: str,
    pipeline_stage: str,
    exit_reason: str = "",
    hold_seconds: float = 0.0,
    entry_price: Optional[float] = None,
    message: str = "",
) -> str:
    """Record ledger only after broker confirmation when success=True."""
    cost_engine = TradeCostEngine()
    gross = 0.0
    cost = 0.0
    net = 0.0
    if action == "SELL" and entry_price and executed_qty > 0:
        breakdown = cost_engine.compute_net_pnl(
            symbol=symbol,
            entry_price=float(entry_price),
            exit_price=float(executed_price),
            quantity=int(executed_qty),
            buy_order_type="market",
            sell_order_type="market",
        )
        gross = float(breakdown.get("gross_pnl") or 0.0)
        cost = float(breakdown.get("total_cost") or 0.0)
        net = float(breakdown.get("net_pnl") or (gross - cost))
    elif action == "BUY" and executed_qty > 0:
        breakdown = cost_engine.compute_trade_cost(
            symbol=symbol,
            side="BUY",
            executed_price=float(executed_price),
            quantity=int(executed_qty),
            order_type="market",
        )
        cost = float(breakdown.get("total_cost") or 0.0)
        net = -cost
        gross = 0.0

    if success and not position_confirmed:
        # Hard rule: never mark success without confirmation
        success = False
        message = (message or "") + " | LEDGER_BLOCKED_UNCONFIRMED"

    return _append_ledger({
        "timestamp": datetime.now().isoformat(),
        "mode": mode,
        "macd_signal": macd_signal,
        "action": action,
        "symbol": symbol,
        "requested_qty": requested_qty,
        "executed_qty": executed_qty if (success and position_confirmed) else 0,
        "order_price": order_price,
        "executed_price": executed_price if (success and position_confirmed) else "",
        "order_id": order_id,
        "hold_seconds": round(hold_seconds, 1),
        "gross_pnl": round(gross, 2) if (success and position_confirmed) else 0,
        "cost": round(cost, 2) if (success and position_confirmed) else 0,
        "net_pnl": round(net, 2) if (success and position_confirmed) else 0,
        "exit_reason": exit_reason,
        "success": bool(success and position_confirmed),
        "position_confirmed": bool(position_confirmed),
        "signal_id": signal_id,
        "idempotency_key": idempotency_key,
        "pipeline_stage": pipeline_stage,
        "git_sha": _git_sha(),
        "message": message,
    })


def execute_sell_all(
    broker,
    symbol: str,
    price: float,
    *,
    mode: str,
    signal_id: str,
    macd_signal: str,
    reason: str,
    entry_price: Optional[float] = None,
    entry_at: Optional[str] = None,
    attempt: int = 1,
) -> dict[str, Any]:
    if symbol not in TRADE_SYMBOLS:
        return {"success": False, "message": f"invalid trade symbol {symbol}"}
    if attempt > MAX_ORDER_ATTEMPTS:
        return {"success": False, "message": f"MAX_ORDER_ATTEMPTS exceeded ({MAX_ORDER_ATTEMPTS})"}

    account = order_coord.infer_account_id(broker, mode)
    before = get_held_quantity(broker, symbol)
    if before is None:
        return {"success": False, "message": "broker held quantity query failed before sell"}
    before = int(before)
    if before <= 0:
        return {
            "success": True,
            "already_flat": True,
            "sold_quantity": 0,
            "remaining_quantity": 0,
            "message": "already flat",
            "fill_confirmed": True,
        }

    with order_coord.coordinated_order(
        mode=mode,
        account=account,
        symbol=symbol,
        side="SELL",
        episode_id=signal_id,
        exit_event_id=f"MACD_SELL:{symbol}:{signal_id}:{attempt}",
        target_qty=before,
        source=SIGNAL_SOURCE,
        reason=reason,
    ) as coordinated:
        if coordinated.blocked:
            return {
                "success": False,
                "message": coordinated.block_reason,
                "blocked_by_coordinator": True,
                "idempotency_key": coordinated.idempotency_key,
            }
        order = broker.sell(symbol, SYMBOL_NAME.get(symbol, symbol), before, float(price), order_type="market")
        od = _order_to_dict(order)
        if not od.get("success"):
            coordinated.mark(order_coord.ORDER_FAILED, broker_error=od.get("message"))
            return {
                "success": False,
                "message": od.get("message") or "sell failed",
                "idempotency_key": coordinated.idempotency_key,
                "order": od,
            }
        confirmed = confirm_quantity(broker, symbol, retry_while_qty_equals=before)
        remaining = confirmed.get("quantity") if confirmed.get("ok") else None
        sold = None if remaining is None else max(0, before - int(remaining))
        hold_seconds = 0.0
        if entry_at:
            try:
                hold_seconds = max(0.0, (datetime.now() - datetime.fromisoformat(str(entry_at))).total_seconds())
            except Exception:
                hold_seconds = 0.0

        if confirmed.get("ok") and int(remaining or 0) == 0:
            coordinated.mark(
                order_coord.ORDER_FILLED,
                sent_qty=before,
                filled_qty=sold,
                remaining_quantity=remaining,
                broker_order_id=od.get("order_id"),
            )
            _record_fill(
                mode=mode,
                macd_signal=macd_signal,
                action="SELL",
                symbol=symbol,
                requested_qty=before,
                executed_qty=int(sold or 0),
                order_price=float(price),
                executed_price=float(od.get("price") or price),
                order_id=str(od.get("order_id") or ""),
                success=True,
                position_confirmed=True,
                signal_id=signal_id,
                idempotency_key=coordinated.idempotency_key,
                pipeline_stage="Sell Executed",
                exit_reason=reason,
                hold_seconds=hold_seconds,
                entry_price=entry_price,
            )
            return {
                "success": True,
                "sold_quantity": int(sold or 0),
                "remaining_quantity": 0,
                "fill_confirmed": True,
                "idempotency_key": coordinated.idempotency_key,
                "order": od,
            }

        coordinated.mark(
            order_coord.ORDER_ACCEPTED if od.get("success") else order_coord.ORDER_FAILED,
            sent_qty=before,
            broker_order_id=od.get("order_id"),
            remaining_quantity=remaining,
        )
        _append_ledger({
            "timestamp": datetime.now().isoformat(),
            "mode": mode,
            "macd_signal": macd_signal,
            "action": "SELL",
            "symbol": symbol,
            "requested_qty": before,
            "executed_qty": 0,
            "order_price": price,
            "executed_price": "",
            "order_id": od.get("order_id") or "",
            "hold_seconds": 0,
            "gross_pnl": 0,
            "cost": 0,
            "net_pnl": 0,
            "exit_reason": reason,
            "success": False,
            "position_confirmed": False,
            "signal_id": signal_id,
            "idempotency_key": coordinated.idempotency_key,
            "pipeline_stage": "Sell Requested",
            "git_sha": _git_sha(),
            "message": "sell accepted but qty not confirmed flat",
        })
        return {
            "success": False,
            "message": "sell not confirmed flat",
            "remaining_quantity": remaining,
            "fill_confirmed": False,
            "idempotency_key": coordinated.idempotency_key,
            "order": od,
        }


def execute_buy(
    broker,
    symbol: str,
    price: float,
    budget: float,
    *,
    mode: str,
    signal_id: str,
    macd_signal: str,
    reason: str,
    attempt: int = 1,
) -> dict[str, Any]:
    if symbol not in TRADE_SYMBOLS:
        return {"success": False, "message": f"invalid trade symbol {symbol}"}
    if attempt > MAX_ORDER_ATTEMPTS:
        return {"success": False, "message": f"MAX_ORDER_ATTEMPTS exceeded ({MAX_ORDER_ATTEMPTS})"}
    if price <= 0:
        return {"success": False, "message": "ORDER_DATA_INVALID: buy price <= 0"}

    # Never buy opposite ETF before opposite is flat
    other = opposite_symbol(symbol)
    if other:
        other_qty = get_held_quantity(broker, other)
        if other_qty is None:
            return {"success": False, "message": "cannot verify opposite flat before buy"}
        if int(other_qty) > 0:
            return {
                "success": False,
                "message": f"opposite position still held ({other} qty={other_qty}); buy blocked",
                "opposite_qty": int(other_qty),
            }

    qty = int(float(budget) // float(price))
    if qty < 1:
        return {"success": False, "message": "budget too small for 1 share"}

    account = order_coord.infer_account_id(broker, mode)
    before = get_held_quantity(broker, symbol)
    if before is None:
        return {"success": False, "message": "broker held quantity query failed before buy"}
    before = int(before)

    with order_coord.coordinated_order(
        mode=mode,
        account=account,
        symbol=symbol,
        side="BUY",
        episode_id=signal_id,
        exit_event_id=f"MACD_BUY:{symbol}:{signal_id}:{attempt}",
        target_qty=qty,
        source=SIGNAL_SOURCE,
        reason=reason,
    ) as coordinated:
        if coordinated.blocked:
            return {
                "success": False,
                "message": coordinated.block_reason,
                "blocked_by_coordinator": True,
                "idempotency_key": coordinated.idempotency_key,
            }
        order = broker.buy(symbol, SYMBOL_NAME.get(symbol, symbol), qty, float(price), order_type="market")
        od = _order_to_dict(order)
        if not od.get("success"):
            coordinated.mark(order_coord.ORDER_FAILED, broker_error=od.get("message"))
            return {
                "success": False,
                "message": od.get("message") or "buy failed",
                "idempotency_key": coordinated.idempotency_key,
                "order": od,
            }
        confirmed = confirm_quantity(broker, symbol, retry_while_qty_equals=before)
        after = confirmed.get("quantity") if confirmed.get("ok") else None
        filled = None if after is None else max(0, int(after) - before)
        if confirmed.get("ok") and filled and filled > 0:
            coordinated.mark(
                order_coord.ORDER_FILLED,
                sent_qty=qty,
                filled_qty=filled,
                remaining_quantity=after,
                broker_order_id=od.get("order_id"),
            )
            _record_fill(
                mode=mode,
                macd_signal=macd_signal,
                action="BUY",
                symbol=symbol,
                requested_qty=qty,
                executed_qty=int(filled),
                order_price=float(price),
                executed_price=float(od.get("price") or price),
                order_id=str(od.get("order_id") or ""),
                success=True,
                position_confirmed=True,
                signal_id=signal_id,
                idempotency_key=coordinated.idempotency_key,
                pipeline_stage="Buy Executed",
                exit_reason=reason,
            )
            return {
                "success": True,
                "bought_quantity": int(filled),
                "after_quantity": int(after or 0),
                "avg_price": confirmed.get("avg_price") or float(od.get("price") or price),
                "fill_confirmed": True,
                "idempotency_key": coordinated.idempotency_key,
                "order": od,
            }

        coordinated.mark(order_coord.ORDER_ACCEPTED, sent_qty=qty, broker_order_id=od.get("order_id"))
        _append_ledger({
            "timestamp": datetime.now().isoformat(),
            "mode": mode,
            "macd_signal": macd_signal,
            "action": "BUY",
            "symbol": symbol,
            "requested_qty": qty,
            "executed_qty": 0,
            "order_price": price,
            "executed_price": "",
            "order_id": od.get("order_id") or "",
            "hold_seconds": 0,
            "gross_pnl": 0,
            "cost": 0,
            "net_pnl": 0,
            "exit_reason": reason,
            "success": False,
            "position_confirmed": False,
            "signal_id": signal_id,
            "idempotency_key": coordinated.idempotency_key,
            "pipeline_stage": "Buy Requested",
            "git_sha": _git_sha(),
            "message": "buy accepted but fill not confirmed",
        })
        return {
            "success": False,
            "message": "buy fill not confirmed",
            "fill_confirmed": False,
            "idempotency_key": coordinated.idempotency_key,
            "order": od,
        }


def switch_to_direction(
    broker,
    direction: str,
    *,
    mode: str,
    budget: float,
    quotes: dict[str, Any],
    signal_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Full switch: sell opposite (if any) → confirm 0 → buy target. Never buy before sell confirm."""
    with _ORDER_PROCESS_LOCK:
        target = target_symbol_for_direction(direction)
        if not target:
            return {"success": False, "message": "unknown direction"}

        ok_q, q_reason = validate_etf_quotes(quotes)
        if not ok_q:
            state["order_block_reason"] = q_reason
            set_pipeline_stage(state, "Sell Requested", False, q_reason)
            return {"success": False, "message": q_reason, "order_data_invalid": True}

        processed = list(state.get("processed_signal_ids") or [])
        if signal_id in processed:
            return {"success": False, "message": "duplicate signal_id blocked", "duplicate": True}

        pos = state.get("position") or {}
        held_symbol = pos.get("symbol")
        if held_symbol == target and int(pos.get("quantity") or 0) > 0:
            return {"success": True, "message": "same direction — no add", "skipped_same_direction": True}

        # Re-check live holdings
        live_target = get_held_quantity(broker, target)
        live_other_sym = opposite_symbol(target)
        live_other = get_held_quantity(broker, live_other_sym) if live_other_sym else 0
        if live_target is None or (live_other_sym and live_other is None):
            return {"success": False, "message": "ORDER_DATA_INVALID: holdings query failed"}

        if int(live_target or 0) > 0 and int(live_other or 0) == 0:
            # Already on target
            state["processed_signal_ids"] = (processed + [signal_id])[-50:]
            return {"success": True, "message": "already holding target", "skipped_same_direction": True}

        macd_signal = direction
        set_pipeline_stage(state, "Signal", True, signal_id)
        state["order_requested_at"] = datetime.now().isoformat()
        state.setdefault("worker", {})["order_requested_at"] = state["order_requested_at"]

        # 1) Sell opposite / any non-target holdings first
        sell_symbols = []
        if live_other_sym and int(live_other or 0) > 0:
            sell_symbols.append(live_other_sym)
        # Also flatten target if somehow both held (shouldn't) — sell other first only
        for sell_sym in sell_symbols:
            price_key = "long" if sell_sym == LONG_SYMBOL else "inverse"
            sell_price = float((quotes.get(price_key) or {}).get("price") or 0)
            if sell_price <= 0:
                msg = f"ORDER_DATA_INVALID: missing sell price for {sell_sym}"
                set_pipeline_stage(state, "Sell Requested", False, msg)
                return {"success": False, "message": msg, "order_data_invalid": True}
            set_pipeline_stage(state, "Sell Requested", True, sell_sym)
            sell_res = None
            for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
                sell_res = execute_sell_all(
                    broker,
                    sell_sym,
                    sell_price,
                    mode=mode,
                    signal_id=signal_id,
                    macd_signal=macd_signal,
                    reason=f"SWITCH_TO_{direction}",
                    entry_price=float(pos.get("avg_price") or 0) or None,
                    entry_at=pos.get("entry_at"),
                    attempt=attempt,
                )
                if sell_res.get("success") and (sell_res.get("fill_confirmed") or sell_res.get("already_flat")):
                    break
            if not sell_res or not sell_res.get("success"):
                msg = (sell_res or {}).get("message") or "sell failed"
                set_pipeline_stage(state, "Sell Executed", False, msg)
                return {"success": False, "message": msg, "sell": sell_res}
            set_pipeline_stage(state, "Sell Executed", True, f"{sell_sym} flat")
            # Hard gate: confirm 0 before buy
            confirm = confirm_quantity(broker, sell_sym)
            if not confirm.get("ok") or int(confirm.get("quantity") or 0) != 0:
                msg = f"sell confirm failed; remaining={confirm.get('quantity')}"
                set_pipeline_stage(state, "Sell Executed", False, msg)
                return {"success": False, "message": msg, "sell": sell_res}

        state["position"] = {
            "symbol": None,
            "quantity": 0,
            "avg_price": 0.0,
            "entry_at": None,
            "signal_id": None,
        }

        # 2) Buy target
        price_key = "long" if target == LONG_SYMBOL else "inverse"
        buy_price = float((quotes.get(price_key) or {}).get("price") or 0)
        if buy_price <= 0:
            msg = f"ORDER_DATA_INVALID: missing buy price for {target}"
            set_pipeline_stage(state, "Buy Requested", False, msg)
            return {"success": False, "message": msg, "order_data_invalid": True}

        set_pipeline_stage(state, "Buy Requested", True, target)
        buy_res = None
        for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
            buy_res = execute_buy(
                broker,
                target,
                buy_price,
                budget,
                mode=mode,
                signal_id=signal_id,
                macd_signal=macd_signal,
                reason=f"ENTER_{direction}",
                attempt=attempt,
            )
            if buy_res.get("success") and buy_res.get("fill_confirmed"):
                break
            if buy_res.get("opposite_qty"):
                break

        if not buy_res or not buy_res.get("success"):
            msg = (buy_res or {}).get("message") or "buy failed"
            set_pipeline_stage(state, "Buy Executed", False, msg)
            return {"success": False, "message": msg, "buy": buy_res}

        set_pipeline_stage(state, "Buy Executed", True, target)
        set_pipeline_stage(state, "Position Confirmed", True, f"qty={buy_res.get('bought_quantity')}")
        set_pipeline_stage(state, "Ledger Recorded", True, signal_id)

        now_iso = datetime.now().isoformat()
        state["position"] = {
            "symbol": target,
            "quantity": int(buy_res.get("bought_quantity") or 0),
            "avg_price": float(buy_res.get("avg_price") or buy_price),
            "entry_at": now_iso,
            "signal_id": signal_id,
        }
        state["broker_executed_at"] = now_iso
        state["last_order_at"] = now_iso
        state.setdefault("worker", {})["broker_executed_at"] = now_iso
        state["processed_signal_ids"] = (processed + [signal_id])[-50:]
        state["order_block_reason"] = None
        state["last_signal_direction"] = direction
        state["last_signal_id"] = signal_id
        return {
            "success": True,
            "buy": buy_res,
            "target": target,
            "message": "switch complete",
        }


def force_liquidate_all(
    broker,
    *,
    mode: str,
    quotes: dict[str, Any],
    state: dict[str, Any],
    reason: str = "15:00_FORCE_LIQUIDATE",
) -> dict[str, Any]:
    """Priority flatten of both ETFs. Independent of MACD signal."""
    with _ORDER_PROCESS_LOCK:
        signal_id = f"FORCE_LIQ:{datetime.now().strftime('%Y%m%d')}"
        results = {}
        all_ok = True
        for symbol in TRADE_SYMBOLS:
            qty = get_held_quantity(broker, symbol)
            if qty is None:
                all_ok = False
                results[symbol] = {"success": False, "message": "qty query failed"}
                continue
            if int(qty) <= 0:
                results[symbol] = {"success": True, "already_flat": True}
                continue
            price_key = "long" if symbol == LONG_SYMBOL else "inverse"
            price = float((quotes.get(price_key) or {}).get("price") or 0)
            if price <= 0:
                # Still attempt with last known avg
                price = float((state.get("position") or {}).get("avg_price") or 1)
            pos = state.get("position") or {}
            res = execute_sell_all(
                broker,
                symbol,
                price,
                mode=mode,
                signal_id=signal_id,
                macd_signal="FORCE",
                reason=reason,
                entry_price=float(pos.get("avg_price") or 0) or None,
                entry_at=pos.get("entry_at"),
            )
            results[symbol] = res
            if not res.get("success"):
                all_ok = False
        if all_ok:
            state["position"] = {
                "symbol": None,
                "quantity": 0,
                "avg_price": 0.0,
                "entry_at": None,
                "signal_id": None,
            }
            state["force_liquidate_pending"] = False
            state["force_liquidate_done_date"] = datetime.now().strftime("%Y-%m-%d")
            set_pipeline_stage(state, "Position Confirmed", True, reason)
        else:
            state["force_liquidate_pending"] = True
        return {"success": all_ok, "results": results}
