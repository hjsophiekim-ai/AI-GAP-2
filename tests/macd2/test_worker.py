"""Unit tests for app.trading.macd2.worker — fake broker + fake market data only."""
from __future__ import annotations

import math
import time as time_module
from datetime import datetime, time as dtime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot, QuoteSnapshot, RuntimeState
from app.trading.macd2.worker import Macd2Worker, run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    period = max(n_minutes // 2, 1)
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 10}
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


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


_PRIOR_DAY = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
# Bootstrap's own has-prior-day check needs its `now` to be on a LATER
# calendar date than every bar in the synthetic session (all 300 minutes
# are dated _PRIOR_DAY) — this is a wholly separate concern from the `now`
# used later to walk through "today's" session bar by bar, which must start
# EARLY (right after the 26-bar EMA warm-up) for resample_completed_3m's
# own now-based completion cutoff to reveal bars progressively.
_BOOTSTRAP_NOW = _PRIOR_DAY + timedelta(days=2)
_SESSION_START_NOW = _PRIOR_DAY + timedelta(minutes=3 * (config.SIGNAL_MIN_BAR_INDEX + 1))


@pytest.fixture
def ready_market_data():
    """A MarketDataService already bootstrapped with a sine-wave session that
    is guaranteed to pass through both a UP_RED-style run and a DOWN_BLUE-style
    reversal (mirrors tests/macd2/test_parity.py's synthetic session). Quotes
    are wired to a fake fetcher too — never the real (blocked) KIS default.
    """
    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    result = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert result.ok, f"fixture bootstrap failed unexpectedly: {result.reason}"
    return svc, _SESSION_START_NOW


def _fresh_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    return state


def test_run_once_skipped_when_auto_trade_off(ready_market_data):
    svc, now = ready_market_data
    state = _fresh_state()
    state.auto_trade_on = False
    broker = FakeBroker(cash=10_000_000.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.skipped == "auto_trade_off"
    assert broker.orders == []


def test_run_once_not_ready_before_warmup():
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (pd.DataFrame(), {}))
    state = _fresh_state()
    broker = FakeBroker(cash=10_000_000.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=datetime(2026, 1, 6, 9, 5, tzinfo=KST))
    assert result.skipped == "NOT_READY"
    assert state.warmup_ready is False


def _find_first_entry_tick(svc, now0, budget=10_000_000.0, *, steps=80):
    """Advance in 3-minute steps (mirroring completed-3m-bar cadence) until a
    flat-entry signal actually fires. Relies on resample_completed_3m's own
    now-based completion cutoff to reveal progressively more of the already-
    loaded synthetic session — no incremental re-fetch simulation needed.
    """
    state = _fresh_state(budget=budget)
    broker = FakeBroker(cash=budget, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    svc.refresh_quotes()
    for step in range(steps):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        if result.actions and result.actions[0].startswith("ENTRY:"):
            return state, broker, result, now
    return state, broker, None, None


def _bootstrapped_sine_service(quote_prices):
    quote_prices = {config.WATCH_SYMBOL: 100.0, **quote_prices}
    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    result = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert result.ok, f"bootstrap failed unexpectedly: {result.reason}"
    return svc


def test_flat_entry_buys_correct_symbol_and_updates_state():
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    svc = _bootstrapped_sine_service(quote_prices)
    svc.refresh_quotes()

    state, broker, result, now = _find_first_entry_tick(svc, _SESSION_START_NOW)

    assert result is not None, "synthetic sine session never produced an entry signal"
    assert state.position is not None
    assert state.position.symbol in (config.LONG_SYMBOL, config.INVERSE_SYMBOL)
    assert state.position.quantity > 0
    assert state.last_signal_direction in (Direction.UP_RED, Direction.DOWN_BLUE)
    assert len(state.processed_signal_ids) == 1
    assert ledger.load_signal_ledger()[0]["signal_type"] in {"INITIAL", "INITIAL_PROVISIONAL"}
    assert ledger.load_execution_ledger()[0]["side"] == "BUY"


def test_same_bar_is_not_evaluated_twice(ready_market_data):
    svc, now0 = ready_market_data
    state, broker, result, now = _find_first_entry_tick(svc, now0)
    assert result is not None
    first_processed = list(state.processed_signal_ids)

    # Re-run at the exact same `now` (same completed bar) — must not re-evaluate.
    result2 = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert state.processed_signal_ids == first_processed
    assert not result2.actions


def test_duplicate_signal_id_is_never_reexecuted_across_many_ticks():
    """20 repeated ticks against the same completed bar -> 0 additional orders."""
    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    svc = _bootstrapped_sine_service(quotes)

    state, broker, result, now = _find_first_entry_tick(svc, _SESSION_START_NOW)
    assert result is not None
    orders_after_first = len(broker.orders)

    for _ in range(20):
        run_once(broker=broker, market_data=svc, state=state, now=now)

    assert len(broker.orders) == orders_after_first  # zero additional orders


def test_provisional_up_crossover_buys_long_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL
    assert broker.orders[0].symbol == config.LONG_SYMBOL
    assert broker.orders[0].side == "BUY"
    assert state.provisional_signal_id == "20260724_140000_UP_RED_PROVISIONAL"
    assert result.signal_dispatch_trace["order_executor_called"] is True
    assert result.order_requested_at


def test_provisional_forming_window_at_1447_uses_current_bar():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start, bars=115)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = datetime(2026, 7, 24, 14, 47, 0, tzinfo=KST)
    state = _fresh_state()
    state.provisional_bar_start = "2026-07-24T14:15:00+09:00"
    state.provisional_bar_end = "2026-07-24T14:18:00+09:00"
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.provisional_bar_start == "2026-07-24T14:45:00+09:00"
    assert state.provisional_bar_end == "2026-07-24T14:48:00+09:00"
    assert state.provisional_input_now == "2026-07-24T14:47:00+09:00"
    assert state.provisional_last_1m_at == "2026-07-24T14:44:00+09:00"
    assert state.provisional_last_1m_close == 100.0


def test_provisional_recomputes_same_forming_bar_from_latest_quote_every_tick():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=130.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    run_once(broker=broker, market_data=svc, state=state, now=now)
    first_diff = state.provisional_diff
    svc._quotes[config.WATCH_SYMBOL] = QuoteSnapshot(
        config.WATCH_SYMBOL, 160.0, datetime.now(KST), 0.0, "test", None,
    )
    run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))

    assert state.provisional_bar_start == "2026-07-24T14:00:00+09:00"
    assert state.provisional_quote_price == 160.0
    assert state.provisional_diff != first_diff


def test_provisional_down_crossover_buys_inverse_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=60.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert state.position is not None
    assert state.position.symbol == config.INVERSE_SYMBOL
    assert broker.orders[0].symbol == config.INVERSE_SYMBOL
    assert broker.orders[0].side == "BUY"
    assert state.provisional_signal_id == "20260724_140000_DOWN_BLUE_PROVISIONAL"
    assert result.signal_dispatch_trace["order_executor_called"] is True


def test_provisional_same_forming_bar_twenty_ticks_orders_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    run_once(broker=broker, market_data=svc, state=state, now=now)
    orders_after_first = len(broker.orders)
    for _ in range(20):
        run_once(broker=broker, market_data=svc, state=state, now=now)

    assert len(broker.orders) == orders_after_first
    assert state.processed_signal_ids.count("20260724_140000_UP_RED_PROVISIONAL") == 1


def test_provisional_same_bar_blocks_opposite_switch():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    run_once(broker=broker, market_data=svc, state=state, now=now)
    svc._quotes[config.WATCH_SYMBOL] = QuoteSnapshot(
        config.WATCH_SYMBOL, 60.0, datetime.now(KST), 0.0, "test", None,
    )
    result_down = run_once(broker=broker, market_data=svc, state=state, now=now)
    svc._quotes[config.WATCH_SYMBOL] = QuoteSnapshot(
        config.WATCH_SYMBOL, 140.0, datetime.now(KST), 0.0, "test", None,
    )
    run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result_down.actions == []
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]
    assert state.order_block_reason == worker.order_executor.BLOCK_DUPLICATE_SIGNAL


def test_provisional_stale_target_quote_blocks_order():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(
        config.LONG_SYMBOL, 15_000.0, datetime.now(KST) - timedelta(seconds=27), 27.0, "test", None,
    )
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=_forming_now(start))

    assert broker.orders == []
    assert state.pending_signal is not None
    assert state.pending_signal["signal_id"] == "20260724_140000_UP_RED_PROVISIONAL"
    assert result.skipped == worker.QUOTE_STALE


def test_provisional_unrelated_etf_stale_does_not_block_order():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    svc._quotes[config.INVERSE_SYMBOL] = QuoteSnapshot(
        config.INVERSE_SYMBOL, 10_000.0, datetime.now(KST) - timedelta(seconds=27), 27.0, "test", None,
    )
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=_forming_now(start))

    assert result.actions == ["ENTRY:UP_RED"]
    assert broker.orders[0].symbol == config.LONG_SYMBOL
    assert result.signal_dispatch_trace["required_quote_symbols"] == [config.WATCH_SYMBOL, config.LONG_SYMBOL]


def test_provisional_same_target_holding_records_flag_and_block_reason():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)
    orders_before = len(broker.orders)

    result = run_once(broker=broker, market_data=svc, state=state, now=_forming_now(start))

    rows = ledger.load_signal_ledger()
    assert len(broker.orders) == orders_before
    assert result.actions == []
    assert state.provisional_signal_id == "20260724_140000_UP_RED_PROVISIONAL"
    assert rows[-1]["signal_id"] == "20260724_140000_UP_RED_PROVISIONAL"
    assert rows[-1]["direction"] == Direction.UP_RED.value
    assert rows[-1]["block_reason"] == worker.order_executor.BLOCK_ALREADY_HOLDING
    assert rows[-1]["executor_called"] == "False"


def test_provisional_quote_recovers_orders_same_signal_id_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(
        config.LONG_SYMBOL, 15_000.0, datetime.now(KST) - timedelta(seconds=27), 27.0, "test", None,
    )
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )
    now = _forming_now(start)

    run_once(broker=broker, market_data=svc, state=state, now=now)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(
        config.LONG_SYMBOL, 15_000.0, datetime.now(KST), 0.0, "test", None,
    )
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))

    assert result.actions == ["ENTRY:UP_RED"]
    assert len([o for o in broker.orders if o.side == "BUY"]) == 1
    assert state.processed_signal_ids == ["20260724_140000_UP_RED_PROVISIONAL"]


def test_provisional_executor_called_within_5_seconds_of_detection():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=_forming_now(start))

    detected = datetime.fromisoformat(result.signal_detected_at)
    executor_called = datetime.fromisoformat(result.signal_dispatch_trace["executor_called_at"])
    assert (executor_called - detected).total_seconds() <= config.SIGNAL_TO_ORDER_REQUEST_MAX_SEC


def test_confirmed_after_provisional_same_bar_does_not_reorder(monkeypatch):
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )
    run_once(broker=broker, market_data=svc, state=state, now=now)
    state.position = None
    broker._positions.clear()
    orders_after_provisional = len(broker.orders)

    confirmed_bar = start + timedelta(minutes=300)
    monkeypatch.setattr(worker, "calculate_macd", lambda _bars: MacdSnapshot(
        bar_dt=confirmed_bar, macd=1.0, signal=0.0, hist=1.0,
        hist_last3=(0.0, -1.0, 1.0), completed_3m_count=101,
        previous_diff=-1.0, current_diff=1.0, relation="ABOVE",
    ))
    run_once(broker=broker, market_data=svc, state=state, now=confirmed_bar + timedelta(minutes=3))

    assert len(broker.orders) == orders_after_provisional


def test_entry_cutoff_blocks_new_entry_after_1455(ready_market_data):
    svc, now0 = ready_market_data
    state = _fresh_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    late_now = now0.replace(hour=14, minute=56)

    result = run_once(broker=broker, market_data=svc, state=state, now=late_now)
    assert state.position is None
    assert not any(a.startswith("ENTRY:") for a in result.actions)


def _svc_with_quote(df_1m, bootstrap_now, quote_prices):
    """MarketDataService whose get_quote() decision-time cache is actually
    populated (get_quote() only reads the cache — refresh_quotes() must run
    at least once, wired to a fake, never the real/blocked KIS default)."""
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _flat_completed_history(start: datetime, bars: int = 100) -> pd.DataFrame:
    return _1m_from_3m_closes(start, [100.0] * bars)


def _provisional_service(df_1m: pd.DataFrame, watch_price: float = 140.0) -> MarketDataService:
    return _svc_with_quote(
        df_1m,
        df_1m["datetime"].iloc[-1] + timedelta(minutes=1),
        {config.WATCH_SYMBOL: watch_price, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )


def _forming_now(start: datetime, bars: int = 100, seconds: int = 5) -> datetime:
    return start + timedelta(minutes=3 * bars, seconds=seconds)


def test_entry_blocked_before_0900_open(ready_market_data):
    svc, now0 = ready_market_data
    state = _fresh_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    early_now = now0.replace(hour=8, minute=59)

    result = run_once(broker=broker, market_data=svc, state=state, now=early_now)
    assert state.position is None
    assert not any(a.startswith("ENTRY:") for a in result.actions)
    assert broker.orders == []


def test_switch_sell_success_buy_failure_leaves_state_flat():
    """docs: 스위칭 부분실패 — SELL clears to 0, BUY then fails; state.position
    must become None immediately (never keep pointing at the already-sold
    symbol), and no duplicate SELL fires on a later tick."""
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    svc = _bootstrapped_sine_service(quote_prices)
    svc.refresh_quotes()

    state, broker, entry_result, entry_now = _find_first_entry_tick(svc, _SESSION_START_NOW)
    assert entry_result is not None
    assert state.position is not None
    held_symbol = state.position.symbol

    broker.fail_next_buy = True
    switch_now = None
    for step in range(1, 60):
        candidate = entry_now + timedelta(minutes=3 * step)
        result = run_once(broker=broker, market_data=svc, state=state, now=candidate)
        if any(a.startswith("OPPOSITE_SIGNAL:") for a in result.actions):
            switch_now = candidate
            break

    assert switch_now is not None, "synthetic session never produced a reversal to exercise"
    assert state.position is None  # flat, not stuck pointing at the sold symbol
    assert broker.get_position(held_symbol) is None

    orders_before = len(broker.orders)
    run_once(broker=broker, market_data=svc, state=state, now=switch_now)  # same bar, re-ticked
    assert len(broker.orders) == orders_before  # no duplicate SELL


def test_position_mismatch_blocks_all_orders(ready_market_data):
    svc, now0 = ready_market_data
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    # Broker's real account disagrees with state.position (state thinks 10 held, broker has 0).

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert result.skipped == worker.RECOVERED_TO_FLAT
    assert state.order_block_reason == worker.RECOVERED_TO_FLAT
    assert broker.orders == []
    assert state.position is None


def test_day_rollover_resets_session_fields_but_allows_same_direction_signal():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}))
    svc.bootstrap(now=prior_day + timedelta(minutes=300, seconds=5))

    state = _fresh_state()
    state.session_date = "20260105"
    state.last_signal_direction = Direction.UP_RED
    state.last_evaluated_bar_ts = "stale-bar-ts-from-yesterday"
    state.processed_signal_ids = ["20260105_090300_UP_RED"]
    state.peak_net_return = 3.3
    state.profit_lock_active = True
    broker = FakeBroker(cash=10_000_000.0)

    worker._apply_day_rollover(state, prior_day + timedelta(days=1))

    assert state.session_date == (prior_day + timedelta(days=1)).strftime("%Y%m%d")
    assert state.last_signal_direction is None
    assert state.last_evaluated_bar_ts is None
    assert state.processed_signal_ids == []
    assert state.peak_net_return == 0.0
    assert state.profit_lock_active is False

    # The permanent signal ledger is untouched by rollover (a separate CSV,
    # never cleared) — only the in-state runtime dedup list is reset.
    ledger.append_signal({
        "trading_date": "20260105", "completed_bar_at": "090300", "signal_id": "20260105_090300_UP_RED",
        "signal_type": "INITIAL", "direction": "UP_RED", "macd": 1.0, "signal": 0.5,
        "hist_last3": "[]", "detected_at": "2026-01-05T09:03:00+09:00",
        "order_requested_at": "", "order_result": "EXECUTED", "block_reason": "",
    })
    assert len(ledger.load_signal_ledger()) == 1  # still there after rollover


def test_worker_tick_never_calls_market_data_network_fetchers():
    """docs: Worker tick에서 KIS network 호출 제거 — run_once() must read the
    already-cached history via get_history_df() only, never trigger a new
    fetch_minute_candles call itself (that is now the history-updater
    thread's job)."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    fetch_calls = {"n": 0}

    def counting_fetch(mode, symbol, count, hour1):
        fetch_calls["n"] += 1
        return _1m_frame(prior_day, _sine_1m_closes(300)), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=counting_fetch)
    svc.bootstrap(now=prior_day + timedelta(minutes=300, seconds=5))
    calls_after_bootstrap = fetch_calls["n"]
    assert calls_after_bootstrap >= 1

    state = _fresh_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    for step in range(10):
        run_once(broker=broker, market_data=svc, state=state, now=prior_day + timedelta(minutes=300 + step, seconds=5))

    assert fetch_calls["n"] == calls_after_bootstrap  # zero additional network fetches from ticking


def test_stop_loss_exits_full_position():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    now = prior_day + timedelta(minutes=300, seconds=5)

    # Drop price well past -1.5% net; the same price feeds both the decision
    # (market_data cache) and the execution fill (FakeBroker).
    quote_prices = {config.LONG_SYMBOL: 14_000.0}
    svc = _svc_with_quote(df_1m, now, quote_prices)

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, 14_000.0)
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("STOP_LOSS:") for a in result.actions)
    assert state.position is None
    assert broker.get_position(config.LONG_SYMBOL) is None
    rows = ledger.load_execution_ledger()
    assert rows[-1]["exit_reason"] == config.EXIT_STOP_LOSS


def test_profit_lock_exits_on_giveback():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    now = prior_day + timedelta(minutes=300, seconds=5)

    # current net return ~3.4% (peak 4.2 - giveback 0.8 == boundary -> exit)
    quote_prices = {config.LONG_SYMBOL: 15_000.0 * 1.034}
    svc = _svc_with_quote(df_1m, now, quote_prices)

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, 15_000.0 * 1.034)
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)
    state.peak_net_return = 4.2
    state.profit_lock_active = True

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("PROFIT_LOCK:") for a in result.actions)
    assert state.position is None


def test_forced_liquidation_at_1500_overrides_everything():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    bootstrap_now = prior_day + timedelta(minutes=300, seconds=5)
    now = prior_day.replace(hour=15, minute=0, second=1)

    quote_prices = {config.LONG_SYMBOL: 20_000.0}  # deep in profit, no SL/PL trigger
    svc = _svc_with_quote(df_1m, bootstrap_now, quote_prices)

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 20_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("FORCED_LIQUIDATION:") for a in result.actions)
    assert state.position is None
    rows = ledger.load_execution_ledger()
    assert rows[-1]["exit_reason"] == config.EXIT_FORCED_LIQUIDATION


def test_worker_lifecycle_single_thread_and_stats():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}))
    svc.bootstrap(now=prior_day + timedelta(minutes=300, seconds=5))

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    holder = {"state": _fresh_state()}
    w = Macd2Worker(
        broker=broker, market_data=svc,
        get_state=lambda: holder["state"], save_state=lambda s: holder.__setitem__("state", s),
    )

    assert w.is_alive() is False
    w.start()
    try:
        first_thread = w._thread
        w.start()  # calling start() again must NOT spawn a second thread
        assert w._thread is first_thread
        assert w.is_alive() is True
        time_module.sleep(0.3)
        stats = w.tick_stats()
        assert stats["tick_n"] >= 1
        assert stats["stalled"] is False
    finally:
        w.stop(join_timeout=5.0)
    assert w.is_alive() is False

    # stop() must not leave a reusable thread object — start() always creates a fresh one.
    assert w._thread is None
