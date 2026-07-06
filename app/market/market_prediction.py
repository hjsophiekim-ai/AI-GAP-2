"""
market_prediction.py

실시간 장세 변화 감지 + 향후 30분/1시간/3시간/내일장 방향 예측.

기존 regime_router(A~F 정적 판단)와 분리된 "동적 예측" 계층이다. 지금은
룰 기반 가중합 점수로 구현하며, 나중에 ML 모델로 교체하기 쉽도록
predict_market_direction()/predict_tomorrow_market() 시그니처를 안정적으로
유지한다 (내부 스코어링 로직만 교체 가능한 구조).

절대 수익을 보장하지 않는다 — 조기경보/확률 추정 도구다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.market import regime_features as rf

HORIZONS = ("30m", "1h", "3h")

# 각 horizon의 "하락압력" 구성요소 가중치 (합계 100). 컴포넌트가 데이터 부재로
# None이면 해당 항목을 제외하고 나머지 가중치로 재정규화한다.
_HORIZON_WEIGHTS = {
    "30m": {"kospi200_futures": 25.0, "foreign": 25.0, "fx": 15.0, "semi_vwap": 15.0, "breadth": 10.0, "nasdaq_futures": 10.0},
    "1h":  {"foreign": 25.0, "kospi200_futures": 20.0, "breadth": 15.0, "fx": 15.0, "theme": 15.0, "nasdaq_futures": 10.0},
    "3h":  {"foreign": 25.0, "breadth": 20.0, "kospi200_futures": 20.0, "fx": 15.0, "theme": 15.0, "news": 5.0},
}

_COMPONENT_LABELS = {
    "kospi200_futures": "KOSPI200 선물 약세",
    "nasdaq_futures": "나스닥 선물 약세",
    "foreign": "외국인 수급 반전(프록시)",
    "fx": "원/달러 환율 급등",
    "semi_vwap": "반도체 대장주 VWAP 이탈",
    "breadth": "상승/하락 종목수 악화",
    "theme": "주도섹터 회전/붕괴",
    "news": "부정적 뉴스 모멘텀",
}


def _futures_component_score(snapshot: dict, field: str) -> Optional[float]:
    deltas = snapshot.get("deltas", {})
    d5 = (deltas.get("5m", {}) or {}).get(field)
    d15 = (deltas.get("15m", {}) or {}).get(field)
    parts = []
    if d5 is not None:
        parts.append((rf._norm(-d5, 0.3), 0.6))
    if d15 is not None:
        parts.append((rf._norm(-d15, 0.6), 0.4))
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / total_w


def _build_components(snapshot: dict, ref_0920: Optional[dict] = None) -> dict:
    """예측에 쓰는 컴포넌트 점수(0~100, 높을수록 하락압력)를 모두 계산한다."""
    return {
        "kospi200_futures": _futures_component_score(snapshot, "kospi200_futures_change_rate"),
        "nasdaq_futures": _futures_component_score(snapshot, "nasdaq_futures_change_rate"),
        "foreign": rf.compute_foreign_flow_reversal_score(snapshot),
        "fx": rf.compute_fx_risk_score(snapshot),
        "semi_vwap": 100.0 - rf.compute_semiconductor_leadership_score(snapshot),
        "breadth": rf.compute_breadth_deterioration_score(snapshot),
        "theme": rf.compute_theme_rotation_score(snapshot, ref_0920),
        "news": rf.compute_news_shock_score(snapshot),
    }


def _weighted_pressure(components: dict, weights: dict) -> tuple[float, dict]:
    """가중합 하락압력(0~100) + 실제 반영된 컴포넌트별 기여도를 반환한다."""
    used = {}
    weighted_sum = 0.0
    weight_total = 0.0
    for key, w in weights.items():
        v = components.get(key)
        if v is None:
            continue
        weighted_sum += v * w
        weight_total += w
        used[key] = v
    pressure = (weighted_sum / weight_total) if weight_total > 0 else 50.0
    return round(max(0.0, min(100.0, pressure)), 2), used


def _pressure_to_probabilities(down_pressure: float) -> tuple[float, float, float]:
    """down_pressure(0~100, 50=중립) -> (p_down, p_sideways, p_up), 합계=100."""
    d = max(0.0, min(100.0, down_pressure))
    bias = d - 50.0  # -50(강한 상승압력) ~ +50(강한 하락압력)
    p_down = max(2.0, min(90.0, 33.34 + bias * 1.1))
    p_up = max(2.0, min(90.0, 33.33 - bias * 1.1))
    p_sideways = max(2.0, 100.0 - p_down - p_up)
    total = p_down + p_sideways + p_up
    p_down, p_sideways, p_up = (round(x / total * 100, 1) for x in (p_down, p_sideways, p_up))
    return p_down, p_sideways, p_up


def _direction_from_probs(p_down: float, p_sideways: float, p_up: float) -> str:
    best = max(p_down, p_sideways, p_up)
    if best == p_down:
        return "DOWN"
    if best == p_up:
        return "UP"
    return "SIDEWAYS"


def _expected_regime(direction: str, down_pressure: float, market_collapse_score: float, current_regime: str) -> str:
    if direction == "DOWN":
        return "E" if market_collapse_score >= 80 else "D"
    if direction == "UP":
        return "A"
    return current_regime or "F"


def predict_market_direction(
    horizon: str,
    snapshot: dict,
    regime_result: Optional[dict] = None,
    ref_0920: Optional[dict] = None,
) -> dict:
    """
    향후 30분/1시간/3시간 국내증시(및 반도체) 방향을 확률로 예측한다.

    Parameters
    ----------
    horizon : "30m" | "1h" | "3h"
    snapshot : market_data_collector.collect() 결과 (deltas 포함)
    regime_result : MarketRegimeRouter.determine_regime() 결과 (선택 — 있으면
                    market_collapse_score/현재 regime을 재사용)
    ref_0920 : 09:20 기준 스냅샷 (theme_rotation_score 계산에 사용)

    Returns
    -------
    dict: horizon, direction, probability_up/sideways/down, expected_regime,
          confidence_score, key_reasons, risk_flags, components
    """
    if horizon not in _HORIZON_WEIGHTS:
        raise ValueError(f"지원하지 않는 horizon: {horizon} (30m/1h/3h만 가능)")

    regime_result = regime_result or {}
    scores = regime_result.get("scores", {}) or {}
    market_collapse_score = scores.get("market_collapse_score")
    if market_collapse_score is None:
        market_collapse_score = rf.compute_market_collapse_score(snapshot, ref_0920)
    current_regime = regime_result.get("regime", "F")

    components = _build_components(snapshot, ref_0920)
    weights = _HORIZON_WEIGHTS[horizon]
    down_pressure, used = _weighted_pressure(components, weights)

    p_down, p_sideways, p_up = _pressure_to_probabilities(down_pressure)
    direction = _direction_from_probs(p_down, p_sideways, p_up)
    expected_regime = _expected_regime(direction, down_pressure, market_collapse_score, current_regime)

    # 신뢰도: 데이터 완결성(가중치 커버리지) x 판단의 뚜렷함(중립 50에서 얼마나 떨어졌는지)
    coverage = (sum(used.values()) and len(used) / len(weights)) or 0.0
    decisiveness = min(1.0, abs(down_pressure - 50.0) / 40.0)
    confidence_score = round(max(10.0, min(95.0, 40.0 * coverage + 55.0 * decisiveness + 5.0)), 1)

    ranked = sorted(used.items(), key=lambda kv: abs(kv[1] - 50.0) * weights.get(kv[0], 0), reverse=True)
    key_reasons = [
        f"{_COMPONENT_LABELS.get(k, k)} ({v:.0f}점)"
        for k, v in ranked[:4] if abs(v - 50.0) >= 8.0
    ]
    if not key_reasons:
        key_reasons = ["뚜렷한 우세 신호 없음 — 중립적 흐름"]

    risk_flags = [
        _COMPONENT_LABELS.get(k, k) for k, v in used.items() if v >= 65.0
    ]

    result = {
        "horizon": horizon,
        "direction": direction,
        "probability_up": p_up,
        "probability_sideways": p_sideways,
        "probability_down": p_down,
        "expected_regime": expected_regime,
        "confidence_score": confidence_score,
        "key_reasons": key_reasons,
        "risk_flags": risk_flags,
        "down_pressure_score": down_pressure,
        "components": used,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
    logger.debug(
        "[MarketPrediction] horizon=%s direction=%s p_down=%.1f expected_regime=%s conf=%.1f",
        horizon, direction, p_down, expected_regime, confidence_score,
    )
    return result


def predict_all_horizons(snapshot: dict, regime_result: Optional[dict] = None, ref_0920: Optional[dict] = None) -> dict:
    """30분/1시간/3시간 예측을 한번에 계산해 {horizon: result} 로 반환한다."""
    return {h: predict_market_direction(h, snapshot, regime_result, ref_0920) for h in HORIZONS}


# ---------------------------------------------------------------------------
# 내일장 예측
# ---------------------------------------------------------------------------

def _tomorrow_state(now_hm: Optional[str] = None) -> str:
    """preliminary(장중) / closing_based(장마감 후) / us_session_updated(다음날 개장전) 구분."""
    now_hm = now_hm or datetime.now().strftime("%H:%M")
    if now_hm < "08:50":
        return "us_session_updated"
    if now_hm < "15:30":
        return "preliminary"
    return "closing_based"


def predict_tomorrow_market(
    snapshot: dict,
    regime_history: Optional[list] = None,
    now_hm: Optional[str] = None,
    ref_0920: Optional[dict] = None,
) -> dict:
    """
    내일 한국장 방향을 예측한다 (오늘 하루 흐름 + 미국장/환율/반도체 대장주 기준).

    Returns
    -------
    dict: tomorrow_direction, probability_up/sideways/down, expected_open_gap,
          expected_leading_sector, semiconductor_next_day_bias, risk_level,
          confidence_score, key_reasons, state
    """
    state = _tomorrow_state(now_hm)
    domestic = snapshot.get("domestic", {})
    overseas = snapshot.get("overseas", {})

    components = _build_components(snapshot, ref_0920)
    market_collapse = rf.compute_market_collapse_score(snapshot, ref_0920)
    semi_collapse = rf.compute_semiconductor_collapse_score(snapshot)

    # regime 변화 이력이 험할수록(악화 방향 전환이 많을수록) 하락 가중
    regime_change_penalty = 0.0
    if regime_history:
        bad = {"D", "E", "F"}
        transitions_to_bad = sum(
            1 for i in range(1, len(regime_history))
            if regime_history[i].get("regime") in bad and regime_history[i - 1].get("regime") not in bad
        )
        regime_change_penalty = min(20.0, transitions_to_bad * 10.0)

    weights = {"foreign": 25.0, "breadth": 15.0, "kospi200_futures": 10.0, "fx": 15.0, "theme": 10.0, "news": 5.0}
    down_pressure, used = _weighted_pressure(components, weights)
    down_pressure = round(min(100.0, down_pressure + regime_change_penalty), 2)

    # 미국 마지막거래일/프리마켓 흐름 반영 (야간 미국장 결과가 다음날 갭에 큰 영향)
    us_bias_components = []
    for key, scale in (("nasdaq", 1.0), ("sox", 2.0)):
        node = overseas.get(key)
        if node and node.get("success"):
            us_bias_components.append(rf._norm(-rf._rate(node), scale))
    last_session = overseas.get("us_last_session", {}) or {}
    for key, scale in (("micron", 3.0), ("nvidia", 3.0)):
        node = last_session.get(key)
        if node and node.get("success") and node.get("change_rate") is not None:
            us_bias_components.append(rf._norm(-node["change_rate"], scale))
    us_bias = sum(us_bias_components) / len(us_bias_components) if us_bias_components else 50.0
    down_pressure = round(min(100.0, max(0.0, down_pressure * 0.7 + us_bias * 0.3)), 2)

    p_down, p_sideways, p_up = _pressure_to_probabilities(down_pressure)
    direction = _direction_from_probs(p_down, p_sideways, p_up)

    if direction == "DOWN":
        gap = "GAP_DOWN" if down_pressure >= 65 else "FLAT"
    elif direction == "UP":
        gap = "GAP_UP" if down_pressure <= 35 else "FLAT"
    else:
        gap = "FLAT"

    leader_sectors = rf._leader_sectors(snapshot)
    expected_leading_sector = leader_sectors[0] if leader_sectors else "unknown"

    if semi_collapse >= 65:
        semi_bias = "NEGATIVE"
    elif semi_collapse <= 35:
        semi_bias = "POSITIVE"
    else:
        semi_bias = "NEUTRAL"

    if market_collapse >= 80 or semi_collapse >= 80:
        risk_level = "HIGH"
    elif market_collapse >= 60 or semi_collapse >= 60:
        risk_level = "ELEVATED"
    else:
        risk_level = "NORMAL"

    us_status = overseas.get("us_market_status", {}) or {}
    coverage = len(used) / len(weights) if weights else 0.0
    decisiveness = min(1.0, abs(down_pressure - 50.0) / 40.0)
    confidence_score = round(max(10.0, min(90.0, 35.0 * coverage + 45.0 * decisiveness + 10.0)), 1)
    if us_status.get("is_us_holiday") or us_status.get("is_us_weekend"):
        confidence_score = round(confidence_score * 0.9, 1)  # 휴장으로 미국 데이터 공백 -> 소폭 하향

    ranked = sorted(used.items(), key=lambda kv: abs(kv[1] - 50.0) * weights.get(kv[0], 0), reverse=True)
    key_reasons = [f"{_COMPONENT_LABELS.get(k, k)} ({v:.0f}점)" for k, v in ranked[:3] if abs(v - 50.0) >= 8.0]
    if regime_change_penalty > 0:
        key_reasons.append(f"오늘 장중 유형이 악화 방향으로 {int(regime_change_penalty/10)}회 전환")
    if us_bias_components:
        key_reasons.append(f"미국 반도체/나스닥 흐름 반영(추정 {us_bias:.0f}점)")
    if not key_reasons:
        key_reasons = ["뚜렷한 우세 신호 없음"]

    return {
        "tomorrow_direction": direction,
        "probability_up": p_up,
        "probability_sideways": p_sideways,
        "probability_down": p_down,
        "expected_open_gap": gap,
        "expected_leading_sector": expected_leading_sector,
        "semiconductor_next_day_bias": semi_bias,
        "risk_level": risk_level,
        "confidence_score": confidence_score,
        "key_reasons": key_reasons,
        "state": state,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
