"""
regime_rules.py

6종 점수 + 플래그로부터 시장 유형(A~F)과 confidence_score(0~100)를 결정한다.
confidence_score < 60 이면 규정에 따라 F(NO_TRADE)로 강등된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_CONFIDENCE_THRESHOLD = 60.0

# 위험관리 우선순위 (동점 시 왼쪽이 우선)
REGIME_PRIORITY = ["E", "D", "B", "A", "C", "F"]

REGIME_LABELS = {
    "A": "강세 주도장",
    "B": "급락 후 반도체 반등장",
    "C": "지수 약세·테마 강세장",
    "D": "갭상승 실패장",
    "E": "급락 지속장",
    "F": "보합/혼조장",
}

REGIME_POLICY_MAP = {
    "A": "policy_leader_top3",
    "B": "policy_semiconductor_rebound",
    "C": "policy_gap_support",
    "D": "policy_no_trade",
    "E": "policy_inverse",
    "F": "policy_no_trade",
}


@dataclass
class RegimeDecision:
    regime: str
    confidence_score: float
    reasons: list = field(default_factory=list)
    raw_candidate: str = ""
    all_candidate_scores: dict = field(default_factory=dict)
    policy_name: str = ""

    def label(self) -> str:
        return REGIME_LABELS.get(self.regime, "알 수 없음")


def _candidate_scores(scores: dict, flags: dict, holiday_mode: bool = False) -> dict:
    candidates: dict[str, tuple] = {}

    # E: 급락 지속장 (안전 최우선)
    e_reasons = []
    if flags.get("kospi_or_kosdaq_below_neg1_5"):
        e_reasons.append("지수 -1.5% 이하 급락")
    if flags.get("usdkrw_rising"):
        e_reasons.append("환율 상승")
    if flags.get("hynix_samsung_weak"):
        e_reasons.append("반도체 대형주 동반 약세")
    candidates["E"] = (scores["risk_off_score"], e_reasons)

    # D: 갭상승 실패장
    d_reasons = []
    if flags.get("gap_up_then_broke_open"):
        d_reasons.append("시초 갭상승 후 시가 이탈")
    if flags.get("upper_wick_large"):
        d_reasons.append("대장주 윗꼬리 발생")
    candidates["D"] = (scores["gap_failure_score"], d_reasons)

    # B: 급락 후 반도체 반등장
    b_reasons = []
    if flags.get("prior_decline"):
        b_reasons.append("전일/최근2일 하이닉스·삼성전자 급락")
    if flags.get("recovered_from_0920_low"):
        b_reasons.append("09:20 저점 회복")
    if flags.get("us_semi_rebound_2of3"):
        b_reasons.append("마이크론/SOX/엔비디아 중 2개 이상 반등")
    candidates["B"] = (scores["semiconductor_rebound_score"], b_reasons)

    # A: 강세 주도장
    # holiday_mode(미국장 휴장/전일휴장)일 때는 국내 09:20 데이터 비중을 높이고
    # 미국 지표(및 last_session 기반 holiday_adjusted 점수) 비중을 낮춘다.
    us_ai = scores.get("us_ai_score", 50.0)
    if holiday_mode and "us_ai_score_holiday_adjusted" in scores:
        us_ai = (us_ai + scores["us_ai_score_holiday_adjusted"]) / 2
    if holiday_mode:
        a_score = us_ai * 0.15 + scores["korea_open_score"] * 0.55 + scores["leader_sector_score"] * 0.30
    else:
        a_score = us_ai * 0.30 + scores["korea_open_score"] * 0.40 + scores["leader_sector_score"] * 0.30
    a_reasons = []
    if flags.get("us_bullish"):
        a_reasons.append("나스닥/SOX 상승")
    if flags.get("korea_open_holds"):
        a_reasons.append("09:20 시초가 사수")
    if flags.get("leader_sector_clear"):
        a_reasons.append("업종/테마 Top3 뚜렷")
    if holiday_mode:
        a_reasons.append("휴장모드: 국내 09:20 흐름 가중치 상향")
    candidates["A"] = (round(min(100.0, a_score), 2), a_reasons)

    # C: 지수 약세·테마 강세장
    c_score = scores["leader_sector_score"] * 0.70 + (100 - scores["korea_open_score"]) * 0.30
    c_reasons = []
    if flags.get("leader_sector_clear"):
        c_reasons.append("특정 테마/섹터 거래대금 집중")
    if scores["korea_open_score"] < 55:
        c_reasons.append("지수는 약하거나 보합")
    candidates["C"] = (round(min(100.0, c_score), 2), c_reasons)

    # F: 보합/혼조장 (기본값)
    candidates["F"] = (50.0, ["뚜렷한 방향성 신호 없음"])

    return candidates


def decide_regime(scores: dict, flags: dict, cfg: dict = None, holiday_mode: bool = False) -> RegimeDecision:
    """점수/플래그로부터 최종 시장 유형을 결정한다."""
    cfg = cfg or {}
    threshold = float(cfg.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD))

    candidates = _candidate_scores(scores, flags, holiday_mode=holiday_mode)
    max_score = max(v[0] for v in candidates.values())

    best_regime = "F"
    for r in REGIME_PRIORITY:
        if abs(candidates[r][0] - max_score) < 1e-6:
            best_regime = r
            break

    best_score, reasons = candidates[best_regime]

    if best_score < threshold:
        return RegimeDecision(
            regime="F",
            confidence_score=round(best_score, 1),
            reasons=[f"신뢰도 부족(<{threshold:.0f}) → NO_TRADE"] + reasons,
            raw_candidate=best_regime,
            all_candidate_scores=candidates,
            policy_name=REGIME_POLICY_MAP["F"],
        )

    return RegimeDecision(
        regime=best_regime,
        confidence_score=round(best_score, 1),
        reasons=reasons or [f"{best_regime} 조건 충족"],
        raw_candidate=best_regime,
        all_candidate_scores=candidates,
        policy_name=REGIME_POLICY_MAP[best_regime],
    )
