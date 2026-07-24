from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot, QuoteSnapshot
from app.trading.macd2.signal_engine import make_signal_id
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _state():
    state = worker.RuntimeState()
    state.auto_trade_on = True
    return state


def _snap(bar_dt: datetime, direction: Direction = Direction.UP_RED) -> MacdSnapshot:
    hist = (1.0, 2.0, 3.0) if direction == Direction.UP_RED else (-1.0, -2.0, -3.0)
    if direction == Direction.HOLD:
        hist = (1.0, 2.0, 1.5)
    return MacdSnapshot(bar_dt=bar_dt, macd=0.0, signal=0.0, hist=hist[-1], hist_last3=hist, completed_3m_count=100)


def _svc(prices=None):
    prices = prices or {config.WATCH_SYMBOL: 100.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a: (pd.DataFrame({"datetime": []}), {}),
        fetch_quote=lambda mode, symbol: (prices.get(symbol), None),
    )
    svc.refresh_quotes()
    return svc


def _patch_snap(monkeypatch, snap: MacdSnapshot):
    monkeypatch.setattr(worker, "calculate_macd", lambda _bars: snap)


def test_prior_day_last_up_red_with_no_today_bar_orders_zero(monkeypatch):
    now = datetime(2026, 7, 24, 9, 6, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(datetime(2026, 7, 23, 15, 27, tzinfo=KST), Direction.UP_RED))
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    state = _state()

    result = worker.run_once(broker=broker, market_data=_svc(), state=state, now=now)

    assert broker.orders == []
    assert result.actions == []
    assert ledger.load_signal_ledger() == []


def test_prior_day_last_down_blue_with_no_today_bar_orders_zero(monkeypatch):
    now = datetime(2026, 7, 24, 9, 6, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(datetime(2026, 7, 23, 15, 27, tzinfo=KST), Direction.DOWN_BLUE))
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    state = _state()

    worker.run_once(broker=broker, market_data=_svc(), state=state, now=now)

    assert broker.orders == []
    assert ledger.load_signal_ledger() == []


def test_before_first_completed_today_bar_orders_zero(monkeypatch):
    _patch_snap(monkeypatch, _snap(datetime(2026, 7, 24, 9, 0, tzinfo=KST), Direction.UP_RED))
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    worker.run_once(
        broker=broker, market_data=_svc(), state=_state(), now=datetime(2026, 7, 24, 9, 2, tzinfo=KST),
    )

    assert broker.orders == []


def test_today_date_and_completed_bar_date_mismatch_creates_no_signal(monkeypatch):
    _patch_snap(monkeypatch, _snap(datetime(2026, 1, 6, 15, 27, tzinfo=KST), Direction.UP_RED))

    worker.run_once(
        broker=FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0}),
        market_data=_svc(), state=_state(), now=datetime(2026, 7, 24, 9, 30, tzinfo=KST),
    )

    assert ledger.load_signal_ledger() == []


def test_five_continuous_up_red_condition_bars_create_one_red_flag(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    for i in range(5):
        bar_dt = datetime(2026, 7, 24, 9, 0 + 3 * i, tzinfo=KST)
        _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED))
        worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))

    rows = ledger.load_signal_ledger()
    assert [r["direction"] for r in rows] == ["UP_RED"]


def test_blocked_order_same_direction_next_bar_adds_no_flag(monkeypatch):
    state = _state()
    svc = _svc()
    old = datetime(2026, 7, 24, 8, 59, 30, tzinfo=KST)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, 15_000.0, old, 999.0, "test")
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    for minute in (0, 3):
        bar_dt = datetime(2026, 7, 24, 9, minute, tzinfo=KST)
        _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED))
        worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))

    assert len(ledger.load_signal_ledger()) == 1
    assert broker.orders == []


def test_up_down_up_counts_three_onsets(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    for minute, direction in ((0, Direction.UP_RED), (3, Direction.DOWN_BLUE), (6, Direction.UP_RED)):
        bar_dt = datetime(2026, 7, 24, 9, minute, tzinfo=KST)
        _patch_snap(monkeypatch, _snap(bar_dt, direction))
        worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))

    assert [r["direction"] for r in ledger.load_signal_ledger()] == ["UP_RED", "DOWN_BLUE", "UP_RED"]


def test_quote_age_27_seconds_is_stale_not_ready():
    svc = _svc()
    old = datetime.now(KST) - timedelta(seconds=27)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, 15_000.0, old, 27.0, "test")

    assert svc.quote_statuses()[config.LONG_SYMBOL] == "STALE"
    assert svc.quote_status() != "READY"


def test_target_quote_stale_waiting_and_no_order(monkeypatch):
    state = _state()
    svc = _svc()
    old = datetime(2026, 7, 24, 8, 59, 30, tzinfo=KST)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, 15_000.0, old, 999.0, "test")
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED))

    result = worker.run_once(
        broker=FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0}),
        market_data=svc, state=state, now=bar_dt + timedelta(minutes=3),
    )

    assert result.skipped == worker.QUOTE_STALE
    assert state.pending_signal["signal_id"] == "20260724_090000_UP_RED"
    assert ledger.load_execution_ledger() == []


def test_quote_recovers_within_10_seconds_orders_original_signal_id(monkeypatch):
    state = _state()
    svc = _svc()
    old = datetime(2026, 7, 24, 8, 59, 30, tzinfo=KST)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, 15_000.0, old, 999.0, "test")
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED))
    worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))
    svc.refresh_quotes()

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(seconds=10, minutes=3))

    assert len([o for o in broker.orders if o.side == "BUY"]) == 1
    assert broker.orders[0].symbol == config.LONG_SYMBOL
    assert state.processed_signal_ids == ["20260724_090000_UP_RED"]
    assert result.actions == ["ENTRY:UP_RED"]


def test_runtime_flat_and_broker_flat_is_match_flat():
    state = _state()
    result = worker.reconcile_position_state(FakeBroker(), state, datetime(2026, 7, 24, 9, 0, tzinfo=KST), force=True)
    assert result == worker.MATCH_FLAT
    assert state.position_reconcile_diag["comparison_result"] == worker.MATCH_FLAT


def test_runtime_flat_broker_holding_recovers_runtime():
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    broker.buy_market(config.LONG_SYMBOL, 3, "seed")
    state = _state()

    result = worker.reconcile_position_state(broker, state, datetime(2026, 7, 24, 9, 0, tzinfo=KST), force=True)

    assert result == worker.RECOVERED_FROM_BROKER
    assert state.position.symbol == config.LONG_SYMBOL
    assert state.position.quantity == 3


def test_runtime_holding_broker_flat_recovers_to_flat():
    state = _state()
    state.position = PositionSnapshot(config.LONG_SYMBOL, 3, 15_000.0)

    result = worker.reconcile_position_state(FakeBroker(), state, datetime(2026, 7, 24, 9, 0, tzinfo=KST), force=True)

    assert result == worker.RECOVERED_TO_FLAT
    assert state.position is None


def test_broker_lookup_failure_is_position_data_error():
    class ErrorBroker(FakeBroker):
        def get_positions(self):
            raise TimeoutError("temporary KIS timeout")

    state = _state()
    result = worker.reconcile_position_state(ErrorBroker(), state, datetime(2026, 7, 24, 9, 0, tzinfo=KST), force=True)
    assert result == worker.POSITION_DATA_ERROR
    assert state.position_reconcile_diag["broker_response_error"]


def test_same_signal_order_sent_once(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED))

    worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))
    worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))

    assert len([o for o in broker.orders if o.side == "BUY"]) == 1


def test_signal_id_date_and_time_are_both_from_completed_bar():
    bar_dt = datetime(2026, 1, 6, 15, 27, tzinfo=KST)
    assert make_signal_id(bar_dt, Direction.UP_RED) == "20260106_152700_UP_RED"
