"""Unit tests for app.trading.tsla_auto.signal_engine."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.tsla_auto import config
from app.trading.tsla_auto.models import Direction
from app.trading.tsla_auto.signal_engine import (
    calculate_macd,
    evaluate_confirmed_macd_flag,
    evaluate_macd_crossover,
    is_tradeable_completed_bar,
    make_signal_id,
    resample_completed_3m,
)

ET = config.ET


def _1m_from_3m_closes(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        bar_start = start + timedelta(minutes=3 * i)
        for j in range(3):
            rows.append({
                "datetime": bar_start + timedelta(minutes=j), "open": close, "high": close,
                "low": close, "close": close, "volume": 10,
            })
    return pd.DataFrame(rows)


def test_resample_anchors_at_0930_et_left_labeled():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    df_1m = _1m_from_3m_closes(start, [100.0] * 10)
    now = start + timedelta(minutes=30)
    bars = resample_completed_3m(df_1m, now=now)
    assert bars["datetime"].iloc[0] == start
    assert bars["datetime"].iloc[1] == start + timedelta(minutes=3)
    # 09:42-09:44 bucket -> bar_start_at = 09:42, never 09:45 (bar_end)
    bar_0942 = start + timedelta(minutes=12)
    assert (bars["datetime"] == bar_0942).any()


def test_3_of_3_1m_bars_required_partial_bin_still_created_by_resample_but_gap_filter_drops_it():
    """resample_completed_3m itself does plain pandas aggregation (matches
    signal_engine.py's job); the "3 of 3 required" gate lives in
    market_data.filter_complete_3m_bars — verified there, not here."""
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    rows = []
    for i in range(9):
        if i == 4:  # drop the middle minute of the 2nd bucket
            continue
        rows.append({"datetime": start + timedelta(minutes=i), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 10})
    df_1m = pd.DataFrame(rows)
    now = start + timedelta(minutes=9)
    bars = resample_completed_3m(df_1m, now=now)
    assert len(bars) == 3  # resample still aggregates the partial bin


def test_make_signal_id_uses_et_bar_start():
    bar_dt = datetime(2026, 7, 30, 10, 42, 0, tzinfo=ET)
    sid = make_signal_id(bar_dt, Direction.UP_RED)
    assert sid == "20260730_104200_UP_RED"


def test_make_signal_id_converts_non_et_input_to_et():
    from zoneinfo import ZoneInfo

    bar_dt_kst = datetime(2026, 7, 30, 23, 42, 0, tzinfo=ZoneInfo("Asia/Seoul"))  # = 10:42 ET (summer)
    sid = make_signal_id(bar_dt_kst, Direction.DOWN_BLUE)
    assert "104200" in sid or "094200" in sid  # DST-dependent, but always ET HHMMSS, never the KST clock time
    assert "234200" not in sid


def test_calculate_macd_up_red_crossover():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    df_1m = _1m_from_3m_closes(start, [100.0] * 30 + [140.0])
    bars_3m = resample_completed_3m(df_1m, now=start + timedelta(minutes=3 * 31))
    snap = calculate_macd(bars_3m)
    assert snap is not None
    direction = evaluate_macd_crossover(snap, None)
    assert direction == Direction.UP_RED


def test_evaluate_macd_crossover_suppresses_repeat_direction():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    df_1m = _1m_from_3m_closes(start, [100.0] * 30 + [140.0])
    bars_3m = resample_completed_3m(df_1m, now=start + timedelta(minutes=3 * 31))
    snap = calculate_macd(bars_3m)
    direction = evaluate_macd_crossover(snap, Direction.UP_RED)
    assert direction == Direction.HOLD


def test_confirmed_flag_separates_raw_color_from_order_flag():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    df_1m = _1m_from_3m_closes(start, [100.0] * 30 + [140.0])
    bars_3m = resample_completed_3m(df_1m, now=start + timedelta(minutes=3 * 31))
    snap = calculate_macd(bars_3m)

    result = evaluate_confirmed_macd_flag(snap)

    assert result.raw_color == Direction.UP_RED
    assert result.confirmed_flag == Direction.UP_RED
    assert result.published_signal_id == make_signal_id(snap.bar_dt, Direction.UP_RED)


def test_same_raw_color_follow_through_has_no_confirmed_reissue():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    df_1m = _1m_from_3m_closes(start, [100.0] * 30 + [140.0] + [150.0])
    bars_3m = resample_completed_3m(df_1m, now=start + timedelta(minutes=3 * 32))
    first = calculate_macd(bars_3m.iloc[:-1])
    second = calculate_macd(bars_3m)

    assert evaluate_confirmed_macd_flag(first).confirmed_flag == Direction.UP_RED
    follow = evaluate_confirmed_macd_flag(second, previous_published_direction=Direction.UP_RED)
    assert follow.raw_color == Direction.UP_RED
    assert follow.confirmed_flag == Direction.HOLD
    assert follow.published_signal_id is None


def test_is_tradeable_completed_bar_requires_same_day_and_completion():
    bar_dt = datetime(2026, 7, 30, 9, 30, tzinfo=ET)
    assert is_tradeable_completed_bar(bar_dt, bar_dt + timedelta(minutes=3)) is True
    assert is_tradeable_completed_bar(bar_dt, bar_dt + timedelta(minutes=2)) is False
    assert is_tradeable_completed_bar(bar_dt, bar_dt + timedelta(days=1, minutes=3)) is False


def test_resample_rejects_naive_datetime():
    with pytest.raises(ValueError):
        resample_completed_3m(pd.DataFrame({"datetime": [datetime(2026, 7, 24, 9, 30)]}), now=datetime.now(ET))
