"""
hynix_swing_explainer.py — SK하이닉스 스윙 판단 한글 설명 생성 모듈.

swing_flag 결과와 개별 지표 신호를 받아
투자자가 이해할 수 있는 한글 판단 이유를 자동 생성합니다.
"""

from __future__ import annotations

from typing import Optional

from app.models.hynix_swing_flag import (
    STRONG_BUY, BUY, WAIT_BUY, NEUTRAL, TAKE_PROFIT, SELL, STRONG_SELL,
    FLAG_LABELS, SELL_RATIO_LABELS,
)


def generate_swing_explanation(
    swing_result: dict,
    micron_features: dict,
    tech_indicators: Optional[dict] = None,
    kospilab_return: Optional[float] = None,
    sox_return: Optional[float] = None,
    usd_krw_change: Optional[float] = None,
) -> str:
    """
    스윙 판단 한글 설명 생성.

    Parameters
    ----------
    swing_result    : evaluate_swing_flag() 반환 dict
    micron_features : compute_micron_features() 반환 dict
    tech_indicators : 기술적 지표 dict (선택)
    kospilab_return : 코스피랩 예상등락률
    sox_return      : SOX 등락률
    usd_krw_change  : 환율 변화율

    Returns
    -------
    str
        판단 이유 한글 설명 (여러 문장)
    """
    ti        = tech_indicators or {}
    flag      = swing_result.get("swing_flag", NEUTRAL)
    score     = swing_result.get("swing_score", 50.0)
    comp      = swing_result.get("component_scores", {})
    bottom_p  = swing_result.get("bottom_probability", 0.0)
    top_p     = swing_result.get("top_probability", 0.0)

    parts: list[str] = []

    # ── 마이크론 신호 설명 ────────────────────────────────────────────────────
    pm_ret    = micron_features.get("micron_premarket_return")
    mom30     = micron_features.get("micron_premarket_30m_momentum")
    mom60     = micron_features.get("micron_premarket_60m_momentum")
    strength  = micron_features.get("micron_session_strength_score") or 50.0
    af_ret    = micron_features.get("micron_aftermarket_return")

    if pm_ret is not None:
        if pm_ret >= 2.0:
            mu_text = f"마이크론 프리마켓이 {pm_ret:+.1f}%로 강세입니다."
        elif pm_ret <= -2.0:
            mu_text = f"마이크론 프리마켓이 {pm_ret:+.1f}%로 약세입니다."
        else:
            mu_text = f"마이크론 프리마켓이 {pm_ret:+.1f}%로 소폭 변동 중입니다."
        parts.append(mu_text)

    # 모멘텀 꺾임 감지
    if mom30 is not None and mom60 is not None:
        if mom60 > 0 and mom30 < mom60 * 0.5:
            parts.append("다만 마이크론 60분 모멘텀 대비 30분 모멘텀이 꺾여, 프리마켓 후반 힘이 빠지고 있습니다.")
        elif mom30 > 0 and mom60 > 0:
            parts.append("마이크론 단기·중기 모멘텀 모두 상승 방향으로 일치합니다.")
        elif mom30 < 0 and mom60 < 0:
            parts.append("마이크론 단기·중기 모멘텀 모두 하락 방향으로 일치합니다.")

    if af_ret is not None and abs(af_ret) >= 1.5:
        direction = "강세" if af_ret > 0 else "약세"
        parts.append(f"마이크론 애프터마켓도 {af_ret:+.1f}%로 {direction}를 보이고 있습니다.")

    # ── 코스피랩 설명 ─────────────────────────────────────────────────────────
    if kospilab_return is not None:
        if kospilab_return >= 1.0:
            parts.append(f"코스피랩 예상등락률이 {kospilab_return:+.2f}%로 우호적입니다.")
        elif kospilab_return <= -1.0:
            parts.append(f"코스피랩 예상등락률이 {kospilab_return:+.2f}%로 부정적입니다.")

    # ── 기술적 지표 설명 ──────────────────────────────────────────────────────
    rsi = ti.get("rsi_14")
    if rsi is not None:
        if rsi <= 30:
            parts.append(f"RSI {rsi:.1f}로 과매도 구간에 진입해 기술적 반등 가능성이 있습니다.")
        elif rsi >= 70:
            parts.append(f"RSI {rsi:.1f}로 과매수 구간에 진입했습니다. 단기 고점 가능성에 주의하세요.")
        elif 40 <= rsi <= 60:
            parts.append(f"RSI {rsi:.1f}로 중립 구간에 있습니다.")

    cross = ti.get("macd_signal_cross")
    if cross == 1:
        parts.append("MACD 골든크로스가 발생했습니다. 단기 상승 모멘텀 신호입니다.")
    elif cross == -1:
        parts.append("MACD 데드크로스가 발생했습니다. 단기 하락 압력이 있습니다.")

    from_high = ti.get("from_20d_high_pct")
    from_low  = ti.get("from_20d_low_pct")
    if from_high is not None and from_high <= -8:
        parts.append(f"최근 20일 고점 대비 {from_high:.1f}% 하락하여 단기 저점 매수 구간으로 진입했습니다.")
    elif from_high is not None and from_high >= -2:
        parts.append(f"최근 20일 고점 부근({from_high:.1f}%)에 위치해 상단 저항이 예상됩니다.")

    if from_low is not None and from_low >= 20:
        parts.append(f"최근 20일 저점 대비 {from_low:.1f}% 급등해 단기 과열 가능성이 있습니다.")
    elif from_low is not None and from_low <= 3:
        parts.append(f"최근 20일 저점 근처({from_low:.1f}%)로 지지선 부근입니다.")

    bb = ti.get("bollinger_pct")
    if bb is not None:
        if bb <= 15:
            parts.append(f"볼린저밴드 하단 근접({bb:.0f}%)으로 기술적 반등 조건이 충족됩니다.")
        elif bb >= 85:
            parts.append(f"볼린저밴드 상단 근접({bb:.0f}%)으로 단기 고점 가능성이 높습니다.")

    candle = ti.get("prev_candle_type")
    if candle == 1:
        parts.append("전일 장대양봉으로 마감해 매수 의지가 확인됩니다.")
    elif candle == -1:
        parts.append("전일 장대음봉으로 마감해 매도 압력이 강했습니다.")

    # 최근 수익률 패턴
    r3d = ti.get("return_3d_pct")
    r5d = ti.get("return_5d_pct")
    if r5d is not None and r5d >= 8:
        parts.append(f"최근 5일간 {r5d:.1f}% 급등해 단기 과열 신호가 있습니다. 즉시 추격매수보다는 눌림목 매수가 유리합니다.")
    elif r3d is not None and r3d <= -5:
        parts.append(f"최근 3일간 {r3d:.1f}% 하락해 단기 낙폭 과대 구간에 진입했습니다.")

    # ── SOX/환율 설명 ─────────────────────────────────────────────────────────
    if sox_return is not None and abs(sox_return) >= 1.5:
        sox_dir = "상승" if sox_return > 0 else "하락"
        parts.append(f"SOX 반도체 지수가 {sox_return:+.1f}%로 {sox_dir}해 반도체 섹터 분위기가 {'우호적' if sox_return > 0 else '부정적'}입니다.")

    if usd_krw_change is not None and usd_krw_change >= 1.0:
        parts.append(f"USD/KRW 환율이 {usd_krw_change:+.1f}% 급등해 외국인 수급 약화 가능성이 있습니다.")

    # ── 최종 액션 요약 ────────────────────────────────────────────────────────
    action_text = _action_summary(flag, score, bottom_p, top_p)
    parts.append(action_text)

    return " ".join(parts) if parts else "데이터 부족으로 판단 설명을 생성할 수 없습니다."


def _action_summary(
    flag: str,
    score: float,
    bottom_p: float,
    top_p: float,
) -> str:
    """플래그에 따른 최종 액션 한줄 요약."""
    label = FLAG_LABELS.get(flag, flag)
    if flag == STRONG_BUY:
        return f"종합 판단: {label} ({score:.0f}점) — 단기 저점 확률 {bottom_p:.0f}%로 매수 적기로 판단됩니다."
    elif flag == BUY:
        return f"종합 판단: {label} ({score:.0f}점) — 분할 매수 접근이 유효합니다."
    elif flag == WAIT_BUY:
        return f"종합 판단: {label} ({score:.0f}점) — 추가 눌림목을 확인 후 매수를 고려하세요."
    elif flag == NEUTRAL:
        return f"종합 판단: {label} ({score:.0f}점) — 명확한 방향성이 없습니다. 관망을 권장합니다."
    elif flag == TAKE_PROFIT:
        ratio = SELL_RATIO_LABELS[TAKE_PROFIT]
        return (
            f"종합 판단: {label} ({score:.0f}점) — 단기 고점 확률 {top_p:.0f}%로 "
            f"{ratio}만 매도하고 나머지는 보유하는 분할매도를 고려하세요(전량매도 아님)."
        )
    elif flag == SELL:
        ratio = SELL_RATIO_LABELS[SELL]
        return f"종합 판단: {label} ({score:.0f}점) — 추가 하락 리스크가 있어 {ratio} 매도 후 관망을 권장합니다."
    elif flag == STRONG_SELL:
        ratio = SELL_RATIO_LABELS[STRONG_SELL]
        return (
            f"종합 판단: {label} ({score:.0f}점) — 단기 고점 확률 {top_p:.0f}%로 "
            f"{ratio} 즉시 손절/매도를 검토하세요."
        )
    return f"종합 판단: {label} ({score:.0f}점)"
