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
    if value is None:
        return default
    return value


def snapshot_field_meta(snapshot: dict, key: str) -> dict:
    value = (snapshot or {}).get(key)
    return value if isinstance(value, dict) else {}


def completed_snapshot_decision_fields(snapshot: dict) -> dict:
    """Read final decision fields from one completed snapshot only.

    UI diagnostics and the order-pipeline panel must use this same view so they
    cannot diverge for the same cycle_id / snapshot_id.
    """
    snap = snapshot or {}
    pipeline = snap.get("pipeline_trace") or {}
    signal = snap.get("signal_summary") or {}
    return {
        "cycle_id": snap.get("cycle_id") or pipeline.get("cycle_id") or signal.get("cycle_id"),
        "snapshot_id": snap.get("snapshot_id"),
        "final_action": snapshot_field(snap, "final_action"),
        "primary_block_reason": snapshot_field(snap, "primary_block_reason"),
        "secondary_reasons": list(snapshot_field(snap, "secondary_reasons", []) or []),
        "range_evidence_score": snapshot_field(snap, "range_evidence_score"),
        "expected_net_edge_pct": snapshot_field(snap, "expected_net_edge_pct"),
        "reward_risk": snapshot_field(snap, "reward_risk"),
        "reward_risk_threshold": (
            snapshot_field(snap, "reward_risk_threshold")
            if snapshot_field(snap, "reward_risk_threshold") is not None
            else snapshot_field(snap, "min_reward_risk")
        ),
        "pipeline_primary_block_reason": (
            pipeline.get("primary_block_reason")
            if pipeline.get("primary_block_reason") is not None
            else signal.get("primary_block_reason")
        ),
    }


def format_reward_risk_display(snapshot: dict) -> str:
    """Show computed RR together with applied threshold when POOR_REWARD_RISK."""
    fields = completed_snapshot_decision_fields(snapshot)
    rr = fields.get("reward_risk")
    thr = fields.get("reward_risk_threshold")
    primary = fields.get("primary_block_reason")
    if rr is None and thr is None:
        return "-"
    if primary == "POOR_REWARD_RISK" and rr is not None and thr is not None:
        return f"{rr} (threshold {thr})"
    if rr is not None and thr is not None and primary == "POOR_REWARD_RISK":
        return f"{rr} (threshold {thr})"
    if rr is not None:
        return str(rr)
    return "-"
