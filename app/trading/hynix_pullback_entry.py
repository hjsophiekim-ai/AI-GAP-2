"""
hynix_pullback_entry.py — 신규 매수 진입 시점을 "눌림목" 부근으로 유도.

최초 매수(또는 스위칭의 재매수 레그) 시점을 강제거래 시간창 시작 시각 등으로
고정하지 않고, 국소 고점 대비 소폭 후퇴 후 반등이 확인되는 지점을 우선 선택한다.
데이터가 부족하거나 눌림목이 끝까지 나타나지 않으면 호출부(엔진)가 데드라인
기준으로 강제 진입할 수 있도록 판정 결과만 반환한다(진입 여부의 최종 결정은
엔진이 데드라인과 함께 종합 판단).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

_DEFAULT_LOOKBACK_BARS = 10
_SHALLOW_PULLBACK_MIN_PCT = 0.3
_SHALLOW_PULLBACK_MAX_PCT = 2.0
_BREAKDOWN_BUFFER = 1.0003  # Require a small hold above the recent low without forcing long waits.


def detect_pullback(
    df_1min: Optional[pd.DataFrame],
    lookback_bars: int = _DEFAULT_LOOKBACK_BARS,
    shallow_min_pct: float = _SHALLOW_PULLBACK_MIN_PCT,
    shallow_max_pct: float = _SHALLOW_PULLBACK_MAX_PCT,
) -> dict:
    """직전 국소 고점 대비 소폭 후퇴(눌림목) + 반등 시작 여부를 판정한다.

    조건(모두 충족해야 눌림목으로 판정):
      1) 국소 고점 대비 후퇴폭이 shallow_min_pct~shallow_max_pct 사이(너무 얕거나 깊지 않음)
      2) 국소 저점을 깨지 않음(추세 붕괴가 아닌 정상적인 눌림)
      3) 마지막 봉이 직전 봉보다 종가가 같거나 높음(반등 시작 확인)
    """
    result = {
        "is_pullback": False, "pullback_pct": None, "recent_high": None, "recent_low": None,
        "current_price": None, "bounce_confirmed": False, "reason": "데이터 부족",
    }
    if df_1min is None or len(df_1min) < 5:
        return result

    work = df_1min.sort_values("datetime").tail(max(5, min(lookback_bars, 10))).copy()
    for col in ("high", "low", "close"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["high", "low", "close"])
    if len(work) < 5:
        return result
    recent_high = float(work["high"].max())
    recent_low = float(work["low"].min())
    current = float(work["close"].iloc[-1])
    result.update(recent_high=recent_high, recent_low=recent_low, current_price=current)

    if recent_high <= 0:
        result["reason"] = "고점 계산 불가"
        return result

    pullback_pct = (recent_high - current) / recent_high * 100
    result["pullback_pct"] = round(pullback_pct, 3)

    atr_pct = None
    if {"high", "low", "close"}.issubset(work.columns) and len(work) >= 3:
        prev_close = work["close"].shift(1)
        tr = pd.concat(
            [
                (work["high"] - work["low"]).abs(),
                (work["high"] - prev_close).abs(),
                (work["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1).dropna()
        if not tr.empty and current > 0:
            atr_pct = float(tr.tail(min(5, len(tr))).mean()) / current * 100.0
    if atr_pct is not None:
        shallow_max_pct = max(shallow_max_pct, min(3.0, atr_pct * 1.5))
    result["atr_pct"] = round(atr_pct, 3) if atr_pct is not None else None

    is_shallow = shallow_min_pct <= pullback_pct <= shallow_max_pct
    not_breaking_down = current > recent_low * _BREAKDOWN_BUFFER

    bounce_confirmed = False
    if len(work) >= 2:
        bounce_confirmed = float(work["close"].iloc[-1]) >= float(work["close"].iloc[-2])
    result["bounce_confirmed"] = bounce_confirmed

    is_pullback = is_shallow and not_breaking_down and bounce_confirmed
    result["is_pullback"] = is_pullback

    if is_pullback:
        result["reason"] = f"국소고점 {recent_high:,.0f} 대비 {pullback_pct:.2f}% 눌림 + 반등 확인"
    elif not is_shallow:
        result["reason"] = f"눌림 폭 {pullback_pct:.2f}%가 기준({shallow_min_pct}~{shallow_max_pct}%) 밖"
    elif not not_breaking_down:
        result["reason"] = f"국소 저점({recent_low:,.0f}) 붕괴 — 눌림목 아닌 추세 이탈 가능성"
    else:
        result["reason"] = "눌림 폭은 적절하나 아직 반등 미확인"

    return result
