"""
hynix_error_analyzer.py — SK하이닉스 예측 오차 분석 모듈.

장 종료 후 실제 가격과 예측값을 비교하고
"왜 빗나갔는지" 한글 설명과 다음 예측 가중치 조정 제안을 반환합니다.
"""

from __future__ import annotations

from typing import Optional


def analyze_prediction_error(
    # 예측값
    predicted_open: Optional[float],
    predicted_high: Optional[float],
    predicted_low: Optional[float],
    predicted_close: Optional[float],
    predicted_tomorrow_return: Optional[float],
    predicted_day3_return: Optional[float],
    # 실제값
    actual_open: float,
    actual_high: float,
    actual_low: float,
    actual_close: float,
    actual_tomorrow_close: Optional[float] = None,
    actual_day3_close: Optional[float] = None,
    # 당시 입력 신호 (원인 분석용)
    micron_signal: Optional[float] = None,
    kospilab_signal: Optional[float] = None,
    sox_signal: Optional[float] = None,
    usd_krw_change_pct: Optional[float] = None,
    hynix_prev_return_pct: Optional[float] = None,
    micron_strength_score: Optional[float] = None,
) -> dict:
    """
    예측 오차 분석.

    Returns
    -------
    dict
        {
          open_error_pct, high_error_pct, low_error_pct, close_error_pct,
          tomorrow_direction_correct, day3_direction_correct,
          error_reasons: [str, ...],
          weight_suggestions: {지표명: 조정방향, ...},
          summary_text: str
        }
    """
    result: dict = {
        "open_error_pct":          None,
        "high_error_pct":          None,
        "low_error_pct":           None,
        "close_error_pct":         None,
        "tomorrow_direction_correct": None,
        "day3_direction_correct":     None,
        "error_reasons":           [],
        "weight_suggestions":      {},
        "summary_text":            "",
    }

    reasons: list[str] = []
    weight_adj: dict[str, str] = {}

    # ── 가격 오차 계산 ───────────────────────────────────────────────────────
    def _pct_error(pred: Optional[float], actual: float) -> Optional[float]:
        if pred is None or actual == 0:
            return None
        return round((pred - actual) / actual * 100, 2)

    result["open_error_pct"]  = _pct_error(predicted_open, actual_open)
    result["high_error_pct"]  = _pct_error(predicted_high, actual_high)
    result["low_error_pct"]   = _pct_error(predicted_low, actual_low)
    result["close_error_pct"] = _pct_error(predicted_close, actual_close)

    close_err = result["close_error_pct"] or 0.0

    # ── 방향 적중 여부 ───────────────────────────────────────────────────────
    if actual_tomorrow_close is not None and predicted_tomorrow_return is not None:
        actual_dir  = actual_tomorrow_close >= actual_close
        predict_dir = predicted_tomorrow_return >= 0
        result["tomorrow_direction_correct"] = actual_dir == predict_dir

    if actual_day3_close is not None and predicted_day3_return is not None:
        actual_dir  = actual_day3_close >= actual_close
        predict_dir = predicted_day3_return >= 0
        result["day3_direction_correct"] = actual_dir == predict_dir

    # ── 원인 분석 ─────────────────────────────────────────────────────────────
    over_pred = close_err > 2.0   # 과대 예측
    under_pred = close_err < -2.0  # 과소 예측

    if abs(close_err) > 2.0:
        # 마이크론 강했지만 실제는 약함
        if micron_signal is not None and micron_signal > 0.3 and close_err < -1.0:
            reasons.append(
                "마이크론 프리마켓이 강했으나 실제 하이닉스는 약했습니다. "
                "마이크론-하이닉스 연동이 이날은 낮았을 가능성이 있습니다."
            )
            weight_adj["micron_premarket_aftermarket"] = "감소 고려"

        # 코스피랩 신호 vs 실제 방향 불일치
        if kospilab_signal is not None and (
            (kospilab_signal > 0 and close_err < -2.0)
            or (kospilab_signal < 0 and close_err > 2.0)
        ):
            reasons.append(
                "코스피랩 예상 방향과 실제 결과가 반대였습니다. "
                "코스피랩 입력값의 정확도를 재확인해주세요."
            )
            weight_adj["kospilab_expected_price"] = "감소 고려"

        # SOX 신호
        if sox_signal is not None and abs(sox_signal) > 0.5 and close_err * sox_signal < 0:
            reasons.append(
                "SOX 지수 방향이 예측과 달랐습니다. "
                "반도체 지수가 독립적으로 움직인 날입니다."
            )
            weight_adj["sox_index"] = "감소 고려"

        # 환율 급등
        if usd_krw_change_pct is not None and usd_krw_change_pct > 1.0 and close_err < -1.0:
            reasons.append(
                f"USD/KRW 환율이 {usd_krw_change_pct:.1f}% 급등하여 "
                "외국인 수급이 약해졌을 가능성이 있습니다."
            )
            weight_adj["usd_krw"] = "증가 고려"

        # 전일 과열
        if hynix_prev_return_pct is not None and hynix_prev_return_pct > 5.0 and close_err < -1.0:
            reasons.append(
                f"전일 SK하이닉스가 {hynix_prev_return_pct:.1f}% 급등 후 "
                "차익실현 매물이 나왔을 수 있습니다."
            )
            weight_adj["hynix_momentum_volume"] = "감소 고려 (과열 후 조정 패턴)"

        # 마이크론 장 후반 꺾임
        if micron_strength_score is not None and micron_strength_score < 40 and over_pred:
            reasons.append(
                "마이크론 프리마켓 강도 점수가 낮았음에도 과대 예측했습니다. "
                "프리마켓 모멘텀이 장 후반 꺾인 경우일 수 있습니다."
            )

    if not reasons:
        if abs(close_err) <= 2.0:
            reasons.append("예측이 비교적 정확했습니다 (종가 오차 2% 이내).")
        else:
            reasons.append(
                "개별 공시, 기관/외국인 수급, 또는 예상치 못한 시장 이벤트가 "
                "예측 정확도에 영향을 미쳤을 수 있습니다."
            )

    result["error_reasons"]      = reasons
    result["weight_suggestions"] = weight_adj
    result["summary_text"]       = _build_summary(result, close_err)

    return result


def _build_summary(result: dict, close_err: float) -> str:
    """오차 분석 요약 한글 텍스트 생성."""
    lines = []

    if result["close_error_pct"] is not None:
        direction = "과대" if close_err > 0 else "과소"
        lines.append(
            f"▶ 종가 예측 오차: {close_err:+.2f}% ({direction} 예측)"
        )

    tomorrow_ok = result.get("tomorrow_direction_correct")
    if tomorrow_ok is not None:
        lines.append(
            f"▶ 내일 방향 적중: {'✅ 적중' if tomorrow_ok else '❌ 빗나감'}"
        )

    day3_ok = result.get("day3_direction_correct")
    if day3_ok is not None:
        lines.append(
            f"▶ 3일후 방향 적중: {'✅ 적중' if day3_ok else '❌ 빗나감'}"
        )

    if result["error_reasons"]:
        lines.append("\n원인 분석:")
        for i, r in enumerate(result["error_reasons"], 1):
            lines.append(f"  {i}. {r}")

    if result["weight_suggestions"]:
        lines.append("\n가중치 조정 제안:")
        for k, v in result["weight_suggestions"].items():
            lines.append(f"  • {k}: {v}")

    return "\n".join(lines)
