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
    chase = (
        snap.get("chase_diagnostics")
        or pipeline.get("chase_diagnostics")
        or signal.get("chase_diagnostics")
        or {}
    )
    primary = snapshot_field(snap, "primary_block_reason")
    final_action = snapshot_field(snap, "final_action")
    # Fast Worker completed snapshot owns final Entry Approved / blocking_reason.
    # Main-cycle FAST_WORKER_OWNS_ENTRY is intermediate only.
    entry_approved = pipeline.get("entry_approved")
    if entry_approved is None:
        entry_approved = signal.get("entry_approved")
    if entry_approved is None and snap.get("source") == "FAST_WORKER":
        entry_approved = final_action == "BUY"
    entry_approved_reason = (
        pipeline.get("entry_approved_reason")
        or signal.get("entry_approved_reason")
        or snap.get("entry_approved_reason")
    )
    if entry_approved_reason is None and snap.get("source") == "FAST_WORKER":
        entry_approved_reason = None if entry_approved else primary
    blocking_reason = pipeline.get("blocking_reason")
    if blocking_reason is None and snap.get("source") == "FAST_WORKER" and not entry_approved:
        blocking_reason = primary

    # Asia/Seoul live time gate only — never reuse a stale snapshot's
    # NEW_ENTRY_TIME_CLOSED for display/decision while the window is open.
    try:
        from app.trading.hynix_switch_risk_gate import is_new_entry_allowed
        from app.utils.time_utils import kst_now

        if primary == "NEW_ENTRY_TIME_CLOSED" and is_new_entry_allowed(kst_now()):
            primary = None
            if blocking_reason == "NEW_ENTRY_TIME_CLOSED":
                blocking_reason = None
            if entry_approved_reason == "NEW_ENTRY_TIME_CLOSED":
                entry_approved_reason = None
            if signal.get("block_reason") == "NEW_ENTRY_TIME_CLOSED":
                signal = {**signal, "block_reason": None, "primary_block_reason": None}
    except Exception:
        pass

    return {
        "cycle_id": snap.get("cycle_id") or pipeline.get("cycle_id") or signal.get("cycle_id"),
        "snapshot_id": snap.get("snapshot_id"),
        "source": snap.get("source"),
        "final_action": final_action,
        "primary_block_reason": primary,
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
        "entry_approved": entry_approved,
        "entry_approved_reason": entry_approved_reason,
        "blocking_reason": blocking_reason,
        "resolved_direction": (
            snapshot_field(snap, "resolved_direction")
            or snapshot_field(snap, "live_trade_direction")
            or signal.get("resolved_direction")
        ),
        "target_symbol": snapshot_field(snap, "target_symbol") or signal.get("target_symbol"),
        "chase_diagnostics": chase if isinstance(chase, dict) else {},
    }


def format_chase_diagnostics_caption(chase: dict) -> str:
    """One-line chase diagnostic caption for CHASE_BLOCK UI."""
    if not chase:
        return ""
    parts = [
        f"dir={chase.get('resolved_direction') or '-'}",
        f"symbol={chase.get('target_symbol') or '-'}",
        f"signal={chase.get('signal_price') if chase.get('signal_price') is not None else '-'}",
        f"current={chase.get('current_price') if chase.get('current_price') is not None else '-'}",
        f"chase={chase.get('chase_pct') if chase.get('chase_pct') is not None else '-'}%",
        f"allowed={chase.get('allowed_chase_pct') if chase.get('allowed_chase_pct') is not None else '-'}%",
        f"age={chase.get('signal_age_sec') if chase.get('signal_age_sec') is not None else '-'}s",
        f"basis_etf={chase.get('calculation_basis_etf') or '-'}",
    ]
    return "CHASE_BLOCK · " + " · ".join(parts)


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
