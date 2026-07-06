"""
test_weight_adjuster.py — 가중치 자동 조정 모듈 테스트.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from app.models.hynix_weight_adjuster import (
    _normalize,
    _apply_constraints,
    _CONSTRAINTS,
    adjust_weights_from_predictions,
    load_weights,
    save_weights,
)


class TestNormalize:
    """_normalize 함수 테스트."""

    def test_sum_equals_one(self):
        weights = {"a": 0.4, "b": 0.3, "c": 0.3}
        norm = _normalize(weights)
        assert abs(sum(norm.values()) - 1.0) < 1e-6

    def test_proportions_maintained(self):
        weights = {"a": 2.0, "b": 2.0, "c": 1.0}
        norm = _normalize(weights)
        assert abs(norm["a"] - norm["b"]) < 1e-6
        assert norm["a"] > norm["c"]

    def test_empty(self):
        result = _normalize({})
        assert result == {}


class TestApplyConstraints:
    """_apply_constraints 함수 테스트."""

    def test_micron_min_enforced(self):
        weights = {k: 0.0 for k in _CONSTRAINTS}
        result = _apply_constraints(weights)
        lo, hi = _CONSTRAINTS["micron_premarket_aftermarket"]
        assert result["micron_premarket_aftermarket"] >= lo

    def test_micron_max_enforced(self):
        weights = {k: 1.0 for k in _CONSTRAINTS}
        result = _apply_constraints(weights)
        lo, hi = _CONSTRAINTS["micron_premarket_aftermarket"]
        assert result["micron_premarket_aftermarket"] <= hi

    def test_kospilab_min_enforced(self):
        weights = {k: 0.0 for k in _CONSTRAINTS}
        result = _apply_constraints(weights)
        lo, hi = _CONSTRAINTS["kospilab_expected_price"]
        assert result["kospilab_expected_price"] >= lo


class TestAdjustWeightsFromPredictions:
    """adjust_weights_from_predictions 함수 테스트."""

    def _make_predictions(self, n: int, correct_ratio: float) -> list[dict]:
        rows = []
        for i in range(n):
            is_correct = (i / n) < correct_ratio
            rows.append({
                "today_return_pct":      "2.0" if is_correct else "-2.0",
                "today_close_expected":  "210000",
                "today_open_expected":   "205000",
                "actual_close":          "210000" if is_correct else "200000",
                "actual_open":           "205000",
            })
        return rows

    def test_result_has_new_weights(self):
        preds = self._make_predictions(20, 0.5)
        result = adjust_weights_from_predictions(preds)
        assert "new_weights" in result
        assert "old_weights" in result

    def test_new_weights_sum_to_one(self):
        preds = self._make_predictions(20, 0.4)
        result = adjust_weights_from_predictions(preds)
        total = sum(result["new_weights"].values())
        assert abs(total - 1.0) < 1e-4, f"가중치 합 = {total}"

    def test_insufficient_data_no_change(self):
        preds = self._make_predictions(3, 0.5)  # 5건 미만
        result = adjust_weights_from_predictions(preds)
        assert result["new_weights"] == result["old_weights"]
        assert "부족" in result["reason"]

    def test_micron_weight_in_bounds(self):
        preds = self._make_predictions(20, 0.3)  # 낮은 정확도
        result = adjust_weights_from_predictions(preds)
        w = result["new_weights"]["micron_premarket_aftermarket"]
        lo, hi = _CONSTRAINTS["micron_premarket_aftermarket"]
        assert lo <= w <= hi

    def test_kospilab_weight_in_bounds(self):
        preds = self._make_predictions(20, 0.7)  # 높은 정확도
        result = adjust_weights_from_predictions(preds)
        w = result["new_weights"]["kospilab_expected_price"]
        lo, hi = _CONSTRAINTS["kospilab_expected_price"]
        assert lo <= w <= hi

    def test_change_per_round_limit(self):
        preds = self._make_predictions(20, 0.3)
        result = adjust_weights_from_predictions(preds)
        old = result["old_weights"]
        new = result["new_weights"]
        for key in old:
            change = abs(new.get(key, 0) - old.get(key, 0))
            assert change <= 0.04, f"{key} 변경량 {change:.4f}이 3%p 초과 (정규화 감안 4%p 허용)"


class TestSaveLoadWeights:
    """save_weights / load_weights 통합 테스트."""

    def test_save_and_reload(self, tmp_path, monkeypatch):
        import app.models.hynix_weight_adjuster as mod
        monkeypatch.setattr(mod, "_WEIGHTS_PATH", tmp_path / "weights.json")
        monkeypatch.setattr(mod, "_HISTORY_PATH", tmp_path / "history.csv")

        test_weights = {
            "micron_premarket_aftermarket": 0.40,
            "kospilab_expected_price":      0.25,
            "sox_index":                    0.10,
            "nvda":                         0.08,
            "qqq_nasdaq_futures":           0.07,
            "usd_krw":                      0.05,
            "hynix_momentum_volume":        0.05,
        }
        save_weights(test_weights, reason="테스트")
        loaded = load_weights()
        for k, v in test_weights.items():
            assert abs(loaded.get(k, 0) - v) < 1e-6
