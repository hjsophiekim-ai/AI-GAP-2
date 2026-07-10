"""
exit_order_coordinator.py — 단일 Exit Order Coordinator.

Dynamic Exit Watcher(1초 주기)와 3분 자동매매 사이클(레거시 TP/SL, 강제청산,
스위칭 매도)이 동시에 같은 포지션에 대해 SELL을 실행하지 못하도록 막는
공용 락 + 최근 체결 쿨다운이다.

키: (mode, symbol, exit_reason_type) — exit_reason_type은 세부 태그(tp1/sl2 등)가
아니라 "stop_loss"/"take_profit"/"liquidation"/"switch" 같은 상위 범주를 써야
서로 다른 두 시스템(레거시 TP/SL vs Dynamic Exit AI)이 같은 이유로 동시에 팔려는
경우도 충돌로 잡아낼 수 있다.

동일 종목에 SELL 주문이 진행 중이거나(락 보유 중) 최근 COOLDOWN_SECONDS 이내에
체결됐으면 추가 SELL을 금지한다.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Dict, Optional, Set

COOLDOWN_SECONDS = 30.0

EXIT_REASON_STOP_LOSS = "stop_loss"
EXIT_REASON_TAKE_PROFIT = "take_profit"
EXIT_REASON_LIQUIDATION = "liquidation"
EXIT_REASON_SWITCH = "switch"

_lock = threading.Lock()
_active_keys: Set[str] = set()
_last_executed_at: Dict[str, float] = {}


def _key(mode: str, symbol: str, exit_reason_type: str) -> str:
    return f"{mode}:{symbol}:{exit_reason_type}"


def classify_exit_reason(reason: str) -> str:
    """Dynamic Exit AI의 자유 텍스트 reason을 상위 범주로 정규화한다."""
    text = reason or ""
    if "손절" in text:
        return EXIT_REASON_STOP_LOSS
    if "익절" in text:
        return EXIT_REASON_TAKE_PROFIT
    if "청산" in text:
        return EXIT_REASON_LIQUIDATION
    return EXIT_REASON_SWITCH


class ExitLockHandle:
    """try_acquire_exit_lock()이 yield하는 핸들.

    bool(handle)로 락 획득 여부를 확인하고, 실제로 매도가 *성공*했을 때만
    mark_executed()를 호출한다 — 실패한 시도(예: 브로커 일시 오류로 인한 즉시
    재시도)는 30초 쿨다운을 유발하지 않아야 하기 때문이다.
    """

    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.executed = False

    def mark_executed(self) -> None:
        self.executed = True

    def __bool__(self) -> bool:
        return self.acquired


@contextmanager
def try_acquire_exit_lock(mode: str, symbol: str, exit_reason_type: str):
    """SELL을 실제로 실행하기 직전 반드시 이 컨텍스트로 감싼다.

    사용례::

        with try_acquire_exit_lock(mode, symbol, EXIT_REASON_STOP_LOSS) as lock:
            if not lock:
                return {"triggered": True, "executed": False, "blocked_by_coordinator": True, ...}
            result = ... 실제 broker.sell() 호출 ...
            if result.get("success"):
                lock.mark_executed()

    락을 획득하지 못하면(다른 곳에서 이미 진행 중이거나 최근 30초 이내 *성공한*
    매도가 있으면) 호출부는 반드시 매도를 스킵해야 한다. 실패한 시도는 쿨다운을
    유발하지 않으므로 같은 사이클 내 즉시 재시도는 계속 허용된다.
    """
    k = _key(mode, symbol, exit_reason_type)
    handle = ExitLockHandle(acquired=False)
    with _lock:
        now = time.monotonic()
        last = _last_executed_at.get(k)
        if k not in _active_keys and (last is None or now - last >= COOLDOWN_SECONDS):
            _active_keys.add(k)
            handle.acquired = True
    try:
        yield handle
    finally:
        if handle.acquired:
            with _lock:
                _active_keys.discard(k)
                if handle.executed:
                    _last_executed_at[k] = time.monotonic()


def is_locked(mode: str, symbol: str, exit_reason_type: str) -> bool:
    k = _key(mode, symbol, exit_reason_type)
    with _lock:
        return k in _active_keys


def seconds_since_last_execution(mode: str, symbol: str, exit_reason_type: str) -> Optional[float]:
    k = _key(mode, symbol, exit_reason_type)
    with _lock:
        last = _last_executed_at.get(k)
    return None if last is None else time.monotonic() - last


def reset_for_tests() -> None:
    """테스트 전용 — 전역 락/쿨다운 상태 초기화."""
    with _lock:
        _active_keys.clear()
        _last_executed_at.clear()
