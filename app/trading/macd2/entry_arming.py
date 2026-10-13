"""MACD2 신규진입 허용(arming) — 감시와 신규진입의 분리 (2026-10-13).

worker 는 **감시** 상태에서 시세조회·플래그 인식·전략 상태계산(N1/P3/E/CHOP)·
신호원장 기록을 전부 그대로 한다. 이 모듈은 그 위에서 **신규 매수만** 막는다.

    entry_enabled = False  -> 신규 매수/신규 진입 0건 (기본값, 재시작 시 저장값 복원)
    entry_enabled = True   -> entry_armed_at **이후 발생한** 신호만 주문 가능

보유 포지션 관리(손절/익절/반대신호 청산)는 이 모듈과 무관하게 계속 돈다.
반대신호로 보유 ETF 를 갈아타야 할 때 신규진입이 막혀 있으면 **매도만** 하고
반대 ETF 는 사지 않는다(worker._execute_or_wait).

소급 금지: OFF 중에 생긴 플래그/pending/candidate 는 ON 이후에도 체결하지 않는다.
판정 기준은 신호의 **발생 시각** = 플래그 3분봉의 마감 시각(봉 시작 + 3분)이다.
그 봉이 entry_armed_at 이전에 마감됐으면 ON 이전 신호다(ENTRY_PRE_ARM_SIGNAL).
발생 시각을 알 수 없으면 주문하지 않는다(fail-safe).

모드(N1/P3/E/CHOP)와 무관하게 똑같이 적용된다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from app.trading.macd2 import config

ENTRY_DISARMED = "ENTRY_DISARMED"
ENTRY_PRE_ARM_SIGNAL = "ENTRY_PRE_ARM_SIGNAL"
BLOCK_REASONS = (ENTRY_DISARMED, ENTRY_PRE_ARM_SIGNAL)

#: 원장 결과 라벨 — 보유 ETF 매도만 하고 반대 ETF 매수는 막았다.
SELL_EXECUTED_ENTRY_DISARMED = "SELL_EXECUTED_ENTRY_DISARMED"

_BAR = timedelta(minutes=3)
_SID_RE = re.compile(r"^(\d{8})_(\d{6})_")


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.KST)
    return dt


def is_enabled(state: Any) -> bool:
    return bool(getattr(state, "entry_enabled", False))


def armed_at(state: Any) -> Optional[datetime]:
    return _parse(getattr(state, "entry_armed_at", None))


def signal_origin_at(signal_id: Optional[str] = None,
                     flag_bar_dt: Optional[datetime] = None) -> Optional[datetime]:
    """신호의 발생 시각 = 플래그 봉 마감(봉 시작 + 3분).

    ``flag_bar_dt`` (봉 시작) 가 있으면 그것을, 없으면 signal_id 의
    ``YYYYMMDD_HHMMSS_`` 접두사(봉 시작)를 쓴다. 둘 다 있으면 더 이른 쪽 —
    T+3 확정 signal_id 는 확인봉 시각이라 플래그봉보다 늦을 수 있다.
    """
    cands: list[datetime] = []
    fb = _parse(flag_bar_dt)
    if fb is not None:
        cands.append(fb)
    m = _SID_RE.match(str(signal_id or ""))
    if m:
        try:
            cands.append(datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=config.KST))
        except ValueError:
            pass
    if not cands:
        return None
    return min(cands) + _BAR


def block_reason(state: Any, *, signal_id: Optional[str] = None,
                 flag_bar_dt: Optional[datetime] = None,
                 origin_at: Optional[datetime] = None) -> Optional[str]:
    """신규진입을 막아야 하면 사유, 허용이면 None."""
    if not is_enabled(state):
        return ENTRY_DISARMED
    armed = armed_at(state)
    if armed is None:
        return ENTRY_DISARMED
    origin = _parse(origin_at) if origin_at is not None else signal_origin_at(signal_id, flag_bar_dt)
    if origin is None or origin < armed:
        return ENTRY_PRE_ARM_SIGNAL
    # 허용은 켠 날에만 유효하다 -- 날짜가 바뀌면 worker 가 OFF 로 돌리지만,
    # 그 tick 보다 먼저 판정이 오더라도 전날 ON 으로는 사지 않는다.
    if origin.astimezone(config.KST).date() != armed.astimezone(config.KST).date():
        return ENTRY_DISARMED
    return None


def set_enabled(state: Any, enabled: bool, *, now_iso: str, changed_by: str = "ui") -> bool:
    """신규진입 허용 토글. 반환값 = 이전 값.

    OFF -> ON 일 때만 entry_armed_at 을 지금으로 찍는다(ON 상태에서 다시 ON 해도
    기준시각은 그대로). ON -> OFF 는 즉시 차단, entry_armed_at 은 지운다.
    """
    prev = is_enabled(state)
    on = bool(enabled)
    state.entry_enabled = on
    state.entry_changed_at = now_iso
    state.entry_changed_by = str(changed_by or "ui")
    if on and not prev:
        state.entry_armed_at = now_iso
    elif not on:
        state.entry_armed_at = None
    return prev
