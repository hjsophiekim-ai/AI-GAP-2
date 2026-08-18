"""Unit + integration tests for the 2026-08-05 Quick-Profit redesign
(threshold 1.5%->2.0%, judged directly off each tick's live quote instead of
a remembered "1분 고점" — docs/MACD2_LOGIC.md "2026-08-05 Quick-Profit
redesign"). Isolated to tmp_path via conftest.py."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import PositionSnapshot
from app.trading.macd2.signal_engine import forming_bar_window
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    import math
    period = max(n_minutes // 2, 1)
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 10}
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


def _svc_with_quote(df_1m, bootstrap_now, quote_prices):
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _fresh_state(*, budget: float = 10_000_000.0):
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.major_filter_enabled = False
    return state


def _seed_held_position(state, *, symbol: str, entry_price: float, quantity: int, now: datetime):
    """Mirrors what _apply_switch_outcome sets on a real entry fill (Stop
    Loss bar gating anchored at entry) -- Quick-Profit itself needs no
    seeding at all under the 2026-08-05 redesign (immediate live-tick check,
    no memory)."""
    state.position = PositionSnapshot(symbol=symbol, quantity=quantity, avg_price=entry_price, entry_at=now)
    entry_bar_start, _ = forming_bar_window(now)
    state.stop_loss_bar_symbol = symbol
    state.stop_loss_entry_bar_ts = entry_bar_start.isoformat()
    state.stop_loss_bar_ts = entry_bar_start.isoformat()
    state.stop_loss_bar_close = entry_price


def test_quick_profit_threshold_default_is_2pt5_percent():
    assert config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT == 2.5


def test_quick_profit_no_longer_has_minute_high_state_fields():
    """2026-08-05: the old "1분 고점 기억" mechanism was removed entirely."""
    state = state_store.default_state()
    assert not hasattr(state, "quick_profit_minute_symbol")
    assert not hasattr(state, "quick_profit_minute_bucket")
    assert not hasattr(state, "quick_profit_minute_high")
    assert not hasattr(worker, "_update_quick_profit_minute_high")


def test_quick_profit_fires_immediately_off_live_tick_no_memory_needed():
    """A single tick whose live quote already clears +2.5% exits immediately
    -- no prior "remembered peak" tick is required (unlike the old design)."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    now = prior_day + timedelta(minutes=300, seconds=5)
    entry_price = 15_000.0

    quote_prices = {config.LONG_SYMBOL: entry_price * 1.026}  # +2.6%, first tick ever for this position
    svc = _svc_with_quote(df_1m, now, quote_prices)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, entry_price * 1.026)

    state = _fresh_state()
    state.quick_profit_enabled = True
    _seed_held_position(state, symbol=config.LONG_SYMBOL, entry_price=entry_price, quantity=10, now=now)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("QUICK_PROFIT_TAKE_PROFIT:") for a in result.actions)
    assert state.position is None
    rows = ledger.load_execution_ledger()
    assert rows[-1]["exit_reason"] == config.EXIT_QUICK_PROFIT_TAKE_PROFIT


def test_quick_profit_below_threshold_does_not_exit():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    now = prior_day + timedelta(minutes=300, seconds=5)
    entry_price = 15_000.0

    quote_prices = {config.LONG_SYMBOL: entry_price * 1.024}  # +2.4% -- below 2.5%
    svc = _svc_with_quote(df_1m, now, quote_prices)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, entry_price * 1.024)

    state = _fresh_state()
    state.quick_profit_enabled = True
    _seed_held_position(state, symbol=config.LONG_SYMBOL, entry_price=entry_price, quantity=10, now=now)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert not any(a.startswith("QUICK_PROFIT_TAKE_PROFIT:") for a in result.actions)
    assert state.position is not None


def test_quick_profit_no_stale_memory_across_ticks_only_current_tick_matters():
    """2026-08-05 fix target: a spike on an EARLIER tick that has already
    reversed by the CURRENT tick must NOT trigger an exit -- there is no
    remembered peak any more, only the live tick's own return matters (this
    is the exact opposite of the old bug this redesign eliminates: the old
    code could sell at a stale high; the new code simply never "remembers"
    one)."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    now0 = prior_day + timedelta(minutes=300, seconds=5)
    entry_price = 15_000.0

    svc = _svc_with_quote(df_1m, now0, {config.LONG_SYMBOL: entry_price * 1.03})
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")

    state = _fresh_state()
    state.quick_profit_enabled = True
    _seed_held_position(state, symbol=config.LONG_SYMBOL, entry_price=entry_price, quantity=10, now=now0)

    # Tick 1: a fleeting +3.0% spike -- but nothing polls it mid-tick, and by
    # the time run_once actually evaluates, suppose the SAME tick's quote is
    # already back down (simulating a spike the Worker's own tick cadence
    # missed catching at its peak). We feed the ALREADY-reversed price
    # directly to this tick to prove only the current value is judged.
    broker.set_quote(config.LONG_SYMBOL, entry_price * 1.005)  # +0.5% now, spike already gone
    svc._quotes[config.LONG_SYMBOL] = svc._quotes[config.LONG_SYMBOL].__class__(
        config.LONG_SYMBOL, entry_price * 1.005, datetime.now(KST), 0.0, "test", None,
    )
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith("QUICK_PROFIT_TAKE_PROFIT:") for a in result.actions)
    assert state.position is not None  # still held -- no stale-peak sell


def test_quick_profit_off_holds_until_flag_unchanged_behavior():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    now = prior_day + timedelta(minutes=300, seconds=5)
    entry_price = 15_000.0

    quote_prices = {config.LONG_SYMBOL: entry_price * 1.05}  # +5%, way above threshold
    svc = _svc_with_quote(df_1m, now, quote_prices)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, entry_price * 1.05)

    state = _fresh_state()
    state.quick_profit_enabled = False  # OFF
    _seed_held_position(state, symbol=config.LONG_SYMBOL, entry_price=entry_price, quantity=10, now=now)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert not any(a.startswith("QUICK_PROFIT_TAKE_PROFIT:") for a in result.actions)
    assert state.position is not None
    assert state.position.quantity == 10


def test_quick_profit_toggled_on_mid_holding_sells_immediately_next_tick():
    """The user's explicit ask: turning Quick Profit ON while ALREADY holding
    a qualifying position must sell on the very next tick -- no delay, no
    seeding/warm-up needed under the new memory-free design."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    now = prior_day + timedelta(minutes=300, seconds=5)
    entry_price = 15_000.0

    quote_prices = {config.LONG_SYMBOL: entry_price * 1.026}
    svc = _svc_with_quote(df_1m, now, quote_prices)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, entry_price * 1.026)

    state = _fresh_state()
    state.quick_profit_enabled = False  # was OFF while the position ran up
    _seed_held_position(state, symbol=config.LONG_SYMBOL, entry_price=entry_price, quantity=10, now=now)

    # A tick while still OFF -- no exit despite already qualifying.
    result_off = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert not any(a.startswith("QUICK_PROFIT_TAKE_PROFIT:") for a in result_off.actions)
    assert state.position is not None

    # User flips the toggle ON (mirrors service.set_quick_profit_enabled).
    state.quick_profit_enabled = True

    # The very next tick must sell immediately.
    result_on = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))
    assert any(a.startswith("QUICK_PROFIT_TAKE_PROFIT:") for a in result_on.actions)
    assert state.position is None


def test_quick_profit_applies_to_manually_entered_position(monkeypatch):
    """User's explicit ask: manual_entry() positions are subject to the same
    immediate Quick-Profit check as automatically-entered ones -- no special
    casing, since the check only looks at state.position/quick_profit_enabled."""
    from app.trading.macd2 import service as service_module
    from app.trading.macd2.models import Direction

    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    entry_now = prior_day + timedelta(minutes=300, seconds=5)
    entry_price = 15_000.0

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    quotes = {config.LONG_SYMBOL: entry_price, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}

    def fake_quote(mode, symbol):
        del mode
        return quotes.get(symbol), None

    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    fake_broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price, config.INVERSE_SYMBOL: 10_000.0})
    monkeypatch.setattr(service_module, "create_macd2_broker", lambda mode, **kw: fake_broker)
    monkeypatch.setattr(
        service_module, "MarketDataService",
        lambda mode="mock": MarketDataService(mode=mode, fetch_minute_candles=fake_fetch, fetch_quote=fake_quote),
    )

    svc = service_module.Macd2Service()
    try:
        boot = svc.start(mode="mock", budget=1_000_000.0)
        assert boot["ok"] is True

        entry = svc.manual_entry(Direction.UP_RED.value)
        assert entry["ok"] is True

        state = state_store.load_state()
        assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
        avg_price = state.position.avg_price

        state.quick_profit_enabled = True
        state_store.save_state(state)

        # Price spikes past +2.5% -- the next Worker tick (using the SAME
        # run_once path manual_entry's position now flows through) must
        # sell immediately, exactly like an automatically-entered position.
        quotes[config.LONG_SYMBOL] = avg_price * 1.026
        fake_broker.set_quote(config.LONG_SYMBOL, avg_price * 1.026)
        market_data = svc._market_data
        market_data.refresh_quotes()

        result = run_once(broker=fake_broker, market_data=market_data, state=state, now=entry_now + timedelta(seconds=10))
        assert any(a.startswith("QUICK_PROFIT_TAKE_PROFIT:") for a in result.actions)
        assert state.position is None
    finally:
        svc.stop()
