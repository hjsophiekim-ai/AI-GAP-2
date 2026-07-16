"""
Shared REAL trading emergency stop.

The stop flag is file-backed so every Streamlit rerun and background thread sees
the same state. The in-process lock serializes order submission inside the
current server process.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.utils.data_paths import STATE_DIR as _STATE_DIR

_ROOT = Path(__file__).resolve().parent.parent.parent
_STOP_FLAG_PATH = _STATE_DIR / "real_auto_trade_emergency_stop.json"

_ORDER_LOCK = threading.RLock()
_LOCAL = threading.local()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def activate_emergency_stop(reason: str = "REAL_AUTO_TRADE_EMERGENCY_STOP") -> dict:
    """Block new REAL orders immediately and persist the stop reason."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": True,
        "reason": reason,
        "activated_at": _now(),
    }
    _STOP_FLAG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def clear_emergency_stop() -> None:
    if _STOP_FLAG_PATH.exists():
        _STOP_FLAG_PATH.unlink()


def get_emergency_stop_state() -> dict:
    if not _STOP_FLAG_PATH.exists():
        return {"active": False}
    try:
        data = json.loads(_STOP_FLAG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data["active"] = bool(data.get("active", True))
            return data
    except Exception:
        pass
    return {"active": True, "reason": "invalid_stop_flag", "activated_at": ""}


def is_emergency_stopped() -> bool:
    return bool(get_emergency_stop_state().get("active"))


@contextmanager
def emergency_liquidation_allowed() -> Iterator[None]:
    """Temporarily allow sell orders that are part of user-requested liquidation."""
    previous = bool(getattr(_LOCAL, "allow_liquidation", False))
    _LOCAL.allow_liquidation = True
    try:
        yield
    finally:
        _LOCAL.allow_liquidation = previous


def _liquidation_allowed() -> bool:
    return bool(getattr(_LOCAL, "allow_liquidation", False))


@contextmanager
def real_order_lock(side: str) -> Iterator[None]:
    """Serialize REAL orders and enforce the emergency stop before submission."""
    state = get_emergency_stop_state()
    if state.get("active") and not (side == "sell" and _liquidation_allowed()):
        reason = state.get("reason") or "REAL 자동매매 긴급정지 상태"
        raise RuntimeError(f"REAL emergency stop active: {reason}")
    with _ORDER_LOCK:
        state = get_emergency_stop_state()
        if state.get("active") and not (side == "sell" and _liquidation_allowed()):
            reason = state.get("reason") or "REAL 자동매매 긴급정지 상태"
            raise RuntimeError(f"REAL emergency stop active: {reason}")
        yield
