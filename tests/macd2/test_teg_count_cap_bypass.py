"""Worker-level integration tests for the TEG count-cap bypass wiring in
worker._resolve_time_window_candidate (2026-08-27) -- mirrors tests/macd2/
test_worker_time_window.py's own harness/fixtures (imported directly, no
duplicated infrastructure). Forces decisions via monkeypatch (same technique
test_tw2_veto_blocks_an_entry_the_base_gate_would_have_approved uses) rather
than constructing a real multi-hour price series that naturally exhausts the
daily entry cap -- this isolates the WIRING logic (worker.py's own bypass
block) from the TEG condition logic itself (already covered condition-by-
condition in test_teg_gate.py)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import config, teg_gate, worker
from app.trading.macd2.models import Direction, MajorFlagDecision
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_worker_time_window import (
    _PRIOR_DAY,
    _fresh_state,
    _prime_pending,
    tw_market_data,  # noqa: F401  (fixture, re-exported via import)
)

KST = config.KST


def _max_entry_count_rejection() -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=False, score=4.0, required_score=4.0, decision=config.TW_REJECT_MAX_ENTRY_COUNT,
        reasons=(config.TW_REJECT_MAX_ENTRY_COUNT,), component_scores={}, metrics={"window": "W1_MORNING_AGGRESSIVE"},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_REJECT_MAX_ENTRY_COUNT,
    )


def _other_rejection(reason: str = config.TW_REJECT_LOW_QUALITY_SCORE) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=reason,
        reasons=(reason,), component_scores={}, metrics={"window": "W1_MORNING_AGGRESSIVE"},
        is_reversal=False, fast_reversal=False, block_reason=reason,
    )


def _approved_teg_decision() -> teg_gate.TEGDecision:
    return teg_gate.TEGDecision(approved=True, conditions={c: True for c in teg_gate.ALL_CONDITIONS}, metrics={}, reject_reasons=())


def _rejected_teg_decision() -> teg_gate.TEGDecision:
    return teg_gate.TEGDecision(
        approved=False, conditions={c: (c != teg_gate.COND_VWAP) for c in teg_gate.ALL_CONDITIONS},
        metrics={}, reject_reasons=(teg_gate.COND_VWAP,),
    )


def test_count_cap_rejection_with_teg_approval_bypasses_exactly_once(tw_market_data, monkeypatch):
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_teg_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _max_entry_count_rejection())
    monkeypatch.setattr(worker.time_window_filter, "evaluate_tw2_extra_vetoes", lambda *a, **kw: (False, None))
    monkeypatch.setattr(worker.teg_gate, "evaluate_teg", lambda *a, **kw: _approved_teg_decision())
    _prime_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    assert state.time_window_teg_count_cap_bypass_used is False
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions), (
        f"a count-cap-only rejection with TEG approval must bypass and enter -- got actions={result.actions!r}"
    )
    assert state.time_window_teg_count_cap_bypass_used is True
    assert state.last_time_window_decision == config.TW_TEG_COUNT_CAP_BYPASS
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", config.LONG_SYMBOL)]


def test_second_bypass_same_day_is_blocked(tw_market_data, monkeypatch):
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_teg_filter_enabled = True
    state.time_window_teg_count_cap_bypass_used = True  # already used earlier today
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _max_entry_count_rejection())
    monkeypatch.setattr(worker.time_window_filter, "evaluate_tw2_extra_vetoes", lambda *a, **kw: (False, None))
    monkeypatch.setattr(worker.teg_gate, "evaluate_teg", lambda *a, **kw: _approved_teg_decision())
    _prime_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions), (
        f"a second same-day bypass must be refused -- got actions={result.actions!r}"
    )
    assert broker.orders == []
    assert state.position is None


def test_count_cap_rejection_with_teg_rejection_stays_blocked(tw_market_data, monkeypatch):
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_teg_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _max_entry_count_rejection())
    monkeypatch.setattr(worker.time_window_filter, "evaluate_tw2_extra_vetoes", lambda *a, **kw: (False, None))
    monkeypatch.setattr(worker.teg_gate, "evaluate_teg", lambda *a, **kw: _rejected_teg_decision())
    _prime_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions)
    assert state.time_window_teg_count_cap_bypass_used is False
    assert broker.orders == []


def test_rejection_for_other_reason_is_never_rescued_by_teg(tw_market_data, monkeypatch):
    """A candidate rejected for VWAP veto / quality score / anything other
    than the count cap must stay rejected even if TEG would have approved
    it -- the bypass ONLY ever considers TW_REJECT_MAX_ENTRY_COUNT."""
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_teg_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _other_rejection())
    teg_called = {"count": 0}

    def _spy_evaluate_teg(*a, **kw):
        teg_called["count"] += 1
        return _approved_teg_decision()

    monkeypatch.setattr(worker.teg_gate, "evaluate_teg", _spy_evaluate_teg)
    _prime_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions)
    assert broker.orders == []
    assert teg_called["count"] == 0, "TEG must never even be consulted for a non-count-cap rejection"
    assert state.time_window_teg_count_cap_bypass_used is False


def test_tw2_itself_never_gets_the_bypass(tw_market_data, monkeypatch):
    """The bypass is a TEG-filter-only feature -- plain TW2 (time_window_2_
    filter_enabled) must stay rejected on a count-cap block even if TEG
    would have approved it."""
    svc, now0 = tw_market_data
    state = _fresh_state()
    state.time_window_teg_filter_enabled = False
    state.time_window_2_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    monkeypatch.setattr(worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: _max_entry_count_rejection())
    teg_called = {"count": 0}

    def _spy_evaluate_teg(*a, **kw):
        teg_called["count"] += 1
        return _approved_teg_decision()

    monkeypatch.setattr(worker.teg_gate, "evaluate_teg", _spy_evaluate_teg)
    _prime_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(a.startswith("TIME_WINDOW_ENTRY") or a.startswith("TIME_WINDOW_SWITCH") for a in result.actions)
    assert broker.orders == []
    assert teg_called["count"] == 0
