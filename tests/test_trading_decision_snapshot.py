from app.ui.trading_decision_snapshot import (
    cycle_status_label,
    selected_completed_decision_snapshot,
    snapshot_field,
)


def _field(value, snapshot_id="done-110449", calculated_at="2026-07-21T11:04:49"):
    return {"value": value, "snapshot_id": snapshot_id, "calculated_at": calculated_at}


def test_running_cycle_renders_only_previous_completed_snapshot():
    completed = {
        "snapshot_id": "done-110449",
        "calculated_at": "2026-07-21T11:04:49",
        "raw_score_leader": _field("HOLD"),
        "enhanced_score": _field(55.1),
        "live_trade_direction": _field("NONE"),
        "final_action": _field("HOLD"),
        "orders_this_cycle": [],
    }
    state = {
        "last_completed_decision_snapshot": completed,
        "last_decision": {"enhanced_score": 72.2, "inverse_pressure_score": 27.8},
        "last_pipeline_trace": {"orders_this_cycle": [{"symbol": "0193T0"}]},
    }
    cycle_status = {
        "last_cycle_started_at": "2026-07-21T11:07:49",
        "last_cycle_completed_at": "2026-07-21T11:04:49",
    }

    snapshot, meta = selected_completed_decision_snapshot(state, cycle_status)

    assert meta["cycle_status"] == "RUNNING"
    assert meta["status_message"] == "새 사이클 계산 중"
    assert snapshot["snapshot_id"] == "done-110449"
    assert snapshot_field(snapshot, "enhanced_score") == 55.1
    assert snapshot["orders_this_cycle"] == []


def test_completed_snapshot_field_ids_and_times_are_consistent():
    snapshot = {
        "snapshot_id": "done-110449",
        "calculated_at": "2026-07-21T11:04:49",
        "raw_score_leader": _field("HYNIX"),
        "enhanced_score": _field(55.1),
        "live_trade_direction": _field("NONE"),
        "actionable_signal": _field("HOLD"),
        "final_action": _field("HOLD"),
        "block_reason": _field("NO_DIRECTION"),
        "momentum_score": _field(42.0),
        "early_reason": _field("NO_EARLY_SIGNAL"),
    }

    for key in (
        "raw_score_leader",
        "enhanced_score",
        "live_trade_direction",
        "actionable_signal",
        "final_action",
        "block_reason",
        "momentum_score",
        "early_reason",
    ):
        assert snapshot[key]["snapshot_id"] == snapshot["snapshot_id"]
        assert snapshot[key]["calculated_at"] == snapshot["calculated_at"]


def test_score_from_different_cycle_is_not_selected_as_current():
    completed = {
        "snapshot_id": "done-110449",
        "calculated_at": "2026-07-21T11:04:49",
        "raw_score_leader": _field("NEUTRAL"),
        "enhanced_score": _field(55.1),
        "final_action": _field("HOLD"),
    }
    state = {
        "last_completed_decision_snapshot": completed,
        "last_decision": {"enhanced_score": 72.2, "inverse_pressure_score": 27.8},
    }

    snapshot, _ = selected_completed_decision_snapshot(
        state,
        {"cycle_status": "RUNNING"},
    )

    assert snapshot_field(snapshot, "enhanced_score") == 55.1
    assert snapshot.get("decision", {}).get("enhanced_score") != 72.2


def test_cycle_status_label_detects_running_from_timestamps():
    assert cycle_status_label({
        "last_cycle_started_at": "2026-07-21T11:07:49",
        "last_cycle_completed_at": "2026-07-21T11:04:49",
    }) == "RUNNING"
    assert cycle_status_label({
        "last_cycle_started_at": "2026-07-21T11:04:49",
        "last_cycle_completed_at": "2026-07-21T11:07:49",
    }) == "IDLE"
