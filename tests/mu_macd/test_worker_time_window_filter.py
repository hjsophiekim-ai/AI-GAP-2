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


def _seed_tw_held_since(state, *, symbol: str, entered_at: datetime, last_bar_close: float) -> None:
    """Simulates a TW-managed position entered two completed 3-minute bars
    ago, whose most recently completed bar closed at ``last_bar_close`` --
    so the very next run_once() call (at ``entered_at`` + 6 minutes or later)
    evaluates that close as eligible (strictly after the entry bar). Mirrors
    tests/macd2/test_worker_held_position_risk_management_warmup.py's own
    ``_seed_held_since`` exactly, for worker._advance_time_window_stop_loss_
    bar's mu_macd-namespaced fields (2026-08-18 fix)."""
    entry_bar_start, _ = worker.forming_bar_window(entered_at)
    last_bar_start = entry_bar_start + timedelta(minutes=3)
    state.time_window_stop_loss_bar_symbol = symbol
    state.time_window_stop_loss_entry_bar_ts = entry_bar_start.isoformat()
    state.time_window_stop_loss_bar_ts = last_bar_start.isoformat()
    state.time_window_stop_loss_bar_close = last_bar_close


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

    # 2026-08-18 fix: the ladder now requires a COMPLETED 3m bar close past
    # the entry bar (not a single live tick) -- seed it two bars into the
    # past (mirrors tests/macd2/test_worker_held_position_risk_management_
    # warmup.py's own _seed_held_since exactly), so this one run_once() call
    # evaluates it immediately.
    entered_at = now0 - timedelta(minutes=9)
    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=200.0, entry_at=entered_at)
    state.time_window_position_active = True
    _seed_tw_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at, last_bar_close=100.0)

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

    # 2026-08-18 fix: seed a completed bar two bars into the past (macd_snap
    # stays None throughout -- zero ticks ever injected into svc -- proving
    # the completed-bar stop-loss tracker is independent of bars_3m/macd_snap
    # readiness, same as before this fix).
    entered_at = now0 - timedelta(minutes=9)
    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=200.0, entry_at=entered_at)
    state.time_window_position_active = True
    _seed_tw_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at, last_bar_close=100.0)

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

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 1_030.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")

    # 2026-08-18 fix: seed a completed bar (closed at the TP1-crossing price)
    # two bars into the past so this one run_once() call evaluates it.
    entered_at = now0 - timedelta(minutes=9)
    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=1_000.0, entry_at=entered_at)
    state.time_window_position_active = True
    _seed_tw_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at, last_bar_close=1_030.0)  # +3.0% net return -- exactly TP1

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

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 1_050.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 5, "seed-order")  # remaining half after an earlier TP1

    # 2026-08-18 fix: seed a completed bar (closed at the TP2-crossing price)
    # two bars into the past so this one run_once() call evaluates it.
    entered_at = now0 - timedelta(minutes=9)
    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=5, avg_price=1_000.0, entry_at=entered_at)
    state.time_window_position_active = True
    state.time_window_tp1_done = True
    state.time_window_peak_net_return = 2.5
    _seed_tw_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at, last_bar_close=1_050.0)  # +5.0% net return -- exactly TP2

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any("MU_MACD_TW_TP2_FULL" in a for a in result.actions)
    assert state.position is None
    assert state.time_window_position_active is False
    assert state.time_window_tp1_done is False
    assert state.time_window_peak_net_return == 0.0


def test_tw_take_profit_fires_on_the_live_tick_not_only_at_bar_close():
    """2026-08-21 user request (real incident found in macd2, same shared
    ladder module): take-profit must fire the instant a live tick crosses
    TP1/TP2, not only once a 3-minute bar has fully closed. No completed
    bar is seeded here at all (_seed_tw_held_since is never called) -- the
    entry-bar self-seed makes _advance_time_window_stop_loss_bar return
    None this tick, so under the OLD code nothing would fire yet."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = MUMarketDataService(mode="mock")
    now0 = datetime(2026, 8, 18, 9, 1, tzinfo=KST)

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 1_060.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")

    entered_at = now0
    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=1_000.0, entry_at=entered_at)
    state.time_window_position_active = True

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any("MU_MACD_TW_TP2_FULL" in a for a in result.actions), (
        f"+6% live tick must trigger TP2 immediately -- got actions={result.actions!r}"
    )
    assert state.position is None


def test_tw_untracked_held_position_is_adopted_and_still_gets_take_profit():
    """2026-08-21 real incident: a position opened through a path that never
    sets time_window_position_active (e.g. manual_entry) got zero take-
    profit/stop-loss coverage for as long as it was held. With the TW
    filter enabled, any held position for the traded symbol must be
    adopted into management on the very next tick."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = MUMarketDataService(mode="mock")
    now0 = datetime(2026, 8, 18, 9, 1, tzinfo=KST)

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 1_060.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=1_000.0, entry_at=now0)
    assert state.time_window_position_active is False  # never tagged

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any("MU_MACD_TW_TP2_FULL" in a for a in result.actions), (
        f"an untracked-but-held position must be adopted and take-profit immediately -- "
        f"got actions={result.actions!r}"
    )
    assert state.position is None


def test_tw_stop_loss_ignores_a_momentary_spike_that_recovers_within_the_same_bar():
    """2026-08-18 real incident: MU_MACD bought 0197X0 on a confirmed BLUE
    flag, then 하이닉스 briefly rebounded (a normal countermove) before
    resuming its real (large, favorable) move -- the position was stopped
    out DURING that brief countermove and missed the payoff entirely,
    because the ladder used to judge STOP_LOSS off every single live tick
    with no smoothing. This is the exact whipsaw macd2's OWN time-window
    ladder already avoided via a completed-bar-close requirement (see
    app.trading.macd2.worker._advance_stop_loss_bar's own docstring) --
    MU_MACD's copy of the ladder had never been given the same protection.
    Proves the fix: a spike that touches -2.0% mid-bar but the bar's LAST
    tick recovers to a safe price must NOT fire STOP_LOSS."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = MUMarketDataService(mode="mock")
    entered_at = datetime(2026, 8, 18, 9, 54, tzinfo=KST)

    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes={config.LONG_SYMBOL: 1_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=1_000.0, entry_at=entered_at)
    state.time_window_position_active = True

    # Tick 1: establishes the entry bar (defensive-fallback seed) -- excluded
    # from its own evaluation by design.
    worker.run_once(broker=broker, market_data=svc, state=state, now=entered_at)

    # Tick 2: rolls into the NEXT bar (bar "N+1"); bar N (the entry bar) is
    # evaluated here but excluded (it IS the entry bar) -- this call also
    # starts tracking bar N+1's own forming close.
    worker.run_once(broker=broker, market_data=svc, state=state, now=entered_at + timedelta(minutes=3))
    assert state.position is not None

    # Tick 3: still bar N+1 -- a momentary spike well past -1.5% stop-loss.
    broker.set_quote(config.LONG_SYMBOL, 980.0)  # -2.0%
    worker.run_once(broker=broker, market_data=svc, state=state, now=entered_at + timedelta(minutes=3, seconds=30))
    assert state.position is not None  # bar N+1 not completed yet -- no evaluation this tick

    # Tick 4: still bar N+1 -- price recovers before the bar closes. Bar
    # N+1's own recorded "close" is now this LAST value (998.0), not the
    # -2.0% spike that briefly touched in between.
    broker.set_quote(config.LONG_SYMBOL, 998.0)  # -0.2% -- comfortably safe
    worker.run_once(broker=broker, market_data=svc, state=state, now=entered_at + timedelta(minutes=3, seconds=90))
    assert state.position is not None

    # Tick 5: bar N+2 -- evaluates bar N+1's completed close (998.0, safe).
    # Must NOT fire STOP_LOSS despite the -2.0% mid-bar spike in tick 3.
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=entered_at + timedelta(minutes=6))
    assert not any("MU_MACD_TW_STOP_LOSS" in a for a in result.actions)
    assert state.position is not None
    assert state.position.quantity == 10


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
    broker.set_quote(config.LONG_SYMBOL, 982.0)  # -1.8% net return -- past MORNING_STOP_LOSS (-1.7%)

    # 2026-08-18 fix: seed a completed bar (closed at the stop-loss-crossing
    # price) two bars into the past so this one run_once() call evaluates it.
    entered_at = now0 - timedelta(minutes=9)
    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=1_000.0, entry_at=entered_at)
    state.time_window_position_active = True
    _seed_tw_held_since(state, symbol=config.LONG_SYMBOL, entered_at=entered_at, last_bar_close=982.0)

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


# ── "TW 1 blue" 예외진입 (2026-08-19) -- ported from app.trading.macd2's own
# down_blue_exception_filter_enabled with identical conditions/logic. The
# flat-then-ramp fixture only ever crosses UP_RED naturally, so these tests
# manually prime state.time_window_pending_flag_direction to DOWN_BLUE
# (bypassing a real crossover) and resolve it on a later tick where the
# fixture's OWN natural confirmed_direction is HOLD (no real crossover there
# to clobber the manually-primed candidate) -- mirrors tests/macd2/
# test_worker_time_window.py's own _prime_pending technique.
def _rejected_decision(block_reason: str = "REJECT_LOW_QUALITY_SCORE"):
    from app.trading.macd2.models import MajorFlagDecision

    return MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=block_reason,
        reasons=(block_reason,), component_scores={}, metrics={}, is_reversal=False,
        fast_reversal=False, block_reason=block_reason,
    )


def _prime_down_blue_pending(state, *, resolve_at) -> None:
    """Primes a pending DOWN_BLUE candidate whose flag bar_dt is exactly one
    completed 3m bar before ``resolve_at``'s own completed bar_dt (this
    fixture's minute grid means bar_dt(T) == T - 3min, so the bar strictly
    BEFORE that one starts 6 minutes before T) -- calling
    run_once(..., now=resolve_at) resolves it on that exact call, matching
    _advance_time_window_filter's own "macd_snap.bar_dt == flag_bar_dt: wait"
    check. Also seeds state.session_date to resolve_at's own date -- a fresh
    state's session_date is None, and run_once's FIRST call is always
    _apply_day_rollover(state, now), which treats None as "new day" and
    wipes time_window_pending_flag_direction/_bar_ts right back out before
    ever reaching _advance_time_window_filter."""
    state.session_date = resolve_at.astimezone(KST).strftime("%Y%m%d")
    state.time_window_pending_flag_direction = Direction.DOWN_BLUE.value
    state.time_window_pending_flag_bar_ts = (resolve_at - timedelta(minutes=6)).isoformat()


def test_down_blue_exception_off_by_default_leaves_rejected_flag_filtered_out(monkeypatch):
    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    now1 = now0 + timedelta(minutes=3)
    svc.ws_connected = True
    svc.ws_last_tick_at = now1

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = _fresh_state_with_tw_enabled()
    assert state.down_blue_exception_filter_enabled is False  # default OFF
    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision())
    _prime_down_blue_pending(state, resolve_at=now1)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)

    assert any(a.startswith("TW_REJECTED:DOWN_BLUE") for a in result.actions)
    assert not any(a.startswith("TW_ENTRY_VIA_DOWN_BLUE_EXCEPTION") for a in result.actions)
    assert broker.orders == []
    assert state.daily_down_blue_exception_used is False


def test_down_blue_exception_on_enters_a_rejected_down_blue_flag_once(monkeypatch):
    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    now1 = now0 + timedelta(minutes=3)
    svc.ws_connected = True
    svc.ws_last_tick_at = now1

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = _fresh_state_with_tw_enabled()
    state.down_blue_exception_filter_enabled = True
    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision())
    _prime_down_blue_pending(state, resolve_at=now1)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)

    assert any(a.startswith("TW_ENTRY_VIA_DOWN_BLUE_EXCEPTION:DOWN_BLUE") for a in result.actions)
    assert state.daily_down_blue_exception_used is True
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL

    # a SECOND rejected DOWN_BLUE candidate the same day must NOT fire again
    now2 = now1 + timedelta(minutes=3)
    svc.ws_last_tick_at = now2
    _prime_down_blue_pending(state, resolve_at=now2)
    result2 = worker.run_once(broker=broker, market_data=svc, state=state, now=now2)
    assert not any(a.startswith("TW_ENTRY_VIA_DOWN_BLUE_EXCEPTION") for a in result2.actions)


def test_down_blue_exception_never_applies_to_up_red(monkeypatch):
    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    now1 = now0 + timedelta(minutes=3)
    svc.ws_connected = True
    svc.ws_last_tick_at = now1

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    state = _fresh_state_with_tw_enabled()
    state.down_blue_exception_filter_enabled = True
    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision())
    state.session_date = now1.astimezone(KST).strftime("%Y%m%d")
    state.time_window_pending_flag_direction = Direction.UP_RED.value
    state.time_window_pending_flag_bar_ts = (now1 - timedelta(minutes=6)).isoformat()

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)

    assert any(a.startswith("TW_REJECTED:UP_RED") for a in result.actions)
    assert not any(a.startswith("TW_ENTRY_VIA_DOWN_BLUE_EXCEPTION") for a in result.actions)
    assert broker.orders == []
    assert state.daily_down_blue_exception_used is False


def test_down_blue_exception_never_overrides_an_already_open_position(monkeypatch):
    """Per design, the exception only ever fires while flat -- it never
    switches/overrides a position the real TW gate already opened."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    now1 = now0 + timedelta(minutes=3)
    svc.ws_connected = True
    svc.ws_last_tick_at = now1

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.LONG_SYMBOL, 1, "seed-order")
    state = _fresh_state_with_tw_enabled()
    state.down_blue_exception_filter_enabled = True
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=1, avg_price=15_000.0, entry_at=now0)
    state.time_window_position_active = True
    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision())
    _prime_down_blue_pending(state, resolve_at=now1)

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)

    assert not any(a.startswith("TW_ENTRY_VIA_DOWN_BLUE_EXCEPTION") for a in result.actions)
    assert state.daily_down_blue_exception_used is False


def test_day_rollover_resets_down_blue_exception_daily_flag_but_not_toggle():
    state = state_store.default_state()
    state.down_blue_exception_filter_enabled = True
    state.session_date = "20260105"
    state.daily_down_blue_exception_used = True

    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 0, tzinfo=KST))

    assert state.daily_down_blue_exception_used is False
    assert state.down_blue_exception_filter_enabled is True  # the toggle itself survives rollover


def test_default_state_has_down_blue_exception_off():
    state = state_store.default_state()
    assert state.down_blue_exception_filter_enabled is False


# ── 반대신호 청산 T+3 재확인 ("휩쏘-내성", 2026-08-19) -- ported from
# app.trading.macd2's own identical worker._resolve_time_window_candidate
# whipsaw branch, so both modules' opposite-signal exit behavior never
# diverges. config.TW_WHIPSAW_REJECT_REASONS is imported straight from
# macd2.config (see mu_macd/config.py), never redefined here.
import pytest  # noqa: E402

from app.trading.macd2 import config as macd2_config  # noqa: E402


@pytest.mark.parametrize(
    "whipsaw_reason", [macd2_config.TW_REJECT_NOT_CONFIRMED, macd2_config.TW_REJECT_MACD_GAP_NOT_EXPANDING],
)
def test_whipsaw_classified_reversal_holds_instead_of_selling(monkeypatch, whipsaw_reason):
    """A rejected REVERSAL candidate (opposite direction vs a held position)
    whose block_reason is a whipsaw reason must leave the held position
    completely untouched -- no sell, no new entry."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)  # this fixture's flag direction is UP_RED (LONG_SYMBOL)
    now1 = now0 + timedelta(minutes=3)
    svc.ws_connected = True
    svc.ws_last_tick_at = now1

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-inverse")  # opposite-direction holding
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision(whipsaw_reason))
    _prime_down_blue_pending(state, resolve_at=now1)  # helper is direction-agnostic despite its name
    state.time_window_pending_flag_direction = Direction.UP_RED.value  # override to the reversal direction

    orders_before = len(broker.orders)
    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)

    assert any(a.startswith("TW_WHIPSAW_HOLD:UP_RED") for a in result.actions), (
        f"a {whipsaw_reason} rejection must be classified a whipsaw and hold, not sell -- got actions={result.actions!r}"
    )
    assert not any(a.startswith("TW_REJECTED_SELL_ONLY") for a in result.actions)
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert state.time_window_position_active is True
    assert len(broker.orders) == orders_before, "no order should be placed for a whipsaw-classified rejection"


def test_rejected_reversal_for_a_non_whipsaw_reason_still_sells_the_held_position(monkeypatch):
    """A rejected REVERSAL candidate for any OTHER reason (quality score,
    time window, max entries, duplicate position) must still fully
    liquidate the held opposite position -- unchanged from before this
    feature, only the whipsaw reasons are new exceptions."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = _build_flat_then_ramp_service()
    now0 = _find_crossing_now(svc)
    now1 = now0 + timedelta(minutes=3)
    svc.ws_connected = True
    svc.ws_last_tick_at = now1

    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-inverse")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision("REJECT_LOW_QUALITY_SCORE"))
    _prime_down_blue_pending(state, resolve_at=now1)
    state.time_window_pending_flag_direction = Direction.UP_RED.value

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now1)

    assert any(a.startswith("TW_REJECTED_SELL_ONLY:UP_RED") for a in result.actions), (
        f"a non-whipsaw rejection must still fully liquidate the held position -- got actions={result.actions!r}"
    )
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.time_window_position_active is False


def test_stop_loss_still_fires_while_a_whipsaw_reversal_candidate_is_pending(monkeypatch):
    """SL(-1.7%)/TP1/TP2/trailing stop must keep firing normally even when a
    T+3 reversal candidate is pending resolution this same tick, and even
    when that candidate would resolve as a whipsaw-hold. Production ordering
    (_advance_time_window_position_management runs before
    _advance_time_window_filter and returns early on a fired exit)
    guarantees this -- mirrors tests/macd2/test_worker_time_window.py's own
    identical parity test."""
    from app.trading.mu_macd.models import PositionSnapshot

    svc = MUMarketDataService(mode="mock")
    now0 = datetime(2026, 8, 18, 9, 1, tzinfo=KST)

    quotes = {config.LONG_SYMBOL: 100.0, config.INVERSE_SYMBOL: 9_700.0}
    broker = FakeBroker(cash=config.DEFAULT_BUDGET, quotes=quotes)
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0  # -3% vs the 9_700.0 quote

    entered_at = now0 - timedelta(minutes=9)
    state = _fresh_state_with_tw_enabled()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=entered_at)
    state.time_window_position_active = True
    _seed_tw_held_since(state, symbol=config.INVERSE_SYMBOL, entered_at=entered_at, last_bar_close=9_700.0)

    # A pending reversal candidate exists this very tick, primed to resolve
    # as a whipsaw-hold if it were ever reached.
    monkeypatch.setattr(worker.twf, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision(macd2_config.TW_REJECT_NOT_CONFIRMED))
    _prime_down_blue_pending(state, resolve_at=now0)
    state.time_window_pending_flag_direction = Direction.UP_RED.value

    result = worker.run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any("MU_MACD_TW_STOP_LOSS" in a for a in result.actions), (
        f"the TW ladder's -1.7% stop-loss must fire even with a pending whipsaw-classified "
        f"reversal candidate this same tick -- got actions={result.actions!r}"
    )
    assert not any("WHIPSAW_HOLD" in a for a in result.actions)
    assert state.position is None
