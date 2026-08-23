"""
mem_probe.py — 2026-08-23 주말 Render 메모리 증가 원인 계측용 순수 관찰(read-only)
백그라운드 스레드.

어떤 거래/상태 로직도 호출·수정하지 않는다 — 이미 존재하는 공개 조회 함수
(get_service().supervisor_status(), get_fast_status(), load_state() 등)만 읽고,
threading.enumerate()/gc.get_objects()로 이미 살아있는 객체만 들여다본다. 원인이
확인되면 이 파일과 streamlit_app.py의 ensure_mem_probe_running() 호출 한 줄을
제거한다 — 상시 운영 코드가 아니라 임시 진단 도구다.

다른 백그라운드 스레드들(hynix_auto_trade_scheduler, minute_bar_archive_scheduler
등)과 동일한 패턴(threading.Thread + 전역 싱글턴 + 멱등 ensure_*_running())을
따른다.
"""

from __future__ import annotations

import gc
import threading
from typing import Optional

from app.logger import logger
from app.utils.time_utils import kst_now

PROBE_INTERVAL_SECONDS = 300.0  # 5분

_lock = threading.Lock()
_instance: Optional["MemProbeThread"] = None

_THREAD_NAME_GROUPS = (
    "macd2-quote-updater",
    "macd2-history-updater",
    "macd2-worker",
    "tsla-auto-quote-updater",
    "tsla-auto-history-updater",
    "mu-macd-worker",
    "mu-macd-ws",
    "HynixAutoTradeCycle",
    "HynixFastTrendWatcher",
    "MinuteBarArchiveWatcher",
    "TwShadowForwardTest",
    "MemProbe",
    "MainThread",
)


def _read_rss_kb() -> Optional[float]:
    """Render 컨테이너는 Linux이므로 /proc/self/status를 우선 사용 — 새 의존성
    없이(psutil 미설치) 실측 RSS를 얻는다."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1])  # kB
    except Exception:
        pass
    try:
        import resource

        return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)  # Linux: KB
    except Exception:
        return None


def _thread_breakdown() -> dict:
    counts: dict[str, int] = {}
    for t in threading.enumerate():
        name = t.name or "?"
        matched = next(
            (g for g in _THREAD_NAME_GROUPS if name == g or name.startswith(g)), None
        )
        key = matched or name
        counts[key] = counts.get(key, 0) + 1
    return counts


def _gc_instance_counts() -> dict:
    """gc.get_objects() 스캔만으로 살아있는 인스턴스 수를 센다 — 대상 클래스에
    등록/추적 코드를 추가하지 않는다."""
    targets: dict[str, Optional[type]] = {}

    try:
        from app.trading.macd2.market_data import MarketDataService as _Macd2MD
        from app.trading.macd2.worker import Macd2Worker as _Macd2W

        targets["macd2_market_data"] = _Macd2MD
        targets["macd2_worker"] = _Macd2W
    except Exception:
        targets["macd2_market_data"] = None
        targets["macd2_worker"] = None

    try:
        from app.trading.tsla_auto.market_data import MarketDataService as _TslaMD
        from app.trading.tsla_auto.worker import TslaAutoWorker as _TslaW

        targets["tsla_market_data"] = _TslaMD
        targets["tsla_worker"] = _TslaW
    except Exception:
        targets["tsla_market_data"] = None
        targets["tsla_worker"] = None

    try:
        from app.trading.mu_macd.market_data import MUMarketDataService as _MuMD
        from app.trading.mu_macd.service import MUMacdService as _MuSvc

        targets["mu_macd_market_data"] = _MuMD
        targets["mu_macd_service"] = _MuSvc
    except Exception:
        targets["mu_macd_market_data"] = None
        targets["mu_macd_service"] = None

    objs = gc.get_objects()
    counts: dict[str, int] = {}
    for key, cls in targets.items():
        if cls is None:
            counts[key] = -1
            continue
        counts[key] = sum(1 for o in objs if isinstance(o, cls))
    return counts


def _macd2_df_1m_stats() -> dict:
    """살아있는 macd2 MarketDataService 인스턴스 중 가장 row가 많은 _df_1m의
    row 수/메모리 크기를 읽는다(여러 개면 그 자체가 이상 신호)."""
    try:
        from app.trading.macd2.market_data import MarketDataService as _Macd2MD
    except Exception:
        return {}
    best_rows: Optional[int] = None
    best_mem_kb: Optional[float] = None
    instance_count = 0
    for o in gc.get_objects():
        if not isinstance(o, _Macd2MD):
            continue
        instance_count += 1
        df = getattr(o, "_df_1m", None)
        if df is None:
            continue
        try:
            rows = len(df)
            mem_kb = float(df.memory_usage(deep=True).sum()) / 1024.0
        except Exception:
            continue
        if best_rows is None or rows > best_rows:
            best_rows, best_mem_kb = rows, mem_kb
    return {
        "instance_count": instance_count,
        "rows": best_rows,
        "mem_kb": round(best_mem_kb, 1) if best_mem_kb is not None else None,
    }


def _macd2_worker_identity() -> dict:
    """이미 존재하는 공개 조회 함수만 호출 — Macd2Service.get_service()는
    없으면 만들 뿐 어떤 스레드도 새로 시작하지 않는다(start()를 호출하지
    않으므로)."""
    try:
        from app.trading.macd2.service import get_service

        stats = get_service().supervisor_status()
        return {
            "worker_alive": stats.get("worker_alive"),
            "instance_id": stats.get("instance_id"),
            "started_at": stats.get("started_at"),
            "tick_n": stats.get("tick_n"),
            "runtime_ui_mode": stats.get("runtime_ui_mode"),
        }
    except Exception as exc:
        return {"error": repr(exc)}


def _hynix_fast_watcher_status() -> dict:
    try:
        from app.services.hynix_auto_trade_scheduler import get_fast_status

        snap = get_fast_status()
        return {
            "last_tick_at": snap.get("last_tick_at"),
            "recent_tick_intervals_sec": snap.get("recent_tick_intervals_sec"),
            "within_operating_window": snap.get("within_operating_window"),
        }
    except Exception as exc:
        return {"error": repr(exc)}


def _hynix_force_flag() -> Optional[bool]:
    try:
        from app.services.hynix_switch_state import load_state

        return bool(load_state().get("force_fast_worker_tick"))
    except Exception:
        return None


class MemProbeThread(threading.Thread):
    def __init__(self, interval_seconds: float = PROBE_INTERVAL_SECONDS):
        super().__init__(daemon=True, name="MemProbe")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info(
            "[MemProbe] 계측 스레드 시작(%.0f초 주기) — 순수 관찰용, 거래/상태 로직에 영향 없음",
            self.interval_seconds,
        )
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(self.interval_seconds)
        logger.info("[MemProbe] 계측 스레드 종료")

    def _tick(self) -> None:
        try:
            rss_kb = _read_rss_kb()
            threads = _thread_breakdown()
            gc_counts = _gc_instance_counts()
            df_stats = _macd2_df_1m_stats()
            macd2_worker = _macd2_worker_identity()
            fast_watcher = _hynix_fast_watcher_status()
            force_flag = _hynix_force_flag()
            # 2026-08-23 사용자 요청: gc.collect()는 강제 GC 사이클을 돌려
            # 관찰 대상인 메모리 패턴 자체를 바꿀 수 있으므로 쓰지 않는다.
            # gc.get_count()는 아무것도 수거/변경하지 않고 현재 세대별
            # 할당-추적 카운터만 읽는 순수 조회라 관찰에 안전하다.
            gen_counts = gc.get_count()
            logger.info(
                "[MemProbe] ts=%s rss_mb=%s thread_total=%d threads=%s "
                "gc_instances=%s macd2_df1m=%s macd2_worker=%s "
                "hynix_fast_watcher=%s hynix_force_fast_tick=%s "
                "gc_gen_counts=%s gc_garbage=%d",
                kst_now().isoformat(),
                round(rss_kb / 1024.0, 1) if rss_kb else None,
                sum(threads.values()),
                threads,
                gc_counts,
                df_stats,
                macd2_worker,
                fast_watcher,
                force_flag,
                gen_counts,
                len(gc.garbage),
            )
        except Exception as exc:  # 계측 실패가 앱에 영향을 주면 안 된다
            logger.warning("[MemProbe] 계측 중 예외(무시하고 계속): %r", exc)


def ensure_mem_probe_running(interval_seconds: float = PROBE_INTERVAL_SECONDS) -> MemProbeThread:
    """스레드가 없거나 죽어 있으면 (재)시작한다. 이미 살아있으면 그대로 반환."""
    global _instance
    with _lock:
        if _instance is None or not _instance.is_alive():
            _instance = MemProbeThread(interval_seconds=interval_seconds)
            _instance.start()
        return _instance
