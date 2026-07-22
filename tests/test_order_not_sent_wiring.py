"""Regression: order-not-sent wiring + Live Direction symbol map + stale fill UI."""

from __future__ import annotations

from datetime import datetime

import pytest

import app.services.hynix_switch_engine as engine
from app.models import OrderResult
from app.trading.adaptive_market_regime import DATA_INSUFFICIENT, STRONG_UP
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL, action_for_live_direction, symbol_for_live_direction
from app.trading.hynix_switch_position_manager import run_switch_or_entry


class _Broker:
    def __init__(self, cash: float = 10_000_000.0, confirm_buy: bool = True):
        self.cash = cash
        self.confirm_buy = confirm_buy
        self.buy_calls = []
        self.holdings: dict = {}

    def get_positions(self):
        return list(self.holdings.values())

    def get_buyable_cash(self):
        return self.cash

    def get_balance(self):
        return self.cash

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        if self.confirm_buy:
            existing = self.holdings.get(symbol, {"symbol": symbol, "name": name, "quantity": 0, "avg_price": price})
            before = int(existing.get("quantity") or 0)
            after = before + int(quantity or 0)
            self.holdings[symbol] = {
                "symbol": symbol, "name": name, "quantity": after,
                "avg_price": price, "hldg_qty": after,
            }
        return OrderResult(
            success=True, mode="dry_run", account_type="dry_run", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id=f"ORD-{len(self.buy_calls)}", message="ok",
        )

    def sell(self, *args, **kwargs):
        raise AssertionError("sell should not run in these tests")


@pytest.fixture(autouse=True)
def _reset_coord():
    from app.trading import exit_order_coordinator as order_coord

    order_coord.reset_for_tests()
    yield
    order_coord.reset_for_tests()


def test_symbol_for_live_direction_up_down_unified():
    assert symbol_for_live_direction("UP") == LONG_SYMBOL
    assert symbol_for_live_direction("DOWN") == SHORT_SYMBOL
    assert action_for_live_direction("UP") == "HYNIX_BUY"
    assert action_for_live_direction("DOWN") == "INVERSE_BUY"
    for label in ("UP", "up", "HYNIX", LONG_SYMBOL):
        assert symbol_for_live_direction(label) == LONG_SYMBOL
    for label in ("DOWN", "down", "INVERSE", SHORT_SYMBOL):
        assert symbol_for_live_direction(label) == SHORT_SYMBOL


def test_buy_approved_cap_gt_zero_broker_buy_once(monkeypatch):
    now = datetime(2026, 7, 22, 10, 30, 0)
    broker = _Broker()
    state = {
        "mode": "mock",
        "position": {},
        "adaptive_regime": {"confirmed_regime": STRONG_UP},
        "weighted_entry_controller_only": False,
    }
    monkeypatch.setattr(
        "app.trading.hynix_switch_position_manager.has_any_slope_data",
        lambda *_a, **_k: False,
    )
    result = run_switch_or_entry(
        state, broker, "HYNIX_BUY", 10_000.0, 5_000.0, now=now,
        forced=True, reason="WEIGHTED_ORDER_CONTROLLER",
        target_position_pct=0.30,
        entry_type="WEIGHTED_RANGE_ENTRY",
        signal_source="WEIGHTED_ORDER_CONTROLLER",
    )
    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0]["symbol"] == LONG_SYMBOL
    assert result.get("failure_code") != "DATA_INSUFFICIENT_POSITION_CAP_ZERO"


def test_buy_approved_cap_zero_hold_with_block_reason(monkeypatch):
    now = datetime(2026, 7, 22, 10, 31, 0)
    broker = _Broker()
    state = {
        "mode": "mock",
        "position": {},
        "adaptive_regime": {"confirmed_regime": DATA_INSUFFICIENT},
        "weighted_entry_controller_only": False,
    }
    monkeypatch.setattr(
        "app.trading.hynix_switch_position_manager.has_any_slope_data",
        lambda *_a, **_k: False,
    )
    result = run_switch_or_entry(
        state, broker, "HYNIX_BUY", 10_000.0, 5_000.0, now=now,
        forced=True, reason="WEIGHTED_ORDER_CONTROLLER",
        target_position_pct=0.30,
        entry_type="WEIGHTED_RANGE_ENTRY",
        signal_source="WEIGHTED_ORDER_CONTROLLER",
    )
    assert broker.buy_calls == []
    assert result["failure_code"] == "DATA_INSUFFICIENT_POSITION_CAP_ZERO"
    assert result.get("requested_qty") == 0

    continuation_state = {
        "last_result": {"action": "ENTER", "entry_path": "PULLBACK", "reason_code": "PULLBACK_ENTRY", "evidence_score": 72},
        "order_sizing_audit": {
            "position_cap": 0.0,
            "target_ratio": 0.30,
            "effective_target_pct": 0.0,
            "calculated_quantity": 0,
            "order_skip_reason": "DATA_INSUFFICIENT_POSITION_CAP_ZERO",
        },
        "last_block_reason": "DATA_INSUFFICIENT_POSITION_CAP_ZERO",
    }
    snap_state = {"last_decision": {}, "live_trade_direction": {"direction": "UP"}, "adaptive_regime": {"confirmed_regime": DATA_INSUFFICIENT}}
    engine._update_fast_worker_decision_snapshot(
        snap_state, now=now, continuation_state=continuation_state,
        early_result={"skipped": True, "reason_code": "DATA_INSUFFICIENT_POSITION_CAP_ZERO"},
    )
    snap = snap_state["last_completed_decision_snapshot"]
    assert snap["final_action"]["value"] == "HOLD"
    assert snap["primary_block_reason"]["value"] == "DATA_INSUFFICIENT_POSITION_CAP_ZERO"
    assert snap["block_reason"]["value"] == "DATA_INSUFFICIENT_POSITION_CAP_ZERO"


def test_down_approved_maps_to_0197x0(monkeypatch):
    now = datetime(2026, 7, 22, 10, 32, 0)
    broker = _Broker()
    state = {
        "mode": "mock",
        "position": {},
        "adaptive_regime": {"confirmed_regime": STRONG_UP},
        "weighted_entry_controller_only": False,
    }
    monkeypatch.setattr(
        "app.trading.hynix_switch_position_manager.has_any_slope_data",
        lambda *_a, **_k: False,
    )
    result = run_switch_or_entry(
        state, broker, action_for_live_direction("DOWN"), 10_000.0, 5_000.0, now=now,
        forced=True, reason="WEIGHTED_ORDER_CONTROLLER",
        target_position_pct=0.25,
        entry_type="WEIGHTED_RANGE_ENTRY",
        signal_source="WEIGHTED_ORDER_CONTROLLER",
    )
    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0]["symbol"] == SHORT_SYMBOL == "0197X0"
    assert result.get("requested_symbol") == SHORT_SYMBOL


def test_up_approved_maps_to_0193t0(monkeypatch):
    now = datetime(2026, 7, 22, 10, 33, 0)
    broker = _Broker()
    state = {
        "mode": "mock",
        "position": {},
        "adaptive_regime": {"confirmed_regime": STRONG_UP},
        "weighted_entry_controller_only": False,
    }
    monkeypatch.setattr(
        "app.trading.hynix_switch_position_manager.has_any_slope_data",
        lambda *_a, **_k: False,
    )
    result = run_switch_or_entry(
        state, broker, action_for_live_direction("UP"), 10_000.0, 5_000.0, now=now,
        forced=True, reason="WEIGHTED_ORDER_CONTROLLER",
        target_position_pct=0.25,
        entry_type="WEIGHTED_RANGE_ENTRY",
        signal_source="WEIGHTED_ORDER_CONTROLLER",
    )
    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0]["symbol"] == LONG_SYMBOL == "0193T0"
    assert result.get("requested_symbol") == LONG_SYMBOL


def test_fast_worker_deferral_not_silent_noop():
    """ENTER approved but no order executed → HOLD with explicit block, never BUY+none."""
    now = datetime(2026, 7, 22, 10, 34, 0)
    state = {"last_decision": {}, "live_trade_direction": {"direction": "UP"}}
    continuation_state = {
        "last_result": {
            "action": "ENTER", "entry_path": "PULLBACK", "reason_code": "PULLBACK_ENTRY",
            "evidence_score": 72, "expected_net_edge_pct": 0.9, "reward_risk": 2.5,
            "structural_signal_label": "PULLBACK", "target_pct": 0.30,
        },
        "last_block_reason": "FAST_WORKER_ENTRY_NOT_EXECUTED",
        "order_sizing_audit": {
            "position_cap": 1.0, "target_ratio": 0.30, "effective_target_pct": 0.30,
            "calculated_quantity": 0, "order_skip_reason": "FAST_WORKER_ENTRY_NOT_EXECUTED",
        },
    }
    engine._mark_fast_worker_deferral(state, now=now)
    engine._update_fast_worker_decision_snapshot(
        state, now=now, continuation_state=continuation_state,
        early_result={"skipped": True, "reason_code": "FAST_WORKER_OWNS_ENTRY"},
    )
    snap = state["last_completed_decision_snapshot"]
    assert snap["final_action"]["value"] == "HOLD"
    assert snap["primary_block_reason"]["value"]
    assert snap["block_reason"]["value"]
    for key in ("final_action", "target_symbol", "ratio", "qty", "block_reason", "order_requested", "coordinator_result", "broker_result"):
        assert key in snap
    assert engine._fast_worker_snapshot_is_complete(snap)
    assert "pending_fast_worker_deferral" not in state


def test_yesterday_execution_message_not_shown_as_today():
    yesterday = "2026-07-21T14:00:00"
    today = "2026-07-22T10:00:00"
    orders = [
        {"timestamp": yesterday, "action": "BUY", "symbol": LONG_SYMBOL, "quantity": 233, "executed_qty": 233, "price": 14475, "success": True},
        {"timestamp": today, "action": "BUY", "symbol": LONG_SYMBOL, "quantity": 10, "executed_qty": 0, "price": 15000, "success": False},
    ]
    kept = engine._orders_are_today(orders, today="20260722")
    assert len(kept) == 1
    assert kept[0]["timestamp"] == today
    assert kept[0]["executed_qty"] == 0
    displayable_confirmed = [
        o for o in kept
        if int(o.get("executed_qty") or 0) > 0 and bool(o.get("success"))
    ]
    assert displayable_confirmed == []


def test_adaptive_cap_info_data_insufficient_is_zero():
    info = engine._adaptive_position_cap_info({"adaptive_regime": {"confirmed_regime": DATA_INSUFFICIENT}})
    assert info["position_cap"] == 0.0
    assert info["block_new_entries"] is True
    sized = engine._effective_target_pct_with_adaptive_cap(0.30, {"adaptive_regime": {"confirmed_regime": DATA_INSUFFICIENT}})
    assert sized["effective_target_pct"] == 0.0
    assert sized["order_skip_reason"] == "DATA_INSUFFICIENT_POSITION_CAP_ZERO"
