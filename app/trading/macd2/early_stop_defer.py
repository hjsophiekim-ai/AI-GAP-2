"""진입 후 30분 이내 첫 일반 손절 1봉 유예 (EARLY_STOP_DEFER, 2026-11-06).

규칙 (research_20261105_early_stop_2bar 58일 검증, 30분 = 민감도 꼭짓점):
  · 진입 후 경과 <= EARLY_STOP_DEFER_MINUTES(30분) 인 상태에서 **첫** TIME_WINDOW_STOP_LOSS(오전/오후 래더
    일반 손절) 판정이 나오면 그 봉에서는 팔지 않고 **다음 완성 3분봉 1개**만 기다린다.
  · 다음 봉 종가에서 기존 손절 기준을 다시 평가 — 여전히 손절이면 TIME_WINDOW_STOP_LOSS 로 청산(deferred=True 기록),
    회복이면 보유. 포지션당 유예는 **최대 1회**(used 플래그) — 두 번째 손절은 즉시 청산.
  · 대기 중 매 틱 net <= EARLY_STOP_DEFER_HARD_STOP(-2.5%) 면 즉시 청산(EARLY_STOP_DEFER_HARD_STOP).
    판정봉이 이미 hard stop 이하면 유예하지 않는다.
  · H50 보류 예외 (2026-11-09): 손절 판정 시점에 **이미** 반대 HY 플래그가 확정돼 H50 이 청산만 보류 중
    (state.h50_hold_active ∧ H50 이 기록한 보유 방향 = 현재 포지션 방향)이면 유예하지 않고 정상 손절한다
    (NOT_DEFERRED / reason=H50_HOLD). 같은 틱에 H50 이 새로 설정되는 경우는 worker 실행순서상(리스크 관리가
    T+3 판정보다 먼저) 손절 판정 때 아직 보류 상태가 아니므로 포함되지 않는다 — 순서는 바꾸지 않는다.
  · MU 상태와 무관. B3 손절·TP·trailing·after-TP1 stop·H50·C1·반대플래그·강제청산 등은 대상이 아니다.
이 모듈은 판정·상태·기록만 한다. 주문은 worker 가 기존 execute_exit 로 낸다.
기존 MU CONFIRM 진입 1봉 유예(2026-11-05, 2f00cf0)는 이 규칙으로 **대체**되어 제거됐다.
"""
from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config, ledger

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()

VERSION = "EARLY_STOP_DEFER_V2_20261109"
LEDGER_FILENAME = "macd2_early_stop_defer_ledger.csv"
LEDGER_COLUMNS = ["trading_date", "event", "at", "symbol", "entry_at", "elapsed_min", "stop_return_pct",
                  "original_stop_pct", "defer_started_at", "defer_until_bar", "hard_stop_pct", "deferred",
                  "reason", "version"]
EVENT_DEFER = "DEFER"
EVENT_RECOVERED = "RECOVERED"
EVENT_STOP_AFTER_DEFER = "STOP_AFTER_DEFER"
EVENT_HARD_STOP = "HARD_STOP"
EVENT_NOT_DEFERRED = "NOT_DEFERRED"
SKIP_DISABLED, SKIP_USED, SKIP_LATE, SKIP_ALREADY_HARD, SKIP_NO_ENTRY_TIME = (
    "DISABLED", "ALREADY_USED", "LATE", "ALREADY_BEYOND_HARD", "NO_ENTRY_TIME")
SKIP_H50_HOLD = "H50_HOLD"


def hard_stop_pct() -> float:
    return float(config.EARLY_STOP_DEFER_HARD_STOP) * 100.0


def entry_time(state, position) -> Optional[datetime]:
    t = getattr(position, "entry_at", None)
    if t is not None:
        return t
    raw = getattr(state, "last_time_window_entry_at", None)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def elapsed_minutes(state, position, now: datetime) -> Optional[float]:
    t = entry_time(state, position)
    if t is None:
        return None
    try:
        return (now - t).total_seconds() / 60.0
    except TypeError:
        return None


def skip_reason(state, position, now: datetime, bar_net_return: float) -> Optional[str]:
    """유예하지 않는 이유. None 이면 유예 대상."""
    if not config.EARLY_STOP_DEFER_ENABLED:
        return SKIP_DISABLED
    if state.early_stop_defer_used or state.early_stop_defer_active:
        return SKIP_USED
    if bar_net_return <= hard_stop_pct():
        return SKIP_ALREADY_HARD
    el = elapsed_minutes(state, position, now)
    if el is None:
        return SKIP_NO_ENTRY_TIME
    if el > float(config.EARLY_STOP_DEFER_MINUTES):
        return SKIP_LATE
    if h50_holding_this_position(state, position):
        return SKIP_H50_HOLD
    return None


def h50_holding_this_position(state, position) -> bool:
    """반대 HY 플래그가 이미 확정돼 H50 이 **이 포지션**의 청산을 보류 중인가 (2026-11-09).
    H50 이 기록한 보유 방향이 현재 포지션 방향과 다르면(남은 상태) False — 기존 유예 그대로."""
    from app.trading.macd2 import small_whipsaw_hold      # 지연 import: 모듈 의존 최소화
    if not small_whipsaw_hold.is_holding(state):
        return False
    # 소유권: worker._advance_h50_hold 의 stale 판정과 같은 규칙(둘 다 알려져 있는데 다르면 이전 포지션의 잔재)
    owner, cur = int(getattr(state, "h50_owner_epoch", 0) or 0), int(getattr(state, "position_epoch", 0) or 0)
    if owner and cur and owner != cur:
        return False
    held = small_whipsaw_hold.held_direction(state)
    sym = getattr(position, "symbol", None)
    if held is None or sym is None:
        return False
    if sym == config.LONG_SYMBOL:
        return held.value == "UP_RED"
    if sym == config.INVERSE_SYMBOL:
        return held.value == "DOWN_BLUE"
    return False


def start(state, *, now: datetime, until_bar: datetime, original_stop_pct: Optional[float]) -> None:
    """유예 시작 — used 를 같이 세워 이 포지션에서 다시는 유예하지 않는다(2회 유예 불가)."""
    state.early_stop_defer_active = True
    state.early_stop_defer_used = True
    state.early_stop_defer_started_at = now.isoformat()
    state.early_stop_defer_until_bar = until_bar.isoformat()
    state.early_stop_defer_original_stop_pct = original_stop_pct


def finish(state) -> None:
    """대기 종료(다음 봉 판정 완료 / hard stop / 청산). used 는 유지한다."""
    state.early_stop_defer_active = False


def clear(state) -> None:
    """포지션 수명 종료 — worker._clear_position_scoped_state 에서만 부른다."""
    state.early_stop_defer_used = False
    state.early_stop_defer_active = False
    state.early_stop_defer_started_at = None
    state.early_stop_defer_until_bar = None
    state.early_stop_defer_original_stop_pct = None


def ledger_path() -> Path:
    return Path(ledger.SIGNAL_LEDGER_PATH).parent / LEDGER_FILENAME


def record(event: str, *, state, position, now: datetime, stop_return_pct: Optional[float] = None,
           reason: str = "") -> None:
    """이벤트 원장 1행 append (기존 원장 컬럼 무변경). 실패해도 주문 경로에 영향 없음."""
    try:
        el = elapsed_minutes(state, position, now)
        et = entry_time(state, position)
        row = {"trading_date": now.strftime("%Y%m%d"), "event": event, "at": now.isoformat(),
               "symbol": getattr(position, "symbol", ""), "entry_at": et.isoformat() if et else "",
               "elapsed_min": "" if el is None else round(el, 2),
               "stop_return_pct": "" if stop_return_pct is None else round(float(stop_return_pct), 4),
               "original_stop_pct": "" if state.early_stop_defer_original_stop_pct is None
               else round(float(state.early_stop_defer_original_stop_pct), 4),
               "defer_started_at": state.early_stop_defer_started_at or "",
               "defer_until_bar": state.early_stop_defer_until_bar or "",
               "hard_stop_pct": round(hard_stop_pct(), 4),
               "deferred": event in (EVENT_DEFER, EVENT_RECOVERED, EVENT_STOP_AFTER_DEFER, EVENT_HARD_STOP),
               "reason": reason, "version": VERSION}
        p = ledger_path()
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.exists() or p.stat().st_size == 0
            with open(p, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
                if new:
                    w.writeheader()
                w.writerow(row)
    except Exception:  # noqa: BLE001
        logger.exception("[MACD2][EARLY_STOP_DEFER] 원장 기록 실패 -- 주문 경로 무영향")


def load_rows() -> list[dict[str, Any]]:
    p = ledger_path()
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def next_bar_end(bar_close_at: datetime) -> datetime:
    """방금 판정한 완성봉 다음 3분봉의 완성 시각."""
    return bar_close_at + timedelta(minutes=3)
