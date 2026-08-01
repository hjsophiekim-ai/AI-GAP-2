"""
test_hynix_primary_trend.py — PRIMARY_TREND(UP/DOWN/RANGE) 분류와 단기 눌림/실제
추세전환 분리, 인버스/하이닉스 신규진입 차단, 2회 연속 확인 반전 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.hynix_primary_trend import (
    PRIMARY_TREND_UP, PRIMARY_TREND_DOWN, PRIMARY_TREND_RANGE,
    MOVE_PULLBACK, MOVE_RANGE_SIGNAL,
    classify_short_term_move, compute_primary_trend, default_reversal_confirmation_state,
    new_hynix_entry_blocked, new_inverse_entry_blocked, update_reversal_confirmation,
)


def _make_uptrend_bars(n: int = 40, start_price: float = 100_000.0, start: datetime | None = None) -> pd.DataFrame:
    """Steadily rising 1-minute bars with a brief 5-minute dip at the very end
    (a pullback within an uptrend, not a reversal)."""
    start = start or datetime(2026, 7, 9, 9, 5)
    rows = []
    price = start_price
    for i in range(n):
        ts = start + timedelta(minutes=i)
        if i >= n - 5:
            price *= 0.998  # last 5 bars: a shallow ~1% pullback
        else:
            price *= 1.0025  # steady climb
        rows.append({
            "datetime": ts, "open": price * 0.999, "high": price * 1.001, "low": price * 0.998,
            "close": price, "volume": 1000 + i * 5,
        })
    return pd.DataFrame(rows)


def _make_downtrend_bars(n: int = 40, start_price: float = 100_000.0, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 7, 9, 9, 5)
    rows = []
    price = start_price
    for i in range(n):
        ts = start + timedelta(minutes=i)
        price *= 0.9975
        rows.append({
            "datetime": ts, "open": price * 1.001, "high": price * 1.002, "low": price * 0.999,
            "close": price, "volume": 1000 + i * 5,
        })
    return pd.DataFrame(rows)


def _make_range_bars(n: int = 40, start_price: float = 100_000.0, start: datetime | None = None) -> pd.DataFrame:
    """Flat, directionless bars — same close every minute so gap/VWAP/EMA-slope/
    swing-structure all land squarely on "no trend" instead of aliasing into a
    spurious direction through 15-minute resampling."""
    start = start or datetime(2026, 7, 9, 9, 5)
    rows = []
    for i in range(n):
        ts = start + timedelta(minutes=i)
        rows.append({
            "datetime": ts, "open": start_price, "high": start_price * 1.0002, "low": start_price * 0.9998,
            "close": start_price, "volume": 1000,
        })
    return pd.DataFrame(rows)


def test_strong_uptrend_5min_pullback_stays_hold_hynix_inverse_blocked():
    """강한 당일 상승 중 5분 -1% 조정은 PULLBACK으로 분류되고, PRIMARY_TREND=UP +
    가격이 VWAP/EMA20 위이면 INVERSE 신규진입이 금지되어야 한다."""
    df = _make_uptrend_bars()
    prev_close = float(df["close"].iloc[0]) / 1.03  # 3% 갭상승 가정

    result = compute_primary_trend(df, prev_close=prev_close, now=df["datetime"].iloc[-1])

    assert result["primary_trend"] == PRIMARY_TREND_UP
    assert result["above_vwap"] is True

    move_kind = classify_short_term_move(result["primary_trend"], "DOWN")
    assert move_kind == MOVE_PULLBACK

    assert new_inverse_entry_blocked(result["primary_trend"], result["above_vwap"], result["above_ema20"]) is True


def test_inverse_switch_only_after_vwap_and_15m_trend_and_swing_low_break():
    """VWAP 및 15분 추세 붕괴 + 주요 저점 붕괴가 2회 연속 확인된 뒤에만 INVERSE로
    전환이 허용되어야 한다(1회만으로는 전환하지 않음)."""
    now = datetime(2026, 7, 9, 10, 0)
    tracker = default_reversal_confirmation_state()

    # 1차 확인 — 아직 전환 확정 아님
    tracker = update_reversal_confirmation(
        tracker, vwap_broken=True, trend_15m_against=True, swing_broken=True,
        target_direction=PRIMARY_TREND_DOWN, now=now,
    )
    assert tracker["should_switch"] is False
    assert tracker["consecutive_count"] == 1

    # 2차 연속 확인 — 이제 전환 확정
    tracker = update_reversal_confirmation(
        tracker, vwap_broken=True, trend_15m_against=True, swing_broken=True,
        target_direction=PRIMARY_TREND_DOWN, now=now + timedelta(minutes=1),
    )
    assert tracker["should_switch"] is True
    assert tracker["consecutive_count"] == 2


def test_reversal_confirmation_resets_when_a_condition_fails_mid_streak():
    now = datetime(2026, 7, 9, 10, 0)
    tracker = default_reversal_confirmation_state()
    tracker = update_reversal_confirmation(
        tracker, vwap_broken=True, trend_15m_against=True, swing_broken=True,
        target_direction=PRIMARY_TREND_DOWN, now=now,
    )
    assert tracker["consecutive_count"] == 1

    # 다음 확인에서 저점 붕괴 조건이 빠지면 스트릭이 리셋된다(단일 사이클로는 전환 불가).
    tracker = update_reversal_confirmation(
        tracker, vwap_broken=True, trend_15m_against=True, swing_broken=False,
        target_direction=PRIMARY_TREND_DOWN, now=now + timedelta(minutes=1),
    )
    assert tracker["consecutive_count"] == 0
    assert tracker["should_switch"] is False


def test_range_allows_fast_watcher_rapid_switching():
    """PRIMARY_TREND가 RANGE일 때만 Fast Watcher의 빠른 스위칭이 허용된다."""
    df = _make_range_bars()
    result = compute_primary_trend(df, prev_close=float(df["close"].iloc[0]), now=df["datetime"].iloc[-1])

    assert result["primary_trend"] == PRIMARY_TREND_RANGE
    assert classify_short_term_move(result["primary_trend"], "UP") == MOVE_RANGE_SIGNAL
    assert classify_short_term_move(result["primary_trend"], "DOWN") == MOVE_RANGE_SIGNAL
    # RANGE에서는 방향 전환에 PRIMARY_TREND 자체가 진입을 막지 않는다.
    assert new_inverse_entry_blocked(result["primary_trend"], result["above_vwap"], result["above_ema20"]) is False
    assert new_hynix_entry_blocked(result["primary_trend"], result["above_vwap"], result["above_ema20"]) is False


def test_downtrend_blocks_new_hynix_entry_mirror_case():
    df = _make_downtrend_bars()
    prev_close = float(df["close"].iloc[0]) * 1.03  # 3% 갭하락 가정
    result = compute_primary_trend(df, prev_close=prev_close, now=df["datetime"].iloc[-1])

    assert result["primary_trend"] == PRIMARY_TREND_DOWN
    assert result["above_vwap"] is False
    assert new_hynix_entry_blocked(result["primary_trend"], result["above_vwap"], result["above_ema20"]) is True
    assert classify_short_term_move(result["primary_trend"], "UP") == MOVE_PULLBACK


def test_insufficient_data_defaults_to_range_not_assumed_trend():
    result = compute_primary_trend(None)
    assert result["primary_trend"] == PRIMARY_TREND_RANGE
    assert "insufficient" in result["reasons"][0]

    short_df = pd.DataFrame({
        "datetime": [datetime(2026, 7, 9, 9, 5)], "open": [100.0], "high": [100.5],
        "low": [99.5], "close": [100.0], "volume": [10],
    })
    result2 = compute_primary_trend(short_df)
    assert result2["primary_trend"] == PRIMARY_TREND_RANGE
