"""
minute_bar_archive_scheduler.py — Streamlit 세션(브라우저 탭)과 무관하게 서버
프로세스 안에서 영업일 장 종료 후 16:00(KST)에 그날의 하이닉스/레버리지/인버스
1분봉을 자동 저장하는 백그라운드 스레드.

app.services.hynix_auto_trade_scheduler와 동일한 패턴(threading.Thread +
전역 싱글턴 + ensure_*_running() 멱등 시작 함수)을 그대로 따른다 — 새로운
스레드/락 인프라를 또 만들지 않는다. 실제 조회/저장 로직은
app.services.minute_bar_archiver에 있고 이 파일은 "하루에 한 번, 16:00 이후에
실행"이라는 스케줄링만 담당한다.

모든 시각 판단은 kst_now()(Asia/Seoul) 기준이다 — Render 등 UTC 서버에서 naive
datetime.now()를 쓰면 16:00 트리거가 실제 KST 16:00과 어긋난다(hynix_auto_trade
_scheduler.py가 2026-07-16에 겪은 것과 동일한 부류의 버그이므로 처음부터
kst_now()만 사용한다).

서버가 재시작돼도(Render 재배포 등) 다음 시작 시 이 스레드가 다시 뜨고,
minute_bar_archiver.run_archive()가 자체적으로 "최근 LOOKBACK_CALENDAR_DAYS
안의 누락 거래일"을 자동 보충하므로, 재시작으로 정확히 16:00 트리거 한 번을
놓쳐도 다음 체크 주기에 그 날짜가 그대로 채워진다.
"""

from __future__ import annotations

import threading
from datetime import datetime, time as dtime, timedelta
from typing import Optional

from app.logger import logger
from app.trading.macd2 import config
from app.utils.time_utils import kst_now

CHECK_INTERVAL_SECONDS = 900.0  # 15분마다 "오늘 16시 지났고 아직 저장 안 됐나" 확인
ARCHIVE_TRIGGER_TIME = dtime(16, 0)

_lock = threading.Lock()
_instance: Optional["MinuteBarArchiveThread"] = None


def _kis_real_client():
    from app.trading.kis_client import create_kis_client

    return create_kis_client(mode="real")


class MinuteBarArchiveThread(threading.Thread):
    def __init__(self, interval_seconds: float = CHECK_INTERVAL_SECONDS):
        super().__init__(daemon=True, name="MinuteBarArchiveWatcher")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._last_run_date: Optional[str] = None  # KST YYYYMMDD this process already archived today

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("[MinuteBarArchive] 백그라운드 스레드 시작(%.0f초 주기, 트리거 %s KST)", self.interval_seconds, ARCHIVE_TRIGGER_TIME)
        while not self._stop_event.is_set():
            self._tick_if_due()
            self._stop_event.wait(self.interval_seconds)
        logger.info("[MinuteBarArchive] 백그라운드 스레드 종료")

    def _tick_if_due(self) -> None:
        now = kst_now()
        if now.weekday() >= 5:
            return
        if now.time() < ARCHIVE_TRIGGER_TIME:
            return
        today_str = now.strftime("%Y%m%d")
        if self._last_run_date == today_str:
            return  # 이 프로세스 안에서 오늘은 이미 실행함 -- 15분마다 중복 실행 방지
        try:
            from app.services.minute_bar_archiver import run_archive

            client = _kis_real_client()
            if client is None:
                logger.warning("[MinuteBarArchive] KIS real client 생성 실패 -- 다음 주기에 재시도")
                return
            results = run_archive(client, source="scheduler")
            saved = [r for r in results if r.get("status") == "saved"]
            logger.info("[MinuteBarArchive] 실행 완료: %d개 날짜 신규 저장 %s", len(saved), [r["date"] for r in saved])
            self._last_run_date = today_str
        except Exception as exc:  # pragma: no cover - real network path
            logger.warning("[MinuteBarArchive] 실행 실패(다음 주기에 재시도): %r", exc)


def ensure_minute_bar_archive_thread_running(interval_seconds: float = CHECK_INTERVAL_SECONDS) -> MinuteBarArchiveThread:
    """스레드가 없거나 죽어 있으면 (재)시작한다. 이미 살아있으면 그대로 반환."""
    global _instance
    with _lock:
        if _instance is None or not _instance.is_alive():
            _instance = MinuteBarArchiveThread(interval_seconds=interval_seconds)
            _instance.start()
        return _instance


def is_archive_thread_running() -> bool:
    return _instance is not None and _instance.is_alive()
