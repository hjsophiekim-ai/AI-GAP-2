from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot, QuoteSnapshot
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_macd_crossover,
    make_signal_id,
    resample_completed_3m,
    signed_b_condition,
)
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _state():
    state = worker.RuntimeState()
    state.auto_trade_on = True
    return state


def _snap(bar_dt: datetime, direction: Direction = Direction.UP_RED) -> MacdSnapshot:
    hist = (1.0, 2.0, 3.0) if direction == Direction.UP_RED else (-1.0, -2.0, -3.0)
    previous_diff = -1.0 if direction == Direction.UP_RED else 1.0
    current_diff = 1.0 if direction == Direction.UP_RED else -1.0
    if direction == Direction.HOLD:
        hist = (1.0, 2.0, 1.5)
        previous_diff = 1.0
        current_diff = 1.5
    return MacdSnapshot(
        bar_dt=bar_dt, macd=current_diff, signal=0.0, hist=hist[-1], hist_last3=hist,
        completed_3m_count=100, previous_diff=previous_diff, current_diff=current_diff,
        relation="ABOVE" if current_diff > 0 else "BELOW",
    )


def _svc(prices=None):
    prices = prices or {config.WATCH_SYMBOL: 100.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a: (pd.DataFrame({"datetime": []}), {}),
        fetch_quote=lambda mode, symbol: (prices.get(symbol), None),
    )
    svc.refresh_quotes()
    return svc


def _1m_from_3m_closes(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        bar_start = start + timedelta(minutes=3 * i)
        for j in range(3):
            rows.append({
                "datetime": bar_start + timedelta(minutes=j),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 10,
            })
    return pd.DataFrame(rows)


def _history_svc(df_1m: pd.DataFrame, prices=None) -> MarketDataService:
    prices = prices or {config.WATCH_SYMBOL: 100.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    empty = pd.DataFrame({"datetime": [], "open": [], "high": [], "low": [], "close": [], "volume": []})
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_minute_candles_for_date=lambda *a: (empty, {}),
        fetch_quote=lambda mode, symbol: (prices.get(symbol), None),
    )
    svc.bootstrap(now=df_1m["datetime"].iloc[-1] + timedelta(minutes=1))
    svc.refresh_quotes()
    return svc


def _assert_latest_primary(df_1m: pd.DataFrame, now: datetime, direction: Direction) -> None:
    snap = calculate_macd(resample_completed_3m(df_1m, now=now))
    assert snap is not None
    assert evaluate_macd_crossover(snap, None) == direction


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


def test_crossover_opposite_signal_sells_then_buys(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    first_bar = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(first_bar, Direction.UP_RED))
    worker.run_once(broker=broker, market_data=svc, state=state, now=first_bar + timedelta(minutes=3))

    second_bar = datetime(2026, 7, 24, 9, 3, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(second_bar, Direction.DOWN_BLUE))
    worker.run_once(broker=broker, market_data=svc, state=state, now=second_bar + timedelta(minutes=3))

    assert [(o.side, o.symbol) for o in broker.orders] == [
        ("BUY", config.LONG_SYMBOL),
        ("SELL", config.LONG_SYMBOL),
        ("BUY", config.INVERSE_SYMBOL),
    ]
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert broker.get_position(config.INVERSE_SYMBOL).quantity > 0


def test_down_blue_crossover_flat_buys_inverse_once(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.DOWN_BLUE))

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert broker.orders[0].side == "BUY"
    assert broker.orders[0].symbol == config.INVERSE_SYMBOL
    assert result.signal_dispatch_trace["order_executor_called"] is True
    assert result.signal_dispatch_trace["position_reconcile_result"] == worker.MATCH_FLAT
    assert result.signal_dispatch_trace["quote_status"] == "READY"
    assert result.signal_dispatch_trace["target_quote_valid"] is True


def test_ten_same_direction_crossover_bars_create_one_flag_and_one_order(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    for i in range(10):
        bar_dt = datetime(2026, 7, 24, 9, 3 * i, tzinfo=KST)
        _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED))
        worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))

    assert [r["direction"] for r in ledger.load_signal_ledger()] == ["UP_RED"]
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]


def test_down_blue_ready_dispatches_executor_once(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.DOWN_BLUE))
    calls = {"n": 0}
    original = worker.order_executor.execute_signal

    def wrapped_execute_signal(**kwargs):
        calls["n"] += 1
        return original(**kwargs)

    monkeypatch.setattr(worker.order_executor, "execute_signal", wrapped_execute_signal)

    worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))

    assert calls["n"] == 1
    assert broker.orders[0].symbol == config.INVERSE_SYMBOL


def test_executor_none_is_recorded_as_signal_not_dispatched(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.DOWN_BLUE))
    monkeypatch.setattr(worker.order_executor, "execute_signal", lambda **kwargs: None)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=bar_dt + timedelta(minutes=3))

    assert result.skipped == worker.SIGNAL_NOT_DISPATCHED
    assert state.order_block_reason == worker.SIGNAL_NOT_DISPATCHED
    assert result.signal_dispatch_trace["order_executor_called"] is True
    assert result.signal_dispatch_trace["final_block_reason"] == worker.SIGNAL_NOT_DISPATCHED
    assert ledger.load_signal_ledger()[0]["block_reason"] == worker.SIGNAL_NOT_DISPATCHED


def test_production_path_up_crossover_buys_long_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 35 + [120.0])
    now = start + timedelta(minutes=3 * 36)
    _assert_latest_primary(df_1m, now, Direction.UP_RED)
    state = _state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    result = worker.run_once(broker=broker, market_data=_history_svc(df_1m), state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]


def test_production_path_down_crossover_buys_inverse_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 35 + [80.0])
    now = start + timedelta(minutes=3 * 36)
    _assert_latest_primary(df_1m, now, Direction.DOWN_BLUE)
    state = _state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})

    result = worker.run_once(broker=broker, market_data=_history_svc(df_1m), state=state, now=now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.INVERSE_SYMBOL)]
    assert result.signal_dispatch_trace["order_executor_called"] is True
    assert result.signal_dispatch_trace["position_reconcile_result"] == worker.MATCH_FLAT
    assert result.signal_dispatch_trace["quote_status"] == "READY"
    assert result.signal_dispatch_trace["target_quote_valid"] is True


def test_production_path_catches_intermediate_down_crossover_when_cache_jumps_ahead():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    # Index 79 is 12:57. Later 13:00/13:03 bars are already in cache, so the
    # latest bar is no longer the crossover bar.
    df_1m = _1m_from_3m_closes(start, [100.0] * 79 + [80.0, 70.0, 60.0])
    now = start + timedelta(minutes=3 * 82)
    crossover_snap = calculate_macd(resample_completed_3m(df_1m.iloc[: 80 * 3], now=start + timedelta(minutes=3 * 80)))
    latest_snap = calculate_macd(resample_completed_3m(df_1m, now=now))
    assert crossover_snap is not None
    assert latest_snap is not None
    assert crossover_snap.bar_dt == datetime(2026, 7, 24, 12, 57, tzinfo=KST)
    assert evaluate_macd_crossover(crossover_snap, None) == Direction.DOWN_BLUE
    assert evaluate_macd_crossover(latest_snap, None) == Direction.HOLD
    state = _state()
    state.last_evaluated_bar_ts = datetime(2026, 7, 24, 12, 54, tzinfo=KST).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})

    result = worker.run_once(broker=broker, market_data=_history_svc(df_1m), state=state, now=now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert state.latest_primary_signal_id == "20260724_125700_DOWN_BLUE"
    assert state.last_evaluated_bar_ts == datetime(2026, 7, 24, 12, 57, tzinfo=KST).isoformat()
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.INVERSE_SYMBOL)]


def test_production_path_signed_b_only_without_crossover_orders_zero():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 35 + [110.0, 120.0, 130.0])
    now = start + timedelta(minutes=3 * 38)
    snap = calculate_macd(resample_completed_3m(df_1m, now=now))
    assert snap is not None
    assert signed_b_condition(snap) == Direction.UP_RED
    assert evaluate_macd_crossover(snap, None) == Direction.HOLD
    state = _state()
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.last_evaluated_bar_ts = snap.bar_dt.isoformat()
    state.last_detected_direction = Direction.UP_RED
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    result = worker.run_once(broker=broker, market_data=_history_svc(df_1m), state=state, now=now)

    assert result.actions == []
    assert broker.orders == []


def test_production_path_same_crossover_bar_twenty_ticks_orders_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 35 + [120.0])
    now = start + timedelta(minutes=3 * 36)
    state = _state()
    svc = _history_svc(df_1m)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    worker.run_once(broker=broker, market_data=svc, state=state, now=now)
    for _ in range(20):
        worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]


def test_production_path_up_then_down_sells_to_zero_then_buys_inverse():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    up_df = _1m_from_3m_closes(start, [100.0] * 35 + [120.0])
    down_df = _1m_from_3m_closes(start, [100.0] * 36 + [80.0])
    up_now = start + timedelta(minutes=3 * 36)
    down_now = start + timedelta(minutes=3 * 37)
    _assert_latest_primary(up_df, up_now, Direction.UP_RED)
    _assert_latest_primary(down_df, down_now, Direction.DOWN_BLUE)
    state = _state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    worker.run_once(broker=broker, market_data=_history_svc(up_df), state=state, now=up_now)
    result = worker.run_once(broker=broker, market_data=_history_svc(down_df), state=state, now=down_now)

    assert result.actions == ["OPPOSITE_SIGNAL:DOWN_BLUE"]
    assert [(o.side, o.symbol) for o in broker.orders] == [
        ("BUY", config.LONG_SYMBOL),
        ("SELL", config.LONG_SYMBOL),
        ("BUY", config.INVERSE_SYMBOL),
    ]
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert broker.get_position(config.INVERSE_SYMBOL).quantity > 0


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


def test_signed_b_shadow_without_crossover_does_not_order(monkeypatch):
    state = _state()
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    snap = MacdSnapshot(
        bar_dt=bar_dt, macd=2.0, signal=0.0, hist=3.0, hist_last3=(1.0, 2.0, 3.0),
        completed_3m_count=100, previous_diff=1.0, current_diff=2.0, relation="ABOVE",
    )
    _patch_snap(monkeypatch, snap)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    worker.run_once(broker=broker, market_data=_svc(), state=state, now=bar_dt + timedelta(minutes=3))

    assert state.signed_b_shadow_direction == Direction.UP_RED
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []


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


def test_worker_start_baseline_blocks_past_crossover(monkeypatch):
    state = _state()
    svc = _svc()
    baseline_bar = datetime(2026, 7, 24, 10, 51, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(baseline_bar, Direction.UP_RED))

    worker.initialize_strategy_session(
        state, svc, now=datetime(2026, 7, 24, 10, 53, tzinfo=KST), worker_instance_id="worker-test",
    )
    result = worker.run_once(
        broker=FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0}),
        market_data=svc, state=state, now=datetime(2026, 7, 24, 10, 53, 5, tzinfo=KST),
    )

    assert result.actions == []
    assert ledger.load_signal_ledger() == []


def test_worker_after_start_new_crossover_orders_once(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    baseline_bar = datetime(2026, 7, 24, 10, 51, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(baseline_bar, Direction.HOLD))
    worker.initialize_strategy_session(state, svc, now=datetime(2026, 7, 24, 10, 53, tzinfo=KST))

    new_bar = datetime(2026, 7, 24, 10, 54, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(new_bar, Direction.UP_RED))
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=new_bar + timedelta(minutes=3))

    assert result.actions == ["ENTRY:UP_RED"]
    assert len([o for o in broker.orders if o.side == "BUY"]) == 1


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
