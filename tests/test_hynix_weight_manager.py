"""
test_hynix_weight_manager.py — 추천 가중치는 '적용' 버튼(explicit call) 후에만 반영되는지 검증.
"""

from __future__ import annotations

import json

import pytest

import app.services.hynix_weight_manager as weight_manager
import app.services.hynix_weight_recommender as recommender


def test_active_weights_unchanged_until_apply_called(tmp_path, monkeypatch):
    monkeypatch.setattr(weight_manager, "_ACTIVE_WEIGHTS_PATH", tmp_path / "hynix_model_weights.json")
    monkeypatch.setattr(recommender, "_RECOMMENDATION_PATH", tmp_path / "hynix_weight_recommendation.json")

    default_weights = weight_manager.get_active_weights()

    fake_recommendation = {
        "skipped": False,
        "current_weights": default_weights,
        "recommended_weights": {
            "base_prediction": 0.50, "existing_micron": 0.15, "hynix_technical": 0.25, "intraday_momentum": 0.10,
        },
        "reason": "test", "sample_size": 150, "expected_improvement": 0.01, "created_at": "2026-07-09T00:00:00",
    }
    recommender._RECOMMENDATION_PATH.write_text(json.dumps(fake_recommendation), encoding="utf-8")

    # 추천 파일이 존재해도 '적용'을 호출하기 전에는 실제 가중치가 바뀌지 않아야 함
    assert weight_manager.get_active_weights() == default_weights

    result = weight_manager.apply_recommended_weights()
    assert result["success"] is True

    applied = weight_manager.get_active_weights()
    assert applied["base_prediction"] == pytest.approx(0.50, abs=1e-3)
    assert applied != default_weights


def test_reset_to_default_restores_config_weights(tmp_path, monkeypatch):
    monkeypatch.setattr(weight_manager, "_ACTIVE_WEIGHTS_PATH", tmp_path / "hynix_model_weights.json")
    default_weights = weight_manager.get_default_weights()

    weight_manager._write_active_weights(
        {"base_prediction": 0.9, "existing_micron": 0.03, "hynix_technical": 0.04, "intraday_momentum": 0.03},
        source="manual_test",
    )
    assert weight_manager.get_active_weights() != default_weights

    result = weight_manager.reset_weights_to_default()
    assert result["success"] is True
    assert weight_manager.get_active_weights() == default_weights


def test_maybe_auto_apply_only_in_mock_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(weight_manager, "_ACTIVE_WEIGHTS_PATH", tmp_path / "hynix_model_weights.json")
    monkeypatch.setattr(recommender, "_RECOMMENDATION_PATH", tmp_path / "hynix_weight_recommendation.json")
    recommender._RECOMMENDATION_PATH.write_text(json.dumps({
        "skipped": False, "recommended_weights": {"base_prediction": 0.5, "existing_micron": 0.15, "hynix_technical": 0.25, "intraday_momentum": 0.10},
    }), encoding="utf-8")

    assert weight_manager.maybe_auto_apply_in_mock("real", True) is None
    default_weights = weight_manager.get_active_weights()
    assert default_weights["base_prediction"] != pytest.approx(0.5, abs=1e-3)

    result = weight_manager.maybe_auto_apply_in_mock("mock", True)
    assert result is not None and result["success"] is True
