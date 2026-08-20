"""Worker-level tests for the "무필터 09:00-11:00" 즉시청산 진입모드
(2026-08-20) -- a 6th peer entry gate alongside MAJOR/SIDEWAYS/TREND_
PERSISTENCE/SINGLE_ENTRY (see worker._judge_no_filter_flag / _judge_entry_gate).
The core requirement under test: a rejected reversal against a position
opened by THIS gate always sells immediately, on the SAME bar the opposite
flag confirms -- never the TIME_WINDOW filter's own two-bar (T -> T+3)
pending/whipsaw-tolerant mechanism, and with no gap/quality check of any
kind. Mirrors tests/macd2/test_worker_time_window.py's harness (FakeBroker +
fake MarketDataService) so no duplicated test infrastructure is introduced.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
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
_INSIDE_WINDOW = datetime(2026, 1, 7, 10, 0, tzinfo=KST)
_OUTSIDE_WINDOW = datetime(2026, 1, 7, 13, 0, tzinfo=KST)


def _fresh_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.no_filter_0900_1100_enabled = True
    return state


@pytest.fixture
def market_data():
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


# ── default-OFF backward compatibility ──────────────────────────────────────

def test_default_off_leaves_judge_entry_gate_at_none():
    """With every filter toggle at its default (all OFF), _judge_entry_gate
    must still return (None, "NONE") -- adding the new 6th gate must never
    change legacy (no-filter-at-all) behavior when the new toggle itself is
    untouched."""
    state = state_store.default_state()
    assert state.no_filter_0900_1100_enabled is False
    decision, mode = worker._judge_entry_gate(
        state=state, bars_3m=None, direction=Direction.UP_RED, position=None,
        now=_INSIDE_WINDOW, signal_id="x",
    )
    assert decision is None
    assert mode == "NONE"


# ── _judge_no_filter_flag: pure time-window gate, no other condition ───────

def test_judge_no_filter_flag_approves_inside_window():
    state = _fresh_state()
    decision = worker._judge_no_filter_flag(state=state, now=_INSIDE_WINDOW, signal_id="sig-1")
    assert decision.approved is True
    assert decision.block_reason is None
    assert state.last_no_filter_0900_1100_approved is True


def test_judge_no_filter_flag_rejects_outside_window():
    state = _fresh_state()
    decision = worker._judge_no_filter_flag(state=state, now=_OUTSIDE_WINDOW, signal_id="sig-2")
    assert decision.approved is False
    assert decision.block_reason == config.NO_FILTER_REJECT_OUTSIDE_WINDOW
    assert state.last_no_filter_0900_1100_approved is False
    assert state.last_no_filter_0900_1100_block_reason == config.NO_FILTER_REJECT_OUTSIDE_WINDOW


def test_no_filter_takes_priority_over_sideways_major_etc_but_not_time_window():
    """_judge_entry_gate priority: TIME_WINDOW > NO_FILTER_0900_1100 >
    SIDEWAYS > MAJOR > ... -- both toggles on means TIME_WINDOW wins."""
    state = _fresh_state()
    state.sideways_filter_enabled = True
    state.major_filter_enabled = True
    _decision, mode = worker._judge_entry_gate(
        state=state, bars_3m=None, direction=Direction.UP_RED, position=None,
        now=_INSIDE_WINDOW, signal_id="x",
    )
    assert mode == "NO_FILTER_0900_1100"

    state.time_window_filter_enabled = True
    minimal_bars_3m = pd.DataFrame({"datetime": [pd.Timestamp(_INSIDE_WINDOW)]})
    _decision2, mode2 = worker._judge_entry_gate(
        state=state, bars_3m=minimal_bars_3m, df_1m=None, direction=Direction.UP_RED, position=None,
        now=_INSIDE_WINDOW, signal_id="x",
    )
    assert mode2 == "TIME_WINDOW"


# ── core regression: reversal against a NO_FILTER position always sells   ──
# ── immediately, on the SAME bar the opposite flag confirms              ──

def test_opposite_flag_switches_a_no_filter_position_on_the_same_bar(market_data, monkeypatch):
    """Contrasts directly with test_worker_time_window.py's own
    test_fresh_opposite_flag_registers_a_pending_candidate_while_position_held
    (TIME_WINDOW never switches on the flag's own bar -- it registers a
    pending candidate and waits for T+3). A NO_FILTER-0900-1100-entered
    position has no such two-bar mechanism: an opposite confirmed flag must
    switch the position on the VERY SAME tick it confirms."""
    svc, now0 = market_data
    assert config.NO_FILTER_ENTRY_WINDOW_START <= now0.astimezone(KST).time() < config.NO_FILTER_ENTRY_WINDOW_END
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    monkeypatch.setattr(worker, "_advance_confirmed_primary", lambda state, macd_snap, now: Direction.UP_RED)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("OPPOSITE_SIGNAL") for a in result.actions), (
        f"expected an immediate OPPOSITE_SIGNAL switch on this same tick, got actions={result.actions}"
    )
    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL, "must have switched into the new (UP_RED) direction's ETF"
    # never went through TIME_WINDOW's pending/whipsaw machinery
    assert state.time_window_pending_flag_direction is None
    assert state.time_window_position_active is False


def test_opposite_flag_outside_window_sells_but_does_not_reenter(market_data, monkeypatch):
    """Outside 09:00-11:00, a NO_FILTER position still gets liquidated on an
    opposite confirmed flag (never left unmonitored), but the new direction
    is NOT entered (sell-only, no re-entry) -- matches the same "sell-only"
    convention every other simple filter already uses when its own gate
    rejects a reversal (worker._execute_reversal_exit_only_for_filtered_entry)."""
    svc, now0 = market_data
    outside_now = now0.replace(hour=13, minute=0)
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    monkeypatch.setattr(worker, "_advance_confirmed_primary", lambda state, macd_snap, now: Direction.UP_RED)

    result = run_once(broker=broker, market_data=svc, state=state, now=outside_now)

    assert any(a.startswith("OPPOSITE_SIGNAL_SELL_ONLY") for a in result.actions), (
        f"expected sell-only (no re-entry) outside the window, got actions={result.actions}"
    )
    assert state.position is None


# ── position risk-management: plain SL/forced-liq, never the TW ladder ────

def test_no_filter_position_never_marks_time_window_position_active(market_data):
    """A NO_FILTER-entered position must never set time_window_position_
    active=True -- that flag gates _advance_held_position_risk_management's
    choice between the TW TP1/TP2/trailing ladder and the plain STOP_LOSS/
    FORCED_LIQUIDATION path; a NO_FILTER position must always get the
    latter."""
    svc, now0 = market_data
    state = _fresh_state()
    for step in range(40):
        now = now0 + timedelta(minutes=3 * step)
        run_once(broker=FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}), market_data=svc, state=state, now=now)
        assert state.time_window_position_active is False


# ── state_store round-trip ──────────────────────────────────────────────────

def test_state_store_round_trips_no_filter_toggle():
    from app.trading.macd2 import state_store as ss

    state = ss.default_state()
    state.no_filter_0900_1100_enabled = True
    state.no_filter_0900_1100_enabled_at = "2026-08-20T10:00:00+09:00"
    state.no_filter_0900_1100_enabled_by = "ui"
    d = ss.serialize(state)
    restored = ss.deserialize(d)
    assert restored.no_filter_0900_1100_enabled is True
    assert restored.no_filter_0900_1100_enabled_by == "ui"
    assert restored.no_filter_0900_1100_filter_version == config.NO_FILTER_0900_1100_FILTER_VERSION


def test_service_set_no_filter_0900_1100_filter_enabled():
    from app.trading.macd2 import service as service_module

    svc = service_module.Macd2Service()
    result = svc.set_no_filter_0900_1100_filter_enabled(True, changed_by="test-suite")
    assert result["ok"] is True
    assert result["no_filter_0900_1100_enabled"] is True
    assert result["previous"] is False
    assert result["no_filter_0900_1100_enabled_by"] == "test-suite"

    state = state_store.load_state()
    assert state.no_filter_0900_1100_enabled is True
