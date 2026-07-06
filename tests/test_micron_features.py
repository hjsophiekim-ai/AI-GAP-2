"""
test_micron_features.py — 마이크론 프리마켓 feature 계산 테스트.
"""

import pandas as pd
import pytest
from datetime import datetime

from app.features.micron_premarket_features import compute_micron_features


def _make_premarket_df(n: int = 60, rising: bool = True) -> pd.DataFrame:
    """프리마켓 n봉 샘플 생성."""
    rows = []
    for i in range(n):
        direction = i if rising else (n - i)
        rows.append({
            "datetime": datetime(2026, 6, 29, 17, i % 60),
            "open":  100.0 + direction * 0.05,
            "high":  100.5 + direction * 0.05,
            "low":   99.8  + direction * 0.05,
            "close": 100.2 + direction * 0.05,
            "volume": 2000 + i * 50,
            "session": "premarket",
        })
    return pd.DataFrame(rows)


class TestComputeMicronFeatures:
    """compute_micron_features 함수 테스트."""

    def test_all_keys_present(self):
        df = _make_premarket_df()
        features = compute_micron_features(df_1min=df)
        expected_keys = {
            "micron_premarket_return",
            "micron_premarket_open_to_now",
            "micron_premarket_high_to_now",
            "micron_premarket_low_to_now",
            "micron_premarket_30m_momentum",
            "micron_premarket_60m_momentum",
            "micron_premarket_vwap",
            "micron_premarket_volume_change",
            "micron_regular_return",
            "micron_aftermarket_return",
            "micron_session_strength_score",
        }
        assert expected_keys == set(features.keys())

    def test_rising_market_positive_return(self):
        df = _make_premarket_df(rising=True)
        features = compute_micron_features(df_1min=df)
        assert features["micron_premarket_return"] is not None
        assert features["micron_premarket_return"] > 0

    def test_falling_market_negative_return(self):
        df = _make_premarket_df(rising=False)
        features = compute_micron_features(df_1min=df)
        assert features["micron_premarket_return"] is not None
        assert features["micron_premarket_return"] < 0

    def test_strength_score_range(self):
        df = _make_premarket_df()
        features = compute_micron_features(df_1min=df)
        score = features["micron_session_strength_score"]
        assert score is not None
        assert 0 <= score <= 100

    def test_none_input(self):
        features = compute_micron_features(df_1min=None)
        # 모든 값이 None이어야 함
        assert all(v is None for v in features.values())

    def test_empty_df(self):
        features = compute_micron_features(df_1min=pd.DataFrame())
        assert all(v is None for v in features.values())

    def test_vwap_between_low_and_high(self):
        df = _make_premarket_df(n=30)
        features = compute_micron_features(df_1min=df)
        vwap = features["micron_premarket_vwap"]
        if vwap is not None:
            low  = df["low"].min()
            high = df["high"].max()
            assert low <= vwap <= high

    def test_with_current_price(self):
        df = _make_premarket_df()
        cp = {"price": 105.0, "open": 100.0, "high": 106.0, "low": 99.0, "volume": 10000}
        features = compute_micron_features(df_1min=df, current_price=cp)
        assert features["micron_premarket_open_to_now"] is not None
        # 현재가 105 > 시가 100 → 양수
        assert features["micron_premarket_open_to_now"] > 0

    def test_regular_and_after_none_when_missing(self):
        df = _make_premarket_df()  # session이 모두 premarket
        features = compute_micron_features(df_1min=df)
        assert features["micron_regular_return"] is None
        assert features["micron_aftermarket_return"] is None
