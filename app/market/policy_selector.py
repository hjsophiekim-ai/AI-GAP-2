"""
policy_selector.py

regime_router 결과 + 리스크 상태(RiskManager) + 현재 시각을 바탕으로
최종 실행 정책을 선택한다. 아래 조건 중 하나라도 해당되면 신규매수를 금지한다.

- confidence_score < 60
- market_risk_score(=risk_off_score) > 75  (인버스만 허용)
- 09:45 이후
- 2회 손절 발생 시 당일 신규매수 금지
- 하루 손실 -2% 도달 시 신규매수 금지
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.market.regime_rules import REGIME_POLICY_MAP

DEFAULT_ENTRY_CUTOFF_TIME = "09:45"
DEFAULT_RISK_OFF_HARD_LIMIT = 75.0


DEFAULT_HOLIDAY_CONFIDENCE_THRESHOLD = 70.0

# 실시간 장세 예측(market_prediction.py) 기반 안전장치 임계값.
# 여기(정책 선택 단계)는 1차 게이트이며, 실행 직전(app/execution) 2차 게이트가
# 더 보수적인 값(75)으로 한 번 더 확인한다 — 두 단계 임계값은 의도적으로 다르다.
DEFAULT_PREDICTED_DOWN_30M_BLOCK = 65.0
DEFAULT_MARKET_COLLAPSE_BLOCK = 70.0
DEFAULT_SEMICONDUCTOR_COLLAPSE_BLOCK = 70.0


@dataclass
class PolicySelection:
    policy_name: str
    regime: str
    confidence_score: float
    allow_new_entry: bool
    block_reasons: list = field(default_factory=list)
    forced_inverse_only: bool = False
    holiday_mode: bool = False
    manual_approval_only: bool = False
    watch_only: bool = False
    semiconductor_blocked: bool = False


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def select_policy(
    regime_result: dict,
    risk_state: dict = None,
    now_hm: str = None,
    policy_cfg: dict = None,
) -> PolicySelection:
    """
    regime_result: MarketRegimeRouter.determine_regime() 반환값.
    risk_state: RiskManager.get_state() 형태
        {"consecutive_losses": int, "daily_pnl_pct": float, "trade_count": int}
    """
    now_hm = now_hm or _now_hm()
    policy_cfg = policy_cfg or {}
    risk_state = risk_state or {}

    regime = regime_result.get("regime", "F")
    confidence = regime_result.get("confidence_score", 0.0)
    risk_off_score = regime_result.get("scores", {}).get("risk_off_score", 0.0)
    base_policy = regime_result.get("policy_name") or REGIME_POLICY_MAP.get(regime, "policy_no_trade")

    entry_cutoff = policy_cfg.get("entry_cutoff_time", DEFAULT_ENTRY_CUTOFF_TIME)
    confidence_threshold = policy_cfg.get("confidence_threshold", 60)
    risk_off_hard_limit = policy_cfg.get("risk_off_hard_limit", DEFAULT_RISK_OFF_HARD_LIMIT)
    max_consecutive_losses = policy_cfg.get("stop_after_consecutive_losses", 2)
    max_daily_loss_pct = policy_cfg.get("max_daily_loss_pct", -2.0)

    block_reasons: list[str] = []
    forced_inverse_only = False

    if confidence < confidence_threshold:
        block_reasons.append(f"confidence_score({confidence:.1f}) < {confidence_threshold}")

    if risk_off_score > risk_off_hard_limit:
        forced_inverse_only = True
        block_reasons.append(f"market_risk_score({risk_off_score:.1f}) > {risk_off_hard_limit}")

    if now_hm >= entry_cutoff:
        block_reasons.append(f"신규매수 가능시간 종료({entry_cutoff} 이후)")

    consecutive_losses = risk_state.get("consecutive_losses", 0)
    if consecutive_losses >= max_consecutive_losses:
        block_reasons.append(f"당일 연속 손절 {consecutive_losses}회 → 신규매수 금지")

    daily_pnl_pct = risk_state.get("daily_pnl_pct", 0.0)
    if daily_pnl_pct <= max_daily_loss_pct:
        block_reasons.append(f"당일 손실 {daily_pnl_pct:.2f}% 도달 → 신규매수 금지")

    if base_policy == "policy_no_trade":
        block_reasons.append(f"{regime} 유형: 정책상 신규매수 금지")

    # ── 실시간 예측 기반 안전장치 (market_prediction.py 연동) ────────────────
    predicted_down_30m = ((regime_result.get("predictions") or {}).get("30m") or {}).get("probability_down")
    predicted_regime_30m = regime_result.get("predicted_regime_30m")
    market_collapse_score = regime_result.get("market_collapse_score")
    semiconductor_collapse_score = regime_result.get("semiconductor_collapse_score")

    predicted_down_threshold = policy_cfg.get("predicted_down_30m_block_threshold", DEFAULT_PREDICTED_DOWN_30M_BLOCK)
    market_collapse_threshold = policy_cfg.get("market_collapse_block_threshold", DEFAULT_MARKET_COLLAPSE_BLOCK)
    semi_collapse_threshold = policy_cfg.get("semiconductor_collapse_block_threshold", DEFAULT_SEMICONDUCTOR_COLLAPSE_BLOCK)

    if predicted_down_30m is not None and predicted_down_30m >= predicted_down_threshold:
        block_reasons.append(
            f"30분 후 하락확률 {predicted_down_30m:.0f}% >= {predicted_down_threshold:.0f} → 신규매수 금지"
        )

    if market_collapse_score is not None and market_collapse_score >= market_collapse_threshold:
        block_reasons.append(
            f"market_collapse_score {market_collapse_score:.0f} >= {market_collapse_threshold:.0f} → 신규매수 금지"
        )

    semiconductor_blocked = False
    if (
        semiconductor_collapse_score is not None
        and semiconductor_collapse_score >= semi_collapse_threshold
        and base_policy == "policy_semiconductor_rebound"
    ):
        semiconductor_blocked = True
        block_reasons.append(
            f"semiconductor_collapse_score {semiconductor_collapse_score:.0f} >= {semi_collapse_threshold:.0f} "
            "→ 반도체 후보 매수 금지"
        )

    allow_new_entry = len(block_reasons) == 0

    policy_name = base_policy
    if forced_inverse_only:
        policy_name = "policy_inverse" if policy_cfg.get("allow_inverse", False) else "policy_no_trade"
        allow_new_entry = policy_name == "policy_inverse"
    elif not allow_new_entry:
        policy_name = "policy_no_trade"

    # ── Holiday Mode 규칙 (전량 차단이 아니라 "자동매수만" 금지) ─────────────
    # 여기서부터의 판단은 allow_new_entry(신규 후보 생성 허용 여부)에는
    # 영향을 주지 않는다 — 후보는 계속 생성하되 자동매수만 금지한다.
    holiday_mode = bool(regime_result.get("holiday_mode", False))
    manual_approval_only = False
    if holiday_mode and allow_new_entry:
        holiday_confidence_threshold = policy_cfg.get(
            "holiday_confidence_threshold", DEFAULT_HOLIDAY_CONFIDENCE_THRESHOLD
        )
        if confidence < holiday_confidence_threshold:
            manual_approval_only = True
            block_reasons.append(
                f"휴장모드: confidence_score({confidence:.1f}) < {holiday_confidence_threshold} "
                "→ 자동매수 금지, 수동승인 후보만 표시"
            )
        if policy_name == "policy_gap_support":
            block_reasons.append("휴장모드: GAP 보조정책(policy_gap_support) 비중 축소 — 수동승인 권장")
            manual_approval_only = True

    # ── 현재 C(또는 A/B)인데 30분 후 D/E 전환이 예상되면 자동매수를 멈추고
    # WATCH_ONLY/MANUAL_ONLY로 강등한다 (신규 후보 생성 자체는 막지 않는다 —
    # "C타입이라고 계속 테마/GAP 매수를 허용"하는 것을 방지하는 핵심 규칙).
    watch_only = False
    if regime in ("A", "B", "C") and predicted_regime_30m in ("D", "E") and allow_new_entry:
        watch_only = True
        manual_approval_only = True
        block_reasons.append(
            f"현재 {regime} 유형이지만 30분 후 {predicted_regime_30m} 전환 예상 "
            "→ 신규 후보는 WATCH_ONLY/수동승인만 허용(자동매수 금지)"
        )

    logger.info(
        "[PolicySelector] regime=%s policy=%s allow_new_entry=%s holiday_mode=%s "
        "manual_approval_only=%s watch_only=%s semiconductor_blocked=%s reasons=%s",
        regime, policy_name, allow_new_entry, holiday_mode, manual_approval_only,
        watch_only, semiconductor_blocked, block_reasons,
    )

    return PolicySelection(
        policy_name=policy_name,
        regime=regime,
        confidence_score=confidence,
        allow_new_entry=allow_new_entry,
        block_reasons=block_reasons,
        forced_inverse_only=forced_inverse_only,
        holiday_mode=holiday_mode,
        manual_approval_only=manual_approval_only,
        watch_only=watch_only,
        semiconductor_blocked=semiconductor_blocked,
    )
