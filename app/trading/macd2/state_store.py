"""MACD2 runtime state store — data/state/macd2_runtime.json only.

Atomic write (tmp + os.replace) + a thread lock. This module owns exactly one
file and never reads/writes MACD v1's macd_hynix_runtime.json /
macd_hynix_state.json paths or schema (docs/MACD2_LOGIC.md §13). Tests must
monkeypatch ``STATE_DIR``/``STATE_PATH`` to a tmp_path — never the real path.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState, RuntimeStatus
from app.utils.data_paths import STATE_DIR

SCHEMA_VERSION = 1

STATE_DIR_PATH: Path = STATE_DIR
STATE_PATH: Path = STATE_DIR_PATH / config.RUNTIME_STATE_FILENAME

_FILE_LOCK = threading.RLock()

_UI_MODE_VALUES = {s.value for s in RuntimeStatus}
_DIRECTION_VALUES = {d.value for d in Direction}


def default_state() -> RuntimeState:
    return RuntimeState()


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
    entry_at_raw = raw.get("entry_at")
    return PositionSnapshot(
        symbol=raw.get("symbol"),
        quantity=int(raw.get("quantity") or 0),
        avg_price=float(raw.get("avg_price") or 0.0),
        entry_at=datetime.fromisoformat(entry_at_raw) if entry_at_raw else None,
    )


def serialize(state: RuntimeState) -> dict[str, Any]:
    """RuntimeState -> a plain JSON-serializable dict of only the known schema fields."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ui_mode": state.ui_mode.value,
        "auto_trade_on": bool(state.auto_trade_on),
        "mode": state.mode,
        "budget": float(state.budget),
        "stopped": bool(state.stopped),
        "stopped_reason": state.stopped_reason,
        "session_date": state.session_date,
        "warmup_ready": bool(state.warmup_ready),
        "last_signal_direction": (
            state.last_signal_direction.value if state.last_signal_direction else None
        ),
        "last_detected_direction": (
            state.last_detected_direction.value if state.last_detected_direction else None
        ),
        "last_executed_direction": (
            state.last_executed_direction.value if state.last_executed_direction else None
        ),
        "current_episode_direction": (
            state.current_episode_direction.value if state.current_episode_direction else None
        ),
        "last_signal_bar_ts": state.last_signal_bar_ts,
        "last_evaluated_bar_ts": state.last_evaluated_bar_ts,
        "processed_signal_ids": list(state.processed_signal_ids),
        "pending_signal": dict(state.pending_signal) if state.pending_signal else None,
        "position": _position_to_dict(state.position),
        "peak_net_return": float(state.peak_net_return),
        "profit_lock_active": bool(state.profit_lock_active),
        "order_block_reason": state.order_block_reason,
        "position_reconcile_diag": dict(state.position_reconcile_diag or {}),
        "last_position_reconcile_at": state.last_position_reconcile_at,
        "updated_at": state.updated_at,
    }


def deserialize(raw: dict[str, Any]) -> RuntimeState:
    """Known-schema fields only — any unexpected key in ``raw`` is silently discarded."""
    base = default_state()
    ui_mode_raw = raw.get("ui_mode")
    ui_mode = RuntimeStatus(ui_mode_raw) if ui_mode_raw in _UI_MODE_VALUES else base.ui_mode
    last_dir_raw = raw.get("last_signal_direction")
    last_dir = Direction(last_dir_raw) if last_dir_raw in _DIRECTION_VALUES else None
    detected_raw = raw.get("last_detected_direction")
    detected_dir = Direction(detected_raw) if detected_raw in _DIRECTION_VALUES else None
    executed_raw = raw.get("last_executed_direction")
    executed_dir = Direction(executed_raw) if executed_raw in _DIRECTION_VALUES else None
    episode_raw = raw.get("current_episode_direction")
    episode_dir = Direction(episode_raw) if episode_raw in _DIRECTION_VALUES else None
    return RuntimeState(
        schema_version=SCHEMA_VERSION,
        ui_mode=ui_mode,
        auto_trade_on=bool(raw.get("auto_trade_on", base.auto_trade_on)),
        mode=str(raw.get("mode", base.mode)),
        budget=float(raw.get("budget", base.budget)),
        stopped=bool(raw.get("stopped", base.stopped)),
        stopped_reason=raw.get("stopped_reason"),
        session_date=raw.get("session_date"),
        warmup_ready=bool(raw.get("warmup_ready", False)),
        last_signal_direction=last_dir,
        last_detected_direction=detected_dir,
        last_executed_direction=executed_dir,
        current_episode_direction=episode_dir,
        last_signal_bar_ts=raw.get("last_signal_bar_ts"),
        last_evaluated_bar_ts=raw.get("last_evaluated_bar_ts"),
        processed_signal_ids=list(raw.get("processed_signal_ids") or []),
        pending_signal=raw.get("pending_signal") if isinstance(raw.get("pending_signal"), dict) else None,
        position=_position_from_dict(raw.get("position")),
        peak_net_return=float(raw.get("peak_net_return", 0.0)),
        profit_lock_active=bool(raw.get("profit_lock_active", False)),
        order_block_reason=raw.get("order_block_reason"),
        position_reconcile_diag=raw.get("position_reconcile_diag") if isinstance(raw.get("position_reconcile_diag"), dict) else {},
        last_position_reconcile_at=raw.get("last_position_reconcile_at"),
        updated_at=raw.get("updated_at"),
    )


def ensure_paths() -> None:
    STATE_DIR_PATH.mkdir(parents=True, exist_ok=True)


def load_state() -> RuntimeState:
    """Load MACD2 runtime state; corrupted JSON recovers to a fresh default rather than raising."""
    with _FILE_LOCK:
        ensure_paths()
        if not STATE_PATH.exists():
            return default_state()
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return default_state()
            return deserialize(raw)
        except Exception:
            return default_state()


def save_state(state: RuntimeState) -> RuntimeState:
    """Atomic write: tmp file + os.replace, guarded by a thread lock."""
    with _FILE_LOCK:
        ensure_paths()
        state.updated_at = datetime.now(config.KST).isoformat()
        payload = serialize(state)
        tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        return state
