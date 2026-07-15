"""
hynix_technical_score.py — SK하이닉스 정밀 기술점수 (calculate_hynix_technical_score).

기존 `hynix_swing_flag.compute_hynix_tech_indicators()`(RSI/MACD 방향/MA5·20·60%/
%B/거래량변화율)를 그대로 재사용하고, 부족한 지표(MACD 히스토그램, 볼린저 상/중/하
밴드, Williams %R, Stochastic K/D, VWAP, MA10/120/200, 1·3·5분봉 방향, 장중 고저
갱신)만 이 파일에서 추가 계산한다. 지표별 실패는 해당 항목만 건너뛰고 나머지로
점수를 계산한다(전체 실패 없음).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from app.logger import logger
from app.models.hynix_swing_flag import compute_hynix_tech_indicators

_RECOVERY_LOOKBACK = 6


def _rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0.0, float("inf"))
    return 100 - 100 / (1 + rs)


def _macd_series(closes: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal, macd - signal


def _bollinger_series(closes: pd.Series, period: int = 20, k: float = 2.0):
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    return mid + k * std, mid, mid - k * std


def _williams_r_series(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> pd.Series:
    hh = highs.rolling(period).max()
    ll = lows.rolling(period).min()
    span = (hh - ll).replace(0.0, float("nan"))
    return (hh - closes) / span * -100.0


def _stochastic_series(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    hh = highs.rolling(period).max()
    ll = lows.rolling(period).min()
    span = (hh - ll).replace(0.0, float("nan"))
    fast_k = (closes - ll) / span * 100.0
    slow_k = fast_k.rolling(smooth_k).mean()
    slow_d = slow_k.rolling(smooth_d).mean()
    return slow_k, slow_d


def _breached_then_recovered(breach_series: pd.Series, lookback: int = _RECOVERY_LOOKBACK) -> bool:
    """최근 lookback봉 중(마지막 제외) breach=True가 있었고 마지막 봉은 False면 회복으로 판단."""
    if breach_series is None or len(breach_series) < 2:
        return False
    window = breach_series.tail(lookback)
    if len(window) < 2:
        return False
    prior = window.iloc[:-1]
    current = window.iloc[-1]
    return bool(prior.fillna(False).any() and not bool(current))


def _vwap_from_1min(df_1min: Optional[pd.DataFrame]) -> Optional[float]:
    if df_1min is None or df_1min.empty:
        return None
    try:
        today = pd.Timestamp.now().normalize()
        work = df_1min.copy()
        work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
        work = work.dropna(subset=["datetime"])
        work = work[work["datetime"] >= today]
        if work.empty:
            work = df_1min.copy()
        vol_sum = float(work["volume"].sum())
        if vol_sum <= 0:
            return None
        typical = (work["high"] + work["low"] + work["close"]) / 3.0
        return float((typical * work["volume"]).sum() / vol_sum)
    except Exception:
        return None


def _bar_direction(df: Optional[pd.DataFrame]) -> Optional[str]:
    if df is None or df.empty:
        return None
    try:
        last = df.iloc[-1]
        if float(last["close"]) > float(last["open"]):
            return "up"
        if float(last["close"]) < float(last["open"]):
            return "down"
        return "flat"
    except Exception:
        return None


def _resample(df_1min: pd.DataFrame, minutes: int) -> Optional[pd.DataFrame]:
    try:
        from app.data_sources.auto_market_collector import _resample_minutes

        return _resample_minutes(df_1min, minutes)
    except Exception as exc:
        logger.debug("[HynixTechnicalScore] 리샘플 실패(%s분): %s", minutes, exc)
        return None


def calculate_hynix_technical_score(df_daily: Optional[pd.DataFrame], df_1min: Optional[pd.DataFrame] = None) -> dict:
    """SK하이닉스 기술점수(0~100) + 판단 사유 Top5.

    Parameters
    ----------
    df_daily : 일봉 OHLCV DataFrame [datetime, open, high, low, close, volume]
    df_1min  : 당일 1분봉 DataFrame (VWAP·1/3/5분 방향·장중 고저 갱신에 사용, 선택)
    """
    points: list[tuple[float, str]] = []
    warnings: list[str] = []
    detail: dict = {}

    if df_daily is None or len(df_daily) < 5:
        return {
            "hynix_technical_score": 50.0,
            "reason_top5": [],
            "warnings": ["일봉 데이터 부족 — 중립값(50) 반환"],
            "detail": detail,
        }

    base = compute_hynix_tech_indicators(df_daily)
    detail.update(base)

    df = df_daily.copy().sort_values("datetime").reset_index(drop=True)
    closes, highs, lows = df["close"], df["high"], df["low"]
    current = float(closes.iloc[-1])

    # ── RSI 재돌파/과열 ──────────────────────────────────────────────────────
    try:
        rsi_series = _rsi_series(closes)
        rsi = base.get("rsi_14")
        detail["rsi_14"] = rsi
        if rsi is not None:
            if rsi >= 70:
                points.append((-10.0, f"RSI(14) {rsi:.1f} — 과매수(70 이상)"))
            elif _breached_then_recovered(rsi_series < 30):
                points.append((15.0, "RSI(14) 30 이하 이탈 후 재돌파"))
    except Exception as exc:
        warnings.append(f"RSI 재돌파 판정 실패: {exc}")

    # ── MACD 골든/데드크로스 + 히스토그램 ───────────────────────────────────
    try:
        macd_line, signal_line, hist = _macd_series(closes)
        detail["macd_histogram"] = round(float(hist.iloc[-1]), 4) if len(hist) else None
        cross = base.get("macd_signal_cross")
        if cross == 1:
            points.append((15.0, "MACD 골든크로스"))
        elif cross == -1:
            points.append((-15.0, "MACD 데드크로스"))
    except Exception as exc:
        warnings.append(f"MACD 계산 실패: {exc}")

    # ── 볼린저 밴드 (상/중/하) ───────────────────────────────────────────────
    try:
        upper, mid, lower = _bollinger_series(closes)
        detail["bollinger_upper"] = round(float(upper.iloc[-1]), 2) if len(upper) else None
        detail["bollinger_mid"] = round(float(mid.iloc[-1]), 2) if len(mid) else None
        detail["bollinger_lower"] = round(float(lower.iloc[-1]), 2) if len(lower) else None
        if detail["bollinger_lower"] is not None:
            below = closes < lower
            if bool(below.iloc[-1]):
                points.append((-12.0, "볼린저 하단 지속 이탈"))
            elif _breached_then_recovered(below):
                points.append((12.0, "볼린저 하단 이탈 후 회복"))
    except Exception as exc:
        warnings.append(f"볼린저밴드 계산 실패: {exc}")

    # ── Williams %R ──────────────────────────────────────────────────────────
    try:
        wr = _williams_r_series(highs, lows, closes)
        wr_now = float(wr.iloc[-1]) if len(wr) and pd.notna(wr.iloc[-1]) else None
        detail["williams_r"] = round(wr_now, 2) if wr_now is not None else None
        if wr_now is not None:
            if wr_now >= -20:
                points.append((-8.0, f"Williams %R {wr_now:.1f} — 과열(-20 이상)"))
            elif _breached_then_recovered(wr <= -80):
                points.append((10.0, "Williams %R -80 이하 이탈 후 회복"))
    except Exception as exc:
        warnings.append(f"Williams %R 계산 실패: {exc}")

    # ── Stochastic K/D ───────────────────────────────────────────────────────
    try:
        slow_k, slow_d = _stochastic_series(highs, lows, closes)
        detail["stochastic_k"] = round(float(slow_k.iloc[-1]), 2) if len(slow_k) and pd.notna(slow_k.iloc[-1]) else None
        detail["stochastic_d"] = round(float(slow_d.iloc[-1]), 2) if len(slow_d) and pd.notna(slow_d.iloc[-1]) else None
    except Exception as exc:
        warnings.append(f"Stochastic 계산 실패: {exc}")

    # ── 이동평균선 10/120/200 ────────────────────────────────────────────────
    try:
        for days, key in [(10, "ma10_position_pct"), (120, "ma120_position_pct"), (200, "ma200_position_pct")]:
            if len(df) >= days:
                ma = float(closes.rolling(days).mean().iloc[-1])
                detail[key] = round((current / ma - 1) * 100, 2) if ma > 0 else None
        ma200_series = closes.rolling(200).mean() if len(df) >= 200 else None
        if ma200_series is not None:
            below200 = closes < ma200_series
            if bool(below200.iloc[-1]):
                points.append((-15.0, "200일 이동평균선 이탈"))
            elif _breached_then_recovered(below200):
                points.append((15.0, "200일 이동평균선 회복"))
    except Exception as exc:
        warnings.append(f"이동평균(10/120/200) 계산 실패: {exc}")

    # ── VWAP (당일 1분봉 기준) ───────────────────────────────────────────────
    try:
        vwap = _vwap_from_1min(df_1min)
        detail["vwap"] = round(vwap, 2) if vwap is not None else None
        if vwap is not None:
            if current > vwap:
                points.append((10.0, "현재가 VWAP 상회"))
            elif current < vwap:
                points.append((-10.0, "현재가 VWAP 하회"))
    except Exception as exc:
        warnings.append(f"VWAP 계산 실패: {exc}")

    # ── 거래량 동반 상승/하락 ────────────────────────────────────────────────
    try:
        vol_chg = base.get("volume_change_pct")
        return_3d = base.get("return_3d_pct")
        if vol_chg is not None and vol_chg > 0 and return_3d is not None:
            if return_3d > 0:
                points.append((8.0, "거래량 증가 동반 상승"))
            elif return_3d < 0:
                points.append((-8.0, "거래량 증가 동반 하락"))
    except Exception as exc:
        warnings.append(f"거래량 신호 계산 실패: {exc}")

    # ── 1/3/5분봉 방향(참고용 detail만, 점수에는 반영하지 않음) + 장중 고저 갱신 ──
    # 요구사항: 초단기(1·3·5분) 모멘텀은 intraday_momentum_score가 별도 가중치로만
    # 반영해야 하며, "당일 주추세"를 나타내야 할 hynix_technical_score에 다시
    # 섞이면(이중 반영) 5분 조정 하나가 기술점수까지 함께 흔들어 HYNIX→INVERSE
    # 전환을 유발할 수 있다(2026-07-15 요구사항: 5분 조정만으로 전환 금지). 방향은
    # UI 참고용으로만 남기고 raw_point_sum에는 더하지 않는다.
    try:
        if df_1min is not None and not df_1min.empty:
            df_3min = _resample(df_1min, 3)
            df_5min = _resample(df_1min, 5)
            detail["minute_direction_1m"] = _bar_direction(df_1min)
            detail["minute_direction_3m"] = _bar_direction(df_3min)
            detail["minute_direction_5m"] = _bar_direction(df_5min)

            today = pd.Timestamp.now().normalize()
            work = df_1min.copy()
            work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
            work = work.dropna(subset=["datetime"])
            intraday = work[work["datetime"] >= today]
            if intraday.empty:
                intraday = work
            if len(intraday) >= 2:
                running_high = intraday["high"].cummax()
                running_low = intraday["low"].cummin()
                last_close = float(intraday["close"].iloc[-1])
                detail["intraday_new_high"] = bool(last_close >= float(running_high.iloc[-1]) * 0.999)
                detail["intraday_new_low"] = bool(last_close <= float(running_low.iloc[-1]) * 1.001)
    except Exception as exc:
        warnings.append(f"분봉 방향/고저갱신 계산 실패: {exc}")

    raw_sum = sum(p for p, _ in points)
    score = round(max(0.0, min(100.0, 50.0 + max(-50.0, min(50.0, raw_sum)))), 2)

    points.sort(key=lambda x: abs(x[0]), reverse=True)
    reason_top5 = [f"{('+' if p > 0 else '')}{p:.0f}점: {desc}" for p, desc in points[:5]]

    return {
        "hynix_technical_score": score,
        "raw_point_sum": round(raw_sum, 2),
        "reason_top5": reason_top5,
        "warnings": warnings,
        "detail": detail,
    }
