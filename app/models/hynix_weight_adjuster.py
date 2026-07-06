"""
hynix_weight_adjuster.py — SK하이닉스 예측 모델 가중치 자동 조정 모듈.

최근 20개 예측 결과를 분석해 성능이 좋은 지표의 가중치를 늘리고
성능이 나쁜 지표의 가중치를 줄입니다.

규칙:
- 1회 실패로 크게 바꾸지 않음
- 1회 조정당 각 지표 최대 ±3%p
- 마이크론 가중치: 30%~60%
- 코스피랩 가중치: 15%~35%
- 전체 합계는 항상 100% 정규화
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent.parent
_WEIGHTS_PATH = _ROOT / "config" / "hynix_model_weights.json"
_HISTORY_PATH = _ROOT / "data" / "predictions" / "weight_adjustment_history.csv"

# 가중치 제약 (min, max)
_CONSTRAINTS = {
    "micron_premarket_aftermarket": (0.30, 0.60),
    "kospilab_expected_price":      (0.15, 0.35),
    "sox_index":                    (0.02, 0.20),
    "nvda":                         (0.01, 0.15),
    "qqq_nasdaq_futures":           (0.01, 0.15),
    "usd_krw":                      (0.01, 0.10),
    "hynix_momentum_volume":        (0.01, 0.15),
}

_MAX_CHANGE_PER_ROUND = 0.03  # 1회 최대 ±3%p


def load_weights() -> dict:
    """현재 가중치 로드."""
    defaults = {
        "micron_premarket_aftermarket": 0.45,
        "kospilab_expected_price":      0.25,
        "sox_index":                    0.10,
        "nvda":                         0.07,
        "qqq_nasdaq_futures":           0.05,
        "usd_krw":                      0.03,
        "hynix_momentum_volume":        0.05,
    }
    try:
        if _WEIGHTS_PATH.exists():
            with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("weights", defaults)
    except Exception:
        pass
    return defaults


def save_weights(weights: dict, reason: str = "") -> None:
    """가중치를 JSON에 저장하고 히스토리 CSV에 기록."""
    _WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version":    "1.0.0",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "weights":    weights,
        "constraints": {
            "micron_min":          0.30,
            "micron_max":          0.60,
            "kospilab_min":        0.15,
            "kospilab_max":        0.35,
            "max_change_per_round": _MAX_CHANGE_PER_ROUND,
        },
    }
    with open(_WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    _save_history(weights, reason)


def _save_history(weights: dict, reason: str) -> None:
    """가중치 조정 이력 CSV에 저장."""
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_exists = _HISTORY_PATH.exists()
    with open(_HISTORY_PATH, "a", newline="", encoding="utf-8-sig") as f:
        fieldnames = ["adjusted_at", "reason"] + list(weights.keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        row = {"adjusted_at": datetime.now().isoformat(), "reason": reason}
        row.update({k: round(v, 4) for k, v in weights.items()})
        writer.writerow(row)


def adjust_weights_from_predictions(
    predictions: list[dict],
    n_recent: int = 20,
) -> dict:
    """
    최근 n개 예측 결과를 분석해 가중치를 자동 조정.

    Parameters
    ----------
    predictions : load_predictions() 결과 (실제값이 채워진 행)
    n_recent    : 분석 대상 최근 예측 수

    Returns
    -------
    dict
        {"old_weights": ..., "new_weights": ..., "changes": ..., "reason": str}
    """
    current = load_weights()

    # 실제값이 있는 예측만 필터링
    completed = [
        p for p in predictions
        if p.get("actual_close") and str(p.get("actual_close", "")).strip()
    ]
    recent = completed[-n_recent:] if len(completed) >= 5 else []

    if not recent:
        return {
            "old_weights": current,
            "new_weights": current,
            "changes":     {},
            "reason":      f"분석 데이터 부족 (실제값 있는 예측 {len(completed)}건 < 5건)",
        }

    # ── 지표별 기여도 분석 ────────────────────────────────────────────────────
    # composite_signal과 실제 방향의 일치율로 각 지표 성능 간접 측정
    # (실제 지표별 신호가 없으므로 composite 전체로 대리)
    correct_count = 0
    total_count = len(recent)
    for p in recent:
        try:
            pred_close   = float(p.get("today_close_expected") or 0)
            actual_close = float(p.get("actual_close") or 0)
            pred_open    = float(p.get("today_open_expected") or actual_close)
            if pred_close <= 0 or actual_close <= 0:
                continue
            pred_dir   = pred_close >= pred_open
            actual_dir = actual_close >= float(p.get("actual_open") or actual_close)
            if pred_dir == actual_dir:
                correct_count += 1
        except Exception:
            continue

    accuracy = correct_count / total_count if total_count > 0 else 0.5

    new_weights = {k: v for k, v in current.items()}
    changes: dict[str, float] = {}

    # 전체 정확도가 50% 미만이면 마이크론 의존도를 소폭 줄임
    if accuracy < 0.45:
        delta = min(_MAX_CHANGE_PER_ROUND, 0.02)
        _adjust(new_weights, changes, "micron_premarket_aftermarket", -delta)
        _adjust(new_weights, changes, "kospilab_expected_price", +delta * 0.5)
        _adjust(new_weights, changes, "hynix_momentum_volume", +delta * 0.5)
        reason_text = (
            f"최근 {total_count}건 방향 정확도 {accuracy:.0%} — "
            "마이크론 가중치 소폭 감소, 자체 모멘텀 증가"
        )
    elif accuracy > 0.65:
        # 정확도 높으면 현재 가중치 유지 (미세 증가)
        delta = min(_MAX_CHANGE_PER_ROUND, 0.01)
        _adjust(new_weights, changes, "micron_premarket_aftermarket", +delta)
        _adjust(new_weights, changes, "hynix_momentum_volume", -delta)
        reason_text = (
            f"최근 {total_count}건 방향 정확도 {accuracy:.0%} (우수) — "
            "마이크론 가중치 소폭 증가"
        )
    else:
        reason_text = (
            f"최근 {total_count}건 방향 정확도 {accuracy:.0%} (적정) — "
            "가중치 유지"
        )

    new_weights = _normalize(new_weights)
    new_weights = _apply_constraints(new_weights)
    new_weights = _normalize(new_weights)

    return {
        "old_weights": current,
        "new_weights": new_weights,
        "changes":     changes,
        "reason":      reason_text,
        "accuracy":    round(accuracy, 4),
        "n_samples":   total_count,
    }


def _adjust(
    weights: dict,
    changes: dict,
    key: str,
    delta: float,
) -> None:
    """단일 가중치 조정 (제약 적용)."""
    lo, hi = _CONSTRAINTS.get(key, (0.0, 1.0))
    old = weights.get(key, 0.0)
    # delta를 ±MAX_CHANGE 범위로 클램핑
    delta = max(-_MAX_CHANGE_PER_ROUND, min(_MAX_CHANGE_PER_ROUND, delta))
    new = max(lo, min(hi, old + delta))
    weights[key] = new
    changes[key] = round(new - old, 4)


def _apply_constraints(weights: dict) -> dict:
    """각 지표 min/max 제약 적용."""
    for key, (lo, hi) in _CONSTRAINTS.items():
        if key in weights:
            weights[key] = max(lo, min(hi, weights[key]))
    return weights


def _normalize(weights: dict) -> dict:
    """전체 합계가 1.0이 되도록 정규화."""
    total = sum(weights.values())
    if total <= 0:
        return weights
    return {k: round(v / total, 6) for k, v in weights.items()}


# ── 스윙 플래그 가중치 조정 ───────────────────────────────────────────────────

_SWING_WEIGHTS_PATH = _ROOT / "config" / "hynix_swing_weights.json"

_SWING_CONSTRAINTS = {
    "micron_premarket": (0.15, 0.50),
    "kospilab":         (0.10, 0.35),
    "tech_position":    (0.10, 0.45),
    "volume_momentum":  (0.03, 0.20),
    "semiconductor":    (0.03, 0.20),
    "currency_risk":    (0.01, 0.10),
}

_SWING_DEFAULTS = {
    "micron_premarket": 0.30,
    "kospilab":         0.20,
    "tech_position":    0.25,
    "volume_momentum":  0.10,
    "semiconductor":    0.10,
    "currency_risk":    0.05,
}


def load_swing_weights() -> dict:
    """스윙 가중치 로드."""
    try:
        if _SWING_WEIGHTS_PATH.exists():
            with open(_SWING_WEIGHTS_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("weights", _SWING_DEFAULTS)
    except Exception:
        pass
    return dict(_SWING_DEFAULTS)


def save_swing_weights(weights: dict, reason: str = "") -> None:
    """스윙 가중치를 JSON에 저장."""
    _SWING_WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_SWING_WEIGHTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["weights"] = weights
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(_SWING_WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def adjust_swing_weights_from_flags(
    swing_flags: list[dict],
    n_recent: int = 20,
) -> dict:
    """
    최근 n개 스윙 플래그 결과를 분석해 스윙 가중치를 자동 조정.

    규칙
    ----
    - BUY 플래그 후 3d 수익률이 반복적으로 음수 → BUY 기준 상향 (micron 비중 감소)
    - SELL 플래그 후 3d 수익률이 반복적으로 양수 → SELL 기준 하향 (tech 비중 증가)
    - 1회 조정 최대 ±3%p

    Parameters
    ----------
    swing_flags : load_swing_flags() 결과
    n_recent    : 분석 기준 최근 건수

    Returns
    -------
    dict
        {old_weights, new_weights, changes, reason, accuracy, n_samples}
    """
    current = load_swing_weights()

    # 실제 결과가 있는 플래그만
    completed = [
        f for f in swing_flags
        if f.get("flag_hit") in ("0", "1", 0, 1)
    ]
    recent = completed[-n_recent:] if len(completed) >= 5 else []

    if not recent:
        return {
            "old_weights": current,
            "new_weights": current,
            "changes":     {},
            "reason":      f"분석 데이터 부족 (적중 여부 확인된 플래그 {len(completed)}건 < 5건)",
        }

    # 전체 적중률
    hits  = sum(1 for f in recent if str(f.get("flag_hit")) == "1")
    total = len(recent)
    accuracy = hits / total

    # 매수/매도 계열 분리 적중률
    buy_flags  = [f for f in recent if f.get("swing_flag") in ("STRONG_BUY", "BUY", "WAIT_BUY")]
    sell_flags = [f for f in recent if f.get("swing_flag") in ("TAKE_PROFIT", "SELL", "STRONG_SELL")]

    buy_acc  = (sum(1 for f in buy_flags  if str(f.get("flag_hit")) == "1") / len(buy_flags)
                if buy_flags else 0.5)
    sell_acc = (sum(1 for f in sell_flags if str(f.get("flag_hit")) == "1") / len(sell_flags)
                if sell_flags else 0.5)

    new_weights = dict(current)
    changes: dict[str, float] = {}

    # BUY 플래그 자주 빗나감 → 마이크론 과대, 기술적 지표 비중 증가
    if buy_acc < 0.40 and buy_flags:
        delta = min(_MAX_CHANGE_PER_ROUND, 0.02)
        _adjust_swing(new_weights, changes, "micron_premarket", -delta)
        _adjust_swing(new_weights, changes, "tech_position", +delta)
        reason_text = (
            f"BUY 플래그 적중률 {buy_acc:.0%} 저조 — "
            "마이크론 가중치 감소, 기술적 지표 가중치 증가"
        )
    # SELL 플래그 자주 빗나감 → 기술적 과대, 코스피랩 비중 증가
    elif sell_acc < 0.40 and sell_flags:
        delta = min(_MAX_CHANGE_PER_ROUND, 0.02)
        _adjust_swing(new_weights, changes, "tech_position", -delta)
        _adjust_swing(new_weights, changes, "kospilab", +delta)
        reason_text = (
            f"SELL 플래그 적중률 {sell_acc:.0%} 저조 — "
            "기술적 지표 가중치 감소, 코스피랩 가중치 증가"
        )
    elif accuracy > 0.65:
        delta = min(_MAX_CHANGE_PER_ROUND, 0.01)
        _adjust_swing(new_weights, changes, "micron_premarket", +delta)
        _adjust_swing(new_weights, changes, "tech_position", -delta / 2)
        _adjust_swing(new_weights, changes, "kospilab", -delta / 2)
        reason_text = (
            f"전체 적중률 {accuracy:.0%} 우수 — "
            "마이크론 가중치 소폭 증가"
        )
    else:
        reason_text = (
            f"전체 적중률 {accuracy:.0%} (적정) — 가중치 유지"
        )

    # 정규화 및 제약 적용
    new_weights = _normalize_swing(new_weights)
    new_weights = _apply_swing_constraints(new_weights)
    new_weights = _normalize_swing(new_weights)

    return {
        "old_weights": current,
        "new_weights": new_weights,
        "changes":     changes,
        "reason":      reason_text,
        "accuracy":    round(accuracy, 4),
        "buy_accuracy": round(buy_acc, 4),
        "sell_accuracy": round(sell_acc, 4),
        "n_samples":   total,
    }


def _adjust_swing(weights: dict, changes: dict, key: str, delta: float) -> None:
    """스윙 가중치 단일 조정."""
    lo, hi = _SWING_CONSTRAINTS.get(key, (0.0, 1.0))
    old = weights.get(key, 0.0)
    delta = max(-_MAX_CHANGE_PER_ROUND, min(_MAX_CHANGE_PER_ROUND, delta))
    new = max(lo, min(hi, old + delta))
    weights[key] = new
    changes[key] = round(new - old, 4)


def _apply_swing_constraints(weights: dict) -> dict:
    """스윙 가중치 제약 적용."""
    for key, (lo, hi) in _SWING_CONSTRAINTS.items():
        if key in weights:
            weights[key] = max(lo, min(hi, weights[key]))
    return weights


def _normalize_swing(weights: dict) -> dict:
    """스윙 가중치 정규화."""
    total = sum(weights.values())
    if total <= 0:
        return weights
    return {k: round(v / total, 6) for k, v in weights.items()}
