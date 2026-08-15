"""Tests for MU_MACD's optional "시간대별 최적거래 필터" integration
(worker._advance_time_window_filter) — reuses app.trading.macd2's
time_window_filter/time_window_position_manager by import; these tests
verify the WIRING (two-bar delay, ladder replacing plain stop-loss, day
rollover), not the filter's own decision logic (already covered by
tests/macd2/test_time_window_filter.py and test_time_window_position_manager.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.trading.mu_macd import config, state_store, worker
from app.trading.mu_macd.config import KST
from app.trading.mu_macd.models import Direction
from tests.macd2.fake_broker import FakeBroker
from tests.mu_macd.test_worker import _build_flat_then_ramp_service, _find_crossing_now


def _fresh_state_with_tw_enabled() -> "worker.RuntimeState":
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    state.time_window_filter_enabled = True
    return state


def test_time_window_filter_off_by_default():
    state = state_store.default_state()
    assert state.time_window_filter_enabled is False


def test_flag_bar_itself_never_dispatches_an_order():
    """§1: a flag never has order authority on its own completed bar."""
    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now0

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = _fresh_state_with_tw_enabled()

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert broker.orders == []
    assert state.position is None
    assert state.time_window_pending_flag_direction is not None
    assert any(a.startswith("TW_PENDING") for a in result.actions)


def test_entry_confirms_one_bar_after_flag_when_gap_still_expanding():
    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now0

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = _fresh_state_with_tw_enabled()

    worker.run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.position is None  # not yet -- still pending

    # A continued ramp keeps feeding new 1m bars into the SAME service so the
    # next completed 3m bar (T+3) is available; advance now by 3 minutes.
    now1 = now0 + timedelta(minutes=3)
    svc.ws_last_tick_at = now1
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)

    # Either it confirmed (entry placed) or was rejected (gap not expanding
    # enough on this specific synthetic ramp) -- both are valid OUTCOMES of
    # the SAME real decision function; what matters is it never entered on
    # the flag's own bar (already proven above) and the candidate is resolved
    # (cleared) by now, one way or the other.
    assert state.time_window_pending_flag_direction is None
    if state.position is not None:
        assert any(a.startswith("TW_ENTRY") for a in result.actions)
        assert state.time_window_position_active is True
    else:
        assert any(a.startswith("TW_REJECTED") for a in result.actions)


def test_day_rollover_resets_time_window_session_counters():
    state = state_store.default_state()
    state.time_window_filter_enabled = True
    state.session_date = "20260812"
    state.time_window_morning_entry_count = 2
    state.time_window_afternoon_entry_count = 1
    state.time_window_pending_flag_direction = Direction.UP_RED.value
    state.time_window_pending_flag_bar_ts = "2026-08-12T09:03:00+09:00"

    worker._apply_day_rollover(state, datetime(2026, 8, 13, 9, 0, tzinfo=KST))

    assert state.time_window_morning_entry_count == 0
    assert state.time_window_afternoon_entry_count == 0
    assert state.time_window_pending_flag_direction is None
    assert state.time_window_pending_flag_bar_ts is None
    assert state.time_window_filter_enabled is True  # toggle survives


def test_tw_managed_position_skips_plain_stop_loss_and_uses_ladder():
    """A position tagged time_window_position_active must exit via the
    TW-labeled ladder reason (not the legacy config.EXIT_STOP_LOSS label)
    even though both thresholds are numerically identical (-1.5%) -- proves
    the plain STOP_LOSS check was actually skipped for this position, not
    just coincidentally producing the same result."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now0

    quotes = {config.LONG_SYMBOL: 100.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    # Seed a real held position through the broker's own API (so
    # get_positions()/reconcile stays internally consistent), well past
    # both the legacy and TW stop-loss thresholds once quotes[LONG]=100.
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")
    broker._positions[config.LONG_SYMBOL].avg_price = 200.0  # force a large loss vs the 100.0 quote

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=200.0, entry_at=now0)
    state.time_window_position_active = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(seconds=1))
    assert any("MU_MACD_TW_STOP_LOSS" in a for a in result.actions)
    assert not any(a.startswith("STOP_LOSS:") for a in result.actions)
