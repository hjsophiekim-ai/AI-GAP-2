"""MU_MACD runtime state store — data/state/mu_macd_runtime.json ONLY.

Atomic write (tmp + os.replace) + a thread lock, same technique as
app.trading.macd2.state_store, but this module owns exactly one file and
never reads/writes any macd2_*/tsla_auto_* path. Tests must monkeypatch
STATE_DIR_PATH/STATE_PATH to a tmp_path — never the real path.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.mu_macd import config
from app.trading.mu_macd.models import Direction, PositionSnapshot, RuntimeState
from app.utils.data_paths import STATE_DIR

SCHEMA_VERSION = 1

STATE_DIR_PATH: Path = STATE_DIR
STATE_PATH: Path = STATE_DIR_PATH / config.RUNTIME_STATE_FILENAME

_FILE_LOCK = threading.RLock()
_DIRECTION_VALUES = {d.value for d in Direction}


def default_state() -> RuntimeState:
    state = RuntimeState()
    state.mode = config.DEFAULT_MODE_DEFAULT
    state.budget = config.DEFAULT_BUDGET
    state.auto_trade_on = config.AUTO_TRADE_ON_DEFAULT
    return state


def _position_to_dict(pos: Optional[PositionSnapshot]) -> Optional[dict[str, Any]]:
    if pos is None:
        return None
    return {
        "symbol": pos.symbol,
        "quantity": pos.quantity,
        "avg_price": pos.avg_price,
        "entry_at": pos.entry_at.isoformat() if pos.entry_at else None,
    }


def _position_from_dict(raw: Any) -> Optional[PositionSnapshot]:
    if not isinstance(raw, dict):
        return None
    symbol = raw.get("symbol")
    if not symbol:
        return None
    entry_at_raw = raw.get("entry_at")
    entry_at = None
    if entry_at_raw:
        try:
            entry_at = datetime.fromisoformat(str(entry_at_raw))
        except ValueError:
            entry_at = None
    try:
        quantity = int(raw.get("quantity") or 0)
        avg_price = float(raw.get("avg_price") or 0.0)
    except (TypeError, ValueError):
        return None
    return PositionSnapshot(symbol=str(symbol), quantity=quantity, avg_price=avg_price, entry_at=entry_at)


def state_to_dict(state: RuntimeState) -> dict[str, Any]:
    d = dict(state.__dict__)
    d["position"] = _position_to_dict(state.position)
    d["schema_version"] = SCHEMA_VERSION
    return d


def state_from_dict(raw: dict[str, Any]) -> RuntimeState:
    state = default_state()
    for key, value in raw.items():
        if key == "position":
            state.position = _position_from_dict(value)
            continue
        if not hasattr(state, key):
            continue
        if key == "last_detected_direction" and value not in _DIRECTION_VALUES and value is not None:
            continue
        setattr(state, key, value)
    return state


def load_state() -> RuntimeState:
    with _FILE_LOCK:
        if not STATE_PATH.exists():
            return default_state()
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default_state()
        if not isinstance(raw, dict):
            return default_state()
        return state_from_dict(raw)


def save_state(state: RuntimeState) -> None:
    with _FILE_LOCK:
        STATE_DIR_PATH.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state_to_dict(state), ensure_ascii=False, indent=2, default=str)
        tmp_path = STATE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, STATE_PATH)
