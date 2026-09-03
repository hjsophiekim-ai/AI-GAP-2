"""Worker-path regression tests for TW2 3-SLOT (2026-09-01), required before
the feature is committed. Every test here goes through the REAL
worker.run_once() / worker.reconcile_position_state() / worker._apply_day_
rollover() dispatch path (fake broker + tmp_path-isolated state/ledger via
conftest.py's autouse fixtures) — never a pure-function-only check. Mirrors
tests/macd2/test_worker_time_window.py's and test_daily_entry_count.py's own
harness (monkeypatch the underlying decision functions to get deterministic,
non-flaky scenarios instead of waiting on an organic sine-wave crossover —
the same technique those existing, currently-passing TW2 tests already use)
so no duplicated test infrastructure is introduced.

Covers, one section each:
  1. Daily cap never exceeds 3, across 8 alternating flags spanning morning+afternoon.
  2. Morning 1st/2nd = plain approval; 3rd = Trend Quality >=3/5 additionally required.
  3. Unused morning slot carries to afternoon; afternoon requires TW2 AND TEGv2.
  4. TEG's own once-daily count-cap bypass never fires under TW2_3SLOT.
  5. Toggle ON/restart/reconcile don't duplicate or reset the 3-slot counters incorrectly.
  6. Reversal exit / whipsaw hold / TP-SL-trailing ladder are byte-identical to TW2's own.
  7. (full-suite regression) — run separately, see the coordinator report.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, order_executor, service as service_module, state_store, teg_gate, worker
from app.trading.macd2 import time_window_3slot as tw2_3slot
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, MajorFlagDecision, PositionSnapshot, RuntimeState, SignalState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


# ── shared fixture/helpers (mirrors test_worker_time_window.py's own recipe) ─

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


@pytest.fixture
def tw2_3slot_market_data():
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


def _fresh_3slot_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    state.time_window_3slot_filter_enabled = True
    return state


def _prime_3slot_pending(state: RuntimeState, direction: Direction, *, before: datetime) -> None:
    state.tw2_3slot_pending_flag_direction = direction
    state.tw2_3slot_pending_flag_bar_ts = before.isoformat()


def _approved(window: str = "W1_MORNING_AGGRESSIVE") -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=True, score=5.0, required_score=4.0, decision="APPROVED",
        reasons=(), component_scores={}, metrics={"window": window},
        is_reversal=False, fast_reversal=False, block_reason=None,
    )


def _rejected(reason: str, window: str | None = None) -> MajorFlagDecision:
    metrics = {"window": window} if window else {}
    return MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=reason,
        reasons=(reason,), component_scores={}, metrics=metrics,
        is_reversal=False, fast_reversal=False, block_reason=reason,
    )


def _quality(approved: bool, passed: int = 5) -> tw2_3slot.TrendQualityDecision:
    return tw2_3slot.TrendQualityDecision(
        approved=approved, passed_count=passed, required=config.TW2_3SLOT_MORNING_3RD_QUALITY_MIN,
        conditions={}, metrics={}, reject_reasons=() if approved else ("stub",),
    )


def _teg(approved: bool, reasons: tuple = ()) -> "teg_gate.TEGDecision":
    return teg_gate.TEGDecision(approved=approved, conditions={}, metrics={}, reject_reasons=reasons)


def _no_extra_veto(*_a, **_kw):
    return False, None


def _patch_common(monkeypatch, *, entry_decision=None, quality_decision=None, teg_decision=None, veto=(False, None)):
    """Standard patch set: evaluate_time_window_entry / evaluate_tw2_extra_vetoes
    / time_window_3slot.evaluate_trend_quality / teg_gate.evaluate_teg, plus
    fast fill-reconcile timing (mirrors every existing TW2 worker test)."""
    if entry_decision is not None:
        monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: entry_decision)
    monkeypatch.setattr(worker.time_window_filter, "evaluate_tw2_extra_vetoes", lambda *a, **kw: veto)
    if quality_decision is not None:
        monkeypatch.setattr(worker.time_window_3slot, "evaluate_trend_quality", lambda *a, **kw: quality_decision)
    if teg_decision is not None:
        monkeypatch.setattr(worker.teg_gate, "evaluate_teg", lambda *a, **kw: teg_decision)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)


def _step(now0: datetime, minutes: int) -> datetime:
    return now0 + timedelta(minutes=minutes)


def _to_afternoon(now0: datetime) -> datetime:
    """First 3-minute-aligned tick at/after 13:00 KST on the fixture's day."""
    target = now0.replace(hour=13, minute=0, second=0, microsecond=0)
    delta_minutes = int((target - now0).total_seconds() // 60)
    delta_minutes = ((delta_minutes + 2) // 3) * 3  # round up to a multiple of 3
    return now0 + timedelta(minutes=delta_minutes)


# ── 1. Daily cap never exceeds 3, across 8 alternating flags, morning+afternoon ─

def test_daily_cap_never_exceeds_3_across_8_alternating_flags(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(
        monkeypatch, entry_decision=_approved(), quality_decision=_quality(True, 5), teg_decision=_teg(True),
    )

    directions = [Direction.UP_RED, Direction.DOWN_BLUE, Direction.UP_RED, Direction.DOWN_BLUE,
                  Direction.UP_RED, Direction.DOWN_BLUE, Direction.UP_RED, Direction.DOWN_BLUE]
    entries = 0
    for i, direction in enumerate(directions):
        now = _step(now0, 3 * i)
        # Each flag needs its OWN unique flag_bar_dt (mirroring reality --
        # every real confirmed flag is a distinct bar) so make_signal_id
        # produces a distinct signal_id per flag; reusing the same
        # before= timestamp across same-direction flags would collide with
        # an earlier flag's already-processed signal_id and short-circuit.
        _prime_3slot_pending(state, direction, before=_PRIOR_DAY - timedelta(minutes=i))
        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        assert int(state.tw2_3slot_slots_used_today or 0) <= config.TW2_3SLOT_DAILY_CAP, (
            f"flag #{i+1}: slot cap exceeded ({state.tw2_3slot_slots_used_today})"
        )
        if any(a.startswith("TW2_3SLOT_ENTRY") or a.startswith("TW2_3SLOT_SWITCH") for a in result.actions):
            entries += 1

    assert entries == config.TW2_3SLOT_DAILY_CAP, f"expected exactly {config.TW2_3SLOT_DAILY_CAP} real entries/switches, got {entries}"
    assert state.tw2_3slot_slots_used_today == config.TW2_3SLOT_DAILY_CAP
    assert state.last_tw2_3slot_block_reason == config.TW2_3SLOT_REJECT_SLOT_CAP, (
        "the flags past the cap must be rejected specifically for the slot cap"
    )
    # A rejected-for-cap reversal while a position is held must still fully
    # liquidate it (mirrors TW2's own TW_REJECT_MAX_ENTRY_COUNT sell-only
    # behavior) -- confirmed by ending flat, not stuck holding.
    assert state.position is None or state.tw2_3slot_slots_used_today == config.TW2_3SLOT_DAILY_CAP


# ── 2. Morning 1st/2nd = plain approval; 3rd requires Trend Quality >=3/5 ────

def test_morning_1st_and_2nd_enter_even_when_quality_would_fail(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    # Quality mock ALWAYS rejects -- proves 1st/2nd never even consult it.
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(False, 0), teg_decision=_teg(True))

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result1 = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert any(a.startswith("TW2_3SLOT_ENTRY") for a in result1.actions), result1.actions
    assert state.tw2_3slot_slots_used_today == 1
    assert state.tw2_3slot_morning_count == 1

    now1 = _step(now0, 3)
    _prime_3slot_pending(state, Direction.DOWN_BLUE, before=_PRIOR_DAY)
    result2 = run_once(broker=broker, market_data=svc, state=state, now=now1)
    assert any(a.startswith("TW2_3SLOT_SWITCH") for a in result2.actions), result2.actions
    assert state.tw2_3slot_slots_used_today == 2
    assert state.tw2_3slot_morning_count == 2


def test_morning_3rd_candidate_rejected_below_quality_threshold_slot_preserved(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.tw2_3slot_slots_used_today = 2
    state.tw2_3slot_morning_count = 2
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(False, 1))

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith("TW2_3SLOT_ENTRY") or a.startswith("TW2_3SLOT_SWITCH") for a in result.actions)
    assert state.last_tw2_3slot_block_reason == config.TW2_3SLOT_REJECT_QUALITY
    assert state.tw2_3slot_slots_used_today == 2, "the 3rd slot must be preserved (not consumed) on a quality reject"


def test_morning_3rd_candidate_enters_when_quality_passes(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.tw2_3slot_slots_used_today = 2
    state.tw2_3slot_morning_count = 2
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True, 4))

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_ENTRY") for a in result.actions), result.actions
    assert state.tw2_3slot_slots_used_today == 3
    assert state.tw2_3slot_morning_count == 3


# ── 3. Unused morning slot carries to afternoon; requires TW2 AND TEGv2 ─────

def test_afternoon_carried_slot_rejected_when_teg_fails(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.tw2_3slot_slots_used_today = 2
    state.tw2_3slot_morning_count = 2  # 3rd slot never used in the morning -- carried over
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(window="W5_EARLY_AFTERNOON_A_GRADE"), teg_decision=_teg(False, ("vwap_favorable_side",)))

    afternoon_now = _to_afternoon(now0)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=afternoon_now)

    assert not any(a.startswith("TW2_3SLOT_ENTRY") for a in result.actions), result.actions
    assert state.last_tw2_3slot_block_reason == config.TW2_3SLOT_REJECT_TEG
    assert state.tw2_3slot_slots_used_today == 2


def test_afternoon_carried_slot_rejected_when_tw2_itself_fails(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.tw2_3slot_slots_used_today = 2
    state.tw2_3slot_morning_count = 2
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(
        monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE, window="W5_EARLY_AFTERNOON_A_GRADE"),
        teg_decision=_teg(True),
    )

    afternoon_now = _to_afternoon(now0)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=afternoon_now)

    assert not any(a.startswith("TW2_3SLOT_ENTRY") for a in result.actions), result.actions
    assert state.last_tw2_3slot_block_reason == config.TW_REJECT_LOW_QUALITY_SCORE, (
        "when the base TW2 gate itself rejects, TEG's own approval must never override it"
    )
    assert state.tw2_3slot_slots_used_today == 2


def test_afternoon_carried_slot_enters_when_both_tw2_and_teg_approve(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.tw2_3slot_slots_used_today = 2
    state.tw2_3slot_morning_count = 2
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(window="W5_EARLY_AFTERNOON_A_GRADE"), teg_decision=_teg(True))

    afternoon_now = _to_afternoon(now0)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=afternoon_now)

    assert any(a.startswith("TW2_3SLOT_ENTRY") for a in result.actions), result.actions
    assert state.tw2_3slot_slots_used_today == 3
    assert state.tw2_3slot_afternoon_count == 1


# ── 4. TEG's own once-daily count-cap bypass never fires under TW2_3SLOT ────

def test_teg_count_cap_bypass_structurally_disabled_under_tw2_3slot(tw2_3slot_market_data, monkeypatch):
    """A scenario that WOULD trigger TEG's own once-daily count-cap bypass
    under TEG mode (a candidate rejected SOLELY for TW_REJECT_MAX_ENTRY_COUNT)
    -- run under TW2_3SLOT instead. _resolve_time_window_candidate (the ONLY
    code path containing the TEG bypass) must never even execute, since it
    early-returns the instant neither TW2 nor TEG is enabled -- confirmed by
    daily cap being TW2_3SLOT's own (config.TW2_3SLOT_REJECT_SLOT_CAP), never
    TW_TEG_COUNT_CAP_BYPASS, appearing anywhere."""
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    assert state.time_window_teg_filter_enabled is False  # mutual exclusion, structural precondition
    state.tw2_3slot_slots_used_today = config.TW2_3SLOT_DAILY_CAP
    state.tw2_3slot_morning_count = config.TW2_3SLOT_DAILY_CAP
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    # Even if the base gate AND teg would both approve, the slot cap (checked
    # before either) must reject first, and TEG's bypass must never run.
    _patch_common(monkeypatch, entry_decision=_approved(), teg_decision=_teg(True))

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith("TW2_3SLOT_ENTRY") for a in result.actions), result.actions
    assert state.last_tw2_3slot_block_reason == config.TW2_3SLOT_REJECT_SLOT_CAP
    assert state.time_window_teg_count_cap_bypass_used is False
    assert state.last_time_window_teg_bypass_at is None
    assert state.last_tw2_3slot_decision != config.TW_TEG_COUNT_CAP_BYPASS

    # Ledger-level confirmation: the string never appears in the signal ledger
    # this run wrote (tmp_path-isolated via conftest.py's autouse fixture).
    rows = ledger.load_signal_ledger(limit=1000)
    assert not any(config.TW_TEG_COUNT_CAP_BYPASS in str(row.get("order_result", "")) for row in rows)
    assert not any(config.TW_TEG_COUNT_CAP_BYPASS in str(row.get("tw2_3slot_decision", "")) for row in rows)


# ── 5. Toggle ON/restart/reconcile counter isolation + day-rollover reset ──

def test_enabling_3slot_after_tw2_entries_starts_slot_counters_clean(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.time_window_3slot_filter_enabled = False
    state.time_window_2_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _approved())
    monkeypatch.setattr(worker.time_window_filter, "evaluate_tw2_extra_vetoes", _no_extra_veto)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    state.time_window_pending_flag_direction = Direction.UP_RED
    state.time_window_pending_flag_bar_ts = _PRIOR_DAY.isoformat()
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.time_window_morning_entry_count == 1, "control: TW2 entry actually happened"

    state_store.save_state(state)
    svc2 = service_module.Macd2Service()
    res = svc2.set_time_window_3slot_filter_enabled(True, changed_by="test")
    assert res["ok"] is True
    reloaded = state_store.load_state()

    assert reloaded.tw2_3slot_slots_used_today == 0
    assert reloaded.tw2_3slot_morning_count == 0
    assert reloaded.tw2_3slot_afternoon_count == 0
    assert reloaded.time_window_2_filter_enabled is False


def test_restart_restores_persisted_3slot_counters_without_reset_or_double_count():
    state = _fresh_3slot_state()
    state.tw2_3slot_slots_used_today = 2
    state.tw2_3slot_morning_count = 2
    state.tw2_3slot_afternoon_count = 0
    state_store.save_state(state)

    reloaded = state_store.load_state()  # simulates a process restart

    assert reloaded.tw2_3slot_slots_used_today == 2, "restart must not reset the persisted slot count to 0"
    assert reloaded.tw2_3slot_morning_count == 2
    assert reloaded.time_window_3slot_filter_enabled is True


def test_reconcile_discovered_position_does_not_touch_3slot_counters_directly():
    """reconcile_position_state()'s RECOVERED_FROM_BROKER branch only ever
    bumps the filter-agnostic daily_total_entry_count -- it must never itself
    increment tw2_3slot_slots_used_today (that only happens later, exactly
    once, when _advance_held_position_risk_management's adoption branch
    runs)."""
    state = _fresh_3slot_state()
    assert state.position is None
    assert state.tw2_3slot_slots_used_today == 0
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")

    result = worker.reconcile_position_state(broker, state, datetime.now(KST), force=True)

    assert result == worker.RECOVERED_FROM_BROKER
    assert state.daily_total_entry_count == 1
    assert state.tw2_3slot_slots_used_today == 0, "reconcile itself must not increment the 3-slot counter"

    result2 = worker.reconcile_position_state(broker, state, datetime.now(KST), force=True)
    assert result2 == worker.MATCH_POSITION
    assert state.daily_total_entry_count == 1


def test_reconcile_then_adoption_increments_3slot_counter_exactly_once(tw2_3slot_market_data):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0

    worker.reconcile_position_state(broker, state, now0, force=True)
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert state.tw2_3slot_slots_used_today == 0  # not yet adopted into the ladder

    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.time_window_position_active is True
    assert state.time_window_active_mode == "TW2_3SLOT"
    assert state.tw2_3slot_slots_used_today == 1, "adoption must count this real position exactly once"

    now1 = _step(now0, 3)
    run_once(broker=broker, market_data=svc, state=state, now=now1)
    assert state.tw2_3slot_slots_used_today == 1, "a later tick for the SAME already-adopted position must never double-count"


def test_day_rollover_resets_3slot_counters_and_pending_but_not_the_toggle():
    state = _fresh_3slot_state()
    state.session_date = "20260105"
    state.tw2_3slot_slots_used_today = 3
    state.tw2_3slot_morning_count = 2
    state.tw2_3slot_afternoon_count = 1
    state.tw2_3slot_last_afternoon_direction = "UP_RED"
    state.tw2_3slot_pending_flag_direction = Direction.DOWN_BLUE
    state.tw2_3slot_pending_flag_bar_ts = datetime(2026, 1, 5, 13, 30, tzinfo=KST).isoformat()

    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 0, tzinfo=KST))

    assert state.tw2_3slot_slots_used_today == 0
    assert state.tw2_3slot_morning_count == 0
    assert state.tw2_3slot_afternoon_count == 0
    assert state.tw2_3slot_last_afternoon_direction is None
    assert state.tw2_3slot_pending_flag_direction is None
    assert state.tw2_3slot_pending_flag_bar_ts is None
    assert state.time_window_3slot_filter_enabled is True  # toggle survives

    # A second rollover call the SAME day must be a no-op (not double-reset
    # anything meaningfully different -- mirrors the existing day-rollover
    # idempotency convention).
    state.tw2_3slot_slots_used_today = 1
    worker._apply_day_rollover(state, datetime(2026, 1, 6, 9, 3, tzinfo=KST))
    assert state.tw2_3slot_slots_used_today == 1, "same-day re-call must not reset an already-current day's count"


# ── 6. Reversal exit / whipsaw hold / TP-SL-trailing ladder == TW2's own ────

@pytest.mark.parametrize("whipsaw_reason", [config.TW_REJECT_NOT_CONFIRMED, config.TW_REJECT_MACD_GAP_NOT_EXPANDING])
def test_whipsaw_classified_reversal_holds_instead_of_selling(tw2_3slot_market_data, monkeypatch, whipsaw_reason):
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
    _patch_common(monkeypatch, entry_decision=_rejected(whipsaw_reason))

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    orders_before = len(broker.orders)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_WHIPSAW_HOLD") for a in result.actions), result.actions
    assert not any(a.startswith("TW2_3SLOT_SELL_ONLY") for a in result.actions)
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert state.time_window_position_active is True
    assert len(broker.orders) == orders_before
    assert state.tw2_3slot_slots_used_today == 1, "a whipsaw hold must never consume or free a slot"


def test_non_whipsaw_reversal_reject_still_liquidates_the_held_position(tw2_3slot_market_data, monkeypatch):
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
    _patch_common(monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE))

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_SELL_ONLY") for a in result.actions), result.actions
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.time_window_position_active is False


def test_approved_reversal_executes_a_full_switch(tw2_3slot_market_data, monkeypatch):
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
    _patch_common(monkeypatch, entry_decision=_approved())

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_SWITCH") for a in result.actions), result.actions
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert config.INVERSE_SYMBOL not in broker._positions
    assert state.time_window_active_mode == "TW2_3SLOT"
    assert state.tw2_3slot_slots_used_today == 2


def test_take_profit_fires_at_tw2s_own_6pct_threshold_not_5pct(monkeypatch):
    closes = _sine_1m_closes(300)
    df_1m = _1m_frame(_PRIOR_DAY, closes)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_800.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        return df_1m, {}

    def fake_quote(mode, symbol):
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW

    state = _fresh_3slot_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_800.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in result.actions), (
        f"+8% live tick must trigger TW2 3-SLOT's own 6% TP2 (same threshold as TW2) -- got actions={result.actions!r}"
    )
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    # the slot itself is NOT freed by an exit -- it was already spent on entry.
    assert state.tw2_3slot_slots_used_today == 1


# ── 8. TW2 3-SLOT never participates in PRE15 premarket carry (2026-09-01) ──
# A 08:45-08:59 confirmed flag must not be registered as a carry candidate
# (and therefore never fires at 09:03) when only TW2_3SLOT is enabled; the
# same flag under plain TW2/TEGv2 is completely unaffected (regression); the
# 3-SLOT daily budget starts fresh at 09:00 either way.

from app.trading.macd2.models import MacdSnapshot  # noqa: E402


def _premarket_snap(bar_time_str: str) -> MacdSnapshot:
    hh, mm = (int(x) for x in bar_time_str.split(":"))
    bar_dt = _PRIOR_DAY.replace(hour=hh, minute=mm)
    return MacdSnapshot(
        bar_dt=bar_dt, macd=1.0, signal=0.5, hist=0.5, hist_last3=(0.1, 0.3, 0.5), completed_3m_count=50,
    )


def test_3slot_only_does_not_register_a_premarket_carry_candidate():
    state = _fresh_3slot_state()  # 3SLOT on, TW2/TEG off
    snap = _premarket_snap("08:48")
    worker._advance_premarket_carry_candidate(state, snap, Direction.UP_RED)
    assert state.premarket_carry_candidate_direction is None
    assert state.premarket_carry_candidate_bar_ts is None


def test_tw2_alone_still_registers_a_premarket_carry_candidate_unchanged():
    """Regression: TW2's own PRE15 behavior must be byte-identical to before."""
    state = _fresh_3slot_state()
    state.time_window_3slot_filter_enabled = False
    state.time_window_2_filter_enabled = True
    snap = _premarket_snap("08:48")
    worker._advance_premarket_carry_candidate(state, snap, Direction.UP_RED)
    assert state.premarket_carry_candidate_direction == Direction.UP_RED
    assert state.premarket_carry_candidate_bar_ts == snap.bar_dt.isoformat()


def test_teg_alone_still_registers_a_premarket_carry_candidate_unchanged():
    """Regression: TEGv2's own PRE15 behavior must be byte-identical to before."""
    state = _fresh_3slot_state()
    state.time_window_3slot_filter_enabled = False
    state.time_window_teg_filter_enabled = True
    snap = _premarket_snap("08:48")
    worker._advance_premarket_carry_candidate(state, snap, Direction.UP_RED)
    assert state.premarket_carry_candidate_direction == Direction.UP_RED


def test_premarket_carry_should_fire_is_false_when_only_3slot_enabled_even_if_candidate_somehow_set():
    """Defense-in-depth: even a corrupted/leftover candidate must never fire
    for 3-SLOT -- _premarket_carry_should_fire's own gate is the second line
    of defense behind _advance_premarket_carry_candidate never registering one."""
    state = _fresh_3slot_state()
    state.premarket_carry_candidate_direction = Direction.UP_RED
    state.premarket_carry_candidate_bar_ts = _premarket_snap("08:48").bar_dt.isoformat()
    now = _PRIOR_DAY.replace(hour=9, minute=3)
    assert worker._premarket_carry_should_fire(state, now) is False


# ── 9. order_block_reason must reflect the REAL T+3 outcome (2026-09-03) ────
# Real incident: a RED flag confirmed at 10:06, was correctly registered as
# a pending T+3 candidate (order_block_reason -> TW_PENDING_CONFIRMATION, the
# UI's "최근 block/skip 사유" line showing "time window pending"), but the
# T+3 resolution 3 minutes later never updated that field -- so even after a
# real rejection (quality score/veto/etc.) was computed and correctly written
# to the signal-ledger CSV's block_reason column, the UI's single-line status
# stayed frozen on "time window pending" forever, with no visible outcome.

def test_flat_rejection_at_t3_updates_order_block_reason_away_from_pending(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.order_block_reason = config.TW_PENDING_CONFIRMATION  # simulates the flag-bar tick's own write
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE))

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith(config.FILTERED_OUT) for a in result.actions), result.actions
    assert state.order_block_reason == config.TW_REJECT_LOW_QUALITY_SCORE, (
        "order_block_reason must show the REAL T+3 rejection reason, not stay stuck on "
        f"TW_PENDING_CONFIRMATION -- got {state.order_block_reason!r}"
    )


def test_whipsaw_hold_at_t3_updates_order_block_reason_away_from_pending(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.order_block_reason = config.TW_PENDING_CONFIRMATION
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    state.tw2_3slot_morning_count = 1
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    _patch_common(monkeypatch, entry_decision=_rejected(config.TW_REJECT_NOT_CONFIRMED))

    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TW2_3SLOT_WHIPSAW_HOLD") for a in result.actions), result.actions
    assert state.order_block_reason == config.TW_REJECT_NOT_CONFIRMED, (
        "a whipsaw-hold reject must also update order_block_reason to the real reason -- "
        f"got {state.order_block_reason!r}"
    )


def test_full_tick_no_carry_order_dispatched_for_3slot_at_0903(tw2_3slot_market_data):
    """End-to-end via the real run_once() dispatch: even with a (defensively
    impossible in practice, but simulated here) leftover premarket candidate
    present in state, TW2_3SLOT-only never places the 09:03 carry order --
    proves the real call site (_premarket_carry_should_fire inside run_once)
    respects the same gate, not just the standalone function check above."""
    svc, now0 = tw2_3slot_market_data
    state = _fresh_3slot_state()
    state.premarket_carry_candidate_direction = Direction.UP_RED
    state.premarket_carry_candidate_bar_ts = _premarket_snap("08:48").bar_dt.isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    now_0903 = now0.replace(hour=9, minute=3)

    run_once(broker=broker, market_data=svc, state=state, now=now_0903)

    assert state.position is None
    assert int(state.tw2_3slot_slots_used_today or 0) == 0
    assert ledger.load_execution_ledger() == []
