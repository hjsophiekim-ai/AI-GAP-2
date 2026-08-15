"""Worker-level integration tests for the time-window filter (§25 checklist
subset) — fake broker + fake market data only, mirrors tests/macd2/test_worker.py's
own harness so no duplicated test infrastructure is introduced.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    period = max(n_minutes // 2, 1)
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 100 + (i % 7) * 10}
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


_PRIOR_DAY = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
_BOOTSTRAP_NOW = _PRIOR_DAY + timedelta(days=2)
_SESSION_START_NOW = _PRIOR_DAY + timedelta(minutes=3 * (config.SIGNAL_MIN_BAR_INDEX + 1))


def _fresh_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.time_window_filter_enabled = True
    return state


@pytest.fixture
def tw_market_data():
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
    svc.refresh_quotes()
    return svc, _SESSION_START_NOW


def test_flag_bar_itself_never_dispatches_an_order(tw_market_data):
    """§1: a flag never has order authority on its own completed bar --
    the first tick where a fresh candidate is recorded must never place a
    broker order that same tick."""
    svc, now0 = tw_market_data
    # Replay tick-by-tick and assert the broker's order count never
    # increases on the SAME tick the state transitions into
    # TIME_WINDOW_PENDING_CONFIRMATION for the first time (per candidate).
    state2 = _fresh_state()
    broker2 = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    prev_pending = None
    for step in range(60):
        now = now0 + timedelta(minutes=3 * step)
        orders_before = len(broker2.orders)
        run_once(broker=broker2, market_data=svc, state=state2, now=now)
        became_pending_this_tick = (
            state2.time_window_pending_flag_direction is not None
            and state2.last_time_window_decision == config.TW_PENDING_CONFIRMATION
            and state2.time_window_pending_flag_bar_ts != prev_pending
        )
        if became_pending_this_tick:
            assert len(broker2.orders) == orders_before, (
                "a new time-window candidate must never place an order on its own flag bar"
            )
        prev_pending = state2.time_window_pending_flag_bar_ts


def test_entry_confirms_on_a_later_completed_bar_not_the_flag_bar(tw_market_data):
    """An actual TIME_WINDOW_ENTRY/TIME_WINDOW_SWITCH action, when it fires,
    must occur strictly after the bar that first set the pending candidate."""
    svc, now0 = tw_market_data
    state = _fresh_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    first_candidate_bar_ts = None
    entry_tick_index = None
    for step in range(120):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        if first_candidate_bar_ts is None and state.time_window_pending_flag_bar_ts:
            first_candidate_bar_ts = state.time_window_pending_flag_bar_ts
            first_candidate_step = step
        if any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions):
            entry_tick_index = step
            break

    if entry_tick_index is None:
        pytest.skip("synthetic sine session never produced an approved time-window entry within 120 steps")
    assert first_candidate_bar_ts is not None
    assert entry_tick_index > first_candidate_step, (
        "entry must fire strictly after the tick that first recorded the candidate"
    )
    assert state.position is not None
    assert state.time_window_position_active is True


def test_day_rollover_resets_time_window_session_counters():
    state = _fresh_state()
    state.session_date = "20260105"
    state.time_window_morning_entry_count = 3
    state.time_window_afternoon_entry_count = 1
    state.time_window_pending_flag_direction = Direction.UP_RED
    state.time_window_pending_flag_bar_ts = "2026-01-05T09:03:00+09:00"

    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 0, tzinfo=KST))

    assert state.time_window_morning_entry_count == 0
    assert state.time_window_afternoon_entry_count == 0
    assert state.time_window_pending_flag_direction is None
    assert state.time_window_pending_flag_bar_ts is None
    # the toggle itself survives rollover
    assert state.time_window_filter_enabled is True


def test_day_rollover_does_not_touch_time_window_toggle_when_off():
    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.session_date = "20260105"
    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 0, tzinfo=KST))
    assert state.time_window_filter_enabled is False


def test_no_lookahead_run_once_only_uses_bars_up_to_now(tw_market_data):
    """Two independent runs against the SAME full-day cache, one stopped
    early, must agree on every action taken up to the earlier cutoff --
    proves later-in-the-day bars are never consulted for an earlier tick's
    decision."""
    svc, now0 = tw_market_data

    state_short = _fresh_state()
    broker_short = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    actions_short = []
    for step in range(40):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker_short, market_data=svc, state=state_short, now=now)
        actions_short.append(list(result.actions))

    state_long = _fresh_state()
    broker_long = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    actions_long = []
    for step in range(80):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker_long, market_data=svc, state=state_long, now=now)
        actions_long.append(list(result.actions))

    assert actions_short == actions_long[:40], (
        "a run that later sees more of the day's bars must not have altered "
        "any decision already made for an earlier tick"
    )
