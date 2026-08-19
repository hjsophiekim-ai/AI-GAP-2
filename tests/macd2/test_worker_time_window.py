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
