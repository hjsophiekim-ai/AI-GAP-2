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
        "strategy_name": state.strategy_name,
        "strategy_version": state.strategy_version,
        "signal_rule": state.signal_rule,
        "session_started_at": state.session_started_at,
        "session_baseline_bar_ts": state.session_baseline_bar_ts,
        "baseline_relation": state.baseline_relation,
        "worker_instance_id": state.worker_instance_id,
        "primary_previous_diff": state.primary_previous_diff,
        "primary_current_diff": state.primary_current_diff,
        "primary_relation": state.primary_relation,
        "latest_primary_flag": state.latest_primary_flag.value if state.latest_primary_flag else None,
        "latest_primary_signal_id": state.latest_primary_signal_id,
        "provisional_bar_start": state.provisional_bar_start,
        "provisional_bar_end": state.provisional_bar_end,
        "provisional_macd": state.provisional_macd,
        "provisional_signal": state.provisional_signal,
        "provisional_diff": state.provisional_diff,
        "provisional_flag": state.provisional_flag.value if state.provisional_flag else None,
        "provisional_signal_id": state.provisional_signal_id,
        "provisional_detected_at": state.provisional_detected_at,
        "provisional_order_requested_at": state.provisional_order_requested_at,
        "provisional_ordered_bar_ts": state.provisional_ordered_bar_ts,
        "signed_b_shadow_direction": (
            state.signed_b_shadow_direction.value if state.signed_b_shadow_direction else None
        ),
        "signed_b_shadow_hist_last3": list(state.signed_b_shadow_hist_last3 or ()),
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
    latest_primary_raw = raw.get("latest_primary_flag")
    latest_primary_flag = Direction(latest_primary_raw) if latest_primary_raw in _DIRECTION_VALUES else None
    provisional_raw = raw.get("provisional_flag")
    provisional_flag = Direction(provisional_raw) if provisional_raw in _DIRECTION_VALUES else None
    signed_b_raw = raw.get("signed_b_shadow_direction")
    signed_b_shadow = Direction(signed_b_raw) if signed_b_raw in _DIRECTION_VALUES else None
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
        strategy_name=str(raw.get("strategy_name") or config.STRATEGY_NAME),
        strategy_version=str(raw.get("strategy_version") or ""),
        signal_rule=str(raw.get("signal_rule") or ""),
        session_started_at=raw.get("session_started_at"),
        session_baseline_bar_ts=raw.get("session_baseline_bar_ts"),
        baseline_relation=raw.get("baseline_relation"),
        worker_instance_id=raw.get("worker_instance_id"),
        primary_previous_diff=raw.get("primary_previous_diff"),
        primary_current_diff=raw.get("primary_current_diff"),
        primary_relation=raw.get("primary_relation"),
        latest_primary_flag=latest_primary_flag,
        latest_primary_signal_id=raw.get("latest_primary_signal_id"),
        provisional_bar_start=raw.get("provisional_bar_start"),
        provisional_bar_end=raw.get("provisional_bar_end"),
        provisional_macd=raw.get("provisional_macd"),
        provisional_signal=raw.get("provisional_signal"),
        provisional_diff=raw.get("provisional_diff"),
        provisional_flag=provisional_flag,
        provisional_signal_id=raw.get("provisional_signal_id"),
        provisional_detected_at=raw.get("provisional_detected_at"),
        provisional_order_requested_at=raw.get("provisional_order_requested_at"),
        provisional_ordered_bar_ts=raw.get("provisional_ordered_bar_ts"),
        signed_b_shadow_direction=signed_b_shadow,
        signed_b_shadow_hist_last3=tuple(raw.get("signed_b_shadow_hist_last3") or ()),
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
