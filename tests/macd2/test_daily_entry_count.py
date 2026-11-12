"""Tests for RuntimeState.daily_total_entry_count -- the filter-mode-agnostic
daily new-position counter added 2026-08-28 (real incident: toggling TW2/
TEGv2 OFF then back ON let its own morning/afternoon counters under-count
the day's TRUE entries, since entries made under "무필터" mode while TW2 was
off never touched them -- allowing more real entries than config.
MAX_DAILY_ENTRIES was meant to permit).

Mirrors tests/macd2/test_worker_time_window.py's / test_no_filter_entry.py's
own harness (FakeBroker + fake MarketDataService, imported directly) so no
duplicated test infrastructure is introduced.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, order_executor, state_store, time_window_filter, worker
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import Direction, SignalState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_no_filter_entry import _fresh_state as _no_filter_fresh_state
from tests.macd2.test_no_filter_entry import _INSIDE_WINDOW
from tests.macd2.test_worker_time_window import (
    _PRIOR_DAY,
    _fresh_state as _tw_fresh_state,
    _prime_pending,
    tw_market_data,  # noqa: F401  (fixture, re-exported via import)
)

KST = config.KST


def _executed_outcome(*, symbol: str, qty: int = 10, price: float = 15_000.0) -> order_executor.ExecutionOutcome:
    buy_result = BrokerOrderResult(
        success=True, order_id="fake-order", symbol=symbol, side="BUY",
        requested_qty=qty, executed_qty=qty, executed_price=price, message="OK",
    )
    return order_executor.ExecutionOutcome(
        signal_id="sig-1", direction=Direction.UP_RED, target_symbol=symbol,
        final_state=SignalState.EXECUTED, quantity=qty, filled_avg_price=price,
        buy_result=buy_result, timestamps={"evaluated_at": datetime.now(KST).isoformat()},
    )


# ── _apply_switch_outcome: counts regardless of which filter (if any) judged the signal ──

def test_apply_switch_outcome_counts_under_time_window_mode():
    state = _tw_fresh_state()
    assert state.daily_total_entry_count == 0
    outcome = _executed_outcome(symbol=config.LONG_SYMBOL)

    worker._apply_switch_outcome(state, outcome, Direction.UP_RED, datetime.now(KST))

    assert state.daily_total_entry_count == 1
    assert state.time_window_morning_entry_count == 0  # only _resolve_time_window_candidate's own EXECUTED branch bumps this


def test_apply_switch_outcome_counts_under_no_filter_mode_too():
    """The exact real-incident gap: an entry made while TW2/TEG is off (no-
    filter mode instead) must still land in the shared daily total, even
    though it never touches time_window_morning_entry_count/afternoon_entry_
    count at all."""
    state = _no_filter_fresh_state()
    assert state.time_window_2_filter_enabled is False
    assert state.no_filter_0900_1100_enabled is True
    outcome = _executed_outcome(symbol=config.INVERSE_SYMBOL)

    worker._apply_switch_outcome(state, outcome, Direction.DOWN_BLUE, datetime.now(KST))

    assert state.daily_total_entry_count == 1
    assert state.time_window_morning_entry_count == 0
    assert state.time_window_afternoon_entry_count == 0


def test_apply_switch_outcome_never_counts_a_failed_or_unfilled_outcome():
    state = _tw_fresh_state()
    failed = order_executor.ExecutionOutcome(
        signal_id="sig-2", direction=Direction.UP_RED, target_symbol=config.LONG_SYMBOL,
        final_state=SignalState.FAILED, quantity=0,
    )
    worker._apply_switch_outcome(state, failed, Direction.UP_RED, datetime.now(KST))
    assert state.daily_total_entry_count == 0


# ── never reset by a filter toggle, only by day rollover ───────────────────

def test_daily_total_entry_count_survives_tw2_and_teg_toggle_off_and_on():
    from app.trading.macd2 import service as service_module

    state = state_store.default_state()
    state.daily_total_entry_count = 3
    state.time_window_2_filter_enabled = True
    state.time_window_teg_filter_enabled = True
    state_store.save_state(state)

    svc = service_module.Macd2Service()
    svc.set_time_window_2_filter_enabled(False, changed_by="test-suite")  # forces TEG off too
    svc.set_no_filter_0900_1100_filter_enabled(True, changed_by="test-suite")
    reloaded = state_store.load_state()
    assert reloaded.daily_total_entry_count == 3

    svc.set_time_window_2_filter_enabled(True, changed_by="test-suite")
    svc.set_time_window_teg_filter_enabled(True, changed_by="test-suite")
    reloaded_again = state_store.load_state()
    assert reloaded_again.daily_total_entry_count == 3


def test_day_rollover_resets_daily_total_entry_count_but_not_the_toggle():
    state = _tw_fresh_state()
    state.session_date = "20260105"
    state.daily_total_entry_count = 4
    state.time_window_teg_filter_enabled = True

    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 0, tzinfo=KST))

    assert state.daily_total_entry_count == 0
    assert state.time_window_teg_filter_enabled is True  # toggle itself survives the rollover


# ── reconcile-discovered positions count exactly once too ──────────────────

def test_reconcile_discovered_position_counts_toward_daily_total_exactly_once():
    """봇 BUY 가 실패/미확정으로 보고된 뒤 브로커에서 발견된 포지션(= 봇 주문의 늦은 체결)은 정확히 한 번 센다.
    (2026-11-12 부터 봇 주문 흔적이 없는 수동 매수는 세지 않는다 -- test_manual_position_slot_exempt.py)"""
    state = _tw_fresh_state()
    assert state.position is None
    assert state.daily_total_entry_count == 0
    state.bot_buy_unconfirmed_symbol = config.LONG_SYMBOL   # 봇 BUY 가 실패로 보고됨
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")  # broker holds a position runtime never recorded entering

    result = worker.reconcile_position_state(broker, state, datetime.now(KST), force=True)

    assert result == worker.RECOVERED_FROM_BROKER
    assert state.daily_total_entry_count == 1

    # A SECOND reconcile tick against the SAME now-tracked position must
    # never double-count it (state.position is no longer None -> MATCH_POSITION).
    result2 = worker.reconcile_position_state(broker, state, datetime.now(KST), force=True)
    assert result2 == worker.MATCH_POSITION
    assert state.daily_total_entry_count == 1


# ── _judge_no_filter_flag: now also respects the shared daily cap ──────────

def test_judge_no_filter_flag_rejects_once_shared_daily_cap_reached():
    state = _no_filter_fresh_state()
    state.daily_total_entry_count = config.MAX_DAILY_ENTRIES  # already at the true daily cap

    decision = worker._judge_no_filter_flag(state=state, now=_INSIDE_WINDOW, signal_id="sig-3")

    assert decision.approved is False
    assert decision.block_reason == config.TW_REJECT_MAX_ENTRY_COUNT


def test_judge_no_filter_flag_still_approves_below_the_shared_daily_cap():
    state = _no_filter_fresh_state()
    state.daily_total_entry_count = config.MAX_DAILY_ENTRIES - 1

    decision = worker._judge_no_filter_flag(state=state, now=_INSIDE_WINDOW, signal_id="sig-4")

    assert decision.approved is True
    assert decision.block_reason is None


# ── evaluate_time_window_entry: before/after reproduction of today's exact gap ──

def test_evaluate_time_window_entry_daily_cap_before_and_after_the_fix():
    """Direct reproduction of the real 2026-08-28 gap at the pure-function
    level (reuses test_time_window_filter.py's own flagged_session recipe --
    a real UP_RED crossover, morning-window classified): TW2's own morning_
    entry_count only ever reflects TW2-gated entries. If 2 more entries
    happened today under no-filter mode (while TW2 was off) with only 3
    counted by TW2 itself, the OLD sum-based check (daily_entry_count=None,
    i.e. every caller before this fix) approves a 6th real entry the true
    daily total (5) should already have blocked; the NEW shared-count check
    (daily_entry_count=5) correctly rejects it.
    """
    from tests.macd2.test_time_window_filter import _down_then_up, _first_up_red_flag

    start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
    bars = _down_then_up(28, 12, start=start)
    flag_idx = _first_up_red_flag(bars)
    flag_dt = bars["datetime"].iloc[flag_idx]
    confirm_bars = bars.iloc[: flag_idx + 2]
    decision_at = confirm_bars["datetime"].iloc[-1] + timedelta(minutes=3)

    # BEFORE the fix: only TW2's own counters are visible (2 morning + 0
    # afternoon = 2 < MAX_DAILY_ENTRIES=5, and 2 < MAX_MORNING_ENTRIES=3 so
    # the morning-specific cap doesn't fire either) -- approves, even though
    # 3 more real entries already happened today under no-filter mode.
    before = time_window_filter.evaluate_time_window_entry(
        confirm_bars, Direction.UP_RED, flag_dt, decision_at,
        morning_entry_count=2, afternoon_entry_count=0, daily_entry_count=None,
    )
    assert before.approved is True

    # AFTER the fix: worker.py now passes the TRUE daily total (5 = 2 TW2 +
    # 3 no-filter) -- correctly rejects the 6th real entry of the day, even
    # though TW2's own morning_entry_count (2) still looks nowhere near
    # either MAX_MORNING_ENTRIES or the old sum-based daily check.
    after = time_window_filter.evaluate_time_window_entry(
        confirm_bars, Direction.UP_RED, flag_dt, decision_at,
        morning_entry_count=2, afternoon_entry_count=0, daily_entry_count=5,
    )
    assert after.approved is False
    assert after.block_reason == config.TW_REJECT_MAX_ENTRY_COUNT


# ── end-to-end: the toggle-off-then-on scenario, through the real dispatch wiring ──

def test_worker_passes_the_shared_daily_total_into_evaluate_time_window_entry(tw_market_data, monkeypatch):
    """Wiring test (mirrors test_teg_count_cap_bypass.py's monkeypatch
    technique -- isolates worker.py's own call-site wiring from
    evaluate_time_window_entry's internal condition logic, already covered
    directly above): worker._resolve_time_window_candidate must pass
    state.daily_total_entry_count through as evaluate_time_window_entry's
    daily_entry_count kwarg. Real incident reproduction: TW2's own
    morning_entry_count (2) looks nowhere near either cap, but 3 more real
    entries happened today under no-filter mode (TW2 was off) -- the shared
    total (5) is already at config.MAX_DAILY_ENTRIES, and a fresh TW2
    candidate must now be rejected once the fix wires that true total
    through, instead of silently approving on TW2's own under-count."""
    svc, now0 = tw_market_data
    state = _tw_fresh_state()
    state.daily_total_entry_count = 5
    state.time_window_morning_entry_count = 2
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    captured_kwargs: dict = {}

    def _spy_evaluate_time_window_entry(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _rejected_decision()

    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", _spy_evaluate_time_window_entry)
    _prime_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert captured_kwargs.get("morning_entry_count") == 2
    assert captured_kwargs.get("daily_entry_count") == 5, (
        "worker.py must pass state.daily_total_entry_count (the TRUE daily total, "
        "including no-filter-mode entries invisible to TW2's own counters) here"
    )
    assert not any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions)
    assert broker.orders == []
    # daily_total_entry_count is untouched by a rejection (no fill happened).
    assert state.daily_total_entry_count == 5


def _independent_tw_market_data():
    """A FRESH MarketDataService built from the exact same recipe as the
    ``tw_market_data`` fixture -- used instead of sharing that fixture's one
    instance across two parallel run_once loops, since MarketDataService may
    hold internal per-instance cache/cursor state that two interleaved
    callers at the same ``now`` could otherwise perturb for each other."""
    import math

    import pandas as pd

    from app.trading.macd2.market_data import MarketDataService
    from tests.macd2.test_worker_time_window import _BOOTSTRAP_NOW, _SESSION_START_NOW

    def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
        period = max(n_minutes // 2, 1)
        return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]

    def _1m_frame(start, closes: list[float]) -> pd.DataFrame:
        rows = [
            {"datetime": start + timedelta(minutes=i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 100 + (i % 7) * 10}
            for i, c in enumerate(closes)
        ]
        return pd.DataFrame(rows)

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


def test_sixth_real_entry_is_actually_blocked_end_to_end_no_mocking():
    """Final pre-deploy check: the REAL evaluate_time_window_entry (not
    monkeypatched) rejects a 6th real entry through the REAL run_once
    dispatch path, fed genuine synthetic 1m->3m->MACD bars (not a canned
    decision) -- this is the exact "OFF then back ON" scenario, end to end.

    Two identical FakeBroker/state pairs, each with its OWN independent
    MarketDataService built from the identical recipe (so per-instance
    cache/cursor state can never leak between them), walk the same
    synthetic session tick by tick. Run A starts with a flat daily count
    (0) -- the FIRST entry it produces confirms the pipeline genuinely
    still enters normally (not broken by this fix). Run B starts with
    daily_total_entry_count already at config.MAX_DAILY_ENTRIES (simulating
    5 real entries that already happened today, some of them under
    no-filter mode while TW2 was off) -- at the SAME tick Run A enters, Run
    B must be blocked with TW_REJECT_MAX_ENTRY_COUNT and place no order at
    all.
    """
    svc_a, now0_a = _independent_tw_market_data()
    svc_b, now0_b = _independent_tw_market_data()
    assert now0_a == now0_b

    state_a = _tw_fresh_state()
    broker_a = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    state_b = _tw_fresh_state()
    state_b.daily_total_entry_count = config.MAX_DAILY_ENTRIES
    broker_b = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    entry_step = None
    for step in range(120):
        now = now0_a + timedelta(minutes=3 * step)
        result_a = run_once(broker=broker_a, market_data=svc_a, state=state_a, now=now)
        result_b = run_once(broker=broker_b, market_data=svc_b, state=state_b, now=now)
        if any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result_a.actions):
            entry_step = step
            break

    if entry_step is None:
        pytest.skip("synthetic sine session never produced an approved time-window entry within 120 steps")

    # Run A: the pipeline genuinely still enters normally (this fix changes
    # nothing about a real, within-cap entry).
    assert state_a.position is not None
    assert broker_a.orders != []

    # Run B: the exact same real signal, same tick, but the true daily total
    # was already at the cap -- must be blocked, not entered.
    assert state_b.position is None, (
        f"Run B (daily_total_entry_count already at MAX_DAILY_ENTRIES={config.MAX_DAILY_ENTRIES}) "
        f"must not have entered a position on the same tick Run A did"
    )
    assert broker_b.orders == []
    assert not any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result_b.actions)
    assert state_b.last_time_window_decision == config.TW_REJECT_MAX_ENTRY_COUNT
    assert state_b.order_block_reason == config.TW_REJECT_MAX_ENTRY_COUNT


def _rejected_decision():
    from app.trading.macd2.models import MajorFlagDecision

    return MajorFlagDecision(
        approved=False, score=0.0, required_score=0.0, decision=config.TW_REJECT_MAX_ENTRY_COUNT,
        reasons=(config.TW_REJECT_MAX_ENTRY_COUNT,), component_scores={}, metrics={"window": "W1_MORNING_AGGRESSIVE"},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_REJECT_MAX_ENTRY_COUNT,
    )
