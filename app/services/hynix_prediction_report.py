"""
hynix_prediction_report.py — 일별 예측 정확도 리포트 생성.

매 장 종료 후(또는 필요 시 수동) `data/reports/hynix_prediction_daily_report.csv`에
그날의 3/5/10/30분·종가 적중률, 평균 점수오차, 최고/최저 상관 신호를 한 행씩 append한다.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.services.hynix_prediction_tracker import (
    _read_decision_log_for_date, _read_outcome_log_for_dates,
    compute_accuracy, compute_score_outcome_correlations,
)

ROOT = Path(__file__).resolve().parent.parent.parent
_REPORT_PATH = ROOT / "data" / "reports" / "hynix_prediction_daily_report.csv"

_REPORT_COLUMNS = [
    "date", "total_predictions",
    "correct_3m", "correct_5m", "correct_10m", "correct_30m", "correct_close",
    "accuracy_3m", "accuracy_5m", "accuracy_10m", "accuracy_30m", "accuracy_close",
    "avg_score_error", "best_signal", "worst_signal", "recommended_weight_adjustment",
]


def _correct_count(outcome_df, horizon) -> int:
    if outcome_df is None or outcome_df.empty:
        return 0
    sub = outcome_df[outcome_df["horizon_minutes"].astype(str) == str(horizon)]
    return int((sub["prediction_correct"].astype(str) == "True").sum())


def generate_daily_prediction_report(date_str: Optional[str] = None) -> dict:
    date_str = date_str or datetime.now().strftime("%Y%m%d")
    decisions = _read_decision_log_for_date(date_str)
    outcomes = _read_outcome_log_for_dates([date_str])

    row = {"date": date_str, "total_predictions": len(decisions)}
    for horizon, key in [(3, "3m"), (5, "5m"), (10, "10m"), (30, "30m"), ("close", "close")]:
        row[f"correct_{key}"] = _correct_count(outcomes, horizon)
        row[f"accuracy_{key}"] = compute_accuracy(outcomes, horizon)

    avg_error = None
    try:
        if not outcomes.empty:
            import pandas as pd

            errs = pd.to_numeric(outcomes["score_error"], errors="coerce").dropna()
            avg_error = round(float(errs.mean()), 2) if not errs.empty else None
    except Exception as exc:
        logger.debug("[PredictionReport] 평균 점수오차 계산 실패: %s", exc)
    row["avg_score_error"] = avg_error

    correlations = compute_score_outcome_correlations(decisions, outcomes, horizon_minutes=30)
    valid = {k: v for k, v in correlations.items() if v is not None}
    if valid:
        best_signal = max(valid, key=lambda k: abs(valid[k]))
        worst_signal = min(valid, key=lambda k: abs(valid[k]))
        row["best_signal"] = best_signal
        row["worst_signal"] = worst_signal
        row["recommended_weight_adjustment"] = (
            f"{best_signal}(상관 {valid[best_signal]:+.2f}) 비중 확대 검토, "
            f"{worst_signal}(상관 {valid[worst_signal]:+.2f}) 비중 축소 검토"
        )
    else:
        row["best_signal"] = ""
        row["worst_signal"] = ""
        row["recommended_weight_adjustment"] = "데이터 부족(30분 outcome 5건 미만) — 판단 불가"

    try:
        _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing_dates = set()
        if _REPORT_PATH.exists():
            with _REPORT_PATH.open("r", encoding="utf-8-sig", newline="") as f:
                for existing_row in csv.DictReader(f):
                    existing_dates.add(existing_row.get("date"))
        if date_str not in existing_dates:
            is_new = not _REPORT_PATH.exists()
            with _REPORT_PATH.open("a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=_REPORT_COLUMNS)
                if is_new:
                    writer.writeheader()
                writer.writerow({col: row.get(col, "") for col in _REPORT_COLUMNS})
    except Exception as exc:
        logger.debug("[PredictionReport] 리포트 저장 실패: %s", exc)

    return row
