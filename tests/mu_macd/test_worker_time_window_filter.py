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
from app.trading.mu_macd.market_data import MUMarketDataService
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


def test_tw_stop_loss_fires_even_when_warmup_is_not_ready():
    """Regression test for the 2026-08-15 safety gap: a TW-managed position
    must have its stop-loss monitored EVERY tick, even during a period where
    bars_3m/macd_snap are not yet ready (e.g. immediately after a process
    restart -- this module's own market feed always starts cold, with no
    historical backfill, per config.WARMUP_MIN_3M_BARS's own comment).

    Before the fix, the ladder check lived entirely inside
    _advance_time_window_filter, which run_once() only reaches AFTER
    macd_snap is computed -- so run_once() returned early with
    skipped="NOT_READY" and a real loss went completely unmonitored. This
    test uses a totally FRESH MUMarketDataService (zero ticks injected) so
    macd_snap is guaranteed None, proving the ladder still fires."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = MUMarketDataService(mode="mock")  # no ticks at all -> macd_snap is None
    now0 = datetime(2026, 8, 18, 9, 1, tzinfo=KST)

    quotes = {config.LONG_SYMBOL: 100.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")
    broker._positions[config.LONG_SYMBOL].avg_price = 200.0  # -50% vs the 100.0 quote, well past -1.5%

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=200.0, entry_at=now0)
    state.time_window_position_active = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any("MU_MACD_TW_STOP_LOSS" in a for a in result.actions)
    assert state.position is None
    assert state.time_window_position_active is False
    # The ladder fired and returned BEFORE ever reaching the warm-up gate.
    assert result.skipped != "NOT_READY"


def test_tw_tp1_partial_fires_at_3pt0_percent_and_leaves_position_active():
    """TP1: +3.0% sells 50% of the held quantity, keeps the remaining
    position under this filter's management (tp1_done=True, position still
    active) rather than a full exit."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = MUMarketDataService(mode="mock")
    now0 = datetime(2026, 8, 18, 9, 1, tzinfo=KST)

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 1_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")
    broker.set_quote(config.LONG_SYMBOL, 1_030.0)  # +3.0% net return -- exactly TP1

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=1_000.0, entry_at=now0)
    state.time_window_position_active = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any("MU_MACD_TW_TP1_PARTIAL" in a for a in result.actions)
    assert state.position is not None
    assert state.position.quantity == 5  # 50% of 10, per MORNING_TP1_SELL_RATIO
    assert state.position.avg_price == 1_000.0  # cost basis unchanged by a partial sell
    assert state.time_window_tp1_done is True
    assert state.time_window_position_active is True  # still managed -- not a full exit


def test_tw_tp2_fires_full_exit_after_tp1_already_done():
    """TP2: once tp1_done is already True (a prior tick sold the TP1 half),
    reaching +5.0% must sell the ENTIRE remaining quantity and fully reset
    this filter's per-position tracking state."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = MUMarketDataService(mode="mock")
    now0 = datetime(2026, 8, 18, 9, 1, tzinfo=KST)

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 1_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 5, "seed-order")  # remaining half after an earlier TP1
    broker.set_quote(config.LONG_SYMBOL, 1_050.0)  # +5.0% net return -- exactly TP2

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=5, avg_price=1_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_tp1_done = True
    state.time_window_peak_net_return = 2.5

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any("MU_MACD_TW_TP2_FULL" in a for a in result.actions)
    assert state.position is None
    assert state.time_window_position_active is False
    assert state.time_window_tp1_done is False
    assert state.time_window_peak_net_return == 0.0


def test_tw_stop_loss_still_fires_normally_once_warmup_is_ready():
    """Confirms the fix didn't just move the bug -- the SAME ladder also
    still fires correctly on a normal, fully-warmed-up tick (not just the
    cold-start edge case above)."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now0

    quotes = {config.LONG_SYMBOL: 1_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")
    broker.set_quote(config.LONG_SYMBOL, 984.0)  # -1.6% net return -- past MORNING_STOP_LOSS (-1.5%)

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=1_000.0, entry_at=now0)
    state.time_window_position_active = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(seconds=1))

    assert any("MU_MACD_TW_STOP_LOSS" in a for a in result.actions)
    assert state.position is None
    assert state.time_window_position_active is False


def test_full_day_dry_run_forces_liquidation_by_close_with_no_exceptions():
    """Full-day tick loop (09:00 -> 15:00, 1-minute steps) with the TW filter
    ON throughout, using the SAME flat-then-ramp fixture as every other test
    in this file. Verifies: (1) run_once never raises across a whole
    session, (2) any position still open is force-liquidated by
    config.FORCE_LIQUIDATE_AT (15:00) and never carries past it, (3) the
    session ends completely flat."""
    svc = _build_flat_then_ramp_service(now_minutes_total=600, start=datetime(2026, 8, 18, 7, 0, tzinfo=KST))
    svc.ws_connected = True

    quotes = {config.LONG_SYMBOL: 1_000.0, config.INVERSE_SYMBOL: 1_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = _fresh_state_with_tw_enabled()

    day_start = datetime(2026, 8, 18, 9, 0, tzinfo=KST)
    for minute in range(0, 361):  # 09:00 through 15:00 inclusive
        now = day_start + timedelta(minutes=minute)
        svc.ws_last_tick_at = now
        # A slow drift on both ETF quotes so stop-loss/TP thresholds get
        # exercised organically over the session, without hand-tuning a
        # specific price at a specific minute.
        drift = 1.0 + 0.0006 * minute
        broker.set_quote(config.LONG_SYMBOL, 1_000.0 * drift)
        broker.set_quote(config.INVERSE_SYMBOL, 1_000.0 / drift)
        worker.run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.position is None
    assert state.time_window_position_active is False
    assert config.LONG_SYMBOL not in broker._positions
    assert config.INVERSE_SYMBOL not in broker._positions


def _force_approved_decision(direction: Direction, *, window: str = "WINDOW1"):
    """Builds a MajorFlagDecision that always approves, for monkeypatching
    twf.evaluate_time_window_entry -- isolates these entry_paused wiring
    tests from the real quality-score/gap-expansion decision logic (already
    covered elsewhere), so a flaky synthetic ramp can't make them approve on
    one run and reject on another."""
    from app.trading.macd2.models import MajorFlagDecision

    return MajorFlagDecision(
        approved=True, score=10.0, required_score=0.0, decision="TW_APPROVED",
        reasons=(), component_scores={}, metrics={"window": window},
        is_reversal=False, fast_reversal=False, block_reason=None,
    )


def test_entry_paused_blocks_flat_new_entry_even_when_tw_filter_is_on(monkeypatch):
    """신규진입 일시정지 must block a flat (no-position) new entry under the
    TW filter exactly like it already does under the legacy path -- this is
    the 2026-08-15 fix: previously state.entry_paused was never even
    checked inside _advance_time_window_filter."""
    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **k: _force_approved_decision(Direction.UP_RED))

    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    svc.ws_connected = True
    svc.ws_last_tick_at = now0

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = _fresh_state_with_tw_enabled()
    state.entry_paused = True

    worker.run_once(broker=broker, market_data=svc, state=state, now=now0)  # -> TW_PENDING
    now1 = now0 + timedelta(minutes=3)
    svc.ws_last_tick_at = now1
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)  # -> candidate resolution

    assert any(a.startswith("TW_ENTRY_PAUSED:") for a in result.actions)
    assert broker.orders == []
    assert state.position is None


def test_entry_paused_still_sells_opposite_held_position_when_tw_filter_is_on(monkeypatch):
    """Mirrors the legacy entry_paused semantics exactly (see its own UI
    help text: "반대 플래그가 뜨면 보유 포지션은 그대로 매도됩니다") -- an opposite-
    direction position already held under the TW filter must still be SOLD
    on a fresh confirmed opposite flag even while new entries are paused;
    only the follow-up re-buy is skipped."""
    from app.trading.mu_macd.models import PositionSnapshot

    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **k: _force_approved_decision(Direction.UP_RED))

    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)  # this fixture's flag direction is UP_RED (LONG_SYMBOL)
    svc.ws_connected = True
    svc.ws_last_tick_at = now0

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-inverse")  # opposite-direction holding

    state = _fresh_state_with_tw_enabled()
    state.entry_paused = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True

    worker.run_once(broker=broker, market_data=svc, state=state, now=now0)  # -> TW_PENDING
    now1 = now0 + timedelta(minutes=3)
    svc.ws_last_tick_at = now1
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)  # -> resolves: sell-only

    assert any(a.startswith("TW_ENTRY_PAUSED_SELL_ONLY:") for a in result.actions)
    assert state.position is None  # opposite holding was sold
    assert state.time_window_position_active is False
    assert config.INVERSE_SYMBOL not in broker._positions
    assert config.LONG_SYMBOL not in broker._positions  # paused -- no re-buy happened
