"""잔고조회(get_positions) 건강 상태 + circuit breaker (2026-10-21).

왜 필요한가
-----------
worker 의 ``reconcile_position_state`` 는 매 tick(보유 중이면 5초마다) 잔고를
조회하고, 실패하면 그 tick 을 POSITION_DATA_ERROR 로 끝낸다. 플래그 평가/원장
기록은 그 조기 return 경로 안에서도 이미 돌지만(2026-08-31·09-11·10-20), 실패
자체에 대해서는 아무 장치가 없었다:

  * 실패해도 다음 tick 에 똑같이 다시 부른다 -- KIS 초당한도(EGW00215)가
    원인이면 호출을 더 얹어 상황을 악화시킨다(get_balance 는 내부에서도
    rate-limit 재시도를 최대 8회 한다).
  * 연결/세션 오류(RemoteDisconnected·timeout)여도 같은 KISClient(같은
    requests.Session)를 계속 쓴다.
  * 실패 횟수 / 마지막 성공 시각 / 복구 사실이 어디에도 남지 않는다.

여기서 하는 일은 **잔고조회 호출 빈도와 클라이언트 수명 관리뿐**이다.
판정(POSITION_DATA_ERROR -> 신규주문 차단, 보유 포지션 보호 청산)은 기존
worker 경로가 그대로 한다. 전략/플래그/청산 규칙은 한 줄도 바꾸지 않는다.

상태는 ``RuntimeState`` 의 ``position_feed_*`` 필드에 둔다(UI·재시작 후 진단용).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from app.logger import logger

#: 이 횟수만큼 연속 실패하면 breaker 를 연다(그 전에는 매 tick 그대로 조회).
OPEN_AFTER_FAILURES = 3
#: breaker 가 열린 뒤 재시도 간격: BASE * 2^(n-OPEN_AFTER), 최대 MAX.
BACKOFF_BASE_SEC = 5.0
BACKOFF_MAX_SEC = 60.0
#: 연결/세션 오류일 때 새 KISClient 로 갈아 끼우는 최소 간격.
CLIENT_REFRESH_MIN_INTERVAL_SEC = 30.0
#: 보유 중 잔고조회 장애 CRITICAL 경고 간격.
HOLDING_ALERT_INTERVAL_SEC = 60.0

FEED_OK = "OK"
FEED_ERROR = "ERROR"            # 이번 tick 에 실제로 불렀고 실패했다
FEED_STALE = "STALE"            # breaker 가 열려 이번 tick 에는 부르지 않았다
CIRCUIT_OPEN_ERROR = "POSITION_FEED_CIRCUIT_OPEN"
HOLDING_ALERT = "POSITION_FEED_DOWN_WHILE_HOLDING"

_CONNECTION_MARKERS = (
    "RemoteDisconnected", "ConnectionError", "ConnectionResetError", "Connection aborted",
    "ConnectTimeout", "ReadTimeout", "Timeout", "timed out", "ProtocolError",
    "BrokenPipe", "Max retries exceeded",
)


def _parse(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def is_connection_error(error: str) -> bool:
    """세션/연결 계열이면 True -- 새 Session 이 도움이 되는 경우다.
    rate-limit(EGW00201/EGW00215) 은 여기에 속하지 않는다(새 클라이언트도 같은
    appkey 라 한도가 같다) -> 기다리는 것만이 해법이다."""
    return any(m in (error or "") for m in _CONNECTION_MARKERS)


def backoff_sec(failures: int) -> float:
    if failures < OPEN_AFTER_FAILURES:
        return 0.0
    return min(BACKOFF_BASE_SEC * (2 ** (failures - OPEN_AFTER_FAILURES)), BACKOFF_MAX_SEC)


def is_open(state) -> bool:
    return int(getattr(state, "position_feed_failures", 0) or 0) >= OPEN_AFTER_FAILURES


def blocked_until(state, now: datetime) -> Optional[datetime]:
    """breaker 가 열려 있고 아직 재시도 시각 전이면 그 시각, 아니면 None."""
    if not is_open(state):
        return None
    retry_at = _parse(getattr(state, "position_feed_retry_at", None))
    if retry_at is not None and now < retry_at:
        return retry_at
    return None


def maybe_refresh_client(broker, state, now: datetime) -> bool:
    """마지막 실패가 연결/세션 계열이면 새 KISClient(새 Session)로 교체한다.
    rate-limit 이면 교체하지 않는다. 교체 간격 제한이 있다."""
    if not is_connection_error(str(getattr(state, "position_feed_last_error", "") or "")):
        return False
    last = _parse(getattr(state, "position_feed_client_refreshed_at", None))
    if last is not None and (now - last).total_seconds() < CLIENT_REFRESH_MIN_INTERVAL_SEC:
        return False
    refresh = getattr(broker, "refresh_kis_session", None)
    if refresh is None:
        return False
    try:
        ok = bool(refresh())
    except Exception as exc:                              # pragma: no cover - 방어
        logger.error("[MACD2][POSITION] 새 KISClient 생성 실패: %r", exc)
        ok = False
    state.position_feed_client_refreshed_at = now.isoformat()
    if ok:
        state.position_feed_client_refreshes = int(state.position_feed_client_refreshes or 0) + 1
        logger.warning("[MACD2][POSITION] 연결 오류 -> 새 KISClient/Session 으로 교체 (누적 %d 회)",
                       state.position_feed_client_refreshes)
    return ok


def note_success(state, now: datetime) -> None:
    failures = int(getattr(state, "position_feed_failures", 0) or 0)
    if failures:
        since = _parse(getattr(state, "position_feed_first_failure_at", None))
        down = f"{(now - since).total_seconds():.0f}s" if since else "?"
        logger.warning(
            "[MACD2][POSITION] POSITION_DATA_RECOVERED -- 잔고조회 정상화 (연속실패 %d 회, 장애 %s)",
            failures, down)
    state.position_feed_failures = 0
    state.position_feed_first_failure_at = None
    state.position_feed_retry_at = None
    state.position_feed_last_success_at = now.isoformat()
    state.position_feed_status = FEED_OK


def note_failure(state, now: datetime, error: str, *, count: bool = True) -> None:
    """``count=False`` 는 주문 경로의 강제 조회(같은 tick 안에서 0.5초 간격으로
    몇 번씩 재조회한다)용이다 -- 오류는 기록하되 breaker 연속실패에는 넣지 않는다.
    그러지 않으면 한 tick 의 재조회 묶음만으로 backoff 가 60초까지 치솟아 다음
    tick 의 pending 재시도(2026-09-29 계약)를 막는다."""
    if not count:
        state.position_feed_last_error = str(error)[:300]
        state.position_feed_last_error_at = now.isoformat()
        state.position_feed_status = FEED_ERROR
        logger.warning("[MACD2][POSITION] 주문 직전 잔고조회 실패(강제 조회) error=%s", str(error)[:200])
        return
    failures = int(getattr(state, "position_feed_failures", 0) or 0) + 1
    state.position_feed_failures = failures
    if failures == 1:
        state.position_feed_first_failure_at = now.isoformat()
    state.position_feed_last_error = str(error)[:300]
    state.position_feed_last_error_at = now.isoformat()
    state.position_feed_status = FEED_ERROR
    wait = backoff_sec(failures)
    state.position_feed_retry_at = (now + timedelta(seconds=wait)).isoformat() if wait > 0 else None
    log = logger.error if failures >= OPEN_AFTER_FAILURES else logger.warning
    log("[MACD2][POSITION] 잔고조회 실패 streak=%d %s error=%s", failures,
        f"breaker OPEN, 다음 시도 {wait:.0f}s 후" if wait > 0 else "", str(error)[:200])


def note_skipped(state) -> None:
    state.position_feed_status = FEED_STALE


def alert_if_holding(state, now: datetime) -> None:
    """breaker 가 열린 채 포지션을 들고 있으면 CRITICAL(60초 간격). 경고 전용 --
    자동청산은 하지 않는다(보유 포지션 보호 청산은 기존 POSITION_DATA_ERROR
    경로가 마지막 성공 잔고 기준으로 그대로 판단한다)."""
    pos = getattr(state, "position", None)
    if pos is None or int(getattr(pos, "quantity", 0) or 0) <= 0 or not is_open(state):
        return
    last = _parse(getattr(state, "position_feed_alert_at", None))
    if last is not None and (now - last).total_seconds() < HOLDING_ALERT_INTERVAL_SEC:
        return
    state.position_feed_alert_at = now.isoformat()
    logger.critical(
        "[MACD2][%s] 잔고조회가 %d 회 연속 실패한 채 포지션 보유 중 -- %s %s주 "
        "(마지막 성공=%s, 오류=%s). 신규진입은 차단됐고 플래그 계산은 계속된다. "
        "자동청산은 하지 않는다 -- 수동 확인 필요.",
        HOLDING_ALERT, int(state.position_feed_failures or 0), pos.symbol, pos.quantity,
        state.position_feed_last_success_at, state.position_feed_last_error)


def diag(state) -> dict[str, Any]:
    return {
        "position_feed_status": getattr(state, "position_feed_status", None),
        "position_feed_failures": int(getattr(state, "position_feed_failures", 0) or 0),
        "position_feed_last_success_at": getattr(state, "position_feed_last_success_at", None),
        "position_feed_last_error": getattr(state, "position_feed_last_error", None),
        "position_feed_retry_at": getattr(state, "position_feed_retry_at", None),
        "position_feed_client_refreshes": int(getattr(state, "position_feed_client_refreshes", 0) or 0),
    }
