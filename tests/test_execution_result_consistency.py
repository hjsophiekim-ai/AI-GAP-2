"""Regression: execution-result consistency / display / decision wiring."""

from __future__ import annotations

from datetime import datetime, timedelta

import app.services.hynix_switch_engine as engine


def test_up_etf_5_10_both_opposite_forces_hold_with_primary_block():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 80.0, "inverse_pressure_score": 50.0, "final_action": "HYNIX_BUY"},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.80,
        cost_pct=0.12,
        expected_mfe_pct=0.80,
        expected_mae_pct=0.35,
    )
    assert result["action"] == "HOLD"
    assert result["reason_code"] == "ETF_5S_10S_BOTH_OPPOSITE"
    assert result["structural_signal_label"] == "HOLD"

    now = datetime(2026, 7, 22, 10, 40, 0)
    state = {"last_decision": {"enhanced_score": 80.0, "inverse_pressure_score": 50.0}, "live_trade_direction": {"direction": "UP"}}
    continuation_state = {
        "last_result": result,
        "confirm_window_directions": {5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        "last_block_reason": None,
    }
    engine._update_fast_worker_decision_snapshot(state, now=now, continuation_state=continuation_state)
    snap = state["last_completed_decision_snapshot"]
    assert snap["final_action"]["value"] == "HOLD"
    assert snap["primary_block_reason"]["value"] == "ETF_5S_10S_BOTH_OPPOSITE"
    assert snap["block_reason"]["value"] == "ETF_5S_10S_BOTH_OPPOSITE"
    assert snap["structural_signal_label"]["value"] == "HOLD"
    assert not (snap["final_action"]["value"] == "BUY" and not snap["block_reason"]["value"])


def test_chase_block_forces_hold_with_primary_block():
    now = datetime(2026, 7, 22, 10, 41, 0)
    state = {"last_decision": {"enhanced_score": 75.0, "inverse_pressure_score": 40.0}, "live_trade_direction": {"direction": "UP"}}
    continuation_state = {
        "last_result": {
            "action": "ENTER",
            "entry_path": "PULLBACK",
            "reason_code": "PULLBACK_ENTRY",
            "evidence_score": 72,
            "structural_signal_label": "PULLBACK",
            "target_pct": 0.30,
        },
        "last_block_reason": "CHASE_BLOCK",
        "order_sizing_audit": {"position_cap": 1.0, "target_ratio": 0.30, "effective_target_pct": 0.30, "calculated_quantity": 10},
    }
    engine._update_fast_worker_decision_snapshot(
        state, now=now, continuation_state=continuation_state,
        early_result={"skipped": True, "reason_code": "CHASE_BLOCK"},
    )
    snap = state["last_completed_decision_snapshot"]
    assert snap["final_action"]["value"] == "HOLD"
    assert snap["primary_block_reason"]["value"] == "CHASE_BLOCK"
    assert snap["block_reason"]["value"] == "CHASE_BLOCK"
    assert engine._fast_worker_snapshot_is_complete(snap)


def test_structural_hold_forbids_final_action_buy():
    action, block, primary = engine._resolve_consistent_final_action(
        order_ok=True,
        continuation_result={"action": "HOLD", "reason_code": "ETF_5S_10S_BOTH_OPPOSITE", "structural_signal_label": "HOLD"},
        structural_label="HOLD",
        block_candidates=[],
    )
    assert action == "HOLD"
    assert primary == "ETF_5S_10S_BOTH_OPPOSITE"
    assert block == "ETF_5S_10S_BOTH_OPPOSITE"


def test_fast_worker_deferral_requires_completed_snapshot_fields():
    now = datetime(2026, 7, 22, 10, 42, 0)
    state = {
        "last_decision": {"enhanced_score": 70.0, "inverse_pressure_score": 40.0},
        "live_trade_direction": {"direction": "UP"},
    }
    engine._mark_fast_worker_deferral(state, now=now)
    # Deferral marker may still exist for advisory wake, but must not own orders.
    assert "pending_fast_worker_deferral" in state

    continuation_state = {
        "last_result": {
            "action": "ENTER",
            "entry_path": "PULLBACK",
            "reason_code": "PULLBACK_ENTRY",
            "evidence_score": 72,
            "expected_net_edge_pct": 0.9,
            "reward_risk": 2.5,
            "structural_signal_label": "PULLBACK",
            "target_pct": 0.30,
        },
        "last_block_reason": "CHASE_BLOCK",
        "order_sizing_audit": {
            "position_cap": 1.0,
            "target_ratio": 0.30,
            "effective_target_pct": 0.30,
            "calculated_quantity": 0,
            "order_skip_reason": "CHASE_BLOCK",
        },
    }
    engine._update_fast_worker_decision_snapshot(
        state, now=now + timedelta(seconds=5), continuation_state=continuation_state,
        early_result={"skipped": True, "reason_code": "CHASE_BLOCK", "order_permission": "BLOCKED"},
    )
    snap = state["last_completed_decision_snapshot"]
    for key in engine._FAST_WORKER_SNAPSHOT_REQUIRED_FIELDS:
        assert key in snap, f"missing required field: {key}"
    assert snap["final_action"]["value"] == "HOLD"
    assert snap["block_reason"]["value"]
    assert snap["primary_block_reason"]["value"]
    assert not engine._is_fast_worker_non_owner_reason(snap["primary_block_reason"]["value"])
    assert "coordinator_result" in snap and isinstance(snap["coordinator_result"], dict)
    assert "broker_result" in snap and isinstance(snap["broker_result"], dict)
    assert engine._fast_worker_snapshot_is_complete(snap)


def test_actual_entry_engine_display_unified_to_weighted_live():
    now = datetime(2026, 7, 22, 10, 43, 0)
    state = {}
    engine._set_live_entry_engine_state(state, now=now, reason="test")
    assert state["configured_entry_engine"] == engine.WEIGHTED_LIVE_ENTRY_ENGINE
    assert state["actual_entry_engine"] == engine.WEIGHTED_LIVE_ENTRY_ENGINE
    assert state["configured_entry_engine"] == state["actual_entry_engine"]
    assert state["entry_orchestrator"]["mode"] == engine.WEIGHTED_LIVE_ENTRY_ENGINE
    assert state["actual_entry_engine"] != "SHADOW_ONLY"
    assert "SHADOW_ONLY" not in str(state["entry_orchestrator"]["mode"])


def test_final_action_buy_requires_order_or_followup_block():
    # No order → never BUY with empty block
    action, block, primary = engine._resolve_consistent_final_action(
        order_ok=False,
        continuation_result={
            "action": "ENTER",
            "reason_code": "PULLBACK_ENTRY",
            "structural_signal_label": "PULLBACK",
        },
        structural_label="PULLBACK",
        block_candidates=[],
    )
    assert action == "HOLD"
    assert primary
    assert block

    # Order succeeded → BUY with empty block reasons
    action2, block2, primary2 = engine._resolve_consistent_final_action(
        order_ok=True,
        continuation_result={
            "action": "ENTER",
            "reason_code": "PULLBACK_ENTRY",
            "structural_signal_label": "PULLBACK",
        },
        structural_label="PULLBACK",
        block_candidates=[],
    )
    assert action2 == "BUY"
    assert block2 is None
    assert primary2 is None


def test_enhanced_score_does_not_overwrite_live_direction_in_resolver():
    """Diagnostic enhanced leader must not flip an execution HOLD from ETF opposite."""
    result = engine.evaluate_range_weighted_entry(
        decision={
            "enhanced_score": 90.0,
            "inverse_pressure_score": 10.0,
            "final_action": "HYNIX_STRONG_BUY",
        },
        direction="UP",
        live_direction="UP",
        confirm_window_directions={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=1.0,
        expected_move_pct=1.0,
        cost_pct=0.1,
        expected_mfe_pct=1.0,
        expected_mae_pct=0.3,
    )
    assert result["action"] == "HOLD"
    assert result["reason_code"] == "ETF_5S_10S_BOTH_OPPOSITE"
    assert result["structural_signal_label"] == "HOLD"


def test_deferral_completed_snapshot_within_15s_has_required_fields():
    """Fast Worker advisory snapshot within 15s is BUY or clear HOLD — never FW ownership."""
    from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL

    now = datetime(2026, 7, 22, 10, 50, 0)
    state = {
        "last_decision": {"final_action": "HYNIX_BUY", "enhanced_score": 80.0, "inverse_pressure_score": 40.0},
        "live_trade_direction": {"direction": "UP"},
    }
    engine._mark_fast_worker_deferral(state, now=now)
    assert state.get("force_fast_worker_tick") is True

    continuation_state = {
        "last_result": {
            "action": "ENTER",
            "entry_path": "CONTINUATION",
            "reason_code": "CONTINUATION_CANDIDATE",
            "evidence_score": 70,
            "structural_signal_label": "BUY",
            "target_pct": 0.30,
        },
        "last_block_reason": "CHASE_BLOCK",
        "order_sizing_audit": {
            "position_cap": 1.0,
            "target_ratio": 0.30,
            "effective_target_pct": 0.30,
            "calculated_quantity": 0,
            "order_skip_reason": "CHASE_BLOCK",
        },
    }
    engine._update_fast_worker_decision_snapshot(
        state, now=now + timedelta(seconds=5), continuation_state=continuation_state,
    )
    snap = state["last_completed_decision_snapshot"]
    age = engine._deferral_age_seconds(
        {"deferred_at": now.isoformat()}, now + timedelta(seconds=5)
    )
    assert age is not None and age <= engine._FAST_WORKER_DEFERRAL_DEADLINE_SECONDS
    for key in engine._FAST_WORKER_SNAPSHOT_REQUIRED_FIELDS:
        assert key in snap, f"missing required field: {key}"
    assert snap["final_action"]["value"] in ("BUY", "HOLD")
    if snap["final_action"]["value"] == "HOLD":
        assert snap["primary_block_reason"]["value"]
        assert not engine._is_fast_worker_non_owner_reason(snap["primary_block_reason"]["value"])
    assert snap["resolved_direction"]["value"] == "UP"
    assert snap["target_symbol"]["value"] == LONG_SYMBOL == "0193T0"
    assert "coordinator_result" in snap and "broker_result" in snap
    assert engine._fast_worker_snapshot_is_complete(snap)

    # DOWN → 0197X0
    state_down = {
        "last_decision": {"final_action": "HYNIX_BUY", "enhanced_score": 20.0, "inverse_pressure_score": 80.0},
        "live_trade_direction": {"direction": "DOWN"},
    }
    engine._update_fast_worker_decision_snapshot(
        state_down,
        now=now + timedelta(seconds=6),
        continuation_state={
            "last_result": {
                "action": "HOLD",
                "reason_code": "CONTINUATION_TOO_WEAK",
                "structural_signal_label": "HOLD",
                "target_pct": 0.0,
            },
            "last_block_reason": "CONTINUATION_TOO_WEAK",
            "order_sizing_audit": {"position_cap": 1.0, "target_ratio": 0.0, "calculated_quantity": 0},
        },
    )
    snap_down = state_down["last_completed_decision_snapshot"]
    assert snap_down["resolved_direction"]["value"] == "DOWN"
    assert snap_down["target_symbol"]["value"] == SHORT_SYMBOL == "0197X0"
    # Enhanced HYNIX_BUY must not overwrite resolved direction wording.
    assert snap_down["signal_summary"]["resolved_direction"] == "DOWN"
    assert snap_down["signal_summary"]["enhanced_final_action_diagnostic"] == "HYNIX_BUY"
    assert snap_down["final_action"]["value"] == "HOLD"


def test_pending_over_15s_records_error_and_clears_indefinite_pending():
    now = datetime(2026, 7, 22, 11, 0, 0)
    state = {
        "last_decision": {"final_action": "HYNIX_BUY", "enhanced_score": 70.0, "inverse_pressure_score": 40.0},
        "live_trade_direction": {"direction": "UP"},
        "pending_fast_worker_deferral": {
            "reason_code": "FAST_WORKER_OWNS_ENTRY",
            "deferred_at": (now - timedelta(seconds=20)).isoformat(),
            "deadline_seconds": 15.0,
        },
        "last_fast_worker_decision_snapshot": {"cycle_status": "PENDING", "source": "MAIN_CYCLE_DEFERRED"},
    }
    age = engine._deferral_age_seconds(state["pending_fast_worker_deferral"], now)
    assert age is not None and age > engine._FAST_WORKER_DEFERRAL_DEADLINE_SECONDS

    engine._record_fast_worker_snapshot_error(
        state,
        now=now,
        code="FAST_WORKER_SNAPSHOT_TIMEOUT",
        detail="FAST_WORKER_SNAPSHOT_PENDING exceeded 15s",
    )
    engine._request_fast_worker_wake(state, reason="FAST_WORKER_SNAPSHOT_TIMEOUT")
    engine._clear_fast_worker_deferral(state)

    assert state["last_fast_worker_snapshot_error"]["code"] == "FAST_WORKER_SNAPSHOT_TIMEOUT"
    assert state.get("force_fast_worker_tick") is True
    assert "pending_fast_worker_deferral" not in state  # zero indefinite pending


def test_continuation_candidate_not_approved_without_order():
    assert engine._sanitize_continuation_reason_code(
        "CONTINUATION_ENTRY_APPROVED", order_requested=False, order_ok=False
    ) == "CONTINUATION_CANDIDATE"
    assert engine._sanitize_continuation_reason_code(
        "CONTINUATION_ENTRY_APPROVED", order_requested=True, order_ok=False
    ) == "CONTINUATION_ENTRY_APPROVED"
    assert engine._promote_continuation_reason_for_order("CONTINUATION_CANDIDATE") == "CONTINUATION_ENTRY_APPROVED"
    assert engine._sanitize_continuation_reason_code("WAIT_FOR_CONFIRMATION") == "CONFIRMATION_PENDING"

    now = datetime(2026, 7, 22, 11, 5, 0)
    state = {
        "last_decision": {"enhanced_score": 75.0, "inverse_pressure_score": 40.0},
        "live_trade_direction": {"direction": "UP"},
    }
    engine._update_fast_worker_decision_snapshot(
        state,
        now=now,
        continuation_state={
            "last_result": {
                "action": "ENTER",
                "entry_path": "CONTINUATION",
                "reason_code": "CONTINUATION_ENTRY_APPROVED",  # stale/illegal pre-order label
                "structural_signal_label": "BUY",
                "target_pct": 0.30,
            },
            "last_block_reason": "ORDER_NOT_EXECUTED",
            "order_sizing_audit": {
                "position_cap": 1.0,
                "target_ratio": 0.30,
                "effective_target_pct": 0.30,
                "calculated_quantity": 0,
                "order_skip_reason": "ORDER_NOT_EXECUTED",
            },
        },
    )
    snap = state["last_completed_decision_snapshot"]
    assert snap["continuation_reason"]["value"] == "CONTINUATION_CANDIDATE"
    assert snap["final_action"]["value"] == "HOLD"
    assert snap["primary_block_reason"]["value"]
    assert not engine._is_fast_worker_non_owner_reason(snap["primary_block_reason"]["value"])
    # Zero “approved” with neither order nor block result
    assert not (
        snap["continuation_reason"]["value"] == "CONTINUATION_ENTRY_APPROVED"
        and not snap["order_requested"]
        and not snap["primary_block_reason"]["value"]
    )


def test_down_direction_maps_only_to_0197x0():
    from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL, symbol_for_live_direction

    assert symbol_for_live_direction("DOWN") == SHORT_SYMBOL == "0197X0"
    assert symbol_for_live_direction("UP") == LONG_SYMBOL == "0193T0"
    # Enhanced HYNIX_BUY label must not flip DOWN mapping.
    assert symbol_for_live_direction("DOWN") != LONG_SYMBOL
