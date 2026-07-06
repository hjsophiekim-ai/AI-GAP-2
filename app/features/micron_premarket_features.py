"""
micron_premarket_features.py — 마이크론(MU) 프리마켓 특징값 생성 모듈.

단순 등락률 외에 프리마켓 내 가격 모멘텀 방향성, VWAP 대비 위치,
거래량 패턴을 점수화하여 SK하이닉스 예측 입력 feature를 생성합니다.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def compute_micron_features(
    df_1min: Optional[pd.DataFrame],
    df_session_summary: Optional[pd.DataFrame] = None,
    current_price: Optional[dict] = None,
) -> dict:
    """
    MU 1분봉 데이터에서 예측용 feature 생성.

    Parameters
    ----------
    df_1min           : 1분봉 DataFrame (datetime, open, high, low, close, volume, session)
    df_session_summary: 세션별 요약 DataFrame (선택)
    current_price     : {price, open, high, low, volume} 현재가 dict (선택)

    Returns
    -------
    dict
        feature명 → 값 (계산 불가 시 None)
    """
    features: dict[str, Optional[float]] = {
        "micron_premarket_return":        None,
        "micron_premarket_open_to_now":   None,
        "micron_premarket_high_to_now":   None,
        "micron_premarket_low_to_now":    None,
        "micron_premarket_30m_momentum":  None,
        "micron_premarket_60m_momentum":  None,
        "micron_premarket_vwap":          None,
        "micron_premarket_volume_change": None,
        "micron_regular_return":          None,
        "micron_aftermarket_return":      None,
        "micron_session_strength_score":  None,
    }

    if df_1min is None or df_1min.empty:
        return features
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not required.issubset(set(df_1min.columns)):
        return features
    if "session" not in df_1min.columns:
        return features

    premarket = df_1min[df_1min["session"] == "premarket"].copy()
    regular   = df_1min[df_1min["session"] == "regular"].copy()
    after     = df_1min[df_1min["session"] == "aftermarket"].copy()

    # ── 프리마켓 feature ─────────────────────────────────────────────────────
    if not premarket.empty:
        pm_open  = float(premarket.iloc[0]["open"])
        pm_close = float(premarket.iloc[-1]["close"])
        pm_high  = float(premarket["high"].max())
        pm_low   = float(premarket["low"].min())
        now_px   = float((current_price or {}).get("price") or pm_close)

        if pm_open > 0:
            features["micron_premarket_return"] = round(
                (pm_close / pm_open - 1) * 100, 4
            )
        if pm_open > 0 and now_px > 0:
            features["micron_premarket_open_to_now"] = round(
                (now_px / pm_open - 1) * 100, 4
            )
        if pm_high > 0 and now_px > 0:
            features["micron_premarket_high_to_now"] = round(
                (now_px / pm_high - 1) * 100, 4
            )
        if pm_low > 0 and now_px > 0:
            features["micron_premarket_low_to_now"] = round(
                (now_px / pm_low - 1) * 100, 4
            )

        # 30분 모멘텀: 최근 30봉 기간 등락률
        if len(premarket) >= 2:
            tail30 = premarket.tail(min(30, len(premarket)))
            p30_o = float(tail30.iloc[0]["open"])
            p30_c = float(tail30.iloc[-1]["close"])
            if p30_o > 0:
                features["micron_premarket_30m_momentum"] = round(
                    (p30_c / p30_o - 1) * 100, 4
                )

        # 60분 모멘텀: 최근 60봉 기간 등락률
        if len(premarket) >= 2:
            tail60 = premarket.tail(min(60, len(premarket)))
            p60_o = float(tail60.iloc[0]["open"])
            p60_c = float(tail60.iloc[-1]["close"])
            if p60_o > 0:
                features["micron_premarket_60m_momentum"] = round(
                    (p60_c / p60_o - 1) * 100, 4
                )

        # VWAP = Σ(typical_price × volume) / Σ(volume)
        vol_sum = float(premarket["volume"].sum())
        if vol_sum > 0:
            typical = (premarket["high"] + premarket["low"] + premarket["close"]) / 3
            vwap = float((typical * premarket["volume"]).sum() / vol_sum)
            features["micron_premarket_vwap"] = round(vwap, 4)

        # 거래량 변화: 전반부 평균 vs 후반부 평균
        mid = len(premarket) // 2
        if mid > 0:
            vol_first  = float(premarket.iloc[:mid]["volume"].mean())
            vol_second = float(premarket.iloc[mid:]["volume"].mean())
            if vol_first > 0:
                features["micron_premarket_volume_change"] = round(
                    (vol_second / vol_first - 1) * 100, 4
                )

    # ── 정규장 feature ───────────────────────────────────────────────────────
    if not regular.empty:
        reg_o = float(regular.iloc[0]["open"])
        reg_c = float(regular.iloc[-1]["close"])
        if reg_o > 0:
            features["micron_regular_return"] = round(
                (reg_c / reg_o - 1) * 100, 4
            )

    # ── 애프터마켓 feature ───────────────────────────────────────────────────
    if not after.empty:
        af_o = float(after.iloc[0]["open"])
        af_c = float(after.iloc[-1]["close"])
        if af_o > 0:
            features["micron_aftermarket_return"] = round(
                (af_c / af_o - 1) * 100, 4
            )

    # ── 세션 강도 점수 ───────────────────────────────────────────────────────
    if not premarket.empty:
        features["micron_session_strength_score"] = _compute_strength_score(
            features, premarket
        )

    return features


def _compute_strength_score(
    features: dict,
    premarket: pd.DataFrame,
) -> float:
    """
    프리마켓 강도 점수 (0 ~ 100).

    채점 항목
    ---------
    - 등락률 크기 (±5% 기준, 최대 ±20점)
    - 30분/60분 모멘텀 방향 일치 여부 (±10점)
    - 모멘텀 꺾임 감지 (-5점)
    - 거래량 증가 여부 (±5점)
    - VWAP 대비 현재가 위치 (±5점)
    """
    score = 50.0

    ret    = features.get("micron_premarket_return")
    mom30  = features.get("micron_premarket_30m_momentum")
    mom60  = features.get("micron_premarket_60m_momentum")
    vol_chg = features.get("micron_premarket_volume_change")
    vwap   = features.get("micron_premarket_vwap")

    # 등락률 점수 (±5% → ±20점 선형)
    if ret is not None:
        score += min(max(ret * 4, -20), 20)

    # 모멘텀 방향 일치
    if mom30 is not None and mom60 is not None:
        if mom30 > 0 and mom60 > 0:
            score += 10
        elif mom30 < 0 and mom60 < 0:
            score -= 10
        # 60m 양수인데 30m이 절반 이하 → 모멘텀 꺾임
        if mom60 > 0 and mom30 < mom60 * 0.5:
            score -= 5

    # 거래량 변화
    if vol_chg is not None:
        if vol_chg > 20:
            score += 5
        elif vol_chg < -20:
            score -= 5

    # VWAP 위/아래
    if vwap is not None and not premarket.empty:
        last_close = float(premarket.iloc[-1]["close"])
        if last_close > vwap:
            score += 5
        elif last_close < vwap:
            score -= 5

    return round(min(max(score, 0.0), 100.0), 2)
