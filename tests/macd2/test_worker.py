"""Unit tests for app.trading.macd2.worker — fake broker + fake market data only."""
from __future__ import annotations

import math
import time as time_module
from datetime import datetime, time as dtime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.worker import Macd2Worker, run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    period = n_minutes
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 10}
        for i, c in enumerate(closes)
    ]
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
    for step in range(steps):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        if result.actions and result.actions[0].startswith("ENTRY:"):
            return state, broker, result, now
    return state, broker, None, None


def _bootstrapped_sine_service(quote_prices):
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
    assert ledger.load_signal_ledger()[0]["signal_type"] == "INITIAL"
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
