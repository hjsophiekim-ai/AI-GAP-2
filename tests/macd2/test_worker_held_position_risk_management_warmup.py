"""Regression tests for the 2026-08-15 fix: a held position's FORCED_
LIQUIDATION / STOP_LOSS / time-window ladder must never depend on macd_snap
readiness (see worker._advance_held_position_risk_management's own
docstring). Before the fix, all three lived inside run_once()'s "Held
position: priority chain", itself reached only AFTER the
``if macd_snap is None: result.skipped = "NOT_READY"; return result`` early
return — so a held position had zero risk management on any tick where
warm-up wasn't ready yet (narrow for MACD2 given its real prior-day
backfill, but the same class of gap as MU_MACD's own same-day fix).

Every test here uses a FRESH (never-bootstrapped) MarketDataService with an
empty history cache -- macd_snap is guaranteed None -- to prove the fix
holds even in the worst case. state.stop_loss_bar_*/entry_bar_ts are seeded
directly (mirroring exactly what a real entry seeds, see worker.py's own
_apply_switch_outcome around "state.stop_loss_bar_symbol = ...") so a
SINGLE run_once() call already sees a bar strictly after the (simulated)
entry bar -- avoiding a multi-tick dance to get _advance_stop_loss_bar past
its own "entry bar itself is never eligible" exclusion.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.macd2 import config, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import PositionSnapshot
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _cold_market_data(quotes: dict) -> MarketDataService:
    """A MarketDataService with an empty 1-minute history cache (never
    bootstrapped) but real, fresh quotes -- macd_snap will be None
    (insufficient warm-up), matching a real cold-start/restart tick."""
    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return pd.DataFrame(), {}

    def fake_quote(mode, symbol):
        del mode
        return quotes.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    svc.refresh_quotes()
    return svc


def _fresh_state(*, budget: float = 10_000_000.0) -> "worker.RuntimeState":
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    return state


def _seed_held_since(state, *, symbol: str, entered_at: datetime, last_bar_close: float) -> None:
    """Simulates a position entered two completed 3-minute bars ago, whose
    most recently completed bar closed at ``last_bar_close`` -- so the very
    next run_once() call (at ``entered_at`` + 6 minutes or later) evaluates
    that close as eligible (strictly after the entry bar)."""
    entry_bar_start, _ = worker.forming_bar_window(entered_at)
    last_bar_start = entry_bar_start + timedelta(minutes=3)
    state.stop_loss_bar_symbol = symbol
    state.stop_loss_entry_bar_ts = entry_bar_start.isoformat()
    state.stop_loss_bar_ts = last_bar_start.isoformat()
    state.stop_loss_bar_close = last_bar_close


def test_forced_liquidation_fires_even_when_macd_snap_not_ready():
    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    market_data = _cold_market_data(quotes)
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")  # so reconcile_position_state doesn't reset state.position

    state = _fresh_state()
    now0 = datetime(2026, 1, 7, 15, 0, 1, tzinfo=KST)  # past FORCE_LIQUIDATE_AT (15:00)
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0, entry_at=now0)

    result = run_once(broker=broker, market_data=market_data, state=state, now=now0)

    assert any(a.startswith("FORCED_LIQUIDATION:") for a in result.actions)
    assert state.position is None
    assert result.skipped != "NOT_READY"


def test_legacy_stop_loss_fires_even_when_macd_snap_not_ready():
    quotes = {config.LONG_SYMBOL: 14_000.0, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    market_data = _cold_market_data(quotes)
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")  # so reconcile_position_state doesn't reset state.position

    state = _fresh_state()
    entered_at = datetime(2026, 1, 7, 9, 0, 0, tzinfo=KST)
    now0 = entered_at + timedelta(minutes=9)
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0, entry_at=entered_at)
    # avg_price 15,000 vs a completed-bar close of 14,000 -> -6.67% net, well past MACD2's -1.5% stop loss.
    _seed_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at, last_bar_close=14_000.0)

    result = run_once(broker=broker, market_data=market_data, state=state, now=now0)

    assert any(a.startswith("STOP_LOSS:") for a in result.actions)
    assert state.position is None
    assert result.skipped != "NOT_READY"


def test_tw_ladder_stop_loss_fires_even_when_macd_snap_not_ready():
    """Same as above, but for a position the time-window filter itself is
    managing -- must exit via the TW-labeled ladder, not the legacy
    STOP_LOSS path (mirrors the analogous MU_MACD regression test)."""
    quotes = {config.LONG_SYMBOL: 14_000.0, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    market_data = _cold_market_data(quotes)
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")  # so reconcile_position_state doesn't reset state.position

    state = _fresh_state()
    state.time_window_filter_enabled = True
    state.time_window_position_active = True
    entered_at = datetime(2026, 1, 7, 9, 0, 0, tzinfo=KST)
    now0 = entered_at + timedelta(minutes=9)
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0, entry_at=entered_at)
    _seed_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at, last_bar_close=14_000.0)

    result = run_once(broker=broker, market_data=market_data, state=state, now=now0)

    assert any(config.EXIT_TW_STOP_LOSS in a for a in result.actions)
    assert not any(a.startswith("STOP_LOSS:") for a in result.actions)
    assert state.position is None
    assert state.time_window_position_active is False
    assert result.skipped != "NOT_READY"


def test_tw_ladder_tp1_partial_fires_even_when_macd_snap_not_ready():
    quotes = {config.LONG_SYMBOL: 15_470.0, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    market_data = _cold_market_data(quotes)
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")  # so sell_market/partial exit has a real position to reduce

    state = _fresh_state()
    state.time_window_filter_enabled = True
    state.time_window_position_active = True
    entered_at = datetime(2026, 1, 7, 9, 0, 0, tzinfo=KST)
    now0 = entered_at + timedelta(minutes=9)
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0, entry_at=entered_at)
    # avg_price 15,000 vs a completed-bar close of 15,470 -> +3.13% gross, but
    # _net_return_pct is fee/slippage-adjusted (TradeCostEngine), so this is
    # +3.04% NET -- past MORNING_TP1 (3.0% net, not gross).
    _seed_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at, last_bar_close=15_470.0)

    result = run_once(broker=broker, market_data=market_data, state=state, now=now0)

    assert any(config.EXIT_TW_TP1_PARTIAL in a for a in result.actions)
    assert state.position is not None
    assert state.position.quantity == 5  # MORNING_TP1_SELL_RATIO=0.5 of 10
    assert state.time_window_tp1_done is True
    assert state.time_window_position_active is True  # still managed -- not a full exit
    assert result.skipped != "NOT_READY"
