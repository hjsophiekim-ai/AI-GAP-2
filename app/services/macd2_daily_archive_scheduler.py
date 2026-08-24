"""
macd2_daily_archive_scheduler.py — Streamlit 프로세스 안에서 영업일 KST 20:30
이후(NXT 20:00 마감 + 여유) 그날의 MACD2 리서치 아카이브(macd2_daily_archiver)를
저장하고, 이어서 GitHub 60일 롤링 동기화(github_analysis_sync)를 "하루 한 번만"
시도하는 백그라운드 스레드.

app.services.minute_bar_archive_scheduler / hynix_auto_trade_scheduler와 동일한
패턴(threading.Thread + 전역 싱글턴 + ensure_*_running() 멱등 시작)을 그대로
따른다.

절대 원칙: 이 스레드는 app.trading.* 를 전혀 import하지 않는다(macd2_daily_
archiver 자체가 트레이딩 코드를 호출하지 않으므로 이 파일도 마찬가지) --
실거래 Worker/시세조회/플래그생성/주문과 100% 분리돼 있고, 이 스레드가 어떤
예외를 만나도 그 예외는 이 파일 밖으로 절대 전파되지 않는다(매 tick을 통째로
try/except).
"""

from __future__ import annotations

import threading
from datetime import datetime, time as dtime, timedelta
from typing import Optional

from app.logger import logger
from app.utils.time_utils import kst_now

CHECK_INTERVAL_SECONDS = 900.0  # 15분마다 확인
ARCHIVE_TRIGGER_TIME = dtime(20, 30)  # NXT(08:00-20:00) 마감 이후

_lock = threading.Lock()
_instance: Optional["Macd2DailyArchiveThread"] = None


def _kis_real_client():
    from app.trading.kis_client import create_kis_client

    return create_kis_client(mode="real")


class Macd2DailyArchiveThread(threading.Thread):
    def __init__(self, interval_seconds: float = CHECK_INTERVAL_SECONDS):
        super().__init__(daemon=True, name="Macd2DailyArchiveWatcher")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._last_run_date: Optional[str] = None
        self.last_result: Optional[dict] = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("[Macd2DailyArchive] 백그라운드 스레드 시작(%.0f초 주기, 트리거 %s KST)", self.interval_seconds, ARCHIVE_TRIGGER_TIME)
        while not self._stop_event.is_set():
            try:
                self._tick_if_due()
            except Exception as exc:  # never let this thread die, and never let it affect anything else
                logger.warning("[Macd2DailyArchive] tick 처리 중 예외(다음 주기에 재시도): %r", exc)
            self._stop_event.wait(self.interval_seconds)
        logger.info("[Macd2DailyArchive] 백그라운드 스레드 종료")

    def _tick_if_due(self) -> None:
        now = kst_now()
        if now.weekday() >= 5:
            return
        if now.time() < ARCHIVE_TRIGGER_TIME:
            return
        today_str = now.strftime("%Y%m%d")
        if self._last_run_date == today_str:
            return
        self._last_run_date = today_str  # set BEFORE running -- a failure must not retry every 15min forever today

        from app.services.macd2_daily_archiver import run_daily_archive
        from app.services import github_analysis_sync

        client = _kis_real_client()
        if client is None:
            logger.warning("[Macd2DailyArchive] KIS real client 생성 실패 -- 오늘은 스킵, 내일 재시도")
            return

        yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        archive_result = {}
        for d in (today_str, yesterday):
            try:
                archive_result[d] = run_daily_archive(client, d)
            except Exception as exc:  # run_daily_archive already isolates internally; this is pure defense-in-depth
                logger.warning("[Macd2DailyArchive] run_daily_archive(%s) 예외: %r", d, exc)

        try:
            # dry_run=False here only EXPRESSES INTENT -- github_analysis_sync
            # itself refuses to push unless GITHUB_ANALYSIS_SYNC_ENABLE_PUSH=true
            # is ALSO set in the environment (see its run_sync docstring: two
            # independent switches must both agree). Until that env var is set
            # on Render, this call is silently forced back to a dry run.
            sync_result = github_analysis_sync.run_sync(dry_run=False)
        except Exception as exc:
            sync_result = {"error": repr(exc)}
        logger.info(
            "[Macd2DailyArchive] 실행 완료: archive=%s sync_dry_run=%s sync_error=%s",
            list(archive_result.keys()), sync_result.get("effective_dry_run"), sync_result.get("error"),
        )
        self.last_result = {"archive": archive_result, "sync": sync_result}


def ensure_macd2_daily_archive_thread_running(interval_seconds: float = CHECK_INTERVAL_SECONDS) -> Macd2DailyArchiveThread:
    global _instance
    with _lock:
        if _instance is None or not _instance.is_alive():
            _instance = Macd2DailyArchiveThread(interval_seconds=interval_seconds)
            _instance.start()
        return _instance


def is_archive_thread_running() -> bool:
    return _instance is not None and _instance.is_alive()
