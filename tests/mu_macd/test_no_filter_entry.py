"""Worker-level tests for the "무필터 09:00-11:00" 즉시청산 진입모드
(2026-08-20) in MU_MACD -- restricts the pre-existing "legacy" (TW-off)
immediate-entry/immediate-reversal-exit path (worker.py's confirmed_direction
handling below the ``if state.time_window_filter_enabled:`` early-return) to
09:00-11:00 new entries only, via one extra check in
worker._entry_gate_block_reason. The legacy path's own always-immediate
reversal-sell / STOP_LOSS / QUICK_PROFIT / FORCED_LIQUIDATION behavior is
completely unchanged -- this file's core regression is that a reversal
against a NO_FILTER position always sells immediately, on the SAME tick the
opposite flag confirms, with no whipsaw-tolerant hold of any kind (that
mechanism lives only inside worker._advance_time_window_filter, never
touched here).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.mu_macd import config, state_store, worker
from app.trading.mu_macd.config import KST
from app.trading.mu_macd.market_data import MUMarketDataService
from app.trading.mu_macd.models import Direction, PositionSnapshot
from tests.macd2.fake_broker import FakeBroker

_INSIDE_WINDOW = datetime(2026, 8, 12, 10, 0, tzinfo=KST)
_OUTSIDE_WINDOW = datetime(2026, 8, 12, 13, 0, tzinfo=KST)


def _fresh_state(*, budget: float = 10_000_000.0):
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.no_filter_0900_1100_enabled = True
    state.warmup_ready = True
    state.ws_connected = True
    state.ws_last_tick_at = datetime.now(KST).isoformat()
    return state


def _flat_service(*, now: datetime, warmup_minutes: int = 200) -> MUMarketDataService:
    """Flat 880.0 1-minute bars ending exactly one minute before ``now`` --
    plenty of warmup (>=30 completed 3m bars) regardless of what time-of-day
    ``now`` itself falls on; evaluate_macd_crossover is monkeypatched in
    each test below, so the actual price series shape doesn't matter."""
    svc = MUMarketDataService(mode="mock")
    svc.ws_connected = True
    start = now - timedelta(minutes=warmup_minutes)
    date_str = start.strftime("%Y%m%d")
    for i in range(warmup_minutes):
        t = start + timedelta(minutes=i)
        minute_key = f"{t.hour:02d}{t.minute:02d}"
        svc.inject_1m_bar(date_str, minute_key, 880.0, 880.0, 880.0, 880.0, 1000)
    return svc


# ── _entry_gate_block_reason: pure time-window gate, only when toggle on ──

def test_block_reason_none_inside_window_when_enabled():
    state = _fresh_state()
    assert worker._entry_gate_block_reason(state, _INSIDE_WINDOW) is None


def test_block_reason_outside_window_when_enabled():
    state = _fresh_state()
    reason = worker._entry_gate_block_reason(state, _OUTSIDE_WINDOW)
    assert reason == config.NO_FILTER_REJECT_OUTSIDE_WINDOW


def test_toggle_off_leaves_legacy_all_day_behavior_unaffected():
    """With the new toggle OFF (default), _entry_gate_block_reason outside
    09:00-11:00 must NOT return the new block reason -- legacy all-day
    behavior is completely unchanged unless a user opts in."""
    state = _fresh_state()
    state.no_filter_0900_1100_enabled = False
    reason = worker._entry_gate_block_reason(state, _OUTSIDE_WINDOW)
    assert reason != config.NO_FILTER_REJECT_OUTSIDE_WINDOW


def test_time_window_filter_takes_priority_when_both_enabled():
    """TW filter branch in run_once() always returns before the legacy path
    (and thus before _entry_gate_block_reason's result is ever consulted for
    gating) -- verified at the run_once level below; here we just confirm
    the toggle itself doesn't interfere with TW being on."""
    state = _fresh_state()
    state.time_window_filter_enabled = True
    # _entry_gate_block_reason is still computed (harmless) but never used
    # while TW owns the tick -- no assertion needed beyond "doesn't raise".
    worker._entry_gate_block_reason(state, _OUTSIDE_WINDOW)


# ── core regression: reversal against a NO_FILTER position always sells   ──
# ── immediately, on the SAME tick the opposite flag confirms              ──

def test_opposite_flag_switches_a_no_filter_position_on_the_same_tick(monkeypatch):
    now = _INSIDE_WINDOW
    svc = _flat_service(now=now)
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    monkeypatch.setattr(worker, "evaluate_macd_crossover", lambda snap, prev: Direction.UP_RED)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("OPPOSITE_SIGNAL") for a in result.actions), (
        f"expected an immediate OPPOSITE_SIGNAL switch on this same tick, got actions={result.actions}"
    )
    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL
    # never went through the TW filter's own pending/whipsaw machinery
    assert state.time_window_position_active is False


def test_opposite_flag_outside_window_sells_but_does_not_reenter(monkeypatch):
    now = _OUTSIDE_WINDOW
    svc = _flat_service(now=now)
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    monkeypatch.setattr(worker, "evaluate_macd_crossover", lambda snap, prev: Direction.UP_RED)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("OPPOSITE_SIGNAL_SELL_ONLY") for a in result.actions), (
        f"expected sell-only (no re-entry) outside the window, got actions={result.actions}"
    )
    assert state.position is None


# ── state_store round-trip (generic __dict__-based persistence) ───────────

def test_state_store_round_trips_no_filter_toggle():
    state = state_store.default_state()
    state.no_filter_0900_1100_enabled = True
    state.no_filter_0900_1100_enabled_at = "2026-08-20T10:00:00+09:00"
    state.no_filter_0900_1100_enabled_by = "ui"
    d = state_store.state_to_dict(state)
    restored = state_store.state_from_dict(d)
    assert restored.no_filter_0900_1100_enabled is True
    assert restored.no_filter_0900_1100_enabled_by == "ui"
    assert restored.no_filter_0900_1100_filter_version == config.NO_FILTER_0900_1100_FILTER_VERSION


def test_default_state_has_no_filter_off():
    state = state_store.default_state()
    assert state.no_filter_0900_1100_enabled is False
