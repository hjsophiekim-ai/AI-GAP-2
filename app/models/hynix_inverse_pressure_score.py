"""
hynix_inverse_pressure_score.py — 인버스(0197X0) 압력점수 (calculate_inverse_pressure_score).

하이닉스가 하락할 가능성을 0~100 점수로 계산한다(70+ 인버스강매수,
50~69 인버스매수, 30~49 보류, 30 미만 하이닉스매수 우위). 하이닉스 기술점수/
모멘텀점수/마이크론점수/kospilab 참고가는 각각의 기존(신규) 모듈 결과를 그대로
재사용하고, 여기서는 "하락 압력" 관점의 재해석/가중합만 수행한다.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from app.logger import logger

INVERSE_STRONG_BUY = "INVERSE_STRONG_BUY"
INVERSE_BUY = "INVERSE_BUY"
HOLD = "HOLD"
HYNIX_BUY_FAVORED = "HYNIX_BUY_FAVORED"

_BASE_SCORE = 15.0


def _tier(score: float) -> str:
    if score >= 70:
        return INVERSE_STRONG_BUY
    if score >= 50:
        return INVERSE_BUY
    if score >= 30:
        return HOLD
    return HYNIX_BUY_FAVORED


def calculate_inverse_pressure_score(
    tech_result: dict,
    momentum_result: dict,
    micron_result: dict,
    kospilab_result: Optional[dict] = None,
    df_1min: Optional[pd.DataFrame] = None,
    current_price: Optional[float] = None,
    investor_flow: Optional[dict] = None,
) -> dict:
    """인버스 압력점수 계산.

    Parameters
    ----------
    tech_result      : calculate_hynix_technical_score() 결과
    momentum_result   : calculate_intraday_momentum_score() 결과
    micron_result     : calculate_existing_micron_score() 결과
    kospilab_result   : app.data_sources.kospilab_scraper.fetch_kospilab_data() 결과 (선택)
    df_1min           : 하이닉스 1분봉 (3분봉 저점 연속하락 판정용, 선택)
    current_price     : 하이닉스 현재가 (VWAP 비교용, 없으면 df_1min 마지막 종가 사용)
    investor_flow     : {"foreign_net_buy":.., "institution_net_buy":..} (없으면 해당 항목 제외)
    """
    points: list[tuple[float, str]] = []
    warnings: list[str] = []

    detail = (tech_result or {}).get("detail", {}) or {}
    momentum_detail = (momentum_result or {}).get("detail", {}) or {}

    # ── 200일선 이탈 ─────────────────────────────────────────────────────────
    try:
        ma200 = detail.get("ma200_position_pct")
        if ma200 is not None and ma200 < 0:
            points.append((15.0, f"200일 이동평균선 이탈({ma200:.2f}%)"))
    except Exception as exc:
        warnings.append(f"200일선 판정 실패: {exc}")

    # ── VWAP 하회 ────────────────────────────────────────────────────────────
    try:
        vwap = detail.get("vwap")
        price = current_price
        if price is None and df_1min is not None and not df_1min.empty:
            price = float(df_1min.sort_values("datetime")["close"].iloc[-1])
        if vwap is not None and price is not None and price < vwap:
            points.append((12.0, "현재가 VWAP 하회"))
    except Exception as exc:
        warnings.append(f"VWAP 판정 실패: {exc}")

    # ── 3분봉 저점 연속 하락 ─────────────────────────────────────────────────
    try:
        if df_1min is not None and not df_1min.empty:
            from app.data_sources.auto_market_collector import _resample_minutes

            df_3min = _resample_minutes(df_1min, 3)
            if df_3min is not None and len(df_3min) >= 3:
                lows = df_3min["low"].tail(3).tolist()
                if lows[0] > lows[1] > lows[2]:
                    points.append((12.0, "3분봉 저점 연속 하락"))
    except Exception as exc:
        warnings.append(f"3분봉 저점 판정 실패: {exc}")

    # ── MACD 하락확산 ────────────────────────────────────────────────────────
    try:
        hist = detail.get("macd_histogram")
        if hist is not None and hist < 0:
            points.append((10.0, f"MACD 히스토그램 음수(하락확산 가능, {hist:.2f})"))
    except Exception as exc:
        warnings.append(f"MACD 하락확산 판정 실패: {exc}")

    # ── RSI 45 이하 ──────────────────────────────────────────────────────────
    try:
        rsi = detail.get("rsi_14")
        if rsi is not None and rsi <= 45:
            points.append((10.0, f"RSI(14) {rsi:.1f} — 45 이하"))
    except Exception as exc:
        warnings.append(f"RSI 판정 실패: {exc}")

    # ── 마이크론 점수 약세 ───────────────────────────────────────────────────
    try:
        micron_score = (micron_result or {}).get("existing_micron_score")
        if micron_score is not None and micron_score < 45:
            weight = min(15.0, (45.0 - micron_score) / 45.0 * 15.0)
            points.append((round(weight, 2), f"마이크론 점수 약세({micron_score:.1f})"))
    except Exception as exc:
        warnings.append(f"마이크론 점수 판정 실패: {exc}")

    # ── kospilab 하이닉스 참고가 하락세 ──────────────────────────────────────
    try:
        kospilab_return = (kospilab_result or {}).get("hynix_reference_return")
        if kospilab_return is not None and kospilab_return < 0:
            weight = min(10.0, abs(kospilab_return) / 3.0 * 10.0)
            points.append((round(weight, 2), f"코스피랩 하이닉스 참고가 하락세({kospilab_return:+.2f}%)"))
    except Exception as exc:
        warnings.append(f"kospilab 판정 실패: {exc}")

    # ── 거래량 동반 하락 ─────────────────────────────────────────────────────
    try:
        vol_chg = detail.get("volume_change_pct")
        return_3d = detail.get("return_3d_pct")
        if vol_chg is not None and vol_chg > 0 and return_3d is not None and return_3d < 0:
            points.append((8.0, "거래량 증가 동반 하락"))
    except Exception as exc:
        warnings.append(f"거래량 판정 실패: {exc}")

    # ── 장중 저점 갱신 ───────────────────────────────────────────────────────
    try:
        if detail.get("intraday_new_low") or momentum_detail.get("new_low_recent"):
            points.append((8.0, "장중 저점 갱신"))
    except Exception as exc:
        warnings.append(f"장중 저점 판정 실패: {exc}")

    # ── 외국인/기관 수급 (있으면 반영, 없으면 제외) ──────────────────────────
    try:
        if investor_flow:
            foreign = investor_flow.get("foreign_net_buy")
            institution = investor_flow.get("institution_net_buy")
            net = (foreign or 0) + (institution or 0)
            if (foreign is not None or institution is not None) and net < 0:
                points.append((10.0, f"외국인/기관 순매도(합계 {net:+,.0f}주)"))
    except Exception as exc:
        warnings.append(f"수급 판정 실패: {exc}")

    raw_sum = sum(p for p, _ in points)
    score = round(max(0.0, min(100.0, _BASE_SCORE + raw_sum)), 2)
    tier = _tier(score)

    points.sort(key=lambda x: abs(x[0]), reverse=True)
    reason_top5 = [f"+{p:.1f}점: {desc}" for p, desc in points[:5]]

    return {
        "inverse_pressure_score": score,
        "inverse_pressure_tier": tier,
        "raw_point_sum": round(raw_sum, 2),
        "reason_top5": reason_top5,
        "warnings": warnings,
    }
