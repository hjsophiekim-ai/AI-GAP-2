"""09:03 예약 매수 tests — 2026-08-07 real incident: an armed scheduled entry
never fired at 09:03, only a real flag at ~09:20 bought instead. Covers:

A. `_apply_day_rollover` race fix — arm_scheduled_entry (service.py) writes
   armed_direction/armed_at straight to state OUTSIDE run_once, uncoordinated
   with session_date. An arm made for TODAY must survive the same-day
   rollover that fires on the day's first tick (this is exactly what broke:
   버튼 누르기 -> 자동매매 시작 -> 그 첫 tick의 rollover가 방금 만든 예약을
   지워버림); only a STALE arm from a PRIOR day should be cleared.
B. `_scheduled_entry_protection_active` — True only while
   scheduled_entry_protected AND before config.SCHEDULED_ENTRY_PROTECTION_UNTIL.
C. End-to-end regression — the exact incident scenario (armed before the
   day's first tick) now fires at 09:03.
D. Protection window (2026-08-07 사용자 요청): a confirmed OPPOSITE flag
   within the protection window does not sell/switch the position (caught/
   logged only); the SAME direction is unaffected (already default
   behavior); after the window, OPPOSITE flags exit normally again;
   STOP_LOSS/FORCED_LIQUIDATION are never affected by the protection.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.signal_engine import forming_bar_window
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST

_WORKER_QUOTES = {
    config.WATCH_SYMBOL: 140.0,
    config.LONG_SYMBOL: 15_000.0,
    config.INVERSE_SYMBOL: 10_000.0,
}


def _at(hour: int, minute: int = 0, second: int = 0, *, day: int = 7) -> datetime:
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


def _fresh_state() -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    return state


# ══════════════════════════════════════════════════════════════════════════
# A. _apply_day_rollover no longer wipes a same-day arm
# ══════════════════════════════════════════════════════════════════════════
def test_rollover_preserves_arm_made_for_today_before_first_tick():
    state = _fresh_state()
    state.session_date = "20260806"  # yesterday -- worker hasn't ticked today yet
    state.scheduled_entry_armed_direction = Direction.DOWN_BLUE
    state.scheduled_entry_armed_at = _at(8, 55).isoformat()  # armed pre-market, TODAY
    state.scheduled_entry_armed_by = "ui"

    worker._apply_day_rollover(state, _at(9, 0, 0))

    assert state.session_date == "20260807"
    assert state.scheduled_entry_armed_direction == Direction.DOWN_BLUE
    assert state.scheduled_entry_armed_at == _at(8, 55).isoformat()
    assert state.scheduled_entry_armed_by == "ui"


def test_rollover_clears_stale_arm_from_a_prior_day():
    state = _fresh_state()
    state.session_date = "20260806"
    state.scheduled_entry_armed_direction = Direction.UP_RED
    state.scheduled_entry_armed_at = datetime(2026, 8, 6, 8, 55, tzinfo=KST).isoformat()  # yesterday

    worker._apply_day_rollover(state, _at(9, 0, 0))

    assert state.scheduled_entry_armed_direction is None
    assert state.scheduled_entry_armed_at is None


def test_rollover_always_clears_scheduled_entry_protected():
    state = _fresh_state()
    state.session_date = "20260806"
    state.scheduled_entry_protected = True

    worker._apply_day_rollover(state, _at(9, 0, 0))

    assert state.scheduled_entry_protected is False


# ══════════════════════════════════════════════════════════════════════════
# B. _scheduled_entry_protection_active
# ══════════════════════════════════════════════════════════════════════════
def test_protection_active_before_cutoff():
    state = _fresh_state()
    state.scheduled_entry_protected = True
    assert worker._scheduled_entry_protection_active(state, _at(9, 9, 59)) is True


def test_protection_inactive_at_cutoff():
    state = _fresh_state()
    state.scheduled_entry_protected = True
    assert worker._scheduled_entry_protection_active(state, _at(9, 10, 0)) is False


def test_protection_inactive_when_flag_not_set():
    state = _fresh_state()
    state.scheduled_entry_protected = False
    assert worker._scheduled_entry_protection_active(state, _at(9, 5)) is False


# ══════════════════════════════════════════════════════════════════════════
# C. End-to-end regression — exact 2026-08-07 incident scenario
# ══════════════════════════════════════════════════════════════════════════
def test_scheduled_entry_fires_even_when_armed_before_days_first_tick():
    """Reproduces the real incident: user arms 09:03 예약매수 BEFORE turning
    on auto-trading (so state.session_date is still yesterday's), then the
    day's first run_once tick at 09:03 must both roll the day over AND still
    fire the scheduled buy -- not silently discard the arm."""
    now = _at(9, 3, 5)
    # Bar generation must stay on whole-minute boundaries -- deriving it from
    # `now` directly (which carries :05 seconds, needed for the fire-window
    # check below) would offset every bar by those same seconds and make
    # market_data.filter_complete_3m_bars's exact per-minute presence check
    # fail for every bin (same pitfall test_sideways_filter.py's
    # _confirmed_flag_scenario docstring warns about).
    warmup_start = now.replace(second=0, microsecond=0) - timedelta(minutes=300)
    df_1m = _1m_from_3m_closes(warmup_start, [100.0] * 100)
    svc = _svc_with_quote(df_1m, now, _WORKER_QUOTES)
    broker = FakeBroker(cash=10_000_000.0, quotes=dict(_WORKER_QUOTES))

    state = _fresh_state()
    state.session_date = "20260806"  # worker has not ticked today yet
    state.scheduled_entry_armed_direction = Direction.DOWN_BLUE
    state.scheduled_entry_armed_at = _at(8, 55).isoformat()  # armed pre-market, today

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("SCHEDULED_ENTRY_0903:") for a in result.actions), (
        f"actions={result.actions} skipped={result.skipped} session_date={state.session_date} "
        f"armed={state.scheduled_entry_armed_direction} order_block_reason={state.order_block_reason} "
        f"should_fire={worker._scheduled_entry_should_fire(state, now)} warmup_ready={state.warmup_ready}"
    )
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert state.scheduled_entry_executed_at is not None
    assert state.scheduled_entry_protected is True


def test_scheduled_entry_does_not_fire_without_arming():
    now = _at(9, 3, 5)
    warmup_start = now.replace(second=0, microsecond=0) - timedelta(minutes=300)
    df_1m = _1m_from_3m_closes(warmup_start, [100.0] * 100)
    svc = _svc_with_quote(df_1m, now, _WORKER_QUOTES)
    broker = FakeBroker(cash=10_000_000.0, quotes=dict(_WORKER_QUOTES))
    state = _fresh_state()

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.warmup_ready is True
    assert not any(a.startswith("SCHEDULED_ENTRY_0903:") for a in result.actions)
    assert state.position is None


# ══════════════════════════════════════════════════════════════════════════
# D. Protection window — opposite flag within it is caught but not acted on
# ══════════════════════════════════════════════════════════════════════════
def _scheduled_entry_then_opposite_flag(*, minutes_after_open: int, held_direction: Direction):
    """Tick 1 (09:03:05): the scheduled entry fires into ``held_direction``,
    exactly like a live worker tick -- this seeds last_confirmed_bar_ts/
    position/scheduled_entry_protected the same way production does, so
    there is no need to hand-craft that state. Tick 2 (09:00 +
    minutes_after_open, +5s -- must be a multiple of 3 to land on a bar
    boundary): a genuine confirmed crossover OPPOSING held_direction (a
    price jump on the bar just before it). Ample prior-day bars provide
    MACD warm-up so the flag can appear this early in the session -- without
    them the very first bar of the day is baseline-only (see
    worker._advance_confirmed_primary) and real warm-up would push any flag
    hours past market open."""
    prior_start = _at(9, 0, day=6)
    today_start = _at(9, 0, day=7)
    flag_direction = Direction.DOWN_BLUE if held_direction == Direction.UP_RED else Direction.UP_RED
    n_today = minutes_after_open // 3
    # Same proven shape tests/macd2/test_sideways_filter.py's own
    # _confirmed_flag_scenario uses for a guaranteed UP_RED crossover
    # (small dip then a huge jump up), mirrored for DOWN_BLUE (small rise
    # then a huge drop).
    tail = [99.5, 99.9, 140.0] if flag_direction == Direction.UP_RED else [100.5, 100.9, 60.0]
    today_closes = [100.0] * (n_today - 3) + tail
    df_1m = pd.concat([
        _1m_from_3m_closes(prior_start, [100.0] * 40),
        _1m_from_3m_closes(today_start, today_closes),
    ], ignore_index=True)

    scheduled_fire_at = _at(9, 3, 5)
    confirm_at = today_start + timedelta(minutes=minutes_after_open, seconds=5)

    state = _fresh_state()
    state.sideways_filter_enabled = False
    state.scheduled_entry_armed_direction = held_direction
    state.scheduled_entry_armed_at = _at(8, 55).isoformat()

    broker = FakeBroker(cash=10_000_000.0, quotes=dict(_WORKER_QUOTES))
    svc = _svc_with_quote(df_1m, confirm_at, _WORKER_QUOTES)

    fire_result = run_once(broker=broker, market_data=svc, state=state, now=scheduled_fire_at)
    assert any(a.startswith("SCHEDULED_ENTRY_0903:") for a in fire_result.actions), fire_result.actions
    held_symbol = state.position.symbol
    assert state.scheduled_entry_protected is True

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)
    return result, state, broker, confirm_at, held_symbol


def test_opposite_flag_inside_protection_window_does_not_exit():
    result, state, broker, confirm_at, held_symbol = _scheduled_entry_then_opposite_flag(
        minutes_after_open=9, held_direction=Direction.UP_RED,  # -> 09:09:05, still inside 09:03-09:10
    )
    assert worker._scheduled_entry_protection_active(state, confirm_at) is True

    assert not any(a.startswith("OPPOSITE_SIGNAL") for a in result.actions)
    assert state.position is not None and state.position.symbol == held_symbol
    assert broker.get_position(held_symbol) is not None
    assert ledger.load_signal_ledger()[-1]["block_reason"] == config.SCHEDULED_ENTRY_PROTECTION_ACTIVE


def test_opposite_flag_after_protection_window_exits_normally():
    result, state, broker, confirm_at, held_symbol = _scheduled_entry_then_opposite_flag(
        minutes_after_open=12, held_direction=Direction.UP_RED,  # -> 09:12:05, past 09:10
    )
    assert worker._scheduled_entry_protection_active(state, confirm_at) is False

    assert any(a.startswith("OPPOSITE_SIGNAL") for a in result.actions)
    assert broker.get_position(held_symbol) is None


def test_stop_loss_still_fires_inside_protection_window():
    """Protection only covers the OPPOSITE-flag exit path; STOP_LOSS remains
    fully active regardless (docs §10 priority, checked before any flag
    handling)."""
    now = _at(9, 6, 0)
    df_1m = _1m_from_3m_closes(now - timedelta(minutes=300), [100.0] * 100)
    svc = _svc_with_quote(df_1m, now, {**_WORKER_QUOTES, config.LONG_SYMBOL: 14_000.0})

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, 14_000.0)
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)
    state.scheduled_entry_protected = True
    state.scheduled_entry_executed_at = _at(9, 3).isoformat()
    bar_start, _ = forming_bar_window(now)
    state.stop_loss_bar_symbol = config.LONG_SYMBOL
    state.stop_loss_entry_bar_ts = (bar_start - timedelta(minutes=6)).isoformat()
    state.stop_loss_bar_ts = (bar_start - timedelta(minutes=3)).isoformat()
    state.stop_loss_bar_close = 14_000.0

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("STOP_LOSS:") for a in result.actions)
    assert state.position is None
    assert broker.get_position(config.LONG_SYMBOL) is None
