"""Optional 추세전환장(sideways/whipsaw) entry filter tests — time-aware v3
gate (2026-08-07). Covers:

A. `_within_time_gate_window` boundary correctness (11:00 inclusive start,
   14:00 exclusive end).
B. Inside 11:00-14:00: score/breakout gate behaves exactly like before
   (unchanged v2 logic).
C. Outside 11:00-14:00: every already-confirmed crossover is approved
   unconditionally, regardless of score or breakout — but score/metrics are
   still computed and reported for observability.
D. Data-insufficiency / invalid-direction rejections are unaffected by time
   (these are input-validation gates, not part of the score/time logic).
E. Worker integration — the SAME confirmed flag is blocked at a time inside
   the gate window and approved at a time outside it; STOP_LOSS remains
   completely ungated regardless of time bucket (docs: filter is order-gate
   only, risk exits are never filtered).

Reuses the exact production compute_component_scores (via flat synthetic
bars, no crossover shaping needed since sideways_filter doesn't judge
crossover-ness itself) and monkeypatches only score_for_direction (as
sideways_filter imported it) to pin an exact total/breakout — same pattern
tests/macd2/test_major_flag_filter.py uses for MAJOR_FLAG's own threshold
tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, sideways_filter, state_store
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.sideways_filter import (
    _within_time_gate_window,
    evaluate_primary_trend_pullback,
    evaluate_sideways_flag,
)
from app.trading.macd2.signal_engine import forming_bar_window
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST
_BASE_PRICE = 1000.0
_BASE_SPREAD = 5.0
_BASE_VOLUME = 1000.0
_BASE_BARS = 40
_DAY1 = datetime(2026, 7, 28, 9, 0, tzinfo=KST)


def _flat_bars(n: int = _BASE_BARS, *, start: datetime = _DAY1) -> pd.DataFrame:
    """compute_component_scores/​_prepare_bars only need valid, sufficient
    completed 3m bars — the crossover shape itself is irrelevant here since
    sideways_filter never re-judges crossover-ness (that already happened in
    signal_engine before this gate is ever called)."""
    rows = [
        {
            "datetime": start + timedelta(minutes=3 * i),
            "open": _BASE_PRICE, "high": _BASE_PRICE + _BASE_SPREAD,
            "low": _BASE_PRICE - _BASE_SPREAD, "close": _BASE_PRICE, "volume": _BASE_VOLUME,
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows)


def _patch_score(monkeypatch, total: float, *, breakout: bool) -> None:
    def fake_score_for_direction(scores_template, metrics, flag_direction):
        del scores_template, flag_direction
        scores = {
            "hist_impulse": float(total), "price_strength": 0.0, "body": 0.0,
            "volume": 0.0, "ema10_trend": 0.0, "ema20_or_vwap": 0.0, "volatility": 0.0,
        }
        shaped = dict(metrics)
        shaped["breakout"] = breakout
        return scores, shaped

    monkeypatch.setattr(sideways_filter, "score_for_direction", fake_score_for_direction)


def _at(hour: int, minute: int = 0, second: int = 0, *, day: int = 6) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=KST)


# ══════════════════════════════════════════════════════════════════════════
# A. Time-gate window boundaries
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    ("now", "inside"),
    [
        (_at(9, 0), False),
        (_at(10, 59, 59), False),
        (_at(11, 0, 0), True),   # start inclusive
        (_at(12, 30), True),
        (_at(13, 59, 59), True),
        (_at(14, 0, 0), False),  # end exclusive
        (_at(15, 20), False),
    ],
)
def test_time_gate_window_boundaries(now, inside):
    assert _within_time_gate_window(now) is inside


# ══════════════════════════════════════════════════════════════════════════
# B. Inside 11:00-14:00 — unchanged score<max-and-not-breakout gate
# ══════════════════════════════════════════════════════════════════════════
def test_inside_window_low_score_no_breakout_is_approved(monkeypatch):
    _patch_score(monkeypatch, total=40.0, breakout=False)
    decision = evaluate_sideways_flag(_flat_bars(), Direction.UP_RED, _at(12, 0))

    assert decision.approved is True
    assert decision.decision == config.SIDEWAYS_APPROVED
    assert decision.score == 40.0
    assert decision.required_score == config.SIDEWAYS_ENTRY_SCORE_MAX


def test_inside_window_high_score_is_rejected(monkeypatch):
    _patch_score(monkeypatch, total=65.0, breakout=False)
    decision = evaluate_sideways_flag(_flat_bars(), Direction.UP_RED, _at(12, 0))

    assert decision.approved is False
    assert decision.decision == config.SIDEWAYS_SCORE_ABOVE_THRESHOLD
    assert decision.block_reason == config.SIDEWAYS_SCORE_ABOVE_THRESHOLD


def test_inside_window_low_score_breakout_is_rejected(monkeypatch):
    _patch_score(monkeypatch, total=40.0, breakout=True)
    decision = evaluate_sideways_flag(_flat_bars(), Direction.UP_RED, _at(12, 0))

    assert decision.approved is False
    assert decision.decision == config.SIDEWAYS_BREAKOUT_BLOCKED


def test_inside_window_boundary_score_exactly_at_max_is_rejected(monkeypatch):
    _patch_score(monkeypatch, total=config.SIDEWAYS_ENTRY_SCORE_MAX, breakout=False)
    decision = evaluate_sideways_flag(_flat_bars(), Direction.UP_RED, _at(11, 30))

    assert decision.approved is False
    assert decision.decision == config.SIDEWAYS_SCORE_ABOVE_THRESHOLD


# ══════════════════════════════════════════════════════════════════════════
# C. Outside 11:00-14:00 — unconditional approval (2026-08-07 v3)
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("now", [_at(9, 5), _at(10, 59), _at(14, 0), _at(15, 20)])
def test_outside_window_high_score_breakout_is_still_approved(monkeypatch, now):
    _patch_score(monkeypatch, total=100.0, breakout=True)
    decision = evaluate_sideways_flag(_flat_bars(), Direction.UP_RED, now)

    assert decision.approved is True
    assert decision.decision == config.SIDEWAYS_APPROVED_OUTSIDE_GATE_WINDOW
    assert decision.block_reason is None


@pytest.mark.parametrize("now", [_at(9, 5), _at(15, 20)])
def test_outside_window_low_score_is_also_approved(monkeypatch, now):
    """Same low-score shape that would be approved inside the window too —
    outside it, approval doesn't depend on score at all."""
    _patch_score(monkeypatch, total=10.0, breakout=False)
    decision = evaluate_sideways_flag(_flat_bars(), Direction.UP_RED, now)

    assert decision.approved is True
    assert decision.decision == config.SIDEWAYS_APPROVED_OUTSIDE_GATE_WINDOW


def test_outside_window_still_reports_score_and_metrics_for_observability(monkeypatch):
    _patch_score(monkeypatch, total=77.0, breakout=True)
    decision = evaluate_sideways_flag(_flat_bars(), Direction.UP_RED, _at(9, 30))

    assert decision.score == 77.0
    assert decision.component_scores  # not empty — real scoring still ran
    assert decision.metrics.get("breakout") is True


# ══════════════════════════════════════════════════════════════════════════
# D. Input-validation gates are time-independent
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("now", [_at(9, 30), _at(12, 0), _at(15, 0)])
def test_insufficient_data_is_rejected_regardless_of_time(now):
    decision = evaluate_sideways_flag(None, Direction.UP_RED, now)

    assert decision.approved is False
    assert decision.decision == config.FILTER_DATA_INSUFFICIENT


@pytest.mark.parametrize("now", [_at(9, 30), _at(12, 0), _at(15, 0)])
def test_invalid_direction_is_rejected_regardless_of_time(now):
    decision = evaluate_sideways_flag(_flat_bars(), "HOLD", now)

    assert decision.approved is False
    assert decision.decision == config.FILTER_INPUT_NOT_CROSSOVER


# ══════════════════════════════════════════════════════════════════════════
# E. Worker integration — order gate only, time bucket controls admission
# ══════════════════════════════════════════════════════════════════════════
_WORKER_QUOTES = {
    config.WATCH_SYMBOL: 140.0,
    config.LONG_SYMBOL: 15_000.0,
    config.INVERSE_SYMBOL: 10_000.0,
}


def _1m_from_3m_closes(start: datetime, closes: list) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        bar_start = start + timedelta(minutes=3 * i)
        for j in range(3):
            rows.append({
                "datetime": bar_start + timedelta(minutes=j),
                "open": close, "high": close, "low": close, "close": close, "volume": 10,
            })
    return pd.DataFrame(rows)


def _svc_with_quote(df_1m: pd.DataFrame, bootstrap_now: datetime, quote_prices: dict) -> MarketDataService:
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _fresh_state() -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.sideways_filter_enabled = True
    return state


def _confirmed_flag_scenario(*, n: int):
    """A single confirmed UP_RED crossover (big jump on the last two bars —
    same shape tests/macd2/test_major_flag_filter.py uses, guaranteed to
    score far above SIDEWAYS_ENTRY_SCORE_MAX and past 4-bar-breakout).
    ``worker_start`` is pinned to an exact-minute 09:00 (matching the
    original test_major_flag_filter.py pattern) so every generated 1-minute
    timestamp lands on a whole minute — offsetting worker_start itself by a
    few seconds instead (rather than only the returned `now`) silently makes
    every completed 3m bar fail market_data.filter_complete_3m_bars's exact
    per-minute presence check, producing a NOT_READY skip that looks like a
    gate rejection but is actually a test-harness bug. `n` (3m-bar count)
    is the only knob used to move the confirmation moment (`now`) across the
    09:00-11:00 / 11:00-14:00 / 14:00-15:30 buckets."""
    worker_start = _at(9, 0, day=6)
    confirm_at = worker_start + timedelta(minutes=3 * n, seconds=5)
    df_1m = _1m_from_3m_closes(worker_start, [100.0] * (n - 3) + [99.5, 99.9, 140.0])
    state = _fresh_state()
    state.last_confirmed_bar_ts = (worker_start + timedelta(minutes=3 * (n - 2))).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes=dict(_WORKER_QUOTES))
    svc = _svc_with_quote(df_1m, confirm_at, _WORKER_QUOTES)
    return svc, state, broker, confirm_at


def test_flag_inside_gate_window_is_blocked():
    svc, state, broker, confirm_at = _confirmed_flag_scenario(n=70)  # -> 12:30:05, bucket B
    assert _within_time_gate_window(confirm_at)

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert not any(a.startswith("ENTRY:") for a in result.actions)
    assert state.position is None
    assert state.last_sideways_approved is False
    assert state.last_sideways_decision in (config.SIDEWAYS_SCORE_ABOVE_THRESHOLD, config.SIDEWAYS_BREAKOUT_BLOCKED)


def test_the_same_shaped_flag_outside_gate_window_is_approved():
    svc, state, broker, confirm_at = _confirmed_flag_scenario(n=35)  # -> 10:45:05, bucket A
    assert not _within_time_gate_window(confirm_at)

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert state.last_sideways_approved is True
    assert state.last_sideways_decision == config.SIDEWAYS_APPROVED_OUTSIDE_GATE_WINDOW


def test_stop_loss_still_exits_outside_the_gate_window():
    """The filter has no authority over risk exits at any time of day (docs:
    STOP_LOSS / PROFIT_LOCK / FORCED_LIQUIDATION are never filtered)."""
    now = _at(10, 0)
    df_1m = _1m_from_3m_closes(now - timedelta(minutes=300), [100.0] * 100)
    svc = _svc_with_quote(df_1m, now, {**_WORKER_QUOTES, config.LONG_SYMBOL: 14_000.0})

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, 14_000.0)
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)
    bar_start, _ = forming_bar_window(now)
    state.stop_loss_bar_symbol = config.LONG_SYMBOL
    state.stop_loss_entry_bar_ts = (bar_start - timedelta(minutes=6)).isoformat()
    state.stop_loss_bar_ts = (bar_start - timedelta(minutes=3)).isoformat()
    state.stop_loss_bar_close = 14_000.0

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("STOP_LOSS:") for a in result.actions)
    assert state.position is None
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert ledger.load_execution_ledger()[-1]["exit_reason"] == config.EXIT_STOP_LOSS


# ══════════════════════════════════════════════════════════════════════════
# F. PRIMARY_TREND pullback filter (2026-08-07, folded into sideways_filter) —
# a confirmed flag running AGAINST today's dominant trend is rejected as a
# pullback regardless of the score gate above; a flag AGREEING with the
# dominant trend (or any flag on a RANGE day) is untouched by this check.
# ══════════════════════════════════════════════════════════════════════════
def _trending_1m_bars(start: datetime, n: int, *, per_bar_pct: float, base: float = 1000.0) -> pd.DataFrame:
    """A monotonic per-bar % move — enough duration for compute_primary_
    trend's 15m/30m EMA-slope and swing-structure votes to read a real
    trend instead of defaulting to RANGE."""
    rows = []
    price = base
    for i in range(n):
        price = price * (1 + per_bar_pct / 100.0)
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": price, "high": price + 0.3, "low": price - 0.3, "close": price,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def test_primary_trend_pullback_rejects_counter_trend_flag_on_down_day():
    start = _at(9, 0)
    df_1m = _trending_1m_bars(start, 90, per_bar_pct=-0.08)
    now = start + timedelta(minutes=90)

    decision = evaluate_primary_trend_pullback(df_1m, Direction.UP_RED, now)

    assert decision is not None
    assert decision.approved is False
    assert decision.decision == config.SIDEWAYS_PRIMARY_TREND_PULLBACK_BLOCKED
    assert decision.metrics.get("primary_trend") == "DOWN"


def test_primary_trend_pullback_allows_aligned_flag_on_down_day():
    start = _at(9, 0)
    df_1m = _trending_1m_bars(start, 90, per_bar_pct=-0.08)
    now = start + timedelta(minutes=90)

    assert evaluate_primary_trend_pullback(df_1m, Direction.DOWN_BLUE, now) is None


def test_primary_trend_pullback_never_rejects_on_range_day():
    start = _at(9, 0)
    df_1m = _1m_from_3m_closes(start, [100.0] * 40)
    now = start + timedelta(minutes=120)

    assert evaluate_primary_trend_pullback(df_1m, Direction.UP_RED, now) is None
    assert evaluate_primary_trend_pullback(df_1m, Direction.DOWN_BLUE, now) is None


def test_primary_trend_pullback_follows_a_genuine_mid_day_reversal():
    """A brief counter-trend flag is rejected while the decline is still the
    dominant trend, but once price has genuinely reversed (VWAP/15m-30m
    slope/swing structure all flip together over a real stretch of time,
    not just a 1-2 bar blip), the SAME direction is no longer a pullback —
    PRIMARY_TREND is recomputed fresh every call, never frozen from earlier
    in the session."""
    start = _at(9, 0)
    down_leg = _trending_1m_bars(start, 90, per_bar_pct=-0.08)
    up_start = start + timedelta(minutes=90)
    up_leg = _trending_1m_bars(up_start, 90, per_bar_pct=0.12, base=float(down_leg["close"].iloc[-1]))
    df_1m = pd.concat([down_leg, up_leg], ignore_index=True)

    mid_now = up_start
    still_down_decision = evaluate_primary_trend_pullback(
        df_1m[df_1m["datetime"] <= mid_now], Direction.UP_RED, mid_now,
    )
    assert still_down_decision is not None and still_down_decision.approved is False

    late_now = start + timedelta(minutes=180)
    reversed_decision = evaluate_primary_trend_pullback(df_1m, Direction.UP_RED, late_now)
    assert reversed_decision is None


def test_worker_sells_held_position_without_reentry_when_flag_is_a_pullback(monkeypatch):
    """End-to-end wiring check (worker.run_once): a confirmed opposite flag
    classified as a PRIMARY_TREND pullback liquidates the held ETF (same
    sell-only/no-re-entry path already used for a MAJOR/score-gate-rejected
    reversal) but never buys the opposite ETF. The classification itself is
    covered by the direct evaluate_primary_trend_pullback tests above; this
    monkeypatches it to a fixed verdict (same style test_major_flag_filter.py
    uses for its own filter-rejection worker test) so this test only proves
    the plumbing, not the trend math."""
    def _force_pullback_rejection(df_1m, flag_direction, now):
        del df_1m, now
        if sideways_filter._as_direction(flag_direction) != Direction.UP_RED:
            return None
        return sideways_filter._reject(
            decision=config.SIDEWAYS_PRIMARY_TREND_PULLBACK_BLOCKED,
            block_reason=config.SIDEWAYS_PRIMARY_TREND_PULLBACK_BLOCKED,
            reasons=["forced test pullback"],
        )

    monkeypatch.setattr(sideways_filter, "evaluate_primary_trend_pullback", _force_pullback_rejection)
    svc, state, broker, confirm_at = _confirmed_flag_scenario(n=35)  # confirmed UP_RED flag, outside gate window
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-inverse")
    state.position = PositionSnapshot(
        symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=_WORKER_QUOTES[config.INVERSE_SYMBOL],
        entry_at=confirm_at - timedelta(minutes=12),
    )
    state.last_detected_direction = Direction.DOWN_BLUE

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert result.actions == ["OPPOSITE_SIGNAL_SELL_ONLY:UP_RED"]
    assert state.position is None
    assert broker.get_position(config.INVERSE_SYMBOL) is None
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert state.last_sideways_decision == config.SIDEWAYS_PRIMARY_TREND_PULLBACK_BLOCKED
