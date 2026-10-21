"""잔고조회(inquire-balance) 공유 TTL 캐시 + 동시호출 coalescing (2026-10-21).

KIS 는 계좌 단위 초당 한도(EGW00215)가 있고 ``KISClient.get_balance`` 는 그 오류를
내부에서 최대 8회 재시도한다. 같은 계좌를 보는 여러 경로가 동시에 부르면 한도를
서로 갉아먹는다. 이 모듈은 **표시/진단 전용** 경로가 쓰는 창구다:

  * ``max_age_sec`` 안의 성공 결과는 재사용한다(오류 결과는 캐시하지 않는다)
  * 같은 계좌로 이미 진행 중인 조회가 있으면 새로 부르지 않고 그 결과를 같이
    받는다(single-flight)

주문 판단의 근거가 되는 경로(MACD2 worker reconcile, 체결 확인 폴링, 비상
매도)는 이 모듈을 쓰지 않고 **직접** 조회한다 -- 주문 직후의 수량을 주문 전에
시작된 응답으로 판단하면 안 되기 때문이다.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Optional

_lock = threading.Lock()
_entries: dict[tuple, dict[str, Any]] = {}


def _key(kis) -> tuple:
    return (str(getattr(kis, "mode", "") or ""), str(getattr(kis, "account_no", "") or ""),
            str(getattr(kis, "product_code", "") or ""))


def get_balance_shared(kis, max_age_sec: float = 5.0, *,
                       fetch: Optional[Callable[[], dict]] = None,
                       wait_timeout_sec: float = 20.0) -> dict:
    """``kis.get_balance()`` 결과를 계좌별로 공유한다. 반환값은 사본이다."""
    key = _key(kis)
    fetch = fetch or kis.get_balance
    if not key[1]:
        # 계좌를 식별할 수 없으면 다른 계좌 결과를 섞을 위험이 있다 -> 캐시하지 않는다.
        try:
            return dict(fetch() or {})
        except Exception as exc:
            return {"positions": [], "error": repr(exc)}
    with _lock:
        entry = _entries.setdefault(key, {"at": None, "value": None, "inflight": None})
        if (entry["value"] is not None and entry["at"] is not None
                and time.monotonic() - entry["at"] <= max_age_sec):
            return copy.deepcopy(entry["value"])
        waiter = entry["inflight"]
        if waiter is None:
            waiter = entry["inflight"] = {"event": threading.Event(), "value": None}
            leader = True
        else:
            leader = False
    if not leader:
        if waiter["event"].wait(timeout=wait_timeout_sec) and waiter["value"] is not None:
            return copy.deepcopy(waiter["value"])
        return {"positions": [], "error": "BALANCE_COALESCE_TIMEOUT"}
    try:
        value = fetch()
    except Exception as exc:
        value = {"positions": [], "error": repr(exc)}
    with _lock:
        waiter["value"] = value
        entry["inflight"] = None
        if isinstance(value, dict) and not value.get("error"):
            entry["value"] = value
            entry["at"] = time.monotonic()
    waiter["event"].set()
    return copy.deepcopy(value)


def clear() -> None:
    """테스트/수동 무효화용."""
    with _lock:
        _entries.clear()
