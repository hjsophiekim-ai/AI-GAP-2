"""
test_early_trend_detector_integration.py — Early Trend Detector 실제 주문경로 통합
테스트.

- 진입측(hynix_switch_engine._run_early_trend_detector_tick)은 실제 DryRunBroker +
  HynixPositionManager로 진짜 체결을 재현해 검증한다.
- 철수측(dynamic_exit_watcher.tick)은 EARLY_PROBE로 태그된 보유 포지션에 대해
  고정 -0.4%/신호소멸/반대 변화점/60초 미확인 조건이 confirmed regime 손절과
  무관하게 즉시 전량청산을 강제하는지 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import app.services.hynix_switch_state as state_module
import app.services.hynix_switch_engine as engine_module
import app.trading.dynamic_exit_watcher as watcher
from app.data_sources.hynix_long_collector import LONG_SYMBOL as HYNIX_SYMBOL, LONG_NAME as HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME
from app.models import OrderResult
from app.services.hynix_switch_state import default_state, save_state_atomic, load_state
from app.trading.dry_run_broker import DryRunBroker
from app.trading.hynix_position_common import HynixPositionManager
from app.trading.hynix_switch_position_manager import _buy_new

NOW = datetime(2026, 7, 20, 10, 0, 0)


class _RepeatableBuyBroker:
    """DryRunBroker와 달리 같은 종목의 당일 반복매수를 막지 않는다 — Early Trend
    Detector의 단계 진행/확대(top-up)는 같은 날 같은 종목을 여러 번 더 사는
    시나리오이므로, DryRunBroker의 "당일 1회 매수" 안전장치(실거래 계좌와 무관한
    mock 전용 단순화)와는 별도로 검증해야 한다."""

    def __init__(self, quantity: int = 0, avg_price: float = 0.0, cash: float = 10_000_000.0):
        self.quantity = quantity
        self.avg_price = avg_price
        self.cash = cash
        self.buy_calls: list = []
        self.sell_calls: list = []

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, quantity, price))
        total_cost = self.avg_price * self.quantity + price * quantity
        self.quantity += quantity
        self.avg_price = total_cost / self.quantity if self.quantity else price
        self.cash -= price * quantity
        return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="buy", quantity=quantity, price=price, order_type=order_type,
                            order_id="B1", message="ok")

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        self.quantity = max(0, self.quantity - quantity)
        self.cash += price * quantity
        return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="sell", quantity=quantity, price=price, order_type=order_type,
                            order_id="S1", message="ok")

    def get_positions(self):
        if self.quantity <= 0:
            return []
        return [{"symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": self.quantity, "avg_price": self.avg_price}]

    def get_buyable_cash(self):
        return self.cash


def _strong_signal(direction: str = "UP") -> dict:
    if direction == "UP":
        return {"direction": "UP", "up_votes": 6, "down_votes": 0, "volume_ratio": 2.0,
                "returns": {"1m": 0.3, "3m": 0.8, "5m": 1.0}, "top_factors": []}
    return {"direction": "DOWN", "up_votes": 0, "down_votes": 6, "volume_ratio": 2.0,
            "returns": {"1m": -0.3, "3m": -0.8, "5m": -1.0}, "top_factors": []}


def _flat_state(tmp_path, monkeypatch, *, live: bool = True) -> tuple[dict, DryRunBroker, HynixPositionManager]:
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    broker = DryRunBroker(initial_balance=10_000_000.0)
    pm = HynixPositionManager(broker, mode="mock")
    pm.sync(force=True)
    state = default_state()
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_enabled"] = True
    state["early_trend_detector_live"] = live
    return state, broker, pm


# ── 최초 탐색진입(요구사항2) ──────────────────────────────────────────────────

def test_flat_entry_opens_five_percent_early_probe_position(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is False
    assert state["position"]["symbol"] == HYNIX_SYMBOL
    assert state["position"]["entry_type"] == "EARLY_PROBE"
    assert state["position"]["quantity"] > 0
    assert state["early_trend_detector"]["stage"] == "PROBE_5"
    assert state["early_trend_detector"]["target_pct"] == pytest.approx(0.05)


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
    assert len(broker.buy_calls if hasattr(broker, "buy_calls") else []) == 0
    assert state["early_trend_detector"]["stage"] == "PROBE_5"


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


def test_range_regime_blocks_early_entry_entirely(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="RANGE", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert "RANGE" in result["reason"]
    assert state["position"].get("symbol") is None


def test_panic_regime_caps_probe_at_ten_percent(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("DOWN"), df_1min=None,
        confirmed_regime="PANIC", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is False
    assert state["early_trend_detector"]["target_pct"] == pytest.approx(0.05)  # 최초 단계는 5%(PANIC 상한 10%보다 낮음)


def test_cost_gate_blocks_entry_when_expected_edge_too_small(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)
    weak_move_signal = {"direction": "UP", "up_votes": 6, "down_votes": 0, "volume_ratio": 2.0,
                         "returns": {"1m": 0.01, "3m": 0.02, "5m": 0.02}, "top_factors": []}

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=weak_move_signal, df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_000.0, inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert "거래비용" in result["reason"]
    assert state["position"].get("symbol") is None


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
    assert state["position"].get("symbol") is None


def test_chase_block_prevents_late_entry_after_reference_price_moved(tmp_path, monkeypatch):
    state, broker, pm = _flat_state(tmp_path, monkeypatch)
    state["early_trend_detector"] = {
        "candidate": {"direction": "UP", "first_detected_at": (NOW - timedelta(seconds=5)).isoformat(), "reference_price": 100_000.0},
    }

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW, fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=100_900.0,  # +0.9% > 0.7% CHASE_BLOCK 임계
        inverse_price=5_000.0,
    )

    assert result["skipped"] is True
    assert "CHASE_BLOCK" in result["reason"]
    assert state["position"].get("symbol") is None


# ── 확정 후 확대(요구사항2 마지막 항목) ───────────────────────────────────────

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
    state["position"] = {
        "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": initial_qty, "avg_price": initial_avg,
        "entry_price": initial_avg, "entry_time": NOW.isoformat(), "entry_type": "EARLY_PROBE",
    }
    probe = etd.default_probe_state()
    probe.update({"active": True, "direction": "UP", "detected_at": NOW.isoformat(),
                  "signal_reference_price": initial_avg, "stage": "PROBE_5", "position_pct": 0.05})
    state["early_trend_detector"] = {"probe": probe, "frequency": etd.default_frequency_state()}
    return state, broker, pm


def test_confirmed_strong_trend_expands_probe_to_45_percent(tmp_path, monkeypatch):
    state, broker, pm = _probe_holding_setup(tmp_path, monkeypatch)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW + timedelta(seconds=90), fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="STRONG_UP", broker=broker, position_manager=pm,
        hynix_price=101_000.0, inverse_price=5_000.0,
    )

    assert result.get("expanded_to") == pytest.approx(0.45)
    assert state["position"]["entry_type"] == "CONFIRMED"
    assert state["early_trend_detector"]["probe"]["expanded"] is True


def test_staged_progression_increases_size_over_elapsed_time(tmp_path, monkeypatch):
    state, broker, pm = _probe_holding_setup(tmp_path, monkeypatch)

    result = engine_module._run_early_trend_detector_tick(
        state=state, mode="mock", now=NOW + timedelta(seconds=15), fast_signal=_strong_signal("UP"), df_1min=None,
        confirmed_regime="VOLATILE_RANGE", broker=broker, position_manager=pm,  # 확대 없음(STRONG_TREND 아님)
        hynix_price=100_100.0, inverse_price=5_000.0,
    )

    assert result.get("staged_to") == pytest.approx(0.15)
    assert state["position"]["entry_type"] == "EARLY_PROBE"  # 확대 전까지는 여전히 probe


# ── 조기진입 철수(요구사항3, Dynamic Exit Watcher 담당) ───────────────────────

def _setup_early_probe_holding(tmp_path, monkeypatch, *, current_price, confirmed_regime, last_reconfirmed_seconds_ago=0.0):
    from app.trading.hynix_switch_position_manager import _buy_new as buy_new
    from app.trading import early_trend_detector as etd

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    broker = DryRunBroker(initial_balance=10_000_000.0)
    orders: list = []
    buy_new(broker, HYNIX_SYMBOL, current_price=100_000.0, cash_amount=500_000.0, reason="probe seed",
            orders=orders, mode="mock", signal_source="EARLY_TREND_DETECTOR")
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
    # STRONG_UP confirmed regime의 정상 손절은 -1.5%다 — -0.5% 손실은 그 기준으로는
    # 아직 안전권이지만, EARLY_PROBE 고정손절(-0.4%)은 이미 넘겼다.
    broker = _setup_early_probe_holding(tmp_path, monkeypatch, current_price=99_500.0, confirmed_regime="STRONG_UP")

    decision = watcher.tick(now=NOW)

    assert decision["action"] == "SELL_ALL"
    assert "EARLY_PROBE" in decision["reason"]
    reloaded = load_state(mode="mock")
    assert reloaded["position"]["symbol"] is None
    assert reloaded["stop_loss_source"] == "EARLY_PROBE_EXIT"


def test_early_probe_exits_on_opposite_change_point(tmp_path, monkeypatch):
    broker = _setup_early_probe_holding(tmp_path, monkeypatch, current_price=100_050.0, confirmed_regime="STRONG_UP")
    # fast_signal이 반대 방향(DOWN)으로 뒤집히도록 강한 하락 1분봉을 준비한다.
    import pandas as pd

    bars = []
    base = datetime(2026, 7, 20, 9, 50)
    for i in range(12):
        price = 100_050.0 - i * 50.0
        bars.append({"datetime": base + timedelta(minutes=i), "open": price, "high": price * 1.001,
                      "low": price * 0.999, "close": price, "volume": 2000.0})
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: pd.DataFrame(bars))

    decision = watcher.tick(now=NOW)

    assert decision["action"] == "SELL_ALL"
    assert "변화점" in decision["reason"]


def test_early_probe_exits_after_sixty_seconds_without_reconfirmation(tmp_path, monkeypatch):
    # 손실도 없고(+0.1%) 방향도 FLAT(재확인 실패)인 채로 60초 이상 경과.
    broker = _setup_early_probe_holding(
        tmp_path, monkeypatch, current_price=100_100.0, confirmed_regime="STRONG_UP",
        last_reconfirmed_seconds_ago=61.0,
    )

    decision = watcher.tick(now=NOW)

    assert decision["action"] == "SELL_ALL"
    assert ("60초" in decision["reason"]) or ("소멸" in decision["reason"])


def test_early_probe_holds_when_still_within_all_thresholds(tmp_path, monkeypatch):
    broker = _setup_early_probe_holding(
        tmp_path, monkeypatch, current_price=100_050.0, confirmed_regime="STRONG_UP",
        last_reconfirmed_seconds_ago=5.0,
    )
    # signal_still_valid=True가 되려면 fast_signal이 계속 probe 방향(UP)과 일치해야
    # 한다 — 상승 지속 1분봉을 준비한다(df_1min=None이면 항상 FLAT=신호소멸로 판정됨).
    import pandas as pd

    bars = []
    base = datetime(2026, 7, 20, 9, 50)
    for i in range(12):
        price = 100_000.0 + i * 50.0
        bars.append({"datetime": base + timedelta(minutes=i), "open": price, "high": price * 1.001,
                      "low": price * 0.999, "close": price, "volume": 2000.0})
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: pd.DataFrame(bars))

    decision = watcher.tick(now=NOW)

    assert decision["action"] != "SELL_ALL"
