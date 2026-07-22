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
    assert state["pending_fast_worker_deferral"]["reason_code"] == "FAST_WORKER_OWNS_ENTRY"

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
        "last_block_reason": "FAST_WORKER_ENTRY_NOT_EXECUTED",
        "order_sizing_audit": {
            "position_cap": 1.0,
            "target_ratio": 0.30,
            "effective_target_pct": 0.30,
            "calculated_quantity": 0,
            "order_skip_reason": "FAST_WORKER_ENTRY_NOT_EXECUTED",
        },
    }
    engine._update_fast_worker_decision_snapshot(
        state, now=now + timedelta(seconds=5), continuation_state=continuation_state,
        early_result={"skipped": True, "reason_code": "FAST_WORKER_OWNS_ENTRY"},
    )
    snap = state["last_completed_decision_snapshot"]
    for key in engine._FAST_WORKER_SNAPSHOT_REQUIRED_FIELDS:
        assert key in snap, f"missing required field: {key}"
    assert snap["final_action"]["value"] == "HOLD"
    assert snap["block_reason"]["value"]
    assert snap["primary_block_reason"]["value"]
    assert "coordinator_result" in snap and isinstance(snap["coordinator_result"], dict)
    assert "broker_result" in snap and isinstance(snap["broker_result"], dict)
    assert engine._fast_worker_snapshot_is_complete(snap)
    assert "pending_fast_worker_deferral" not in state


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
