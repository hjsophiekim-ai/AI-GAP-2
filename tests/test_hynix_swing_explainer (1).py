"""
test_hynix_swing_explainer.py — 스윙 판단 한글 설명 생성 테스트.
"""

from __future__ import annotations

import pytest

from app.models.hynix_swing_flag import (
    STRONG_BUY, BUY, WAIT_BUY, NEUTRAL, TAKE_PROFIT, SELL, STRONG_SELL,
    evaluate_swing_flag,
)
from app.models.hynix_swing_explainer import generate_swing_explanation


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
        "micron_aftermarket_return":      1.0,
        "micron_session_strength_score":  strength,
    }


def _base_tech() -> dict:
    return {
        "rsi_14":            45.0,
        "macd":              0.5,
        "macd_signal_cross": 1,  # 골든크로스
        "ma5_position_pct":  1.0,
        "ma20_position_pct": 2.0,
        "ma60_position_pct": 4.0,
        "from_20d_high_pct": -8.0,
        "from_20d_low_pct":  5.0,
        "bollinger_pct":     25.0,
        "prev_candle_type":  1,   # 장대양봉
        "return_3d_pct":     -3.0,
        "return_5d_pct":     2.0,
        "return_10d_pct":    5.0,
        "volume_change_pct": 30.0,
    }


class TestGenerateSwingExplanation:
    """generate_swing_explanation 함수 테스트."""

    def test_returns_nonempty_string(self):
        mf = _base_micron()
        swing = evaluate_swing_flag(micron_features=mf, tech_indicators=_base_tech())
        result = generate_swing_explanation(
            swing_result=swing,
            micron_features=mf,
            tech_indicators=_base_tech(),
        )
        assert isinstance(result, str)
        assert len(result) > 10

    def test_korean_characters_present(self):
        mf = _base_micron()
        swing = evaluate_swing_flag(micron_features=mf)
        result = generate_swing_explanation(swing_result=swing, micron_features=mf)
        # 한글 문자가 포함되어야 함
        korean_count = sum(1 for c in result if "가" <= c <= "힣")
        assert korean_count > 5, f"한글 문자가 너무 적음: {korean_count}개"

    def test_strong_buy_contains_buy_keyword(self):
        mf = _base_micron(strength=95.0, pm_ret=5.0)
        ti = {
            "rsi_14": 22.0, "from_20d_high_pct": -12.0, "bollinger_pct": 8.0,
            "macd_signal_cross": 0, "prev_candle_type": 0,
            "ma5_position_pct": None, "ma20_position_pct": None, "ma60_position_pct": None,
            "from_20d_low_pct": None, "return_3d_pct": None, "return_5d_pct": None,
            "return_10d_pct": None, "volume_change_pct": None, "macd": None,
        }
        swing = evaluate_swing_flag(
            micron_features=mf,
            kospilab_expected_return_pct=3.0,
            tech_indicators=ti,
            sox_return_pct=2.0,
        )
        result = generate_swing_explanation(
            swing_result=swing,
            micron_features=mf,
            tech_indicators=ti,
            kospilab_return=3.0,
        )
        # 매수 관련 키워드가 있어야 함
        buy_keywords = ["매수", "저점", "반등", "강세", "우호"]
        assert any(kw in result for kw in buy_keywords), \
            f"매수 키워드 없음. 설명: {result}"

    def test_strong_sell_contains_sell_keyword(self):
        mf = _base_micron(strength=10.0, pm_ret=-5.0)
        ti = {
            "rsi_14": 82.0, "from_20d_high_pct": -0.5, "bollinger_pct": 92.0,
            "macd_signal_cross": -1, "prev_candle_type": -1,
            "ma5_position_pct": None, "ma20_position_pct": None, "ma60_position_pct": None,
            "from_20d_low_pct": 25.0, "return_3d_pct": None, "return_5d_pct": 12.0,
            "return_10d_pct": None, "volume_change_pct": None, "macd": None,
        }
        swing = evaluate_swing_flag(
            micron_features=mf,
            kospilab_expected_return_pct=-3.0,
            tech_indicators=ti,
        )
        result = generate_swing_explanation(
            swing_result=swing,
            micron_features=mf,
            tech_indicators=ti,
        )
        sell_keywords = ["매도", "고점", "과매수", "약세", "주의", "하락"]
        assert any(kw in result for kw in sell_keywords), \
            f"매도 키워드 없음. 설명: {result}"

    def test_empty_tech_indicators(self):
        mf = _base_micron()
        swing = evaluate_swing_flag(micron_features=mf)
        result = generate_swing_explanation(
            swing_result=swing,
            micron_features=mf,
            tech_indicators=None,
        )
        assert isinstance(result, str) and len(result) > 5

    def test_data_insufficient_graceful(self):
        empty_mf = {k: None for k in _base_micron().keys()}
        swing = evaluate_swing_flag(micron_features=empty_mf)
        result = generate_swing_explanation(
            swing_result=swing,
            micron_features=empty_mf,
        )
        assert isinstance(result, str)

    def test_macd_golden_cross_in_explanation(self):
        mf = _base_micron()
        ti = dict(_base_tech())
        ti["macd_signal_cross"] = 1  # 골든크로스
        swing = evaluate_swing_flag(micron_features=mf, tech_indicators=ti)
        result = generate_swing_explanation(swing_result=swing, micron_features=mf, tech_indicators=ti)
        assert "골든크로스" in result

    def test_macd_dead_cross_in_explanation(self):
        mf = _base_micron(strength=30.0, pm_ret=-2.0)
        ti = dict(_base_tech())
        ti["macd_signal_cross"] = -1
        swing = evaluate_swing_flag(micron_features=mf, tech_indicators=ti)
        result = generate_swing_explanation(swing_result=swing, micron_features=mf, tech_indicators=ti)
        assert "데드크로스" in result

    def test_bollinger_lower_in_explanation(self):
        mf = _base_micron()
        ti = dict(_base_tech())
        ti["bollinger_pct"] = 8.0
        swing = evaluate_swing_flag(micron_features=mf, tech_indicators=ti)
        result = generate_swing_explanation(swing_result=swing, micron_features=mf, tech_indicators=ti)
        assert "볼린저" in result

    def test_rsi_oversold_in_explanation(self):
        mf = _base_micron()
        ti = dict(_base_tech())
        ti["rsi_14"] = 25.0
        swing = evaluate_swing_flag(micron_features=mf, tech_indicators=ti)
        result = generate_swing_explanation(swing_result=swing, micron_features=mf, tech_indicators=ti)
        assert "RSI" in result and ("과매도" in result or "반등" in result)

    def test_action_summary_always_present(self):
        mf = _base_micron()
        swing = evaluate_swing_flag(micron_features=mf)
        result = generate_swing_explanation(swing_result=swing, micron_features=mf)
        assert "종합 판단:" in result
