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
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_macd_crossover,
    forming_bar_window,
    make_signal_id,
    resample_completed_3m,
)
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
    state.major_filter_enabled = False
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
    loaded synthetic sine-wave session — no incremental re-fetch simulation
    needed. The Primary crossover is confirmed-completed-bar-only (docs
    2026-07-27 KIS-parity fix) and dispatches immediately on a single tick —
    no arm/confirm gap needed here (that only applies to the shadow/candidate
    forming-bar display, not order authority)."""
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

    # 주문 후 실제 잔고와 state 일치 (docs §5): the broker's own real position
    # must exactly match what state.position claims — never trust the order
    # response alone, always reconcile against the actual account.
    broker_position = broker.get_position(state.position.symbol)
    assert broker_position is not None
    assert broker_position.quantity == state.position.quantity
    assert broker_position.avg_price == state.position.avg_price


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


def test_provisional_candidate_never_dispatches_order_shadow_only():
    """docs §1/§5 MACD single-path fix: the forming-bar candidate must NEVER
    place a real order, touch the signal ledger, or gain processed_signal_ids
    authority — only the confirmed, completed-3m-bar crossover may (see
    _advance_confirmed_primary/test_confirmed_dispatch_within_5_seconds_of_detection).
    The candidate's own shadow/diagnostic fields (provisional_flag,
    candidate_confirmed_at) still populate normally for UI display."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    run_once(broker=broker, market_data=svc, state=state, now=now)  # arms the candidate
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))  # candidate confirmed (shadow only)

    assert result.actions == []
    assert state.position is None
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []
    assert state.candidate_confirmed_at is not None
    assert state.provisional_flag == Direction.UP_RED


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


def test_provisional_down_candidate_never_dispatches_order_shadow_only():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=60.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    run_once(broker=broker, market_data=svc, state=state, now=now)  # arms the candidate
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))  # candidate confirmed (shadow only)

    assert result.actions == []
    assert state.position is None
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []
    assert state.provisional_flag == Direction.DOWN_BLUE


def test_provisional_candidate_repeated_ticks_never_dispatch_order():
    """Repeated ticks against a persistently-confirmed forming-bar candidate
    must never place any order — order authority stays exclusively with the
    confirmed completed-bar crossover (docs §1/§5)."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = _forming_now(start)
    state = _fresh_state()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    run_once(broker=broker, market_data=svc, state=state, now=now)  # arms the candidate
    confirm_now = now + timedelta(seconds=5)
    run_once(broker=broker, market_data=svc, state=state, now=confirm_now)  # candidate confirmed (shadow only)
    for _ in range(20):
        run_once(broker=broker, market_data=svc, state=state, now=confirm_now)

    assert broker.orders == []
    assert ledger.load_signal_ledger() == []


def test_provisional_momentary_revert_cancels_candidate_zero_signals_and_orders():
    """docs 2026-07-27 momentary-crossing fix (shadow display only since docs
    §1/§5): a single-tick crossing that reverts before a second fresh
    confirming tick cancels the shadow candidate; and — regardless of
    revert/re-confirm — the forming-bar candidate never reaches the
    ledger/order layer at all, confirmed or not."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = _forming_now(start)
    state = _fresh_state()
    state.last_confirmed_bar_ts = (now - timedelta(minutes=3)).isoformat()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == []

    svc._quotes[config.WATCH_SYMBOL] = QuoteSnapshot(
        config.WATCH_SYMBOL, 100.0, datetime.now(KST), 0.0, "test", None,
    )
    run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=2))  # reverts -> cancel
    assert state.candidate_flag is None

    svc._quotes[config.WATCH_SYMBOL] = QuoteSnapshot(
        config.WATCH_SYMBOL, 140.0, datetime.now(KST), 0.0, "test", None,
    )
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=4))

    assert result.actions == []
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []
    assert state.provisional_flag == Direction.UP_RED  # shadow re-confirmed, no order authority


def test_provisional_same_bar_candidate_cancel_then_reconfirm_never_dispatches():
    """후보 취소 후 같은 3분봉에서 재교차가 다시 2회(>=3s) 확인되어 shadow
    candidate가 재확정되더라도(docs §1/§5) 주문·원장 기록은 0건이어야 한다."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _flat_completed_history(start)
    svc = _provisional_service(df_1m, watch_price=140.0)
    now = _forming_now(start)
    state = _fresh_state()
    state.last_confirmed_bar_ts = (now - timedelta(minutes=3)).isoformat()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    run_once(broker=broker, market_data=svc, state=state, now=now)  # candidate armed
    svc._quotes[config.WATCH_SYMBOL] = QuoteSnapshot(
        config.WATCH_SYMBOL, 100.0, datetime.now(KST), 0.0, "test", None,
    )
    run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=2))  # reverts -> cancel
    svc._quotes[config.WATCH_SYMBOL] = QuoteSnapshot(
        config.WATCH_SYMBOL, 140.0, datetime.now(KST), 0.0, "test", None,
    )
    run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=4))  # re-crossed: fresh 1st sighting
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=8))  # 2nd sighting, gap=4s -> confirm

    assert result.actions == []
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []
    assert state.provisional_flag == Direction.UP_RED
    assert state.candidate_confirmed_at is not None


def test_baseline_only_first_forming_bar_of_day_produces_no_signal_or_order():
    """docs 2026-07-27: on the first forming bar of a new trading day,
    previous_diff still refers to YESTERDAY's last completed bar — an
    overnight-gap zero-crossing there must set direction baseline only,
    never a candidate/signal/order, until today's own first 3m bar completes."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    declining_closes = [200.0 - i * 1.0 for i in range(100)]  # ends with previous_diff < 0
    df_1m = _1m_from_3m_closes(prior_day, declining_closes)
    quote_prices = {config.WATCH_SYMBOL: 300.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=prior_day + timedelta(days=2))
    svc.refresh_quotes()

    state = _fresh_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    next_day_open = prior_day.replace(day=6) + timedelta(seconds=5)  # 2026-01-06 09:00:05 (next trading day)
    result = run_once(broker=broker, market_data=svc, state=state, now=next_day_open)

    assert result.actions == []
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []
    assert state.candidate_flag is None
    assert state.provisional_flag is None

    # Baseline diagnostics (from initialize_strategy_session-style bootstrap
    # baseline, not this gate) still exist — this test only asserts the new
    # crossing itself never became order-authoritative.
    assert state.primary_previous_diff is not None and state.primary_previous_diff < 0


def test_provisional_candidate_opposite_direction_never_triggers_switch():
    """A held position (opened via the real confirmed-bar Primary crossover)
    must never be switched/sold by a forming-bar CANDIDATE, no matter how
    strongly the live quote suggests the opposite direction — only another
    confirmed completed-bar crossover has that authority (docs 2026-07-27
    KIS-parity fix)."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])  # real UP_RED flag at 13:57
    confirmed_now = start + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state()
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.last_confirmed_bar_ts = (start + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    svc = _svc_with_quote(
        df_1m, confirmed_now,
        {config.WATCH_SYMBOL: 140.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    entry_result = run_once(broker=broker, market_data=svc, state=state, now=confirmed_now)
    assert entry_result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL

    svc._quotes[config.WATCH_SYMBOL] = QuoteSnapshot(
        config.WATCH_SYMBOL, 60.0, datetime.now(KST), 0.0, "test", None,
    )
    result = run_once(broker=broker, market_data=svc, state=state, now=confirmed_now + timedelta(seconds=5))

    assert result.actions == []
    assert state.position.symbol == config.LONG_SYMBOL  # untouched by the candidate
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]


def test_confirmed_dispatch_within_5_seconds_of_detection():
    """docs §6 5초 재현 검증: a genuine completed-bar crossover reaches the
    order executor within SIGNAL_TO_ORDER_REQUEST_MAX_SEC (5s) of detection."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    now = start + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state()
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.last_confirmed_bar_ts = (start + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    svc = _svc_with_quote(df_1m, now, {config.WATCH_SYMBOL: 140.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    detected = datetime.fromisoformat(result.signal_detected_at)
    executor_called = datetime.fromisoformat(result.signal_dispatch_trace["executor_called_at"])
    assert (executor_called - detected).total_seconds() <= config.SIGNAL_TO_ORDER_REQUEST_MAX_SEC


def test_confirmed_flag_time_and_signal_id_use_bar_start_not_bar_end():
    """docs §1: 3m bars are 09:00-anchored, left-labeled — the 13:57-14:00 bar's
    flag_time/signal_id must show 13:57 (bar_start), never 14:00 (bar_end).
    Only evaluated_at (detected_at)/order_requested_at may fall at/after the
    bar's own completion instant (14:00)."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    now = start + timedelta(minutes=3 * 100, seconds=5)  # 14:00:05
    bar_start = start + timedelta(minutes=3 * 99)  # 13:57:00
    bar_end = bar_start + timedelta(minutes=3)  # 14:00:00
    state = _fresh_state()
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.last_confirmed_bar_ts = (start + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    svc = _svc_with_quote(df_1m, now, {config.WATCH_SYMBOL: 140.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    row = ledger.load_signal_ledger()[0]
    assert row["completed_bar_at"] == bar_start.strftime("%H%M%S") == "135700"
    assert row["signal_id"] == f"{bar_start:%Y%m%d}_{bar_start:%H%M%S}_UP_RED"
    assert "135700" in row["signal_id"]
    detected_at = datetime.fromisoformat(row["detected_at"])
    order_requested_at = datetime.fromisoformat(row["order_requested_at"])
    assert detected_at >= bar_end
    assert order_requested_at >= bar_end


def test_history_gap_blocks_confirmed_signal_until_backfilled():
    """docs §4: a completed 3m bar missing one of its 3 constituent 1-minute
    bars must never be treated as confirmed — HISTORY_GAP blocks that bar's
    crossover/MAJOR-filter/order evaluation (0 orders, 0 ledger rows) without
    advancing last_confirmed_bar_ts, so the SAME bar dispatches normally once
    the gap is backfilled by a later incremental merge."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    full_1m = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    gap_minute = start + timedelta(minutes=3 * 99 + 1)  # middle minute of the new 13:57-14:00 bar
    gapped_1m = full_1m[full_1m["datetime"] != gap_minute].reset_index(drop=True)
    now = start + timedelta(minutes=3 * 100, seconds=5)  # 14:00:05
    baseline_ts = (start + timedelta(minutes=3 * 98)).isoformat()
    state = _fresh_state()
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.last_confirmed_bar_ts = baseline_ts
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    svc = _svc_with_quote(
        gapped_1m, now, {config.WATCH_SYMBOL: 140.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == []
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []
    assert state.order_block_reason == "HISTORY_GAP"
    assert state.last_confirmed_bar_ts == baseline_ts  # not advanced — same bar retried later

    # Gap backfilled by a later incremental merge (e.g. market_data's own
    # history-updater catching up) -> the SAME bar now dispatches normally.
    svc._df_1m = full_1m
    result2 = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))

    assert result2.actions == ["ENTRY:UP_RED"]
    assert len(ledger.load_signal_ledger()) == 1
    # 2026-08-20 fix (real incident: dashboard kept showing "HISTORY_GAP" /
    # bootstrap_status FAILED forever after a real gap had already been
    # backfilled -- order_block_reason was set to HISTORY_GAP above but
    # nothing ever cleared it back). Must go back to None once the SAME
    # check comes back clean, not linger from the earlier gapped tick.
    assert state.order_block_reason is None


def test_compute_today_signal_overview_classifies_by_session_started_at():
    """docs §3: the recomputed today-overview never touches order_executor/
    major_flag_filter — it only classifies each bar as HISTORICAL_REPLAY_ONLY
    (bar closed before this Worker session started) or LIVE_CONFIRMED (bar
    closed at/after session start), purely for the stats panel."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    now = start + timedelta(minutes=3 * 100, seconds=5)  # 14:00:05
    bar_start = start + timedelta(minutes=3 * 99)  # 13:57:00
    bar_end = bar_start + timedelta(minutes=3)  # 14:00:00

    overview_hist = worker.compute_today_signal_overview(
        df_1m, now=now, session_started_at=(bar_end + timedelta(minutes=1)).isoformat(),
    )
    matching = [r for r in overview_hist if r["bar_start_at"] == bar_start.isoformat()]
    assert len(matching) == 1
    assert matching[0]["origin"] == "HISTORICAL_REPLAY_ONLY"
    assert matching[0]["direction"] == "UP_RED"
    assert "135700" in matching[0]["signal_id"]

    overview_live = worker.compute_today_signal_overview(
        df_1m, now=now, session_started_at=(bar_start - timedelta(minutes=1)).isoformat(),
    )
    matching_live = [r for r in overview_live if r["bar_start_at"] == bar_start.isoformat()]
    assert len(matching_live) == 1
    assert matching_live[0]["origin"] == "LIVE_CONFIRMED"


def test_compute_today_signal_overview_skips_first_today_bar_as_baseline(monkeypatch):
    today_start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(today_start, [100.0, 100.0])
    now = today_start + timedelta(minutes=6, seconds=5)

    def fake_calculate_macd(window):
        bar_dt = window.iloc[-1]["datetime"]
        return MacdSnapshot(
            bar_dt=bar_dt,
            macd=-10.0,
            signal=-5.0,
            hist=-5.0,
            hist_last3=(-3.0, -4.0, -5.0),
            completed_3m_count=len(window),
            previous_diff=-4.0,
            current_diff=-5.0,
            previous_macd=-9.0,
            previous_signal=-4.0,
        )

    monkeypatch.setattr(worker, "calculate_macd", fake_calculate_macd)

    overview = worker.compute_today_signal_overview(
        df_1m, now=now, session_started_at=today_start.isoformat(),
    )

    assert overview == []


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

    # 2026-08-28 fix: the broker-side qty loss (10 -> 0) that reconcile just
    # silently adopted above must now leave a real trace in BOTH ledgers —
    # not just the runtime-state skip. FakeBroker's quote for LONG_SYMBOL
    # equals the position's own entry_price (15_000.0), so net_pnl should be
    # a small negative (fee/tax only), never the misleading 0.0 a plain
    # BROKER_DIRECT stub would have recorded.
    exec_rows = ledger.load_execution_ledger()
    sell_rows = [r for r in exec_rows if r["side"] == "SELL"]
    assert len(sell_rows) == 1
    sell_row = sell_rows[0]
    assert sell_row["symbol"] == config.LONG_SYMBOL
    assert sell_row["executed_qty"] == "10"
    assert sell_row["source"] == "RECONCILE_BACKFILL"
    assert sell_row["exit_reason"] == worker.RECOVERED_TO_FLAT
    assert float(sell_row["net_pnl"]) < 0.0

    signal_rows = ledger.load_signal_ledger()
    assert any(r["signal_type"] == "RECONCILE_DISCOVERED_SELL" for r in signal_rows)


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


def _macd_snap(bar_dt, previous_diff, current_diff, macd=0.0, signal=0.0):
    return MacdSnapshot(
        bar_dt=bar_dt, macd=macd, signal=signal, hist=current_diff,
        hist_last3=(previous_diff, previous_diff, current_diff), completed_3m_count=200,
        previous_diff=previous_diff, current_diff=current_diff,
        relation="ABOVE" if current_diff > 0 else ("BELOW" if current_diff < 0 else "EQUAL"),
    )


def test_day_rollover_does_not_reset_last_detected_direction():
    """2026-08-20 NXT fix (조건 3 핵심): last_detected_direction을 자정마다
    reset하던 옛 동작을 제거했다 — NXT 포함 연속 시계열에서는 날짜가 바뀐다고
    MACD 상태가 실제로 끊기지 않으므로, 08:45 BLUE가 09:00에도 "유지"로
    남아야 한다(새 이벤트 아님)."""
    state = _fresh_state()
    state.session_date = "20260819"
    state.last_detected_direction = Direction.DOWN_BLUE

    worker._apply_day_rollover(state, datetime(2026, 8, 20, 8, 0, tzinfo=KST))

    assert state.session_date == "20260820"
    assert state.last_detected_direction == Direction.DOWN_BLUE


def test_day_rollover_does_not_cause_duplicate_flag_on_a_borderline_zero_diff_bar():
    """조건 3 회귀 테스트: 날짜가 바뀌는 시점에 하필 직전 확정봉의 diff가
    정확히 0.0(경계값)이었다가 그대로 같은 방향(BLUE)으로 이어지는 경우,
    last_detected_direction이 rollover에 의해 None으로 리셋됐었다면
    evaluate_macd_crossover의 "동일 방향 반복 억제"가 무력화되어 이미 발생한
    BLUE 이벤트가 자정 이후 첫 봉에서 또 한 번 "새 이벤트"로 잘못 발행됐을
    것이다. 리셋을 제거한 이후에는 이 봉이 HOLD로 남아야 한다."""
    state = _fresh_state()

    # Day 1: a genuine DOWN_BLUE crossover (bar A), then a same-direction bar
    # (bar B) whose OWN previous_diff happens to land exactly on 0.0.
    bar_a = datetime(2026, 8, 19, 15, 0, tzinfo=KST)
    snap_a = _macd_snap(bar_a, previous_diff=2.0, current_diff=-1.0)
    direction_a = worker._advance_confirmed_primary(state, snap_a, now=bar_a + timedelta(minutes=3))
    assert direction_a == Direction.DOWN_BLUE
    assert state.last_detected_direction == Direction.DOWN_BLUE

    bar_b = datetime(2026, 8, 19, 19, 57, tzinfo=KST)
    snap_b = _macd_snap(bar_b, previous_diff=-1.0, current_diff=-4.0)
    direction_b = worker._advance_confirmed_primary(state, snap_b, now=bar_b + timedelta(minutes=3))
    assert direction_b == Direction.HOLD

    worker._apply_day_rollover(state, datetime(2026, 8, 20, 8, 0, tzinfo=KST))
    assert state.last_detected_direction == Direction.DOWN_BLUE  # NOT reset to None

    # Day 2's first confirmed bar: previous_diff lands exactly at the zero
    # boundary (>=0 is True) with current_diff negative -- this matches
    # evaluate_macd_crossover's raw DOWN_BLUE trigger condition even though
    # the state has already been BLUE since bar_a. Only the "suppress a
    # repeat of last_detected_direction" check stops this from firing as a
    # brand-new (duplicate) flag.
    bar_c = datetime(2026, 8, 20, 8, 3, tzinfo=KST)
    snap_c = _macd_snap(bar_c, previous_diff=0.0, current_diff=-2.0)
    direction_c = worker._advance_confirmed_primary(state, snap_c, now=bar_c + timedelta(minutes=3))

    assert direction_c == Direction.HOLD  # must NOT re-fire as a duplicate DOWN_BLUE
    assert state.last_detected_direction == Direction.DOWN_BLUE


def test_cold_start_replay_matches_a_continuously_running_instance(ready_market_data):
    """조건 4 회귀 테스트: Worker가 처음부터 계속 켜져 있던 경우와, 같은
    시장 데이터를 두고 그 시각에 막 재시작(state.json 유실 등 진짜 cold
    start)한 경우가 last_detected_direction/primary_relation에서 정확히
    같은 결과를 내야 한다 -- 재시작 전후 MACD/Signal/플래그가 완전히
    동일해야 한다는 요구사항의 핵심."""
    svc, now0 = ready_market_data
    df_1m = svc.get_history_df()
    later_now = now0 + timedelta(minutes=3 * 40)  # well past the sine wave's first reversal

    # Ground truth: replay every completed 3m bar up to `later_now` directly
    # via the same production primitives initialize_strategy_session's cold-
    # start branch now uses internally.
    bars_3m = resample_completed_3m(df_1m, now=later_now)
    expected_direction = None
    expected_flag_snap = None
    for pos in range(len(bars_3m)):
        snap = calculate_macd(bars_3m.iloc[: pos + 1])
        if snap is None:
            continue
        direction = evaluate_macd_crossover(snap, expected_direction)
        if direction != Direction.HOLD:
            expected_direction = direction
            expected_flag_snap = snap
    assert expected_direction is not None, "fixture must contain at least one real crossover"

    cold_state = _fresh_state()  # no last_confirmed_bar_ts -> true cold start
    worker.initialize_strategy_session(cold_state, svc, now=later_now)

    assert cold_state.last_detected_direction == expected_direction
    assert cold_state.latest_primary_flag == expected_direction
    assert cold_state.latest_primary_signal_id == make_signal_id(expected_flag_snap.bar_dt, expected_direction)


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
    # Stop Loss is evaluated from the completed 3-minute ETF bar close onward,
    # excluding the entry/execution bar (docs 2026-08-02 Exit Rule) -- seed the
    # tracker as if entry happened bars ago and the immediately-prior bar
    # already completed at the loss price, so this tick's bar rollover fires.
    bar_start, _ = forming_bar_window(now)
    state.stop_loss_bar_symbol = config.LONG_SYMBOL
    state.stop_loss_entry_bar_ts = (bar_start - timedelta(minutes=6)).isoformat()
    state.stop_loss_bar_ts = (bar_start - timedelta(minutes=3)).isoformat()
    state.stop_loss_bar_close = 14_000.0

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("STOP_LOSS:") for a in result.actions)
    assert state.position is None
    assert broker.get_position(config.LONG_SYMBOL) is None
    rows = ledger.load_execution_ledger()
    assert rows[-1]["exit_reason"] == config.EXIT_STOP_LOSS


def test_stop_loss_excludes_entry_bar_then_fires_on_next_completed_bar_close():
    """docs 2026-08-02 Exit Rule: 3-Minute Confirmed Bars -- a deep loss that
    happens WITHIN the entry/execution 3-minute bar must not stop out; Stop
    Loss only becomes eligible once the NEXT 3-minute bar has fully closed.
    Three ticks: T0 (inside the execution bar, deep loss quote -> no exit),
    T0+3min (the execution bar just completed -- still excluded -> no exit),
    T0+6min (the bar AFTER the execution bar just completed at the loss
    price -- first eligible check -> STOP_LOSS)."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    t0 = prior_day + timedelta(minutes=300, seconds=5)
    bar1_start, _ = forming_bar_window(t0)
    assert bar1_start == t0.replace(second=0, microsecond=0)

    quote_prices = {config.LONG_SYMBOL: 14_000.0}  # -6.67% raw vs 15,000 entry -- well past -1.5%
    svc = _svc_with_quote(df_1m, t0, quote_prices)

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, 14_000.0)
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)
    # Seed the tracker exactly as _apply_switch_outcome would right after a
    # real entry fill at t0: the execution bar is bar1 itself.
    state.stop_loss_bar_symbol = config.LONG_SYMBOL
    state.stop_loss_entry_bar_ts = bar1_start.isoformat()
    state.stop_loss_bar_ts = bar1_start.isoformat()
    state.stop_loss_bar_close = 15_000.0

    # Tick 1: still inside bar1 (the execution bar) -- no exit despite the
    # deep-loss quote.
    result1 = run_once(broker=broker, market_data=svc, state=state, now=t0)
    assert not any(a.startswith("STOP_LOSS:") for a in result1.actions)
    assert state.position is not None

    # Tick 2: bar1 (execution bar) just completed -- still excluded.
    t1 = t0 + timedelta(minutes=3)
    result2 = run_once(broker=broker, market_data=svc, state=state, now=t1)
    assert not any(a.startswith("STOP_LOSS:") for a in result2.actions)
    assert state.position is not None

    # Tick 3: bar2 (the bar AFTER the execution bar) just completed at the
    # loss price -- first eligible check -> Stop Loss fires.
    t2 = t0 + timedelta(minutes=6)
    result3 = run_once(broker=broker, market_data=svc, state=state, now=t2)
    assert any(a.startswith("STOP_LOSS:") for a in result3.actions)
    assert state.position is None
    rows = ledger.load_execution_ledger()
    assert rows[-1]["exit_reason"] == config.EXIT_STOP_LOSS


def test_profit_lock_tracks_giveback_without_exit():
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

    assert not any(a.startswith("PROFIT_LOCK:") for a in result.actions)
    assert state.position is not None
    assert state.profit_lock_active is True


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


def test_concurrent_start_calls_never_spawn_two_ticking_threads():
    """2026-09-03 real incident: Macd2Worker.start()'s check-then-launch was
    unguarded -- two threads calling start() at nearly the same moment
    could both pass the is_alive() check and each spawn their OWN daemon
    thread, with self._thread pointing at only one of them (the other
    becomes a permanently orphaned second ticking loop, silently
    corrupting shared state/ledger writes with no coordination -- real
    evidence: two ledger rows ~7 seconds apart with different worker_
    instance_id, one on 24-minutes-stale bar data). Fires many concurrent
    start() calls via a thread pool to reliably reproduce the race window
    and asserts only ONE thread object/lease survives."""
    import threading as _threading

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

    threads = [_threading.Thread(target=w.start) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    try:
        time_module.sleep(0.2)
        assert w.is_alive() is True
        started_thread = w._thread
        # A second, later call must still be a no-op against the survivor.
        w.start()
        assert w._thread is started_thread
    finally:
        w.stop(join_timeout=5.0)


def test_superseded_worker_stops_ticking_on_next_loop_iteration():
    """2026-09-03 real incident fix: once a NEWER instance claims the shared
    worker lease file, an OLDER still-running instance must detect this on
    its very next loop iteration and stop permanently -- never continue
    load-mutate-saving the shared state/ledger files."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}))
    svc.bootstrap(now=prior_day + timedelta(minutes=300, seconds=5))
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    holder = {"state": _fresh_state()}
    w = Macd2Worker(
        broker=broker, market_data=svc,
        get_state=lambda: holder["state"], save_state=lambda s: holder.__setitem__("state", s),
        tick_interval_sec=0.05,
    )

    w.start()
    try:
        time_module.sleep(0.2)
        assert w.is_alive() is True
        # A "newer" instance claims the lease out from under this one.
        worker._claim_worker_lease("some-other-newer-instance-id")
        time_module.sleep(0.3)
        assert w.is_alive() is False, "the superseded instance must stop ticking, not keep running"
        stats = w.tick_stats()
        assert "SUPERSEDED_BY_NEWER_WORKER_INSTANCE" in str(stats.get("last_exception"))
    finally:
        w.stop(join_timeout=5.0)


def test_restart_catchup_recovers_a_reversal_missed_inside_a_multi_bar_gap():
    """2026-08-04 fix: a restart that occurs after MULTIPLE bars have already
    formed since the last live tick (not just one) used to lose a reversal
    that happened on an EARLIER bar within that gap, not the newest one —
    initialize_strategy_session's catch-up walk correctly recorded
    latest_primary_flag/last_detected_direction for it, but never gave it an
    actual order-dispatch chance (the crossover itself is bar-local and can
    never re-fire on a later bar), silently leaving the position on the
    wrong side of the market indefinitely. Verified crossovers for this
    close sequence (via calculate_macd bar-by-bar): bar99=DOWN_BLUE,
    bar102=UP_RED, bar105=DOWN_BLUE."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    closes = [100.0] * 99 + [92.0, 96.0, 103.0, 104.0, 103.0, 98.0, 90.0, 85.0, 84.0]
    df_1m_full = _1m_from_3m_closes(start, closes)
    bar99_dt = start + timedelta(minutes=3 * 99)

    state = _fresh_state()

    # A genuine first start of the day, baselined right before any
    # crossover has happened yet (Case A).
    df_1m_at_start = df_1m_full[df_1m_full["datetime"] < bar99_dt]
    svc0 = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_start, {}))
    svc0.bootstrap(now=bar99_dt)
    worker.initialize_strategy_session(state, svc0, now=bar99_dt)

    # A genuine LIVE tick (not a restart) evaluates bar99 normally.
    quote_prices = {config.WATCH_SYMBOL: 92.0, config.LONG_SYMBOL: 9_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=10_000_000.0, quotes=quote_prices)
    df_1m_bar99 = df_1m_full[df_1m_full["datetime"] < bar99_dt + timedelta(minutes=3)]
    svc1 = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m_bar99, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    tick1_now = bar99_dt + timedelta(minutes=3, seconds=5)
    svc1.bootstrap(now=tick1_now)
    svc1.refresh_quotes()
    result1 = run_once(broker=broker, market_data=svc1, state=state, now=tick1_now)
    assert result1.actions == ["ENTRY:DOWN_BLUE"]
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL

    # The process now dies for an EXTENDED outage spanning MULTIPLE new
    # bars (100, 101, 102-UP_RED, 103) before it ever gets another live
    # tick — data available at restart time goes up through bar 103.
    bar103_end = start + timedelta(minutes=3 * 104)
    df_1m_at_restart = df_1m_full[df_1m_full["datetime"] < bar103_end]
    svc2 = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    restart_now = bar103_end + timedelta(seconds=5)
    svc2.bootstrap(now=restart_now)
    worker.initialize_strategy_session(state, svc2, now=restart_now)

    # The walk must have found bar102's UP_RED and queued a correction —
    # the position hasn't changed YET (that's the next tick's job).
    assert state.last_detected_direction == Direction.UP_RED
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert state.pending_signal is not None and state.pending_signal.get("direction") == "UP_RED"

    # The very next tick after restart must consult that pending signal and
    # actually correct the position to LONG — the whole point of this test.
    svc2.refresh_quotes()
    result2 = run_once(broker=broker, market_data=svc2, state=state, now=restart_now)
    assert result2.actions == ["OPPOSITE_SIGNAL:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL


def test_restart_with_fully_lost_state_still_catches_up_when_today_already_has_bars():
    """2026-08-05 fix: a same-day restart whose PERSISTED state.json was lost
    entirely (last_confirmed_bar_ts empty -- e.g. a Render redeploy/disk
    hiccup resetting data/state/macd2_runtime.json -- not a genuine brand-new
    trading day) used to be indistinguishable from a true first-ever start
    today, silently swallowing whichever bar was newest at restart time as a
    no-dispatch baseline (2026-08-05 real incident: an already-held INVERSE
    position was never switched to LONG on a confirmed UP_RED mid-afternoon).
    Once today already has more than one completed bar, initialize_strategy_
    session must treat this the same as an ordinary same-day resume (replay
    from bar 0) instead of a cold start."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    closes = [100.0] * 99 + [92.0, 96.0, 103.0, 104.0, 103.0, 98.0, 90.0, 85.0, 84.0]
    df_1m_full = _1m_from_3m_closes(start, closes)

    # Simulate: the Worker was genuinely running earlier today (already held
    # an INVERSE position from a real DOWN_BLUE entry), but its entire
    # persisted state was then lost -- last_confirmed_bar_ts is empty even
    # though a real position is still held.
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0)
    assert state.last_confirmed_bar_ts is None

    # Data available at "restart" time goes up through bar103 (right after
    # bar102's UP_RED crossover) -- several bars already completed today
    # (len(today_indices) > 1), so this can never be a fresh 09:00 cold start.
    bar103_end = start + timedelta(minutes=3 * 104)
    df_1m_at_restart = df_1m_full[df_1m_full["datetime"] < bar103_end]
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}))
    restart_now = bar103_end + timedelta(seconds=5)
    svc.bootstrap(now=restart_now)

    worker.initialize_strategy_session(state, svc, now=restart_now)

    # bar102's UP_RED must be recovered from the catch-up walk (not silently
    # swallowed as a baseline) and queued as a pending correction, since it
    # conflicts with the still-held INVERSE position.
    assert state.last_detected_direction == Direction.UP_RED
    assert state.pending_signal is not None
    assert state.pending_signal.get("direction") == "UP_RED"
    assert state.pending_signal.get("reason") == "RESTART_CATCH_UP_MULTI_BAR_GAP"

    # 2026-08-05 fix: this same-day-restart-with-lost-state detection must
    # also flag that user toggles (major_filter_enabled etc.) may have
    # silently reverted to their config defaults, so the UI can warn.
    assert state.possible_toggle_reset_at is not None
    assert state.possible_toggle_reset_at == restart_now.isoformat()

    # The very next live tick must consult that pending signal and actually
    # switch the position to LONG.
    quote_prices = {config.WATCH_SYMBOL: 92.0, config.LONG_SYMBOL: 9_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=10_000_000.0, quotes=quote_prices)
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed")
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=restart_now)
    svc.refresh_quotes()
    result = run_once(broker=broker, market_data=svc, state=state, now=restart_now)
    assert result.actions == ["OPPOSITE_SIGNAL:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL


def test_initialize_strategy_session_records_premarket_catchup_flag_to_ledger():
    """2026-08-20 fix (사용자 요청 — 신호 원장에 프리마켓 08:00~09:00 크로스오버도
    표시): run_once()'s own live tick already records a confirmed flag
    detected BEFORE config.SESSION_OPEN to the signal ledger (block_reason=
    BEFORE_SESSION_OPEN, no order ever placed). But a premarket flag that
    completed on an EARLIER bar than the Worker's most recent (re)start —
    discovered only via initialize_strategy_session's catch-up walk — used to
    update state.latest_primary_flag/last_detected_direction but never touch
    the ledger at all, so it silently never appeared in "신호 원장" even
    though an equivalent LIVE premarket flag would have. This must show up
    the same way: BLOCKED, BEFORE_SESSION_OPEN, no order."""
    start = datetime(2026, 7, 24, 4, 0, tzinfo=KST)
    closes = [100.0] * 99 + [92.0, 96.0, 103.0, 104.0, 103.0, 98.0, 90.0, 85.0, 84.0]
    df_1m_full = _1m_from_3m_closes(start, closes)
    bar99_dt = start + timedelta(minutes=3 * 99)
    assert bar99_dt.time() < config.SESSION_OPEN  # sanity: this bar really is premarket

    state = _fresh_state()
    # initialize_strategy_session's catch-up walk deliberately stops ONE bar
    # short of the newest available bar (leaving it for the Worker's very
    # first live run_once() tick) -- include a couple of bars AFTER bar99 so
    # bar99 itself falls inside the walked range instead of being that
    # left-over newest bar.
    restart_now = start + timedelta(minutes=3 * 102, seconds=5)
    df_1m_at_restart = df_1m_full[df_1m_full["datetime"] < start + timedelta(minutes=3 * 102)]
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}))
    svc.bootstrap(now=restart_now)

    worker.initialize_strategy_session(state, svc, now=restart_now)

    rows = ledger.load_signal_ledger()
    premarket_rows = [r for r in rows if r.get("completed_bar_at") == bar99_dt.strftime("%H%M%S")]
    assert len(premarket_rows) == 1
    assert premarket_rows[0]["direction"] == "DOWN_BLUE"
    assert premarket_rows[0]["block_reason"] == "BEFORE_SESSION_OPEN"
    assert premarket_rows[0]["order_result"] == "BLOCKED"

    # A second (re)start replaying the SAME bar must not duplicate the row —
    # ledger.append_signal's own signal_id dedup makes this a safe no-op.
    state2 = _fresh_state()
    svc2 = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}))
    svc2.bootstrap(now=restart_now)
    worker.initialize_strategy_session(state2, svc2, now=restart_now)
    rows_after_second_restart = ledger.load_signal_ledger()
    assert len([r for r in rows_after_second_restart if r.get("completed_bar_at") == bar99_dt.strftime("%H%M%S")]) == 1


def test_second_restart_does_not_discard_a_still_pending_catchup_signal():
    """2026-08-06 fix: a THIRD restart back-to-back used to unconditionally
    wipe state.pending_signal to None every single call, regardless of
    resuming_today -- on a host that restarts the whole process every minute
    or two (2026-08-06 real incident: 6+ distinct worker_instance_id values
    inside 30 minutes), a genuine RESTART_CATCH_UP_MULTI_BAR_GAP pending
    signal set by one restart could be silently discarded by the VERY NEXT
    restart before any live tick ever got a chance to act on it -- and since
    the catch-up walk also marks every bar it visits as already-evaluated,
    that flag could never be re-detected either (a filter-approved 12:03
    DOWN_BLUE entry never even reached order_executor). This test starts
    FLAT (an INITIAL entry, not a held-position REVERSAL) and fires TWO
    restarts back to back with no new market data between them and more than
    config.PENDING_SIGNAL_RETRY_SEC (30s) of wall-clock time apart, so the
    second restart's own catch-up walk finds nothing new -- the only way the
    signal survives is by not being wiped, with its detected_at refreshed to
    the second restart's own `now` (not restart 1's stale one) so the retry
    window is judged fairly from this restart's live tick."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    closes = [100.0] * 99 + [92.0, 96.0, 103.0, 104.0, 103.0, 98.0, 90.0, 85.0, 84.0]
    df_1m_full = _1m_from_3m_closes(start, closes)

    state = _fresh_state()  # flat -- no position
    bar103_end = start + timedelta(minutes=3 * 104)
    df_1m_at_restart = df_1m_full[df_1m_full["datetime"] < bar103_end]
    svc1 = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}))
    restart1_now = bar103_end + timedelta(seconds=5)
    svc1.bootstrap(now=restart1_now)
    worker.initialize_strategy_session(state, svc1, now=restart1_now)

    assert state.pending_signal is not None
    assert state.pending_signal.get("direction") == "UP_RED"
    assert state.pending_signal.get("signal_type") == "INITIAL"
    assert state.pending_signal.get("detected_at") == restart1_now.isoformat()

    # Process dies again almost immediately, well past PENDING_SIGNAL_RETRY_
    # SEC (30s) later, with NO new market data -- restart 2's own catch-up
    # walk has nothing left to find (resume_from already sits past bar102).
    restart2_now = restart1_now + timedelta(seconds=90)
    assert 90 > config.PENDING_SIGNAL_RETRY_SEC
    quote_prices = {config.WATCH_SYMBOL: 100.0, config.LONG_SYMBOL: 9_000.0, config.INVERSE_SYMBOL: 10_000.0}
    svc2 = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc2.bootstrap(now=restart2_now)
    worker.initialize_strategy_session(state, svc2, now=restart2_now)

    assert state.pending_signal is not None  # must survive -- not wiped
    assert state.pending_signal.get("direction") == "UP_RED"
    assert state.pending_signal.get("signal_type") == "INITIAL"
    assert state.pending_signal.get("reason") == "RESTART_CATCH_UP_MULTI_BAR_GAP"
    assert state.pending_signal.get("detected_at") == restart2_now.isoformat()  # refreshed

    # And it must actually be actionable on the very next live tick.
    broker = FakeBroker(cash=10_000_000.0, quotes=quote_prices)
    svc2.refresh_quotes()
    result = run_once(broker=broker, market_data=svc2, state=state, now=restart2_now)
    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL


def test_restart_with_fully_lost_state_at_true_market_open_still_baselines_only():
    """The 2026-08-05 fix above must NOT fire for a genuine first bar of the
    day (len(today_indices) <= 1) -- that case still baselines silently with
    no catch-up, exactly as before."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    # Exactly ONE completed 3-minute bar exists today (09:00-09:03) -- a
    # genuine market-open cold start, not a restart deep into the session.
    df_1m_at_start = _1m_from_3m_closes(start, [100.0])
    call_now = start + timedelta(minutes=3, seconds=5)

    state = _fresh_state()
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_start, {}))
    svc.bootstrap(now=call_now)

    worker.initialize_strategy_session(state, svc, now=call_now)

    assert state.pending_signal is None
    assert state.last_detected_direction is None
    assert state.possible_toggle_reset_at is None


def test_day_rollover_clears_possible_toggle_reset_warning():
    state = _fresh_state()
    state.session_date = "20260804"
    state.possible_toggle_reset_at = "2026-08-04T13:30:00+09:00"

    worker._apply_day_rollover(state, datetime(2026, 8, 5, 9, 0, tzinfo=KST))

    assert state.possible_toggle_reset_at is None
