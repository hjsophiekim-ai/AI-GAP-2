"""
hynix_auto_trade_scheduler.py — Streamlit 세션(브라우저 탭)과 무관하게 서버 프로세스
안에서 3분마다 하이닉스⇄인버스 Enhanced 자동매매 사이클을 실행하는 백그라운드 스레드.

Dynamic Exit Watcher(1초 주기, 이미 보유 중인 포지션의 TP/SL만 감시)와는 역할이
다르다 — 이 스레드는 신규 진입/스위칭/강제청산 "판단"(update_hynix_auto_trade_loop)을
3분마다 수행한다. 브라우저 탭이 하나도 열려있지 않아도, 서버 프로세스가 살아있는
한 auto_trade_on 상태를 계속 확인하며 동작한다.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Optional

from app.logger import logger
from app.services.hynix_switch_state import load_state

DEFAULT_INTERVAL_SECONDS = 180.0

_status_lock = threading.Lock()
_status = {
    "last_cycle_started_at": None,
    "last_cycle_completed_at": None,
    "next_cycle_at": None,
    "cycle_count_today": 0,
    "_cycle_count_date": None,
    "last_cycle_result_summary": None,
    "restart_count": 0,
}


def get_status() -> dict:
    """UI가 표시할 상태 스냅샷. cycle_thread_alive는 항상 스레드 객체에서 실시간으로 확인한다."""
    with _status_lock:
        snap = {k: v for k, v in _status.items() if not k.startswith("_")}
    snap["cycle_thread_alive"] = is_cycle_thread_running()
    return snap


class HynixAutoTradeCycleThread(threading.Thread):
    def __init__(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS):
        super().__init__(daemon=True, name="HynixAutoTradeCycle")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("[HynixAutoTradeCycle] 백그라운드 사이클 스레드 시작(%.0f초 주기)", self.interval_seconds)
        while not self._stop_event.is_set():
            self._run_cycle_if_enabled()
            with _status_lock:
                _status["next_cycle_at"] = (datetime.now() + timedelta(seconds=self.interval_seconds)).isoformat()
            self._stop_event.wait(self.interval_seconds)
        logger.info("[HynixAutoTradeCycle] 백그라운드 사이클 스레드 종료")

    def _run_cycle_if_enabled(self) -> None:
        from app.services.hynix_switch_engine import update_hynix_auto_trade_loop

        state = load_state()
        if not state.get("auto_trade_on") or state.get("stopped"):
            return

        today = datetime.now().strftime("%Y%m%d")
        started_at = datetime.now()
        with _status_lock:
            _status["last_cycle_started_at"] = started_at.isoformat()
        try:
            result = update_hynix_auto_trade_loop(mode=state.get("mode"))
            trace = result.get("pipeline_trace") or {}
            summary = {
                "prediction_signal": trace.get("prediction_signal"),
                "stopped_stage": trace.get("stopped_stage"),
                "skipped": bool(result.get("skipped", False)),
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("[HynixAutoTradeCycle] 사이클 실행 실패: %s", exc)
            summary = {"error": str(exc)}
        completed_at = datetime.now()
        with _status_lock:
            _status["last_cycle_completed_at"] = completed_at.isoformat()
            _status["last_cycle_result_summary"] = summary
            if _status["_cycle_count_date"] != today:
                _status["_cycle_count_date"] = today
                _status["cycle_count_today"] = 0
            _status["cycle_count_today"] += 1


_cycle_lock = threading.Lock()
_cycle_instance: Optional[HynixAutoTradeCycleThread] = None


def ensure_cycle_thread_running(interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> HynixAutoTradeCycleThread:
    """사이클 스레드가 없거나 죽어 있으면 (재)시작한다. 이미 살아있으면 그대로 반환."""
    global _cycle_instance
    with _cycle_lock:
        if _cycle_instance is None or not _cycle_instance.is_alive():
            if _cycle_instance is not None:
                with _status_lock:
                    _status["restart_count"] += 1
                logger.warning("[HynixAutoTradeCycle] 스레드가 죽어있어 재시작합니다")
            _cycle_instance = HynixAutoTradeCycleThread(interval_seconds=interval_seconds)
            _cycle_instance.start()
        return _cycle_instance


def stop_cycle_thread() -> None:
    global _cycle_instance
    with _cycle_lock:
        if _cycle_instance is not None:
            _cycle_instance.stop()
            _cycle_instance = None


def is_cycle_thread_running() -> bool:
    return _cycle_instance is not None and _cycle_instance.is_alive()
