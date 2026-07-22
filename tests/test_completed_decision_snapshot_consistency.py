"""Regression: completed decision snapshot block-reason consistency."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

import app.services.hynix_switch_engine as engine
from app.models import OrderResult
from app.ui.trading_decision_snapshot import (
    completed_snapshot_decision_fields,
    format_reward_risk_display,
    snapshot_field,
)


def _field(value, snapshot_id="snap-1", calculated_at="2026-07-22T10:30:00"):
    return {"value": value, "snapshot_id": snapshot_id, "calculated_at": calculated_at}


def test_same_cycle_diagnostics_and_pipeline_primary_block_match():
    now = datetime(2026, 7, 22, 10, 30, 0)
    state = {
        "last_decision": {"enhanced_score": 75.0, "inverse_pressure_score": 40.0},
        "live_trade_direction": {"direction": "UP"},
    }
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
        "order_sizing_audit": {
            "position_cap": 1.0,
            "target_ratio": 0.30,
            "effective_target_pct": 0.30,
            "calculated_quantity": 10,
            "order_skip_reason": "CHASE_BLOCK",
        },
    }
    engine._update_fast_worker_decision_snapshot(
        state,
        now=now,
        continuation_state=continuation_state,
        early_result={"skipped": True, "reason_code": "POOR_REWARD_RISK"},
    )
    snap = state["last_completed_decision_snapshot"]
    fields = completed_snapshot_decision_fields(snap)

    assert fields["final_action"] == "HOLD"
    assert fields["primary_block_reason"] == "CHASE_BLOCK"
    assert fields["pipeline_primary_block_reason"] == "CHASE_BLOCK"
    assert fields["primary_block_reason"] == snap["pipeline_trace"]["primary_block_reason"]
    assert fields["primary_block_reason"] == snap["signal_summary"]["primary_block_reason"]
    assert "POOR_REWARD_RISK" in (fields["secondary_reasons"] or [])
    assert fields["primary_block_reason"] != "POOR_REWARD_RISK"
    # Same-cycle evidence / edge / RR from one snapshot only.
    assert fields["range_evidence_score"] == 72
    assert fields["expected_net_edge_pct"] == 0.9
    assert fields["reward_risk"] == 2.5
    assert fields["cycle_id"] == now.strftime("%Y%m%d%H%M")
    assert snap["pipeline_trace"]["cycle_id"] == fields["cycle_id"]


def test_hold_shows_exactly_one_final_primary_reason():
    primary, secondary = engine._finalize_primary_and_secondary_block_reasons(
        "CHASE_BLOCK",
        "POOR_REWARD_RISK",
        "PULLBACK_ENTRY",
        reward_risk=2.5,
        min_reward_risk=1.5,
    )
    assert primary == "CHASE_BLOCK"
    assert secondary.count("CHASE_BLOCK") == 0
    assert "POOR_REWARD_RISK" in secondary
    # Inconsistent POOR with RR that would pass must not become primary.
    primary2, secondary2 = engine._finalize_primary_and_secondary_block_reasons(
        "POOR_REWARD_RISK",
        "CHASE_BLOCK",
        reward_risk=2.5,
        min_reward_risk=1.5,
    )
    assert primary2 == "CHASE_BLOCK"
    assert primary2 != "POOR_REWARD_RISK"
    assert "POOR_REWARD_RISK" not in ([primary2] if primary2 else [])


def test_poor_reward_risk_display_shows_value_and_threshold():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 80.0, "inverse_pressure_score": 40.0, "final_action": "HYNIX_BUY"},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.80,
        cost_pct=0.12,
        expected_mfe_pct=0.40,
        expected_mae_pct=0.40,
    )
    assert result["reason_code"] == "POOR_REWARD_RISK"
    assert result["reward_risk"] < result["reward_risk_threshold"]
    assert result["min_reward_risk"] == result["reward_risk_threshold"]

    now = datetime(2026, 7, 22, 10, 31, 0)
    state = {
        "last_decision": {"enhanced_score": 80.0, "inverse_pressure_score": 40.0},
        "live_trade_direction": {"direction": "UP"},
    }
    continuation_state = {
        "last_result": result,
        "last_block_reason": "POOR_REWARD_RISK",
    }
    engine._update_fast_worker_decision_snapshot(state, now=now, continuation_state=continuation_state)
    snap = state["last_completed_decision_snapshot"]
    fields = completed_snapshot_decision_fields(snap)
    assert fields["primary_block_reason"] == "POOR_REWARD_RISK"
    assert fields["final_action"] == "HOLD"
    display = format_reward_risk_display(snap)
    assert str(fields["reward_risk"]) in display
    assert "threshold" in display
    assert str(fields["reward_risk_threshold"]) in display
    # Forbid RR-passing value while primary is POOR_REWARD_RISK.
    assert float(fields["reward_risk"]) < float(fields["reward_risk_threshold"])


def test_stale_poor_reward_risk_with_passing_rr_is_demoted():
    """Root-cause regression: RR=2.5 must not keep POOR_REWARD_RISK as primary."""
    now = datetime(2026, 7, 22, 10, 32, 0)
    state = {
        "last_decision": {"enhanced_score": 75.0, "inverse_pressure_score": 40.0},
        "live_trade_direction": {"direction": "UP"},
    }
    continuation_state = {
        "last_result": {
            "action": "ENTER",
            "reason_code": "PULLBACK_ENTRY",
            "evidence_score": 72,
            "expected_net_edge_pct": 0.9,
            "reward_risk": 2.5,
            "reward_risk_threshold": 1.5,
            "min_reward_risk": 1.5,
            "structural_signal_label": "PULLBACK",
            "hard_blocks": ["POOR_REWARD_RISK"],  # stale / mismatched
        },
        "last_block_reason": "CHASE_BLOCK",
    }
    engine._update_fast_worker_decision_snapshot(
        state,
        now=now,
        continuation_state=continuation_state,
        early_result={"skipped": True, "reason_code": "POOR_REWARD_RISK"},
    )
    snap = state["last_completed_decision_snapshot"]
    fields = completed_snapshot_decision_fields(snap)
    assert fields["primary_block_reason"] == "CHASE_BLOCK"
    assert fields["reward_risk"] == 2.5
    assert fields["primary_block_reason"] != "POOR_REWARD_RISK"
    assert "threshold" not in format_reward_risk_display(snap) or fields["primary_block_reason"] != "POOR_REWARD_RISK"


def test_buy_approved_order_coordinator_and_broker_called_once(monkeypatch):
    from app.trading import exit_order_coordinator as order_coord
    from app.trading.hynix_switch_position_manager import run_switch_or_entry
    from app.trading.hynix_symbols import LONG_SYMBOL

    order_coord.reset_for_tests()
    broker_buy_calls: list = []
    coord_calls = {"n": 0}

    class _Broker:
        def get_positions(self):
            return []

        def get_buyable_cash(self):
            return 10_000_000.0

        def get_balance(self):
            return 10_000_000.0

        def buy(self, symbol, name, quantity, price, order_type="limit"):
            broker_buy_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
            return OrderResult(
                success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                side="buy", quantity=quantity, price=price, order_type=order_type,
                order_id=f"B{len(broker_buy_calls)}", message="ok",
            )

        def sell(self, *args, **kwargs):
            raise AssertionError("sell should not run")

    real_coordinated = order_coord.coordinated_order

    def _counting_coordinated(*args, **kwargs):
        coord_calls["n"] += 1
        return real_coordinated(*args, **kwargs)

    monkeypatch.setattr(order_coord, "coordinated_order", _counting_coordinated)
    monkeypatch.setattr(
        "app.trading.hynix_switch_position_manager.has_any_slope_data",
        lambda *_a, **_k: False,
    )

    now = datetime(2026, 7, 22, 10, 33, 0)
    state = {
        "mode": "mock",
        "position": {},
        "adaptive_regime": {"confirmed_regime": "STRONG_UP", "position_cap": 1.0},
        "weighted_entry_controller_only": False,
    }
    result = run_switch_or_entry(
        state, _Broker(), "HYNIX_BUY", 10_000.0, 5_000.0, now=now,
        forced=True, reason="WEIGHTED_ORDER_CONTROLLER",
        target_position_pct=0.30,
        entry_type="WEIGHTED_RANGE_ENTRY",
        signal_source="WEIGHTED_ORDER_CONTROLLER",
    )
    assert result.get("failure_code") not in ("DATA_INSUFFICIENT_POSITION_CAP_ZERO",)
    assert coord_calls["n"] == 1
    assert len(broker_buy_calls) == 1
    assert broker_buy_calls[0]["symbol"] == LONG_SYMBOL
    order_coord.reset_for_tests()


def test_main_cycle_snapshot_syncs_pipeline_primary_block():
    now = datetime(2026, 7, 22, 10, 34, 0)
    state = {
        "trend_continuation_entry": {
            "last_result": {
                "action": "HOLD",
                "reason_code": "POOR_REWARD_RISK",
                "evidence_score": 60,
                "expected_net_edge_pct": 0.2,
                "reward_risk": 1.1,
                "reward_risk_threshold": 1.5,
                "min_reward_risk": 1.5,
                "structural_signal_label": "HOLD",
                "hard_blocks": ["POOR_REWARD_RISK"],
                "entry_path": "NONE",
            },
            "last_block_reason": "POOR_REWARD_RISK",
        },
        "live_trade_direction": {"direction": "UP"},
    }
    trace = {
        "order_sent": False,
        "broker_executed": False,
        "entry_approved": False,
        "primary_block_reason": "STALE_OTHER_REASON",
        "signal_summary": {
            "raw_score_leader": "HYNIX",
            "block_reason": "STALE_OTHER_REASON",
            "primary_block_reason": "STALE_OTHER_REASON",
        },
        "early_decision": {},
    }
    snap = engine._build_completed_decision_snapshot(
        enhanced_result={},
        decision={"enhanced_score": 70.0, "inverse_pressure_score": 40.0},
        trace=trace,
        state=state,
        now=now,
        orders_this_cycle=[],
        new_entry_allowed_now=True,
    )
    fields = completed_snapshot_decision_fields(snap)
    assert fields["primary_block_reason"] == "POOR_REWARD_RISK"
    assert snap["pipeline_trace"]["primary_block_reason"] == "POOR_REWARD_RISK"
    assert snap["signal_summary"]["primary_block_reason"] == "POOR_REWARD_RISK"
    assert fields["final_action"] == "HOLD"
    assert snapshot_field(snap, "final_action") == "HOLD"
    display = format_reward_risk_display(snap)
    assert "1.1" in display
    assert "threshold" in display
    assert "1.5" in display
