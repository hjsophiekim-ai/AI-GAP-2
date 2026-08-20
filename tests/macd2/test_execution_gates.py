from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, order_executor, worker
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot, QuoteSnapshot, SignalState
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_confirmed_macd_flag,
    evaluate_macd_crossover,
    make_signal_id,
    make_provisional_signal_id,
    PrimaryCrossoverResult,
    resample_completed_3m,
    signed_b_condition,
)
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _state() -> worker.RuntimeState:
    state = worker.RuntimeState()
    state.auto_trade_on = True
    return state


def _primed_state(baseline_bar_dt: datetime = datetime(2026, 7, 24, 8, 57, tzinfo=KST)) -> worker.RuntimeState:
    """A state that has already evaluated one earlier same-day completed
    bar, so the next NEW completed bar in a test is treated as a genuine
    same-day continuation rather than the (baseline-only) first bar this
    state has ever seen (docs 2026-07-27 KIS-parity fix,
    _advance_confirmed_primary). strategy_version/signal_rule must already
    match config here — run_once's own version-mismatch reset would
    otherwise wipe last_confirmed_bar_ts right back to None on the first
    tick (production states always get this from initialize_strategy_session
    before any tick runs; this test helper stands in for that)."""
    state = _state()
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.last_confirmed_bar_ts = baseline_bar_dt.isoformat()
    return state


def _snap(bar_dt: datetime, direction: Direction = Direction.UP_RED) -> MacdSnapshot:
    hist = (1.0, 2.0, 3.0) if direction == Direction.UP_RED else (-1.0, -2.0, -3.0)
    previous_diff = -1.0 if direction == Direction.UP_RED else 1.0
    current_diff = 1.0 if direction == Direction.UP_RED else -1.0
    if direction == Direction.HOLD:
        hist = (1.0, 2.0, 1.5)
        previous_diff = 1.0
        current_diff = 1.5
    return MacdSnapshot(
        bar_dt=bar_dt, macd=current_diff, signal=0.0, hist=hist[-1], hist_last3=hist,
        completed_3m_count=100, previous_diff=previous_diff, current_diff=current_diff,
        relation="ABOVE" if current_diff > 0 else "BELOW",
    )


def _svc_with_stale_symbol(stale_symbol: str, *, recovers_after: "int | None" = None, prices=None):
    """A MarketDataService whose fetch_quote errors for ``stale_symbol`` — for
    ``recovers_after`` forced-refresh calls, then returns a fresh price
    forever after (``None`` = never recovers). Other symbols are always
    fresh from construction. Used to exercise the 2026-07-27 QUOTE_STALE
    synchronous-retry fix without relying on wall-clock staleness."""
    prices = prices or {config.WATCH_SYMBOL: 100.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    call_count = {"n": 0}

    def fetch_quote(mode, symbol):
        if symbol == stale_symbol:
            call_count["n"] += 1
            if recovers_after is None or call_count["n"] <= recovers_after:
                return None, "STALE_TEST"
        return prices.get(symbol), None

    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a: (pd.DataFrame({"datetime": []}), {}),
        fetch_quote=fetch_quote,
    )
    fresh_at = datetime.now(KST)
    for symbol, price in prices.items():
        if symbol == stale_symbol:
            continue
        svc._quotes[symbol] = QuoteSnapshot(symbol, price, fresh_at, 0.0, "test")
    old = fresh_at - timedelta(seconds=999)
    svc._quotes[stale_symbol] = QuoteSnapshot(stale_symbol, prices.get(stale_symbol, 0.0), old, 999.0, "test")
    return svc


def _svc(prices=None):
    prices = prices or {config.WATCH_SYMBOL: 100.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a: (pd.DataFrame({"datetime": []}), {}),
        fetch_quote=lambda mode, symbol: (prices.get(symbol), None),
    )
    svc.refresh_quotes()
    return svc


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


def _history_svc(df_1m: pd.DataFrame, prices=None) -> MarketDataService:
    prices = prices or {config.WATCH_SYMBOL: 100.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    empty = pd.DataFrame({"datetime": [], "open": [], "high": [], "low": [], "close": [], "volume": []})
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_minute_candles_for_date=lambda *a: (empty, {}),
        fetch_quote=lambda mode, symbol: (prices.get(symbol), None),
    )
    svc.bootstrap(now=df_1m["datetime"].iloc[-1] + timedelta(minutes=1))
    svc.refresh_quotes()
    return svc


def _assert_latest_primary(df_1m: pd.DataFrame, now: datetime, direction: Direction) -> None:
    snap = calculate_macd(resample_completed_3m(df_1m, now=now))
    assert snap is not None
    assert evaluate_macd_crossover(snap, None) == direction


def _patch_snap(monkeypatch, snap: MacdSnapshot, svc: MarketDataService, now: datetime):
    monkeypatch.setattr(worker, "calculate_macd", lambda _bars: snap)
    # Keep the (otherwise-empty) 1m history "fresh" relative to the upcoming
    # `now` so the 2026-07-27 quote/history freshness gate (HISTORY_EMPTY/
    # HISTORY_STALE, worker._update_history_freshness_diag) doesn't block
    # these dispatch-focused tests, which monkeypatch calculate_macd
    # directly and never rely on real 1m row content.
    svc._df_1m = pd.DataFrame([{
        "datetime": now - timedelta(seconds=5),
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1,
    }])

    def fake_primary(*args, **kwargs):
        now_kw = kwargs.get("now")
        previous_direction = kwargs.get("previous_direction")
        if now_kw is not None:
            if snap.bar_dt.date() != now_kw.date() or now_kw < snap.bar_dt + timedelta(minutes=3):
                return PrimaryCrossoverResult(snap, Direction.HOLD, None)
        direction = evaluate_macd_crossover(snap, previous_direction)
        signal_id = make_provisional_signal_id(snap.bar_dt, direction) if direction != Direction.HOLD else None
        return PrimaryCrossoverResult(snap, direction, signal_id)
    monkeypatch.setattr(
        worker,
        "evaluate_primary_forming_crossover",
        fake_primary,
    )


def test_prior_day_last_up_red_with_no_today_bar_orders_zero(monkeypatch):
    now = datetime(2026, 7, 24, 9, 6, tzinfo=KST)
    svc = _svc()
    _patch_snap(monkeypatch, _snap(datetime(2026, 7, 23, 15, 27, tzinfo=KST), Direction.UP_RED), svc, now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    state = _state()

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert broker.orders == []
    assert result.actions == []
    assert ledger.load_signal_ledger() == []


def test_prior_day_last_down_blue_with_no_today_bar_orders_zero(monkeypatch):
    now = datetime(2026, 7, 24, 9, 6, tzinfo=KST)
    svc = _svc()
    _patch_snap(monkeypatch, _snap(datetime(2026, 7, 23, 15, 27, tzinfo=KST), Direction.DOWN_BLUE), svc, now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    state = _state()

    worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert broker.orders == []
    assert ledger.load_signal_ledger() == []


def test_first_completed_bar_of_new_day_with_genuine_crossover_now_dispatches(monkeypatch):
    """2026-08-18 fix: a day's first completed bar used to be forced to
    baseline-only (see git history), which silently swallowed a genuine
    overnight-gap-driven crossover — verified against a real KIS chart read
    the gate used to miss. The bar's own date (today) matches ``now``'s date
    and it has actually completed by ``now``, so it now dispatches like any
    other bar; only a bar that's still anchored to a PRIOR date (see
    test_prior_day_last_*_with_no_today_bar_orders_zero below) stays
    baseline-only."""
    state = _primed_state(baseline_bar_dt=datetime(2026, 7, 23, 15, 27, tzinfo=KST))
    state.session_date = "20260724"
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    first_bar = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = first_bar + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(first_bar, Direction.DOWN_BLUE), svc, now)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.INVERSE_SYMBOL)]
    assert [r["direction"] for r in ledger.load_signal_ledger()] == ["DOWN_BLUE"]
    assert state.last_confirmed_bar_ts == first_bar.isoformat()
    assert state.last_detected_direction == Direction.DOWN_BLUE


def test_before_first_completed_today_bar_orders_zero(monkeypatch):
    now = datetime(2026, 7, 24, 9, 2, tzinfo=KST)
    svc = _svc()
    _patch_snap(monkeypatch, _snap(datetime(2026, 7, 24, 9, 0, tzinfo=KST), Direction.UP_RED), svc, now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    worker.run_once(broker=broker, market_data=svc, state=_state(), now=now)

    assert broker.orders == []


def test_today_date_and_completed_bar_date_mismatch_creates_no_signal(monkeypatch):
    now = datetime(2026, 7, 24, 9, 30, tzinfo=KST)
    svc = _svc()
    _patch_snap(monkeypatch, _snap(datetime(2026, 1, 6, 15, 27, tzinfo=KST), Direction.UP_RED), svc, now)

    worker.run_once(
        broker=FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0}),
        market_data=svc, state=_state(), now=now,
    )

    assert ledger.load_signal_ledger() == []


def test_first_ever_evaluated_bar_with_genuine_crossover_now_dispatches(monkeypatch):
    """2026-08-18 fix (see test_first_completed_bar_of_new_day_with_genuine_
    crossover_now_dispatches above): the first completed bar this state has
    EVER evaluated is treated the same as any other same-day bar once its
    own date matches ``now``'s date and it has actually completed — a real
    crossover on it now dispatches instead of being forced to baseline-only
    HOLD."""
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    svc = _svc()
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    state = _state()

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]
    assert [r["direction"] for r in ledger.load_signal_ledger()] == ["UP_RED"]
    assert state.last_confirmed_bar_ts == bar_dt.isoformat()
    assert state.last_detected_direction == Direction.UP_RED


def test_five_continuous_up_red_condition_bars_create_one_red_flag(monkeypatch):
    state = _primed_state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    for i in range(5):
        bar_dt = datetime(2026, 7, 24, 9, 0 + 3 * i, tzinfo=KST)
        now = bar_dt + timedelta(minutes=3)
        _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)
        worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    rows = ledger.load_signal_ledger()
    assert [r["direction"] for r in rows] == ["UP_RED"]


def test_blocked_order_same_direction_next_bar_adds_no_flag(monkeypatch):
    """Target quote never recovers within the 3-attempt/15s synchronous
    QUOTE_STALE window (2026-07-27 fix) -> first bar's signal finalizes as
    MISSED_SIGNAL_QUOTE_STALE (one ledger row), never placing an order; the
    second (same-direction) bar is suppressed by the repeat-direction gate
    before it ever reaches the quote check, so no second row/order either."""
    state = _primed_state()
    svc = _svc_with_stale_symbol(config.LONG_SYMBOL)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    for minute in (0, 3):
        bar_dt = datetime(2026, 7, 24, 9, minute, tzinfo=KST)
        now = bar_dt + timedelta(minutes=3)
        _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)
        worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert len(ledger.load_signal_ledger()) == 1
    assert broker.orders == []


def test_up_down_up_counts_three_onsets(monkeypatch):
    state = _primed_state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    for minute, direction in ((0, Direction.UP_RED), (3, Direction.DOWN_BLUE), (6, Direction.UP_RED)):
        bar_dt = datetime(2026, 7, 24, 9, minute, tzinfo=KST)
        now = bar_dt + timedelta(minutes=3)
        _patch_snap(monkeypatch, _snap(bar_dt, direction), svc, now)
        worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert [r["direction"] for r in ledger.load_signal_ledger()] == ["UP_RED", "DOWN_BLUE", "UP_RED"]


def test_crossover_opposite_signal_sells_then_buys(monkeypatch):
    state = _primed_state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    first_bar = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    first_now = first_bar + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(first_bar, Direction.UP_RED), svc, first_now)
    worker.run_once(broker=broker, market_data=svc, state=state, now=first_now)

    second_bar = datetime(2026, 7, 24, 9, 3, tzinfo=KST)
    second_now = second_bar + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(second_bar, Direction.DOWN_BLUE), svc, second_now)
    worker.run_once(broker=broker, market_data=svc, state=state, now=second_now)

    assert [(o.side, o.symbol) for o in broker.orders] == [
        ("BUY", config.LONG_SYMBOL),
        ("SELL", config.LONG_SYMBOL),
        ("BUY", config.INVERSE_SYMBOL),
    ]
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert broker.get_position(config.INVERSE_SYMBOL).quantity > 0


def test_switch_sell_cleared_buy_not_requested_keeps_signal_pending(monkeypatch):
    state = _primed_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=20, avg_price=10_000.0)
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 20, "seed")
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)

    def fake_execute_signal(**kwargs):
        return order_executor.ExecutionOutcome(
            signal_id=kwargs["signal_id"],
            direction=kwargs["direction"],
            target_symbol=config.LONG_SYMBOL,
            final_state=SignalState.BLOCKED,
            block_reason=order_executor.BLOCK_INSUFFICIENT_QTY,
            sell_result=BrokerOrderResult(True, "SELL-1", config.INVERSE_SYMBOL, "SELL", 20, 20, 10_000.0, "OK"),
            sell_qty_after=0,
            timestamps={"sell_requested_at": now.isoformat(), "sell_confirmed_at": now.isoformat()},
        )

    monkeypatch.setattr(worker.order_executor, "execute_signal", fake_execute_signal)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["OPPOSITE_SIGNAL:UP_RED"]
    assert state.position is None
    assert state.pending_signal is not None
    assert state.pending_signal["signal_id"] == "20260724_090000_UP_RED"
    assert state.pending_signal["order_requested"] is False
    assert state.processed_signal_ids == []


def test_down_blue_crossover_flat_buys_inverse_once(monkeypatch):
    state = _primed_state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.DOWN_BLUE), svc, now)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert broker.orders[0].side == "BUY"
    assert broker.orders[0].symbol == config.INVERSE_SYMBOL
    assert result.signal_dispatch_trace["order_executor_called"] is True
    assert result.signal_dispatch_trace["position_reconcile_result"] == worker.MATCH_FLAT
    assert result.signal_dispatch_trace["quote_status"] == "READY"
    assert result.signal_dispatch_trace["target_quote_valid"] is True


def test_ten_same_direction_crossover_bars_create_one_flag_and_one_order(monkeypatch):
    state = _primed_state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    for i in range(10):
        bar_dt = datetime(2026, 7, 24, 9, 3 * i, tzinfo=KST)
        now = bar_dt + timedelta(minutes=3)
        _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)
        worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert [r["direction"] for r in ledger.load_signal_ledger()] == ["UP_RED"]
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]


def test_down_blue_ready_dispatches_executor_once(monkeypatch):
    state = _primed_state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.DOWN_BLUE), svc, now)
    calls = {"n": 0}
    original = worker.order_executor.execute_signal

    def wrapped_execute_signal(**kwargs):
        calls["n"] += 1
        return original(**kwargs)

    monkeypatch.setattr(worker.order_executor, "execute_signal", wrapped_execute_signal)

    worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert calls["n"] == 1
    assert broker.orders[0].symbol == config.INVERSE_SYMBOL


def test_executor_none_is_recorded_as_signal_not_dispatched(monkeypatch):
    state = _primed_state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.DOWN_BLUE), svc, now)
    monkeypatch.setattr(worker.order_executor, "execute_signal", lambda **kwargs: None)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.skipped == worker.SIGNAL_NOT_DISPATCHED
    assert state.order_block_reason == worker.SIGNAL_NOT_DISPATCHED
    assert result.signal_dispatch_trace["order_executor_called"] is True
    assert result.signal_dispatch_trace["final_block_reason"] == worker.SIGNAL_NOT_DISPATCHED
    assert ledger.load_signal_ledger()[0]["block_reason"] == worker.SIGNAL_NOT_DISPATCHED


def test_production_path_up_crossover_buys_long_once():
    """Real completed-bar data (not a live quote) drives the Primary
    crossover since the 2026-07-27 KIS-parity fix: 99 flat bars then a real
    price jump on the 100th bar (13:57) is what makes previous_diff<=0,
    current_diff>0 — the SAME confirmed MACD(12,26,9) KIS itself charts."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    now = start + timedelta(minutes=3 * 100, seconds=5)
    state = _primed_state(baseline_bar_dt=start + timedelta(minutes=3 * 98))
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    svc = _history_svc(df_1m, prices={config.WATCH_SYMBOL: 140.0, config.LONG_SYMBOL: 15_000.0})

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]
    assert state.latest_primary_signal_id == "20260724_135700_UP_RED"


def test_production_path_down_crossover_buys_inverse_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 99 + [60.0])
    now = start + timedelta(minutes=3 * 100, seconds=5)
    state = _primed_state(baseline_bar_dt=start + timedelta(minutes=3 * 98))
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    svc = _history_svc(df_1m, prices={config.WATCH_SYMBOL: 60.0, config.INVERSE_SYMBOL: 10_000.0})

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.INVERSE_SYMBOL)]
    assert result.signal_dispatch_trace["order_executor_called"] is True
    assert result.signal_dispatch_trace["position_reconcile_result"] == worker.MATCH_FLAT
    assert result.signal_dispatch_trace["quote_status"] == "READY"
    assert result.signal_dispatch_trace["target_quote_valid"] is True
    assert state.latest_primary_signal_id == "20260724_135700_DOWN_BLUE"


def test_production_path_uses_latest_confirmed_color_when_cache_jumps_ahead():
    """_advance_confirmed_primary (6a2fd07 known-good rule) evaluates ONLY the
    single latest completed bar each tick — it never re-scans intermediate
    bars the cache jumped past. Baseline is primed to an early, already-flat
    bar (12:51); by the time the cache has jumped ahead to the latest bar
    (13:03), a genuine previous_diff<=0 -> current_diff>0/<0 crossing must
    still exist AT that latest bar for it to fire — it is not retroactively
    applied from an earlier intermediate bar."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 81 + [60.0])
    now = start + timedelta(minutes=3 * 82)
    state = _primed_state(baseline_bar_dt=datetime(2026, 7, 24, 12, 51, tzinfo=KST))
    state.last_evaluated_bar_ts = datetime(2026, 7, 24, 12, 51, tzinfo=KST).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})

    result = worker.run_once(broker=broker, market_data=_history_svc(df_1m), state=state, now=now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert state.latest_primary_signal_id == "20260724_130300_DOWN_BLUE"
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.INVERSE_SYMBOL)]


def test_production_path_treats_post_baseline_1300_bar_as_new_signal():
    """Real completed-bar data: 81 flat bars (baseline at 12:57, index 79)
    then a real drop on the bar at 13:03 (index 81) — a bar strictly AFTER
    the established baseline must still fire as a genuine new signal."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 81 + [60.0])
    now = start + timedelta(minutes=3 * 82, seconds=5)
    state = _primed_state(baseline_bar_dt=datetime(2026, 7, 24, 12, 57, tzinfo=KST))
    state.last_evaluated_bar_ts = datetime(2026, 7, 24, 12, 57, tzinfo=KST).isoformat()
    state.session_baseline_bar_ts = state.last_evaluated_bar_ts
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    svc = _history_svc(df_1m, prices={config.WATCH_SYMBOL: 60.0, config.INVERSE_SYMBOL: 10_000.0})

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert state.latest_primary_signal_id == "20260724_130300_DOWN_BLUE"
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.INVERSE_SYMBOL)]


def test_production_path_signed_b_only_without_crossover_orders_zero():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 35 + [110.0, 120.0, 130.0])
    now = start + timedelta(minutes=3 * 38)
    snap = calculate_macd(resample_completed_3m(df_1m, now=now))
    assert snap is not None
    assert signed_b_condition(snap) == Direction.UP_RED
    assert evaluate_macd_crossover(snap, None) == Direction.HOLD
    state = _primed_state(baseline_bar_dt=snap.bar_dt - timedelta(minutes=3))
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.last_evaluated_bar_ts = snap.bar_dt.isoformat()
    state.last_detected_direction = Direction.UP_RED
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    result = worker.run_once(broker=broker, market_data=_history_svc(df_1m), state=state, now=now)

    assert result.actions == []
    assert broker.orders == []


def test_production_path_same_crossover_bar_twenty_ticks_orders_once():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    now = start + timedelta(minutes=3 * 100, seconds=5)
    state = _primed_state(baseline_bar_dt=start + timedelta(minutes=3 * 98))
    svc = _history_svc(df_1m, prices={config.WATCH_SYMBOL: 140.0, config.LONG_SYMBOL: 15_000.0})
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    for _ in range(20):
        worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]


def test_production_path_up_then_down_sells_to_zero_then_buys_inverse():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    up_df = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    down_df = _1m_from_3m_closes(start, [100.0] * 99 + [140.0, 140.0, 60.0])
    up_now = start + timedelta(minutes=3 * 100, seconds=5)
    down_now = start + timedelta(minutes=3 * 102, seconds=5)
    state = _primed_state(baseline_bar_dt=start + timedelta(minutes=3 * 98))
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    up_svc = _history_svc(up_df, prices={config.WATCH_SYMBOL: 140.0, config.LONG_SYMBOL: 15_000.0})
    worker.run_once(broker=broker, market_data=up_svc, state=state, now=up_now)

    down_svc = _history_svc(down_df, prices={config.WATCH_SYMBOL: 60.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    result = worker.run_once(broker=broker, market_data=down_svc, state=state, now=down_now)

    assert result.actions == ["OPPOSITE_SIGNAL:DOWN_BLUE"]
    assert [(o.side, o.symbol) for o in broker.orders] == [
        ("BUY", config.LONG_SYMBOL),
        ("SELL", config.LONG_SYMBOL),
        ("BUY", config.INVERSE_SYMBOL),
    ]
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert broker.get_position(config.INVERSE_SYMBOL).quantity > 0


def test_opposite_flag_sells_but_does_not_reenter_when_order_gate_blocks_switch():
    """2026-08-06 fix: a confirmed opposite color flag must still SELL the
    already-held, now-wrong-direction position even when a non-filter order
    gate (here: WATCH_SYMBOL quote/history mismatch) blocks the re-entry leg
    -- it must never freeze the whole reversal into doing nothing at all (the
    2026-08-06 real incident this reproduces: a confirmed DOWN_BLUE while
    holding a position produced zero order attempts and the position sat
    losing money for the rest of the session). It must still never re-enter
    the new direction under the same data-quality doubt."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _1m_from_3m_closes(start, [100.0] * 99 + [140.0])
    now = start + timedelta(minutes=3 * 100, seconds=5)
    state = _primed_state(baseline_bar_dt=start + timedelta(minutes=3 * 98))
    state.last_detected_direction = Direction.DOWN_BLUE
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-inverse")
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0)
    svc = _history_svc(
        df_1m,
        prices={
            config.WATCH_SYMBOL: 1_000.0,  # Deliberate quote/history mismatch: blocks re-entry only.
            config.LONG_SYMBOL: 15_000.0,
            config.INVERSE_SYMBOL: 10_000.0,
        },
    )

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["OPPOSITE_SIGNAL_SELL_ONLY:UP_RED"]
    assert [(o.side, o.symbol) for o in broker.orders] == [
        ("BUY", config.INVERSE_SYMBOL), ("SELL", config.INVERSE_SYMBOL),
    ]
    assert state.position is None  # sold out -- never re-entered LONG_SYMBOL
    assert state.latest_primary_signal_id == "20260724_135700_UP_RED"
    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[0]["signal_id"] == "20260724_135700_UP_RED"
    assert rows[0]["signal_type"] == "REVERSAL"
    assert rows[0]["direction"] == "UP_RED"
    assert rows[0]["order_result"] == "SELL_EXECUTED_ENTRY_FILTERED"
    assert rows[0]["block_reason"] == "QUOTE_HISTORY_PRICE_MISMATCH"


def test_quote_age_27_seconds_is_stale_not_ready():
    svc = _svc()
    old = datetime.now(KST) - timedelta(seconds=27)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, 15_000.0, old, 27.0, "test")

    assert svc.quote_statuses()[config.LONG_SYMBOL] == "STALE"
    assert svc.quote_status() != "READY"


def test_target_quote_stale_waiting_and_no_order(monkeypatch):
    """2026-07-27 fix: QUOTE_STALE is resolved synchronously within this one
    call (force-refresh + retry up to 3x/15s) — if the target quote never
    recovers, the signal finalizes as MISSED_SIGNAL_QUOTE_STALE with no
    lingering pending_signal (nothing left for a later tick to dispatch
    late) and no order/execution row."""
    state = _primed_state()
    svc = _svc_with_stale_symbol(config.LONG_SYMBOL)
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)

    result = worker.run_once(
        broker=FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0}),
        market_data=svc, state=state, now=now,
    )

    assert result.skipped == config.MISSED_SIGNAL_QUOTE_STALE
    assert state.pending_signal is None
    assert ledger.load_execution_ledger() == []
    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[0]["signal_id"] == "20260724_090000_UP_RED"
    assert rows[0]["block_reason"] == config.MISSED_SIGNAL_QUOTE_STALE


def test_signed_b_shadow_without_crossover_does_not_order(monkeypatch):
    state = _state()
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    snap = MacdSnapshot(
        bar_dt=bar_dt, macd=2.0, signal=0.0, hist=3.0, hist_last3=(1.0, 2.0, 3.0),
        completed_3m_count=100, previous_diff=1.0, current_diff=2.0, relation="ABOVE",
    )
    svc = _svc()
    _patch_snap(monkeypatch, snap, svc, now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.signed_b_shadow_direction == Direction.UP_RED
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []


def test_quote_recovers_within_retries_orders_original_signal_id(monkeypatch):
    """2026-07-27 fix: QUOTE_STALE recovery is synchronous within the SAME
    run_once() call/tick — a target quote that is stale on the first forced
    refresh but fresh by the second still orders exactly once, in one call,
    using the original signal_id."""
    state = _primed_state()
    svc = _svc_with_stale_symbol(config.LONG_SYMBOL, recovers_after=1)
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert len([o for o in broker.orders if o.side == "BUY"]) == 1
    assert broker.orders[0].symbol == config.LONG_SYMBOL
    assert state.processed_signal_ids == ["20260724_090000_UP_RED"]
    assert result.actions == ["ENTRY:UP_RED"]
    assert state.last_quote_stale_result == "RECOVERED"


def test_opposite_signal_after_missed_quote_stale_processes_normally(monkeypatch):
    """A signal that finalized as MISSED_SIGNAL_QUOTE_STALE leaves nothing
    pending (2026-07-27 fix) — a genuinely NEW opposite confirmed signal on a
    later bar must dispatch normally, not be blocked by leftover state."""
    state = _primed_state()
    svc = _svc_with_stale_symbol(config.LONG_SYMBOL)
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    worker.run_once(broker=broker, market_data=svc, state=state, now=now)
    assert state.pending_signal is None
    assert broker.orders == []

    svc._quotes[config.INVERSE_SYMBOL] = QuoteSnapshot(config.INVERSE_SYMBOL, 10_000.0, datetime.now(KST), 0.0, "test")
    next_bar = datetime(2026, 7, 24, 9, 3, tzinfo=KST)
    next_now = next_bar + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(next_bar, Direction.DOWN_BLUE), svc, next_now)
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=next_now)

    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert broker.orders[-1].side == "BUY"
    assert broker.orders[-1].symbol == config.INVERSE_SYMBOL


def test_worker_start_baseline_blocks_past_crossover(monkeypatch):
    state = _state()
    svc = _svc()
    baseline_bar = datetime(2026, 7, 24, 10, 51, tzinfo=KST)
    init_now = datetime(2026, 7, 24, 10, 53, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(baseline_bar, Direction.UP_RED), svc, init_now)

    worker.initialize_strategy_session(state, svc, now=init_now, worker_instance_id="worker-test")
    tick_now = datetime(2026, 7, 24, 10, 53, 5, tzinfo=KST)
    result = worker.run_once(
        broker=FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0}),
        market_data=svc, state=state, now=tick_now,
    )

    assert result.actions == []
    assert ledger.load_signal_ledger() == []


def test_worker_after_start_new_crossover_orders_once(monkeypatch):
    state = _state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    baseline_bar = datetime(2026, 7, 24, 10, 51, tzinfo=KST)
    init_now = datetime(2026, 7, 24, 10, 53, tzinfo=KST)
    _patch_snap(monkeypatch, _snap(baseline_bar, Direction.HOLD), svc, init_now)
    worker.initialize_strategy_session(state, svc, now=init_now)

    new_bar = datetime(2026, 7, 24, 10, 54, tzinfo=KST)
    tick_now = new_bar + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(new_bar, Direction.UP_RED), svc, tick_now)
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=tick_now)

    assert result.actions == ["ENTRY:UP_RED"]
    assert len([o for o in broker.orders if o.side == "BUY"]) == 1


def test_runtime_flat_and_broker_flat_is_match_flat():
    state = _state()
    result = worker.reconcile_position_state(FakeBroker(), state, datetime(2026, 7, 24, 9, 0, tzinfo=KST), force=True)
    assert result == worker.MATCH_FLAT
    assert state.position_reconcile_diag["comparison_result"] == worker.MATCH_FLAT


def test_runtime_flat_broker_holding_recovers_runtime():
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    broker.buy_market(config.LONG_SYMBOL, 3, "seed")
    state = _state()

    result = worker.reconcile_position_state(broker, state, datetime(2026, 7, 24, 9, 0, tzinfo=KST), force=True)

    assert result == worker.RECOVERED_FROM_BROKER
    assert state.position.symbol == config.LONG_SYMBOL
    assert state.position.quantity == 3
    # 2026-08-20 fix (real incident: a position the broker held that runtime
    # never recorded entering left ZERO trace in the signal ledger -- there
    # was no way to tell when/how it appeared). Must now write a discovery
    # row so it is at least visible/auditable going forward.
    rows = ledger.load_signal_ledger()
    discovered = [r for r in rows if r["signal_type"] == "RECONCILE_DISCOVERED"]
    assert len(discovered) == 1
    assert discovered[0]["order_result"] == "RECONCILE_DISCOVERED_POSITION"
    assert config.LONG_SYMBOL in discovered[0]["signal_id"]


def test_runtime_holding_broker_flat_recovers_to_flat():
    state = _state()
    state.position = PositionSnapshot(config.LONG_SYMBOL, 3, 15_000.0)

    result = worker.reconcile_position_state(FakeBroker(), state, datetime(2026, 7, 24, 9, 0, tzinfo=KST), force=True)

    assert result == worker.RECOVERED_TO_FLAT
    assert state.position is None


def test_broker_lookup_failure_is_position_data_error():
    class ErrorBroker(FakeBroker):
        def get_positions(self):
            raise TimeoutError("temporary KIS timeout")

    state = _state()
    result = worker.reconcile_position_state(ErrorBroker(), state, datetime(2026, 7, 24, 9, 0, tzinfo=KST), force=True)
    assert result == worker.POSITION_DATA_ERROR
    assert state.position_reconcile_diag["broker_response_error"]


def test_same_signal_order_sent_once(monkeypatch):
    state = _primed_state()
    svc = _svc()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    bar_dt = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    now = bar_dt + timedelta(minutes=3)
    _patch_snap(monkeypatch, _snap(bar_dt, Direction.UP_RED), svc, now)

    worker.run_once(broker=broker, market_data=svc, state=state, now=now)
    worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert len([o for o in broker.orders if o.side == "BUY"]) == 1


def test_signal_id_date_and_time_are_both_from_completed_bar():
    bar_dt = datetime(2026, 1, 6, 15, 27, tzinfo=KST)
    assert make_signal_id(bar_dt, Direction.UP_RED) == "20260106_152700_UP_RED"
