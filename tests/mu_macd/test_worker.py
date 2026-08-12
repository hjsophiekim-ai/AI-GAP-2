"""MOCK/READ-ONLY end-to-end test: WebSocket-shaped ticks -> 1m bars -> 3m
bars -> MACD confirmed crossover -> order dispatch, entirely in mock mode
(FakeBroker, no network — see conftest._block_real_network). Verifies the
worker's own decision matches an independent recomputation using the exact
same reused pure functions (signal_engine.resample_completed_3m/
calculate_macd/evaluate_macd_crossover), so this is also a regression check
that worker.py didn't silently drift from those functions' real behavior.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m
from app.trading.mu_macd import config, ledger, state_store, worker
from app.trading.mu_macd.config import KST
from app.trading.mu_macd.market_data import MUMarketDataService
from app.trading.mu_macd.models import Direction
from tests.macd2.fake_broker import FakeBroker


def _build_flat_then_ramp_service(now_minutes_total: int = 170) -> MUMarketDataService:
    """150 minutes flat at 880.0 (warms EMA well past EMA_SLOW=26 * 3min --
    50 completed 3m bars, comfortably clearing WARMUP_MIN_3M_BARS=30 even at
    the crossing bar itself), then a steady ramp for the remaining minutes --
    guaranteed to eventually push the histogram from <=0 to >0 (a real
    UP_RED-equivalent crossover)."""
    svc = MUMarketDataService(mode="mock")
    date_str = "20260812"
    start = datetime(2026, 8, 12, 9, 0, tzinfo=KST)
    flat_minutes = 150
    for i in range(now_minutes_total):
        t = start + timedelta(minutes=i)
        minute_key = f"{t.hour:02d}{t.minute:02d}"
        if i < flat_minutes:
            price = 880.0
        else:
            price = 880.0 + 3.0 * (i - flat_minutes + 1)  # steep enough for an unambiguous, early crossing
        svc.inject_1m_bar(date_str, minute_key, price, price, price, price, 1000)
    return svc


def _now_after(svc: MUMarketDataService, extra_minutes: int = 4) -> datetime:
    df = svc.get_history_df()
    return df["datetime"].iloc[-1] + timedelta(minutes=extra_minutes)


def _find_crossing_now(svc: MUMarketDataService) -> datetime:
    """Scan forward bar-by-bar to find the exact moment evaluate_macd_crossover
    first reports a real (non-HOLD) direction -- the crossing itself, not
    some later bar where the diff is already well past zero."""
    df = svc.get_history_df()
    for i in range(len(df)):
        candidate_now = df["datetime"].iloc[i] + timedelta(minutes=1)
        bars_3m = resample_completed_3m(df, now=candidate_now)
        snap = calculate_macd(bars_3m)
        if snap is None:
            continue
        direction = evaluate_macd_crossover(snap, None)
        if direction != Direction.HOLD:
            return candidate_now
    raise AssertionError("fixture never produces a real crossover -- widen the ramp")


def test_worker_matches_independent_macd_recomputation_and_places_expected_order():
    svc = _build_flat_then_ramp_service()
    now = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now

    # ── independent expectation, using the SAME reused pure functions ──
    df_1m = svc.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    assert len(bars_3m) >= config.WARMUP_MIN_3M_BARS  # test data must actually clear the warm-up gate
    macd_snap = calculate_macd(bars_3m)
    assert macd_snap is not None
    expected_direction = evaluate_macd_crossover(macd_snap, None)

    # A flat-then-ramp-up series must produce UP_RED (histogram crossing
    # from <=0 to >0) somewhere at/after the ramp — if this fails, the test
    # fixture itself needs a bigger ramp, not the worker.
    assert expected_direction in (Direction.UP_RED, Direction.HOLD)

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    if expected_direction == Direction.HOLD:
        assert state.position is None
        return

    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL  # UP_RED -> leverage ETF
    assert "ENTRY:UP_RED" in result.actions

    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[0]["direction"] == "UP_RED"
    assert rows[0]["order_result"] == "EXECUTED"
    assert rows[0]["signal_rule"] == config.SIGNAL_RULE
    assert rows[0]["strategy_name"] == config.STRATEGY_NAME


def test_new_entry_blocked_when_ws_stale_even_with_valid_flag():
    svc = _build_flat_then_ramp_service()
    now = _find_crossing_now(svc)  # a real, non-HOLD flag definitely fires at this exact now
    svc.ws_connected = True
    svc.ws_last_tick_at = now - timedelta(seconds=config.WS_STALE_MAX_SEC + 5)  # stale

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None
    assert result.skipped == config.BLOCK_WS_STALE
    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[-1]["block_reason"] == config.BLOCK_WS_STALE
    assert rows[-1]["order_result"] == "BLOCKED"


def test_new_entry_blocked_when_warmup_insufficient():
    svc = _build_flat_then_ramp_service(now_minutes_total=20)  # far short of WARMUP_MIN_3M_BARS
    now = _now_after(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True

    worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None
    assert state.warmup_ready is False


def test_opposite_flag_sells_but_never_rebuys_when_ws_stale():
    """A confirmed opposite-direction MU flag while holding a position must
    still SELL the held ETF even when WS is stale (data-quality doubt), but
    must NEVER place the follow-up BUY into the new direction."""
    svc = _build_flat_then_ramp_service()
    now = _find_crossing_now(svc)  # a real, non-HOLD flag definitely fires at this exact now
    svc.ws_connected = True
    svc.ws_last_tick_at = now - timedelta(seconds=config.WS_STALE_MAX_SEC + 5)  # stale -> blocks the BUY leg only

    df_1m = svc.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    macd_snap = calculate_macd(bars_3m)
    expected_direction = evaluate_macd_crossover(macd_snap, None)
    assert expected_direction == Direction.UP_RED  # the flat-then-ramp-up fixture only ever crosses upward

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.INVERSE_SYMBOL, 100, "seed-inverse")  # held OPPOSITE of the incoming UP_RED flag

    from app.trading.mu_macd.models import PositionSnapshot
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=100, avg_price=10_000.0, entry_at=now - timedelta(minutes=30))

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None  # sold out -- never re-entered LONG_SYMBOL
    assert any(a.startswith("OPPOSITE_SIGNAL_SELL_ONLY") for a in result.actions)
    assert [(o.side, o.symbol) for o in broker.orders] == [
        ("BUY", config.INVERSE_SYMBOL), ("SELL", config.INVERSE_SYMBOL),
    ]  # the seed buy, then the sell-only exit -- no third (re-entry buy) order


def test_opposite_flag_sells_all_existing_qty_and_buys_full_new_position():
    """The happy-path reversal (entry gate CLEAR): a confirmed opposite MU
    flag must sell 100% of the held ETF and buy into the new direction —
    this is exactly the scenario the user reported as broken in the
    existing Hynix MACD2 program historically (stop loss worked, but
    opposite-flag sell-all+buy-new did not always fire)."""
    svc = _build_flat_then_ramp_service()
    now = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now  # fresh -- entry gate clear

    df_1m = svc.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    macd_snap = calculate_macd(bars_3m)
    expected_direction = evaluate_macd_crossover(macd_snap, None)
    assert expected_direction == Direction.UP_RED

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    seed_qty = 100
    broker.buy_market(config.INVERSE_SYMBOL, seed_qty, "seed-inverse")  # held OPPOSITE of the incoming UP_RED flag

    from app.trading.mu_macd.models import PositionSnapshot
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    state.warmup_bars_3m_count = config.WARMUP_MIN_3M_BARS  # gate already satisfied structurally; run_once recomputes it anyway
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=seed_qty, avg_price=10_000.0, entry_at=now - timedelta(minutes=30))

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    # ── the held INVERSE position must be FULLY sold, none left over ──────
    inverse_positions = [p for p in broker.get_positions() if p.symbol == config.INVERSE_SYMBOL and p.quantity > 0]
    assert inverse_positions == []
    # ── a brand-new LONG position must exist, sized off the real budget ───
    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL
    assert state.position.quantity > 0
    long_positions = [p for p in broker.get_positions() if p.symbol == config.LONG_SYMBOL]
    assert long_positions and long_positions[0].quantity == state.position.quantity
    assert f"OPPOSITE_SIGNAL:{Direction.UP_RED.value}" in result.actions

    sides_symbols = [(o.side, o.symbol) for o in broker.orders]
    assert sides_symbols[0] == ("BUY", config.INVERSE_SYMBOL)  # the seed
    assert sides_symbols[1] == ("SELL", config.INVERSE_SYMBOL)  # sell-all of the old direction
    assert sides_symbols[2] == ("BUY", config.LONG_SYMBOL)  # buy-full of the new direction

    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[0]["signal_type"] == "REVERSAL"
    assert rows[0]["order_result"] == "EXECUTED"
    assert rows[0]["direction"] == "UP_RED"


def test_full_sequence_entry_then_reversal_across_separate_run_once_calls():
    """Same RuntimeState object reused across TWO separate run_once() calls,
    exactly like the real service loop's persistent state (not a single
    isolated call) -- entry on an UP_RED flag, then later a genuine
    DOWN_BLUE flag must sell 100% of the LONG position and buy 100% into
    INVERSE. This is the exact multi-tick scenario the user reported as
    historically broken (stop loss worked; the follow-through reversal
    across ticks did not)."""
    svc = MUMarketDataService(mode="mock")
    date_str = "20260812"
    start = datetime(2026, 8, 12, 9, 0, tzinfo=KST)
    # flat (150) -> ramp up (30) -> flat at the peak (10) -> ramp down (30)
    prices = [880.0] * 150 + [880.0 + 3.0 * i for i in range(1, 31)] + [970.0] * 10 + [970.0 - 3.0 * i for i in range(1, 31)]
    for i, price in enumerate(prices):
        t = start + timedelta(minutes=i)
        svc.inject_1m_bar(date_str, f"{t.hour:02d}{t.minute:02d}", price, price, price, price, 1000)

    df_full = svc.get_history_df()

    def _crossing_after(min_index: int, expected: Direction) -> tuple[datetime, int]:
        for i in range(min_index, len(df_full)):
            candidate_now = df_full["datetime"].iloc[i] + timedelta(minutes=1)
            partial = df_full[df_full["datetime"] <= df_full["datetime"].iloc[i]]
            bars_3m = resample_completed_3m(partial, now=candidate_now)
            snap = calculate_macd(bars_3m)
            if snap is None:
                continue
            prior = evaluate_macd_crossover(snap, None)
            if prior == expected:
                return candidate_now, i
        raise AssertionError(f"fixture never crosses {expected}")

    up_now, up_idx = _crossing_after(150, Direction.UP_RED)
    down_now, _down_idx = _crossing_after(up_idx + 1, Direction.DOWN_BLUE)

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    svc.ws_connected = True

    # ── tick 1: entry on the real UP_RED flag ──────────────────────────────
    svc.ws_last_tick_at = up_now
    worker.run_once(broker=broker, market_data=svc, state=state, now=up_now)
    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL
    long_qty_after_entry = state.position.quantity
    assert any(p.symbol == config.LONG_SYMBOL and p.quantity == long_qty_after_entry for p in broker.get_positions())

    # ── tick 2 (same state object, later time): the real DOWN_BLUE flag must
    # sell ALL of the LONG position and buy INTO the full INVERSE position ──
    svc.ws_last_tick_at = down_now
    result2 = worker.run_once(broker=broker, market_data=svc, state=state, now=down_now)

    long_positions = [p for p in broker.get_positions() if p.symbol == config.LONG_SYMBOL and p.quantity > 0]
    assert long_positions == [], "the old LONG position must be fully sold, not left over"
    assert state.position is not None
    assert state.position.symbol == config.INVERSE_SYMBOL
    assert state.position.quantity > 0
    inverse_positions = [p for p in broker.get_positions() if p.symbol == config.INVERSE_SYMBOL]
    assert inverse_positions and inverse_positions[0].quantity == state.position.quantity
    assert f"OPPOSITE_SIGNAL:{Direction.DOWN_BLUE.value}" in result2.actions

    rows = ledger.load_signal_ledger()
    assert len(rows) == 2
    assert rows[0]["signal_type"] == "INITIAL" and rows[0]["direction"] == "UP_RED"
    assert rows[1]["signal_type"] == "REVERSAL" and rows[1]["direction"] == "DOWN_BLUE"
    assert rows[1]["order_result"] == "EXECUTED"


def test_stop_loss_exit_never_blocked_by_ws_staleness():
    """An existing position's stop-loss exit must fire even if the WS feed
    is completely stale/disconnected -- only NEW entries are gated on WS
    health (mirrors macd2's own 'sell-only never blocked' principle)."""
    svc = _build_flat_then_ramp_service(now_minutes_total=100)
    now = _now_after(svc)
    svc.ws_connected = False  # fully disconnected
    svc.ws_last_tick_at = None

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 100, "seed-long")  # real broker holding, must match state.position
    from app.trading.mu_macd.models import PositionSnapshot
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=100, avg_price=10_000.0, entry_at=now - timedelta(minutes=30))
    broker.set_quote(config.LONG_SYMBOL, 5_000.0)
    # current quote (5,000) vs avg_price (10,000) => -50% net return, well past -1.5% stop loss
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None
    assert any(a.startswith("STOP_LOSS") for a in result.actions)
