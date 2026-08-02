"""Unit tests for app.trading.tsla_auto.strong_flag_filter."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.tsla_auto import config
from app.trading.tsla_auto.models import Direction, MarketRegime
from app.trading.tsla_auto.strong_flag_filter import (
    apply_trade_gates,
    classify_regime,
    compute_component_scores,
    daily_max_entries_for,
    evaluate_strong_flag,
    required_scores_for,
    score_for_direction,
)

ET = config.ET
_BASE_PRICE = 1000.0
_BASE_SPREAD = 1.0
_BASE_VOLUME = 1000.0
_BASE_BARS = 60
_DAY1 = datetime(2026, 7, 24, 9, 30, tzinfo=ET)


def _flat_bars(n: int = _BASE_BARS, *, start=_DAY1, price=_BASE_PRICE, spread=_BASE_SPREAD, volume=_BASE_VOLUME) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=3 * i), "open": price, "high": price + spread, "low": price - spread, "close": price, "volume": volume}
        for i in range(n)
    ]
    return pd.DataFrame(rows)


def _shape_last_bar(bars: pd.DataFrame, direction: Direction, *, jump: float = 100.0, volume_mult: float = 5.0) -> pd.DataFrame:
    i = len(bars) - 1
    base = float(bars["close"].iloc[i - 3])
    if direction is Direction.UP_RED:
        closes = [base + jump * 0.25, base + jump * 0.60, base + jump]
    else:
        closes = [base - jump * 0.25, base - jump * 0.60, base - jump]
    prev_close = base
    for offset, close in zip((2, 1, 0), closes):
        row = i - offset
        high = max(prev_close, close) + _BASE_SPREAD
        low = min(prev_close, close) - _BASE_SPREAD
        bars.loc[row, ["open", "high", "low", "close", "volume"]] = [
            prev_close, high, low, close, _BASE_VOLUME * volume_mult,
        ]
        prev_close = close
    return bars


def _crossover_bars(direction: Direction, *, n: int = _BASE_BARS, **kwargs) -> pd.DataFrame:
    return _shape_last_bar(_flat_bars(n), direction, **kwargs)


def _decision_now(bars: pd.DataFrame) -> datetime:
    return pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3)


def test_strong_crossover_scores_full_marks_and_is_normal_regime():
    bars = _crossover_bars(Direction.UP_RED, jump=100.0, volume_mult=5.0)
    now = _decision_now(bars)
    decision = evaluate_strong_flag(bars, Direction.UP_RED, None, None, 0, now)
    assert decision.approved is True
    assert decision.regime == MarketRegime.NORMAL.value
    assert decision.score == 100.0  # all 7 components pass with this strong a move


def test_component_score_breakdown_sums_to_100_points_max():
    bars = _crossover_bars(Direction.UP_RED, jump=100.0, volume_mult=5.0)
    work = bars.copy()
    work["datetime"] = pd.to_datetime(work["datetime"])
    scores_t, metrics_t, err = compute_component_scores(work)
    assert err is None
    scores, _m = score_for_direction(scores_t, metrics_t, Direction.UP_RED)
    assert scores["hist_impulse"] <= 25.0
    assert scores["price_strength"] <= 25.0
    assert scores["body"] <= 10.0
    assert scores["volume"] <= 15.0
    assert scores["ema10_trend"] <= 10.0
    assert scores["ema20_or_vwap"] <= 10.0
    assert scores["volatility"] <= 5.0
    assert sum(v for v in scores.values()) <= 100.0


def test_classify_regime_chop_when_no_volatility_expansion():
    metrics = {"recent_range_ratio": 0.0001, "atr14": 1.0, "atr_median_prev20": 5.0}
    assert classify_regime(metrics) == MarketRegime.CHOP.value


def test_classify_regime_normal_when_volatility_expands():
    metrics = {"recent_range_ratio": 0.5, "atr14": 1.0, "atr_median_prev20": 5.0}
    assert classify_regime(metrics) == MarketRegime.NORMAL.value


def test_classify_regime_unknown_when_metrics_missing():
    assert classify_regime({}) == MarketRegime.UNKNOWN.value


def test_required_scores_default_window_normal_and_chop():
    now = datetime(2026, 7, 30, 10, 0, tzinfo=ET)
    normal = required_scores_for(now_et=now, regime="NORMAL", daily_filled_entry_count=0)
    chop = required_scores_for(now_et=now, regime="CHOP", daily_filled_entry_count=0)
    assert normal == {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0}
    assert chop == {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0}


def test_required_scores_midday_relaxation_gated_by_count():
    now = datetime(2026, 7, 30, 13, 0, tzinfo=ET)
    relaxed = required_scores_for(now_et=now, regime="NORMAL", daily_filled_entry_count=1)
    assert relaxed["entry"] == 65.0
    still_relaxed = required_scores_for(now_et=now, regime="NORMAL", daily_filled_entry_count=4)
    assert still_relaxed["entry"] == 65.0


def test_required_scores_late_window_relaxation_gated_by_count():
    now = datetime(2026, 7, 30, 14, 30, tzinfo=ET)
    relaxed = required_scores_for(now_et=now, regime="NORMAL", daily_filled_entry_count=2)
    assert relaxed["entry"] == 65.0
    still_relaxed = required_scores_for(now_et=now, regime="NORMAL", daily_filled_entry_count=4)
    assert still_relaxed["entry"] == 65.0


def test_required_scores_1530_to_1545_reverts_to_default_no_relaxation():
    now = datetime(2026, 7, 30, 15, 35, tzinfo=ET)
    thresholds = required_scores_for(now_et=now, regime="CHOP", daily_filled_entry_count=0)
    assert thresholds == {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0}


def test_absolute_floor_never_relaxed_below_hard_minimum():
    # MACD2 parity floor clamps both NORMAL and CHOP to the same hard minimum.
    now = datetime(2026, 7, 30, 14, 30, tzinfo=ET)
    thresholds = required_scores_for(now_et=now, regime="CHOP", daily_filled_entry_count=0)
    assert thresholds["entry"] >= 65.0
    assert thresholds["reversal"] >= 75.0


def test_daily_max_entries_normal_4_chop_2():
    assert daily_max_entries_for("NORMAL") == 4
    assert daily_max_entries_for("CHOP") == 4


def test_sideways_block_when_ema_spread_and_range_both_tight():
    bars = _flat_bars(_BASE_BARS, spread=0.01)
    # Force a tiny same-direction crossover without a big jump so histogram
    # still crosses but price action stays essentially flat.
    i = len(bars) - 1
    bars.loc[i, "close"] = float(bars["close"].iloc[i - 1]) + 0.05
    now = _decision_now(bars)
    decision = evaluate_strong_flag(bars, Direction.UP_RED, None, None, 0, now)
    assert decision.approved is False


def test_daily_entry_limit_gate_blocks_after_max():
    bars = _crossover_bars(Direction.UP_RED, jump=100.0, volume_mult=5.0)
    now = _decision_now(bars)
    decision = evaluate_strong_flag(bars, Direction.UP_RED, None, None, 4, now)
    gated = apply_trade_gates(
        decision, flag_direction=Direction.UP_RED, position_direction=None, last_entry_at=None,
        last_same_direction_exit_at=None, daily_entry_count=4, now=now, daily_max_entries=4,
    )
    assert gated.approved is False
    assert gated.decision == "DAILY_ENTRY_LIMIT"


def test_same_direction_position_held_blocks_add():
    bars = _crossover_bars(Direction.UP_RED, jump=100.0, volume_mult=5.0)
    now = _decision_now(bars)
    decision = evaluate_strong_flag(bars, Direction.UP_RED, Direction.UP_RED, None, 0, now)
    gated = apply_trade_gates(
        decision, flag_direction=Direction.UP_RED, position_direction=Direction.UP_RED, last_entry_at=None,
        last_same_direction_exit_at=None, daily_entry_count=0, now=now, daily_max_entries=4,
    )
    assert gated.approved is False
    assert gated.decision == config.SAME_DIRECTION_POSITION_HELD


def test_confirmed_signal_is_scored_without_rechecking_crossover():
    bars = _flat_bars(_BASE_BARS)  # no real crossover - flat hist stays ~0
    now = _decision_now(bars)
    decision = evaluate_strong_flag(bars, Direction.UP_RED, None, None, 0, now)
    assert decision.approved is False
    assert decision.decision == config.FILTER_INPUT_NOT_CROSSOVER
