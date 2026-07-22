"""Regression A–F: WOC LIVE owns new-entry orders; Fast Worker is diagnostics-only."""

from __future__ import annotations

import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.services.hynix_switch_engine as engine
import app.services.hynix_switch_state as state_module
from app.data_sources.hynix_long_collector import LONG_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
from app.models import OrderResult
from app.trading import exit_order_coordinator as order_coord


_MID = datetime(2026, 7, 22, 10, 30, 0)
_AFTER_CUTOFF = datetime(2026, 7, 22, 14, 55, 0)


class _BuyingBroker:
    def __init__(self, cash: float = 10_000_000.0):
        self.cash = cash
        self.positions: list = []
        self.buy_calls: list = []
        self.lock = threading.Lock()

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        with self.lock:
            self.buy_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
            self.positions = [{"symbol": symbol, "name": name, "quantity": quantity, "avg_price": price}]
            self.cash -= quantity * price
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id=f"W{len(self.buy_calls)}", message="ok",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type=order_type,
            order_id="S1", message="ok",
        )

    def get_positions(self):
        return list(self.positions)

    def get_buyable_cash(self):
        return self.cash

    def get_balance(self):
        return self.cash


@pytest.fixture(autouse=True)
def _reset_coord():
    order_coord.reset_for_tests()
    yield
    order_coord.reset_for_tests()


def _enter_eval(**overrides):
    base = {
        "action": "ENTER",
        "entry_path": "PULLBACK",
        "reason_code": "PULLBACK_ENTRY",
        "evidence_score": 72,
        "expected_net_edge_pct": 0.9,
        "reward_risk": 2.5,
        "structural_signal_label": "PULLBACK",
        "target_pct": 0.30,
        "score_gap": 40.0,
        "contributions": {"live_direction": 18.0},
    }
    base.update(overrides)
    return base


def _patch_woc_gates(monkeypatch, *, enter: bool = True, chase: bool = False):
    monkeypatch.setattr(
        engine,
        "evaluate_range_weighted_entry",
        lambda **kwargs: _enter_eval() if enter else {
            "action": "HOLD",
            "reason_code": "CONTINUATION_TOO_WEAK",
            "structural_signal_label": "HOLD",
            "target_pct": 0.0,
        },
    )
    monkeypatch.setattr(engine, "_effective_target_pct_with_adaptive_cap", lambda target, state: {
        "position_cap": 1.0,
        "target_ratio": float(target or 0.3),
        "effective_target_pct": float(target or 0.3),
        "order_skip_reason": None,
    })
    monkeypatch.setattr(engine, "_load_etf_own_minute_cache", lambda symbol: None)

    import app.trading.strategy_architecture as sa
    monkeypatch.setattr(sa, "chase_hard_block", lambda moved: bool(chase))
    monkeypatch.setattr(sa, "entry_timing_ok", lambda held: (True, None))
    monkeypatch.setattr(sa, "episode_gate_blocks_entry", lambda *a, **k: False)
    monkeypatch.setattr(sa, "get_episode_gate_mode", lambda state: "OFF")

    monkeypatch.setattr(engine, "range_episode_allows_entry", lambda *a, **k: (True, None))
    monkeypatch.setattr(engine, "detect_opposite_episode_transition", lambda **k: True)
    monkeypatch.setattr(engine, "reset_range_episode_probe_state", lambda *a, **k: None)
    monkeypatch.setattr(engine, "update_range_episode_structural_events", lambda *a, **k: None)
    monkeypatch.setattr(engine, "mark_range_reversal_probe_entered", lambda *a, **k: None)

    import app.trading.range_weighted_optimize as rwo
    monkeypatch.setattr(rwo, "daily_loss_limit_reached_from_pct", lambda *a, **k: False)
    monkeypatch.setattr(rwo, "resolve_day_regime_from_cache", lambda: "NORMAL")
    _real_cfg = rwo.get_range_weighted_config()
    monkeypatch.setattr(rwo, "get_range_weighted_config", lambda: _real_cfg)

    import app.trading.early_trend_detector as etd
    monkeypatch.setattr(etd, "evaluate_cost_gate", lambda *a, **k: {"blocked": False, "cost_pct": 0.12})


def _base_state():
    return {
        "mode": "mock",
        "auto_trade_on": True,
        "position": {},
        "live_trade_direction": {
            "direction": "UP",
            "direction_held_seconds": 30.0,
            "direction_held_since": (_MID - __import__("datetime").timedelta(seconds=30)).isoformat(),
            "direction_episode_id": "UP:ep-test",
        },
        "last_decision": {"final_action": "HYNIX_BUY", "enhanced_score": 80.0, "inverse_pressure_score": 20.0},
        "adaptive_regime": {"confirmed_regime": "RANGE", "confidence": 80.0},
        "early_trend_detector": {"live_slopes": {}, "macd_williams_episode": {"confirmed": True}},
        "trend_continuation_entry": {
            "direction": "UP",
            "direction_episode_id": "UP:ep-test",
            "reference_price": 10_000.0,
            "first_detected_at": (_MID - __import__("datetime").timedelta(seconds=30)).isoformat(),
        },
        "configured_entry_engine": engine.WEIGHTED_LIVE_ENTRY_ENGINE,
        "actual_entry_engine": engine.WEIGHTED_LIVE_ENTRY_ENGINE,
        "weighted_entry_controller_only": True,
    }


def test_a_woc_live_buy_risk_ok_flat_no_snapshot_buys_once(monkeypatch):
    """A: WOC LIVE + BUY + Risk OK + flat + session + no FW snapshot → not blocked by FW; buy once."""
    _patch_woc_gates(monkeypatch, enter=True)
    broker = _BuyingBroker()
    state = _base_state()
    # Explicitly no Fast Worker snapshot.
    assert "last_fast_worker_decision_snapshot" not in state

    result = engine._execute_weighted_order_controller_entry(
        state=state,
        broker=broker,
        decision=state["last_decision"],
        hynix_price=10_030.0,
        inverse_price=19_940.0,
        now=_MID,
    )

    assert result["entry_owner"] == engine.WOC_ENTRY_OWNER
    assert result["configured_entry_engine"] == engine.WEIGHTED_LIVE_ENTRY_ENGINE
    assert result["actual_entry_engine"] == engine.WEIGHTED_LIVE_ENTRY_ENGINE
    assert result["entry_approved"] is True
    assert result["order_sent"] is True
    assert result["broker_executed"] is True
    assert result["order_reservation"] == "RESERVED"
    assert result.get("primary_block_reason") not in (
        "FAST_WORKER_OWNS_ENTRY",
        "FAST_WORKER_SNAPSHOT_PENDING",
        "MAIN_CYCLE_ENTRY_DEFERRED",
    )
    assert "MAIN_CYCLE_ENTRY_DEFERRED" not in (result.get("entry_approved_reason") or "")
    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0]["symbol"] == LONG_SYMBOL
    assert state.get("pending_fast_worker_deferral") is None


def test_b_fast_worker_diagnostics_only_zero_buys(monkeypatch, tmp_path):
    """B: Fast Worker diagnostics-only → 0 buys, 0 order ownership claims."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    _patch_woc_gates(monkeypatch, enter=True)
    broker = _BuyingBroker()
    monkeypatch.setattr(engine, "_create_strategy_broker", lambda *a, **kw: broker)

    import app.data_sources.auto_market_collector as auto_collector_module
    import app.data_sources.hynix_long_collector as long_collector_module
    import app.data_sources.hynix_inverse_collector as inverse_collector_module

    monkeypatch.setattr(auto_collector_module, "_fetch_hynix_current_from_kis", lambda mode=None: 100_030.0)
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 10_030.0, "stale": False})
    monkeypatch.setattr(inverse_collector_module, "collect_inverse_current", lambda mode=None: {"current_price": 19_940.0, "stale": False})
    monkeypatch.setattr(
        engine,
        "_run_early_trend_detector_tick",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Early direct-order path must not run")),
    )

    state = state_module.load_state(mode="mock")
    state.update(_base_state())
    state["early_trend_detector_enabled"] = True
    state["early_trend_detector_live"] = True
    state_module.save_state_atomic(state)

    result = engine.run_early_trend_fast_feed_tick(mode="mock", now=_MID)
    early = result.get("early_result") or {}
    assert broker.buy_calls == []
    assert early.get("order_permission") in ("DIAGNOSTIC_ONLY", "BLOCKED", None) or early.get("skipped") is not False
    assert early.get("entry_owner") in (None, "")
    assert "FAST_WORKER_OWNS_ENTRY" not in str(early.get("reason_code") or "")


def test_c_main_and_watcher_concurrent_exactly_one_order(monkeypatch):
    """C: main + watcher concurrent → exactly 1 order (OrderCoordinator)."""
    _patch_woc_gates(monkeypatch, enter=True)
    broker = _BuyingBroker()
    state = _base_state()
    state["trend_continuation_entry"]["direction_episode_id"] = "UP:shared-ep"

    def _run_woc():
        local = dict(state)
        local["trend_continuation_entry"] = dict(state["trend_continuation_entry"])
        return engine._execute_weighted_order_controller_entry(
            state=local,
            broker=broker,
            decision=state["last_decision"],
            hynix_price=10_030.0,
            inverse_price=19_940.0,
            now=_MID,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_run_woc)
        f2 = pool.submit(_run_woc)
        r1, r2 = f1.result(), f2.result()

    assert len(broker.buy_calls) == 1
    winners = [r for r in (r1, r2) if r.get("broker_executed")]
    blocked = [r for r in (r1, r2) if r.get("order_reservation") == "DUPLICATE_ORDER_BLOCKED" or r.get("primary_block_reason") == "DUPLICATE_ORDER_BLOCKED"]
    assert len(winners) == 1
    # Loser may be blocked by coordinator or by WOC_ENTRY_ALREADY_ATTEMPTED after first fill.
    assert len(blocked) + sum(1 for r in (r1, r2) if r.get("primary_block_reason") in ("WOC_ENTRY_ALREADY_ATTEMPTED", "TARGET_ALREADY_FILLED")) >= 1


def test_d_risk_reject_zero_orders_real_reason(monkeypatch, tmp_path):
    """D: Risk reject → 0 orders, primary_block_reason = real risk reason."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.broker_factory as broker_factory_module

    broker = _BuyingBroker()
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(
        enhanced_score_module,
        "calculate_enhanced_hynix_prediction_score",
        lambda mode=None: {
            "enhanced_score": 80.0,
            "inverse_pressure_score": 20.0,
            "intraday_momentum_score": 70.0,
            "hynix_current_price": 100_000.0,
            "inverse_current_price": 5_000.0,
            "data_valid": {"hynix_signal_price": True},
            "market_data": {"hynix_minute": {"df_1min": None}},
            "reason_top5": [],
        },
    )
    monkeypatch.setattr(
        decider_module,
        "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "HYNIX_BUY",
            "enhanced_score": 80.0,
            "inverse_pressure_score": 20.0,
            "score_gap": 60.0,
            "score_gap_below_forced_trade_threshold": False,
            "reasons": ["test"],
        },
    )
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    monkeypatch.setattr(engine, "_run_shadow_cycle_ai_and_decision_v2", lambda *a, **kw: {})

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["stopped"] = True
    state["stopped_reason"] = "RISK_DAILY_LOSS_LIMIT"
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID)
    trace = result["pipeline_trace"]
    assert broker.buy_calls == []
    assert trace["order_sent"] is False
    assert trace["risk_manager_ok"] is False
    reason = (trace.get("risk_manager_reason") or trace.get("primary_block_reason") or "")
    assert "RISK" in reason.upper() or "중단" in reason or "stopped" in reason.lower() or "자동매매" in reason
    assert "FAST_WORKER" not in (trace.get("primary_block_reason") or "")


def test_e_after_1450_zero_orders_new_entry_cutoff(monkeypatch):
    """E: after 14:50 → 0 orders, NEW_ENTRY_CUTOFF."""
    _patch_woc_gates(monkeypatch, enter=True)
    broker = _BuyingBroker()
    state = _base_state()

    result = engine._execute_weighted_order_controller_entry(
        state=state,
        broker=broker,
        decision=state["last_decision"],
        hynix_price=10_030.0,
        inverse_price=19_940.0,
        now=_AFTER_CUTOFF,
    )
    assert broker.buy_calls == []
    assert result["order_sent"] is False
    assert result["primary_block_reason"] == "NEW_ENTRY_CUTOFF"
    assert result["entry_owner"] == engine.WOC_ENTRY_OWNER


def test_f_buy_all_safety_pass_final_not_hold_or_fw_pending(monkeypatch):
    """F: BUY + all safety pass → Final Action not HOLD / not FAST_WORKER_SNAPSHOT_PENDING."""
    _patch_woc_gates(monkeypatch, enter=True)
    broker = _BuyingBroker()
    state = _base_state()

    result = engine._execute_weighted_order_controller_entry(
        state=state,
        broker=broker,
        decision=state["last_decision"],
        hynix_price=10_030.0,
        inverse_price=19_940.0,
        now=_MID,
    )
    assert result["broker_executed"] is True
    action, block, primary = engine._resolve_consistent_final_action(
        order_ok=True,
        continuation_result=result.get("continuation_eval") or _enter_eval(),
        structural_label="PULLBACK",
        block_candidates=[result.get("primary_block_reason"), "FAST_WORKER_SNAPSHOT_PENDING"],
    )
    assert action == "BUY"
    assert block is None
    assert primary is None
    assert primary != "FAST_WORKER_SNAPSHOT_PENDING"
    assert not engine._is_fast_worker_non_owner_reason(primary)


def test_mock_integration_evidence_fields(monkeypatch):
    """Mock integration evidence: BUY judgment, entry_owner, reserve, order sent, fill."""
    _patch_woc_gates(monkeypatch, enter=True)
    broker = _BuyingBroker()
    state = _base_state()

    result = engine._execute_weighted_order_controller_entry(
        state=state,
        broker=broker,
        decision=state["last_decision"],
        hynix_price=10_030.0,
        inverse_price=19_940.0,
        now=_MID,
    )
    evidence = {
        "buy_judgment": (result.get("continuation_eval") or {}).get("action"),
        "entry_owner": result["entry_owner"],
        "entry_approved": result["entry_approved"],
        "order_reservation": result["order_reservation"],
        "order_sent": result["order_sent"],
        "broker_executed": result["broker_executed"],
        "position_confirmed": result["position_confirmed"],
        "post_fill_qty": (state.get("position") or {}).get("quantity"),
        "trade_counter": state.get("daily_trade_count"),
        "broker_calls": list(broker.buy_calls),
        "execution_error": result.get("execution_error"),
    }
    assert evidence["buy_judgment"] == "ENTER"
    assert evidence["entry_owner"] == "WEIGHTED_ORDER_CONTROLLER"
    assert evidence["entry_approved"] is True
    assert evidence["order_reservation"] == "RESERVED"
    assert evidence["order_sent"] is True
    assert evidence["broker_executed"] is True
    assert evidence["position_confirmed"] is True
    assert evidence["post_fill_qty"] and evidence["post_fill_qty"] > 0
    assert evidence["trade_counter"] and evidence["trade_counter"] >= 1
    assert len(evidence["broker_calls"]) == 1


def test_broker_reject_still_logs_request_and_error(monkeypatch):
    class _RejectBroker(_BuyingBroker):
        def buy(self, symbol, name, quantity, price, order_type="limit"):
            self.buy_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
            return OrderResult(
                success=False, mode="mock", account_type="mock", symbol=symbol, name=name,
                side="buy", quantity=quantity, price=price, order_type=order_type,
                order_id="R1", message="reject",
                rt_cd="1", msg_cd="EGW00001", msg1="모의거부",
            )

    _patch_woc_gates(monkeypatch, enter=True)
    broker = _RejectBroker()
    state = _base_state()
    result = engine._execute_weighted_order_controller_entry(
        state=state,
        broker=broker,
        decision=state["last_decision"],
        hynix_price=10_030.0,
        inverse_price=19_940.0,
        now=_MID,
    )
    assert len(broker.buy_calls) == 1  # request was attempted
    assert result["order_sent"] is True
    assert result["broker_executed"] is False
    assert result.get("execution_error") or result.get("switch", {}).get("broker_error") or result.get("switch", {}).get("failure_code")


def test_never_defer_with_fast_worker_owns_entry():
    """Ownership invariant: marking deferral must not be required for WOC LIVE orders."""
    now = _MID
    state = {}
    # Helper still exists for advisory wake, but reason must not gate WOC.
    engine._mark_fast_worker_deferral(state, now=now)
    assert state["pending_fast_worker_deferral"]["reason_code"] == "FAST_WORKER_SNAPSHOT_ADVISORY"
    # Resolve must never promote that to primary when filtered.
    action, block, primary = engine._resolve_consistent_final_action(
        order_ok=False,
        continuation_result=_enter_eval(),
        structural_label="PULLBACK",
        block_candidates=["FAST_WORKER_OWNS_ENTRY", "MAIN_CYCLE_ENTRY_DEFERRED: FAST_WORKER_OWNS_ENTRY", "FAST_WORKER_SNAPSHOT_PENDING"],
    )
    assert action == "HOLD"
    assert not engine._is_fast_worker_non_owner_reason(primary)
    assert primary not in (
        "FAST_WORKER_OWNS_ENTRY",
        "FAST_WORKER_SNAPSHOT_PENDING",
        "MAIN_CYCLE_ENTRY_DEFERRED",
    )
    assert not engine._is_fast_worker_non_owner_reason(None)
    assert engine._is_fast_worker_non_owner_reason("FAST_WORKER_SNAPSHOT_PENDING")
