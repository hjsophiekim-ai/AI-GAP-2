"""tw_shadow_forward_test.py — read-only shadow forward-test for the
"시간대별 최적거래 필터" 즉시진입 하이브리드 연구 후보 (scripts/tw_gate_
hybrid_immediate_before10_research.py: 09:00-09:59 확정 플래그는 즉시진입,
10:00 이후는 기존 T+3 재확인 그대로).

2026-08-18 사용자 지시: 백테스트(TRAIN/VAL/OOS)에서 결론이 갈렸으므로(OOS만
보면 하이브리드가 나아 보이지만 TRAIN/VAL은 오히려 나빠짐, 표본도 작음)
production 실거래 로직(app.trading.macd2/mu_macd)에는 전혀 연결하지 않는다
(TW_IMMEDIATE_ENTRY_ENABLED는 계속 False). 대신 매일 새로 쌓이는 실제
1분봉(app.services.minute_bar_archiver가 장마감 후 저장하는 replay_<date>_
{hynix,long,inverse}_1m.csv)으로 "현재 운영중인 T+3 방식"과 "하이브리드
후보"를 동시에 재생해서 그 결과만 기록해 나간다 -- 최소 20 신규거래일 동안
파라미터를 건드리지 않고 쌓은 뒤, compare_accumulated()로 복리수익률/PF/
MDD/최대연속손실 및 09:00-10:00 구간만의 성과를 비교한다.

이 파일은 실거래 코드를 단 한 줄도 import하지 않는다(app.trading.macd2.
service/worker, app.trading.mu_macd.*는 물론, 그 상태/원장 파일도 전혀
건드리지 않음) -- scripts/tw_gate_relaxed_optimization.py(순수 pandas 기반
백테스트 재생 로직)와 scripts/tw_gate_immediate_entry_research.py/
tw_gate_hybrid_immediate_before10_research.py(이미 검증된 baseline/hybrid
재생 함수)만 재사용하는, 완전히 분리된 관찰 전용 잡이다.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.logger import logger
from app.utils.data_paths import STATE_DIR
from app.utils.time_utils import kst_now

CHECK_INTERVAL_SECONDS = 900.0  # minute_bar_archive_scheduler와 동일한 15분 주기

SHADOW_LOG_PATH = STATE_DIR / "tw_shadow_forward_test_log.json"
_MAX_LOG_ENTRIES = 500  # ~2년치 거래일 -- minute_bar_archiver._MAX_LOG_ENTRIES와 동일한 여유
_IMMEDIATE_CUTOFF = dtime(10, 0)

_log_lock = threading.Lock()


def _load_log() -> list[dict[str, Any]]:
    if not SHADOW_LOG_PATH.exists():
        return []
    try:
        raw = json.loads(SHADOW_LOG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _save_log(entries: list[dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = SHADOW_LOG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(SHADOW_LOG_PATH)


def recorded_dates() -> set[str]:
    return {e["date"] for e in _load_log() if "date" in e}


def _build_single_day_cache(date_ymd: str):
    import scripts.tw_gate_relaxed_optimization as base

    # base._ALL_HYNIX_DATES is computed once at that module's FIRST import --
    # this scheduler thread lives for the whole server process, so a date
    # archived after that first import would otherwise never be seen as a
    # valid "prior trading date" for a LATER date's warmup. Refresh it every
    # call (cheap: one glob over data/cache) rather than caching staleness.
    base._ALL_HYNIX_DATES = base._all_cached_hynix_dates()
    cache, notes = base._prepare_day_cache([date_ymd])
    return base, cache, notes


def run_for_date(date_ymd: str) -> Optional[dict[str, Any]]:
    """Replays ``date_ymd``'s already-archived 1분봉 through both the
    baseline (current production T+3) and hybrid (09:00-09:59 즉시진입)
    evaluators, and appends one record to SHADOW_LOG_PATH. Idempotent: a
    no-op (returns the existing record) if this date was already recorded.
    Returns None if that date's 1m archive isn't complete yet (still
    pending minute_bar_archiver, or a non-trading day)."""
    from app.services.minute_bar_archiver import is_complete

    with _log_lock:
        existing = {e["date"]: e for e in _load_log()}
        if date_ymd in existing:
            return existing[date_ymd]

        if not is_complete(date_ymd):
            return None

        import scripts.tw_gate_immediate_entry_research as imm
        import scripts.tw_gate_hybrid_immediate_before10_research as hybrid

        base, cache, notes = _build_single_day_cache(date_ymd)
        if not cache:
            logger.info("[TWShadowForwardTest] %s skipped: %s", date_ymd, "; ".join(notes) or "no data")
            return None

        baseline_trades = imm.run_over_cache(cache, immediate=False)
        hybrid_trades = hybrid.run_hybrid_over_cache(cache)

        entry = {
            "date": date_ymd,
            "recorded_at": kst_now().isoformat(),
            "baseline_trades": [dataclasses.asdict(t) for t in baseline_trades],
            "hybrid_trades": [dataclasses.asdict(t) for t in hybrid_trades],
        }
        entries = _load_log()
        entries.append(entry)
        entries = entries[-_MAX_LOG_ENTRIES:]
        _save_log(entries)
        logger.info(
            "[TWShadowForwardTest] %s recorded: baseline=%d trades, hybrid=%d trades",
            date_ymd, len(baseline_trades), len(hybrid_trades),
        )
        return entry


def run_pending() -> list[str]:
    """Records every not-yet-shadow-tested date whose 1분봉 archive is
    already complete (per minute_bar_archiver's own candidate-date window,
    so this naturally self-heals the same way the archiver itself does after
    a missed trigger/redeploy). Returns the list of newly-recorded dates."""
    from app.services.minute_bar_archiver import candidate_dates, is_complete

    newly_recorded = []
    already = recorded_dates()
    for date_ymd in candidate_dates():
        if date_ymd in already or not is_complete(date_ymd):
            continue
        if run_for_date(date_ymd) is not None:
            newly_recorded.append(date_ymd)
    return newly_recorded


def _trades_from_dicts(rows: list[dict[str, Any]]):
    import scripts.tw_gate_relaxed_optimization as base

    return [base.Trade(**row) for row in rows]


def _is_0900_1000_entry(trade) -> bool:
    """entry_time is an ISO string with the KST offset already baked in
    (produced from a KST-tz-aware datetime), so its own .time() component
    IS the KST wall-clock time -- no separate tz conversion needed."""
    try:
        entry_dt = datetime.fromisoformat(trade.entry_time)
    except (TypeError, ValueError):
        return False
    return dtime(9, 0) <= entry_dt.time() < _IMMEDIATE_CUTOFF


def compare_accumulated(min_days: int = 20) -> Optional[dict[str, Any]]:
    """Aggregates every recorded shadow day into baseline-vs-hybrid metrics
    (복리수익률/PF/MDD/최대연속손실) plus a 09:00-10:00-only slice for each.
    Returns None until at least ``min_days`` days have been recorded (no
    partial/premature comparison). Uses the exact same base.metrics() the
    original research backtests used -- no separate metric logic."""
    import scripts.tw_gate_relaxed_optimization as base

    entries = _load_log()
    if len(entries) < min_days:
        return None

    all_baseline = []
    all_hybrid = []
    for e in entries:
        all_baseline.extend(_trades_from_dicts(e["baseline_trades"]))
        all_hybrid.extend(_trades_from_dicts(e["hybrid_trades"]))

    n_days = len(entries)
    m_baseline = base.metrics(all_baseline, n_days)
    m_hybrid = base.metrics(all_hybrid, n_days)

    early_baseline = [t for t in all_baseline if _is_0900_1000_entry(t)]
    early_hybrid = [t for t in all_hybrid if _is_0900_1000_entry(t)]
    m_baseline_early = base.metrics(early_baseline, n_days)
    m_hybrid_early = base.metrics(early_hybrid, n_days)

    hybrid_wins = (
        m_hybrid["compounded_cumulative_return_pct"] > m_baseline["compounded_cumulative_return_pct"]
        and isinstance(m_hybrid["profit_factor"], (int, float)) and isinstance(m_baseline["profit_factor"], (int, float))
        and m_hybrid["profit_factor"] > m_baseline["profit_factor"]
        and m_hybrid["max_drawdown_pct"] <= m_baseline["max_drawdown_pct"]
    )

    return {
        "days_recorded": n_days,
        "date_range": [entries[0]["date"], entries[-1]["date"]],
        "baseline": m_baseline,
        "hybrid": m_hybrid,
        "baseline_0900_1000_only": m_baseline_early,
        "hybrid_0900_1000_only": m_hybrid_early,
        "hybrid_beats_baseline_return_pf_without_worse_mdd": hybrid_wins,
    }


# ── background scheduler thread (mirrors minute_bar_archive_scheduler.py) ──
_lock = threading.Lock()
_instance: Optional["TWShadowForwardTestThread"] = None


class TWShadowForwardTestThread(threading.Thread):
    def __init__(self, interval_seconds: float = CHECK_INTERVAL_SECONDS):
        super().__init__(daemon=True, name="TWShadowForwardTestWatcher")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("[TWShadowForwardTest] 백그라운드 스레드 시작(%.0f초 주기, read-only, production 로직 무관)", self.interval_seconds)
        while not self._stop_event.is_set():
            try:
                newly = run_pending()
                if newly:
                    logger.info("[TWShadowForwardTest] 신규 기록: %s", newly)
            except Exception as exc:  # pragma: no cover - defensive top-level guard
                logger.warning("[TWShadowForwardTest] 실행 실패(다음 주기에 재시도): %r", exc)
            self._stop_event.wait(self.interval_seconds)
        logger.info("[TWShadowForwardTest] 백그라운드 스레드 종료")


def ensure_tw_shadow_forward_test_thread_running(interval_seconds: float = CHECK_INTERVAL_SECONDS) -> TWShadowForwardTestThread:
    global _instance
    with _lock:
        if _instance is None or not _instance.is_alive():
            _instance = TWShadowForwardTestThread(interval_seconds=interval_seconds)
            _instance.start()
        return _instance


def is_shadow_forward_test_thread_running() -> bool:
    return _instance is not None and _instance.is_alive()
