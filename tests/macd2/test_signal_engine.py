"""Unit tests for app.trading.macd2.signal_engine — pure-function only, no fixtures files."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config
from app.trading.macd2.models import Direction, MacdSnapshot
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_macd_crossover,
    evaluate_signed_b,
    is_tradeable_completed_bar,
    make_signal_id,
    resample_completed_3m,
)

KST = config.KST


def _minute_bars(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        dt = start + timedelta(minutes=i)
        rows.append({"datetime": dt, "open": close, "high": close, "low": close, "close": close, "volume": 1})
    return pd.DataFrame(rows)


def test_resample_completed_3m_excludes_incomplete_bar():
    start = datetime(2026, 7, 23, 9, 0, tzinfo=KST)
    # 7 one-minute bars -> bars at 09:00 (complete), 09:03 (complete), 09:06 (1 bar, incomplete)
    bars_1m = _minute_bars(start, [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0])
    now = start + timedelta(minutes=7)  # 09:07 — 09:06 window not yet closed (closes at 09:09)

    out = resample_completed_3m(bars_1m, now=now)

    assert list(out["datetime"]) == [start, start + timedelta(minutes=3)]
    # 09:00 bar: opens 100, closes at 102 (bars at minute 0,1,2)
    first = out.iloc[0]
    assert first["open"] == 100.0
    assert first["close"] == 102.0
    assert first["high"] == 102.0
    assert first["low"] == 100.0


def test_resample_completed_3m_boundary_is_inclusive_at_exact_close():
    start = datetime(2026, 7, 23, 9, 0, tzinfo=KST)
    bars_1m = _minute_bars(start, [100.0, 101.0, 102.0])
    now = start + timedelta(minutes=3)  # exactly the 09:00 bar's close time

    out = resample_completed_3m(bars_1m, now=now)

    assert len(out) == 1
    assert out.iloc[0]["datetime"] == start


def test_resample_completed_3m_empty_input_returns_empty_frame():
    now = datetime(2026, 7, 23, 9, 5, tzinfo=KST)
    out = resample_completed_3m(pd.DataFrame(), now=now)
    assert out.empty


def test_resample_completed_3m_rejects_naive_now():
    bars_1m = _minute_bars(datetime(2026, 7, 23, 9, 0, tzinfo=KST), [100.0, 101.0, 102.0])
    with pytest.raises(ValueError, match="timezone-aware"):
        resample_completed_3m(bars_1m, now=datetime(2026, 7, 23, 9, 5))


def test_resample_completed_3m_rejects_naive_bar_datetime():
    now = datetime(2026, 7, 23, 9, 5, tzinfo=KST)
    naive_bars = pd.DataFrame([
        {"datetime": datetime(2026, 7, 23, 9, 0), "open": 1, "high": 1, "low": 1, "close": 1},
    ])
    with pytest.raises(ValueError, match="timezone-aware"):
        resample_completed_3m(naive_bars, now=now)


def test_resample_completed_3m_dedupes_keeping_latest_value():
    start = datetime(2026, 7, 23, 9, 0, tzinfo=KST)
    rows = [
        {"datetime": start, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1},
        {"datetime": start, "open": 100.0, "high": 100.0, "low": 100.0, "close": 105.0, "volume": 1},  # revised
        {"datetime": start + timedelta(minutes=1), "open": 105.0, "high": 105.0, "low": 105.0, "close": 105.0, "volume": 1},
        {"datetime": start + timedelta(minutes=2), "open": 105.0, "high": 105.0, "low": 105.0, "close": 105.0, "volume": 1},
    ]
    now = start + timedelta(minutes=3)
    out = resample_completed_3m(pd.DataFrame(rows), now=now)
    assert len(out) == 1
    assert out.iloc[0]["close"] == 105.0


def _macd_snapshot(hist_last3: tuple[float, float, float]) -> MacdSnapshot:
    return MacdSnapshot(
        bar_dt=datetime(2026, 7, 23, 10, 27, tzinfo=KST),
        macd=0.0,
        signal=0.0,
        hist=hist_last3[-1],
        hist_last3=hist_last3,
        completed_3m_count=30,
    )


@pytest.mark.parametrize(
    "hist_last3,previous_direction,expected",
    [
        # h2=1, h1=2, h0=3 -> d0=1>0, d1=1>0, all positive -> UP_RED (no prior direction)
        ((1.0, 2.0, 3.0), None, Direction.UP_RED),
        # same pattern but already confirmed UP_RED -> repeat suppressed -> HOLD
        ((1.0, 2.0, 3.0), Direction.UP_RED, Direction.HOLD),
        # mirrored negative pattern -> DOWN_BLUE
        ((-1.0, -2.0, -3.0), None, Direction.DOWN_BLUE),
        ((-1.0, -2.0, -3.0), Direction.DOWN_BLUE, Direction.HOLD),
        # negative but "less negative" (d0>0 while still <0 throughout) — docs §6: not UP_RED
        ((-3.0, -2.0, -1.0), None, Direction.HOLD),
        # positive but "less positive" — docs §6: not DOWN_BLUE
        ((3.0, 2.0, 1.0), None, Direction.HOLD),
        # h2 negative but h0,h1 both positive with both deltas positive -> still UP_RED
        # (docs §6 only constrains h0/h1's sign, not h2's)
        ((-1.0, 1.0, 2.0), None, Direction.UP_RED),
        # opposite prior direction does not suppress a genuinely new signal
        ((1.0, 2.0, 3.0), Direction.DOWN_BLUE, Direction.UP_RED),
    ],
)
def test_evaluate_signed_b(hist_last3, previous_direction, expected):
    snap = _macd_snapshot(hist_last3)
    assert evaluate_signed_b(snap, previous_direction) == expected


@pytest.mark.parametrize(
    "previous_diff,current_diff,previous_direction,expected",
    [
        (-0.1, 0.2, None, Direction.UP_RED),
        (0.0, 0.2, None, Direction.UP_RED),
        (0.1, -0.2, None, Direction.DOWN_BLUE),
        (0.0, -0.2, None, Direction.DOWN_BLUE),
        (0.1, 0.2, None, Direction.HOLD),
        (-0.2, -0.1, None, Direction.HOLD),
        (-0.1, 0.2, Direction.UP_RED, Direction.HOLD),
        (0.1, -0.2, Direction.DOWN_BLUE, Direction.HOLD),
        (-0.1, 0.2, Direction.DOWN_BLUE, Direction.UP_RED),
    ],
)
def test_evaluate_macd_crossover(previous_diff, current_diff, previous_direction, expected):
    snap = MacdSnapshot(
        bar_dt=datetime(2026, 7, 24, 9, 3, tzinfo=KST),
        macd=current_diff,
        signal=0.0,
        hist=current_diff,
        hist_last3=(0.0, previous_diff, current_diff),
        completed_3m_count=30,
        previous_diff=previous_diff,
        current_diff=current_diff,
    )
    assert evaluate_macd_crossover(snap, previous_direction) == expected


def test_calculate_macd_none_when_insufficient_bars():
    start = datetime(2026, 7, 23, 9, 0, tzinfo=KST)
    bars_3m = pd.DataFrame([
        {"datetime": start + timedelta(minutes=3 * i), "close": 100.0 + i} for i in range(10)
    ])
    assert calculate_macd(bars_3m) is None


def test_calculate_macd_matches_pandas_ewm_adjust_false():
    start = datetime(2026, 7, 23, 9, 0, tzinfo=KST)
    closes = [100.0 + i * 0.5 for i in range(40)]
    bars_3m = pd.DataFrame([
        {"datetime": start + timedelta(minutes=3 * i), "close": c} for i, c in enumerate(closes)
    ])

    snap = calculate_macd(bars_3m)

    assert snap is not None
    closes_series = pd.Series(closes)
    ema_fast = closes_series.ewm(span=config.EMA_FAST, adjust=False).mean()
    ema_slow = closes_series.ewm(span=config.EMA_SLOW, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=config.EMA_SIGNAL, adjust=False).mean()
    hist = macd - signal

    assert snap.macd == round(float(macd.iloc[-1]), 6)
    assert snap.signal == round(float(signal.iloc[-1]), 6)
    assert snap.hist == round(float(hist.iloc[-1]), 6)
    assert snap.hist_last3 == (
        round(float(hist.iloc[-3]), 6),
        round(float(hist.iloc[-2]), 6),
        round(float(hist.iloc[-1]), 6),
    )
    assert snap.completed_3m_count == len(bars_3m)


def test_make_signal_id_format():
    bar_dt = datetime(2026, 7, 23, 10, 27, tzinfo=KST)
    assert make_signal_id(bar_dt, Direction.DOWN_BLUE) == "20260723_102700_DOWN_BLUE"


def test_make_signal_id_rejects_naive_bar_time():
    with pytest.raises(ValueError):
        make_signal_id(datetime(2026, 7, 23, 10, 27), Direction.UP_RED)


def test_tradeable_completed_bar_requires_same_day_and_after_open():
    now = datetime(2026, 7, 24, 9, 6, tzinfo=KST)
    assert is_tradeable_completed_bar(datetime(2026, 7, 24, 9, 0, tzinfo=KST), now)
    assert not is_tradeable_completed_bar(datetime(2026, 7, 23, 15, 27, tzinfo=KST), now)
    assert not is_tradeable_completed_bar(datetime(2026, 7, 24, 8, 57, tzinfo=KST), now)
    assert not is_tradeable_completed_bar(datetime(2026, 7, 24, 9, 3, tzinfo=KST), datetime(2026, 7, 24, 9, 5, tzinfo=KST))
