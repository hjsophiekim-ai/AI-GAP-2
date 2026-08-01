"""
test_hynix_prediction_tracker.py — 판단 로그 저장 + 3/5/10/30분 결과 추적 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

import app.services.hynix_prediction_tracker as tracker


def _patch_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "_DECISION_LOG_PATH", tmp_path / "trade_decision_log.csv")
    monkeypatch.setattr(tracker, "_OUTCOME_LOG_PATH", tmp_path / "prediction_outcome_log.csv")
    monkeypatch.setattr(tracker, "_PENDING_PATH", tmp_path / "hynix_pending_outcomes.json")


def _enhanced_result(score=60.0):
    return {
        "base_prediction_score": score, "existing_micron_score": score,
        "hynix_technical_score": score, "intraday_momentum_score": score,
        "inverse_pressure_score": 100 - score, "enhanced_score": score,
        "reason_top5": ["r1", "r2"], "micron_detail": {"micron_1min_score": score, "micron_3min_score": score},
    }


def test_log_trade_decision_writes_csv_row(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)
    now = datetime(2026, 7, 9, 10, 0, 0)

    tracker.log_trade_decision(
        now, 100_000, 5_000, _enhanced_result(), {"final_action": "HYNIX_BUY"},
        actual_trade_executed=True, position_symbol="000660",
    )

    assert tracker._DECISION_LOG_PATH.exists()
    df = pd.read_csv(tracker._DECISION_LOG_PATH)
    assert len(df) == 1
    assert df.iloc[0]["final_action"] == "HYNIX_BUY"
    assert df.iloc[0]["actual_trade_executed"] == True  # noqa: E712
    assert df.iloc[0]["reason_top1"] == "r1"


def test_pending_outcome_not_resolved_before_horizon(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)
    decision_time = datetime(2026, 7, 9, 10, 0, 0)
    tracker.enqueue_pending_outcomes(decision_time, "HYNIX_BUY", 100_000, 5_000, 60.0)

    resolved = tracker.check_and_resolve_pending_outcomes(decision_time + timedelta(minutes=2), 100_500, 5_010)

    assert resolved == []
    assert not tracker._OUTCOME_LOG_PATH.exists()


def test_pending_outcome_resolved_at_horizon(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)
    decision_time = datetime(2026, 7, 9, 10, 0, 0)
    tracker.enqueue_pending_outcomes(decision_time, "HYNIX_BUY", 100_000, 5_000, 60.0)

    resolved = tracker.check_and_resolve_pending_outcomes(decision_time + timedelta(minutes=3, seconds=1), 101_000, 4_950)

    horizons_resolved = {r["horizon_minutes"] for r in resolved}
    assert 3 in horizons_resolved
    df = pd.read_csv(tracker._OUTCOME_LOG_PATH)
    row_3m = df[df["horizon_minutes"] == 3].iloc[0]
    assert abs(row_3m["hynix_return_pct"] - 1.0) < 1e-6
    assert row_3m["prediction_correct"] in (True, "True")


def test_correlation_requires_minimum_rows():
    decision_df = pd.DataFrame({
        "timestamp": [datetime(2026, 7, 9, 10, 0)], "base_prediction_score": [60],
        "existing_micron_score": [60], "hynix_technical_score": [60],
        "intraday_momentum_score": [60], "inverse_pressure_score": [40],
    })
    outcome_df = pd.DataFrame({
        "decision_timestamp": [datetime(2026, 7, 9, 10, 0)], "horizon_minutes": [30], "hynix_return_pct": [1.0],
    })
    correlations = tracker.compute_score_outcome_correlations(decision_df, outcome_df, horizon_minutes=30)
    assert all(v is None for v in correlations.values())  # 5건 미만이라 상관계수 계산 안 함
