"""Unit + integration tests for Profit Lock — MACD convergence early exit
(docs §10 2026-08-05 spec, docs/MACD2_LOGIC.md "2026-08-05 Profit Lock — MACD
Convergence Early Exit"). Isolated to tmp_path via conftest.py."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _macd_snap(bar_dt: datetime, macd: float, signal: float) -> MacdSnapshot:
    hist = macd - signal
    return MacdSnapshot(
        bar_dt=bar_dt, macd=macd, signal=signal, hist=hist,
        hist_last3=(hist, hist, hist), completed_3m_count=200,
        previous_diff=1.0, current_diff=1.0, relation="ABOVE",
    )


def _fresh_state(*, budget: float = 10_000_000.0):
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.major_filter_enabled = False
    return state


def _drive(state, *, symbol, direction, entry_price, quantity, steps):
    """``steps``: list of (support_gap, current_price) pairs, one per
    completed WATCH_SYMBOL 3-minute bar (3 minutes apart, starting 09:00).
    UP_RED encodes gap as macd (signal=0); DOWN_BLUE encodes gap as signal
    (macd=0) — matches _held_direction_support_gap's own formula. Returns the
    LAST call's should_exit result."""
    bar0 = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    should_exit = False
    for i, (gap, price) in enumerate(steps):
        bar_dt = bar0 + timedelta(minutes=3 * i)
        snap = _macd_snap(bar_dt, macd=gap, signal=0.0) if direction == Direction.UP_RED \
            else _macd_snap(bar_dt, macd=0.0, signal=gap)
        should_exit = worker._advance_profit_lock(
            state, symbol=symbol, direction=direction, macd_snap=snap,
            current_price=price, entry_price=entry_price, quantity=quantity,
        )
    return should_exit


# ── _held_direction_support_gap ────────────────────────────────────────────

def test_support_gap_up_red_is_macd_minus_signal():
    snap = _macd_snap(datetime(2026, 1, 5, 9, 3, tzinfo=KST), macd=10.0, signal=4.0)
    assert worker._held_direction_support_gap(Direction.UP_RED, snap) == pytest.approx(6.0)


def test_support_gap_down_blue_is_signal_minus_macd():
    snap = _macd_snap(datetime(2026, 1, 5, 9, 3, tzinfo=KST), macd=4.0, signal=10.0)
    assert worker._held_direction_support_gap(Direction.DOWN_BLUE, snap) == pytest.approx(6.0)


def test_support_gap_returns_none_for_hold_direction():
    snap = _macd_snap(datetime(2026, 1, 5, 9, 3, tzinfo=KST), macd=4.0, signal=10.0)
    assert worker._held_direction_support_gap(Direction.HOLD, snap) is None


# ── _advance_profit_lock: seeding / same-bar no-op ─────────────────────────

def test_advance_profit_lock_first_call_seeds_baseline_no_exit():
    state = _fresh_state()
    bar0 = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    snap = _macd_snap(bar0, macd=10.0, signal=2.0)

    should_exit = worker._advance_profit_lock(
        state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED, macd_snap=snap,
        current_price=15_300.0, entry_price=15_000.0, quantity=10,
    )

    assert should_exit is False
    assert state.profit_lock_symbol == config.LONG_SYMBOL
    assert state.profit_lock_entry_bar_ts == bar0.isoformat()
    assert state.profit_lock_bars_since_entry == 0
    assert state.profit_lock_gap_history == []


def test_advance_profit_lock_same_completed_bar_is_never_reevaluated():
    """진행봉(같은 완성봉 재확인)으로는 절대 청산 판정하지 않는다 — 반복 호출은
    no-op."""
    state = _fresh_state()
    bar0 = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    snap0 = _macd_snap(bar0, macd=10.0, signal=2.0)
    worker._advance_profit_lock(
        state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED, macd_snap=snap0,
        current_price=15_300.0, entry_price=15_000.0, quantity=10,
    )
    for _ in range(5):
        should_exit = worker._advance_profit_lock(
            state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED, macd_snap=snap0,
            current_price=20_000.0, entry_price=15_000.0, quantity=10,
        )
        assert should_exit is False
    assert state.profit_lock_bars_since_entry == 0


# ── 5 conditions: isolated failures + full pass ────────────────────────────

def test_profit_lock_no_exit_below_min_net_return():
    state = _fresh_state()
    entry_price = 15_000.0
    steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.009),   # peak +0.9%
        (4.0, entry_price * 1.006),
        (1.5, entry_price * 1.005),   # current +0.5% < 1.0% required
    ]
    should_exit = _drive(state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED,
                          entry_price=entry_price, quantity=10, steps=steps)
    assert should_exit is False


def test_profit_lock_no_exit_before_three_completed_bars():
    state = _fresh_state()
    entry_price = 15_000.0
    steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.030),
        (1.5, entry_price * 1.027),  # only bars_since_entry == 2 here
    ]
    should_exit = _drive(state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED,
                          entry_price=entry_price, quantity=10, steps=steps)
    assert should_exit is False
    assert state.profit_lock_bars_since_entry == 2


def test_profit_lock_no_exit_without_two_consecutive_contractions():
    state = _fresh_state()
    entry_price = 15_000.0
    steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.030),
        (4.0, entry_price * 1.028),
        (6.0, entry_price * 1.027),  # gap widens again -- breaks the streak
    ]
    should_exit = _drive(state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED,
                          entry_price=entry_price, quantity=10, steps=steps)
    assert should_exit is False
    assert state.profit_lock_bars_since_entry == 3
    assert state.profit_lock_contraction_count == 0


def test_profit_lock_no_exit_when_gap_ratio_exceeds_threshold():
    state = _fresh_state()
    entry_price = 15_000.0
    steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.030),
        (7.0, entry_price * 1.028),
        (6.0, entry_price * 1.027),  # ratio 6/8 = 0.75 > 0.25
    ]
    should_exit = _drive(state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED,
                          entry_price=entry_price, quantity=10, steps=steps)
    assert should_exit is False
    assert state.profit_lock_contraction_count == 2
    assert state.profit_lock_gap_ratio == pytest.approx(0.75)


def test_profit_lock_no_exit_below_min_drawdown():
    state = _fresh_state()
    entry_price = 15_000.0
    steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.020),
        (4.0, entry_price * 1.019),
        (1.5, entry_price * 1.019),  # peak 2.0%, current 1.9% -- giveback only 0.1pp
    ]
    should_exit = _drive(state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED,
                          entry_price=entry_price, quantity=10, steps=steps)
    assert should_exit is False
    assert state.profit_lock_drawdown_pct < config.PROFIT_LOCK_MIN_DRAWDOWN_PP


def test_profit_lock_support_gap_non_positive_defers_to_opposite_signal():
    """support_gap <= 0 must never exit via Profit Lock -- OPPOSITE_SIGNAL owns
    that case (checked at a higher priority in worker.run_once)."""
    state = _fresh_state()
    entry_price = 15_000.0
    steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.030),
        (4.0, entry_price * 1.028),
        (-1.0, entry_price * 1.027),  # gap flipped negative
    ]
    should_exit = _drive(state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED,
                          entry_price=entry_price, quantity=10, steps=steps)
    assert should_exit is False
    assert state.profit_lock_current_support_gap == -1.0


@pytest.mark.parametrize(
    "direction,symbol",
    [(Direction.UP_RED, config.LONG_SYMBOL), (Direction.DOWN_BLUE, config.INVERSE_SYMBOL)],
)
def test_profit_lock_exits_when_all_five_conditions_met(direction, symbol):
    state = _fresh_state()
    entry_price = 15_000.0
    steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.030),    # bars=1, peak return 3.0%
        (4.0, entry_price * 1.028),    # bars=2, contraction 1 (8->4)
        (1.5, entry_price * 1.027),    # bars=3, contraction 2 (4->1.5), ratio 1.5/8=0.1875, drawdown 0.3pp
    ]
    should_exit = _drive(state, symbol=symbol, direction=direction,
                          entry_price=entry_price, quantity=10, steps=steps)

    assert should_exit is True
    assert state.profit_lock_bars_since_entry == 3
    assert state.profit_lock_contraction_count == 2
    assert state.profit_lock_gap_ratio == pytest.approx(1.5 / 8.0)
    assert state.profit_lock_max_support_gap == pytest.approx(8.0)
    assert state.profit_lock_current_support_gap == pytest.approx(1.5)
    assert state.profit_lock_peak_return_pct >= config.PROFIT_LOCK_MIN_NET_RETURN_PCT
    assert state.profit_lock_drawdown_pct >= config.PROFIT_LOCK_MIN_DRAWDOWN_PP


def test_profit_lock_exit_flag_never_refires_for_the_same_completed_bar():
    """중복 매도 방지: 같은 완성봉에서 should_exit=True가 한 번 나온 뒤 다시
    호출해도 재청산 신호를 내지 않는다(caller가 이미 청산해서 다음 tick부터는
    포지션이 없겠지만, 방어적으로 같은 bar_key 재호출 자체가 no-op임을 검증)."""
    state = _fresh_state()
    entry_price = 15_000.0
    steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.030),
        (4.0, entry_price * 1.028),
        (1.5, entry_price * 1.027),
    ]
    first = _drive(state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED,
                   entry_price=entry_price, quantity=10, steps=steps)
    assert first is True

    last_bar = datetime(2026, 1, 5, 9, 0, tzinfo=KST) + timedelta(minutes=3 * (len(steps) - 1))
    snap_repeat = _macd_snap(last_bar, macd=1.5, signal=0.0)
    second = worker._advance_profit_lock(
        state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED, macd_snap=snap_repeat,
        current_price=entry_price * 1.027, entry_price=entry_price, quantity=10,
    )
    assert second is False


def test_profit_lock_state_restart_persistence_continues_the_same_streak():
    """Worker 재시작 시나리오: 2봉까지 진행된 state를 직렬화/역직렬화한 뒤
    이어서 3번째 봉을 먹이면 재시작 전과 동일하게 판정된다."""
    state = _fresh_state()
    entry_price = 15_000.0
    partial_steps = [
        (8.0, entry_price),
        (8.0, entry_price * 1.030),
        (4.0, entry_price * 1.028),
    ]
    _drive(state, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED,
           entry_price=entry_price, quantity=10, steps=partial_steps)
    assert state.profit_lock_bars_since_entry == 2

    restarted = state_store.deserialize(state_store.serialize(state))
    assert restarted.profit_lock_bars_since_entry == 2
    assert restarted.profit_lock_gap_history == state.profit_lock_gap_history

    bar3 = datetime(2026, 1, 5, 9, 0, tzinfo=KST) + timedelta(minutes=9)
    snap3 = _macd_snap(bar3, macd=1.5, signal=0.0)
    should_exit = worker._advance_profit_lock(
        restarted, symbol=config.LONG_SYMBOL, direction=Direction.UP_RED, macd_snap=snap3,
        current_price=entry_price * 1.027, entry_price=entry_price, quantity=10,
    )
    assert should_exit is True


# ── run_once() integration ──────────────────────────────────────────────

def _svc_with_quote(df_1m, bootstrap_now, quote_prices):
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _flat_1m_frame(start: datetime, minutes: int = 300) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=i), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 10}
        for i in range(minutes)
    ]
    return pd.DataFrame(rows)


def _patch_macd_sequence(monkeypatch, snapshots):
    """worker.calculate_macd is bound via `from signal_engine import
    calculate_macd` -- monkeypatching worker.calculate_macd intercepts every
    call inside run_once() without touching signal_engine.py itself."""
    it = iter(snapshots)

    def _fake(_bars_3m):
        try:
            return next(it)
        except StopIteration:
            return snapshots[-1]

    monkeypatch.setattr(worker, "calculate_macd", _fake)


def test_run_once_profit_lock_exit_executes_sell_and_records_ledger(monkeypatch):
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _flat_1m_frame(prior_day)
    now0 = prior_day + timedelta(minutes=300, seconds=5)
    entry_price = 15_000.0

    quote_prices = {config.LONG_SYMBOL: entry_price, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    svc = _svc_with_quote(df_1m, now0, quote_prices)

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=entry_price)
    state.profit_lock_enabled = True  # 2026-08-05: default is OFF; enable explicitly for this test

    snaps = [
        _macd_snap(prior_day + timedelta(minutes=300 + 3 * i), macd=gap, signal=0.0)
        for i, gap in enumerate([8.0, 8.0, 4.0, 1.5])
    ]
    _patch_macd_sequence(monkeypatch, snaps)

    prices = [entry_price, entry_price * 1.030, entry_price * 1.028, entry_price * 1.027]
    result = None
    for i, price in enumerate(prices):
        broker.set_quote(config.LONG_SYMBOL, price)
        svc._quotes[config.LONG_SYMBOL] = svc._quotes[config.LONG_SYMBOL].__class__(
            config.LONG_SYMBOL, price, datetime.now(KST), 0.0, "test", None,
        )
        result = run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3 * i))

    assert any(a.startswith("PROFIT_LOCK_MACD_CONVERGENCE:") for a in result.actions)
    assert state.position is None
    rows = ledger.load_execution_ledger()
    last = rows[-1]
    assert last["exit_reason"] == config.EXIT_PROFIT_LOCK_MACD_CONVERGENCE
    assert last["profit_lock_enabled"] == "True"
    assert float(last["profit_lock_gap_ratio"]) == pytest.approx(1.5 / 8.0)
    assert float(last["profit_lock_contraction_count"]) == 2


def test_run_once_profit_lock_disabled_preserves_existing_behavior(monkeypatch):
    """OFF: no Profit Lock exit even though every numeric condition is met --
    existing Stop Loss/OPPOSITE_SIGNAL/FORCED_LIQUIDATION/Quick-Profit-only
    behavior is completely unaffected."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _flat_1m_frame(prior_day)
    now0 = prior_day + timedelta(minutes=300, seconds=5)
    entry_price = 15_000.0

    quote_prices = {config.LONG_SYMBOL: entry_price, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    svc = _svc_with_quote(df_1m, now0, quote_prices)

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=entry_price)
    state.profit_lock_enabled = False

    snaps = [
        _macd_snap(prior_day + timedelta(minutes=300 + 3 * i), macd=gap, signal=0.0)
        for i, gap in enumerate([8.0, 8.0, 4.0, 1.5])
    ]
    _patch_macd_sequence(monkeypatch, snaps)

    prices = [entry_price, entry_price * 1.030, entry_price * 1.028, entry_price * 1.027]
    result = None
    for i, price in enumerate(prices):
        broker.set_quote(config.LONG_SYMBOL, price)
        svc._quotes[config.LONG_SYMBOL] = svc._quotes[config.LONG_SYMBOL].__class__(
            config.LONG_SYMBOL, price, datetime.now(KST), 0.0, "test", None,
        )
        result = run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3 * i))

    assert not any(a.startswith("PROFIT_LOCK") for a in result.actions)
    assert state.position is not None
    assert state.position.quantity == 10


def test_run_once_forced_liquidation_overrides_profit_lock(monkeypatch):
    """docs §10 priority: FORCED_LIQUIDATION (1) beats PROFIT_LOCK_MACD_CONVERGENCE
    (4) even when every Profit Lock condition is also numerically satisfied."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _flat_1m_frame(prior_day)
    entry_price = 15_000.0

    quote_prices = {config.LONG_SYMBOL: entry_price * 1.030, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}
    now_after_1500 = prior_day.replace(hour=15, minute=1)
    svc = _svc_with_quote(df_1m, now_after_1500, quote_prices)

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: entry_price * 1.030})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=entry_price)
    state.profit_lock_enabled = True  # 2026-08-05: default is OFF; enable explicitly so this test is meaningful
    state.profit_lock_bars_since_entry = 3
    state.profit_lock_gap_history = [8.0, 4.0, 1.5]
    state.profit_lock_symbol = config.LONG_SYMBOL
    state.profit_lock_entry_bar_ts = prior_day.isoformat()
    state.profit_lock_last_bar_ts = (prior_day + timedelta(minutes=6)).isoformat()

    snap = _macd_snap(prior_day + timedelta(minutes=9), macd=1.5, signal=0.0)
    monkeypatch.setattr(worker, "calculate_macd", lambda _bars: snap)

    result = run_once(broker=broker, market_data=svc, state=state, now=now_after_1500)

    assert any(a.startswith("FORCED_LIQUIDATION:") for a in result.actions)
    assert not any(a.startswith("PROFIT_LOCK") for a in result.actions)
    assert state.position is None
    rows = ledger.load_execution_ledger()
    assert rows[-1]["exit_reason"] == config.EXIT_FORCED_LIQUIDATION
