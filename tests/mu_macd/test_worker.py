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
from typing import Optional

import pandas as pd
import pytest

from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m
from app.trading.mu_macd import config, ledger, state_store, worker
from app.trading.mu_macd.config import KST
from app.trading.mu_macd.market_data import MUMarketDataService
from app.trading.mu_macd.models import Direction, PositionSnapshot
from tests.macd2.fake_broker import FakeBroker


def _build_flat_then_ramp_service(now_minutes_total: int = 170, start: Optional[datetime] = None) -> MUMarketDataService:
    """150 minutes flat at 880.0 (warms EMA well past EMA_SLOW=26 * 3min --
    50 completed 3m bars, comfortably clearing WARMUP_MIN_3M_BARS=30 even at
    the crossing bar itself), then a steady ramp for the remaining minutes --
    guaranteed to eventually push the histogram from <=0 to >0 (a real
    UP_RED-equivalent crossover).

    2026-08-14: default ``start`` moved 2h earlier than KRX open (was
    09:00, now 07:00) so the DEFAULT crossing (~09:33) lands clear of the
    new 11:00-14:00 MIDDAY_ENTRY_PAUSE window -- every existing caller here
    wants an ordinary, unblocked crossing. Tests that specifically want a
    crossing INSIDE the pause window pass the original
    ``start=datetime(2026, 8, 12, 9, 0, tzinfo=KST)`` explicitly (crossing
    ~11:33)."""
    svc = MUMarketDataService(mode="mock")
    date_str = "20260812"
    start = start or datetime(2026, 8, 12, 7, 0, tzinfo=KST)
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

    # 2026-08-13 regression lock: order_executor.execute_signal is shared
    # with macd2 -- its _record_leg used to hardcode macd2's OWN ledger
    # module, so every MU_MACD execution silently landed in macd2's
    # execution ledger instead of mu_macd's own. Must land here now.
    exec_rows = ledger.load_execution_ledger()
    assert len(exec_rows) == 1
    assert exec_rows[0]["symbol"] == config.LONG_SYMBOL
    assert exec_rows[0]["side"] == "BUY"


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


def test_new_entry_blocked_when_entry_paused_but_flag_still_recorded():
    """2026-08-14 feature: user-toggled "신규진입 일시정지" must still let MU
    price collection / MACD flag detection / signal-ledger recording happen
    exactly as normal -- only the resulting BUY is blocked."""
    svc = _build_flat_then_ramp_service()
    now = _find_crossing_now(svc)  # a real, non-HOLD flag definitely fires at this exact now
    svc.ws_connected = True
    svc.ws_last_tick_at = now  # fresh -- WS/warmup gates are clear, ONLY entry_paused should block

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    state.entry_paused = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None
    assert result.skipped == config.BLOCK_ENTRY_PAUSED_BY_USER
    assert broker.orders == []  # no BUY was ever placed
    rows = ledger.load_signal_ledger()
    assert len(rows) == 1  # the flag itself was still recorded
    assert rows[-1]["signal_type"] == "INITIAL"
    assert rows[-1]["block_reason"] == config.BLOCK_ENTRY_PAUSED_BY_USER
    assert rows[-1]["order_result"] == "BLOCKED"


def test_opposite_flag_sells_but_never_rebuys_when_entry_paused():
    """Same "sell always, buy only if the gate is clear" principle as the
    WS-stale case, exercised for the entry_paused gate instead."""
    svc = _build_flat_then_ramp_service()
    now = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now  # fresh -- ONLY entry_paused should block the re-buy leg

    df_1m = svc.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    macd_snap = calculate_macd(bars_3m)
    expected_direction = evaluate_macd_crossover(macd_snap, None)
    assert expected_direction == Direction.UP_RED

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.INVERSE_SYMBOL, 100, "seed-inverse")  # held OPPOSITE of the incoming UP_RED flag

    from app.trading.mu_macd.models import PositionSnapshot
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    state.entry_paused = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=100, avg_price=10_000.0, entry_at=now - timedelta(minutes=30))

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None  # sold out -- never re-entered LONG_SYMBOL
    assert any(a.startswith("OPPOSITE_SIGNAL_SELL_ONLY") for a in result.actions)
    assert [(o.side, o.symbol) for o in broker.orders] == [
        ("BUY", config.INVERSE_SYMBOL), ("SELL", config.INVERSE_SYMBOL),
    ]  # the seed buy, then the sell-only exit -- no third (re-entry buy) order


def test_run_flags_only_records_flag_without_touching_broker_or_position():
    """2026-08-14: after a REAL-mode restart, the broker can't be
    reconstructed without the human re-entering the confirm phrase, but MU
    price collection/MACD flag detection must keep running via this
    broker-less path -- a real flag must still get logged (BLOCKED, with
    the dedicated block_reason) and state.position must be left EXACTLY as
    it was (this function must never guess/clear/touch it -- it has no
    broker to verify anything against)."""
    svc = _build_flat_then_ramp_service()
    now = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now

    from app.trading.mu_macd.models import PositionSnapshot
    state = state_store.default_state()
    state.mode = "real"
    state.auto_trade_on = True
    stale_position = PositionSnapshot(
        symbol=config.INVERSE_SYMBOL, quantity=50, avg_price=8_000.0, entry_at=now - timedelta(hours=2),
    )
    state.position = stale_position

    result = worker.run_flags_only(market_data=svc, state=state, now=now)

    assert state.position is stale_position  # completely untouched
    assert result.skipped == config.BLOCK_REAL_BROKER_NOT_AUTHENTICATED
    assert any(a.startswith("FLAGS_ONLY_NO_BROKER") for a in result.actions)
    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[0]["order_result"] == "BLOCKED"
    assert rows[0]["block_reason"] == config.BLOCK_REAL_BROKER_NOT_AUTHENTICATED
    assert rows[0]["direction"] == "UP_RED"


def test_entry_gate_midday_pause_window_boundaries():
    """2026-08-14 user-requested schedule: entries ON 09:00-11:00, OFF
    11:00-14:00, ON again 14:00 through the existing NEW_ENTRY_CUTOFF/
    FORCE_LIQUIDATE_AT close-of-day logic (both untouched). Exercises the
    exact boundaries directly against _entry_gate_block_reason (WS/warmup
    already satisfied, so only the midday window itself can be blocking)."""
    base_state = state_store.default_state()
    base_state.ws_connected = True
    base_state.warmup_ready = True

    for hh, mm, expected_blocked in [
        (10, 59, False), (11, 0, True), (12, 30, True), (13, 59, True), (14, 0, False),
    ]:
        now = datetime(2026, 8, 13, hh, mm, tzinfo=KST)
        state = state_store.default_state()
        state.ws_connected = True
        state.warmup_ready = True
        state.ws_last_tick_at = now.isoformat()
        reason = worker._entry_gate_block_reason(state, now)
        if expected_blocked:
            assert reason == config.BLOCK_MIDDAY_ENTRY_PAUSE, f"{hh:02d}:{mm:02d} should be blocked"
        else:
            assert reason is None, f"{hh:02d}:{mm:02d} should be clear, got {reason}"


def test_new_entry_blocked_during_midday_pause_but_flag_still_recorded():
    """User's own example: reuses the shared flat-then-ramp fixture with its
    ORIGINAL 09:00 start (rather than the shared default's 07:00), whose
    first real crossover lands at 11:33 KST -- squarely inside the new
    11:00-14:00 pause window."""
    svc = _build_flat_then_ramp_service(start=datetime(2026, 8, 12, 9, 0, tzinfo=KST))
    now = _find_crossing_now(svc)
    assert now.time() >= config.MIDDAY_ENTRY_PAUSE_START and now.time() < config.MIDDAY_ENTRY_PAUSE_END
    svc.ws_connected = True
    svc.ws_last_tick_at = now

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None
    assert result.skipped == config.BLOCK_MIDDAY_ENTRY_PAUSE
    assert broker.orders == []
    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[-1]["block_reason"] == config.BLOCK_MIDDAY_ENTRY_PAUSE
    assert rows[-1]["order_result"] == "BLOCKED"


def test_opposite_flag_sells_but_never_rebuys_during_midday_pause():
    """User's own example: 10:50에 보유 중, 11:05(-ish)에 반대 플래그가 뜨면
    보유 포지션은 전량 청산하되 신규(재)진입은 하지 않는다. Reuses the same
    11:33 KST crossing as above (explicit 09:00 start override)."""
    svc = _build_flat_then_ramp_service(start=datetime(2026, 8, 12, 9, 0, tzinfo=KST))
    now = _find_crossing_now(svc)
    assert now.time() >= config.MIDDAY_ENTRY_PAUSE_START and now.time() < config.MIDDAY_ENTRY_PAUSE_END
    svc.ws_connected = True
    svc.ws_last_tick_at = now

    df_1m = svc.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    macd_snap = calculate_macd(bars_3m)
    expected_direction = evaluate_macd_crossover(macd_snap, None)
    assert expected_direction == Direction.UP_RED

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


def test_entry_gate_allows_entry_at_krx_open_when_dnasmu_warmup_ready():
    """2026-08-13: explicit product decision -- DNASMU (pre-day-session
    feed, see config.WS_TR_KEY_EXTENDED) is trusted to drive real entries
    from KRX open (09:00 KST) onward, same as RBAQMU does from 10:00. A
    service started ~07:30 should have WARMUP_MIN_3M_BARS by 09:00 and no
    longer be blocked once WS is fresh and warmed up."""
    now = datetime(2026, 8, 13, 9, 0, tzinfo=KST)  # exactly SESSION_OPEN
    state = state_store.default_state()
    state.ws_connected = True
    state.ws_last_tick_at = now.isoformat()
    state.warmup_bars_3m_count = config.WARMUP_MIN_3M_BARS
    state.warmup_ready = True

    assert worker._entry_gate_block_reason(state, now) is None


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
    # 2026-08-14: starts 2h before KRX open (was 09:00) so both crossings
    # below (~09:33 UP_RED, ~10:21 DOWN_BLUE) land clear of the new
    # 11:00-14:00 MIDDAY_ENTRY_PAUSE window -- this test is about the
    # multi-tick reversal mechanics, not that window.
    start = datetime(2026, 8, 12, 7, 0, tzinfo=KST)
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


def test_quick_profit_take_profit_fires_when_enabled_and_threshold_reached():
    """When quick_profit_enabled is ON, a held position must be closed the
    instant net return reaches QUICK_PROFIT_TAKE_PROFIT_NET_PCT (2.5%),
    independent of MU flag state -- mirrors the Stop Loss exit's own
    'checked every tick, never gated on WS health' behavior."""
    svc = _build_flat_then_ramp_service(now_minutes_total=100)
    now = _now_after(svc)
    svc.ws_connected = False  # exits are never gated on WS health
    svc.ws_last_tick_at = None

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 100, "seed-long")
    from app.trading.mu_macd.models import PositionSnapshot
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    state.quick_profit_enabled = True
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=100, avg_price=10_000.0, entry_at=now - timedelta(minutes=30))
    broker.set_quote(config.LONG_SYMBOL, 10_250.0)  # exactly +2.5% net return

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None
    assert any(a.startswith("QUICK_PROFIT_TAKE_PROFIT") for a in result.actions)
    long_positions = [p for p in broker.get_positions() if p.symbol == config.LONG_SYMBOL and p.quantity > 0]
    assert long_positions == []


def test_quick_profit_take_profit_does_not_fire_when_disabled():
    """Default state has quick_profit_enabled=False -- a position past the
    2.5% threshold must stay held (no auto take-profit) until the user
    explicitly turns the filter on."""
    svc = _build_flat_then_ramp_service(now_minutes_total=100)
    now = _now_after(svc)
    svc.ws_connected = False
    svc.ws_last_tick_at = None

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 100, "seed-long")
    from app.trading.mu_macd.models import PositionSnapshot
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    assert state.quick_profit_enabled is False  # default OFF
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=100, avg_price=10_000.0, entry_at=now - timedelta(minutes=30))
    broker.set_quote(config.LONG_SYMBOL, 10_500.0)  # +5%, well past the threshold

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL
    assert not any(a.startswith("QUICK_PROFIT_TAKE_PROFIT") for a in result.actions)


# ── _do_reconcile ledger corrections (2026-08-13 fix) — a quantity jump
# discovered only via broker reconciliation (never a normal order fill)
# must leave an execution-ledger trace, not silently overwrite state.position
# with no trace. Real incident: a partial-fill BUY's "cancel" didn't
# actually stop the resting KIS limit order, which kept filling in the
# background for ~24 minutes -- state.position silently jumped 110 -> 994
# shares with zero ledger row until this fix. ──────────────────────────────

def test_reconcile_qty_increase_records_ledger_correction_with_implied_price():
    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 8_425.0})
    broker.buy_market(config.INVERSE_SYMBOL, 994, "seed")  # broker already shows the full untracked fill
    state = state_store.default_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=110, avg_price=8_425.0)

    now = datetime(2026, 8, 13, 10, 40, tzinfo=KST)
    result = worker._do_reconcile(broker, state, now, confirm_retries=0)

    assert result == "RECOVERED_QTY_MISMATCH"
    assert state.position.quantity == 994
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["symbol"] == config.INVERSE_SYMBOL
    assert rows[0]["side"] == "BUY"
    assert int(rows[0]["executed_qty"]) == 884
    assert rows[0]["exit_reason"] == "RECONCILE_QTY_INCREASE_UNTRACKED_FILL"
    assert float(rows[0]["executed_price"]) == pytest.approx(8_425.0)


def test_reconcile_position_discovered_while_flat_records_ledger():
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 9_500.0})
    broker.buy_market(config.LONG_SYMBOL, 50, "seed")
    state = state_store.default_state()
    state.position = None

    now = datetime(2026, 8, 13, 11, 0, tzinfo=KST)
    result = worker._do_reconcile(broker, state, now, confirm_retries=0)

    assert result == "RECOVERED_FROM_BROKER"
    assert state.position is not None and state.position.quantity == 50
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY"
    assert rows[0]["exit_reason"] == "RECONCILE_POSITION_DISCOVERED_UNTRACKED"


def test_reconcile_position_vanished_records_ledger_with_blank_price():
    """Broker shows flat but state thought it held a position -- get_positions()
    never tells us a sell price, so the ledger row must leave price blank
    rather than fabricate one (e.g. from the entry price, which would imply
    a false zero P&L)."""
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 9_500.0})
    state = state_store.default_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=100, avg_price=9_000.0)

    now = datetime(2026, 8, 13, 11, 30, tzinfo=KST)
    result = worker._do_reconcile(broker, state, now, confirm_retries=0)

    assert result == "RECOVERED_TO_FLAT"
    assert state.position is None
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["side"] == "SELL"
    assert int(rows[0]["executed_qty"]) == 100
    assert rows[0]["exit_reason"] == "RECONCILE_POSITION_VANISHED_UNTRACKED"
    assert rows[0]["executed_price"] == ""


class _GlitchOnceBroker:
    """Wraps a real FakeBroker but returns a WRONG get_positions() snapshot
    on its first call only, then delegates normally -- simulates a single
    stale/settlement-lagged KIS inquire-balance read."""

    def __init__(self, inner, wrong_positions):
        self._inner = inner
        self._wrong = wrong_positions
        self._calls = 0

    def get_positions(self):
        self._calls += 1
        if self._calls == 1:
            return self._wrong
        return self._inner.get_positions()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_reconcile_transient_mismatch_is_reconfirmed_before_correcting():
    """2026-08-14 real incident regression: a genuinely still-held position
    got recorded as RECONCILE_POSITION_VANISHED_UNTRACKED (no fill price at
    all) from exactly one stale broker.get_positions() read. _do_reconcile
    must re-check before believing a first mismatch enough to overwrite
    state.position / write an untracked-correction ledger row."""
    inner = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 9_500.0})
    inner.buy_market(config.LONG_SYMBOL, 100, "seed")
    broker = _GlitchOnceBroker(inner, wrong_positions=[])  # first read: falsely "flat"
    state = state_store.default_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=100, avg_price=9_000.0)

    now = datetime(2026, 8, 14, 11, 30, tzinfo=KST)
    result = worker._do_reconcile(broker, state, now, confirm_retries=1, confirm_delay_sec=0.0)

    assert result == "MATCH_POSITION"
    assert state.position is not None and state.position.quantity == 100
    assert ledger.load_execution_ledger() == []
