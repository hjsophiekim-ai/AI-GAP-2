"""model_calibration.py — 과거 예측 오차의 rolling bias를 계산/저장/적용한다.

data/model_calibration/hynix_bias.json, data/model_calibration/market_bias.json
을 읽고 쓴다. 파일이 없거나 손상되어 있으면 항상 보정값 0으로 시작한다
(예외를 던지지 않는다 — calibration은 있으면 도움이 되는 보조 장치일 뿐,
없다고 예측 자체가 실패해서는 안 된다).

과최적화 방지 원칙:
  - 표본 20건 미만: 계산된 bias의 30%만 반영
  - 표본 20~49건: 70%까지 반영
  - 표본 50건 이상: 100% 반영
  - 반영 후 보정폭은 항상 ±0.8%(수익률 기준) 이내로 clip
  - market_collapse_score>=80(극단적 급락) 구간에서는 보정폭을 추가로 축소(0.4배)
    — 극단적 이벤트에서는 평상시 편향 보정을 그대로 적용하면 안 되기 때문.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.utils.data_paths import MODEL_CALIBRATION_DIR as CALIBRATION_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
HYNIX_BIAS_PATH = CALIBRATION_DIR / "hynix_bias.json"
MARKET_BIAS_PATH = CALIBRATION_DIR / "market_bias.json"

MAX_CORRECTION_PCT = 0.8
HYNIX_HORIZON_PRICE_FIELDS = {
    "30m": ("predicted_price_30m", "actual_price_30m"),
    "1h": ("predicted_price_1h", "actual_price_1h"),
    "3h": ("predicted_price_3h", "actual_price_3h"),
    "close": ("predicted_close_today", "actual_close_today"),
    "tomorrow_open": ("predicted_open_tomorrow", "actual_open_tomorrow"),
}


def _sample_weight(n: int) -> float:
    if n < 20:
        return 0.30
    if n < 50:
        return 0.70
    return 1.00


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[ModelCalibration] %s 읽기 실패(0으로 시작): %s", path, exc)
    return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        logger.warning("[ModelCalibration] %s 저장 실패: %s", path, exc)


# ---------------------------------------------------------------------------
# 조회 (예측 시점에 사용)
# ---------------------------------------------------------------------------

def get_hynix_bias_correction(horizon: str, market_collapse_score: Optional[float] = None) -> float:
    """horizon별 rolling bias 보정값(%, predicted_return에 더할 값)."""
    data = _read_json(HYNIX_BIAS_PATH)
    entry = data.get(horizon)
    if not entry:
        return 0.0
    bias_pct = entry.get("bias_pct", 0.0)
    sample_count = entry.get("sample_count", 0)
    correction = bias_pct * _sample_weight(sample_count)
    if market_collapse_score is not None and market_collapse_score >= 80.0:
        correction *= 0.4
    return max(-MAX_CORRECTION_PCT, min(MAX_CORRECTION_PCT, correction))


def get_hynix_bias_info(horizon: str) -> dict:
    """UI 표시용 — 보정값 + 표본 수 + 반영 비율."""
    data = _read_json(HYNIX_BIAS_PATH)
    entry = data.get(horizon, {})
    sample_count = entry.get("sample_count", 0)
    return {
        "bias_pct": entry.get("bias_pct", 0.0),
        "sample_count": sample_count,
        "applied_weight": _sample_weight(sample_count) if sample_count else 0.0,
        "correction_applied_pct": get_hynix_bias_correction(horizon),
        "computed_at": entry.get("computed_at"),
    }


def get_market_bias_info() -> dict:
    return _read_json(MARKET_BIAS_PATH)


# ---------------------------------------------------------------------------
# 계산/저장 (백테스트 리포트와 연동해서 주기적으로 재계산)
# ---------------------------------------------------------------------------

def compute_and_save_hynix_bias(rows: list[dict]) -> dict:
    """
    scripts/generate_backtest_report.py::build_hynix_backtest()가 반환하는
    row(리스트, predicted_price_X/actual_price_X/base_price 포함)를 받아
    horizon별 평균 오차(수익률 기준 bias, %)를 계산해 저장한다.
    """
    result: dict = {}
    for horizon, (pred_key, actual_key) in HYNIX_HORIZON_PRICE_FIELDS.items():
        errors_pct = []
        for row in rows:
            predicted = row.get(pred_key)
            actual = row.get(actual_key)
            base = row.get("base_price")
            if predicted is None or actual is None or not base:
                continue
            predicted_return = (predicted - base) / base * 100
            actual_return = (actual - base) / base * 100
            errors_pct.append(actual_return - predicted_return)
        if errors_pct:
            result[horizon] = {
                "bias_pct": round(sum(errors_pct) / len(errors_pct), 4),
                "sample_count": len(errors_pct),
                "computed_at": datetime.now().isoformat(timespec="seconds"),
            }
    _write_json(HYNIX_BIAS_PATH, result)
    return result


def compute_and_save_market_bias(rows: list[dict]) -> dict:
    """
    scripts/generate_backtest_report.py::build_market_backtest()가 반환하는
    row를 받아 horizon별 적중률/하락(위험) 편향 정도/D<->C 전환 감지 실패율을
    저장한다. 유형 분류 문제라 하이닉스처럼 %bias를 쓰지 않는다.
    """
    result: dict = {"horizons": {}, "computed_at": datetime.now().isoformat(timespec="seconds")}
    for horizon in ("30m", "1h", "3h"):
        matched = [r for r in rows if r.get(f"match_{horizon}") is not None]
        if not matched:
            continue
        correct = sum(1 for r in matched if r[f"match_{horizon}"])
        over_risk = sum(
            1 for r in matched
            if not r[f"match_{horizon}"] and r.get(f"predicted_regime_{horizon}") in ("D", "E")
        )
        result["horizons"][horizon] = {
            "sample_count": len(matched),
            "accuracy_pct": round(correct / len(matched) * 100, 1),
            "over_risk_miss_count": over_risk,
            "over_risk_miss_ratio_pct": round(over_risk / len(matched) * 100, 1),
        }

    cd_attempted = [r for r in rows if r.get("predicted_cd_transition") and r.get("cd_transition_confirmed") is not None]
    cd_confirmed = [r for r in cd_attempted if r.get("cd_transition_confirmed") is True]
    dc_recovery_attempted = [r for r in rows if r.get("current_regime") in ("D", "E") and r.get("predicted_regime_30m") == "C"]
    result["cd_transition"] = {
        "attempted": len(cd_attempted),
        "confirmed": len(cd_confirmed),
        "failure_rate_pct": round((1 - len(cd_confirmed) / len(cd_attempted)) * 100, 1) if cd_attempted else None,
    }
    result["dc_recovery_detection"] = {"attempted": len(dc_recovery_attempted)}
    _write_json(MARKET_BIAS_PATH, result)
    return result
