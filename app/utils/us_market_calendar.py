"""
us_market_calendar.py — 미국 주식시장 세션 감지 (KST 기준).

서머타임 미적용 기본값:
  프리마켓:  17:00 ~ 22:30 KST
  정규장:   22:30 ~ 05:00 KST (+1일)
  애프터마켓: 05:00 ~ 09:00 KST
  장 없음:   09:00 ~ 17:00 KST

실전 주문 기능과 절대 연결하지 않습니다.
"""

from __future__ import annotations

from datetime import datetime, date
from typing import Optional

try:
    import pytz
    _KST = pytz.timezone("Asia/Seoul")
    _HAS_PYTZ = True
except ImportError:
    _HAS_PYTZ = False
    _KST = None

# ── 세션 상수 (KST 분 단위) ───────────────────────────────────────────────────

_PREMARKET_START = 17 * 60        # 17:00 KST
_PREMARKET_END   = 22 * 60 + 30   # 22:30 KST
_REGULAR_END     = 5 * 60         # 05:00 KST (+1)
_AFTER_END       = 9 * 60         # 09:00 KST

# 미국 주요 고정 공휴일 (월, 일)
_US_FIXED_HOLIDAYS: frozenset[tuple[int, int]] = frozenset({
    (1, 1),    # New Year's Day
    (7, 4),    # Independence Day
    (12, 24),  # Christmas Eve (half-day → 미적용)
    (12, 25),  # Christmas Day
})


# ── 세션 감지 ─────────────────────────────────────────────────────────────────

def get_session_kst(dt: Optional[datetime] = None) -> str:
    """
    KST 기준으로 현재 미국 주식시장 세션을 반환.

    Returns
    -------
    "premarket" | "regular" | "aftermarket" | "weekend" | "holiday" | "closed"
    """
    if dt is None:
        dt = _now_kst()
    elif _HAS_PYTZ and dt.tzinfo is None:
        dt = _KST.localize(dt)

    weekday = dt.weekday()   # 0=월 … 6=일
    minutes = dt.hour * 60 + dt.minute

    # ── 주말 판정 ─────────────────────────────────────────────────────────────
    # KST 일요일(6) 전체 = 미국 토요일 + 일요일 → 장 없음
    if weekday == 6:
        return "weekend"

    # KST 토요일(5) 09:00 ~ 17:00 사이 = 미국 시장 모두 닫힘
    if weekday == 5 and _AFTER_END <= minutes < _PREMARKET_START:
        return "weekend"

    # ── 공휴일 판정 ───────────────────────────────────────────────────────────
    # KST 22:30 이후는 미국 "같은 날"이 아닐 수 있으나 간단화로 KST 날짜 기준
    if (dt.month, dt.day) in _US_FIXED_HOLIDAYS:
        # 프리마켓 이후부터는 공휴일로 처리
        if minutes >= _PREMARKET_START or minutes < _AFTER_END:
            return "holiday"

    # ── 세션 분류 ─────────────────────────────────────────────────────────────
    if _PREMARKET_START <= minutes < _PREMARKET_END:
        return "premarket"

    if minutes >= _PREMARKET_END or minutes < _REGULAR_END:
        return "regular"

    if _REGULAR_END <= minutes < _AFTER_END:
        return "aftermarket"

    # 09:00 ~ 17:00 KST = 미국 야간 (장 없음)
    return "closed"


def get_collection_status(dt: Optional[datetime] = None) -> dict:
    """
    현재 세션과 데이터 수집 가능 여부를 반환.

    Returns
    -------
    dict
        session         : 세션명 문자열
        is_trading      : 프리/정규/애프터 중 하나면 True
        is_market_open  : 정규장 여부
        can_collect_mu  : MU 데이터 수집 가능 여부
        reason          : 설명 문자열 (한글)
    """
    session = get_session_kst(dt)
    is_trading = session in ("premarket", "regular", "aftermarket")
    is_open    = session == "regular"

    reason_map = {
        "premarket":    "미국 프리마켓 (MU 프리마켓 데이터 수집 가능)",
        "regular":      "미국 정규장 (실시간 데이터 수집 가능)",
        "aftermarket":  "미국 애프터마켓 (당일 종가 기준 데이터 수집 가능)",
        "weekend":      "주말 — 미국 시장 휴장 (데이터 수집 불가)",
        "holiday":      "미국 공휴일 — 장 없음 (데이터 수집 불가)",
        "closed":       "한국 낮 시간 — 미국 장 없음 (전일 데이터만 사용 가능)",
    }

    return {
        "session":        session,
        "is_trading":     is_trading,
        "is_market_open": is_open,
        "can_collect_mu": is_trading,
        "reason":         reason_map.get(session, session),
    }


def is_us_trading_hours(dt: Optional[datetime] = None) -> bool:
    """프리마켓·정규장·애프터마켓 중 하나라도 활성 상태인지."""
    return get_session_kst(dt) in ("premarket", "regular", "aftermarket")


def is_weekend_or_holiday(dt: Optional[datetime] = None) -> bool:
    """주말 또는 공휴일 여부."""
    return get_session_kst(dt) in ("weekend", "holiday")


def data_unavailable_reason(dt: Optional[datetime] = None) -> Optional[str]:
    """
    데이터 수집 불가 시 한글 이유 반환. 수집 가능하면 None.
    """
    status = get_collection_status(dt)
    if not status["can_collect_mu"]:
        return status["reason"]
    return None


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _now_kst() -> datetime:
    """현재 KST 시각 반환."""
    if _HAS_PYTZ:
        return datetime.now(tz=_KST)
    from datetime import timezone, timedelta
    kst = timezone(timedelta(hours=9))
    return datetime.now(tz=kst)
