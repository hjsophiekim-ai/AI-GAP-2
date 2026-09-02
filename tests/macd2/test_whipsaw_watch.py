"""Tests for the shared TW2/TW2_3SLOT whipsaw-watch follow-up exit
(2026-09-02, real incident) added to app/trading/macd2/{models,config,
time_window_filter,worker,state_store}.py.

Real incident this fixes: a TW2_3SLOT DOWN_BLUE (inverse) position saw an
opposite UP_RED flag confirm at 13:57. The T+3 re-check at 14:00 legitimately
whipsaw-held (gap 213.74 -> 177.04, did not expand) -- but the opposite
direction then kept strengthening for 6+ more completed bars (177 -> 302 ->
393 -> 905 -> 1140 -> 1442 -> 1640) with zero further tracking, only exiting
24+ minutes later via an unrelated breakeven-stop. A read-only simulation
confirmed a 1-bar-later watch would have sold at 14:03 instead.

Section 1: pure-function tests for time_window_filter.evaluate_whipsaw_watch,
using REAL MACD/EMA math on synthetic 3m bars (no mocking of the indicator
math itself). Section 2: worker.run_once() orchestration fixtures reproducing
the real 13:57 incident's gap sequence (mocked at the evaluate_whipsaw_watch
boundary, same convention every other TW2/TW2_3SLOT worker test in this repo
already uses for the underlying decision functions) for both TW2_3SLOT and
plain TW2. Section 3: hand-off (fresh opposite flag supersedes a stale watch)
and the _apply_exit_outcome safety net (any other exit reason also clears a
stale watch).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.macd2 import config, state_store, time_window_filter, worker
from app.trading.macd2.models import Direction, MajorFlagDecision, PositionSnapshot, WhipsawWatchDecision
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (
    _fresh_3slot_state,
    _patch_common as _patch_common_3slot,
    _prime_3slot_pending,
    _rejected as _rejected_3slot,
    tw2_3slot_market_data,
)
from tests.macd2.test_worker_time_window import _fresh_state, tw_market_data

KST = config.KST


# ── Section 1: evaluate_whipsaw_watch pure-function tests, real math ───────

def _3m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=3 * i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 100}
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


_START = datetime(2026, 1, 5, 9, 0, tzinfo=KST)


def test_insufficient_bars_returns_insufficient_data_and_keeps_waiting():
    closes = [100.0 + i for i in range(10)]  # well under MAJOR_EMA_SLOW + 1
    bars = _3m_frame(_START, closes)
    decision = time_window_filter.evaluate_whipsaw_watch(bars, Direction.UP_RED, 50.0, 5.0)
    assert decision.insufficient_data is True
    assert decision.should_sell is False
    assert decision.should_release is False
    assert decision.current_gap == 50.0
    assert decision.current_ema_spread == 5.0


def test_continued_deterioration_in_watched_direction_flags_should_sell():
    """A strengthening one-directional trend: both the signed MACD gap and
    the signed EMA10-EMA20 spread genuinely keep expanding bar over bar for
    a real trend, exactly the dynamic behind the 177->302->393->...->1640
    real incident sequence -- computed here via the REAL indicator math on
    two growing prefixes of the same accelerating price series, not via
    canned numbers."""
    n = 45
    closes = [100.0 + 0.25 * i * i for i in range(n)]  # accelerating uptrend
    bars = _3m_frame(_START, closes)

    baseline = time_window_filter.evaluate_whipsaw_watch(bars.iloc[:35], Direction.UP_RED, float("-inf"), float("-inf"))
    assert baseline.insufficient_data is False
    later = time_window_filter.evaluate_whipsaw_watch(
        bars.iloc[:40], Direction.UP_RED, baseline.current_gap, baseline.current_ema_spread,
    )
    assert later.insufficient_data is False
    assert later.current_gap > baseline.current_gap
    assert later.current_ema_spread > baseline.current_ema_spread
    assert later.should_sell is True
    assert later.should_release is False


def test_reversal_back_toward_original_direction_releases_the_watch():
    """The signed gap flips back to favor the ORIGINALLY held direction
    (<=0 for the watched/opposite direction) -- must release, not sell."""
    n = 40
    up = [100.0 + 0.3 * i for i in range(n // 2)]
    down = [up[-1] - 0.3 * i for i in range(1, n // 2 + 1)]
    closes = up + down
    bars = _3m_frame(_START, closes)
    decision = time_window_filter.evaluate_whipsaw_watch(bars, Direction.UP_RED, float("-inf"), float("-inf"))
    assert decision.insufficient_data is False
    assert decision.current_gap <= 0
    assert decision.should_release is True
    assert decision.should_sell is False


def test_only_one_of_gap_or_ema_spread_expanding_keeps_watching_not_sell():
    """Both signals must expand together -- if the LAST reference values are
    set higher than what actually happened for one of the two signals, the
    AND-gate must not fire even though the other signal genuinely expanded."""
    n = 45
    closes = [100.0 + 0.25 * i * i for i in range(n)]
    bars = _3m_frame(_START, closes)
    current = time_window_filter.evaluate_whipsaw_watch(bars, Direction.UP_RED, float("-inf"), float("-inf"))
    assert current.insufficient_data is False

    # Pretend the EMA spread was ALREADY higher than today's real value --
    # so ema_spread did not expand even though gap (compared against a very
    # low last_gap) looks like it did.
    decision = time_window_filter.evaluate_whipsaw_watch(
        bars, Direction.UP_RED, last_gap=float("-inf"), last_ema_spread=current.current_ema_spread + 1_000.0,
    )
    assert decision.should_sell is False
    assert decision.should_release is False


# ── Section 2: worker.run_once() fixtures reproducing the real 13:57 incident ─

def _canned_whipsaw_watch(monkeypatch, target_module, decisions: list[WhipsawWatchDecision]):
    calls = {"n": 0}

    def _fake(*_a, **_kw):
        i = min(calls["n"], len(decisions) - 1)
        calls["n"] += 1
        return decisions[i]

    monkeypatch.setattr(target_module.time_window_filter, "evaluate_whipsaw_watch", _fake)
    return calls


def test_tw2_3slot_real_incident_fixture_holds_at_t3_then_sells_on_first_deterioration_bar(
    tw2_3slot_market_data, monkeypatch,
):
    """Reproduces the real 2026-09-02 13:57 incident under TW2_3SLOT: DOWN_BLUE
    (inverse) held, opposite UP_RED flag whipsaw-holds at T+3 (gap 213.74 ->
    177.04, did not expand -- config.TW_REJECT_MACD_GAP_NOT_EXPANDING), then
    the watch's very first re-check on the next completed bar sees the real
    incident's continued deterioration (gap 177.04 -> 302.04) and the signed
    EMA10-EMA20 spread also expanding -- full liquidation must fire on that
    bar, exactly matching the read-only simulation's "would have sold at
    14:03" finding (1 bar after the hold, not 24+ minutes later via an
    unrelated breakeven-stop as actually happened live)."""
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    state.tw2_3slot_morning_count = 1
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    _patch_common_3slot(monkeypatch, entry_decision=_rejected_3slot(config.TW_REJECT_MACD_GAP_NOT_EXPANDING))

    calls = _canned_whipsaw_watch(monkeypatch, worker, [
        WhipsawWatchDecision(should_sell=False, should_release=False, current_gap=177.04, current_ema_spread=40.0),
        WhipsawWatchDecision(should_sell=True, should_release=False, current_gap=302.04, current_ema_spread=65.0),
    ])

    _prime_3slot_pending(state, Direction.UP_RED, before=_START)
    result_t3 = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_WHIPSAW_HOLD") for a in result_t3.actions), result_t3.actions
    assert state.whipsaw_watch_active is True
    assert state.whipsaw_watch_direction == Direction.UP_RED
    assert state.whipsaw_watch_mode == "TW2_3SLOT"
    assert state.whipsaw_watch_last_gap == 177.04
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL

    now1 = now0 + timedelta(minutes=3)
    result_next = run_once(broker=broker, market_data=svc, state=state, now=now1)

    assert any(a.startswith(config.WHIPSAW_WATCH_DETERIORATION_EXIT) for a in result_next.actions), result_next.actions
    assert state.position is None, "the held inverse position must be fully liquidated on the deterioration bar"
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.whipsaw_watch_active is False
    assert calls["n"] == 2, "hold at T+3 (1 seed call) + exactly one advance call before selling"


def test_plain_tw2_real_incident_fixture_holds_at_t3_then_sells_on_first_deterioration_bar(
    tw_market_data, monkeypatch,
):
    """Same real-incident reproduction as the TW2_3SLOT fixture above, but
    under plain TW2 (state.time_window_teg_filter_enabled=True), proving the
    watch mechanism is the SAME shared function/state for both modes."""
    from app.trading.macd2.models import Direction as _Direction

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
        approved=False, score=1.0, required_score=4.0, decision=config.TW_REJECT_MACD_GAP_NOT_EXPANDING,
        reasons=(config.TW_REJECT_MACD_GAP_NOT_EXPANDING,), component_scores={}, metrics={},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_REJECT_MACD_GAP_NOT_EXPANDING,
    )
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: rejected)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    calls = _canned_whipsaw_watch(monkeypatch, worker, [
        WhipsawWatchDecision(should_sell=False, should_release=False, current_gap=177.04, current_ema_spread=40.0),
        WhipsawWatchDecision(should_sell=True, should_release=False, current_gap=302.04, current_ema_spread=65.0),
    ])

    result_t3 = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert any(a.startswith("TIME_WINDOW_WHIPSAW_HOLD") for a in result_t3.actions), result_t3.actions
    assert state.whipsaw_watch_active is True
    assert state.whipsaw_watch_mode == "TW2"

    now1 = now0 + timedelta(minutes=3)
    result_next = run_once(broker=broker, market_data=svc, state=state, now=now1)

    assert any(a.startswith(config.WHIPSAW_WATCH_DETERIORATION_EXIT) for a in result_next.actions), result_next.actions
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.whipsaw_watch_active is False
    assert calls["n"] == 2


def test_watch_releases_with_no_order_when_opposite_direction_recovers(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    _patch_common_3slot(monkeypatch, entry_decision=_rejected_3slot(config.TW_REJECT_MACD_GAP_NOT_EXPANDING))

    _canned_whipsaw_watch(monkeypatch, worker, [
        WhipsawWatchDecision(should_sell=False, should_release=False, current_gap=177.04, current_ema_spread=40.0),
        WhipsawWatchDecision(should_sell=False, should_release=True, current_gap=-5.0, current_ema_spread=-2.0),
    ])

    _prime_3slot_pending(state, Direction.UP_RED, before=_START)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.whipsaw_watch_active is True

    orders_before = len(broker.orders)
    result_next = run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=3))
    assert any(a.startswith(config.WHIPSAW_WATCH_RELEASED) for a in result_next.actions), result_next.actions
    assert state.whipsaw_watch_active is False
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert len(broker.orders) == orders_before, "a release must never place an order"


# ── Section 3: hand-off to a fresh flag, and the _apply_exit_outcome safety net ─

def test_fresh_opposite_confirmed_flag_supersedes_and_clears_a_stale_watch(tw2_3slot_market_data, monkeypatch):
    """Directive requirement: '새 반대 confirmed flag가 다시 나오면 기존 reversal
    로직으로 즉시 넘기고 watch 종료' -- a genuinely NEW confirmed opposite flag
    detected while a watch is active must clear the stale watch and register
    its own fresh T+3 candidate instead of running both mechanisms at once."""
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    state.whipsaw_watch_active = True
    state.whipsaw_watch_direction = Direction.UP_RED
    state.whipsaw_watch_mode = "TW2_3SLOT"
    state.whipsaw_watch_origin_flag_bar_ts = (now0 - timedelta(minutes=6)).isoformat()
    state.whipsaw_watch_started_at = (now0 - timedelta(minutes=3)).isoformat()
    state.whipsaw_watch_last_gap = 177.04
    state.whipsaw_watch_last_ema_spread = 40.0
    state.whipsaw_watch_last_checked_bar_ts = (now0 - timedelta(minutes=3)).isoformat()
    state.whipsaw_watch_bars_checked = 1

    monkeypatch.setattr(worker, "_advance_confirmed_primary", lambda state, macd_snap, now: Direction.UP_RED)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert state.whipsaw_watch_active is False, "the stale watch must be cleared the instant a fresh flag arrives"
    assert state.tw2_3slot_pending_flag_direction == Direction.UP_RED, (
        "the fresh flag's own T+3 cycle must take over"
    )
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL, (
        "the held position must not be touched on the flag's own detection bar"
    )
    del result


def test_position_closing_for_any_other_reason_clears_a_stale_watch():
    """Safety net: a held position can close for ANY reason (stop-loss/
    profit-lock/forced-liquidation/etc.) while a whipsaw-watch is tracking
    it -- exactly the class of gap behind the real incident, where the
    position exited via an unrelated breakeven-stop while a reversal
    candidate may have been left dangling. _apply_exit_outcome is the one
    function EVERY full-exit path in worker.py already routes through to
    reset time_window_* state; this checks it also clears a stale
    whipsaw_watch_active=True the same way, never leaving it to survive
    past the position it described."""
    from app.trading.macd2.models import SignalState

    state = state_store.default_state()
    state.whipsaw_watch_active = True
    state.whipsaw_watch_direction = Direction.UP_RED
    state.whipsaw_watch_mode = "TW2_3SLOT"
    state.whipsaw_watch_origin_flag_bar_ts = "2026-09-02T13:57:00+09:00"
    state.whipsaw_watch_started_at = "2026-09-02T14:00:00+09:00"
    state.whipsaw_watch_last_gap = 177.04
    state.whipsaw_watch_last_ema_spread = 40.0
    state.whipsaw_watch_last_checked_bar_ts = "2026-09-02T14:00:00+09:00"
    state.whipsaw_watch_bars_checked = 1

    class _StopLossOutcomeStub:
        final_state = SignalState.EXECUTED
        target_symbol = config.INVERSE_SYMBOL
        sell_result = None
        buy_result = None
        block_reason = config.EXIT_STOP_LOSS

    worker._apply_exit_outcome(state, _StopLossOutcomeStub())

    assert state.whipsaw_watch_active is False, "any full exit must clear a stale whipsaw watch"
    assert state.whipsaw_watch_direction is None
    assert state.whipsaw_watch_mode is None
    assert state.whipsaw_watch_last_gap is None
    assert state.whipsaw_watch_bars_checked == 0
