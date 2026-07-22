"""WEIGHTED_ORDER_CONTROLLER is the sole live new-entry engine (wiring tests)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import app.services.hynix_execution_ledger as ledger
import app.services.hynix_switch_engine as engine
import app.services.hynix_switch_state as state_module
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
from app.data_sources.hynix_long_collector import LONG_SYMBOL
from app.models import OrderResult
from app.services.hynix_switch_state import default_state
from app.trading.hynix_switch_position_manager import run_switch_or_entry
from app.trading import early_trend_live_feed as feed

_MID = datetime(2026, 7, 22, 10, 0, 0)

REQUIRED_BUY_FIELDS = (
    "signal_source",
    "actual_entry_engine",
    "entry_path",
    "weighted_evidence",
    "expected_net_edge",
    "reward_risk",
    "direction_episode_id",
    "decision_snapshot_id",
    "deployed_git_sha",
)


class _BuyingBroker:
    def __init__(self, cash: float = 10_000_000.0):
        self.cash = cash
        self.positions: list = []
        self.buy_calls: list = []

    def buy(self, symbol, name, quantity, price, order_type="limit"):
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


def _seed_up_history(state: dict, now: datetime) -> None:
    history = {}
    for seconds, signal_price, long_price, inverse_price in (
        (30, 100_000.0, 10_000.0, 20_000.0),
        (20, 100_010.0, 10_010.0, 19_980.0),
        (10, 100_020.0, 10_020.0, 19_960.0),
    ):
        ts = now - timedelta(seconds=seconds)
        history = feed.record_price_sample(history, engine.SIGNAL_SYMBOL, signal_price, ts)
        history = feed.record_price_sample(history, engine.HYNIX_SYMBOL, long_price, ts)
        history = feed.record_price_sample(history, engine.INVERSE_SYMBOL, inverse_price, ts)
    state["early_trend_detector"] = {"price_history": history}
    state["live_trade_direction"] = {
        "direction": "UP",
        "direction_held_since": (now - timedelta(seconds=20)).isoformat(),
        "direction_held_seconds": 20.0,
        "direction_episode_id": f"UP:{(now - timedelta(seconds=20)).isoformat()}",
    }


def _seed_down_history(state: dict, now: datetime) -> None:
    history = {}
    for seconds, signal_price, long_price, inverse_price in (
        (30, 100_000.0, 10_000.0, 20_000.0),
        (20, 99_990.0, 9_990.0, 20_020.0),
        (10, 99_980.0, 9_980.0, 20_040.0),
    ):
        ts = now - timedelta(seconds=seconds)
        history = feed.record_price_sample(history, engine.SIGNAL_SYMBOL, signal_price, ts)
        history = feed.record_price_sample(history, engine.HYNIX_SYMBOL, long_price, ts)
        history = feed.record_price_sample(history, engine.INVERSE_SYMBOL, inverse_price, ts)
    state["early_trend_detector"] = {"price_history": history}
    state["live_trade_direction"] = {
        "direction": "DOWN",
        "direction_held_since": (now - timedelta(seconds=20)).isoformat(),
        "direction_held_seconds": 20.0,
        "direction_episode_id": f"DOWN:{(now - timedelta(seconds=20)).isoformat()}",
    }


def _patch_fast_feed(monkeypatch, broker, *, long_price: float, inverse_price: float, signal_price: float):
    import app.data_sources.auto_market_collector as auto_collector_module
    import app.data_sources.hynix_long_collector as long_collector_module
    import app.data_sources.hynix_inverse_collector as inverse_collector_module
    import app.trading.etf_entry_confirmation as etf_confirmation_module

    monkeypatch.setattr(engine, "_create_strategy_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(auto_collector_module, "_fetch_hynix_current_from_kis", lambda mode=None: signal_price)
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": long_price, "stale": False})
    monkeypatch.setattr(inverse_collector_module, "collect_inverse_current", lambda mode=None: {"current_price": inverse_price, "stale": False})
    monkeypatch.setattr(
        engine,
        "_load_etf_own_minute_cache",
        lambda symbol: pd.DataFrame({"close": [long_price * 0.99, long_price * 0.995], "volume": [1000, 1000]}),
    )
    monkeypatch.setattr(etf_confirmation_module, "compute_etf_vwap", lambda df: long_price * 0.995)
    monkeypatch.setattr(
        engine,
        "_run_early_trend_detector_tick",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Early direct-order path must not run")),
    )


def _ledger_buy_rows(tmp_path: Path) -> list[dict]:
    path = ledger._LEDGER_PATH
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if r.get("action") == "BUY" and str(r.get("success")).lower() in ("true", "1")]


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    ledger_path = tmp_path / "hynix_execution_ledger.csv"
    monkeypatch.setattr(ledger, "_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    runtime = {"git_sha": "deadbeef_test_sha", "orders_enabled_by_deployment": True}
    monkeypatch.setattr("app.utils.runtime_info.read_runtime_info", lambda: runtime)
    monkeypatch.setattr(engine, "read_runtime_info", lambda: runtime)
    return ledger_path


def test_valid_up_input_places_one_weighted_buy(isolated_ledger, monkeypatch):
    broker = _BuyingBroker()
    monkeypatch.setattr(engine, "_create_strategy_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(engine, "evaluate_range_weighted_entry", lambda **kwargs: {
        "action": "ENTER", "entry_path": "CONTINUATION", "reason_code": "CONTINUATION_CANDIDATE",
        "evidence_score": 70, "expected_net_edge_pct": 0.8, "reward_risk": 2.0,
        "structural_signal_label": "BUY", "target_pct": 0.30, "score_gap": 40.0,
        "contributions": {"live_direction": 18.0},
    })
    monkeypatch.setattr(engine, "_effective_target_pct_with_adaptive_cap", lambda target, state: {
        "position_cap": 1.0, "target_ratio": 0.30, "effective_target_pct": 0.30, "order_skip_reason": None,
    })
    monkeypatch.setattr(engine, "range_episode_allows_entry", lambda *a, **k: (True, None))
    monkeypatch.setattr(engine, "detect_opposite_episode_transition", lambda **k: True)
    monkeypatch.setattr(engine, "reset_range_episode_probe_state", lambda *a, **k: None)
    monkeypatch.setattr(engine, "update_range_episode_structural_events", lambda *a, **k: None)
    monkeypatch.setattr(engine, "mark_range_reversal_probe_entered", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_load_etf_own_minute_cache", lambda symbol: None)
    import app.trading.strategy_architecture as sa
    monkeypatch.setattr(sa, "chase_hard_block", lambda moved: False)
    monkeypatch.setattr(sa, "entry_timing_ok", lambda held: (True, None))
    monkeypatch.setattr(sa, "episode_gate_blocks_entry", lambda *a, **k: False)
    monkeypatch.setattr(sa, "get_episode_gate_mode", lambda state: "OFF")
    import app.trading.range_weighted_optimize as rwo
    monkeypatch.setattr(rwo, "daily_loss_limit_reached_from_pct", lambda *a, **k: False)
    monkeypatch.setattr(rwo, "resolve_day_regime_from_cache", lambda: "NORMAL")
    _cfg = rwo.get_range_weighted_config()
    monkeypatch.setattr(rwo, "get_range_weighted_config", lambda: _cfg)
    import app.trading.early_trend_detector as etd
    monkeypatch.setattr(etd, "evaluate_cost_gate", lambda *a, **k: {"blocked": False, "cost_pct": 0.12})

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["last_decision"] = {"final_action": "HYNIX_STRONG_BUY", "enhanced_score": 75.5, "inverse_pressure_score": 24.5}
    state["last_enhanced_result"] = {"intraday_momentum_score": 70.0}
    state["adaptive_regime"] = {"confirmed_regime": "RANGE", "confidence": 80.0}
    state["last_completed_decision_snapshot"] = {"snapshot_id": "snap-up-1"}
    state["live_trade_direction"] = {
        "direction": "UP",
        "direction_held_since": (_MID - timedelta(seconds=20)).isoformat(),
        "direction_held_seconds": 20.0,
        "direction_episode_id": "UP:ep-up-1",
    }
    state["trend_continuation_entry"] = {
        "direction": "UP", "direction_episode_id": "UP:ep-up-1",
        "reference_price": 10_000.0, "first_detected_at": (_MID - timedelta(seconds=20)).isoformat(),
    }
    state_module.save_state_atomic(state)

    result = engine._execute_weighted_order_controller_entry(
        state=state, broker=broker, decision=state["last_decision"],
        hynix_price=10_030.0, inverse_price=19_940.0, now=_MID,
    )
    updated = state

    assert result.get("broker_executed") is True
    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0]["symbol"] == LONG_SYMBOL
    assert updated["actual_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"
    assert updated["configured_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"

    buys = _ledger_buy_rows(isolated_ledger)
    assert len(buys) == 1
    row = buys[0]
    assert row["signal_source"] == "WEIGHTED_ORDER_CONTROLLER"
    for field in REQUIRED_BUY_FIELDS:
        assert row.get(field) not in (None, ""), f"missing ledger field {field}"
    assert row["actual_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"
    assert row["deployed_git_sha"] == "deadbeef_test_sha"


def test_valid_down_input_places_one_weighted_inverse_buy(isolated_ledger, monkeypatch):
    broker = _BuyingBroker()
    monkeypatch.setattr(engine, "evaluate_range_weighted_entry", lambda **kwargs: {
        "action": "ENTER", "entry_path": "CONTINUATION", "reason_code": "CONTINUATION_CANDIDATE",
        "evidence_score": 70, "expected_net_edge_pct": 0.8, "reward_risk": 2.0,
        "structural_signal_label": "BUY", "target_pct": 0.30, "score_gap": 40.0,
        "contributions": {"live_direction": 18.0},
    })
    monkeypatch.setattr(engine, "_effective_target_pct_with_adaptive_cap", lambda target, state: {
        "position_cap": 1.0, "target_ratio": 0.30, "effective_target_pct": 0.30, "order_skip_reason": None,
    })
    monkeypatch.setattr(engine, "range_episode_allows_entry", lambda *a, **k: (True, None))
    monkeypatch.setattr(engine, "detect_opposite_episode_transition", lambda **k: True)
    monkeypatch.setattr(engine, "reset_range_episode_probe_state", lambda *a, **k: None)
    monkeypatch.setattr(engine, "update_range_episode_structural_events", lambda *a, **k: None)
    monkeypatch.setattr(engine, "mark_range_reversal_probe_entered", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_load_etf_own_minute_cache", lambda symbol: None)
    import app.trading.strategy_architecture as sa
    monkeypatch.setattr(sa, "chase_hard_block", lambda moved: False)
    monkeypatch.setattr(sa, "entry_timing_ok", lambda held: (True, None))
    monkeypatch.setattr(sa, "episode_gate_blocks_entry", lambda *a, **k: False)
    monkeypatch.setattr(sa, "get_episode_gate_mode", lambda state: "OFF")
    import app.trading.range_weighted_optimize as rwo
    monkeypatch.setattr(rwo, "daily_loss_limit_reached_from_pct", lambda *a, **k: False)
    monkeypatch.setattr(rwo, "resolve_day_regime_from_cache", lambda: "NORMAL")
    _cfg = rwo.get_range_weighted_config()
    monkeypatch.setattr(rwo, "get_range_weighted_config", lambda: _cfg)
    import app.trading.early_trend_detector as etd
    monkeypatch.setattr(etd, "evaluate_cost_gate", lambda *a, **k: {"blocked": False, "cost_pct": 0.12})

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["last_decision"] = {"final_action": "INVERSE_STRONG_BUY", "enhanced_score": 24.5, "inverse_pressure_score": 75.5}
    state["adaptive_regime"] = {"confirmed_regime": "RANGE", "confidence": 80.0}
    state["last_completed_decision_snapshot"] = {"snapshot_id": "snap-down-1"}
    state["live_trade_direction"] = {
        "direction": "DOWN",
        "direction_held_since": (_MID - timedelta(seconds=20)).isoformat(),
        "direction_held_seconds": 20.0,
        "direction_episode_id": "DOWN:ep-1",
    }
    state["trend_continuation_entry"] = {
        "direction": "DOWN", "direction_episode_id": "DOWN:ep-1",
        "reference_price": 20_000.0, "first_detected_at": (_MID - timedelta(seconds=20)).isoformat(),
    }
    state_module.save_state_atomic(state)

    engine._execute_weighted_order_controller_entry(
        state=state, broker=broker, decision=state["last_decision"],
        hynix_price=9_970.0, inverse_price=20_060.0, now=_MID,
    )
    updated = state

    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0]["symbol"] == INVERSE_SYMBOL
    assert updated["actual_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"
    buys = _ledger_buy_rows(isolated_ledger)
    assert len(buys) == 1
    assert buys[0]["signal_source"] == "WEIGHTED_ORDER_CONTROLLER"


def test_early_off_still_allows_weighted_buy(isolated_ledger, monkeypatch):
    broker = _BuyingBroker()
    monkeypatch.setattr(engine, "evaluate_range_weighted_entry", lambda **kwargs: {
        "action": "ENTER", "entry_path": "PULLBACK", "reason_code": "PULLBACK_ENTRY",
        "evidence_score": 72, "expected_net_edge_pct": 0.9, "reward_risk": 2.5,
        "structural_signal_label": "PULLBACK", "target_pct": 0.30, "score_gap": 40.0,
        "contributions": {"live_direction": 18.0},
    })
    monkeypatch.setattr(engine, "_effective_target_pct_with_adaptive_cap", lambda target, state: {
        "position_cap": 1.0, "target_ratio": 0.30, "effective_target_pct": 0.30, "order_skip_reason": None,
    })
    monkeypatch.setattr(engine, "range_episode_allows_entry", lambda *a, **k: (True, None))
    monkeypatch.setattr(engine, "detect_opposite_episode_transition", lambda **k: True)
    monkeypatch.setattr(engine, "reset_range_episode_probe_state", lambda *a, **k: None)
    monkeypatch.setattr(engine, "update_range_episode_structural_events", lambda *a, **k: None)
    monkeypatch.setattr(engine, "mark_range_reversal_probe_entered", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_load_etf_own_minute_cache", lambda symbol: None)
    import app.trading.strategy_architecture as sa
    monkeypatch.setattr(sa, "chase_hard_block", lambda moved: False)
    monkeypatch.setattr(sa, "entry_timing_ok", lambda held: (True, None))
    monkeypatch.setattr(sa, "episode_gate_blocks_entry", lambda *a, **k: False)
    monkeypatch.setattr(sa, "get_episode_gate_mode", lambda state: "OFF")
    import app.trading.range_weighted_optimize as rwo
    monkeypatch.setattr(rwo, "daily_loss_limit_reached_from_pct", lambda *a, **k: False)
    monkeypatch.setattr(rwo, "resolve_day_regime_from_cache", lambda: "NORMAL")
    _cfg = rwo.get_range_weighted_config()
    monkeypatch.setattr(rwo, "get_range_weighted_config", lambda: _cfg)
    import app.trading.early_trend_detector as etd
    monkeypatch.setattr(etd, "evaluate_cost_gate", lambda *a, **k: {"blocked": False, "cost_pct": 0.12})

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_enabled"] = False
    state["early_trend_detector_live"] = False
    state["last_decision"] = {"final_action": "HYNIX_STRONG_BUY", "enhanced_score": 75.5, "inverse_pressure_score": 24.5}
    state["adaptive_regime"] = {"confirmed_regime": "RANGE", "confidence": 80.0}
    state["last_completed_decision_snapshot"] = {"snapshot_id": "snap-early-off"}
    state["live_trade_direction"] = {
        "direction": "UP",
        "direction_held_since": (_MID - timedelta(seconds=20)).isoformat(),
        "direction_held_seconds": 20.0,
        "direction_episode_id": "UP:ep-early-off",
    }
    state["trend_continuation_entry"] = {
        "direction": "UP", "direction_episode_id": "UP:ep-early-off",
        "reference_price": 10_000.0, "first_detected_at": (_MID - timedelta(seconds=20)).isoformat(),
    }
    state_module.save_state_atomic(state)

    result = engine._execute_weighted_order_controller_entry(
        state=state, broker=broker, decision=state["last_decision"],
        hynix_price=10_030.0, inverse_price=19_940.0, now=_MID,
    )
    updated = state

    assert result.get("reason") != "EARLY_DISABLED_PRICE_FEED_ONLY"
    assert updated["actual_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"
    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0]["symbol"] == LONG_SYMBOL
    buys = _ledger_buy_rows(isolated_ledger)
    assert len(buys) == 1
    assert buys[0]["signal_source"] == "WEIGHTED_ORDER_CONTROLLER"


def test_legacy_and_active_sources_cannot_buy_when_weighted_only(isolated_ledger):
    state = default_state("mock")
    state["weighted_entry_controller_only"] = True
    broker = _BuyingBroker()
    now = _MID

    for source, entry_type in (
        ("ENHANCED_LEGACY", "NORMAL"),
        ("ENHANCED_REGIME_SWITCH", "NORMAL"),
        ("ACTIVE_STRATEGY_MOCK", "NORMAL"),
        ("ACTIVE_ONLY", "NORMAL"),
        ("ACTIVE_FUSION", "NORMAL"),
        ("EARLY_TREND_DETECTOR", "EARLY_PROBE"),
        ("EARLY_PROBE_INITIAL", "EARLY_PROBE"),
    ):
        broker.buy_calls.clear()
        result = run_switch_or_entry(
            state, broker, "HYNIX_BUY", 10_000.0, 20_000.0, now=now,
            signal_source=source, entry_type=entry_type, forced=True,
        )
        assert result["failure_code"] == "LEGACY_ENTRY_SOURCE_BLOCKED", source
        assert broker.buy_calls == []

    buys = _ledger_buy_rows(isolated_ledger)
    assert buys == []


def test_weighted_buy_records_all_required_ledger_fields(isolated_ledger):
    state = default_state("mock")
    state["weighted_entry_controller_only"] = True
    state["mode"] = "mock"
    broker = _BuyingBroker()
    audit = {
        "actual_entry_engine": "WEIGHTED_ORDER_CONTROLLER_LIVE",
        "entry_path": "CONTINUATION",
        "weighted_evidence": '{"live_direction": 18.0}',
        "expected_net_edge": 0.42,
        "reward_risk": 2.5,
        "direction_episode_id": "UP:ep-1",
        "decision_snapshot_id": "snap-ledger",
        "deployed_git_sha": "deadbeef_test_sha",
        "episode_id": "UP:ep-1",
    }
    result = run_switch_or_entry(
        state, broker, "HYNIX_BUY", 10_000.0, 20_000.0, now=_MID,
        forced=True, reason="WEIGHTED_ORDER_CONTROLLER",
        target_position_pct=0.25, entry_type="WEIGHTED_RANGE_ENTRY",
        signal_source="WEIGHTED_ORDER_CONTROLLER", fusion_metadata=audit,
    )
    assert result["acted"] is True
    assert len(broker.buy_calls) == 1
    buys = _ledger_buy_rows(isolated_ledger)
    assert len(buys) == 1
    row = buys[0]
    for field in REQUIRED_BUY_FIELDS:
        assert row.get(field) not in (None, ""), field
    assert row["signal_source"] == "WEIGHTED_ORDER_CONTROLLER"
    assert row["entry_path"] == "CONTINUATION"
    assert row["actual_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"
