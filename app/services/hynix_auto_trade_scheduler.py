"""
hynix_auto_trade_scheduler.py — Streamlit 세션(브라우저 탭)과 무관하게 서버 프로세스
안에서 3분마다 하이닉스⇄인버스 Enhanced 자동매매 사이클을 실행하는 백그라운드 스레드.

Dynamic Exit Watcher(1초 주기, 이미 보유 중인 포지션의 TP/SL만 감시)와는 역할이
다르다 — 이 스레드는 신규 진입/스위칭/강제청산 "판단"(update_hynix_auto_trade_loop)을
3분마다 수행한다. 브라우저 탭이 하나도 열려있지 않아도, 서버 프로세스가 살아있는
한 auto_trade_on 상태를 계속 확인하며 동작한다.

모든 시각 판단은 kst_now()(Asia/Seoul) 기준이다 — Render 등 UTC로 배포된 서버에서
naive datetime.now()를 쓰면 "지금이 몇 시인지" 자체가 서버 타임존만큼 어긋나 14:50
신규매수 차단/15:15 강제청산/일일 사이클 카운트 리셋이 실제 KST 시각과 무관하게
잘못된 시점에 발동한다(2026-07-16 실측: Render UTC 23:12를 그대로 "23:12"로 판정해
KST 08:12임에도 14:50 이후로 오판, cycle_count_today가 밤새 계속 누적되어 284까지
증가). 또한 08:50~15:30(KST) 운영창 밖에서는 시세/주문/계좌조회를 하지 않고
heartbeat만 유지한다 — 장외에 3분마다 전체 사이클을 계속 돌리는 것 자체가 불필요한
KIS API 호출과 카운터 누적의 원인이었다.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, time as _dtime
from typing import Optional

from app.logger import logger
from app.services.hynix_switch_state import load_state
from app.utils.time_utils import kst_now
from app.utils.data_paths import SCHEDULER_HEARTBEAT_PATH

DEFAULT_INTERVAL_SECONDS = 180.0
FAST_WATCHER_INTERVAL_SECONDS = 30.0
# 요구사항(2026-07-20 최종) — auto_trade_on이면 이 워처가 5초 주기로 돈다.
# 5초 틱(run_early_trend_fast_feed_tick)이 WOC LIVE 신규진입을 소유한다.
# 별도의 새 스레드/락/heartbeat 인프라를 또 만들지 않고, 기존 30초
# 워처의 대기시간만 상태에 따라 동적으로 줄인다 — 무거운 전체 재계산(30초 주기
# run_fast_trend_watcher_tick)은 진단 전용으로 유지하고, 그 사이 5초 틱은
# 시세 갱신 + evaluate_range_weighted_entry → run_switch_or_entry를 수행한다.
EARLY_DETECTOR_FAST_INTERVAL_SECONDS = 5.0
# 요구사항(2026-07-16) — 장 마감 후에도 오늘 저장된 1분봉으로 EOD regime 분석을
# 계속 표시하되(요구사항3), 장외 heartbeat마다(수 초~수 분 간격) 매번 KIS를 다시
# 조회하는 것은 불필요한 API 호출이므로 최소 이 간격(초)만큼은 재사용한다.
_EOD_REGIME_REFRESH_INTERVAL_SECONDS = 900.0

_status_lock = threading.Lock()
_status = {
    "last_cycle_started_at": None,
    "last_cycle_completed_at": None,
    "next_cycle_at": None,
    "cycle_count_today": 0,
    "_cycle_count_date": None,
    "last_cycle_result_summary": None,
    "restart_count": 0,
    "last_heartbeat_at": None,
    "within_operating_window": None,
    "last_heartbeat_only_at": None,
}
_fast_status = {
    "last_started_at": None,
    "last_completed_at": None,
    "next_run_at": None,
    "run_count_today": 0,
    "_run_count_date": None,
    "last_result_summary": None,
    "restart_count": 0,
    "last_heartbeat_at": None,
    "within_operating_window": None,
    # 5s Worker cadence diagnostics (last_tick_at + recent ~10 intervals).
    "last_tick_at": None,
    "recent_tick_intervals_sec": [],
    "_tick_at_history": [],
}

_FAST_TICK_HISTORY_MAX = 11  # 10 intervals require 11 timestamps


def _record_fast_status_tick(completed_at: datetime) -> None:
    """Update in-memory Fast Worker last_tick_at and recent interval history."""
    tick_iso = completed_at.isoformat()
    history = list(_fast_status.get("_tick_at_history") or [])
    history.append(tick_iso)
    history = history[-_FAST_TICK_HISTORY_MAX:]
    intervals: list[float] = []
    for i in range(1, len(history)):
        try:
            delta = (
                datetime.fromisoformat(str(history[i]))
                - datetime.fromisoformat(str(history[i - 1]))
            ).total_seconds()
            intervals.append(round(float(delta), 3))
        except Exception:
            continue
    _fast_status["_tick_at_history"] = history
    _fast_status["last_tick_at"] = tick_iso
    _fast_status["recent_tick_intervals_sec"] = intervals[-10:]


def get_status() -> dict:
    """UI가 표시할 상태 스냅샷. cycle_thread_alive는 항상 스레드 객체에서 실시간으로 확인한다."""
    with _status_lock:
        snap = {k: v for k, v in _status.items() if not k.startswith("_")}
    try:
        started = datetime.fromisoformat(str(snap.get("last_cycle_started_at"))) if snap.get("last_cycle_started_at") else None
    except Exception:
        started = None
    try:
        completed = datetime.fromisoformat(str(snap.get("last_cycle_completed_at"))) if snap.get("last_cycle_completed_at") else None
    except Exception:
        completed = None
    snap["cycle_status"] = "RUNNING" if started and (completed is None or started > completed) else "IDLE"
    snap["cycle_thread_alive"] = is_cycle_thread_running()
    snap["fast_trend_watcher"] = get_fast_status()
    return snap


def get_fast_status() -> dict:
    with _status_lock:
        snap = {k: v for k, v in _fast_status.items() if not k.startswith("_")}
    snap["thread_alive"] = is_fast_trend_watcher_running()
    return snap


def _write_heartbeat_file() -> None:
    """스케줄러 상태를 DATA_ROOT(영구 디스크 마운트 시 그쪽)에 파일로 남긴다.

    _status/_fast_status는 프로세스 메모리에만 있어 컨테이너가 재시작되면
    사라진다 — "마지막으로 언제까지 살아있었는지"조차 재시작 직후에는 알 수
    없다. 이 파일은 매 틱(heartbeat-only 포함)마다 갱신되므로, 재시작 직후에도
    이전 프로세스가 언제 마지막으로 응답했는지 화면에서 바로 확인할 수 있다."""
    try:
        with _status_lock:
            snapshot = {"cycle": dict(_status), "fast": dict(_fast_status)}
        SCHEDULER_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = SCHEDULER_HEARTBEAT_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, default=str), encoding="utf-8")
        tmp_path.replace(SCHEDULER_HEARTBEAT_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[HynixAutoTradeScheduler] heartbeat 파일 기록 실패(무해): %s", exc)


def read_heartbeat_file() -> Optional[dict]:
    """디스크에 남은 마지막 heartbeat 스냅샷을 읽는다(현재 프로세스 재시작 여부와
    무관하게 UI에서 "재시작 전 마지막 상태"를 보여주기 위함). 파일이 없거나
    손상됐으면 None."""
    try:
        if not SCHEDULER_HEARTBEAT_PATH.exists():
            return None
        return json.loads(SCHEDULER_HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[HynixAutoTradeScheduler] heartbeat 파일 로드 실패: %s", exc)
        return None


def _reset_cycle_count_if_new_kst_day(now) -> None:
    """KST 날짜가 바뀌면 cycle_count_today를 초기화한다.

    heartbeat-only(장외) 틱에서도 매번 호출되므로, 실제 첫 장전 사이클이 돌기 전에도
    KST 자정이 지나는 즉시 카운터가 0으로 보인다."""
    today = now.strftime("%Y%m%d")
    with _status_lock:
        if _status["_cycle_count_date"] != today:
            _status["_cycle_count_date"] = today
            _status["cycle_count_today"] = 0


def _reset_fast_run_count_if_new_kst_day(now) -> None:
    today = now.strftime("%Y%m%d")
    with _status_lock:
        if _fast_status["_run_count_date"] != today:
            _fast_status["_run_count_date"] = today
            _fast_status["run_count_today"] = 0


_EOD_ANALYSIS_START_TIME = _dtime(15, 30)


def _maybe_refresh_eod_regime(now, state: dict) -> None:
    """요구사항(2026-07-16) — 장 마감 후에도 오늘 저장된 1분봉으로 EOD regime
    분석만(주문 없음) 주기적으로 갱신한다. 너무 자주(heartbeat마다) 다시
    조회하지 않도록 마지막 계산 이후 _EOD_REGIME_REFRESH_INTERVAL_SECONDS가
    지났을 때만 재계산한다."""
    if not state.get("auto_trade_on"):
        return
    existing = (state.get("adaptive_regime_eod") or {}).get("snapshot") or {}
    last_computed_at = existing.get("computed_at")
    if last_computed_at:
        try:
            from datetime import datetime as _datetime

            age_seconds = (now - _datetime.fromisoformat(last_computed_at)).total_seconds()
            if 0 <= age_seconds < _EOD_REGIME_REFRESH_INTERVAL_SECONDS:
                return
        except Exception:
            pass
    try:
        from app.services.hynix_switch_engine import compute_eod_regime_only

        compute_eod_regime_only(mode=state.get("mode"), now=now)
    except Exception as exc:
        logger.debug("[HynixAutoTradeScheduler] EOD regime 분석 실패(무해, 다음 주기에 재시도): %s", exc)


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
                _status["next_cycle_at"] = (kst_now() + timedelta(seconds=self.interval_seconds)).isoformat()
            self._stop_event.wait(self.interval_seconds)
        logger.info("[HynixAutoTradeCycle] 백그라운드 사이클 스레드 종료")

    def _run_cycle_if_enabled(self) -> None:
        from app.services.hynix_switch_engine import update_hynix_auto_trade_loop
        from app.trading.hynix_switch_risk_gate import is_within_operating_window

        now = kst_now()
        _reset_cycle_count_if_new_kst_day(now)
        within_window = is_within_operating_window(now)
        with _status_lock:
            _status["last_heartbeat_at"] = now.isoformat()
            _status["within_operating_window"] = within_window

        state = load_state()
        if not state.get("auto_trade_on") or state.get("stopped"):
            _write_heartbeat_file()
            return

        if not within_window:
            # 장외(08:50 이전/15:30 이후) — 신규 시세/주문/계좌조회를 하지 않고 스레드가
            # 살아있다는 사실(heartbeat)만 기록한다. cycle_count_today는 증가시키지 않는다.
            with _status_lock:
                _status["last_heartbeat_only_at"] = now.isoformat()
            # 요구사항(2026-07-16) — 단, 장 마감 후(15:30 이후)에는 오늘 저장된 1분봉으로
            # EOD(End-Of-Day) regime 분석만(주문 없음) 주기적으로 갱신한다. 08:50 이전
            # (장 시작 전, 아직 오늘 데이터가 없음)에는 실행하지 않는다.
            if now.time() >= _EOD_ANALYSIS_START_TIME:
                _maybe_refresh_eod_regime(now, state)
            _write_heartbeat_file()
            return

        started_at = now
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
        completed_at = kst_now()
        with _status_lock:
            _status["last_cycle_completed_at"] = completed_at.isoformat()
            _status["last_cycle_result_summary"] = summary
            _status["cycle_count_today"] += 1
        _write_heartbeat_file()


class HynixFastTrendWatcherThread(threading.Thread):
    def __init__(self, interval_seconds: float = FAST_WATCHER_INTERVAL_SECONDS):
        super().__init__(daemon=True, name="HynixFastTrendWatcher")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._last_full_tick_at: Optional[object] = None

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def wake(self) -> None:
        """Interrupt the sleep wait so the next Fast Worker tick runs immediately."""
        self._wake_event.set()

    def run(self) -> None:
        logger.info("[HynixFastTrendWatcher] start (%.0fs interval, %.0fs when Early Detector LIVE)",
                    self.interval_seconds, EARLY_DETECTOR_FAST_INTERVAL_SECONDS)
        while not self._stop_event.is_set():
            self._run_if_enabled()
            next_interval = self._next_interval_seconds()
            with _status_lock:
                _fast_status["next_run_at"] = (kst_now() + timedelta(seconds=next_interval)).isoformat()
            # Wait for interval OR explicit wake OR stop.
            self._wake_event.clear()
            triggered = self._wake_event.wait(timeout=max(0.0, float(next_interval)))
            if self._stop_event.is_set():
                break
            if triggered:
                logger.info("[HynixFastTrendWatcher] woken early for deferred-entry completeness")
        logger.info("[HynixFastTrendWatcher] stopped")

    def _fast_cadence_active(self, state: dict) -> bool:
        return bool(
            state.get("auto_trade_on")
            and not state.get("stopped")
        )

    def _next_interval_seconds(self) -> float:
        try:
            state = load_state()
        except Exception:
            return self.interval_seconds
        # Main cycle asked for an immediate Fast Worker tick (deferral / timeout).
        if state.get("force_fast_worker_tick"):
            return 0.0
        if self._fast_cadence_active(state):
            return EARLY_DETECTOR_FAST_INTERVAL_SECONDS
        return self.interval_seconds

    def _run_if_enabled(self) -> None:
        from app.services.hynix_switch_engine import run_fast_trend_watcher_tick, run_early_trend_fast_feed_tick
        from app.trading.hynix_switch_risk_gate import is_within_operating_window

        now = kst_now()
        _reset_fast_run_count_if_new_kst_day(now)
        within_window = is_within_operating_window(now)
        with _status_lock:
            _fast_status["last_heartbeat_at"] = now.isoformat()
            _fast_status["within_operating_window"] = within_window

        state = load_state()
        if not state.get("auto_trade_on") or state.get("stopped"):
            _write_heartbeat_file()
            return

        if not within_window:
            # 장외에는 빠른 추세감시도 시세조회를 하지 않는다(heartbeat만 유지).
            _write_heartbeat_file()
            return

        # 요구사항(2026-07-20 최종) — Early Detector가 LIVE인 동안에는 이 워처가
        # 5초마다 깨어나지만, calculate_enhanced_hynix_prediction_score() 등
        # 무거운 전체 재계산은 여전히 최소 FAST_WATCHER_INTERVAL_SECONDS(30초)
        # 간격으로만 수행한다 — KIS 분봉/계좌 API 호출량을 그대로 유지하기 위함.
        # 그 사이 5초 틱은 가벼운 현재가 2건 조회로 실시간 기울기만 갱신한다.
        fast_cadence_active = self._fast_cadence_active(state)
        due_for_full_tick = (
            not fast_cadence_active
            or self._last_full_tick_at is None
            or (now - self._last_full_tick_at).total_seconds() >= FAST_WATCHER_INTERVAL_SECONDS
        )

        started_at = now
        with _status_lock:
            _fast_status["last_started_at"] = started_at.isoformat()
        try:
            if due_for_full_tick:
                result = run_fast_trend_watcher_tick(mode=state.get("mode"))
                self._last_full_tick_at = now
                summary = {
                    "skipped": bool(result.get("skipped", False)),
                    "reason": result.get("reason"),
                    "direction": (result.get("fast_signal") or {}).get("direction"),
                    "tick_kind": "full",
                }
            else:
                result = run_early_trend_fast_feed_tick(mode=state.get("mode"), now=now)
                summary = {
                    "skipped": bool(result.get("skipped", False)),
                    "reason": result.get("reason") or ((result.get("early_result") or {}).get("reason_code")),
                    "tick_kind": "fast_feed",
                    "entry_owner": ((result.get("early_result") or {}).get("entry_owner")),
                    "order_permission": ((result.get("early_result") or {}).get("order_permission")),
                }
        except Exception as exc:  # noqa: BLE001
            logger.error("[HynixFastTrendWatcher] run failed: %s", exc)
            summary = {"error": str(exc)}
        completed_at = kst_now()
        with _status_lock:
            _fast_status["last_completed_at"] = completed_at.isoformat()
            _fast_status["last_result_summary"] = summary
            _fast_status["run_count_today"] += 1
            _record_fast_status_tick(completed_at)
        _write_heartbeat_file()


_cycle_lock = threading.Lock()
_cycle_instance: Optional[HynixAutoTradeCycleThread] = None
_fast_lock = threading.Lock()
_fast_instance: Optional[HynixFastTrendWatcherThread] = None


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


def ensure_fast_trend_watcher_running(interval_seconds: float = FAST_WATCHER_INTERVAL_SECONDS) -> HynixFastTrendWatcherThread:
    global _fast_instance
    with _fast_lock:
        if _fast_instance is None or not _fast_instance.is_alive():
            if _fast_instance is not None:
                with _status_lock:
                    _fast_status["restart_count"] += 1
                logger.warning("[HynixFastTrendWatcher] dead thread restarting")
            _fast_instance = HynixFastTrendWatcherThread(interval_seconds=interval_seconds)
            _fast_instance.start()
        return _fast_instance


def wake_fast_trend_watcher() -> bool:
    """Wake the Fast Worker sleep wait so the next diagnostics tick runs ASAP.

    Used for advisory snapshot refresh only — Fast Worker never owns live
    new-entry orders when actual_entry_engine is WEIGHTED_ORDER_CONTROLLER_LIVE.
    """
    with _fast_lock:
        if _fast_instance is None or not _fast_instance.is_alive():
            return False
        _fast_instance.wake()
        return True


def ensure_auto_trade_background_threads(
    cycle_interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    fast_interval_seconds: float = FAST_WATCHER_INTERVAL_SECONDS,
) -> dict:
    """Start both Hynix auto-trade background loops and return their liveness.

    The Streamlit page has several controls that can call st.rerun() before the
    lower-page bootstrap block is reached. Keeping a single top-level helper
    prevents a manual resume from only changing state while the actual cycle
    threads remain absent.
    """
    cycle = ensure_cycle_thread_running(interval_seconds=cycle_interval_seconds)
    fast = ensure_fast_trend_watcher_running(interval_seconds=fast_interval_seconds)
    return {
        "cycle_thread_alive": cycle.is_alive(),
        "fast_thread_alive": fast.is_alive(),
    }


def stop_fast_trend_watcher() -> None:
    global _fast_instance
    with _fast_lock:
        if _fast_instance is not None:
            _fast_instance.stop()
            _fast_instance = None


def is_fast_trend_watcher_running() -> bool:
    return _fast_instance is not None and _fast_instance.is_alive()
