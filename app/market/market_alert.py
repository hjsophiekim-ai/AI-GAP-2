"""
market_alert.py

조기경보(Market Alert) 산출. NONE -> WATCH -> WARNING -> CRITICAL 4단계.

CRITICAL이면 auto_trader/position_guard가 신규매수를 즉시 차단하고 보유
포지션을 방어적으로 재평가해야 한다 (연결은 app/execution 쪽에서 처리).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

NONE = "NONE"
WATCH = "WATCH"
WARNING = "WARNING"
CRITICAL = "CRITICAL"

_LEVEL_ORDER = {NONE: 0, WATCH: 1, WARNING: 2, CRITICAL: 3}

_ACTION_TEXT = {
    NONE: "정상 — 특별 조치 불필요",
    WATCH: "관찰 강화 — 신규매수 시 신중하게 접근",
    WARNING: "신규매수 신중(수동승인 권장), 보유종목 리스크 즉시 재평가",
    CRITICAL: "신규매수 즉시 중단·자동매수 OFF, 보유종목 즉시 리스크 재평가 및 위험청산 검토",
}

_BAD_REGIMES = {"D", "E"}


@dataclass
class MarketAlert:
    alert_level: str
    reasons: list = field(default_factory=list)
    action_recommendation: str = ""

    def at_least(self, level: str) -> bool:
        return _LEVEL_ORDER.get(self.alert_level, 0) >= _LEVEL_ORDER.get(level, 0)


def _escalate(current: str, new: str) -> str:
    return new if _LEVEL_ORDER.get(new, 0) > _LEVEL_ORDER.get(current, 0) else current


def compute_alert_level(
    current_regime: str,
    predicted_regime_30m: Optional[str],
    predicted_down_30m: float,
    market_collapse_score: float,
    semiconductor_collapse_score: float,
    foreign_flow_reversal_score: float = 50.0,
    hynix_vwap_broken: bool = False,
    kospi200_futures_weak: bool = False,
) -> MarketAlert:
    """
    Parameters
    ----------
    current_regime : 현재 확정 유형 (A~F)
    predicted_regime_30m : 30분 후 예상 유형
    predicted_down_30m : predict_market_direction("30m")["probability_down"]
    market_collapse_score, semiconductor_collapse_score : regime_features 산출값
    foreign_flow_reversal_score : regime_features 산출값 (WATCH 판단용)
    hynix_vwap_broken : 하이닉스가 VWAP 아래로 이탈했는지
    kospi200_futures_weak : KOSPI200 선물 당일 등락률이 뚜렷하게 마이너스인지
    """
    level = NONE
    reasons: list[str] = []

    # ── WATCH ──────────────────────────────────────────────────────────────
    regime_mismatch = bool(predicted_regime_30m) and predicted_regime_30m != current_regime
    foreign_drop = foreign_flow_reversal_score >= 65.0
    if regime_mismatch:
        reasons.append(f"현재 유형({current_regime})과 30분 후 예상 유형({predicted_regime_30m}) 불일치")
        level = _escalate(level, WATCH)
    if foreign_drop:
        reasons.append(f"외국인 수급(프록시) 반전 위험 {foreign_flow_reversal_score:.0f}점")
        level = _escalate(level, WATCH)

    # ── WARNING ────────────────────────────────────────────────────────────
    transition_risk = (
        current_regime in ("A", "B", "C")
        and predicted_regime_30m in _BAD_REGIMES
        and predicted_down_30m >= 60.0
    )
    if transition_risk:
        reasons.append(f"{current_regime}→{predicted_regime_30m} 전환 위험(하락확률 {predicted_down_30m:.0f}%)")
        level = _escalate(level, WARNING)

    if hynix_vwap_broken and kospi200_futures_weak:
        reasons.append("하이닉스 VWAP 이탈 + KOSPI200 선물 약세 동시 발생")
        level = _escalate(level, WARNING)

    # ── CRITICAL ───────────────────────────────────────────────────────────
    if market_collapse_score >= 80.0:
        reasons.append(f"market_collapse_score {market_collapse_score:.0f} >= 80")
        level = _escalate(level, CRITICAL)
    if semiconductor_collapse_score >= 80.0:
        reasons.append(f"semiconductor_collapse_score {semiconductor_collapse_score:.0f} >= 80")
        level = _escalate(level, CRITICAL)
    if predicted_down_30m >= 80.0:
        reasons.append(f"predicted_down_30m {predicted_down_30m:.0f}% >= 80")
        level = _escalate(level, CRITICAL)

    if not reasons:
        reasons = ["특이 신호 없음"]

    alert = MarketAlert(alert_level=level, reasons=reasons, action_recommendation=_ACTION_TEXT[level])
    if level in (WARNING, CRITICAL):
        logger.warning("[MarketAlert] level=%s reasons=%s", level, reasons)
    return alert


def compute_alert_from_results(
    regime_result: dict,
    prediction_30m: dict,
    snapshot: dict,
) -> MarketAlert:
    """regime_router/market_prediction 결과 dict들로부터 편의상 바로 계산한다."""
    scores = regime_result.get("scores", {}) or {}
    domestic = snapshot.get("domestic", {})
    hynix = domestic.get("hynix", {}) or {}
    hynix_vwap_broken = bool(
        hynix.get("current_price") and hynix.get("vwap") and hynix["current_price"] < hynix["vwap"]
    )
    kospi200_futures_weak = (domestic.get("kospi200_futures", {}) or {}).get("change_rate", 0) is not None and (
        (domestic.get("kospi200_futures", {}) or {}).get("change_rate", 0) < -0.5
    )

    return compute_alert_level(
        current_regime=regime_result.get("regime", "F"),
        predicted_regime_30m=prediction_30m.get("expected_regime"),
        predicted_down_30m=prediction_30m.get("probability_down", 0.0),
        market_collapse_score=scores.get("market_collapse_score", 0.0),
        semiconductor_collapse_score=scores.get("semiconductor_collapse_score", 0.0),
        foreign_flow_reversal_score=scores.get("foreign_flow_reversal_score", 50.0),
        hynix_vwap_broken=hynix_vwap_broken,
        kospi200_futures_weak=bool(kospi200_futures_weak),
    )
