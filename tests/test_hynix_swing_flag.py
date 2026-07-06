"""
test_hynix_swing_flag.py — 스윙 플래그 모듈 테스트.
"""

from __future__ import annotations

import pytest

from app.models.hynix_swing_flag import (
    STRONG_BUY, BUY, WAIT_BUY, NEUTRAL, TAKE_PROFIT, SELL, STRONG_SELL,
    evaluate_swing_flag,
    compute_hynix_tech_indicators,
    _score_to_flag,
)

import pandas as pd
from datetime import datetime, timedelta


# ── 샘플 데이터 ──────────────────────────────────────────────────────────────

def _base_micron(strength: float = 70.0, pm_ret: float = 2.0) -> dict:
    return {
        "micron_premarket_return":        pm_ret,
        "micron_premarket_open_to_now":   pm_ret * 0.8,
        "micron_premarket_high_to_now":   -0.3,
        "micron_premarket_low_to_now":    pm_ret * 1.1,
        "micron_premarket_30m_momentum":  pm_ret * 0.6,
        "micron_premarket_60m_momentum":  pm_ret * 0.9,
        "micron_premarket_vwap":          102.5,
        "micron_premarket_volume_change": 15.0,
        "micron_regular_return":          None,
        "micron_aftermarket_return":      None,
        "micron_session_strength_score":  strength,
    }


def _base_tech(rsi: float = 50.0, from_high: float = -5.0, bb: float = 50.0) -> dict:
    return {
        "rsi_14":            rsi,
        "macd":              0.5,
        "macd_signal_cross": 0,
        "ma5_position_pct":  1.0,
        "ma20_position_pct": 2.0,
        "ma60_position_pct": 4.0,
        "from_20d_high_pct": from_high,
        "from_20d_low_pct":  8.0,
        "bollinger_pct":     bb,
        "prev_candle_type":  0,
        "return_3d_pct":     -2.0,
        "return_5d_pct":     3.0,
        "return_10d_pct":    5.0,
        "volume_change_pct": 20.0,
    }


def _make_daily_df(n: int = 70, trend: float = 0.003) -> pd.DataFrame:
    """n일 샘플 일봉 데이터 생성."""
    rows = []
    price = 200_000.0
    for i in range(n):
        price = price * (1 + trend + (i % 3 - 1) * 0.001)
        rows.append({
            "datetime": datetime(2026, 1, 1) + timedelta(days=i),
            "open":   price * 0.998,
            "high":   price * 1.008,
            "low":    price * 0.992,
            "close":  price,
            "volume": 5_000_000 + i * 10_000,
        })
    return pd.DataFrame(rows)


class TestScoreToFlag:
    """_score_to_flag 경계값 테스트."""

    def test_strong_buy(self):
        assert _score_to_flag(85.0) == STRONG_BUY
        assert _score_to_flag(100.0) == STRONG_BUY

    def test_buy(self):
        assert _score_to_flag(70.0) == BUY
        assert _score_to_flag(84.9) == BUY

    def test_wait_buy(self):
        assert _score_to_flag(55.0) == WAIT_BUY
        assert _score_to_flag(69.9) == WAIT_BUY

    def test_neutral(self):
        assert _score_to_flag(45.0) == NEUTRAL
        assert _score_to_flag(54.9) == NEUTRAL

    def test_take_profit(self):
        assert _score_to_flag(30.0) == TAKE_PROFIT
        assert _score_to_flag(44.9) == TAKE_PROFIT

    def test_sell(self):
        assert _score_to_flag(15.0) == SELL
        assert _score_to_flag(29.9) == SELL

    def test_strong_sell(self):
        assert _score_to_flag(14.9) == STRONG_SELL
        assert _score_to_flag(0.0) == STRONG_SELL


class TestEvaluateSwingFlag:
    """evaluate_swing_flag 함수 테스트."""

    def test_score_in_range(self):
        result = evaluate_swing_flag(micron_features=_base_micron())
        assert 0 <= result["swing_score"] <= 100

    def test_flag_set(self):
        result = evaluate_swing_flag(micron_features=_base_micron())
        assert result["swing_flag"] in {
            STRONG_BUY, BUY, WAIT_BUY, NEUTRAL, TAKE_PROFIT, SELL, STRONG_SELL
        }

    def test_strong_buy_signal(self):
        result = evaluate_swing_flag(
            micron_features=_base_micron(strength=95.0, pm_ret=5.0),
            kospilab_expected_return_pct=3.0,
            tech_indicators=_base_tech(rsi=25.0, from_high=-12.0, bb=10.0),
            sox_return_pct=2.5,
            nvda_return_pct=3.0,
        )
        assert result["swing_score"] >= 70, f"점수 {result['swing_score']}가 70 미만"

    def test_strong_sell_signal(self):
        result = evaluate_swing_flag(
            micron_features=_base_micron(strength=10.0, pm_ret=-5.0),
            kospilab_expected_return_pct=-3.0,
            tech_indicators=_base_tech(rsi=80.0, from_high=-0.5, bb=90.0),
            sox_return_pct=-2.5,
            nvda_return_pct=-3.0,
        )
        assert result["swing_score"] <= 30, f"점수 {result['swing_score']}가 30 초과"

    def test_bottom_probability_in_range(self):
        result = evaluate_swing_flag(micron_features=_base_micron())
        assert 0 <= result["bottom_probability"] <= 100

    def test_top_probability_in_range(self):
        result = evaluate_swing_flag(micron_features=_base_micron())
        assert 0 <= result["top_probability"] <= 100

    def test_confidence_in_range(self):
        result = evaluate_swing_flag(
            micron_features=_base_micron(),
            tech_indicators=_base_tech(),
        )
        assert 0 <= result["confidence_score"] <= 100

    def test_price_zones_with_base_price(self):
        result = evaluate_swing_flag(
            micron_features=_base_micron(),
            hynix_prev_close=200_000.0,
        )
        bz_low  = result.get("buy_zone_low")
        bz_high = result.get("buy_zone_high")
        target  = result.get("target_price")
        stop    = result.get("stop_loss_price")
        assert bz_low is not None and bz_high is not None
        assert target is not None and stop is not None

    def test_empty_micron_features(self):
        empty = {k: None for k in _base_micron().keys()}
        result = evaluate_swing_flag(micron_features=empty)
        assert 0 <= result["swing_score"] <= 100

    def test_component_scores_present(self):
        result = evaluate_swing_flag(micron_features=_base_micron())
        comp = result.get("component_scores", {})
        assert len(comp) == 6

    def test_flag_color_present(self):
        result = evaluate_swing_flag(micron_features=_base_micron())
        assert result.get("flag_color", "").startswith("#")

    def test_flag_label_korean(self):
        result = evaluate_swing_flag(micron_features=_base_micron())
        label = result.get("flag_label", "")
        assert len(label) > 0

    def test_holding_days_none_for_neutral(self):
        # NEUTRAL 플래그는 보유기간 None
        neutral_micron = _base_micron(strength=50.0, pm_ret=0.0)
        result = evaluate_swing_flag(
            micron_features=neutral_micron,
            tech_indicators=_base_tech(rsi=50.0, from_high=-5.0, bb=50.0),
        )
        if result["swing_flag"] == NEUTRAL:
            assert result["expected_holding_days"] is None


class TestComputeHynixTechIndicators:
    """compute_hynix_tech_indicators 함수 테스트."""

    def test_rsi_in_range(self):
        df = _make_daily_df(n=70)
        indicators = compute_hynix_tech_indicators(df)
        rsi = indicators.get("rsi_14")
        if rsi is not None:
            assert 0 <= rsi <= 100

    def test_bollinger_pct_reasonable(self):
        df = _make_daily_df(n=70)
        indicators = compute_hynix_tech_indicators(df)
        bb = indicators.get("bollinger_pct")
        if bb is not None:
            assert -50 <= bb <= 200  # 극단값 허용

    def test_insufficient_data(self):
        df = _make_daily_df(n=3)
        indicators = compute_hynix_tech_indicators(df)
        # 데이터 부족 → rsi None
        assert indicators.get("rsi_14") is None

    def test_ma_positions_present(self):
        df = _make_daily_df(n=70, trend=0.002)
        indicators = compute_hynix_tech_indicators(df)
        # 상승 트렌드면 현재가 > MA → 양수
        assert indicators.get("ma5_position_pct") is not None
        assert indicators.get("ma20_position_pct") is not None
        assert indicators.get("ma60_position_pct") is not None

    def test_from_20d_high_nonpositive(self):
        df = _make_daily_df(n=30)
        indicators = compute_hynix_tech_indicators(df)
        high_pct = indicators.get("from_20d_high_pct")
        if high_pct is not None:
            # 현재가는 항상 20일 고점 이하 또는 같음
            assert high_pct <= 0.01  # 약간의 부동소수점 허용

    def test_empty_df(self):
        indicators = compute_hynix_tech_indicators(pd.DataFrame())
        assert all(v is None for v in indicators.values())
