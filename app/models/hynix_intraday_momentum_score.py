"""
hynix_intraday_momentum_score.py — 장중 모멘텀 점수 (calculate_intraday_momentum_score).

`hynix_technical_score`의 "1·3·5분 전부상승/하락" 항목보다 더 세밀하게,
연속 방향성(streak), 고저 돌파 타이밍, 급등/급락 속도를 점수화한다.
1분봉만 있으면 되고, 3/5분봉은 내부에서 리샘플해 사용한다(별도 캐시 불필요).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from app.logger import logger

_STREAK_LOOKBACK = 4
_SURGE_WINDOW_MIN = 3
_SURGE_THRESHOLD_PCT = 0.5


def _resample(df_1min: pd.DataFrame, minutes: int) -> Optional[pd.DataFrame]:
    try:
        from app.data_sources.auto_market_collector import _resample_minutes

        return _resample_minutes(df_1min, minutes)
    except Exception as exc:
        logger.debug("[IntradayMomentum] 리샘플 실패(%s분): %s", minutes, exc)
        return None


def _cum_return_pct(df: Optional[pd.DataFrame], lookback_bars: int) -> Optional[float]:
    if df is None or len(df) < 2:
        return None
    work = df.tail(min(lookback_bars, len(df)))
    try:
        first_open = float(work.iloc[0]["open"])
        last_close = float(work.iloc[-1]["close"])
    except Exception:
        return None
    if first_open <= 0:
        return None
    return (last_close / first_open - 1.0) * 100.0


def _direction_streak(df_1min: pd.DataFrame, lookback: int = _STREAK_LOOKBACK) -> Optional[str]:
    if df_1min is None or len(df_1min) < lookback:
        return None
    tail = df_1min.tail(lookback)
    directions = []
    for _, row in tail.iterrows():
        try:
            if float(row["close"]) > float(row["open"]):
                directions.append("up")
            elif float(row["close"]) < float(row["open"]):
                directions.append("down")
            else:
                directions.append("flat")
        except Exception:
            return None
    if all(d == "up" for d in directions):
        return "up"
    if all(d == "down" for d in directions):
        return "down"
    return "mixed"


def _breakout_recency(df_1min: pd.DataFrame, within_bars: int = 2) -> dict:
    """최근 within_bars봉 내에 당일 신고가/신저가가 발생했는지."""
    result = {"new_high_recent": False, "new_low_recent": False}
    try:
        today = pd.Timestamp.now().normalize()
        work = df_1min.copy()
        work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
        work = work.dropna(subset=["datetime"])
        intraday = work[work["datetime"] >= today]
        if intraday.empty:
            intraday = work
        if len(intraday) < 2:
            return result
        running_high = intraday["high"].cummax()
        running_low = intraday["low"].cummin()
        is_new_high = intraday["high"] >= running_high
        is_new_low = intraday["low"] <= running_low
        result["new_high_recent"] = bool(is_new_high.tail(within_bars).any())
        result["new_low_recent"] = bool(is_new_low.tail(within_bars).any())
    except Exception:
        pass
    return result


def calculate_intraday_momentum_score(df_1min: Optional[pd.DataFrame]) -> dict:
    """장중 모멘텀 점수(0~100, 50=중립) + 판단 사유."""
    points: list[tuple[float, str]] = []
    warnings: list[str] = []
    detail: dict = {}

    if df_1min is None or df_1min.empty or len(df_1min) < 3:
        return {
            "intraday_momentum_score": 50.0,
            "reason_top5": [],
            "warnings": ["1분봉 데이터 부족 — 중립값(50) 반환"],
            "detail": detail,
        }

    df_1min = df_1min.sort_values("datetime").reset_index(drop=True)

    try:
        df_3min = _resample(df_1min, 3)
        df_5min = _resample(df_1min, 5)

        ret_5m_bars = _cum_return_pct(df_1min, 5)
        ret_3m_bars = _cum_return_pct(df_3min, 3) if df_3min is not None else None
        detail["return_last_5_1min_bars_pct"] = round(ret_5m_bars, 3) if ret_5m_bars is not None else None
        detail["return_last_3_3min_bars_pct"] = round(ret_3m_bars, 3) if ret_3m_bars is not None else None

        if ret_5m_bars is not None:
            points.append((max(-20.0, min(20.0, ret_5m_bars / 0.8 * 20.0)), f"최근 5개 1분봉 누적수익률 {ret_5m_bars:+.2f}%"))
        if ret_3m_bars is not None:
            points.append((max(-15.0, min(15.0, ret_3m_bars / 1.2 * 15.0)), f"최근 3개 3분봉 누적수익률 {ret_3m_bars:+.2f}%"))
    except Exception as exc:
        warnings.append(f"누적수익률 모멘텀 계산 실패: {exc}")

    try:
        streak = _direction_streak(df_1min)
        detail["direction_streak"] = streak
        if streak == "up":
            points.append((8.0, f"최근 {_STREAK_LOOKBACK}개 1분봉 연속 상승"))
        elif streak == "down":
            points.append((-8.0, f"최근 {_STREAK_LOOKBACK}개 1분봉 연속 하락"))
    except Exception as exc:
        warnings.append(f"방향 지속성(streak) 계산 실패: {exc}")

    try:
        breakout = _breakout_recency(df_1min)
        detail.update(breakout)
        if breakout["new_high_recent"]:
            points.append((7.0, "최근 2봉 내 당일 신고가 갱신"))
        if breakout["new_low_recent"]:
            points.append((-7.0, "최근 2봉 내 당일 신저가 갱신"))
    except Exception as exc:
        warnings.append(f"고저 돌파 타이밍 계산 실패: {exc}")

    try:
        surge_ret = _cum_return_pct(df_1min, _SURGE_WINDOW_MIN)
        detail["surge_window_return_pct"] = round(surge_ret, 3) if surge_ret is not None else None
        if surge_ret is not None:
            if surge_ret >= _SURGE_THRESHOLD_PCT:
                points.append((10.0, f"최근 {_SURGE_WINDOW_MIN}분간 급등({surge_ret:+.2f}%)"))
            elif surge_ret <= -_SURGE_THRESHOLD_PCT:
                points.append((-10.0, f"최근 {_SURGE_WINDOW_MIN}분간 급락({surge_ret:+.2f}%)"))
    except Exception as exc:
        warnings.append(f"급등/급락 속도 계산 실패: {exc}")

    raw_sum = sum(p for p, _ in points)
    score = round(max(0.0, min(100.0, 50.0 + max(-50.0, min(50.0, raw_sum)))), 2)

    points.sort(key=lambda x: abs(x[0]), reverse=True)
    reason_top5 = [f"{('+' if p > 0 else '')}{p:.1f}점: {desc}" for p, desc in points[:5]]

    return {
        "intraday_momentum_score": score,
        "raw_point_sum": round(raw_sum, 2),
        "reason_top5": reason_top5,
        "warnings": warnings,
        "detail": detail,
    }
