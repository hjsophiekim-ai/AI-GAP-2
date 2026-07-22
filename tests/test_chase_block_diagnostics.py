"""CHASE_BLOCK diagnostics + Fast Worker tick cadence exposure."""

from __future__ import annotations

from datetime import datetime, timedelta

import app.services.hynix_switch_engine as engine
from app.trading.strategy_architecture import CHASE_HARD_BLOCK_PCT
from app.ui.trading_decision_snapshot import (
    completed_snapshot_decision_fields,
    format_chase_diagnostics_caption,
)


def test_build_chase_diagnostics_includes_required_fields():
    now = datetime(2026, 7, 22, 11, 20, 0)
    diag = engine._build_chase_diagnostics(
        resolved_direction="UP",
        target_symbol="0193T0",
        signal_price=10_000.0,
        current_price=10_070.0,
        chase_pct=0.7,
        first_detected_at=(now - timedelta(seconds=12)).isoformat(),
        now=now,
        calculation_basis_etf="0193T0",
    )
    assert diag["resolved_direction"] == "UP"
    assert diag["target_symbol"] == "0193T0"
    assert diag["signal_price"] == 10_000.0
    assert diag["current_price"] == 10_070.0
    assert diag["chase_pct"] == 0.7
    assert diag["allowed_chase_pct"] == float(CHASE_HARD_BLOCK_PCT)
    assert diag["signal_age_sec"] == 12.0
    assert diag["calculation_basis_etf"] == "0193T0"


def test_fast_worker_snapshot_embeds_chase_diagnostics_on_chase_block():
    now = datetime(2026, 7, 22, 11, 21, 0)
    state = {
        "last_decision": {"enhanced_score": 75.0, "inverse_pressure_score": 40.0},
        "live_trade_direction": {"direction": "UP"},
    }
    chase = engine._build_chase_diagnostics(
        resolved_direction="UP",
        target_symbol="0193T0",
        signal_price=10_000.0,
        current_price=10_080.0,
        chase_pct=0.8,
        signal_age_sec=8.5,
        calculation_basis_etf="0193T0",
    )
    continuation_state = {
        "last_result": {
            "action": "ENTER",
            "entry_path": "PULLBACK",
            "reason_code": "PULLBACK_ENTRY",
            "evidence_score": 72,
            "expected_net_edge_pct": 0.9,
            "reward_risk": 2.5,
            "reward_risk_threshold": 1.5,
            "min_reward_risk": 1.5,
            "structural_signal_label": "PULLBACK",
            "hard_blocks": [],
        },
        "last_block_reason": "CHASE_BLOCK",
        "reference_price": 10_000.0,
        "moved_pct_since_signal": 0.8,
        "first_detected_at": (now - timedelta(seconds=8)).isoformat(),
        "chase_diagnostics": chase,
        "order_sizing_audit": {
            "position_cap": 1.0,
            "target_ratio": 0.30,
            "effective_target_pct": 0.30,
            "calculated_quantity": 0,
            "order_skip_reason": "CHASE_BLOCK",
        },
    }
    engine._update_fast_worker_decision_snapshot(
        state,
        now=now,
        continuation_state=continuation_state,
        early_result={
            "skipped": True,
            "reason_code": "CHASE_BLOCK",
            "chase_diagnostics": chase,
        },
    )
    snap = state["last_completed_decision_snapshot"]
    fields = completed_snapshot_decision_fields(snap)
    assert fields["primary_block_reason"] == "CHASE_BLOCK"
    assert fields["final_action"] == "HOLD"
    assert fields["entry_approved"] is False
    assert fields["entry_approved_reason"] == "CHASE_BLOCK"
    assert fields["blocking_reason"] == "CHASE_BLOCK"
    assert fields["chase_diagnostics"]["chase_pct"] == 0.8
    assert fields["chase_diagnostics"]["allowed_chase_pct"] == float(CHASE_HARD_BLOCK_PCT)
    assert fields["chase_diagnostics"]["calculation_basis_etf"] == "0193T0"
    assert snap["pipeline_trace"]["chase_diagnostics"]["signal_price"] == 10_000.0
    assert snap["signal_summary"]["chase_diagnostics"]["current_price"] == 10_080.0
    caption = format_chase_diagnostics_caption(fields["chase_diagnostics"])
    assert "chase=0.8%" in caption
    assert "basis_etf=0193T0" in caption


def test_fast_worker_owns_entry_never_final_primary():
    primary, secondary = engine._finalize_primary_and_secondary_block_reasons(
        "FAST_WORKER_OWNS_ENTRY",
        "CHASE_BLOCK",
        "MAIN_CYCLE_ENTRY_DEFERRED: FAST_WORKER_OWNS_ENTRY",
        reward_risk=2.5,
        min_reward_risk=1.5,
    )
    assert primary == "CHASE_BLOCK"
    assert primary != "FAST_WORKER_OWNS_ENTRY"
    assert engine._is_fast_worker_non_owner_reason("FAST_WORKER_OWNS_ENTRY")
    assert engine._is_fast_worker_non_owner_reason("MAIN_CYCLE_ENTRY_DEFERRED: FAST_WORKER_OWNS_ENTRY")


def test_record_fast_worker_tick_intervals():
    state: dict = {}
    t0 = datetime(2026, 7, 22, 11, 30, 0)
    for i in range(5):
        engine._record_fast_worker_tick(state, now=t0 + timedelta(seconds=5 * i))
    assert state["last_fast_worker_tick_at"] == (t0 + timedelta(seconds=20)).isoformat()
    assert len(state["fast_worker_tick_at_history"]) == 5
    assert state["fast_worker_recent_tick_intervals_sec"] == [5.0, 5.0, 5.0, 5.0]


def test_scheduler_records_tick_intervals():
    from app.services import hynix_auto_trade_scheduler as sched

    with sched._status_lock:
        sched._fast_status["_tick_at_history"] = []
        sched._fast_status["last_tick_at"] = None
        sched._fast_status["recent_tick_intervals_sec"] = []

    t0 = datetime(2026, 7, 22, 11, 40, 0)
    for i in range(3):
        with sched._status_lock:
            sched._record_fast_status_tick(t0 + timedelta(seconds=5 * i))

    status = sched.get_fast_status()
    assert status["last_tick_at"] == (t0 + timedelta(seconds=10)).isoformat()
    assert status["recent_tick_intervals_sec"] == [5.0, 5.0]
