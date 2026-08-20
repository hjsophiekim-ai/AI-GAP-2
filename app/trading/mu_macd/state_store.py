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

# Frozen at import time -- see app.trading.macd2.state_store's identical
# pattern and app.trading.macd2.ledger's docstring for the 2026-08-19
# incident this guards against, ported here for the same risk in MU_MACD.
_DEFAULT_STATE_PATH: Path = STATE_PATH
LIVE_WORKER_MARKER_ENV = "MU_MACD_LIVE_WORKER_PID"


def _assert_safe_to_write_state() -> None:
    if STATE_PATH != _DEFAULT_STATE_PATH:
        return  # already redirected elsewhere (pytest conftest, an isolated script) -- safe
    if os.environ.get(LIVE_WORKER_MARKER_ENV) == str(os.getpid()):
        return  # this process IS the genuine live MU_MACD service process -- safe
    raise RuntimeError(
        f"REFUSING to write to the production MU_MACD state file ({_DEFAULT_STATE_PATH}). "
        "This looks like an ad-hoc/replay script calling save_state() (directly or via "
        "worker.run_once()) without isolating the state path first. Redirect "
        "state_store.STATE_DIR_PATH and state_store.STATE_PATH to a tmp directory BEFORE "
        "calling run_once(). If this genuinely is the live service process, set "
        f"os.environ['{LIVE_WORKER_MARKER_ENV}'] = str(os.getpid()) once at startup instead "
        "(see app.trading.mu_macd.service.MUMacdService)."
    )


_FILE_LOCK = threading.RLock()
_DIRECTION_VALUES = {d.value for d in Direction}


def default_state() -> RuntimeState:
    state = RuntimeState()
    state.mode = config.DEFAULT_MODE_DEFAULT
    state.budget = config.DEFAULT_BUDGET
    state.auto_trade_on = config.AUTO_TRADE_ON_DEFAULT
    state.quick_profit_enabled = config.QUICK_PROFIT_ENABLED_DEFAULT
    state.time_window_filter_enabled = config.TIME_WINDOW_FILTER_ENABLED_DEFAULT
    state.down_blue_exception_filter_enabled = config.TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT
    state.down_blue_exception_filter_version = config.TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION
    state.no_filter_0900_1100_enabled = config.NO_FILTER_0900_1100_FILTER_DEFAULT
    state.no_filter_0900_1100_filter_version = config.NO_FILTER_0900_1100_FILTER_VERSION
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
        if key == "time_window_pending_flag_direction" and value not in _DIRECTION_VALUES and value is not None:
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
    _assert_safe_to_write_state()
    with _FILE_LOCK:
        STATE_DIR_PATH.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state_to_dict(state), ensure_ascii=False, indent=2, default=str)
        tmp_path = STATE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, STATE_PATH)
