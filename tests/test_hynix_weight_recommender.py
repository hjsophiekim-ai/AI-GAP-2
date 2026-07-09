"""
test_hynix_weight_recommender.py — 가중치 추천(샘플 가드/합계1.0/±5%p 제한) 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

import app.services.hynix_prediction_tracker as tracker
import app.services.hynix_weight_recommender as recommender
import app.services.hynix_weight_manager as weight_manager


def _build_synthetic_logs(tmp_path, n_per_day=25, n_days=5):
    decision_rows = []
    outcome_rows = []
    base_time = datetime(2026, 7, 1, 10, 0, 0)
    for day in range(n_days):
        for i in range(n_per_day):
            ts = base_time + timedelta(days=day, minutes=i * 3)
            tech = 50 + (i % 10) * 3 - 15
            micron = 50 + ((i * 7) % 21) - 10
            actual_return = tech * 0.08  # hynix_technical_score와 강하게 상관되도록 구성
            decision_rows.append({
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "hynix_price": 100000, "inverse_price": 5000,
                "base_prediction_score": 50, "existing_micron_score": micron, "micron_1min_score": micron, "micron_3min_score": micron,
                "hynix_technical_score": tech, "intraday_momentum_score": 50, "inverse_pressure_score": 100 - tech,
                "enhanced_score": 50 + tech * 0.1, "final_action": "HOLD", "actual_trade_executed": False, "position_symbol": "",
                "reason_top1": "", "reason_top2": "", "reason_top3": "", "reason_top4": "", "reason_top5": "",
            })
            outcome_rows.append({
                "decision_timestamp": ts.isoformat(), "outcome_timestamp": (ts + timedelta(minutes=30)).isoformat(),
                "horizon_minutes": 30, "predicted_action": "HOLD", "predicted_direction": "neutral",
                "hynix_price_at_decision": 100000, "hynix_price_at_outcome": 100000 * (1 + actual_return / 100),
                "inverse_price_at_decision": 5000, "inverse_price_at_outcome": 5000,
                "hynix_return_pct": actual_return, "inverse_return_pct": 0, "prediction_correct": "True",
                "score_error": 1.0, "realized_trade_pnl": "",
            })
    dec_path = tmp_path / "trade_decision_log.csv"
    out_path = tmp_path / "prediction_outcome_log.csv"
    pd.DataFrame(decision_rows).to_csv(dec_path, index=False)
    pd.DataFrame(outcome_rows).to_csv(out_path, index=False)
    return dec_path, out_path


def _patch(tmp_path, monkeypatch, n_per_day=25, n_days=5):
    dec_path, out_path = _build_synthetic_logs(tmp_path, n_per_day=n_per_day, n_days=n_days)
    monkeypatch.setattr(tracker, "_DECISION_LOG_PATH", dec_path)
    monkeypatch.setattr(tracker, "_OUTCOME_LOG_PATH", out_path)
    monkeypatch.setattr(recommender, "_RECOMMENDATION_PATH", tmp_path / "hynix_weight_recommendation.json")
    monkeypatch.setattr(weight_manager, "_ACTIVE_WEIGHTS_PATH", tmp_path / "hynix_model_weights.json")


def test_skips_when_sample_size_below_100(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch, n_per_day=10, n_days=1)  # 10건 < 100

    result = recommender.recommend_weight_adjustment()

    assert result["skipped"] is True
    assert result["recommended_weights"] is None


def test_recommended_weights_sum_to_one_and_within_delta(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch, n_per_day=25, n_days=5)  # 125건 >= 100

    result = recommender.recommend_weight_adjustment()

    assert result["skipped"] is False
    assert result["sample_size"] >= 100
    weights = result["recommended_weights"]
    assert abs(sum(weights.values()) - 1.0) < 1e-3

    current = result["current_weights"]
    for key, value in weights.items():
        assert abs(value - current[key]) <= recommender.MAX_DELTA + 1e-6


def test_technical_score_identified_as_predictive_signal(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch, n_per_day=25, n_days=5)

    result = recommender.recommend_weight_adjustment()

    correlations = result["correlations"]
    assert correlations["hynix_technical_score"] is not None
    assert abs(correlations["hynix_technical_score"]) > abs(correlations.get("existing_micron_score") or 0)
