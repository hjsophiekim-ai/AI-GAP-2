"""Optional Daily Single-Entry entry filter tests (2026-08-10 redesign —
sequence-count cap, not the old 11:00-cutoff/one-shot design).

Covers:

A. Pure `evaluate_single_entry` unit tests — invalid-direction rejection,
   approval while under the daily cap, rejection once ``daily_entry_count``
   reaches the cap, and a custom ``max_daily_entries`` override.
B. Worker integration — toggle OFF leaves legacy behavior completely
   unchanged (gate never invoked); toggle ON gates a NEW BUY only: a
   confirmed flag is approved while under config.SINGLE_ENTRY_MAX_DAILY_
   ENTRIES, blocked once the cap is reached, and a blocked REVERSAL still
   liquidates the held position (sell-only/no-re-entry) exactly like the
   other three optional filters.

Reuses the exact `_1m_from_3m_closes` / `_svc_with_quote` / confirmed-flag
big-jump-on-last-2-bars shape tests/macd2/test_trend_persistence_filter.py
already uses — no new MACD/crossover test infrastructure invented here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.macd2 import config, state_store
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.single_entry_filter import evaluate_single_entry
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


# ══════════════════════════════════════════════════════════════════════════
# A. Pure evaluate_single_entry unit tests
# ══════════════════════════════════════════════════════════════════════════
def test_invalid_direction_is_rejected():
    decision = evaluate_single_entry(Direction.HOLD, 0)
    assert decision.approved is False
    assert decision.decision == config.FILTER_INPUT_NOT_CROSSOVER


def test_approved_while_under_daily_cap():
    decision = evaluate_single_entry(Direction.UP_RED, config.SINGLE_ENTRY_MAX_DAILY_ENTRIES - 1)
    assert decision.approved is True
    assert decision.decision == config.SINGLE_ENTRY_APPROVED


def test_rejected_once_daily_cap_reached():
    decision = evaluate_single_entry(Direction.DOWN_BLUE, config.SINGLE_ENTRY_MAX_DAILY_ENTRIES)
    assert decision.approved is False
    assert decision.decision == config.SINGLE_ENTRY_DAILY_LIMIT_REACHED


def test_custom_max_daily_entries_override():
    blocked = evaluate_single_entry(Direction.UP_RED, 2, max_daily_entries=2)
    approved = evaluate_single_entry(Direction.UP_RED, 2, max_daily_entries=3)
    assert blocked.approved is False
    assert approved.approved is True


# ══════════════════════════════════════════════════════════════════════════
# B. Worker integration — order gate only, gates a NEW BUY only
# ══════════════════════════════════════════════════════════════════════════
_WORKER_QUOTES = {
    config.WATCH_SYMBOL: 140.0,
    config.LONG_SYMBOL: 15_000.0,
    config.INVERSE_SYMBOL: 10_000.0,
}


def _at(hour: int, minute: int = 0, second: int = 0, *, day: int = 6) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=KST)


def _1m_from_3m_closes(start: datetime, closes: list) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        bar_start = start + timedelta(minutes=3 * i)
        for j in range(3):
            rows.append({
                "datetime": bar_start + timedelta(minutes=j),
                "open": close, "high": close, "low": close, "close": close, "volume": 10,
            })
    return pd.DataFrame(rows)


def _svc_with_quote(df_1m: pd.DataFrame, bootstrap_now: datetime, quote_prices: dict) -> MarketDataService:
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _fresh_state(*, single_entry_enabled: bool) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.single_entry_filter_enabled = single_entry_enabled
    return state


def _confirmed_flag_scenario(*, n: int, single_entry_enabled: bool, daily_single_entry_count: int = 0):
    """A single confirmed UP_RED crossover (big jump on the last two bars —
    same shape test_trend_persistence_filter.py uses)."""
    worker_start = _at(9, 0, day=6)
    confirm_at = worker_start + timedelta(minutes=3 * n, seconds=5)
    df_1m = _1m_from_3m_closes(worker_start, [100.0] * (n - 3) + [99.5, 99.9, 140.0])
    state = _fresh_state(single_entry_enabled=single_entry_enabled)
    state.daily_single_entry_count = daily_single_entry_count
    state.last_confirmed_bar_ts = (worker_start + timedelta(minutes=3 * (n - 2))).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes=dict(_WORKER_QUOTES))
    svc = _svc_with_quote(df_1m, confirm_at, _WORKER_QUOTES)
    return svc, state, broker, confirm_at


def test_toggle_off_leaves_legacy_behavior_completely_unchanged():
    """state.single_entry_filter_enabled is False -- _judge_entry_gate must
    return (None, "NONE") and the confirmed flag enters unconditionally,
    regardless of daily count, exactly like before this filter existed."""
    svc, state, broker, confirm_at = _confirmed_flag_scenario(n=35, single_entry_enabled=False)

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert state.last_single_entry_approved is None
    assert state.last_single_entry_decision is None


def test_toggle_on_under_cap_approves_new_buy():
    svc, state, broker, confirm_at = _confirmed_flag_scenario(
        n=35, single_entry_enabled=True, daily_single_entry_count=config.SINGLE_ENTRY_MAX_DAILY_ENTRIES - 1,
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert state.last_single_entry_approved is True
    assert state.last_single_entry_decision == config.SINGLE_ENTRY_APPROVED
    assert state.daily_single_entry_count == config.SINGLE_ENTRY_MAX_DAILY_ENTRIES
    assert state.last_single_entry_at is not None


def test_toggle_on_at_cap_blocks_new_buy():
    svc, state, broker, confirm_at = _confirmed_flag_scenario(
        n=35, single_entry_enabled=True, daily_single_entry_count=config.SINGLE_ENTRY_MAX_DAILY_ENTRIES,
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert not any(a.startswith("ENTRY:") for a in result.actions)
    assert state.position is None
    assert state.last_single_entry_approved is False
    assert state.last_single_entry_decision == config.SINGLE_ENTRY_DAILY_LIMIT_REACHED


def test_reversal_blocked_by_daily_limit_still_liquidates_held_position():
    """A confirmed OPPOSITE flag that the gate rejects must still sell the
    currently-held ETF (sell-only/no-re-entry) -- it just does not flip into
    the opposite ETF. Mirrors test_trend_persistence_filter.py's equivalent
    coverage for the Trend Persistence gate."""
    worker_start = _at(9, 0, day=6)
    n = 40
    confirm_at = worker_start + timedelta(minutes=3 * n, seconds=5)
    # Flat history, then a sharp DROP on the last two bars -> confirmed DOWN_BLUE.
    df_1m = _1m_from_3m_closes(worker_start, [140.0] * (n - 3) + [140.5, 140.1, 99.0])
    state = _fresh_state(single_entry_enabled=True)
    state.daily_single_entry_count = config.SINGLE_ENTRY_MAX_DAILY_ENTRIES  # cap already reached today
    state.last_confirmed_bar_ts = (worker_start + timedelta(minutes=3 * (n - 2))).isoformat()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)
    broker = FakeBroker(cash=10_000_000.0, quotes=dict(_WORKER_QUOTES))
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    svc = _svc_with_quote(df_1m, confirm_at, _WORKER_QUOTES)

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert any(a.startswith("OPPOSITE_SIGNAL_SELL_ONLY:") for a in result.actions)
    assert state.position is None
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert broker.get_position(config.INVERSE_SYMBOL) is None
    assert state.last_single_entry_approved is False
