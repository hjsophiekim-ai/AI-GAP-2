"""
test_hynix_predictor.py — SK하이닉스 예측 모델 테스트.
"""

import pytest
from app.models.hynix_predictor import predict_hynix


def _base_micron_features(strength: float = 70.0, pm_ret: float = 2.0) -> dict:
    return {
        "micron_premarket_return":        pm_ret,
        "micron_premarket_open_to_now":   pm_ret * 0.8,
        "micron_premarket_high_to_now":   -0.5,
        "micron_premarket_low_to_now":    pm_ret * 1.2,
        "micron_premarket_30m_momentum":  pm_ret * 0.6,
        "micron_premarket_60m_momentum":  pm_ret * 0.9,
        "micron_premarket_vwap":          102.5,
        "micron_premarket_volume_change": 15.0,
        "micron_regular_return":          None,
        "micron_aftermarket_return":      None,
        "micron_session_strength_score":  strength,
    }


class TestPredictHynix:
    """predict_hynix 함수 테스트."""

    def test_required_fields_present(self):
        pred = predict_hynix(micron_features=_base_micron_features())
        required = {
            "today_open_expected", "today_high_expected",
            "today_low_expected", "today_close_expected",
            "today_return_pct", "tomorrow_return_pct", "day3_return_pct",
            "two_week_high_date", "two_week_high_price", "two_week_high_prob",
            "two_week_low_date", "two_week_low_price", "two_week_low_prob",
            "up_probability", "down_probability", "confidence_score",
            "predicted_at", "model_version", "weights_used", "composite_signal",
            "signals",
        }
        assert required.issubset(set(pred.keys()))

    def test_probabilities_sum_to_100(self):
        pred = predict_hynix(micron_features=_base_micron_features())
        total = pred["up_probability"] + pred["down_probability"]
        assert abs(total - 100.0) < 0.5, f"상승+하락 확률 합계 = {total}"

    def test_strong_micron_positive_return(self):
        features = _base_micron_features(strength=85.0, pm_ret=4.0)
        pred = predict_hynix(
            micron_features=features,
            kospilab_expected_return_pct=2.5,
            sox_return_pct=1.5,
        )
        assert pred["today_return_pct"] > 0

    def test_weak_micron_negative_return(self):
        features = _base_micron_features(strength=20.0, pm_ret=-3.0)
        pred = predict_hynix(
            micron_features=features,
            kospilab_expected_return_pct=-2.0,
            sox_return_pct=-1.0,
        )
        assert pred["today_return_pct"] < 0

    def test_up_probability_higher_on_bullish(self):
        features = _base_micron_features(strength=90.0, pm_ret=5.0)
        pred = predict_hynix(micron_features=features)
        assert pred["up_probability"] > pred["down_probability"]

    def test_down_probability_higher_on_bearish(self):
        features = _base_micron_features(strength=10.0, pm_ret=-5.0)
        pred = predict_hynix(micron_features=features)
        assert pred["down_probability"] > pred["up_probability"]

    def test_price_range_with_base(self):
        features = _base_micron_features()
        pred = predict_hynix(
            micron_features=features,
            hynix_prev_close=200_000.0,
        )
        assert pred["today_high_expected"] is not None
        assert pred["today_low_expected"] is not None
        assert pred["today_high_expected"] >= pred["today_low_expected"]

    def test_confidence_in_range(self):
        pred = predict_hynix(micron_features=_base_micron_features())
        assert 0 <= pred["confidence_score"] <= 100

    def test_empty_micron_features(self):
        empty = {k: None for k in _base_micron_features().keys()}
        pred = predict_hynix(micron_features=empty)
        assert "today_return_pct" in pred
        # 신호 없을 때 확률 합계는 100
        total = pred["up_probability"] + pred["down_probability"]
        assert abs(total - 100.0) < 0.5

    def test_two_week_high_price_gt_low_price(self):
        features = _base_micron_features()
        pred = predict_hynix(
            micron_features=features,
            hynix_prev_close=200_000.0,
        )
        high = pred.get("two_week_high_price") or 0
        low  = pred.get("two_week_low_price") or 0
        if high and low:
            assert high >= low
