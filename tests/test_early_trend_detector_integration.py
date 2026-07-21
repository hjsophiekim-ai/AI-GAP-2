"""
test_early_trend_detector_integration.py — Early Trend Detector는 입력·SHADOW만.

2026-07-22: LIVE 신규매수는 WEIGHTED_ORDER_CONTROLLER 전용. Early Detector tick은
진단/SHADOW만 수행하고 broker BUY를 내지 않는다. 철수(EARLY_PROBE 태그 포지션)는
Dynamic Exit Watcher 경로를 그대로 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import app.services.hynix_switch_state as state_module
import app.services.hynix_switch_engine as engine_module
import app.trading.dynamic_exit_watcher as watcher
from app.data_sources.hynix_long_collector import LONG_SYMBOL as HYNIX_SYMBOL, LONG_NAME as HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME
from app.trading.hynix_symbols import SIGNAL_SYMBOL
from app.models import OrderResult
from app.services.hynix_switch_state import default_state, save_state_atomic, load_state
from app.trading.dry_run_broker import DryRunBroker
from app.trading.hynix_position_common import HynixPositionManager

NOW = datetime(2026, 7, 20, 10, 0, 0)


class _RepeatableBuyBroker:
    def __init__(self, quantity: int = 0, avg_price: float = 0.0, cash: float = 10_000_000.0):
        self.quantity = quantity
        self.avg_price = avg_price
        self.symbol = HYNIX_SYMBOL
        self.name = HYNIX_NAME
        self.cash = cash
        self.buy_calls: list = []
        self.sell_calls: list = []

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, quantity, price))
        total_cost = self.avg_price * self.quantity + price * quantity
        self.symbol = symbol
        self.name = name
        self.quantity += quantity
        self.avg_price = total_cost / self.quantity if self.quantity else price
        self.cash -= price * quantity
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id="B1", message="ok",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        self.quantity = max(0, self.quantity - quantity)
        self.cash += price * quantity
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type=order_type,
            order_id="S1", message="ok",
        )

    def get_positions(self):
        if self.quantity <= 0:
            return []
        return [{"symbol": self.symbol, "name": self.name, "quantity": self.quantity, "avg_price": self.avg_price}]

    def get_buyable_cash(self):
        return self.cash


def _strong_signal(direction: str = "UP") -> dict:
    if direction == "UP":
        return {
            "direction": "UP", "up_votes": 6, "down_votes": 0, "volume_ratio": 2.0,
            "returns": {"1m": 0.3, "3m": 0.8, "5m": 1.0}, "top_factors": [],
        }
    return {
        "direction": "DOWN", "up_votes": 0, "down_votes": 6, "volume_ratio": 2.0,
        "returns": {"1m": -0.3, "3m": -0.8, "5m": -1.0}, "top_factors": [],
    }


def _flat_state(tmp_path, monkeypatch, *, live: bool = True, mode: str = "mock"):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    broker = _RepeatableBuyBroker(cash=10_000_000.0)
    pm = HynixPositionManager(broker, mode=mode)
    pm.sync(force=True)
    state = default_state(mode)
    state["mode"] = mode
    state["auto_trade_on"] = True
    state["early_trend_detector_enabled"] = True
    state["early_trend_detector_live"] = live
    state["weighted_entry_controller_only"] = True
    return state, broker, pm


def test_flat_entry_is_shadow_only_no_early_probe_buy(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert result["reason_code"] == "EARLY_INPUT_ONLY"
    assert state["position"].get("symbol") is None
    assert broker.buy_calls == []
    assert state["actual_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"
    from app.trading import early_trend_detector as etd

    assert state["early_trend_detector"]["stage"] == etd.STAGE_INITIAL
    assert state["early_trend_detector"]["target_pct"] == pytest.approx(0.30)


def test_live_and_shadow_both_refuse_broker_buy(tmp_path, monkeypatch):
    results = {}
    for mode, live in (("mock", True), ("mock", False)):
        state, broker, pm = _flat_state(tmp_path / f"{mode}_{live}", monkeypatch, live=live, mode="mock")
        result = engine_module._run_early_trend_detector_tick(
            state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
            confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
            hynix_price=100_000.0, inverse_price=5_000.0,
        )
        results[live] = {
            "skipped": result["skipped"],
            "buys": len(broker.buy_calls),
            "symbol": state["position"].get("symbol"),
            "engine": state.get("actual_entry_engine"),
        }
    assert results[True]["buys"] == 0
    assert results[False]["buys"] == 0
    assert results[True]["symbol"] is None
    assert results[True]["engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"


def test_shadow_mode_computes_diagnostics_without_placing_order(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch, live=False)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert "SHADOW" in result["reason"]
    assert state["position"].get("symbol") is None
    assert broker.buy_calls == []
    from app.trading import early_trend_detector as etd

    assert state["early_trend_detector"]["stage"] == etd.STAGE_INITIAL


def test_weak_or_flat_signal_does_not_enter(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)
    flat_signal = {"direction": "FLAT", "up_votes": 3, "down_votes": 3, "top_factors": []}

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=flat_signal, df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert state["position"].get("symbol") is None
    assert broker.buy_calls == []


def test_range_regime_early_tick_stays_shadow(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="RANGE", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert result["reason_code"] == "EARLY_INPUT_ONLY"
    assert state["early_trend_detector"]["target_pct"] == pytest.approx(0.30)
    assert broker.buy_calls == []


def test_cost_gate_blocks_before_shadow_order_path(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)
    weak_move_signal = {
        "direction": "UP", "up_votes": 6, "down_votes": 0, "volume_ratio": 2.0,
        "returns": {"1m": 0.01, "3m": 0.02, "5m": 0.02}, "top_factors": [],
    }

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=weak_move_signal, df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert "거래비용" in result["reason"] or result["reason_code"] in ("EARLY_INPUT_ONLY", "NO_EARLY_SIGNAL")
    assert broker.buy_calls == []


def test_fake_signal_circuit_breaker_halts_new_entries(tmp_path, monkeypatch):
    from app.trading import early_trend_detector as etd

    state, broker, pm = _flat_state(tmp_path, monkeypatch)
    freq = etd.default_frequency_state()
    freq = etd.register_probe_round_trip_closed(freq, NOW, was_fake_signal_loss=True)
    freq = etd.register_probe_round_trip_closed(freq, NOW, was_fake_signal_loss=True)
    state["early_trend_detector"] = {"frequency": freq}

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert "서킷브레이커" in result["reason"]
    assert broker.buy_calls == []


def test_chase_block_prevents_late_entry_after_reference_price_moved(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)
    state["early_trend_detector"] = {
        "candidate": {
            "direction": "UP",
            "first_detected_at": (NOW - timedelta(seconds=5)).isoformat(),
            "reference_price": 100_000.0,
        },
    }

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_900.0,
        inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert "CHASE_BLOCK" in result["reason"]
    assert broker.buy_calls == []


def test_live_slope_diagnostics_do_not_place_early_buy(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)
    flat_vote_signal = {
        "direction": "FLAT", "up_votes": 3, "down_votes": 3,
        "returns": {"1m": 0.0, "3m": -0.5, "5m": -0.8}, "top_factors": [],
    }
    live_slopes = {
        SIGNAL_SYMBOL: {"direction": "DOWN", "slopes": {5: -0.05, 10: -0.08, 20: -0.12, 30: -0.15}, "windows_available": 4},
        INVERSE_SYMBOL: {"direction": "UP", "slopes": {5: 0.05, 10: 0.08, 20: 0.12, 30: 0.15}, "windows_available": 4},
        HYNIX_SYMBOL: {"direction": "DOWN", "slopes": {5: -0.05, 10: -0.08, 20: -0.12, 30: -0.15}, "windows_available": 4},
    }

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=flat_vote_signal, df_1min=None,
        confirmed_regime="STRONG_DOWN", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0, live_slopes=live_slopes,
    )

    assert result["skipped"] is True
    assert result["reason_code"] == "EARLY_INPUT_ONLY"
    assert broker.buy_calls == []


def _probe_holding_setup(tmp_path, monkeypatch, *, initial_qty: int = 5, initial_avg: float = 100_000.0):
    from app.trading import early_trend_detector as etd

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    broker = _RepeatableBuyBroker(quantity=initial_qty, avg_price=initial_avg)
    pm = HynixPositionManager(broker, mode="mock")
    pm.sync(force=True)

    state = default_state()
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_enabled"] = True
    state["early_trend_detector_live"] = True
    state["weighted_entry_controller_only"] = True
    state["position"] = {
        "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": initial_qty, "avg_price": initial_avg,
        "entry_price": initial_avg, "entry_time": NOW.isoformat(), "entry_type": "EARLY_PROBE",
    }
    probe = etd.default_probe_state()
    probe.update({
        "active": True, "direction": "UP", "detected_at": NOW.isoformat(),
        "signal_reference_price": initial_avg, "stage": "PROBE_5", "position_pct": 0.05,
    })
    state["early_trend_detector"] = {"probe": probe, "frequency": etd.default_frequency_state()}
    return state, broker, pm


def test_early_probe_expansion_is_blocked(tmp_path, monkeypatch):
    state, broker, pm = _probe_holding_setup(tmp_path, monkeypatch)
    before_buys = len(broker.buy_calls)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW + timedelta(seconds=90), fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=101_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert result["reason_code"] == "EARLY_INPUT_ONLY"
    assert len(broker.buy_calls) == before_buys


def _setup_early_probe_holding(tmp_path, monkeypatch, *, current_price, confirmed_regime, last_reconfirmed_seconds_ago=0.0):
    from app.trading.hynix_switch_position_manager import _buy_new as buy_new
    from app.trading import early_trend_detector as etd

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    broker = DryRunBroker(initial_balance=10_000_000.0)
    orders: list = []
    # Seed holding outside live Early path (exit-path unit test only).
    state_seed = default_state("mock")
    state_seed["weighted_entry_controller_only"] = False
    buy_new(
        broker, HYNIX_SYMBOL, current_price=100_000.0, cash_amount=500_000.0, reason="probe seed",
        orders=orders, mode="mock", signal_source="EARLY_TREND_DETECTOR",
    )
    qty = orders[0]["quantity"]

    state = load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["adaptive_regime"] = {"confirmed_regime": confirmed_regime, "snapshot": {}}
    state["position"] = {
        **state["position"], "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": qty,
        "avg_price": 100_000.0, "entry_price": 100_000.0,
        "entry_time": (NOW - timedelta(minutes=2)).isoformat(), "entry_type": "EARLY_PROBE",
    }
    probe = etd.default_probe_state()
    probe.update({
        "active": True, "direction": "UP", "detected_at": (NOW - timedelta(minutes=2)).isoformat(),
        "signal_reference_price": 100_000.0, "stage": "PROBE_5", "position_pct": 0.05,
        "last_reconfirmed_at": (NOW - timedelta(seconds=last_reconfirmed_seconds_ago)).isoformat(),
    })
    state["early_trend_detector"] = {"probe": probe, "frequency": etd.default_frequency_state()}
    save_state_atomic(state)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: current_price)
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: broker)
    return broker


def test_early_probe_hits_fixed_point_four_percent_stop_even_when_confirmed_regime_would_not_yet_trigger(tmp_path, monkeypatch):
    _setup_early_probe_holding(tmp_path, monkeypatch, current_price=99_500.0, confirmed_regime="STRONG_UP")

    decision = watcher.tick(now=NOW)

    assert decision["action"] == "SELL_ALL"
    assert "EARLY_PROBE" in decision["reason"]
    reloaded = load_state(mode="mock")
    assert reloaded["position"]["symbol"] is None
    assert reloaded["stop_loss_source"] == "EARLY_PROBE_EXIT"


def test_early_probe_holds_on_first_opposite_change_point(tmp_path, monkeypatch):
    _setup_early_probe_holding(tmp_path, monkeypatch, current_price=100_050.0, confirmed_regime="STRONG_UP")
    import pandas as pd

    bars = []
    base = datetime(2026, 7, 20, 9, 50)
    for i in range(12):
        price = 100_050.0 - i * 50.0
        bars.append({
            "datetime": base + timedelta(minutes=i), "open": price, "high": price * 1.001,
            "low": price * 0.999, "close": price, "volume": 2000.0,
        })
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: pd.DataFrame(bars))

    decision = watcher.tick(now=NOW)

    assert decision["action"] == "HOLD"


def test_early_probe_exits_after_sixty_seconds_without_reconfirmation(tmp_path, monkeypatch):
    _setup_early_probe_holding(
        tmp_path, monkeypatch, current_price=100_100.0, confirmed_regime="STRONG_UP",
        last_reconfirmed_seconds_ago=61.0,
    )

    decision = watcher.tick(now=NOW)

    assert decision["action"] == "SELL_ALL"
    assert ("60초" in decision["reason"]) or ("30초" in decision["reason"]) or ("소멸" in decision["reason"])


def test_early_probe_holds_when_still_within_all_thresholds(tmp_path, monkeypatch):
    _setup_early_probe_holding(
        tmp_path, monkeypatch, current_price=100_550.0, confirmed_regime="STRONG_UP",
        last_reconfirmed_seconds_ago=5.0,
    )
    import pandas as pd

    bars = []
    base = datetime(2026, 7, 20, 9, 50)
    for i in range(12):
        price = 100_000.0 + i * 50.0
        bars.append({
            "datetime": base + timedelta(minutes=i), "open": price, "high": price * 1.001,
            "low": price * 0.999, "close": price, "volume": 2000.0,
        })
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: pd.DataFrame(bars))

    decision = watcher.tick(now=NOW)

    assert decision["action"] != "SELL_ALL"
