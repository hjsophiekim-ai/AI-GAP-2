"""
etf_entry_confirmation.py — 방향판단(000660/Adaptive Regime)과 주문실행
데이터(0193T0/0197X0 실제 거래 ETF)를 분리한다(요구사항 2026-07-20).

000660은 Adaptive Regime·큰 방향·추세구조 판단에만 쓴다. 실제 신규진입/확대/
청산 타이밍은 반드시 실제 거래 ETF(0193T0/0197X0) 자신의 1분봉으로 재확인한
뒤에만 실행한다 — 하이닉스(000660) 신호만으로 ETF 주문을 내보내지 않는다.

이 모듈은 절대 000660의 분봉을 0193T0/0197X0 데이터로 대체하지 않는다 —
0193T0은 app.data_sources.hynix_long_collector.collect_long_minute(), 0197X0은
app.data_sources.hynix_inverse_collector.collect_inverse_minute()이 각각 수집한
"진짜 그 ETF 자신의" 1분봉만 쓴다. 둘 중 하나라도 데이터가 없거나
오래됐으면(stale) ETF_DATA_INSUFFICIENT로 즉시 fail-closed 처리한다 — 정상
데이터가 확인되기 전까지 신규진입을 절대 허용하지 않는다.

confirm_etf_entry()가 반환하는 4가지 차단 코드:
  ETF_DATA_INSUFFICIENT — ETF 자체 분봉이 없음/부족함/오래됨(신규 롱 진입까지 포함해 항상 차단)
  ETF_DIRECTION_MISMATCH — ETF 자체 VWAP 또는 기울기 방향이 기초자산 방향과 불일치
  CHASE_BLOCK — 신호 발생가 대비 ETF가 이미 0.7% 이상 이동
  ETF_EXTREME_BLOCK — 최근 3분 고점/저점 0.2% 이내(추격 진입)

10/20/30초 단위 기울기는 이 코드베이스에 진짜 sub-minute(초 단위) 시세 피드가
없어(1분봉이 가장 짧은 캔들) 정확히 계산할 수 없다 — "가장 가까운 가용
해상도"로 최근 1분봉 종가 간 기울기를 근사한다(요구사항 문구의 "nearest
available slope"에 해당하는 명시적 근사).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL

ETF_DATA_INSUFFICIENT = "ETF_DATA_INSUFFICIENT"
ETF_DIRECTION_MISMATCH = "ETF_DIRECTION_MISMATCH"
CHASE_BLOCK = "CHASE_BLOCK"
ETF_EXTREME_BLOCK = "ETF_EXTREME_BLOCK"

MIN_BARS_FOR_CONFIRMATION = 5
CHASE_BLOCK_MOVE_PCT = 0.7
EXTREME_ZONE_PCT = 0.2
# 요구사항(2026-07-20 최종) — CHASE_BLOCK 극값 기준을 최근 3분에서 1분으로 축소.
EXTREME_LOOKBACK_MINUTES = 1
VOLUME_SURGE_RATIO = 1.5
VOLUME_SURGE_LOOKBACK_BARS = 5


def fetch_etf_minute_bars(symbol: str, mode: Optional[str] = None) -> dict:
    """symbol(0193T0/0197X0)의 진짜 자기 자신 1분봉을 가져온다 — 절대 000660으로
    대체하지 않는다. 반환 스키마는 두 수집기 모두 동일: {df_1min, source, status,
    stale, last_bar_time, error}."""
    if symbol == LONG_SYMBOL:
        from app.data_sources.hynix_long_collector import collect_long_minute

        return collect_long_minute(mode=mode)
    if symbol == INVERSE_SYMBOL:
        from app.data_sources.hynix_inverse_collector import collect_inverse_minute

        return collect_inverse_minute(mode=mode)
    return {
        "df_1min": None, "source": None, "status": "unsupported_symbol", "stale": False,
        "last_bar_time": None, "error": f"ETF confirmation unsupported for symbol={symbol!r}",
    }


def compute_etf_vwap(df_1min: pd.DataFrame) -> Optional[float]:
    if df_1min is None or df_1min.empty or "volume" not in df_1min.columns:
        return None
    work = df_1min.sort_values("datetime")
    vol = work["volume"].fillna(0)
    if float(vol.sum()) <= 0:
        return round(float(work["close"].mean()), 4)
    typical = (work["high"] + work["low"] + work["close"]) / 3.0
    return round(float((typical * vol).sum() / vol.sum()), 4)


def compute_etf_slope_direction(df_1min: pd.DataFrame) -> Optional[str]:
    """가장 가까운 가용 해상도(1분봉 종가 간)로 방향을 근사한다 — 진짜 10/20/30초
    tick 데이터가 없을 때의 명시적 근사(요구사항 "nearest available slope")."""
    if df_1min is None or len(df_1min) < 2:
        return None
    work = df_1min.sort_values("datetime")
    prev_close = float(work["close"].iloc[-2])
    last_close = float(work["close"].iloc[-1])
    if prev_close <= 0:
        return None
    change_pct = (last_close / prev_close - 1.0) * 100.0
    if change_pct > 0.01:
        return "UP"
    if change_pct < -0.01:
        return "DOWN"
    return "FLAT"


def compute_etf_recent_extreme(df_1min: pd.DataFrame, lookback_minutes: int = EXTREME_LOOKBACK_MINUTES) -> tuple[Optional[float], Optional[float]]:
    """최근 lookback_minutes의 고점/저점을 "지금 이 순간 이전까지" 기준으로 낸다.

    마지막(현재) 봉 자체를 제외한다 — 포함하면 순조로운 상승 추세의 마지막 종가가
    항상 그 자체로 "최근 고점"이 되어(오르는 중이면 방금 값이 곧 최고값이므로)
    정상적인 추세추종 진입까지 매번 ETF_EXTREME_BLOCK으로 막아버린다. 이 함수는
    "새로 고점을 만드는 중"과 "이미 만들어진 고점/저점 근처에서 추격하는 것"을
    구분하기 위한 것이므로, 직전까지의 구조만 기준으로 삼는다."""
    if df_1min is None or len(df_1min) < 2:
        return None, None
    work = df_1min.sort_values("datetime")
    prior = work.iloc[:-1]
    cutoff = work["datetime"].iloc[-1] - pd.Timedelta(minutes=lookback_minutes)
    recent = prior[prior["datetime"] >= cutoff]
    if recent.empty:
        return None, None
    return round(float(recent["high"].max()), 4), round(float(recent["low"].min()), 4)


def compute_etf_volume_surge(df_1min: pd.DataFrame, lookback_bars: int = VOLUME_SURGE_LOOKBACK_BARS) -> Optional[bool]:
    """요구사항1(2026-07-20 최종) — ETF 자체 거래량 급증. 직전 lookback_bars개
    평균 대비 마지막 봉 거래량이 VOLUME_SURGE_RATIO배 이상이면 True. 데이터가
    부족하면 판단하지 않는다(None)."""
    if df_1min is None or df_1min.empty or "volume" not in df_1min.columns or len(df_1min) < lookback_bars + 1:
        return None
    work = df_1min.sort_values("datetime")
    try:
        recent_vol = float(work["volume"].iloc[-1])
        baseline = work["volume"].iloc[-(lookback_bars + 1):-1]
        baseline_avg = float(baseline.mean())
    except Exception:
        return None
    if baseline_avg <= 0:
        return None
    return (recent_vol / baseline_avg) >= VOLUME_SURGE_RATIO


def compute_etf_breakouts(df_1min: pd.DataFrame, current_price: Optional[float], direction: str) -> dict:
    """요구사항1(2026-07-20 최종) — ETF 자체 VWAP 이탈/최근 1분봉 고점·저점
    돌파를 하나로 묶어 반환한다(Early Trend Detector 조기신호 입력용)."""
    vwap = compute_etf_vwap(df_1min)
    vwap_breakout = None
    if vwap is not None and current_price is not None:
        vwap_breakout = (current_price > vwap) if direction == "UP" else (current_price < vwap)

    recent_high, recent_low = compute_etf_recent_extreme(df_1min, lookback_minutes=EXTREME_LOOKBACK_MINUTES)
    structure_breakout = None
    if current_price is not None:
        if direction == "UP" and recent_high:
            structure_breakout = current_price > recent_high
        elif direction == "DOWN" and recent_low:
            structure_breakout = current_price < recent_low

    return {
        "vwap": vwap, "vwap_breakout": vwap_breakout, "structure_breakout": structure_breakout,
        "recent_high": recent_high, "recent_low": recent_low,
    }


def confirm_etf_entry(
    *, symbol: str, underlying_direction: str, current_price: Optional[float],
    signal_reference_price: Optional[float] = None, mode: Optional[str] = None,
    minute_bars_result: Optional[dict] = None,
) -> dict:
    """실제 거래 ETF(symbol) 자신의 1분봉으로 신규진입을 재확인한다.

    underlying_direction: "UP"|"DOWN" — 000660/Adaptive Regime이 판단한 기초
    방향(이 함수는 이 방향을 다시 계산하지 않고 그대로 받아, ETF 자신의 데이터와
    "일치하는지"만 확인한다). 반환: {approved, block_code, reason, source, stale,
    last_bar_time, using_genuine_etf_data, vwap, slope_direction, moved_pct_since_signal,
    recent_high, recent_low}."""
    minute_bars_result = minute_bars_result if minute_bars_result is not None else fetch_etf_minute_bars(symbol, mode=mode)
    df = minute_bars_result.get("df_1min")
    stale = bool(minute_bars_result.get("stale"))
    diagnostics = {
        "symbol": symbol, "source": minute_bars_result.get("source"), "stale": stale,
        "status": minute_bars_result.get("status"), "last_bar_time": minute_bars_result.get("last_bar_time"),
        "using_genuine_etf_data": bool(df is not None and not getattr(df, "empty", True) and not stale),
        "vwap": None, "slope_direction": None, "moved_pct_since_signal": None,
        "recent_high": None, "recent_low": None,
    }

    if df is None or getattr(df, "empty", True) or len(df) < MIN_BARS_FOR_CONFIRMATION or stale:
        return {
            **diagnostics, "approved": False, "block_code": ETF_DATA_INSUFFICIENT,
            "reason": (
                f"{symbol} 1분봉 데이터 부족/오래됨(source={minute_bars_result.get('source')}, "
                f"stale={stale}, error={minute_bars_result.get('error')}) — 신규진입 차단(fail-closed)"
            ),
        }

    vwap = compute_etf_vwap(df)
    diagnostics["vwap"] = vwap
    if vwap and current_price:
        etf_vwap_direction = "UP" if current_price >= vwap else "DOWN"
        if etf_vwap_direction != underlying_direction:
            return {
                **diagnostics, "approved": False, "block_code": ETF_DIRECTION_MISMATCH,
                "reason": f"{symbol} 자체 VWAP 기준 방향({etf_vwap_direction})이 기초자산 방향({underlying_direction})과 불일치",
            }

    slope_direction = compute_etf_slope_direction(df)
    diagnostics["slope_direction"] = slope_direction
    if slope_direction and slope_direction != "FLAT" and slope_direction != underlying_direction:
        return {
            **diagnostics, "approved": False, "block_code": ETF_DIRECTION_MISMATCH,
            "reason": f"{symbol} 자체 기울기 방향({slope_direction})이 기초자산 방향({underlying_direction})과 불일치",
        }

    if signal_reference_price and current_price:
        moved_pct = round(abs(current_price / signal_reference_price - 1.0) * 100.0, 4)
        diagnostics["moved_pct_since_signal"] = moved_pct
        if moved_pct >= CHASE_BLOCK_MOVE_PCT:
            return {
                **diagnostics, "approved": False, "block_code": CHASE_BLOCK,
                "reason": f"CHASE_BLOCK: 신호가 대비 {moved_pct}% 이동(임계 {CHASE_BLOCK_MOVE_PCT}%)",
            }

    recent_high, recent_low = compute_etf_recent_extreme(df)
    diagnostics["recent_high"], diagnostics["recent_low"] = recent_high, recent_low
    if current_price and recent_high and recent_low:
        # 요구사항 — "최근 3분 극값 0.2% 이내"는 현재가가 그 직전 고점/저점에 아직
        # 못 미친 채 근접한(=추격 매수/매도) 경우만 뜻한다. 현재가가 이미 그
        # 직전 극값을 방향에 맞게 새로 갱신했다면(예: 상승 추세 진입 중 신고가
        # 경신) 그건 추격이 아니라 정상 추세추종이므로 막지 않는다 — 거리값이
        # 음수(이미 돌파)이면 차단하지 않는다.
        if underlying_direction == "UP" and recent_high > 0:
            distance_pct = (recent_high - current_price) / recent_high * 100.0
            if 0.0 <= distance_pct <= EXTREME_ZONE_PCT:
                return {
                    **diagnostics, "approved": False, "block_code": ETF_EXTREME_BLOCK,
                    "reason": f"ETF_EXTREME_BLOCK: 최근 {EXTREME_LOOKBACK_MINUTES}분 고점 {recent_high} 대비 {EXTREME_ZONE_PCT}% 이내",
                }
        if underlying_direction == "DOWN" and recent_low > 0:
            distance_pct = (current_price - recent_low) / recent_low * 100.0
            if 0.0 <= distance_pct <= EXTREME_ZONE_PCT:
                return {
                    **diagnostics, "approved": False, "block_code": ETF_EXTREME_BLOCK,
                    "reason": f"ETF_EXTREME_BLOCK: 최근 {EXTREME_LOOKBACK_MINUTES}분 저점 {recent_low} 대비 {EXTREME_ZONE_PCT}% 이내",
                }

    return {**diagnostics, "approved": True, "block_code": None, "reason": f"{symbol} 자체 데이터 확인 통과"}
