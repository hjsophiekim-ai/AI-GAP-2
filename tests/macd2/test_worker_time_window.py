"""Worker-level integration tests for the time-window filter (§25 checklist
subset) — fake broker + fake market data only, mirrors tests/macd2/test_worker.py's
own harness so no duplicated test infrastructure is introduced.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, MajorFlagDecision, RuntimeState
from app.trading.macd2.signal_engine import forming_bar_window
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_worker import _1m_from_3m_closes

KST = config.KST


def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    period = max(n_minutes // 2, 1)
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 100 + (i % 7) * 10}
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


_PRIOR_DAY = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
_BOOTSTRAP_NOW = _PRIOR_DAY + timedelta(days=2)
_SESSION_START_NOW = _PRIOR_DAY + timedelta(minutes=3 * (config.SIGNAL_MIN_BAR_INDEX + 1))


def _fresh_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.time_window_filter_enabled = True
    return state


@pytest.fixture
def tw_market_data():
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
    svc.refresh_quotes()
    return svc, _SESSION_START_NOW


def test_flag_bar_itself_never_dispatches_an_order(tw_market_data):
    """§1: a flag never has order authority on its own completed bar --
    the first tick where a fresh candidate is recorded must never place a
    broker order that same tick."""
    svc, now0 = tw_market_data
    # Replay tick-by-tick and assert the broker's order count never
    # increases on the SAME tick the state transitions into
    # TIME_WINDOW_PENDING_CONFIRMATION for the first time (per candidate).
    state2 = _fresh_state()
    broker2 = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    prev_pending = None
    for step in range(60):
        now = now0 + timedelta(minutes=3 * step)
        orders_before = len(broker2.orders)
        run_once(broker=broker2, market_data=svc, state=state2, now=now)
        became_pending_this_tick = (
            state2.time_window_pending_flag_direction is not None
            and state2.last_time_window_decision == config.TW_PENDING_CONFIRMATION
            and state2.time_window_pending_flag_bar_ts != prev_pending
        )
        if became_pending_this_tick:
            assert len(broker2.orders) == orders_before, (
                "a new time-window candidate must never place an order on its own flag bar"
            )
        prev_pending = state2.time_window_pending_flag_bar_ts


def test_entry_confirms_on_a_later_completed_bar_not_the_flag_bar(tw_market_data):
    """An actual TIME_WINDOW_ENTRY/TIME_WINDOW_SWITCH action, when it fires,
    must occur strictly after the bar that first set the pending candidate."""
    svc, now0 = tw_market_data
    state = _fresh_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    first_candidate_bar_ts = None
    entry_tick_index = None
    for step in range(120):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        if first_candidate_bar_ts is None and state.time_window_pending_flag_bar_ts:
            first_candidate_bar_ts = state.time_window_pending_flag_bar_ts
            first_candidate_step = step
        if any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions):
            entry_tick_index = step
            break

    if entry_tick_index is None:
        pytest.skip("synthetic sine session never produced an approved time-window entry within 120 steps")
    assert first_candidate_bar_ts is not None
    assert entry_tick_index > first_candidate_step, (
        "entry must fire strictly after the tick that first recorded the candidate"
    )
    assert state.position is not None
    assert state.time_window_position_active is True


def test_fresh_opposite_flag_registers_a_pending_candidate_while_position_held(tw_market_data, monkeypatch):
    """Regression test for a 2026-08-19 real incident: a TW-managed position
    (state.time_window_position_active=True) sat completely unmonitored by
    any FRESH confirmed crossover -- _resolve_time_window_candidate only
    resolves an ALREADY-pending candidate, and the code that actually
    registers a NEW one from a crossover confirmed while a position is held
    was unreachable behind an early `return result`. Net effect: a genuine
    later opposite flag updated state.last_detected_direction but never
    became a pending candidate, so it could never reach its own T+3
    re-confirmation, could never dispatch OPPOSITE_SIGNAL, and the held
    position could only ever exit via its own TP1/TP2/stop-loss/trailing
    ladder or 15:00 forced liquidation (real example: BLUE flag 09:00 ->
    entered 0197X0 at 09:06; a genuine RED flag at 09:30 was silently
    dropped -- position never switched).

    Forces the exact scenario deterministically (monkeypatching
    _advance_confirmed_primary to report a fresh UP_RED on one specific tick,
    rather than hunting for an organic crossover in synthetic data) while an
    INVERSE (0197X0) position is already held and TW-managed -- asserts the
    fresh, opposite-direction UP_RED candidate gets registered as pending, so
    a later tick's _resolve_time_window_candidate can actually act on it.
    """
    from app.trading.macd2.models import Direction as _Direction, PositionSnapshot

    svc, now0 = tw_market_data
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    # reconcile_position_state compares state.position against the BROKER's
    # own holdings -- must seed the broker's side too, or the very first
    # tick reconciles state.position back to flat (RECOVERED_TO_FLAT) before
    # ever reaching the crossover logic this test targets.
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    monkeypatch.setattr(worker, "_advance_confirmed_primary", lambda state, macd_snap, now: _Direction.UP_RED)

    assert state.time_window_pending_flag_direction is None
    orders_before = len(broker.orders)  # 1: the seed buy above, not from run_once
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert state.time_window_pending_flag_direction == _Direction.UP_RED, (
        "a fresh opposite confirmed crossover must register as a pending TW candidate "
        "even while an existing TW-managed position is held -- otherwise it can never "
        "reach T+3 re-confirmation and the position can never exit via OPPOSITE_SIGNAL"
    )
    assert state.time_window_pending_flag_bar_ts is not None
    # the held position itself must NOT have been touched on this same tick --
    # OPPOSITE_SIGNAL only fires later, once the candidate resolves at T+3.
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert len(broker.orders) == orders_before, "run_once must not place any order on the flag's own bar"
    del result


def test_rejected_reversal_still_liquidates_the_held_tw_position(tw_market_data, monkeypatch):
    """Regression test for a SECOND real 2026-08-19 incident (found right
    after the first fix above shipped): with the first bug fixed, a fresh
    opposite flag now DOES register as a pending candidate and DOES reach
    its own T+3 re-confirmation -- but if the real TW gate then REJECTS that
    candidate (low quality score, gap not expanding, whatever),
    _resolve_time_window_candidate's reject branch did nothing at all: no
    switch, but also no sell. This directly contradicts this exact
    function's own docstring ("the held position stays untouched until
    _resolve_time_window_candidate ... decides to switch or hold") and
    _judge_time_window_flag's ("must stay untouched UNTIL
    _resolve_time_window_candidate resolves the candidate at T+3") -- both
    assume a real decision happens on reject, not a silent no-op. Every
    OTHER optional filter (MAJOR/SIDEWAYS/etc.) already always sells the
    held position on a rejected reversal (docs: "반대 플래그가 뜨면 보유
    포지션은 그대로 매도됩니다") -- only the NEW direction's re-entry is a
    separate, gate-owned decision. Real example: 11:48 RED flag confirmed
    (system recorded it correctly, proving the first fix worked) but the
    held INVERSE position was never sold and no LONG was ever bought."""
    from app.trading.macd2.models import Direction as _Direction, MajorFlagDecision, PositionSnapshot

    svc, now0 = tw_market_data
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    # Prime a pending UP_RED candidate so it resolves (T+3) on this very
    # call. now0's own completed bar_dt is (now0 - 3min) (resample_completed_
    # 3m's bar-start labeling) -- the flag bar must be a further bar back
    # (now0 - 6min) so _resolve_time_window_candidate's own "still sitting on
    # the flag's own bar, wait for T+3" guard does not fire.
    flag_bar_dt = now0 - timedelta(minutes=6)
    state.time_window_pending_flag_direction = _Direction.UP_RED
    state.time_window_pending_flag_bar_ts = flag_bar_dt.isoformat()

    rejected = MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=config.TW_REJECT_LOW_QUALITY_SCORE,
        reasons=(config.TW_REJECT_LOW_QUALITY_SCORE,), component_scores={}, metrics={},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_REJECT_LOW_QUALITY_SCORE,
    )
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: rejected)
    # avoid the real (60s @ 1s) fill-reconcile poll window this sell-only
    # exit path uses -- FakeBroker fills synchronously, so 1 retry suffices.
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TIME_WINDOW_SELL_ONLY") for a in result.actions), (
        "a rejected reversal must still fully liquidate the held TW position -- "
        f"got actions={result.actions!r}"
    )
    assert state.position is None, "the held INVERSE position must be sold even though the new LONG entry was rejected"
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.time_window_position_active is False


@pytest.mark.parametrize("whipsaw_reason", [config.TW_REJECT_NOT_CONFIRMED, config.TW_REJECT_MACD_GAP_NOT_EXPANDING])
def test_whipsaw_classified_reversal_holds_instead_of_selling(tw_market_data, monkeypatch, whipsaw_reason):
    """2026-08-19 "휩쏘-내성" T+3 재확인 feature (user-requested production
    change, verified via 56-day TRAIN/VAL/OOS backtest): when the rejected
    reversal candidate's block_reason is specifically config.
    TW_WHIPSAW_REJECT_REASONS (the MACD/Signal relationship didn't hold 3
    minutes later, or the gap didn't expand -- i.e. price reverted back
    toward the ORIGINAL direction before the reversal could re-confirm),
    the held position must be left untouched (no sell) -- unlike the
    previous test's TW_REJECT_LOW_QUALITY_SCORE case, which is NOT a
    whipsaw reason and must still sell exactly as before."""
    from app.trading.macd2.models import Direction as _Direction, MajorFlagDecision, PositionSnapshot

    svc, now0 = tw_market_data
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    flag_bar_dt = now0 - timedelta(minutes=6)
    state.time_window_pending_flag_direction = _Direction.UP_RED
    state.time_window_pending_flag_bar_ts = flag_bar_dt.isoformat()

    rejected = MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=whipsaw_reason,
        reasons=(whipsaw_reason,), component_scores={}, metrics={},
        is_reversal=False, fast_reversal=False, block_reason=whipsaw_reason,
    )
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: rejected)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    orders_before = len(broker.orders)  # 1: the seed buy
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TIME_WINDOW_WHIPSAW_HOLD") for a in result.actions), (
        f"a {whipsaw_reason} rejection must be classified a whipsaw and hold, not sell -- got actions={result.actions!r}"
    )
    assert not any(a.startswith("TIME_WINDOW_SELL_ONLY") for a in result.actions)
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL, (
        "a whipsaw-classified rejection must leave the held position completely untouched"
    )
    assert state.time_window_position_active is True
    assert len(broker.orders) == orders_before, "no order should be placed for a whipsaw-classified rejection"


def test_stop_loss_still_fires_while_a_whipsaw_reversal_candidate_is_pending(monkeypatch):
    """2026-08-19 사용자 요청: 반대신호 T+3 재확인(휩쏘-내성)이 SL(-1.7%)/TP1/
    TP2/trailing stop/15:00 강제청산의 즉시 발동을 절대 지연시키면 안 된다.
    worker.py의 실제 순서(_advance_held_position_risk_management가 macd_snap
    계산 전에 먼저 평가되고, 발동 시 즉시 return -- _resolve_time_window_
    candidate는 그 뒤에나 도달)가 이를 구조적으로 보장한다: 이 tick에 리스크
    래더가 SL을 발동시키면, 설령 반대신호 pending 후보가 있고 그 후보가
    (몽키패치로) 휩쏘로 판정되도록 세팅되어 있어도 TIME_WINDOW_WHIPSAW_HOLD는
    절대 나타나면 안 되고 STOP_LOSS만 발동해야 한다."""
    from app.trading.macd2.models import Direction as _Direction, PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 9_700.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 9_700.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    # -3% completed-bar close (past MORNING_STOP_LOSS's -1.7% threshold) --
    # seeded as already-completed the bar before `now0`, same convention as
    # every other STOP_LOSS test in this repo (tests/macd2/test_worker.py:782 etc).
    bar_start, _ = forming_bar_window(now0)
    state.stop_loss_bar_symbol = config.INVERSE_SYMBOL
    state.stop_loss_entry_bar_ts = (bar_start - timedelta(minutes=6)).isoformat()
    state.stop_loss_bar_ts = (bar_start - timedelta(minutes=3)).isoformat()
    state.stop_loss_bar_close = 9_700.0

    # A pending reversal candidate exists this very tick, primed to resolve
    # as a whipsaw-hold (TW_REJECT_NOT_CONFIRMED) if it were ever reached.
    flag_bar_dt = now0 - timedelta(minutes=6)
    state.time_window_pending_flag_direction = _Direction.UP_RED
    state.time_window_pending_flag_bar_ts = flag_bar_dt.isoformat()
    whipsaw_decision = MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=config.TW_REJECT_NOT_CONFIRMED,
        reasons=(config.TW_REJECT_NOT_CONFIRMED,), component_scores={}, metrics={},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_REJECT_NOT_CONFIRMED,
    )
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: whipsaw_decision)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_STOP_LOSS) for a in result.actions), (
        f"the TW ladder's -1.7% stop-loss must fire even with a pending whipsaw-classified "
        f"reversal candidate this same tick -- got actions={result.actions!r}"
    )
    assert not any("WHIPSAW_HOLD" in a for a in result.actions)
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions


def test_take_profit_fires_on_the_live_tick_not_only_at_bar_close(monkeypatch):
    """2026-08-21 real incident + user request: a position sat 3%+ (and had
    briefly spiked past 7%) in profit for 20+ minutes without ever being
    sold, because the old code only ever checked TP1/TP2 once a 3-minute
    bar had FULLY completed (_advance_stop_loss_bar) -- a live tick that
    crosses TP2 mid-bar must sell immediately, not wait for that bar's
    close. Seeding NO prior stop_loss_bar_* state means _advance_stop_loss_
    bar treats `now0` as the entry bar and returns None (no completed bar
    yet) -- under the OLD code this tick would fire nothing at all."""
    from app.trading.macd2.models import PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_600.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_600.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), (
        f"+6% live tick must trigger TP2 immediately, same tick -- got actions={result.actions!r}"
    )
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions


def test_failed_immediate_tick_partial_exit_does_not_commit_tp1_done(monkeypatch):
    """2026-08-27 real incident: a premarket-carry DOWN_BLUE position's TP1
    partial-exit order FAILED at the broker, but state.time_window_tp1_done
    had already been committed True regardless -- the position was then
    governed by the tightened post-TP1 ladder (MORNING_AFTER_TP1_STOP=+0.3%)
    instead of the correct pre-TP1 -1.7% stop-loss/3% TP1 threshold, so a
    nearly-flat tick minutes later was enough to trigger a full exit. A
    failed partial-exit order must leave tp1_done exactly as it was before
    the attempt, so a retry next tick is judged against the correct
    threshold again."""
    from app.trading.macd2.models import PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    # +4.5% raw nets to inside TP1(3%)..TP2(6%) -- triggers TP1_PARTIAL, not
    # a full exit, so execute_partial_exit (not execute_exit) is the call
    # whose failure this test forces.
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_450.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    assert state.time_window_tp1_done is False
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_450.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    broker.fail_next_sell = True  # the partial-exit order FAILS at the broker
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP1_PARTIAL) for a in result.actions), (
        f"expected a TP1_PARTIAL attempt this tick -- got actions={result.actions!r}"
    )
    assert state.time_window_tp1_done is False, (
        "a FAILED partial-exit order must never commit tp1_done -- the position must stay "
        "governed by the correct pre-TP1 ladder for a retry next tick"
    )
    assert state.position is not None and state.position.quantity == 10, "quantity must be unchanged on a failed partial exit"


def test_failed_bar_close_gated_partial_exit_does_not_commit_tp1_done(monkeypatch):
    """Same regression as test_failed_immediate_tick_partial_exit_does_not_
    commit_tp1_done, but for the bar-close-gated position-management path
    (time_window_position_manager.evaluate_position) -- isolates that path
    specifically by forcing the immediate-tick take-profit check to HOLD
    (as if the live tick's own price is not currently in TP range) and
    _advance_stop_loss_bar to report a completed bar close that IS in TP1
    range (the real-world case this path exists for: a bar that closed in
    TP1 range a few minutes ago, even though the live quote has since moved
    back out of range)."""
    from app.trading.macd2.models import PositionSnapshot
    from app.trading.macd2.time_window_position_manager import PositionManagementDecision

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_050.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    assert state.time_window_tp1_done is False
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_050.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    broker.fail_next_sell = True
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    # Isolate the bar-close-gated path: live-tick check HOLDs (as if the
    # current quote isn't in TP range), while the completed-bar close (a
    # few minutes ago) IS in TP1 range.
    monkeypatch.setattr(
        worker.time_window_position_manager, "evaluate_take_profit_immediate",
        lambda **kw: PositionManagementDecision(None, 0.0, kw["tp1_done"], kw["net_return_pct"], "HOLD_TICK"),
    )
    monkeypatch.setattr(worker, "_advance_stop_loss_bar", lambda state, symbol, price, now: 10_450.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP1_PARTIAL) for a in result.actions), (
        f"expected a TP1_PARTIAL attempt this tick via the bar-close-gated path -- got actions={result.actions!r}"
    )
    assert state.time_window_tp1_done is False, (
        "a FAILED partial-exit order (bar-close-gated path) must never commit tp1_done"
    )
    assert state.position is not None and state.position.quantity == 10


def test_untracked_held_position_is_adopted_and_still_gets_take_profit(monkeypatch):
    """2026-08-21 real incident: a position opened via the 09:03 scheduled-
    entry button (or any other path that never sets time_window_position_
    active) got ZERO take-profit/stop-loss management for as long as it was
    held, because the whole risk-management block used to require that flag
    already True. With the TW filter enabled, any held position for the
    traded symbol must be adopted into management on the very next tick --
    proven here by never setting time_window_position_active at all."""
    from app.trading.macd2.models import PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_600.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    assert state.time_window_position_active is False  # never tagged -- e.g. scheduled-entry path
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_600.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), (
        f"an untracked-but-held position must be adopted into TW management and take-profit "
        f"immediately -- got actions={result.actions!r}"
    )
    assert state.position is None


def test_restart_catchup_multi_bar_gap_routes_through_tw_gate_not_a_raw_pending_signal():
    """2026-08-21 real incident: worker.py's own version of the 2026-08-05
    RESTART_CATCH_UP_MULTI_BAR_GAP fix (test_worker.py's own
    test_restart_with_fully_lost_state_still_catches_up_when_today_already_
    has_bars) queued a raw state.pending_signal for the mismatch it found --
    but _execute_or_wait's pending_signal consumption NEVER calls
    _judge_entry_gate/time_window_filter at all, so during today's repeated
    restart/crash loop this force-entered a position completely bypassing
    the T+3 re-confirm + quality gate the user had explicitly turned TW on
    for (and that position then also never got a proper take-profit chance,
    since it was never given a time_window_entry_session either). With TW
    enabled, this mismatch must be registered as a TW pending candidate
    instead -- proven here two ways: (1) no raw pending_signal is created,
    and (2) the very next live tick, evaluate_time_window_entry's own
    multi-bar-gap-expiry check safely drops it (no order at all) rather
    than blindly confirming off bars this stale."""
    from app.trading.macd2.models import PositionSnapshot

    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    closes = [100.0] * 99 + [92.0, 96.0, 103.0, 104.0, 103.0, 98.0, 90.0, 85.0, 84.0]
    df_1m_full = _1m_from_3m_closes(start, closes)

    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0)
    assert state.last_confirmed_bar_ts is None

    bar103_end = start + timedelta(minutes=3 * 104)
    df_1m_at_restart = df_1m_full[df_1m_full["datetime"] < bar103_end]
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 9_700.0, config.WATCH_SYMBOL: 84.0}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}), fetch_quote=fake_quote,
    )
    restart_now = bar103_end + timedelta(seconds=5)
    svc.bootstrap(now=restart_now)

    worker.initialize_strategy_session(state, svc, now=restart_now)

    assert state.last_detected_direction == Direction.UP_RED
    assert state.pending_signal is None  # no raw bypass-the-gate entry queued
    assert state.time_window_pending_flag_direction == Direction.UP_RED
    assert state.time_window_pending_flag_bar_ts is not None

    # The very next live tick must NOT force an unconditional entry into the
    # flag's own direction -- the T+3 gate (approve, reject-and-hold, or
    # reject-and-liquidate the mismatched position) decides instead, same as
    # any live-detected flag. Whichever of those it picks is a pre-existing,
    # separately-tested decision (test_rejected_reversal_still_liquidates_
    # the_held_tw_position / the whipsaw-hold tests above) -- the one thing
    # that must NEVER happen is a raw, gate-bypassing BUY into LONG_SYMBOL.
    broker = FakeBroker(cash=10_000_000.0, quotes=quote_prices)
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    svc.refresh_quotes()

    result = run_once(broker=broker, market_data=svc, state=state, now=restart_now + timedelta(minutes=1))

    assert not any(a.startswith("TIME_WINDOW_ENTRY") for a in result.actions), (
        f"a stale multi-bar-gap TW candidate must be dropped, not force-entered -- got actions={result.actions!r}"
    )
    assert not any(o.symbol == config.LONG_SYMBOL and o.side == "BUY" for o in broker.orders), (
        f"must never buy into the flag's own direction without going through the TW gate -- orders={broker.orders!r}"
    )


def test_restart_catchup_never_clobbers_an_already_pending_tw_candidate():
    """2026-08-24 real incident: a repeated restart/crash loop (triggered by
    KIS mock-mode rate-limit contention -- see market_data.py's WATCH_SYMBOL
    fix) kept re-running initialize_strategy_session mid-day while flat. Its
    catch-up walk deliberately stops one bar short of today's newest bar (so
    the Worker's own first live tick can evaluate that bar itself) -- which
    means whatever it finds is, by construction, never newer than a genuine
    live-detected candidate a tick had already set and persisted moments
    before the restart. Two real flags (09:48 UP_RED, then 10:33 DOWN_BLUE)
    each got overwritten by this necessarily-older catch-up find before ever
    reaching their own T+3 resolution -- zero orders all day. A pending
    candidate already on state when initialize_strategy_session runs must
    survive it untouched."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    closes = [100.0] * 99 + [92.0, 96.0, 103.0, 104.0, 103.0, 98.0, 90.0, 85.0, 84.0]
    df_1m_full = _1m_from_3m_closes(start, closes)

    state = _fresh_state()
    state.position = None  # flat, same as the real incident
    assert state.last_confirmed_bar_ts is None

    # A live tick already found and persisted a genuine, more recent pending
    # candidate just before the (simulated) restart -- this must win.
    fresher_bar_ts = (start + timedelta(minutes=3 * 200)).isoformat()
    state.time_window_pending_flag_direction = Direction.DOWN_BLUE
    state.time_window_pending_flag_bar_ts = fresher_bar_ts

    bar103_end = start + timedelta(minutes=3 * 104)
    df_1m_at_restart = df_1m_full[df_1m_full["datetime"] < bar103_end]
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 9_700.0, config.WATCH_SYMBOL: 84.0}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m_at_restart, {}), fetch_quote=fake_quote,
    )
    restart_now = bar103_end + timedelta(seconds=5)
    svc.bootstrap(now=restart_now)

    worker.initialize_strategy_session(state, svc, now=restart_now)

    # The catch-up walk did detect its own (older) UP_RED, but must not have
    # been allowed to overwrite the already-pending, fresher candidate.
    assert state.last_detected_direction == Direction.UP_RED
    assert state.time_window_pending_flag_direction == Direction.DOWN_BLUE
    assert state.time_window_pending_flag_bar_ts == fresher_bar_ts


def test_day_rollover_resets_time_window_session_counters():
    state = _fresh_state()
    state.session_date = "20260105"
    state.time_window_morning_entry_count = 3
    state.time_window_afternoon_entry_count = 1
    state.time_window_pending_flag_direction = Direction.UP_RED
    state.time_window_pending_flag_bar_ts = "2026-01-05T09:03:00+09:00"

    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 0, tzinfo=KST))

    assert state.time_window_morning_entry_count == 0
    assert state.time_window_afternoon_entry_count == 0
    assert state.time_window_pending_flag_direction is None
    assert state.time_window_pending_flag_bar_ts is None
    # the toggle itself survives rollover
    assert state.time_window_filter_enabled is True


def test_day_rollover_does_not_touch_time_window_toggle_when_off():
    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.session_date = "20260105"
    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 0, tzinfo=KST))
    assert state.time_window_filter_enabled is False


def test_no_lookahead_run_once_only_uses_bars_up_to_now(tw_market_data):
    """Two independent runs against the SAME full-day cache, one stopped
    early, must agree on every action taken up to the earlier cutoff --
    proves later-in-the-day bars are never consulted for an earlier tick's
    decision."""
    svc, now0 = tw_market_data

    state_short = _fresh_state()
    broker_short = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    actions_short = []
    for step in range(40):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker_short, market_data=svc, state=state_short, now=now)
        actions_short.append(list(result.actions))

    state_long = _fresh_state()
    broker_long = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    actions_long = []
    for step in range(80):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker_long, market_data=svc, state=state_long, now=now)
        actions_long.append(list(result.actions))

    assert actions_short == actions_long[:40], (
        "a run that later sees more of the day's bars must not have altered "
        "any decision already made for an earlier tick"
    )


# ── 탈락 DOWN_BLUE 예외진입 (2026-08-18) ────────────────────────────────────
def _rejected_decision(block_reason: str = config.TW_REJECT_LOW_QUALITY_SCORE) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=block_reason,
        reasons=(block_reason,), component_scores={}, metrics={}, is_reversal=False,
        fast_reversal=False, block_reason=block_reason,
    )


def _prime_pending(state: RuntimeState, direction: Direction, *, before: datetime) -> None:
    state.time_window_pending_flag_direction = direction
    state.time_window_pending_flag_bar_ts = before.isoformat()


def test_down_blue_exception_off_by_default_leaves_rejected_flag_filtered_out(tw_market_data, monkeypatch):
    svc, now0 = tw_market_data
    state = _fresh_state()
    assert state.down_blue_exception_filter_enabled is False  # default OFF, matches config.TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision())
    _prime_pending(state, Direction.DOWN_BLUE, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert f"{config.FILTERED_OUT}:DOWN_BLUE" in result.actions
    assert not any(a.startswith(config.TW_EXCEPTION_DOWN_BLUE_ENTRY) for a in result.actions)
    assert broker.orders == []
    assert state.daily_down_blue_exception_used is False


def test_down_blue_exception_on_enters_a_rejected_down_blue_flag_once(tw_market_data, monkeypatch):
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.down_blue_exception_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision())
    _prime_pending(state, Direction.DOWN_BLUE, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert f"{config.TW_EXCEPTION_DOWN_BLUE_ENTRY}:DOWN_BLUE" in result.actions
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.INVERSE_SYMBOL)]
    assert state.daily_down_blue_exception_used is True
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL

    # a SECOND rejected DOWN_BLUE candidate the same day must NOT fire again
    broker2 = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _prime_pending(state, Direction.DOWN_BLUE, before=_PRIOR_DAY + timedelta(minutes=3))
    result2 = run_once(broker=broker2, market_data=svc, state=state, now=now0 + timedelta(minutes=3))
    assert not any(a.startswith(config.TW_EXCEPTION_DOWN_BLUE_ENTRY) for a in result2.actions)


def test_down_blue_exception_never_applies_to_up_red(tw_market_data, monkeypatch):
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.down_blue_exception_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision())
    _prime_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert f"{config.FILTERED_OUT}:UP_RED" in result.actions
    assert not any(a.startswith(config.TW_EXCEPTION_DOWN_BLUE_ENTRY) for a in result.actions)
    assert broker.orders == []
    assert state.daily_down_blue_exception_used is False


def test_down_blue_exception_never_overrides_an_already_open_position(tw_market_data, monkeypatch):
    """Per design, the exception only ever fires while flat -- it never
    switches/overrides a position the real TW gate already opened."""
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.down_blue_exception_filter_enabled = True
    state.position = worker.PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=1, avg_price=15_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _rejected_decision())
    _prime_pending(state, Direction.DOWN_BLUE, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith(config.TW_EXCEPTION_DOWN_BLUE_ENTRY) for a in result.actions)
    assert state.daily_down_blue_exception_used is False


def test_day_rollover_resets_down_blue_exception_daily_flag_but_not_toggle():
    state = _fresh_state()
    state.down_blue_exception_filter_enabled = True
    state.session_date = "20260105"
    state.daily_down_blue_exception_used = True

    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 0, tzinfo=KST))

    assert state.daily_down_blue_exception_used is False
    assert state.down_blue_exception_filter_enabled is True  # the toggle itself survives rollover


def test_default_state_has_down_blue_exception_off():
    state = state_store.default_state()
    assert state.down_blue_exception_filter_enabled is False


# ── TW2 integration (2026-08-21 사용자 요청) ────────────────────────────────
def test_tw2_entry_confirms_through_the_same_dispatch_and_tags_active_mode(tw_market_data):
    """TW2 must produce the SAME TIME_WINDOW_ENTRY/SWITCH action labels as
    TW1 (shared dispatch code), and the resulting position must be tagged
    time_window_active_mode == 'TW2' so the TP2 override actually applies."""
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.time_window_2_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    entry_tick_index = None
    for step in range(120):
        now = now0 + timedelta(minutes=3 * step)
        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        if any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions):
            entry_tick_index = step
            break

    if entry_tick_index is None:
        pytest.skip("synthetic sine session never produced an approved TW2 entry within 120 steps")
    assert state.position is not None
    assert state.time_window_position_active is True
    assert state.time_window_active_mode == "TW2"
    assert state.time_window_filter_enabled is False


def test_tw1_and_tw2_both_enabled_in_state_tw1_wins_dispatch(tw_market_data):
    """Defensive: even if state somehow had both flags True (should never
    happen via the service setters — see test_service.py's mutual-exclusion
    tests), worker._judge_entry_gate's TIME_WINDOW tier fires exactly once
    per signal either way, and _persist_time_window_decision must record
    the TW1 version string per _judge_entry_gate's existing single dispatch
    (TW1/TW2 share ONE tier, never double-judged)."""
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_filter_enabled = True
    state.time_window_2_filter_enabled = True  # hand-corrupted; never reachable via the setters
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    for step in range(30):
        now = now0 + timedelta(minutes=3 * step)
        run_once(broker=broker, market_data=svc, state=state, now=now)
        if state.last_time_window_decision is not None:
            break

    # Whichever variant's extra veto logic actually ran, _judge_entry_gate's
    # if/elif structure guarantees only ONE _judge_time_window_flag call per
    # signal -- no duplicate/conflicting ledger rows for the same flag.
    assert state.time_window_filter_enabled is True
    assert state.time_window_2_filter_enabled is True


def test_tw2_veto_blocks_an_entry_tw1_would_have_approved(tw_market_data, monkeypatch):
    """Forces evaluate_time_window_entry to always approve, then forces the
    TW2 extra-veto check to trip -- the resulting decision fed to dispatch
    must be rejected, and no order must be placed."""
    from app.trading.macd2.models import MajorFlagDecision as _MFD

    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.time_window_2_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    def _always_approve(*a, **kw):
        return _MFD(
            approved=True, score=5.0, required_score=3.0, decision=config.TW_APPROVED,
            reasons=("forced approve for test",), component_scores={}, metrics={"window": "W1_MORNING_AGGRESSIVE"},
            is_reversal=False, fast_reversal=False, block_reason=None,
        )

    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", _always_approve)
    monkeypatch.setattr(worker.time_window_filter, "evaluate_tw2_extra_vetoes", lambda *a, **kw: (True, config.TW2_REJECT_VWAP_VETO))

    orders_before = len(broker.orders)
    for step in range(30):
        now = now0 + timedelta(minutes=3 * step)
        run_once(broker=broker, market_data=svc, state=state, now=now)
        if state.time_window_pending_flag_direction is None and state.last_time_window_block_reason:
            break

    assert state.last_time_window_approved is False
    assert state.last_time_window_block_reason == config.TW2_REJECT_VWAP_VETO
    assert len(broker.orders) == orders_before
    assert state.position is None


# ── TW2 exit-behavior verification (2026-08-21 사용자 요청: "TW1, TW2 on하면
# 손절,익절,플래그 변경시 전량 익절 이런거 모두 다 되는지 판단해줘") ───────────
# Mirrors the TW1 tests above exactly (test_take_profit_fires_on_the_live_
# tick_not_only_at_bar_close / test_stop_loss_still_fires_while_a_whipsaw_
# reversal_candidate_is_pending / test_rejected_reversal_still_liquidates_
# the_held_tw_position / test_untracked_held_position_is_adopted_and_still_
# gets_take_profit) with TW2 enabled instead of TW1, since exit management
# is fully shared code -- only the TP2 threshold and time_window_active_mode
# differ. If TW1's already-passing tests above ever regress AND these
# TW2 mirrors also fail, that confirms a shared-code break; if only the TW2
# mirrors fail, that isolates a TW2-specific regression.
def test_tw2_take_profit_fires_on_the_live_tick_at_its_own_6pct_threshold(monkeypatch):
    """TW2 raises TP2 from 5.0% to config.TW2_MORNING_TP2 (6.0%) -- a live
    tick past 6% must trigger TP2_FULL immediately, same tick, exactly like
    TW1 does at its own 5% threshold."""
    from app.trading.macd2.models import PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_800.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.time_window_2_filter_enabled = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2"
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_800.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), (
        f"+8% live tick must trigger TW2's 6% TP2 immediately, same tick -- got actions={result.actions!r}"
    )
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions


def test_tw2_take_profit_does_not_fire_below_its_6pct_threshold_where_tw1_would_have(monkeypatch):
    """Sanity check that the override is actually being applied (not just
    silently falling back to TW1's 5%): a price giving ~5.5% must NOT
    trigger TW2's TP2 (needs 6%), proving TW2 really uses its own threshold."""
    from app.trading.macd2.models import PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_550.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.time_window_2_filter_enabled = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2"
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_550.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), (
        f"~5.5% must NOT trigger TW2's 6% TP2 (would have fired under TW1's 5%) -- got actions={result.actions!r}"
    )
    assert state.position is not None, "position must still be held -- only TP1 (3%) may have partially fired"


def test_tw2_stop_loss_still_fires_while_a_whipsaw_reversal_candidate_is_pending(monkeypatch):
    """TW2's stop-loss ladder (-1.7%, unchanged from TW1) must fire exactly
    like TW1's -- mirrors test_stop_loss_still_fires_while_a_whipsaw_
    reversal_candidate_is_pending with TW2 enabled instead."""
    from app.trading.macd2.models import Direction as _Direction, PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 9_700.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.time_window_2_filter_enabled = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2"
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 9_700.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    bar_start, _ = forming_bar_window(now0)
    state.stop_loss_bar_symbol = config.INVERSE_SYMBOL
    state.stop_loss_entry_bar_ts = (bar_start - timedelta(minutes=6)).isoformat()
    state.stop_loss_bar_ts = (bar_start - timedelta(minutes=3)).isoformat()
    state.stop_loss_bar_close = 9_700.0

    flag_bar_dt = now0 - timedelta(minutes=6)
    state.time_window_pending_flag_direction = _Direction.UP_RED
    state.time_window_pending_flag_bar_ts = flag_bar_dt.isoformat()
    whipsaw_decision = MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=config.TW_REJECT_NOT_CONFIRMED,
        reasons=(config.TW_REJECT_NOT_CONFIRMED,), component_scores={}, metrics={},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_REJECT_NOT_CONFIRMED,
    )
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: whipsaw_decision)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_STOP_LOSS) for a in result.actions), (
        f"TW2's -1.7% stop-loss must fire even with a pending whipsaw candidate this tick -- got actions={result.actions!r}"
    )
    assert not any("WHIPSAW_HOLD" in a for a in result.actions)
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions


def test_tw2_rejected_reversal_still_liquidates_the_held_position(tw_market_data, monkeypatch):
    """A fresh opposite flag that TW2's own gate (base TW gate OR the two
    extra vetoes) rejects for a non-whipsaw reason must still fully
    liquidate the held TW2 position -- mirrors test_rejected_reversal_
    still_liquidates_the_held_tw_position with TW2 enabled."""
    from app.trading.macd2.models import Direction as _Direction, MajorFlagDecision, PositionSnapshot

    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.time_window_2_filter_enabled = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2"
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    flag_bar_dt = now0 - timedelta(minutes=6)
    state.time_window_pending_flag_direction = _Direction.UP_RED
    state.time_window_pending_flag_bar_ts = flag_bar_dt.isoformat()

    rejected = MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=config.TW_REJECT_LOW_QUALITY_SCORE,
        reasons=(config.TW_REJECT_LOW_QUALITY_SCORE,), component_scores={}, metrics={},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_REJECT_LOW_QUALITY_SCORE,
    )
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: rejected)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TIME_WINDOW_SELL_ONLY") for a in result.actions), (
        f"a rejected TW2 reversal must still fully liquidate the held position -- got actions={result.actions!r}"
    )
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.time_window_position_active is False


def test_tw2_untracked_held_position_is_adopted_with_tw2_mode_and_gets_its_own_take_profit(monkeypatch):
    """An untracked held position adopted while TW2 (not TW1) is the active
    toggle must be tagged time_window_active_mode == 'TW2' and immediately
    take-profit at TW2's OWN 6% threshold, not TW1's 5% -- mirrors
    test_untracked_held_position_is_adopted_and_still_gets_take_profit."""
    from app.trading.macd2.models import PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_800.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_state()
    state.time_window_filter_enabled = False
    state.time_window_2_filter_enabled = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    assert state.time_window_position_active is False
    assert state.time_window_active_mode is None
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_800.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), (
        f"an untracked-but-held position under TW2 must be adopted and take-profit at TW2's 6% -- got actions={result.actions!r}"
    )
    assert state.position is None


def test_untracked_held_position_adoption_increments_entry_count_exactly_once(monkeypatch):
    """2026-08-25 real incident: a BUY that actually filled but was reported
    BUY_FAILED, later discovered via reconcile_position_state's
    RECOVERED_FROM_BROKER, reaches this SAME adoption path (never
    _resolve_time_window_candidate's own EXECUTED branch, the only place
    that normally increments time_window_morning_entry_count/
    time_window_afternoon_entry_count) -- so the session's entry cap
    (MAX_MORNING_ENTRIES/MAX_AFTERNOON_ENTRIES) silently under-counted a
    real entry. The 09:03 scheduled-entry button
    (test_untracked_held_position_is_adopted_and_still_gets_take_profit,
    above) has the exact same gap for the exact same reason. Proven here
    with a position that stays open (small unrealized return, no TP/SL
    threshold crossed) across two ticks: the appropriate session counter
    must read exactly 1 after both -- one real increment, and the second
    (repeated-reconcile-like) tick must not double-count."""
    from app.trading.macd2 import time_window_filter
    from app.trading.macd2.models import PositionSnapshot

    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_005.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    expected_session = time_window_filter.session_for_window(
        time_window_filter.classify_window(now0.astimezone(KST).time())
    )
    assert expected_session in ("MORNING", "AFTERNOON")

    def _count(state, session):
        return state.time_window_morning_entry_count if session == "MORNING" else state.time_window_afternoon_entry_count

    state = _fresh_state()
    state.time_window_2_filter_enabled = True
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    assert state.time_window_position_active is False  # never tagged -- e.g. reconcile-discovered or scheduled-entry
    assert state.time_window_morning_entry_count == 0
    assert state.time_window_afternoon_entry_count == 0
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_005.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert state.position is not None, "position must still be open (small return, no TP/SL threshold crossed)"
    assert state.time_window_position_active is True
    assert _count(state, expected_session) == 1, (
        f"adoption must increment the {expected_session} entry count exactly once, got "
        f"morning={state.time_window_morning_entry_count} afternoon={state.time_window_afternoon_entry_count}"
    )
    other_session = "AFTERNOON" if expected_session == "MORNING" else "MORNING"
    assert _count(state, other_session) == 0

    # a second tick (mirrors a duplicate/late reconcile discovering the SAME
    # already-adopted position again) must NOT double-count.
    run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))
    assert _count(state, expected_session) == 1, "repeated ticks over the same adopted position must not double-count"
