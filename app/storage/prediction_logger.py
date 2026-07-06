"""
prediction_logger.py — 예측 결과 저장 모듈.

예측 실행 때마다 입력 feature 전체, 예측 결과, 메타정보를
CSV(핵심값)와 JSONL(전체 상세)로 기록합니다.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent.parent
_PRED_DIR = _ROOT / "data" / "predictions"

PREDICTIONS_CSV   = _PRED_DIR / "hynix_predictions.csv"
PREDICTIONS_JSONL = _PRED_DIR / "hynix_prediction_details.jsonl"

_CSV_FIELDS = [
    "predicted_at", "model_version",
    "today_return_pct",
    "today_open_expected", "today_high_expected",
    "today_low_expected",  "today_close_expected",
    "tomorrow_return_pct", "day3_return_pct",
    "two_week_high_date",  "two_week_high_price", "two_week_high_prob",
    "two_week_low_date",   "two_week_low_price",  "two_week_low_prob",
    "up_probability", "down_probability", "confidence_score",
    "composite_signal",
    # 실제 결과 (장 종료 후 업데이트)
    "actual_open", "actual_high", "actual_low", "actual_close",
    "actual_tomorrow_close", "actual_day3_close",
]


def log_prediction(
    prediction: dict,
    micron_features: dict,
    micron_current_price: Any,
    kospilab_inputs: dict,
    other_inputs: dict,
) -> None:
    """
    예측 결과를 CSV + JSONL에 저장.

    Parameters
    ----------
    prediction          : predict_hynix() 반환 dict
    micron_features     : compute_micron_features() 반환 dict
    micron_current_price: fetch_mu_current_price() 반환 dict 또는 None
    kospilab_inputs     : {kospilab_expected_price, kospilab_expected_return_pct}
    other_inputs        : {sox_return_pct, nvda_return_pct, qqq_return_pct, ...}
    """
    _PRED_DIR.mkdir(parents=True, exist_ok=True)

    # CSV: 핵심 예측값
    row = {f: prediction.get(f, "") for f in _CSV_FIELDS}
    file_exists = PREDICTIONS_CSV.exists()
    with open(PREDICTIONS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    # JSONL: 전체 상세
    record = {
        "predicted_at":          prediction.get("predicted_at", datetime.now().isoformat()),
        "model_version":         prediction.get("model_version", ""),
        "weights_used":          prediction.get("weights_used", {}),
        "prediction":            prediction,
        "micron_features":       micron_features,
        "micron_current_price":  micron_current_price,
        "kospilab_inputs":       kospilab_inputs,
        "other_inputs":          other_inputs,
    }
    with open(PREDICTIONS_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_predictions() -> list[dict]:
    """저장된 예측 목록 전체 로드."""
    if not PREDICTIONS_CSV.exists():
        return []
    with open(PREDICTIONS_CSV, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def update_actual_results(
    predicted_at_prefix: str,
    actual_open: float,
    actual_high: float,
    actual_low: float,
    actual_close: float,
    actual_tomorrow_close: Optional[float] = None,
    actual_day3_close: Optional[float] = None,
) -> bool:
    """
    장 종료 후 실제 결과를 CSV에 업데이트.

    Parameters
    ----------
    predicted_at_prefix : 예측 시각 앞 16자리 (분 단위 매칭)

    Returns
    -------
    bool : 업데이트 성공 여부
    """
    if not PREDICTIONS_CSV.exists():
        return False

    rows = []
    updated = False
    with open(PREDICTIONS_CSV, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("predicted_at", "")[:16] == predicted_at_prefix[:16]:
                row["actual_open"]  = actual_open
                row["actual_high"]  = actual_high
                row["actual_low"]   = actual_low
                row["actual_close"] = actual_close
                if actual_tomorrow_close is not None:
                    row["actual_tomorrow_close"] = actual_tomorrow_close
                if actual_day3_close is not None:
                    row["actual_day3_close"] = actual_day3_close
                updated = True
            rows.append(row)

    if updated:
        with open(PREDICTIONS_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return updated
