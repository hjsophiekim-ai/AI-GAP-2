"""Helpers for rendering one completed Hynix decision snapshot in the UI."""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def cycle_status_label(cycle_status: dict) -> str:
    explicit = (cycle_status or {}).get("cycle_status")
    if explicit:
        return str(explicit).upper()
    started = _parse_iso((cycle_status or {}).get("last_cycle_started_at"))
    completed = _parse_iso((cycle_status or {}).get("last_cycle_completed_at"))
    if started and (completed is None or started > completed):
        return "RUNNING"
    return "IDLE"


def selected_completed_decision_snapshot(state: dict, cycle_status: dict) -> tuple[dict, dict]:
    """Return the only decision snapshot the UI may render.

    The UI must not mix current in-flight values with the previous completed
    decision. During RUNNING it still renders the last completed snapshot and
    reports that a new cycle is being calculated.
    """
    snapshot = (state or {}).get("last_completed_decision_snapshot") or {}
    status = cycle_status_label(cycle_status)
    meta = {
        "cycle_status": status,
        "is_running": status == "RUNNING",
        "status_message": "새 사이클 계산 중" if status == "RUNNING" else None,
    }
    return snapshot, meta


def snapshot_field(snapshot: dict, key: str, default=None):
    value = (snapshot or {}).get(key)
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return default


def snapshot_field_meta(snapshot: dict, key: str) -> dict:
    value = (snapshot or {}).get(key)
    return value if isinstance(value, dict) else {}
