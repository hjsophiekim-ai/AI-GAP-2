"""Common Hynix order coordinator.

This module serializes every Hynix ETF order in-process and preserves the
legacy exit-lock API used by older switching code. It is intentionally small:
the actual broker calls still live in ``hynix_switch_position_manager`` so the
existing KIS confirmation and ledger code remains the single post-order path.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Set

COOLDOWN_SECONDS = 30.0

EXIT_REASON_STOP_LOSS = "stop_loss"
EXIT_REASON_TAKE_PROFIT = "take_profit"
EXIT_REASON_LIQUIDATION = "liquidation"
EXIT_REASON_SWITCH = "switch"

ORDER_PENDING = "PENDING"
ORDER_ACCEPTED = "ACCEPTED"
ORDER_FILLED = "FILLED"
ORDER_FAILED = "FAILED"
ORDER_CANCELLED = "CANCELLED"
TERMINAL_RETRYABLE = {ORDER_FAILED, ORDER_CANCELLED}

_legacy_lock = threading.Lock()
_legacy_active_keys: Set[str] = set()
_legacy_last_executed_at: Dict[str, float] = {}

_order_lock = threading.RLock()
_order_records: Dict[str, dict] = {}
_blocked_duplicate_count = 0
_last_order_latency: Optional[dict] = None


def _legacy_key(mode: str, symbol: str, exit_reason_type: str) -> str:
    return f"{mode}:{symbol}:{exit_reason_type}"


def classify_exit_reason(reason: str) -> str:
    text = str(reason or "").lower()
    if "hard_stop" in text or "stop" in text or "손절" in text:
        return EXIT_REASON_STOP_LOSS
    if "profit" in text or "tp" in text or "익절" in text:
        return EXIT_REASON_TAKE_PROFIT
    if "15:15" in text or "liquidation" in text or "청산" in text:
        return EXIT_REASON_LIQUIDATION
    return EXIT_REASON_SWITCH


class ExitLockHandle:
    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.executed = False

    def mark_executed(self) -> None:
        self.executed = True

    def __bool__(self) -> bool:
        return self.acquired


@contextmanager
def try_acquire_exit_lock(mode: str, symbol: str, exit_reason_type: str):
    """Backward-compatible sell-only cooldown lock."""
    key = _legacy_key(mode, symbol, exit_reason_type)
    handle = ExitLockHandle(acquired=False)
    with _legacy_lock:
        now = time.monotonic()
        last = _legacy_last_executed_at.get(key)
        if key not in _legacy_active_keys and (last is None or now - last >= COOLDOWN_SECONDS):
            _legacy_active_keys.add(key)
            handle.acquired = True
    try:
        yield handle
    finally:
        if handle.acquired:
            with _legacy_lock:
                _legacy_active_keys.discard(key)
                if handle.executed:
                    _legacy_last_executed_at[key] = time.monotonic()


def is_locked(mode: str, symbol: str, exit_reason_type: str) -> bool:
    with _legacy_lock:
        return _legacy_key(mode, symbol, exit_reason_type) in _legacy_active_keys


def seconds_since_last_execution(mode: str, symbol: str, exit_reason_type: str) -> Optional[float]:
    with _legacy_lock:
        last = _legacy_last_executed_at.get(_legacy_key(mode, symbol, exit_reason_type))
    return None if last is None else time.monotonic() - last


def _safe_token(value) -> str:
    return str(value or "-").replace(":", "_").replace("|", "_")


def infer_account_id(broker, mode: str) -> str:
    for attr in ("account_no", "account_number", "account_id"):
        value = getattr(broker, attr, None)
        if value:
            return str(value)
    return f"{mode}-account"


def generate_order_idempotency_key(
    *, mode: str, account: str, symbol: str, side: str,
    episode_id: Optional[str], exit_event_id: Optional[str], target_qty: int,
) -> str:
    return ":".join([
        _safe_token(mode),
        _safe_token(account),
        _safe_token(symbol),
        _safe_token(side).upper(),
        _safe_token(episode_id),
        _safe_token(exit_event_id),
        str(int(target_qty or 0)),
    ])


def get_broker_held_quantity(broker, symbol: str) -> Optional[int]:
    """Return actual broker/KIS held quantity. None means query failure."""
    try:
        positions = broker.get_positions()
    except Exception:
        return None
    for pos in positions or []:
        pos_symbol = pos.get("symbol") if isinstance(pos, dict) else getattr(pos, "symbol", None)
        if pos_symbol != symbol:
            continue
        qty = pos.get("quantity") if isinstance(pos, dict) else getattr(pos, "quantity", 0)
        try:
            return int(qty or 0)
        except Exception:
            return 0
    return 0


@dataclass
class CoordinatedOrder:
    idempotency_key: str
    mode: str
    account: str
    symbol: str
    side: str
    source: str
    requested_qty: int
    severity: Optional[str] = None
    reason: Optional[str] = None
    detected_at: Optional[str] = None
    attempt: int = 1
    blocked: bool = False
    block_reason: Optional[str] = None
    started_at: float = field(default_factory=time.monotonic)

    def mark(self, status: str, **updates) -> None:
        global _last_order_latency
        record = _order_records.get(self.idempotency_key, {})
        latency = round(time.monotonic() - self.started_at, 3)
        record.update(
            status=status,
            updated_at=datetime.now().isoformat(),
            latency_seconds=latency,
            **updates,
        )
        _order_records[self.idempotency_key] = record
        _last_order_latency = {
            "idempotency_key": self.idempotency_key,
            "status": status,
            "latency_seconds": latency,
            "source": self.source,
            "severity": self.severity,
        }


@contextmanager
def coordinated_order(
    *, mode: str, account: str, symbol: str, side: str, episode_id: Optional[str],
    exit_event_id: Optional[str], target_qty: int, source: str,
    severity: Optional[str] = None, reason: Optional[str] = None,
    detected_at: Optional[str] = None,
):
    """Serialize orders and block duplicate idempotency keys.

    The lock covers the broker order and the immediate balance confirmation.
    This prevents overlapping Fast Worker/Dynamic Exit sells from overselling.
    """
    global _blocked_duplicate_count
    side = str(side or "").upper()
    target_qty = int(target_qty or 0)
    idem = generate_order_idempotency_key(
        mode=mode,
        account=account,
        symbol=symbol,
        side=side,
        episode_id=episode_id,
        exit_event_id=exit_event_id,
        target_qty=target_qty,
    )
    with _order_lock:
        existing = _order_records.get(idem)
        if existing and existing.get("status") not in TERMINAL_RETRYABLE:
            _blocked_duplicate_count += 1
            yield CoordinatedOrder(
                idempotency_key=idem,
                mode=mode,
                account=account,
                symbol=symbol,
                side=side,
                source=source,
                requested_qty=target_qty,
                severity=severity,
                reason=reason,
                detected_at=detected_at,
                attempt=int(existing.get("attempt") or 1),
                blocked=True,
                block_reason=f"DUPLICATE_ORDER_BLOCKED: status={existing.get('status')}",
            )
            return
        attempt = int(existing.get("attempt") or 0) + 1 if existing else 1
        order = CoordinatedOrder(
            idempotency_key=idem,
            mode=mode,
            account=account,
            symbol=symbol,
            side=side,
            source=source,
            requested_qty=target_qty,
            severity=severity,
            reason=reason,
            detected_at=detected_at,
            attempt=attempt,
        )
        _order_records[idem] = {
            "idempotency_key": idem,
            "status": ORDER_PENDING,
            "mode": mode,
            "account": account,
            "symbol": symbol,
            "side": side,
            "source": source,
            "severity": severity,
            "reason": reason,
            "detected_at": detected_at,
            "requested_qty": target_qty,
            "attempt": attempt,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        try:
            yield order
        except Exception as exc:
            order.mark(ORDER_FAILED, error=str(exc))
            raise
        finally:
            if _order_records.get(idem, {}).get("status") == ORDER_PENDING:
                order.mark(ORDER_CANCELLED, reason="left coordinator without broker result")


def snapshot() -> dict:
    with _order_lock:
        return {
            "active_orders": [
                dict(r) for r in _order_records.values()
                if r.get("status") in (ORDER_PENDING, ORDER_ACCEPTED)
            ],
            "recent_orders": [dict(r) for r in list(_order_records.values())[-10:]],
            "blocked_duplicate_count": _blocked_duplicate_count,
            "last_order_latency": dict(_last_order_latency or {}),
        }


def reset_for_tests() -> None:
    global _blocked_duplicate_count, _last_order_latency
    with _legacy_lock:
        _legacy_active_keys.clear()
        _legacy_last_executed_at.clear()
    with _order_lock:
        _order_records.clear()
        _blocked_duplicate_count = 0
        _last_order_latency = None
