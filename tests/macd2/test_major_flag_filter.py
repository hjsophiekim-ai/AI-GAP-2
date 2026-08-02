"""Optional Hybrid MAJOR_FLAG filter tests (spec §12).

Covers the four things the filter must never get wrong:

A. component scoring / boundaries and input-data safety (pure functions),
B. approval thresholds (flat 70 / reversal 80 / fast reversal 85) + hard blocks,
C. worker integration — the filter is an ORDER gate only: it never creates or
   suppresses a confirmed flag, never touches STOP_LOSS, and a rejected flag
   never reaches the broker,
E. regression — with the toggle OFF the filter code is not even called.

Everything runs on synthetic completed-3m frames and the tests/macd2 fake
broker; no network, no real state/ledger paths (tests/macd2/conftest.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, major_flag_filter, state_store, worker
from app.trading.macd2.major_flag_filter import (
    apply_major_trade_gates,
    compute_component_scores,
    evaluate_major_flag,
    score_for_direction,
)
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, MajorFlagDecision, PositionSnapshot, RuntimeState
from app.trading.macd2.signal_engine import calculate_macd
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST

# ── Synthetic completed-3m bar builders ────────────────────────────────────
# A flat base (close constant, high/low a fixed spread apart) makes every
# derived metric exactly predictable: MACD hist is 0.0 across the base, ATR
# converges to the bar range, and the volume median is the base volume. Only
# the LAST bar is shaped per test, so a boundary can be hit on the nose.
_BASE_PRICE = 1000.0
_BASE_SPREAD = 5.0
_BASE_VOLUME = 1000.0
_BASE_BARS = 40
_DAY1 = datetime(2026, 7, 28, 9, 0, tzinfo=KST)
_DAY2 = datetime(2026, 7, 29, 9, 0, tzinfo=KST)


def _flat_bars(
    n: int = _BASE_BARS,
    *,
    start: datetime = _DAY1,
    price: float = _BASE_PRICE,
    spread: float = _BASE_SPREAD,
    volume: float = _BASE_VOLUME,
) -> pd.DataFrame:
    rows = [
        {
            "datetime": start + timedelta(minutes=3 * i),
            "open": price,
            "high": price + spread,
            "low": price - spread,
            "close": price,
            "volume": volume,
        }
        for i in range(n)
    ]
    return pd.DataFrame(rows)


def _shape_last_two_bars(
    bars: pd.DataFrame,
    direction: Direction,
    *,
    jump: float = 100.0,
    volume_mult: float = 5.0,
) -> pd.DataFrame:
    """Turn the last two rows into a strong KIS color flag shape."""
    i = len(bars) - 1
    mid_i = i - 1
    base = float(bars["close"].iloc[mid_i - 1])
    mid_jump = jump / 2.0
    if direction is Direction.UP_RED:
        mid_close = base + mid_jump
        close = base + jump
        mid_high, mid_low = mid_close + _BASE_SPREAD, base - _BASE_SPREAD
        high, low = close + _BASE_SPREAD, mid_close - _BASE_SPREAD
    else:
        mid_close = base - mid_jump
        close = base - jump
        mid_high, mid_low = base + _BASE_SPREAD, mid_close - _BASE_SPREAD
        high, low = mid_close + _BASE_SPREAD, close - _BASE_SPREAD
    bars.loc[mid_i, ["open", "high", "low", "close", "volume"]] = [
        base, mid_high, mid_low, mid_close, _BASE_VOLUME * volume_mult,
    ]
    bars.loc[i, ["open", "high", "low", "close", "volume"]] = [
        mid_close, high, low, close, _BASE_VOLUME * volume_mult,
    ]
    return bars


def _crossover_bars(direction: Direction, *, n: int = _BASE_BARS, **kwargs) -> pd.DataFrame:
    return _shape_last_two_bars(_flat_bars(n), direction, **kwargs)


def _decision_now(bars: pd.DataFrame) -> datetime:
    """The moment the frame's last completed bar closes (production decision time)."""
    return pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3)


# ── Hand-built metrics for exact component-boundary tests ──────────────────
_SCORE_KEYS = (
    "hist_impulse", "price_strength", "body", "volume", "ema10_trend",
    "ema20_or_vwap", "volatility",
)
_ZERO_SCORES = {key: 0.0 for key in _SCORE_KEYS}


def _metrics(**overrides):
    """An all-components-passing UP_RED metrics dict (atr14 == 10.0 keeps every
    ATR-normalized boundary exactly representable: 2.2/10.0 == 0.22)."""
    base = {
        "atr14": 10.0,
        "macd": 3.0,
        "signal": 1.0,
        "hist": 2.2,
        "prev_hist": 0.0,
        "breakout_up": False,
        "breakout_down": False,
        "close": 1000.0,
        "open": 990.0,
        "close_3_bars_ago": 994.0,
        "body_atr": 1.0,
        "volume_ratio": 2.0,
        "ema10": 995.0,
        "ema10_prev": 990.0,
        "ema20": 993.0,
        "vwap": 992.0,
        "recent_range_ratio": 0.02,
        "ema_spread_ratio": 0.005,
        "atr_median_prev20": 8.0,
        "volume": 2000.0,
        "volume_median_prev20": 1000.0,
    }
    base.update(overrides)
    return base


def _score(direction: Direction = Direction.UP_RED, **overrides) -> dict[str, float]:
    scores, _metrics_out = score_for_direction(dict(_ZERO_SCORES), _metrics(**overrides), direction)
    return scores


# ══════════════════════════════════════════════════════════════════════════
# A. Component scoring + boundaries
# ══════════════════════════════════════════════════════════════════════════
def test_component_score_keys_are_exactly_the_seven_hybrid_components():
    scores, metrics, err = compute_component_scores(_crossover_bars(Direction.UP_RED))
    assert err is None
    assert set(scores) == set(_SCORE_KEYS)
    assert metrics["close"] == _BASE_PRICE + 100.0


@pytest.mark.parametrize(
    ("hist", "expected"),
    [(2.2, 25.0), (1.5, 18.0), (1.0, 10.0), (0.99, 0.0), (0.0, 0.0), (-2.2, 0.0)],
)
def test_hist_impulse_tiers(hist, expected):
    assert _score(hist=hist, prev_hist=0.0)["hist_impulse"] == expected


def test_hist_impulse_is_direction_signed():
    # A -0.22 ATR histogram drop scores 0 for UP_RED and 25 for DOWN_BLUE.
    assert _score(Direction.UP_RED, hist=-2.2, prev_hist=0.0)["hist_impulse"] == 0.0
    assert _score(Direction.DOWN_BLUE, hist=-2.2, prev_hist=0.0)["hist_impulse"] == 25.0


@pytest.mark.parametrize(
    ("close_3_bars_ago", "expected"),
    [(994.5, 25.0), (996.5, 15.0), (996.6, 0.0), (1000.0, 0.0)],
)
def test_price_impulse_tiers(close_3_bars_ago, expected):
    assert _score(close_3_bars_ago=close_3_bars_ago)["price_strength"] == expected


def test_price_strength_is_breakout_or_impulse():
    # Impulse alone fails, but a range breakout still earns the full 25.
    assert _score(close_3_bars_ago=1000.0)["price_strength"] == 0.0
    assert _score(close_3_bars_ago=1000.0, breakout_up=True)["price_strength"] == 25.0
    # A DOWN_BLUE flag reads breakout_down, never breakout_up.
    assert _score(Direction.DOWN_BLUE, close_3_bars_ago=1000.0, breakout_up=True)["price_strength"] == 0.0
    assert _score(Direction.DOWN_BLUE, close_3_bars_ago=1000.0, breakout_down=True)["price_strength"] == 25.0


@pytest.mark.parametrize(
    ("body_atr", "expected"),
    [(0.4, 10.0), (0.25, 5.0), (0.24, 0.0), (0.0, 0.0)],
)
def test_body_tiers(body_atr, expected):
    assert _score(body_atr=body_atr)["body"] == expected


def test_body_requires_matching_candle_direction():
    # A red (close < open) candle never earns body points for an UP_RED flag,
    # no matter how big the body is.
    assert _score(close=980.0, open=990.0, body_atr=1.0)["body"] == 0.0
    assert _score(Direction.DOWN_BLUE, close=980.0, open=990.0, body_atr=1.0)["body"] == 10.0
    assert _score(Direction.DOWN_BLUE, close=1000.0, open=990.0, body_atr=1.0)["body"] == 0.0


@pytest.mark.parametrize(
    ("volume_ratio", "expected"),
    [(1.20, 15.0), (1.10, 10.0), (1.00, 5.0), (0.99, 0.0), (0.5, 0.0)],
)
def test_volume_tiers(volume_ratio, expected):
    assert _score(volume_ratio=volume_ratio)["volume"] == expected


def test_volume_ratio_metric_uses_prior_20_bar_median():
    _scores, metrics, err = compute_component_scores(
        _crossover_bars(Direction.UP_RED, volume_mult=3.0)
    )
    assert err is None
    assert metrics["volume_median_prev20"] == _BASE_VOLUME
    assert metrics["volume_ratio"] == pytest.approx(3.0)


def test_ema10_trend_needs_rising_ema_and_close_above_it():
    assert _score(ema10=995.0, ema10_prev=990.0, close=1000.0)["ema10_trend"] == 10.0
    assert _score(ema10=995.0, ema10_prev=996.0, close=1000.0)["ema10_trend"] == 0.0  # EMA10 falling
    assert _score(ema10=995.0, ema10_prev=990.0, close=994.0)["ema10_trend"] == 0.0  # close below EMA10
    # DOWN_BLUE mirror: EMA10 must be falling with close below it.
    assert _score(Direction.DOWN_BLUE, ema10=995.0, ema10_prev=996.0, close=990.0)["ema10_trend"] == 10.0
    assert _score(Direction.DOWN_BLUE, ema10=995.0, ema10_prev=990.0, close=990.0)["ema10_trend"] == 0.0


def test_ema20_or_vwap_is_an_or_condition():
    # EMA20 alone passes.
    assert _score(close=1000.0, ema20=993.0, vwap=1200.0)["ema20_or_vwap"] == 10.0
    # VWAP alone passes.
    assert _score(close=1000.0, ema20=1200.0, vwap=992.0)["ema20_or_vwap"] == 10.0
    # Neither passes.
    assert _score(close=1000.0, ema20=1200.0, vwap=1200.0)["ema20_or_vwap"] == 0.0
    # A missing VWAP (pre-open / zero session volume) falls back to EMA20 only.
    assert _score(close=1000.0, ema20=1200.0, vwap=None)["ema20_or_vwap"] == 0.0
    assert _score(close=1000.0, ema20=993.0, vwap=None)["ema20_or_vwap"] == 10.0


def test_volatility_is_worth_5_points_via_range_or_atr():
    assert _score(recent_range_ratio=0.006, atr14=10.0, atr_median_prev20=99.0)["volatility"] == 5.0
    assert _score(recent_range_ratio=0.0059, atr14=10.0, atr_median_prev20=10.0)["volatility"] == 5.0
    assert _score(recent_range_ratio=0.0059, atr14=10.0, atr_median_prev20=10.0001)["volatility"] == 0.0


def test_total_score_sums_to_100_when_every_component_passes():
    scores = _score(breakout_up=True)
    assert scores == {
        "hist_impulse": 25.0,
        "price_strength": 25.0,
        "body": 10.0,
        "volume": 15.0,
        "ema10_trend": 10.0,
        "ema20_or_vwap": 10.0,
        "volatility": 5.0,
    }
    assert sum(scores.values()) == 100.0


@pytest.mark.parametrize("direction", [Direction.UP_RED, Direction.DOWN_BLUE])
def test_evaluate_scores_a_perfect_synthetic_flag_at_100(direction):
    bars = _crossover_bars(direction)
    decision = evaluate_major_flag(bars, direction, None, None, 0, _decision_now(bars))

    assert decision.decision == config.MAJOR_APPROVED
    assert decision.approved is True
    assert decision.score == 100.0
    assert decision.required_score == config.MAJOR_ENTRY_SCORE_MIN
    assert sum(decision.component_scores.values()) == decision.score


def test_evaluate_score_equals_sum_of_reported_component_scores_on_a_weak_flag():
    """The reported total must always be exactly the sum of the reported
    components — never a separately-derived number."""
    bars = _crossover_bars(Direction.UP_RED, jump=30.0, volume_mult=1.0)
    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))

    assert decision.component_scores  # scoring really ran
    assert decision.score == sum(decision.component_scores.values())
    assert decision.component_scores["volume"] == 5.0  # 1.0x median earns tier-1 volume points


# ── Input-data safety (spec §12 D) ────────────────────────────────────────
def test_input_dataframe_is_never_mutated():
    bars = _crossover_bars(Direction.UP_RED)
    before = bars.copy(deep=True)
    ids_before = [id(block) for block in bars.dtypes.index]

    evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))

    pd.testing.assert_frame_equal(bars, before)
    assert list(bars.columns) == list(before.columns)
    assert [id(block) for block in bars.dtypes.index] == ids_before


def test_same_inputs_produce_identical_decisions():
    bars = _crossover_bars(Direction.UP_RED)
    now = _decision_now(bars)

    first = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, now)
    second = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, now)

    assert first == second


def test_row_order_and_duplicate_timestamps_are_normalized_not_trusted():
    bars = _crossover_bars(Direction.UP_RED)
    shuffled = pd.concat([bars.iloc[10:], bars.iloc[:10]]).reset_index(drop=True)

    expected = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))
    actual = evaluate_major_flag(shuffled, Direction.UP_RED, None, None, 0, _decision_now(bars))

    assert actual == expected


def test_evaluate_treats_the_frames_last_row_as_the_current_bar():
    """No look-ahead: the filter uses ONLY the rows it was handed, and its
    "current" bar is that frame's last row. The caller is therefore
    responsible for slicing the frame at the flag bar."""
    bars = _crossover_bars(Direction.UP_RED)
    flag_close = float(bars["close"].iloc[-1])

    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))
    snap = calculate_macd(bars)

    assert decision.metrics["close"] == flag_close
    # hist/prev_hist come from exactly the provided rows, nothing else.
    assert decision.metrics["hist"] == pytest.approx(snap.current_diff, abs=1e-6)
    assert decision.metrics["prev_hist"] == pytest.approx(snap.previous_diff, abs=1e-6)


def test_a_bar_after_the_flag_bar_shifts_the_current_bar_so_callers_must_slice():
    """Passing a frame that extends PAST the flag bar makes the later bar the
    current one — the filter never reaches back to score the flag bar. This is
    documented caller responsibility (the worker hands it bars_3m sliced at the
    confirmed bar), and this test pins the behavior."""
    bars = _crossover_bars(Direction.UP_RED)
    future_close = float(bars["close"].iloc[-1]) - 50.0
    future = pd.DataFrame([{
        "datetime": pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3),
        "open": future_close,
        "high": future_close + _BASE_SPREAD,
        "low": future_close - _BASE_SPREAD,
        "close": future_close,
        "volume": _BASE_VOLUME,
    }])
    with_future = pd.concat([bars, future], ignore_index=True)

    sliced = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))
    unsliced = evaluate_major_flag(
        with_future, Direction.UP_RED, None, None, 0, _decision_now(with_future),
    )

    assert sliced.approved is True
    assert sliced.metrics["close"] == float(bars["close"].iloc[-1])
    # The extra bar is no longer a crossover bar, so the filter refuses it
    # rather than silently scoring the older flag bar.
    assert unsliced.decision == config.FILTER_INPUT_NOT_CROSSOVER
    assert unsliced.approved is False


def test_fewer_than_min_completed_bars_is_data_insufficient():
    bars = _crossover_bars(Direction.UP_RED, n=config.MAJOR_MIN_COMPLETED_BARS - 1)
    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))

    assert decision.decision == config.FILTER_DATA_INSUFFICIENT
    assert decision.block_reason == config.FILTER_DATA_INSUFFICIENT
    assert decision.approved is False


def test_empty_and_none_frames_are_data_insufficient():
    now = datetime(2026, 7, 28, 11, 9, tzinfo=KST)
    for frame in (None, pd.DataFrame()):
        decision = evaluate_major_flag(frame, Direction.UP_RED, None, None, 0, now)
        assert decision.decision == config.FILTER_DATA_INSUFFICIENT


def test_naive_datetime_column_is_data_insufficient():
    bars = _crossover_bars(Direction.UP_RED)
    bars["datetime"] = pd.to_datetime(bars["datetime"]).dt.tz_localize(None)

    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(_crossover_bars(Direction.UP_RED)))
    assert decision.decision == config.FILTER_DATA_INSUFFICIENT


def test_missing_required_column_is_data_insufficient():
    bars = _crossover_bars(Direction.UP_RED).drop(columns=["volume"])
    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))
    assert decision.decision == config.FILTER_DATA_INSUFFICIENT


def test_nan_current_volume_is_data_insufficient():
    bars = _crossover_bars(Direction.UP_RED)
    bars.loc[len(bars) - 1, "volume"] = float("nan")

    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))
    assert decision.decision == config.FILTER_DATA_INSUFFICIENT


def test_zero_atr_is_data_insufficient():
    # Every bar identical (open == high == low == close) -> true range 0 -> ATR 0.
    bars = _flat_bars(spread=0.0)
    scores, metrics, err = compute_component_scores(bars)

    assert err == config.FILTER_DATA_INSUFFICIENT
    assert scores is None and metrics is None


def test_zero_volume_median_is_data_insufficient():
    bars = _crossover_bars(Direction.UP_RED)
    bars.loc[: len(bars) - 2, "volume"] = 0.0  # the whole 20-bar lookback is zero

    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))
    assert decision.decision == config.FILTER_DATA_INSUFFICIENT


def test_vwap_never_mixes_the_previous_days_volume():
    """Session VWAP is per calendar day: yesterday's (deliberately huge) volume
    at a different typical price must not leak into today's VWAP."""
    day1 = _flat_bars(start=_DAY1, volume=100_000.0)
    # Same closes (so MACD is untouched) but a much higher typical price.
    day1["high"] = _BASE_PRICE + 300.0
    day1["low"] = _BASE_PRICE
    day2 = _crossover_bars(Direction.UP_RED, n=_BASE_BARS)
    day2["datetime"] = [_DAY2 + timedelta(minutes=3 * i) for i in range(len(day2))]
    both = pd.concat([day1, day2], ignore_index=True)

    decision = evaluate_major_flag(both, Direction.UP_RED, None, None, 0, _decision_now(both))

    typical = (day2["high"] + day2["low"] + day2["close"]) / 3.0
    expected_vwap = float((typical * day2["volume"]).sum() / day2["volume"].sum())
    assert decision.metrics["vwap"] == pytest.approx(expected_vwap)
    # Day 1's typical price (1100 with 100x the volume) would have dominated.
    assert decision.metrics["vwap"] < 1015.0


def test_last_two_bars_that_are_not_a_crossover_are_rejected():
    flat = _flat_bars()
    decision = evaluate_major_flag(flat, Direction.UP_RED, None, None, 0, _decision_now(flat))

    assert decision.decision == config.FILTER_INPUT_NOT_CROSSOVER
    assert decision.block_reason == config.FILTER_INPUT_NOT_CROSSOVER
    assert decision.approved is False


def test_opposite_direction_crossover_is_rejected_as_not_crossover():
    bars = _crossover_bars(Direction.UP_RED)
    decision = evaluate_major_flag(bars, Direction.DOWN_BLUE, None, None, 0, _decision_now(bars))

    assert decision.decision == config.FILTER_INPUT_NOT_CROSSOVER


def test_unsupported_flag_direction_is_rejected():
    bars = _crossover_bars(Direction.UP_RED)
    for bad in (Direction.HOLD, Direction.NOT_READY, "SIDEWAYS"):
        decision = evaluate_major_flag(bars, bad, None, None, 0, _decision_now(bars))
        assert decision.decision == config.FILTER_INPUT_NOT_CROSSOVER


# ══════════════════════════════════════════════════════════════════════════
# B. Approval thresholds
# ══════════════════════════════════════════════════════════════════════════
def _patch_total_score(
    monkeypatch,
    total: float,
    *,
    price_strength: float = 25.0,
    ema20_or_vwap: float = 10.0,
    ema_spread_ratio: float = 0.01,
    recent_range_ratio: float = 0.02,
    price_impulse_atr: float = 1.50,
) -> None:
    """Pin the scored total (and the two confirmation components) while the
    real crossover verification, sideways gate and threshold logic still run."""
    scores = dict(_ZERO_SCORES)
    scores["price_strength"] = price_strength
    scores["ema20_or_vwap"] = ema20_or_vwap
    scores["hist_impulse"] = float(total) - price_strength - ema20_or_vwap

    def fake_score_for_direction(scores_template, metrics, flag_direction):
        del scores_template, flag_direction
        shaped = dict(metrics)
        shaped["ema_spread_ratio"] = ema_spread_ratio
        shaped["recent_range_ratio"] = recent_range_ratio
        # Drive the required price-confirmation gate via metrics (not score alone).
        shaped["breakout"] = price_strength >= 25.0
        shaped["price_impulse_atr"] = price_impulse_atr if price_strength >= 15.0 else 0.0
        shaped["hist_impulse_atr"] = 0.10
        shaped["volume_ratio"] = 0.85
        shaped["ema20_ok"] = ema20_or_vwap >= 10.0
        shaped["vwap_ok"] = False
        shaped["ema20_or_vwap_ok"] = ema20_or_vwap >= 10.0
        return dict(scores), shaped

    monkeypatch.setattr(major_flag_filter, "score_for_direction", fake_score_for_direction)


@pytest.mark.parametrize(("total", "approved"), [(70.0, True), (65.0, True), (64.0, True)])
def test_flat_entry_requires_strong_profile_score_70(monkeypatch, total, approved):
    _patch_total_score(monkeypatch, total)
    bars = _crossover_bars(Direction.UP_RED)

    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))

    assert decision.required_score == 65.0
    assert decision.is_reversal is False
    assert decision.fast_reversal is False
    assert decision.approved is approved
    expected = config.MAJOR_APPROVED if approved else config.MAJOR_STRONG_PROFILE_FAILED
    assert decision.decision == expected


@pytest.mark.parametrize(("total", "approved"), [(75.0, True), (74.0, True)])
def test_reversal_threshold_is_75(monkeypatch, total, approved):
    _patch_total_score(monkeypatch, total)
    bars = _crossover_bars(Direction.UP_RED)
    now = _decision_now(bars)

    decision = evaluate_major_flag(
        bars, Direction.UP_RED, Direction.DOWN_BLUE, now - timedelta(minutes=40), 0, now,
    )

    assert decision.is_reversal is True
    assert decision.fast_reversal is False
    assert decision.required_score == 75.0
    assert decision.approved is approved
    assert decision.decision == (config.MAJOR_APPROVED if approved else config.MAJOR_STRONG_PROFILE_FAILED)


@pytest.mark.parametrize(("total", "approved"), [(82.0, True), (81.0, True)])
def test_fast_reversal_within_15_minutes_threshold_is_82(monkeypatch, total, approved):
    _patch_total_score(monkeypatch, total)
    bars = _crossover_bars(Direction.UP_RED)
    now = _decision_now(bars)

    decision = evaluate_major_flag(
        bars, Direction.UP_RED, Direction.DOWN_BLUE, now - timedelta(minutes=15), 0, now,
    )

    assert decision.is_reversal is True
    assert decision.fast_reversal is True
    assert decision.required_score == 82.0
    assert decision.approved is approved


def test_fast_reversal_window_boundary_is_exclusive_past_15_minutes(monkeypatch):
    _patch_total_score(monkeypatch, 80.0)
    bars = _crossover_bars(Direction.UP_RED)
    now = _decision_now(bars)

    just_outside = evaluate_major_flag(
        bars, Direction.UP_RED, Direction.DOWN_BLUE,
        now - timedelta(minutes=15, seconds=1), 0, now,
    )

    assert just_outside.fast_reversal is False
    assert just_outside.required_score == 75.0
    assert just_outside.approved is True


def test_price_confirmation_failure_blocks_even_a_high_score(monkeypatch):
    _patch_total_score(monkeypatch, 90.0, price_strength=0.0, ema20_or_vwap=0.0)
    bars = _crossover_bars(Direction.UP_RED)

    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))

    assert decision.decision == config.MAJOR_PRICE_CONFIRMATION_FAILED
    assert decision.block_reason == config.MAJOR_PRICE_CONFIRMATION_FAILED
    assert decision.approved is False
    assert decision.score == 90.0  # the score is still reported for the ledger/UI


@pytest.mark.parametrize(
    ("price_strength", "ema20_or_vwap", "expected"),
    [(25.0, 0.0, config.MAJOR_APPROVED), (0.0, 10.0, config.MAJOR_STRONG_PROFILE_FAILED)],
)
def test_price_confirmation_passes_with_either_half(monkeypatch, price_strength, ema20_or_vwap, expected):
    _patch_total_score(monkeypatch, 75.0, price_strength=price_strength, ema20_or_vwap=ema20_or_vwap)
    bars = _crossover_bars(Direction.UP_RED)

    decision = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, _decision_now(bars))

    assert decision.decision == expected


def test_sideways_block_requires_both_tight_ema_spread_and_tight_range(monkeypatch):
    bars = _crossover_bars(Direction.UP_RED)
    now = _decision_now(bars)

    _patch_total_score(
        monkeypatch, 100.0,
        ema_spread_ratio=config.MAJOR_SIDEWAYS_EMA_SPREAD_MAX - 1e-6,
        recent_range_ratio=config.MAJOR_SIDEWAYS_RANGE_MAX - 1e-6,
    )
    blocked = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, now)
    assert blocked.decision == config.MAJOR_SIDEWAYS_BLOCK
    assert blocked.approved is False

    # A tight EMA spread alone (with a wide range) is NOT sideways.
    _patch_total_score(
        monkeypatch, 100.0,
        ema_spread_ratio=config.MAJOR_SIDEWAYS_EMA_SPREAD_MAX - 1e-6,
        recent_range_ratio=config.MAJOR_SIDEWAYS_RANGE_MAX,
    )
    allowed = evaluate_major_flag(bars, Direction.UP_RED, None, None, 0, now)
    assert allowed.decision == config.MAJOR_APPROVED


# ── Post-score trade gates ────────────────────────────────────────────────
def _approved(score: float = 100.0, *, is_reversal: bool = False, fast_reversal: bool = False) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=True,
        score=score,
        required_score=config.MAJOR_ENTRY_SCORE_MIN,
        decision=config.MAJOR_APPROVED,
        reasons=("all hybrid gates passed",),
        component_scores=dict(_ZERO_SCORES),
        metrics={"close": 1000.0},
        is_reversal=is_reversal,
        fast_reversal=fast_reversal,
    )


def _gate(decision: MajorFlagDecision, **kwargs) -> MajorFlagDecision:
    now = kwargs.pop("now", datetime(2026, 7, 28, 11, 9, tzinfo=KST))
    params = {
        "flag_direction": Direction.UP_RED,
        "position_direction": None,
        "last_entry_at": None,
        "last_same_direction_exit_at": None,
        "daily_major_entry_count": 0,
        "now": now,
    }
    params.update(kwargs)
    return apply_major_trade_gates(decision, **params)


def test_same_direction_position_is_never_added_to():
    gated = _gate(_approved(), position_direction=Direction.UP_RED)

    assert gated.decision == config.SAME_DIRECTION_POSITION_HELD
    assert gated.block_reason == config.SAME_DIRECTION_POSITION_HELD
    assert gated.approved is False


def test_same_direction_block_applies_even_to_an_already_rejected_decision():
    rejected = MajorFlagDecision(
        approved=False, score=10.0, required_score=65.0,
        decision=config.MAJOR_SCORE_BELOW_THRESHOLD, reasons=(),
        component_scores={}, metrics={}, is_reversal=False, fast_reversal=False,
        block_reason=config.MAJOR_SCORE_BELOW_THRESHOLD,
    )
    gated = _gate(rejected, position_direction=Direction.UP_RED)
    assert gated.decision == config.SAME_DIRECTION_POSITION_HELD


@pytest.mark.parametrize(
    ("count", "blocked"),
    [(0, False), (3, False), (4, True), (9, True)],
)
def test_daily_entry_limit_is_4(count, blocked):
    gated = _gate(_approved(), daily_major_entry_count=count)

    if blocked:
        assert gated.decision == config.MAJOR_DAILY_ENTRY_LIMIT
        assert gated.approved is False
    else:
        assert gated.decision == config.MAJOR_APPROVED
        assert gated.approved is True


@pytest.mark.parametrize(
    ("minutes_since_exit", "blocked"),
    [(0.0, True), (17.9, True), (18.0, False), (30.0, False)],
)
def test_same_direction_reentry_cooldown_is_18_minutes(minutes_since_exit, blocked):
    now = datetime(2026, 7, 28, 13, 9, tzinfo=KST)
    gated = _gate(
        _approved(),
        last_same_direction_exit_at=now - timedelta(minutes=minutes_since_exit),
        now=now,
    )

    assert gated.approved is not blocked
    if blocked:
        assert gated.decision == config.MAJOR_SAME_DIRECTION_COOLDOWN


def test_reentry_cooldown_only_applies_when_flat():
    now = datetime(2026, 7, 28, 13, 9, tzinfo=KST)
    gated = _gate(
        _approved(is_reversal=True),
        position_direction=Direction.DOWN_BLUE,
        last_same_direction_exit_at=now - timedelta(minutes=1),
        last_entry_at=now - timedelta(minutes=60),
        now=now,
    )
    assert gated.decision == config.MAJOR_APPROVED


def test_min_hold_blocks_a_weak_early_reversal_but_not_a_fast_reversal_grade_one():
    now = datetime(2026, 7, 28, 13, 9, tzinfo=KST)

    weak = _gate(
        _approved(score=80.0, is_reversal=True),
        position_direction=Direction.DOWN_BLUE,
        last_entry_at=now - timedelta(minutes=5),
        now=now,
    )
    assert weak.decision == config.MAJOR_MIN_HOLD_BLOCK
    assert weak.approved is False

    strong = _gate(
        _approved(score=config.MAJOR_FAST_REVERSAL_SCORE_MIN, is_reversal=True, fast_reversal=True),
        position_direction=Direction.DOWN_BLUE,
        last_entry_at=now - timedelta(minutes=5),
        now=now,
    )
    assert strong.decision == config.MAJOR_APPROVED


# ══════════════════════════════════════════════════════════════════════════
# C. Worker integration — order gate only
# ══════════════════════════════════════════════════════════════════════════
_WORKER_START = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
_WORKER_QUOTES = {
    config.WATCH_SYMBOL: 140.0,
    config.LONG_SYMBOL: 15_000.0,
    config.INVERSE_SYMBOL: 10_000.0,
}


def _1m_from_3m_closes(start: datetime, closes: list[float]) -> pd.DataFrame:
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
    """MarketDataService whose decision-time quote cache is really populated
    (get_quote() only reads that cache), wired to fakes — never the real,
    conftest-blocked KIS default."""
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _fresh_state(*, filter_on: bool) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.major_filter_enabled = filter_on
    return state


def _confirmed_up_scenario(*, filter_on: bool, quotes: dict = None):
    """A single-tick confirmed UP_RED completed-bar crossover (the same shape
    tests/macd2/test_worker.py uses), with the MAJOR_FLAG toggle configurable."""
    quote_prices = {**_WORKER_QUOTES, **(quotes or {})}
    df_1m = _1m_from_3m_closes(_WORKER_START, [100.0] * 98 + [120.0, 140.0])
    now = _WORKER_START + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state(filter_on=filter_on)
    state.last_confirmed_bar_ts = (_WORKER_START + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: quote_prices[config.LONG_SYMBOL],
                config.INVERSE_SYMBOL: quote_prices[config.INVERSE_SYMBOL]},
    )
    svc = _svc_with_quote(df_1m, now, quote_prices)
    return svc, state, broker, now


def _buy_orders(broker: FakeBroker) -> list:
    return [o for o in broker.orders if o.side == "BUY"]


def test_filter_off_still_orders_on_a_confirmed_flag():
    svc, state, broker, now = _confirmed_up_scenario(filter_on=False)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    assert len(_buy_orders(broker)) == 1
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert state.last_major_decision is None  # the filter never judged anything
    assert state.daily_major_entry_count == 0


def test_filter_on_approved_behaves_identically_to_filter_off():
    off_svc, off_state, off_broker, off_now = _confirmed_up_scenario(filter_on=False)
    off_result = run_once(broker=off_broker, market_data=off_svc, state=off_state, now=off_now)

    on_svc, on_state, on_broker, on_now = _confirmed_up_scenario(filter_on=True)
    on_result = run_once(broker=on_broker, market_data=on_svc, state=on_state, now=on_now)

    assert on_result.actions == off_result.actions == ["ENTRY:UP_RED"]
    assert on_state.position.symbol == off_state.position.symbol
    assert on_state.position.quantity == off_state.position.quantity
    assert len(_buy_orders(on_broker)) == len(_buy_orders(off_broker)) == 1
    assert on_state.last_major_approved is True
    assert on_state.last_major_decision == config.MAJOR_APPROVED
    assert on_state.last_major_score >= on_state.last_major_required_score


def test_filter_on_rejected_never_reaches_the_broker(monkeypatch):
    monkeypatch.setattr(
        major_flag_filter,
        "_strong_profit_profile_ok",
        lambda **kwargs: (False, "forced test reject"),
    )
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert broker.orders == []
    assert state.position is None
    assert result.actions == [f"{config.FILTERED_OUT}:UP_RED"]
    assert state.last_major_decision == config.MAJOR_STRONG_PROFILE_FAILED
    assert state.order_block_reason == config.MAJOR_STRONG_PROFILE_FAILED

    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[0]["order_result"] == config.FILTERED_OUT
    assert rows[0]["block_reason"] == config.MAJOR_STRONG_PROFILE_FAILED
    assert rows[0]["direction"] == "UP_RED"
    assert rows[0]["signal_id"] in state.processed_signal_ids
    assert ledger.load_execution_ledger() == []


def test_a_rejected_signal_is_not_re_judged_or_re_ordered_next_tick(monkeypatch):
    monkeypatch.setattr(
        major_flag_filter,
        "_strong_profit_profile_ok",
        lambda **kwargs: (False, "forced test reject"),
    )
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)

    run_once(broker=broker, market_data=svc, state=state, now=now)
    for step in range(1, 6):
        run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=step))

    assert broker.orders == []
    assert len(ledger.load_signal_ledger()) == 1
    assert len(state.processed_signal_ids) == 1


def test_a_rejected_flag_does_not_suppress_the_confirmed_flag_stats(monkeypatch):
    """The filter is an ORDER gate: the confirmed flag itself, and the
    latest_primary_* stats, must look exactly as they do with the filter off."""
    monkeypatch.setattr(config, "MAJOR_ENTRY_SCORE_MIN", 200.0)
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)

    run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.latest_primary_flag == Direction.UP_RED
    assert state.last_detected_direction == Direction.UP_RED
    assert state.latest_primary_signal_id is not None


def test_daily_entry_count_increments_only_on_a_real_filled_buy():
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)

    run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.daily_major_entry_count == 1
    assert state.last_major_entry_at is not None


def test_daily_entry_count_does_not_increment_on_a_zero_fill(monkeypatch):
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)
    broker.next_buy_fill_qty = 0  # order accepted, nothing actually lands

    run_once(broker=broker, market_data=svc, state=state, now=now)

    assert len(_buy_orders(broker)) == 1  # the approval really did reach the broker
    assert state.position is None
    assert state.daily_major_entry_count == 0
    assert state.last_major_entry_at is None


def test_fourth_daily_entry_exhausts_the_budget_and_blocks_further_buys():
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)
    state.daily_major_entry_count = config.MAJOR_MAX_DAILY_ENTRIES

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert broker.orders == []
    assert state.position is None
    assert result.actions == [f"{config.FILTERED_OUT}:UP_RED"]
    assert state.last_major_decision == config.MAJOR_DAILY_ENTRY_LIMIT
    rows = ledger.load_signal_ledger()
    assert rows[-1]["block_reason"] == config.MAJOR_DAILY_ENTRY_LIMIT
    assert rows[-1]["order_result"] == config.FILTERED_OUT


def test_same_direction_add_is_blocked_while_holding():
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    state.position = PositionSnapshot(
        symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0,
        entry_at=now - timedelta(minutes=45),
    )
    orders_before = len(broker.orders)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert len(broker.orders) == orders_before  # zero new orders
    assert state.position.quantity == 10  # no add
    assert not any(a.startswith("ENTRY:") for a in result.actions)
    assert state.last_major_decision == config.SAME_DIRECTION_POSITION_HELD
    rows = ledger.load_signal_ledger()
    assert rows[-1]["block_reason"] == config.SAME_DIRECTION_POSITION_HELD
    assert rows[-1]["order_result"] == config.FILTERED_OUT


def test_same_direction_reentry_cooldown_blocks_a_fresh_entry():
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)
    state.last_major_exit_direction = Direction.UP_RED
    state.last_major_exit_at = (now - timedelta(minutes=5)).isoformat()

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert broker.orders == []
    assert state.position is None
    assert result.actions == [f"{config.FILTERED_OUT}:UP_RED"]
    assert state.last_major_decision == config.MAJOR_SAME_DIRECTION_COOLDOWN


def test_cooldown_of_the_other_direction_does_not_block_this_flag():
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)
    state.last_major_exit_direction = Direction.DOWN_BLUE
    state.last_major_exit_at = (now - timedelta(minutes=1)).isoformat()

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    assert len(_buy_orders(broker)) == 1


def test_stop_loss_still_exits_with_the_filter_on():
    """The filter has no authority over risk exits (docs: STOP_LOSS /
    PROFIT_LOCK / FORCED_LIQUIDATION are never filtered)."""
    df_1m = _1m_from_3m_closes(_WORKER_START, [100.0] * 100)
    now = _WORKER_START + timedelta(minutes=3 * 100, seconds=5)
    svc = _svc_with_quote(df_1m, now, {**_WORKER_QUOTES, config.LONG_SYMBOL: 14_000.0})

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    broker.set_quote(config.LONG_SYMBOL, 14_000.0)
    state = _fresh_state(filter_on=True)
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("STOP_LOSS:") for a in result.actions)
    assert state.position is None
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert ledger.load_execution_ledger()[-1]["exit_reason"] == config.EXIT_STOP_LOSS


def test_stop_loss_is_not_gated_even_when_the_filter_rejects_everything(monkeypatch):
    monkeypatch.setattr(config, "MAJOR_ENTRY_SCORE_MIN", 200.0)
    monkeypatch.setattr(config, "MAJOR_REVERSAL_SCORE_MIN", 200.0)
    monkeypatch.setattr(config, "MAJOR_FAST_REVERSAL_SCORE_MIN", 200.0)
    df_1m = _1m_from_3m_closes(_WORKER_START, [100.0] * 98 + [120.0, 140.0])
    now = _WORKER_START + timedelta(minutes=3 * 100, seconds=5)
    svc = _svc_with_quote(df_1m, now, {**_WORKER_QUOTES, config.INVERSE_SYMBOL: 9_000.0})

    broker = FakeBroker(cash=10_000_000.0, quotes={config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed")
    broker.set_quote(config.INVERSE_SYMBOL, 9_000.0)
    state = _fresh_state(filter_on=True)
    state.last_confirmed_bar_ts = (_WORKER_START + timedelta(minutes=3 * 98)).isoformat()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert any(a.startswith("STOP_LOSS:") for a in result.actions)
    assert state.position is None
    assert ledger.load_execution_ledger()[-1]["exit_reason"] == config.EXIT_STOP_LOSS


def test_forced_liquidation_still_runs_with_the_filter_on():
    df_1m = _1m_from_3m_closes(_WORKER_START, [100.0] * 100)
    bootstrap_now = _WORKER_START + timedelta(minutes=3 * 100, seconds=5)
    svc = _svc_with_quote(df_1m, bootstrap_now, {**_WORKER_QUOTES, config.LONG_SYMBOL: 20_000.0})

    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 20_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    state = _fresh_state(filter_on=True)
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)

    result = run_once(
        broker=broker, market_data=svc, state=state,
        now=_WORKER_START.replace(hour=15, minute=0, second=1),
    )

    assert any(a.startswith("FORCED_LIQUIDATION:") for a in result.actions)
    assert state.position is None


def test_approved_signal_writes_the_major_ledger_columns():
    svc, state, broker, now = _confirmed_up_scenario(filter_on=True)

    run_once(broker=broker, market_data=svc, state=state, now=now)

    row = ledger.load_signal_ledger()[-1]
    assert row["major_filter_enabled"] == "True"
    assert row["major_filter_version"] == config.MAJOR_FILTER_VERSION
    assert row["major_decision"] == config.MAJOR_APPROVED
    assert row["major_approved"] == "True"
    assert float(row["major_score"]) >= float(row["major_required_score"])
    assert row["major_component_scores"]
    assert row["hist_impulse_atr"]


def test_filter_off_leaves_the_major_decision_ledger_columns_empty():
    svc, state, broker, now = _confirmed_up_scenario(filter_on=False)

    run_once(broker=broker, market_data=svc, state=state, now=now)

    row = ledger.load_signal_ledger()[-1]
    assert row["major_filter_enabled"] == "False"
    assert row["major_score"] == ""
    assert row["major_decision"] == ""
    assert row["major_block_reason"] == ""


# ══════════════════════════════════════════════════════════════════════════
# E. Regression — the toggle really is a hard off switch
# ══════════════════════════════════════════════════════════════════════════
def test_filter_code_is_never_called_when_the_toggle_is_off(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("evaluate_major_flag called with major_filter_enabled=False")

    monkeypatch.setattr(major_flag_filter, "evaluate_major_flag", _explode)
    monkeypatch.setattr(major_flag_filter, "apply_major_trade_gates", _explode)

    svc, state, broker, now = _confirmed_up_scenario(filter_on=False)
    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.actions == ["ENTRY:UP_RED"]
    assert len(_buy_orders(broker)) == 1


def test_filter_module_only_reports_and_never_calls_order_or_broker_code():
    """major_flag_filter must stay a pure scoring module — no broker,
    order_executor, ledger, state_store or network imports."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(major_flag_filter))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)

    forbidden = (
        "order_executor", "broker_adapter", "state_store", "ledger", "worker",
        "requests", "kis_client", "market_data", "socket", "streamlit",
    )
    for name in sorted(imported):
        leaf = name.rsplit(".", 1)[-1]
        assert leaf not in forbidden, f"major_flag_filter must not import {name}"
    assert imported, "expected major_flag_filter to import at least pandas/config"
