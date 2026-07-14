"""
hynix_action_decider.py — 최종 판단 결정 (decide_hynix_or_inverse_action).

enhanced_score(하이닉스 강세)와 inverse_pressure_score(하이닉스 약세/인버스 강세)를
함께 보고 최종 행동(HYNIX_STRONG_BUY/HYNIX_BUY/HOLD/INVERSE_BUY/INVERSE_STRONG_BUY)을
결정한다. 데이터 부족·신호 상충 시에는 항상 HOLD로 안전하게 귀결시킨다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.trading.hynix_fast_trend import is_live_hynix_uptrend

ROOT = Path(__file__).resolve().parent.parent.parent
_WEIGHTS_PATH = ROOT / "config" / "hynix_enhanced_weights.json"

HYNIX_STRONG_BUY = "HYNIX_STRONG_BUY"
HYNIX_BUY = "HYNIX_BUY"
HOLD = "HOLD"
INVERSE_BUY = "INVERSE_BUY"
INVERSE_STRONG_BUY = "INVERSE_STRONG_BUY"

_DEFAULT_THRESHOLDS = {
    "strong_buy_enhanced_min": 75,
    "strong_buy_inverse_max": 40,
    "buy_enhanced_min": 60,
    "buy_inverse_max": 50,
    "hold_min": 45,
    "hold_max": 59,
    "inverse_buy_min": 50,
    "inverse_strong_buy_min": 70,
    "conflict_enhanced_min": 65,
    "conflict_inverse_min": 55,
    "min_score_gap_for_forced_trade": 5,
}

_MICRON_STRONG_THRESHOLD = 70.0
_TECH_STRONG_WEAK_THRESHOLD = 30.0


def _load_thresholds() -> dict:
    try:
        if _WEIGHTS_PATH.exists():
            data = json.loads(_WEIGHTS_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULT_THRESHOLDS, **(data.get("decision_thresholds") or {})}
    except Exception as exc:
        logger.debug("[ActionDecider] 임계값 로드 실패, 기본값 사용: %s", exc)
    return dict(_DEFAULT_THRESHOLDS)


def decide_hynix_or_inverse_action(enhanced_result: dict, current_position: Optional[dict] = None) -> dict:
    """최종 판단(final_action) 결정.

    Parameters
    ----------
    enhanced_result   : calculate_enhanced_hynix_prediction_score() 결과
    current_position  : {"symbol": "000660"|"0197X0"|None, ...} 현재 보유 정보(선택, 참고용)
    """
    th = _load_thresholds()

    enhanced_score = float(enhanced_result.get("enhanced_score", 50.0))
    raw_inverse_pressure_score = float(enhanced_result.get("inverse_pressure_score", 100.0 - enhanced_score))
    inverse_score = max(0.0, min(100.0, 100.0 - enhanced_score))
    existing_micron_score = float(enhanced_result.get("existing_micron_score", 50.0))
    hynix_technical_score = float(enhanced_result.get("hynix_technical_score", 50.0))
    data_valid = enhanced_result.get("data_valid", {}) or {}
    fast_live_trend = enhanced_result.get("fast_live_trend") or enhanced_result.get("live_hynix_trend") or {}

    reasons: list[str] = []
    if abs(raw_inverse_pressure_score - inverse_score) > 0.01:
        reasons.append(
            f"score polarity normalized: hynix_score={enhanced_score:.1f}, "
            f"inverse_score=100-hynix={inverse_score:.1f}, raw_inverse_pressure={raw_inverse_pressure_score:.1f}"
        )

    # ── 1. 데이터 부족 ────────────────────────────────────────────────────────
    if not (data_valid.get("base_prediction", True) and data_valid.get("hynix_technical", True)):
        reasons.append("핵심 데이터(기존 예측점수/기술점수) 부족 — 안전하게 보류")
        return _result(HOLD, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    # ── 2. 신호 상충 ─────────────────────────────────────────────────────────
    if enhanced_score >= th["conflict_enhanced_min"] and raw_inverse_pressure_score >= th["conflict_inverse_min"]:
        reasons.append(
            f"enhanced_score {enhanced_score:.1f}(≥{th['conflict_enhanced_min']})와 "
            f"inverse_pressure_score {inverse_score:.1f}(≥{th['conflict_inverse_min']}) 동시 상승 — 상충 보류"
        )

    if existing_micron_score >= _MICRON_STRONG_THRESHOLD and hynix_technical_score <= _TECH_STRONG_WEAK_THRESHOLD:
        reasons.append(
            f"마이크론 강세({existing_micron_score:.1f}) vs 하이닉스 기술점수 강한 약세({hynix_technical_score:.1f}) 상충 — 보류"
        )
        return _result(HOLD, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    if existing_micron_score <= (100 - _MICRON_STRONG_THRESHOLD) and hynix_technical_score >= (100 - _TECH_STRONG_WEAK_THRESHOLD):
        reasons.append(
            f"마이크론 강한 약세({existing_micron_score:.1f}) vs 하이닉스 기술점수 강세({hynix_technical_score:.1f}) 상충 — 보류"
        )
        return _result(HOLD, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    # ── 3. 기본 임계값 판정 ──────────────────────────────────────────────────
    if enhanced_score >= th["strong_buy_enhanced_min"] and inverse_score < th["strong_buy_inverse_max"]:
        reasons.append(f"enhanced_score {enhanced_score:.1f}≥{th['strong_buy_enhanced_min']}, inverse {inverse_score:.1f}<{th['strong_buy_inverse_max']}")
        return _result(HYNIX_STRONG_BUY, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    if enhanced_score >= th["buy_enhanced_min"] and inverse_score < th["buy_inverse_max"]:
        reasons.append(f"enhanced_score {enhanced_score:.1f}≥{th['buy_enhanced_min']}, inverse {inverse_score:.1f}<{th['buy_inverse_max']}")
        return _result(HYNIX_BUY, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    if th["hold_min"] <= enhanced_score <= th["hold_max"]:
        reasons.append(f"enhanced_score {enhanced_score:.1f} is in HOLD band({th['hold_min']}~{th['hold_max']})")
        return _result(HOLD, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    if is_live_hynix_uptrend(fast_live_trend):
        returns = fast_live_trend.get("returns") or {}
        reasons.append(
            "live Hynix uptrend blocks new INVERSE: "
            f"VWAP=above, 3m={returns.get('3m')}, 5m={returns.get('5m')}, "
            f"ema_slope={fast_live_trend.get('ema_slope_pct')}"
        )
        return _result(HOLD, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    if inverse_score >= th["inverse_strong_buy_min"]:
        reasons.append(f"inverse_pressure_score {inverse_score:.1f}≥{th['inverse_strong_buy_min']}")
        return _result(INVERSE_STRONG_BUY, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    if inverse_score >= th["inverse_buy_min"]:
        reasons.append(f"inverse_pressure_score {inverse_score:.1f}≥{th['inverse_buy_min']}")
        return _result(INVERSE_BUY, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)

    if th["hold_min"] <= enhanced_score <= th["hold_max"]:
        reasons.append(f"enhanced_score {enhanced_score:.1f} — 보류 구간({th['hold_min']}~{th['hold_max']})")
    else:
        reasons.append(f"enhanced_score {enhanced_score:.1f}, inverse_pressure_score {inverse_score:.1f} — 명확한 신호 없음")
    return _result(HOLD, enhanced_score, inverse_score, reasons, th, score_gap_blocked=False)


def _result(final_action: str, enhanced_score: float, inverse_score: float, reasons: list[str], th: dict, score_gap_blocked: bool) -> dict:
    score_gap = abs(enhanced_score - inverse_score)
    return {
        "final_action": final_action,
        "enhanced_score": enhanced_score,
        "inverse_pressure_score": inverse_score,
        "score_gap": round(score_gap, 2),
        "score_gap_below_forced_trade_threshold": score_gap < th["min_score_gap_for_forced_trade"],
        "reasons": reasons,
    }
