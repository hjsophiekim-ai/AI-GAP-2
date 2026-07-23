"""Isolated 5-second fixed-schedule worker for MACD Hynix auto trading.

Does not call Enhanced / WOC / Early / Active / Fusion / Regime / Prediction.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import importlib
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd

from app.logger import logger
from app.trading import macd_hynix_order_manager as om
from app.trading.macd_hynix_strategy import (
    CONTINUATION_REENTRY_ENABLED,
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    ENTRY_CONTINUATION,
    ENTRY_INITIAL,
    ENTRY_OPEN_IMMEDIATE,
    ENTRY_OPEN_SCALE,
    EXIT_OPPOSITE,
    EXIT_PROFIT_LOCK,
    EXIT_SL,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    OPEN_IMMEDIATE_BUDGET_FRACTION,
    OPENING_PROBE_ENABLED,
    SIGNAL_SOURCE_CONTINUATION,
    SIGNAL_SOURCE_OPEN_IMMEDIATE,
    SIGNAL_SYMBOL,
    WARMUP_1M_BARS,
    WARMUP_3M_BARS,
    compute_warmup_macd,
    evaluate_continuation_reentry,
    evaluate_macd_direction,
    evaluate_opening_probe,
    evaluate_position_exits,
    first_regular_3m_bar_closed,
    in_open_probe_window,
    open_probe_window_expired,
    opening_probe_b_confirms,
    opposite_symbol,
    resample_completed_3m,
    tail_prior_day_1m,
    target_symbol_for_direction,
)
from app.trading.macd_hynix_order_manager import SIGNAL_SOURCE

KST = ZoneInfo("Asia/Seoul")
TICK_SECONDS = 5.0
# Stall if no heartbeat for this long while strategy is on (2–3 missed 5s ticks).
TICK_STALL_SEC = 15.0
# Bound KIS I/O so a hung quote/minute call cannot freeze the 5s loop forever.
KIS_CALL_TIMEOUT_SEC = 8.0
# Cadence diagnostics only — NEVER use this length as tick count (UI looked "frozen" at 40).
INTERVAL_HISTORY_MAX = 40
SESSION_START = (9, 0)
NO_NEW_SWITCH_AFTER = (14, 55)
FORCE_LIQUIDATE_AT = (15, 0)
# Prior trading days to scan for warm-up 1m caches (weekends skipped).
WARMUP_LOOKBACK_DAYS = 8
KIS_MINUTE_API = "inquire-time-itemchartprice"

_T = TypeVar("_T")

_worker_thread: Optional[threading.Thread] = None
_watchdog_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_wake_event = threading.Event()
_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "alive": False,
    "last_tick_at": None,
    "tick_intervals": [],
    "tick_n": 0,
    "tick_seq": 0,
    "started_at": None,
    "last_error": None,
    "primary_error": None,
    "main_cycle_3m_wait_count": 0,
    "thread_ident": None,
    "force_restarted_at": None,
    "modules_reloaded_at": None,
    "stale_worker": False,
    "stalled": False,
    "stall_reason": None,
    "stall_recovered_at": None,
    "run_once_source_hash": None,
}
_tick_counter = 0
_LOADED_MODULE_DIGEST = ""
_LOADED_GIT_SHA = ""
_RUN_ONCE_SRC_HASH = ""
_STALE_CHECK_EVERY_N_TICKS = 6  # ~30s at 5s ticks
_ZOMBIE_PENDING_SEC = 45.0
_WATCHDOG_INTERVAL_SEC = 5.0
_recover_lock = threading.Lock()
_last_stall_recover_mono = 0.0
# Shared pool for bounded KIS calls (workers stay small; avoid per-tick thread storms).
_kis_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="macd-kis"
)


def _run_once_source_hash() -> str:
    """Hash of run_once source — proves which tick body is loaded (cached)."""
    global _RUN_ONCE_SRC_HASH
    if _RUN_ONCE_SRC_HASH:
        return _RUN_ONCE_SRC_HASH
    try:
        import inspect

        src = inspect.getsource(run_once)
        _RUN_ONCE_SRC_HASH = hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]
        return _RUN_ONCE_SRC_HASH
    except Exception:
        return ""


def _invalidate_run_once_hash() -> None:
    global _RUN_ONCE_SRC_HASH
    _RUN_ONCE_SRC_HASH = ""


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    except Exception:
        return ""


def _stack_module_digest() -> str:
    """Digest of strategy+order_manager+worker source on disk (identity of tradable code)."""
    base = Path(__file__).resolve().parent
    parts = [
        _file_digest(base / "macd_hynix_strategy.py"),
        _file_digest(base / "macd_hynix_order_manager.py"),
        _file_digest(base / "macd_hynix_worker.py"),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _capture_loaded_identity() -> dict[str, str]:
    """Record identity of bytecode currently loaded in this process."""
    global _LOADED_MODULE_DIGEST, _LOADED_GIT_SHA
    _LOADED_MODULE_DIGEST = _stack_module_digest()
    # Cache once — never spawn git on every 5s tick (empty string must not re-trigger).
    sha = om._git_sha() or "unknown"
    _LOADED_GIT_SHA = sha
    return {"module_digest": _LOADED_MODULE_DIGEST, "git_sha": _LOADED_GIT_SHA}



def worker_identity() -> dict[str, Any]:
    disk = _stack_module_digest()
    git = om._git_sha() or ""
    stale = bool(
        (_LOADED_MODULE_DIGEST and disk and disk != _LOADED_MODULE_DIGEST)
        or (_LOADED_GIT_SHA and git and git != _LOADED_GIT_SHA)
    )
    return {
        "loaded_module_digest": _LOADED_MODULE_DIGEST,
        "disk_module_digest": disk,
        "loaded_git_sha": _LOADED_GIT_SHA,
        "disk_git_sha": git,
        "stale_worker": stale,
        "stale_reason": (
            "DISK_NEWER_THAN_LOADED_MODULE"
            if (_LOADED_MODULE_DIGEST and disk and disk != _LOADED_MODULE_DIGEST)
            else (
                "GIT_SHA_MOVED_SINCE_IMPORT"
                if (_LOADED_GIT_SHA and git and git != _LOADED_GIT_SHA)
                else None
            )
        ),
    }


def reload_macd_trading_stack(*, reason: str = "manual") -> dict[str, Any]:
    """Stop worker thread, reload strategy/om/worker from disk, recapture identity.

    ``force_restart`` alone is NOT enough after ``git pull``: the daemon keeps the
    already-imported bytecode. Start / stale-gate must call this.

    Never ``join`` the current thread (stale detection inside the worker used to
    deadlock on self-join for the join timeout window).
    """
    global _worker_thread
    stop_worker()
    cur = threading.current_thread()
    if _worker_thread and _worker_thread.is_alive() and _worker_thread is not cur:
        _worker_thread.join(timeout=3.0)
    if _worker_thread is not cur:
        _worker_thread = None
    _stop_event.clear()

    import app.trading.macd_hynix_order_manager as om_mod
    import app.trading.macd_hynix_strategy as strat_mod
    import app.trading.macd_hynix_worker as worker_mod

    importlib.reload(strat_mod)
    importlib.reload(om_mod)
    reloaded = importlib.reload(worker_mod)
    ident = reloaded._capture_loaded_identity()
    reloaded._invalidate_run_once_hash()
    with reloaded._status_lock:
        reloaded._status["modules_reloaded_at"] = datetime.now().isoformat()
        reloaded._status["stale_worker"] = False
        reloaded._status["reload_reason"] = reason
        reloaded._status["run_once_source_hash"] = reloaded._run_once_source_hash()
    logger.warning(
        "[MACDHynix] trading stack reloaded reason=%s digest=%s git=%s",
        reason,
        ident.get("module_digest"),
        ident.get("git_sha"),
    )
    return {"ok": True, "reason": reason, **ident}


def _call_with_timeout(
    fn: Callable[[], _T],
    *,
    timeout_sec: float = KIS_CALL_TIMEOUT_SEC,
    label: str = "kis_call",
) -> tuple[Optional[_T], Optional[str]]:
    """Run ``fn`` in the shared pool; on timeout/error return (None, reason)."""
    try:
        fut = _kis_executor.submit(fn)
        return fut.result(timeout=timeout_sec), None
    except concurrent.futures.TimeoutError:
        logger.warning("[MACDHynix] %s timed out after %.1fs — skip", label, timeout_sec)
        return None, f"TIMEOUT_{timeout_sec:.0f}s:{label}"
    except Exception as exc:
        logger.warning("[MACDHynix] %s failed: %s", label, exc)
        return None, f"{label}:{exc}"


def _parse_tick_at(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def tick_age_sec(last_tick_at: Any = None) -> Optional[float]:
    """Seconds since last heartbeat (in-memory status preferred)."""
    if last_tick_at is None:
        with _status_lock:
            last_tick_at = _status.get("last_tick_at")
    ts = _parse_tick_at(last_tick_at)
    if ts is None:
        return None
    return max(0.0, (datetime.now() - ts).total_seconds())


def detect_worker_stall(
    *,
    state: Optional[dict[str, Any]] = None,
    status: Optional[dict[str, Any]] = None,
    stall_sec: float = TICK_STALL_SEC,
) -> dict[str, Any]:
    """Return stall diagnostics. Stalled when strategy is on and ticks are dead/frozen."""
    st = state if state is not None else om.load_state()
    wst = status if status is not None else get_worker_status()
    strategy_on = bool(
        st.get("strategy_enabled")
        or st.get("auto_trade_on")
        or st.get("force_liquidate_pending")
    )
    thread_alive = bool(wst.get("thread_alive"))
    last_tick = wst.get("last_tick_at") or (st.get("worker") or {}).get("last_tick_at")
    age = tick_age_sec(last_tick)
    reason: Optional[str] = None
    stalled = False
    if strategy_on:
        if not thread_alive:
            stalled = True
            reason = "WORKER_THREAD_DEAD"
        elif age is None:
            # Strategy on but never heartbeated after start — treat as stall after grace.
            started = _parse_tick_at(wst.get("started_at") or (st.get("worker") or {}).get("started_at"))
            if started is not None and (datetime.now() - started).total_seconds() > stall_sec:
                stalled = True
                reason = "WORKER_NO_HEARTBEAT"
        elif age > stall_sec:
            stalled = True
            reason = "WORKER_TICK_STALE"
    return {
        "stalled": stalled,
        "stall_reason": reason,
        "strategy_on": strategy_on,
        "thread_alive": thread_alive,
        "last_tick_at": last_tick,
        "tick_age_sec": round(age, 3) if age is not None else None,
        "stall_sec": stall_sec,
        "tick_n": wst.get("tick_n") if wst.get("tick_n") is not None else (st.get("worker") or {}).get("tick_n"),
    }


def _persist_heartbeat(
    *,
    tick_n: int,
    intervals: Optional[list[float]] = None,
    error: Optional[str] = None,
    partial: bool = False,
) -> str:
    """Always refresh last_tick_at (even on partial/failed ticks). Returns ISO timestamp.

    ``tick_n`` / ``tick_seq`` are monotonic counters — never capped.
    ``tick_intervals`` is a rolling cadence window of size INTERVAL_HISTORY_MAX only.
    """
    now_iso = datetime.now().isoformat()
    src_hash = _run_once_source_hash()
    with _status_lock:
        _status["alive"] = True
        _status["last_tick_at"] = now_iso
        _status["tick_n"] = int(tick_n)
        _status["tick_seq"] = int(tick_n)
        _status["stalled"] = False
        _status["stall_reason"] = None
        _status["run_once_source_hash"] = src_hash
        if error is not None:
            _status["last_error"] = error
            _status["primary_error"] = error
        iv = list(intervals if intervals is not None else (_status.get("tick_intervals") or []))
    try:
        state = om.load_state()
        w = state.setdefault("worker", {})
        w["alive"] = True
        w["last_tick_at"] = now_iso
        w["tick_n"] = int(tick_n)
        w["tick_seq"] = int(tick_n)
        w["tick_intervals"] = [round(x, 3) for x in iv[-INTERVAL_HISTORY_MAX:]]
        w["intervals_buf_len"] = len(w["tick_intervals"])
        w["intervals_buf_cap"] = INTERVAL_HISTORY_MAX
        w["avg_interval"] = _avg(iv[-20:])
        w["p95_interval"] = _p95(iv[-20:])
        w["main_cycle_3m_wait_count"] = 0
        w["run_once_source_hash"] = src_hash
        if error is not None:
            w["last_error"] = error
            state["primary_error"] = error
        if partial and error:
            if not state.get("primary_block_reason"):
                state["primary_block_reason"] = "TICK_PARTIAL_ERROR"
        elif state.get("primary_block_reason") in ("WORKER_STALLED", "TICK_PARTIAL_ERROR"):
            state["primary_block_reason"] = None
        state["worker_code_sha"] = _LOADED_GIT_SHA or "unknown"
        state["module_digest"] = _LOADED_MODULE_DIGEST
        om.save_state(state)
    except Exception as exc:
        logger.warning("[MACDHynix] heartbeat persist failed: %s", exc)
    return now_iso


def recover_stalled_worker(*, reason: str = "stall_watchdog") -> dict[str, Any]:
    """Reload modules + force-restart worker thread after a detected stall."""
    global _last_stall_recover_mono
    with _recover_lock:
        now_m = time.monotonic()
        # Cooldown so UI refresh + watchdog don't thrash restarts.
        if now_m - _last_stall_recover_mono < 8.0:
            return {"ok": False, "skipped": "recover_cooldown", "reason": reason}
        _last_stall_recover_mono = now_m

    logger.error("[MACDHynix] WORKER_STALLED recover reason=%s — reload+restart", reason)
    try:
        st = om.load_state()
        st["primary_block_reason"] = "WORKER_STALLED"
        w = st.setdefault("worker", {})
        w["stalled"] = True
        w["stall_reason"] = reason
        om.save_state(st)
    except Exception:
        pass

    # Prefer out-of-band reload when called from the worker thread itself.
    cur = threading.current_thread()
    if _worker_thread is cur:
        def _deferred() -> None:
            time.sleep(0.15)
            try:
                reload_macd_trading_stack(reason=reason)
                import app.trading.macd_hynix_worker as wmod

                wmod._start_worker_thread_only()
                wmod._ensure_watchdog_running()
                with wmod._status_lock:
                    wmod._status["stall_recovered_at"] = datetime.now().isoformat()
                    wmod._status["stalled"] = False
            except Exception:
                logger.exception("[MACDHynix] deferred stall recover failed")

        threading.Thread(target=_deferred, name="macd-stall-recover", daemon=True).start()
        return {"ok": True, "deferred": True, "reason": reason}

    reload_macd_trading_stack(reason=reason)
    import app.trading.macd_hynix_worker as wmod

    status = wmod._start_worker_thread_only()
    wmod._ensure_watchdog_running()
    with wmod._status_lock:
        wmod._status["stall_recovered_at"] = datetime.now().isoformat()
        wmod._status["stalled"] = False
        wmod._status["stall_reason"] = None
    try:
        st = om.load_state()
        st["primary_block_reason"] = None
        w = st.setdefault("worker", {})
        w["stalled"] = False
        w["stall_reason"] = None
        w["stall_recovered_at"] = datetime.now().isoformat()
        om.save_state(st)
    except Exception:
        pass
    return {"ok": True, "deferred": False, "reason": reason, "status": status}


def _watchdog_loop() -> None:
    logger.info("[MACDHynix] stall watchdog started (every %.1fs, stall>%.1fs)", _WATCHDOG_INTERVAL_SEC, TICK_STALL_SEC)
    while not _stop_event.is_set():
        if _stop_event.wait(timeout=_WATCHDOG_INTERVAL_SEC):
            break
        try:
            info = detect_worker_stall()
            if info.get("stalled"):
                with _status_lock:
                    _status["stalled"] = True
                    _status["stall_reason"] = info.get("stall_reason")
                recover_stalled_worker(reason=str(info.get("stall_reason") or "WORKER_STALLED"))
        except Exception:
            logger.exception("[MACDHynix] watchdog iteration failed")
    logger.info("[MACDHynix] stall watchdog stopped")


def _ensure_watchdog_running() -> None:
    global _watchdog_thread
    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, name="macd-hynix-watchdog", daemon=True
    )
    _watchdog_thread.start()


# Capture identity at first import (updated again after reload).
_capture_loaded_identity()


def _now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def _hhmm(dt: datetime) -> tuple[int, int]:
    return dt.hour, dt.minute


def in_trading_session(now: Optional[datetime] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    return hm >= SESSION_START and hm < (15, 30)


def allow_new_switch(now: Optional[datetime] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    return hm >= SESSION_START and hm < NO_NEW_SWITCH_AFTER


def should_force_liquidate(now: Optional[datetime] = None, done_date: Optional[str] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    today = now.strftime("%Y-%m-%d")
    if done_date == today:
        return False
    return hm >= FORCE_LIQUIDATE_AT


def _p95(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _avg(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def get_worker_status() -> dict[str, Any]:
    with _status_lock:
        intervals = list(_status.get("tick_intervals") or [])
        tid = int(_worker_thread.ident) if _worker_thread and _worker_thread.ident else None
        snap = dict(_status)
        last_tick = snap.get("last_tick_at")
    ident = worker_identity()
    age = tick_age_sec(last_tick)
    tick_n = int(snap.get("tick_n") or 0)
    return {
        **snap,
        "tick_intervals": intervals[-INTERVAL_HISTORY_MAX:],
        "intervals_buf_len": len(intervals[-INTERVAL_HISTORY_MAX:]),
        "intervals_buf_cap": INTERVAL_HISTORY_MAX,
        "avg_interval": _avg(intervals[-20:]),
        "p95_interval": _p95(intervals[-20:]),
        "main_cycle_3m_wait_count": int(snap.get("main_cycle_3m_wait_count") or 0),
        "tick_n": tick_n,
        "tick_seq": int(snap.get("tick_seq") or tick_n),
        "thread_alive": bool(_worker_thread and _worker_thread.is_alive()),
        "thread_ident": tid,
        "thread_name": _worker_thread.name if _worker_thread else None,
        "watchdog_alive": bool(_watchdog_thread and _watchdog_thread.is_alive()),
        "tick_age_sec": round(age, 3) if age is not None else None,
        "run_once_source_hash": snap.get("run_once_source_hash") or _run_once_source_hash(),
        "worker_code_sha": _LOADED_GIT_SHA or "unknown",
        **ident,
    }


def _load_minute_df(mode: str, count: int = 120) -> pd.DataFrame:
    """Fetch 000660 1m bars via KIS; fall back to local cache CSV."""
    df, _diag = load_macd_minute_history(mode, count=count, now=_now_kst())
    return df


def _weekday_prior_dates(today: datetime, n: int = WARMUP_LOOKBACK_DAYS) -> list[str]:
    """Recent Mon–Fri calendar dates strictly before ``today`` (KST naive)."""
    out: list[str] = []
    d = today.date() if hasattr(today, "date") else today
    from datetime import date as date_cls

    if not isinstance(d, date_cls):
        d = pd.Timestamp(today).date()
    cur = d
    while len(out) < n:
        cur = cur - timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
    return out


def _read_1m_csv(path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path)
        if "datetime" not in df.columns:
            return pd.DataFrame()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.warning("[MACDHynix] 1m csv read failed %s: %s", path, exc)
        return pd.DataFrame()


def _load_prior_day_minute_df(mode: str, day: str) -> pd.DataFrame:
    """Prior trading day 000660 1m from replay cache (warm-up)."""
    from app.utils.data_paths import CACHE_DIR

    tag = day.replace("-", "")
    path = CACHE_DIR / f"replay_{tag}_hynix_1m.csv"
    if path.exists():
        return _read_1m_csv(path)
    return pd.DataFrame()


def _load_prior_history_1m(now: datetime) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load prior trading-day 1m bars until ≥ WARMUP_1M_BARS (or best effort).

    Sources (in order per day): replay_YYYYMMDD_hynix_1m.csv, then naver multi 1m.
    """
    from app.utils.data_paths import CACHE_DIR

    sources_tried: list[str] = []
    frames: list[pd.DataFrame] = []
    days = _weekday_prior_dates(now, WARMUP_LOOKBACK_DAYS)
    naver_path = CACHE_DIR / "naver_multi_1m" / "000660_1m.csv"
    naver_df = _read_1m_csv(naver_path) if naver_path.exists() else pd.DataFrame()
    if not naver_df.empty:
        sources_tried.append(str(naver_path))

    for day in days:
        day_frames: list[pd.DataFrame] = []
        replay = _load_prior_day_minute_df("mock", day)
        if not replay.empty:
            day_frames.append(replay)
            sources_tried.append(f"replay_{day.replace('-', '')}_hynix_1m.csv")
        if not naver_df.empty:
            mask = naver_df["datetime"].dt.strftime("%Y-%m-%d") == day
            part = naver_df.loc[mask]
            if not part.empty:
                day_frames.append(part)
        if day_frames:
            frames.extend(day_frames)
        merged_so_far = (
            pd.concat(frames, ignore_index=True).drop_duplicates("datetime")
            if frames else pd.DataFrame()
        )
        if len(merged_so_far) >= WARMUP_1M_BARS:
            break

    if not frames:
        # Last-resort: entire naver multi file (may span many days)
        if not naver_df.empty:
            frames.append(naver_df.tail(WARMUP_1M_BARS + 50))

    if not frames:
        return pd.DataFrame(), {
            "api_name": "local_prior_1m",
            "sources_tried": sources_tried,
            "prior_days_scanned": days,
            "received_1m_bars": 0,
            "time_range": None,
            "failure_reason": "NO_PRIOR_1M_CACHE",
        }

    df = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    # Prefer most recent warm-up window
    if len(df) > WARMUP_1M_BARS + 30:
        df = df.tail(WARMUP_1M_BARS + 30).reset_index(drop=True)
    t0 = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t1 = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    return df, {
        "api_name": "local_prior_1m",
        "sources_tried": list(dict.fromkeys(sources_tried)),
        "prior_days_scanned": days,
        "received_1m_bars": int(len(df)),
        "time_range": {"first": t0, "last": t1},
        "failure_reason": None if len(df) >= WARMUP_1M_BARS else "PRIOR_1M_SHORT",
    }


# In-memory day history — bootstrap once, incremental merge on ticks (never re-fetch 300 bars).
_HISTORY_CACHE: dict[str, Any] = {
    "session_date": None,
    "df_1m": None,
    "bootstrap_ok": False,
    "bootstrap_at": None,
    "bootstrap_elapsed_sec": None,
    "diag": {},
}
_history_lock = threading.Lock()
HOT_QUOTE_TIMEOUT_SEC = 3.0
HOT_MINUTE_TIMEOUT_SEC = 4.0
KIS_PAGE_SIZE = 30
KIS_MAX_PAGES = 12  # ≤360 bars if each page is full


def _candles_to_df(candles: list, today: str) -> pd.DataFrame:
    rows: list[dict] = []
    for c in candles or []:
        hhmmss = str(c.get("time") or "").strip().replace(":", "")
        if len(hhmmss) < 6:
            continue
        hhmmss = hhmmss[:6]
        raw_date = str(c.get("date") or "").strip().replace("-", "")
        if len(raw_date) >= 8 and raw_date[:8].isdigit():
            ymd = raw_date[:8]
        else:
            ymd = str(today).replace("-", "")[:8]
        try:
            ts = datetime.strptime(f"{ymd}{hhmmss}", "%Y%m%d%H%M%S")
        except ValueError:
            continue
        rows.append({
            "datetime": ts,
            "open": float(c.get("open") or 0),
            "high": float(c.get("high") or 0),
            "low": float(c.get("low") or 0),
            "close": float(c.get("close") or 0),
            "volume": int(c.get("volume") or 0),
        })
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )


def _fetch_kis_minute_1m(
    mode: str,
    count: int,
    today: str,
    *,
    hour1: str = "",
    timeout_sec: float = KIS_CALL_TIMEOUT_SEC,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One KIS 1m page for 000660 (≈30 bars). Optional ``hour1`` for pagination."""
    rows_err: Optional[str] = None
    requested = int(count)
    try:
        from app.trading.kis_client import create_kis_client

        client = create_kis_client(mode if mode in ("mock", "real") else "mock")
        if client is None:
            rows_err = "kis_client_none"
            candles: list = []
        else:
            def _pull():
                return client.get_minute_candles(
                    SIGNAL_SYMBOL, period_min=1, count=count, hour1=hour1
                ) or []

            candles, to_err = _call_with_timeout(
                _pull, timeout_sec=timeout_sec, label=f"get_minute_candles:{hour1 or 'latest'}"
            )
            if to_err:
                rows_err = to_err
                candles = []
    except Exception as exc:
        rows_err = str(exc)
        candles = []
        logger.warning("[MACDHynix] minute fetch failed: %s", exc)

    df = _candles_to_df(candles or [], today)
    t0 = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t1 = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    return df, {
        "api_name": KIS_MINUTE_API,
        "requested_1m_bars": requested,
        "received_1m_bars": int(len(df)),
        "hour1": hour1 or "",
        "time_range": {"first": t0, "last": t1},
        "failure_reason": rows_err,
    }


def _fetch_kis_minute_paged(
    mode: str,
    today: str,
    *,
    target_bars: int = 120,
    timeout_sec: float = HOT_MINUTE_TIMEOUT_SEC,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Page KIS minute API backwards until ``target_bars`` or max pages."""
    pages: list[pd.DataFrame] = []
    page_diags: list[dict] = []
    hour1 = ""
    for page_i in range(KIS_MAX_PAGES):
        part, diag = _fetch_kis_minute_1m(
            mode, KIS_PAGE_SIZE, today, hour1=hour1, timeout_sec=timeout_sec
        )
        diag = {**diag, "page": page_i + 1}
        page_diags.append(diag)
        if part.empty:
            break
        pages.append(part)
        merged = (
            pd.concat(pages, ignore_index=True)
            .drop_duplicates("datetime")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        if len(merged) >= target_bars:
            pages = [merged]
            break
        # Oldest bar time → next page cursor
        oldest = pd.Timestamp(part["datetime"].iloc[0])
        next_h = oldest.strftime("%H%M%S")
        if next_h == hour1:
            break
        hour1 = next_h
        # Brief pause to respect KIS rate limits during bootstrap only
        time.sleep(0.05)
    if not pages:
        return pd.DataFrame(), {
            "api_name": KIS_MINUTE_API,
            "pages": page_diags,
            "kis_requests": len(page_diags),
            "received_1m_bars": 0,
            "failure_reason": page_diags[-1].get("failure_reason") if page_diags else "NO_PAGES",
        }
    df = (
        pd.concat(pages, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    t0 = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t1 = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    return df, {
        "api_name": KIS_MINUTE_API,
        "pages": page_diags,
        "kis_requests": len(page_diags),
        "requested_1m_bars": target_bars,
        "received_1m_bars": int(len(df)),
        "time_range": {"first": t0, "last": t1},
        "failure_reason": None,
    }


def bootstrap_macd_history(
    mode: str = "mock",
    *,
    now: Optional[datetime] = None,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """OFF hot-path warm-up: prior-day ≥300×1m + paged today KIS → ≥100 completed 3m.

    Stores result in ``_HISTORY_CACHE`` and state.bootstrap. Orders stay blocked
    until ``bootstrap_ok`` / warmup_ready.
    """
    now = now or _now_kst()
    today = now.strftime("%Y-%m-%d")
    t0 = time.monotonic()
    state = state if state is not None else om.load_state()
    boot = state.setdefault("bootstrap", {})
    boot.update({
        "status": "RUNNING",
        "started_at": datetime.now().isoformat(),
        "session_date": today,
        "kis_requests": 0,
        "received_1m_bars": 0,
        "completed_3m_count": 0,
        "ok": False,
        "reason": None,
    })
    om.save_state(state)

    prior_df, prior_diag = _load_prior_history_1m(now)
    # Prefer local prior; page KIS for today (and fill if prior short).
    live_df, live_diag = _fetch_kis_minute_paged(
        mode, today, target_bars=max(120, WARMUP_1M_BARS // 2), timeout_sec=HOT_MINUTE_TIMEOUT_SEC
    )
    if live_df.empty:
        from app.utils.data_paths import CACHE_DIR

        cache = CACHE_DIR / "hynix_minute_1m.csv"
        if cache.exists():
            live_df = _read_1m_csv(cache)
            live_diag = {
                **live_diag,
                "cache_fallback": str(cache),
                "received_1m_bars": int(len(live_df)),
            }

    frames = [f for f in (prior_df, live_df) if f is not None and not f.empty]
    if not frames:
        elapsed = round(time.monotonic() - t0, 3)
        boot.update({
            "status": "FAILED",
            "ok": False,
            "reason": "NO_1M_BARS",
            "elapsed_sec": elapsed,
            "prior": prior_diag,
            "live": live_diag,
            "finished_at": datetime.now().isoformat(),
        })
        with _history_lock:
            _HISTORY_CACHE.update({
                "session_date": today,
                "df_1m": pd.DataFrame(),
                "bootstrap_ok": False,
                "bootstrap_at": datetime.now().isoformat(),
                "bootstrap_elapsed_sec": elapsed,
                "diag": {"prior": prior_diag, "live": live_diag},
            })
        state["bootstrap"] = boot
        om.save_state(state)
        return boot

    df = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    # Keep a generous window but not unbounded
    if len(df) > WARMUP_1M_BARS + 120:
        df = df.tail(WARMUP_1M_BARS + 120).reset_index(drop=True)
    bars3 = resample_completed_3m(df, now=now)
    elapsed = round(time.monotonic() - t0, 3)
    ok = len(bars3) >= WARMUP_3M_BARS
    reason = None if ok else (
        f"WARMUP_LT_{WARMUP_3M_BARS}" if len(bars3) >= 3 else "DATA_INSUFFICIENT"
    )
    t_first = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t_last = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    diag = {
        "prior": prior_diag,
        "live": live_diag,
        "kis_requests": int((live_diag or {}).get("kis_requests") or 0),
        "received_1m_bars": int(len(df)),
        "completed_3m_count": int(len(bars3)),
        "time_range": {"first": t_first, "last": t_last},
        "failure_reason": reason,
    }
    with _history_lock:
        _HISTORY_CACHE.update({
            "session_date": today,
            "df_1m": df.copy(),
            "bootstrap_ok": ok,
            "bootstrap_at": datetime.now().isoformat(),
            "bootstrap_elapsed_sec": elapsed,
            "diag": diag,
        })

    boot.update({
        "status": "OK" if ok else "SHORT",
        "ok": ok,
        "reason": reason,
        "elapsed_sec": elapsed,
        "kis_requests": diag["kis_requests"],
        "received_1m_bars": diag["received_1m_bars"],
        "completed_3m_count": diag["completed_3m_count"],
        "time_range": diag["time_range"],
        "finished_at": datetime.now().isoformat(),
        "prior": prior_diag,
        "live": {k: v for k, v in (live_diag or {}).items() if k != "pages"},
    })
    state["bootstrap"] = boot
    # Seed warm-up display immediately
    _ensure_macd_warmup(state, df, now, load_diag=diag)
    if ok:
        state["primary_block_reason"] = None
        if str(state.get("order_block_reason") or "").startswith("WARMUP"):
            state["order_block_reason"] = None
    else:
        state["order_block_reason"] = f"WARMUP_BOOTSTRAP:{reason}"
    om.refresh_runtime_status(state, worker_alive=True)
    om.save_state(state)
    logger.warning(
        "[MACDHynix] bootstrap done ok=%s 1m=%s 3m=%s kis_req=%s elapsed=%.2fs reason=%s",
        ok, len(df), len(bars3), diag["kis_requests"], elapsed, reason,
    )
    return boot


def load_macd_minute_history(
    mode: str,
    *,
    count: int = 30,
    now: Optional[datetime] = None,
    force_bootstrap: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Hot-path history: use day cache + one incremental KIS page (not full 300 re-fetch)."""
    now = now or _now_kst()
    today = now.strftime("%Y-%m-%d")

    with _history_lock:
        cached_df = _HISTORY_CACHE.get("df_1m")
        cached_day = _HISTORY_CACHE.get("session_date")
        boot_ok = bool(_HISTORY_CACHE.get("bootstrap_ok"))
        cached_diag = dict(_HISTORY_CACHE.get("diag") or {})

    if force_bootstrap or cached_df is None or cached_day != today or (
        isinstance(cached_df, pd.DataFrame) and cached_df.empty
    ):
        boot = bootstrap_macd_history(mode, now=now)
        with _history_lock:
            cached_df = _HISTORY_CACHE.get("df_1m")
            cached_diag = dict(_HISTORY_CACHE.get("diag") or {})
            boot_ok = bool(_HISTORY_CACHE.get("bootstrap_ok"))
        if cached_df is None or (isinstance(cached_df, pd.DataFrame) and cached_df.empty):
            return pd.DataFrame(), {
                **cached_diag,
                "bootstrap": boot,
                "failure_reason": boot.get("reason") or "NO_1M_BARS",
            }
        return cached_df.copy(), {
            **cached_diag,
            "bootstrap_ok": boot_ok,
            "from_cache": False,
            "incremental": False,
        }

    # Incremental: one latest page only (fast path for 5s ticks)
    live_df, live_diag = _fetch_kis_minute_1m(
        mode, min(count, KIS_PAGE_SIZE), today, timeout_sec=HOT_MINUTE_TIMEOUT_SEC
    )
    base = cached_df if isinstance(cached_df, pd.DataFrame) else pd.DataFrame()
    if live_df is not None and not live_df.empty:
        df = (
            pd.concat([base, live_df], ignore_index=True)
            .drop_duplicates("datetime")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        if len(df) > WARMUP_1M_BARS + 120:
            df = df.tail(WARMUP_1M_BARS + 120).reset_index(drop=True)
        with _history_lock:
            _HISTORY_CACHE["df_1m"] = df.copy()
    else:
        df = base.copy() if isinstance(base, pd.DataFrame) else pd.DataFrame()

    bars3 = resample_completed_3m(df, now=now) if not df.empty else pd.DataFrame()
    t0 = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t1 = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    diag = {
        **cached_diag,
        "live_incremental": live_diag,
        "from_cache": True,
        "incremental": True,
        "bootstrap_ok": boot_ok,
        "requested_1m_bars": WARMUP_1M_BARS,
        "received_1m_bars": int(len(df)),
        "completed_1m_count": int(len(df)),
        "completed_3m_count": int(len(bars3)),
        "time_range": {"first": t0, "last": t1},
        "last_bar_time": t1,
        "failure_reason": None if len(bars3) >= WARMUP_3M_BARS else (
            f"WARMUP_LT_{WARMUP_3M_BARS}" if len(bars3) >= 3 else "DATA_INSUFFICIENT"
        ),
    }
    with _history_lock:
        _HISTORY_CACHE["diag"] = diag
    return df, diag


def _prior_session_date(today: datetime) -> str:
    days = _weekday_prior_dates(today, 1)
    return days[0] if days else (today - timedelta(days=1)).strftime("%Y-%m-%d")


def _ensure_macd_warmup(
    state: dict[str, Any],
    df_1m: pd.DataFrame,
    now: datetime,
    *,
    load_diag: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Always-on warm-up (independent of OPENING_PROBE_ENABLED).

    Sets opening_probe.warmup_ready when ≥100 completed 3m bars are available
    from prior+live history — never waits for same-day intraday accumulation.
    """
    op = state.setdefault("opening_probe", {})
    # Prefer pure prior-day tail for probe hist snapshot; fall back to full df.
    prior_day = _prior_session_date(now)
    prev_df = _load_prior_day_minute_df("mock", prior_day)
    warmup_1m = tail_prior_day_1m(prev_df) if not prev_df.empty else pd.DataFrame()
    if warmup_1m.empty and df_1m is not None and not df_1m.empty:
        # Use all non-today bars from the merged feed as warm-up seed
        today = now.strftime("%Y-%m-%d")
        mask = pd.to_datetime(df_1m["datetime"]).dt.strftime("%Y-%m-%d") < today
        warmup_1m = df_1m.loc[mask].tail(WARMUP_1M_BARS).reset_index(drop=True)
        if warmup_1m.empty:
            warmup_1m = df_1m.tail(WARMUP_1M_BARS).reset_index(drop=True)

    warm = compute_warmup_macd(warmup_1m, now=None, diagnostics=load_diag or {})
    # If dedicated prior warm-up short but merged feed already has ≥100 3m, mark ready.
    if not warm.get("ok") and df_1m is not None and not df_1m.empty:
        merged_bars = resample_completed_3m(df_1m, now=now)
        if len(merged_bars) >= WARMUP_3M_BARS:
            warm = compute_warmup_macd(
                df_1m.tail(max(WARMUP_1M_BARS, len(df_1m))).reset_index(drop=True),
                now=now,
                diagnostics=load_diag or {},
            )

    op["warmup_ready"] = bool(warm.get("ok"))
    op["warmup_reason"] = warm.get("reason")
    op["warmup_hist_last2"] = warm.get("hist_last2") or []
    op["warmup_hist_deltas"] = warm.get("hist_deltas") or []
    if warm.get("hist_last3"):
        op["warmup_hist_last3"] = warm.get("hist_last3")
    state["opening_warmup_macd"] = warm
    state["macd_warmup_diagnostics"] = warm.get("diagnostics") or load_diag or {}
    return warm


def _refresh_opening_warmup(state: dict[str, Any], df_1m: pd.DataFrame, now: datetime, mode: str) -> None:
    """Compat wrapper for opening-probe path — delegates to always-on warm-up."""
    _ensure_macd_warmup(state, df_1m, now)


def _record_hynix_sample(state: dict[str, Any], now: datetime, price: Optional[float]) -> None:
    if price is None or price <= 0:
        return
    op = state.setdefault("opening_probe", {})
    samples = list(op.get("price_samples_5s") or [])
    samples.append([now.isoformat(), float(price)])
    op["price_samples_5s"] = samples[-12:]


def _quote_cache_path(symbol: str) -> Optional[Path]:
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2] / "data" / "cache"
    mapping = {
        SIGNAL_SYMBOL: root / "hynix_current.json",
        LONG_SYMBOL: root / "hynix_long_current.json",
        INVERSE_SYMBOL: root / "hynix_inverse_current.json",
    }
    return mapping.get(symbol)


def _quote_from_local_cache(symbol: str, *, max_age_sec: float = 600.0) -> Optional[dict[str, Any]]:
    """Fallback ETF/hynix quote from data/cache/*.json when KIS inquire-price fails."""
    path = _quote_cache_path(symbol)
    if path is None or not path.exists():
        return None
    try:
        import json as _json

        raw = _json.loads(path.read_text(encoding="utf-8"))
        price = float(raw.get("current_price") or raw.get("price") or 0)
        if price <= 0:
            return None
        cached_at = str(raw.get("cached_at") or "")
        if cached_at:
            try:
                age = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds()
                if age > max_age_sec:
                    return None
            except Exception:
                pass
        return {
            "price": price,
            "change_pct": raw.get("change_rate") or raw.get("change_pct"),
            "bid": None,
            "ask": None,
            "updated_at": cached_at or datetime.now().isoformat(),
            "ok": True,
            "api_function": f"local_cache:{path.name}",
            "symbol": symbol,
            "response_code": None,
            "error_message": None,
            "retry_count": 0,
            "from_cache": True,
        }
    except Exception:
        return None


def _quote_from_broker(
    broker, symbol: str, *, retries: int = 1, timeout_sec: float = HOT_QUOTE_TIMEOUT_SEC
) -> dict[str, Any]:
    """Fetch one symbol quote; on failure try local cache, else concrete error fields.

    Each broker/KIS call is bounded by ``timeout_sec`` so a hung socket
    cannot block the 5s worker loop indefinitely.
    """
    price = None
    change_pct = None
    bid = None
    ask = None
    last_error: Optional[str] = None
    api_fn = "broker.get_current_price"
    response_code: Optional[str] = None
    attempts = 0

    for attempt in range(1, max(1, retries) + 1):
        attempts = attempt
        if hasattr(broker, "get_current_price"):
            api_fn = f"{type(broker).__name__}.get_current_price"

            def _pull_broker():
                return broker.get_current_price(symbol)

            raw, to_err = _call_with_timeout(
                _pull_broker,
                timeout_sec=timeout_sec,
                label=f"quote:{symbol}",
            )
            if to_err:
                last_error = to_err
                price = None
            else:
                try:
                    if isinstance(raw, dict):
                        response_code = str(
                            raw.get("rt_cd") or raw.get("msg_cd") or raw.get("code") or ""
                        ) or None
                        price = float(raw.get("current_price") or raw.get("price") or 0)
                        change_pct = raw.get("change_pct")
                        bid = raw.get("bid") or raw.get("bid_price")
                        ask = raw.get("ask") or raw.get("ask_price")
                        if raw.get("error") or raw.get("message"):
                            last_error = str(raw.get("error") or raw.get("message"))
                    elif raw is not None:
                        price = float(raw)
                    if price is not None and price > 0:
                        break
                    if price is not None and price <= 0:
                        last_error = f"non-positive price={price}"
                        price = None
                except Exception as exc:
                    last_error = str(exc)
                    price = None

        if (price is None or price <= 0) and hasattr(broker, "kis"):
            api_fn = "broker.kis.get_current_price"

            def _pull_kis():
                return broker.kis.get_current_price(symbol)

            raw, to_err = _call_with_timeout(
                _pull_kis,
                timeout_sec=timeout_sec,
                label=f"kis_quote:{symbol}",
            )
            if to_err:
                last_error = to_err
                price = None
            else:
                try:
                    if isinstance(raw, dict):
                        response_code = str(
                            raw.get("rt_cd") or raw.get("msg_cd") or raw.get("code") or ""
                        ) or None
                        price = float(raw.get("current_price") or raw.get("price") or 0)
                        change_pct = raw.get("change_rate") or raw.get("change_pct")
                        if raw.get("msg1") or raw.get("message"):
                            last_error = str(raw.get("msg1") or raw.get("message"))
                    if price is not None and price > 0:
                        break
                    if price is not None and price <= 0:
                        last_error = f"non-positive price={price}"
                        price = None
                except Exception as exc:
                    last_error = str(exc)
                    price = None

        # Timeouts will not clear by retrying immediately — fall through to cache.
        if last_error and str(last_error).startswith("TIMEOUT_"):
            break
        if attempt < retries:
            time.sleep(0.05)

    ok = price is not None and price > 0
    if not ok:
        cached = _quote_from_local_cache(symbol)
        if cached:
            return cached

    result = {
        "price": price if ok else None,
        "change_pct": change_pct,
        "bid": bid,
        "ask": ask,
        "updated_at": datetime.now().isoformat(),
        "ok": ok,
        "api_function": api_fn,
        "symbol": symbol,
        "response_code": response_code,
        "error_message": None if ok else (last_error or "quote unavailable"),
        "retry_count": attempts,
    }
    return result


def _refresh_quotes(broker, state: dict[str, Any]) -> dict[str, Any]:
    """Fetch 3 quotes in parallel with short timeouts (keep tick ≤5s)."""
    phase_t0 = time.monotonic()
    futs = {
        "hynix": _kis_executor.submit(
            lambda: _quote_from_broker(
                broker, SIGNAL_SYMBOL, retries=1, timeout_sec=HOT_QUOTE_TIMEOUT_SEC
            )
        ),
        "long": _kis_executor.submit(
            lambda: _quote_from_broker(
                broker, LONG_SYMBOL, retries=1, timeout_sec=HOT_QUOTE_TIMEOUT_SEC
            )
        ),
        "inverse": _kis_executor.submit(
            lambda: _quote_from_broker(
                broker, INVERSE_SYMBOL, retries=1, timeout_sec=HOT_QUOTE_TIMEOUT_SEC
            )
        ),
    }
    quotes: dict[str, Any] = {}
    for key, fut in futs.items():
        try:
            quotes[key] = fut.result(timeout=HOT_QUOTE_TIMEOUT_SEC + 0.5)
        except Exception as exc:
            quotes[key] = {
                "ok": False,
                "price": None,
                "symbol": {"hynix": SIGNAL_SYMBOL, "long": LONG_SYMBOL, "inverse": INVERSE_SYMBOL}[key],
                "api_function": "parallel_quote",
                "error_message": str(exc),
                "retry_count": 0,
                "response_code": None,
            }
    hynix, long_q, inv_q = quotes["hynix"], quotes["long"], quotes["inverse"]
    state["prices"] = {
        "hynix": hynix.get("price"),
        "long": long_q.get("price"),
        "inverse": inv_q.get("price"),
        "updated_at": datetime.now().isoformat(),
    }
    errors = []
    for key, q in quotes.items():
        if not q.get("ok"):
            errors.append({
                "api_function": q.get("api_function"),
                "symbol": q.get("symbol"),
                "response_code": q.get("response_code"),
                "error_message": q.get("error_message"),
                "retry_count": q.get("retry_count"),
                "slot": key,
            })
    state["quote_errors"] = errors
    phases = state.setdefault("tick_phases", {})
    phases["quotes_sec"] = round(time.monotonic() - phase_t0, 3)
    critical = (not hynix.get("ok")) or (not long_q.get("ok") and not inv_q.get("ok"))
    if critical and errors:
        state["order_block_reason"] = (
            f"QUOTE_ERROR: {errors[0].get('symbol')} via {errors[0].get('api_function')}: "
            f"{errors[0].get('error_message')} (code={errors[0].get('response_code')}, "
            f"retries={errors[0].get('retry_count')})"
        )
    elif str(state.get("order_block_reason") or "").startswith("QUOTE_ERROR:"):
        state["order_block_reason"] = None
    return quotes



def _next_action_label(state: dict[str, Any]) -> str:
    if state.get("force_liquidate_pending"):
        return "청산 대기"
    op = state.get("opening_probe") or {}
    if op.get("awaiting_09_03_confirm"):
        return "09:03 B 확인 대기"
    if op.get("window_active") and not op.get("immediate_fired_today"):
        return "09:00 개시 프로브"
    direction = state.get("pending_signal_direction") or state.get("display_direction")
    pos = state.get("position") or {}
    held = pos.get("symbol")
    target = target_symbol_for_direction(direction)
    if state.get("order_block_reason"):
        return "주문 보류(ORDER_DATA_INVALID)"
    reentry = state.get("reentry") or {}
    if reentry.get("eligible") and not held:
        return "연속재진입 대기"
    if direction == DIR_UP and held != LONG_SYMBOL:
        return "KODEX 매수"
    if direction == DIR_DOWN and held != INVERSE_SYMBOL:
        return "SOL 매수"
    if held:
        return "기존 보유 유지"
    return "대기"


def _held_etf_price(quotes: dict[str, Any], symbol: Optional[str]) -> Optional[float]:
    if symbol == LONG_SYMBOL:
        px = (quotes.get("long") or {}).get("price")
    elif symbol == INVERSE_SYMBOL:
        px = (quotes.get("inverse") or {}).get("price")
    else:
        return None
    try:
        val = float(px)
        return val if val > 0 else None
    except Exception:
        return None


def run_once(
    *,
    broker=None,
    now: Optional[datetime] = None,
    df_1m: Optional[pd.DataFrame] = None,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Single worker tick. Injectable for tests/replay."""
    now = now or _now_kst()
    state = state if state is not None else om.load_state()
    mode = str(state.get("mode") or "mock")
    result: dict[str, Any] = {"ok": True, "now": now.isoformat(), "actions": []}
    reentry_enabled = bool(
        state.get("continuation_reentry_enabled")
        if state.get("continuation_reentry_enabled") is not None
        else CONTINUATION_REENTRY_ENABLED
    )
    state["continuation_reentry_enabled"] = reentry_enabled
    probe_enabled = bool(
        state.get("opening_probe_enabled")
        if state.get("opening_probe_enabled") is not None
        else OPENING_PROBE_ENABLED
    )
    state["opening_probe_enabled"] = probe_enabled

    if not state.get("auto_trade_on") and not state.get("force_liquidate_pending"):
        result["skipped"] = "auto_trade_off"
        return result

    # Build broker if needed
    own_broker = False
    if broker is None:
        try:
            # MOCK: never touch REAL confirm / safety gates.
            if mode == "mock":
                broker = om.create_macd_broker("mock")
            else:
                # UI stores real_confirm_ok after phrase match; worker must pass the
                # configured confirm text into KisRealBroker gate 4 (empty string fails).
                real_ready = bool(state.get("real_confirm_ok"))
                confirm_text = ""
                if real_ready:
                    from app.config import get_config

                    confirm_text = str(get_config().real_confirm_text() or "")
                broker = om.create_macd_broker(
                    mode,
                    real_confirm_text=confirm_text,
                    real_ready=real_ready,
                )
            own_broker = True
            # Clear stale gate errors from a prior failed create / session.
            if state.get("order_block_reason"):
                state["order_block_reason"] = None
        except Exception as exc:
            state["order_block_reason"] = f"broker create failed: {exc}"
            om.refresh_runtime_status(state, worker_alive=True)
            om.save_state(state)
            result["ok"] = False
            result["error"] = str(exc)
            return result

    try:
        tick_t0 = time.monotonic()
        state["tick_phases"] = {}
        quotes = _refresh_quotes(broker, state)

        # Always compute MACD/direction for UI display (even outside session).
        # Orders remain gated by session / allow_new_switch below.
        session_date = now.strftime("%Y-%m-%d")
        load_diag: dict[str, Any] = {}
        hist_t0 = time.monotonic()
        if df_1m is None:
            # Hot path: cache + one incremental page (bootstrap is off-path on Start).
            df_1m, load_diag = load_macd_minute_history(mode, count=30, now=now)
        state["tick_phases"]["history_sec"] = round(time.monotonic() - hist_t0, 3)
        warm = _ensure_macd_warmup(state, df_1m, now, load_diag=load_diag)
        boot = state.get("bootstrap") or {}
        warmup_ready = bool((state.get("opening_probe") or {}).get("warmup_ready") or boot.get("ok"))
        boot_status = str(boot.get("status") or "")
        # Gate only when Start-bootstrap is in-flight or failed short — not on unit tests
        # that inject df_1m without a bootstrap record.
        if boot_status == "RUNNING" or (
            boot_status in ("FAILED", "SHORT") and not warmup_ready
        ):
            state["order_block_reason"] = state.get("order_block_reason") or (
                f"WARMUP_BOOTSTRAP:{boot.get('reason') or warm.get('reason') or boot_status}"
            )
        elif str(state.get("order_block_reason") or "").startswith("WARMUP_BOOTSTRAP:") and warmup_ready:
            state["order_block_reason"] = None

        eval_t0 = time.monotonic()
        eval_res = evaluate_macd_direction(
            df_1m,
            now=now,
            last_signal_direction=state.get("last_signal_direction"),
            last_signal_bar_ts=state.get("last_signal_bar_ts"),
            session_date=session_date,
        )
        state["tick_phases"]["macd_eval_sec"] = round(time.monotonic() - eval_t0, 3)
        state["display_direction"] = eval_res.get("display_direction") or DIR_HOLD
        state["macd"] = {
            "macd": eval_res.get("macd"),
            "signal": eval_res.get("signal"),
            "hist": eval_res.get("hist"),
            "hist_last3": eval_res.get("hist_last3") or [],
            "hist_deltas": eval_res.get("hist_deltas") or [],
            "reason": eval_res.get("reason"),
            "bar_ts": eval_res.get("bar_ts"),
        }
        # If evaluate still warm-up-starved, surface rich diagnostics (not bare WARMUP_LT_26).
        if not eval_res.get("ok"):
            diag = state.get("macd_warmup_diagnostics") or load_diag or {}
            reason = eval_res.get("reason") or "SIGNAL_INACTIVE"
            state["macd"]["reason"] = reason
            state["macd"]["diagnostics"] = diag
            if warm.get("ok") and warm.get("macd") is not None:
                # Warm-up MACD numbers available for UI even if last bar gated oddly.
                state["macd"].update({
                    "macd": warm.get("macd"),
                    "signal": warm.get("signal"),
                    "hist": warm.get("hist"),
                    "hist_last3": warm.get("hist_last3") or [],
                    "hist_deltas": warm.get("hist_deltas") or [],
                    "reason": warm.get("reason") or reason,
                })
        result["macd"] = eval_res
        # Per-tick visibility: prove 3m bars + flags are computed every cycle
        state["last_macd_bars_ok"] = bool(eval_res.get("ok"))
        flag_now = eval_res.get("display_direction") or DIR_HOLD
        state["last_flag"] = flag_now
        state["current_flag"] = flag_now
        state["last_new_signal"] = bool(eval_res.get("new_signal"))
        state["last_signal_eval"] = {
            "bar_ts": eval_res.get("bar_ts"),
            "bar_close_ts": eval_res.get("bar_close_ts"),
            "hist_last3": eval_res.get("hist_last3") or [],
            "hist_deltas": eval_res.get("hist_deltas") or [],
            "flag": flag_now,
            "new_signal": bool(eval_res.get("new_signal")),
            "signal_id": eval_res.get("signal_id"),
            "reason": eval_res.get("reason"),
            "at": now.isoformat(),
        }
        # Single completed_signal snapshot — UI must read only this (not recompute).
        state["completed_signal"] = {
            "flag": flag_now,
            "signal_id": eval_res.get("signal_id") or state.get("pending_signal_id") or state.get("last_signal_id"),
            "bar_ts": eval_res.get("bar_ts"),
            "completed_bar_at": eval_res.get("bar_close_ts"),
            "new_signal": bool(eval_res.get("new_signal")),
            "reason": eval_res.get("reason"),
            "hist_last3": eval_res.get("hist_last3") or [],
            "at": now.isoformat(),
            "worker_code_sha": _LOADED_GIT_SHA or "unknown",
            "run_once_source_hash": _run_once_source_hash(),
        }
        state["worker_code_sha"] = _LOADED_GIT_SHA or "unknown"
        state["git_sha"] = state["worker_code_sha"]
        state["module_digest"] = _LOADED_MODULE_DIGEST
        ident = worker_identity()
        state["stale_worker"] = bool(ident.get("stale_worker"))
        state["stale_worker_reason"] = ident.get("stale_reason")
        # Keep UI aliases in sync (armed_at survives pending clear after fill).
        if state.get("pending_signal_at") and not state.get("armed_at"):
            state["armed_at"] = state.get("pending_signal_at")
        om.refresh_runtime_status(state, worker_alive=True)

        # Clear half-written / zombie pending that blocks arms incorrectly.
        if state.get("pending_signal_id") and not state.get("pending_signal_direction"):
            logger.warning(
                "[MACDHynix] clearing orphan pending_id without direction: %s",
                state.get("pending_signal_id"),
            )
            state["pending_signal_id"] = None
            state["pending_signal_at"] = None
            state["pending_entry_kind"] = None
        if state.get("pending_signal_at") and not state.get("pending_signal_id"):
            state["pending_signal_at"] = None
        if state.get("pending_signal_id") and state.get("pending_signal_at"):
            try:
                pend_age = (
                    now - datetime.fromisoformat(str(state["pending_signal_at"]))
                ).total_seconds()
            except Exception:
                pend_age = 9999.0
            if pend_age > _ZOMBIE_PENDING_SEC:
                logger.warning(
                    "[MACDHynix] clearing zombie pending age=%.1fs sid=%s",
                    pend_age,
                    state.get("pending_signal_id"),
                )
                state["decision_trace"] = {
                    **(state.get("decision_trace") or {}),
                    "cleared_zombie_pending": state.get("pending_signal_id"),
                    "zombie_age_sec": pend_age,
                }
                state["pending_signal_id"] = None
                state["pending_signal_direction"] = None
                state["pending_signal_at"] = None
                state["pending_entry_kind"] = None
                state["pending_signal_source"] = None
                state["pending_budget_fraction"] = None
                state["pending_open_scale"] = None

        # Opening-probe path still refreshes pre-09:00 when probe enabled
        if probe_enabled and now.hour < 9:
            _refresh_opening_warmup(state, df_1m, now, mode)

        # 15:00 force liquidate — always highest priority
        if should_force_liquidate(now, state.get("force_liquidate_done_date")) or state.get("force_liquidate_pending"):
            state["force_liquidate_pending"] = True
            liq = om.force_liquidate_all(broker, mode=mode, quotes=quotes, state=state)
            result["actions"].append({"force_liquidate": liq})
            state["next_action"] = "청산 대기" if not liq.get("success") else "청산 완료"
            om.refresh_runtime_status(state, worker_alive=True)
            om.save_state(state)
            return result

        if not in_trading_session(now):
            result["skipped"] = "outside_session"
            state["next_action"] = _next_action_label(state)
            om.refresh_runtime_status(state, worker_alive=True)
            # Pre-open / after-close: orders are gated; surface MARKET_CLOSED clearly.
            if state.get("strategy_enabled") and not state.get("force_liquidate_pending"):
                state["primary_block_reason"] = "MARKET_CLOSED"
                state["order_execution_enabled"] = False
            om.save_state(state)
            return result

        op = state.setdefault("opening_probe", {})
        hynix_px = (quotes.get("hynix") or {}).get("price")
        try:
            hynix_px_f = float(hynix_px) if hynix_px is not None else None
        except Exception:
            hynix_px_f = None

        if probe_enabled:
            if now.hour == 9 and now.minute == 0 and hynix_px_f and hynix_px_f > 0:
                if op.get("day_open_price") is None:
                    op["day_open_price"] = hynix_px_f
            if not op.get("warmup_ready"):
                _refresh_opening_warmup(state, df_1m, now, mode)
            if in_open_probe_window(now):
                op["window_active"] = True
                _record_hynix_sample(state, now, hynix_px_f)
            elif open_probe_window_expired(now) and op.get("window_active") and not op.get("immediate_fired_today"):
                op["window_abandoned"] = True
                op["window_active"] = False

        pos = state.get("position") or {}
        held_symbol = pos.get("symbol")
        held_qty = int(pos.get("quantity") or 0)
        entry_px = float(pos.get("avg_price") or 0)

        # Opposite MACD B confirm — priority over SL / profit lock
        opposite_pending = False
        if eval_res.get("new_signal") and eval_res.get("signal_direction") and held_symbol and held_qty > 0:
            new_dir = eval_res["signal_direction"]
            new_target = target_symbol_for_direction(new_dir)
            if new_target and new_target != held_symbol:
                sid = eval_res["signal_id"]
                processed = set(state.get("processed_signal_ids") or [])
                # Do NOT re-stamp pending_signal_at when already armed — that prevented
                # next-tick execution (age never reached 0.5*TICK) so blue/red flags
                # showed in UI with no ETF order.
                if sid not in processed and sid != state.get("pending_signal_id"):
                    state["pending_signal_id"] = sid
                    state["pending_signal_direction"] = new_dir
                    state["pending_signal_at"] = now.isoformat()
                    state["armed_at"] = now.isoformat()
                    state["signal_type"] = "REVERSAL"
                    state["duplicate_block_reason"] = None
                    state["pending_entry_kind"] = ENTRY_INITIAL
                    state["pending_signal_source"] = SIGNAL_SOURCE
                    state["last_signal_at"] = now.isoformat()
                    state["last_signal_bar_ts"] = eval_res.get("bar_ts")
                    state["last_signal_direction"] = new_dir
                    state["last_signal_id"] = sid
                    om.begin_order_latency(
                        state,
                        signal_id=sid,
                        completed_3m_bar_at=eval_res.get("bar_close_ts"),
                        signal_detected_at=now.isoformat(),
                    )
                    om.set_pipeline_stage(state, "Signal", True, sid)
                    result["actions"].append({"opposite_signal": sid, "direction": new_dir})
                    opposite_pending = True
                    if op.get("awaiting_09_03_confirm"):
                        op["awaiting_09_03_confirm"] = False
                        op["confirm_checked"] = True
                elif sid == state.get("pending_signal_id"):
                    # Already armed — keep pending age so execute_now can fire this tick.
                    opposite_pending = True
                elif sid in processed:
                    state["duplicate_block_reason"] = f"DUPLICATE_SIGNAL_ID:{sid}"

        # SL / profit-lock every tick while in position (skip when opposite armed this tick)
        # Priority after 15:00 + opposite: SL then PROFIT_LOCK (no fixed +3% TP)
        if not opposite_pending and held_symbol and held_qty > 0 and entry_px > 0:
            cur_px = _held_etf_price(quotes, held_symbol)
            if cur_px is not None:
                pl_prev = state.get("profit_lock") or {}
                exit_eval = evaluate_position_exits(
                    held_symbol,
                    entry_px,
                    cur_px,
                    held_qty,
                    peak_net_return=float(pl_prev.get("peak_net_return") or 0.0),
                    profit_lock_active=bool(pl_prev.get("profit_lock_active")),
                )
                state["profit_lock"] = {
                    "peak_net_return": exit_eval["peak_net_return"],
                    "current_net_return": exit_eval["current_net_return"],
                    "giveback_pct": exit_eval["giveback_pct"],
                    "profit_lock_active": exit_eval["profit_lock_active"],
                }
                result["profit_lock"] = state["profit_lock"]
                exit_hit = exit_eval.get("exit_reason")
                if exit_hit:
                    exit_res = om.exit_position_full(
                        broker,
                        mode=mode,
                        quotes=quotes,
                        state=state,
                        reason=exit_hit,
                        signal_id=f"{exit_hit}:{pos.get('signal_id') or now.isoformat()}",
                    )
                    result["actions"].append({"exit": exit_res, "reason": exit_hit})
                    state["next_action"] = _next_action_label(state)
                    om.refresh_runtime_status(state, worker_alive=True)
                    om.save_state(state)
                    # SL / profit-lock: end tick — never INITIAL-rebuy same bar.
                    return result

        # Keep last_signal_direction after SL/profit-lock/flat — same-dir re-entry forbidden;
        # a new episode arms only on opposite confirmed B signal.
        ep = state.get("direction_episode") or {}

        pos = state.get("position") or {}
        flat = not pos.get("symbol") or int(pos.get("quantity") or 0) <= 0

        # Continuation re-entry diagnostics (every tick when flat after TP)
        cont = evaluate_continuation_reentry(
            df_1m,
            direction=str(ep.get("direction") or eval_res.get("display_direction") or ""),
            episode=ep,
            now=now,
            enabled=reentry_enabled,
        )
        state["reentry"] = {
            "eligible": bool(cont.get("eligible")),
            "block_reason": cont.get("block_reason"),
            "bars_since_tp": cont.get("bars_since_tp") or 0,
            "hist_contracted": bool(cont.get("hist_contracted")),
            "hist_last3": cont.get("hist_last3") or [],
            "enabled": reentry_enabled,
            "episode_reentry_used": bool(ep.get("continuation_reentry_used")),
        }
        result["reentry"] = state["reentry"]

        # ── Opening probe: 09:00 immediate 50% ─────────────────────────────
        if (
            probe_enabled
            and in_open_probe_window(now)
            and not op.get("immediate_fired_today")
            and flat
            and not state.get("pending_signal_id")
            and (quotes.get("hynix") or {}).get("ok")
        ):
            warm = state.get("opening_warmup_macd") or {}
            day_open = op.get("day_open_price")
            if day_open and hynix_px_f and warm.get("ok"):
                samples = [(s[0], s[1]) for s in (op.get("price_samples_5s") or [])]
                probe = evaluate_opening_probe(
                    warm,
                    hynix_price=hynix_px_f,
                    day_open_price=float(day_open),
                    long_quote=quotes.get("long"),
                    inverse_quote=quotes.get("inverse"),
                    price_samples_5s=samples,
                    now=now,
                )
                op["last_eval_at"] = now.isoformat()
                op["last_eval_reason"] = probe.get("reason")
                op["last_eval_signal"] = probe.get("signal")
                result["opening_probe"] = probe
                if probe.get("ok_to_trade") and probe.get("direction"):
                    sid = f"OPEN_IMM:{probe['signal']}:{now.strftime('%Y%m%d%H%M%S')}"
                    state["pending_signal_id"] = sid
                    state["pending_signal_direction"] = probe["direction"]
                    state["pending_signal_at"] = now.isoformat()
                    state["pending_entry_kind"] = ENTRY_OPEN_IMMEDIATE
                    state["pending_signal_source"] = SIGNAL_SOURCE_OPEN_IMMEDIATE
                    state["pending_budget_fraction"] = OPEN_IMMEDIATE_BUDGET_FRACTION
                    om.begin_order_latency(
                        state,
                        signal_id=sid,
                        completed_3m_bar_at=None,
                        signal_detected_at=now.isoformat(),
                    )
                    om.set_pipeline_stage(state, "Signal", True, sid)
                    result["actions"].append({"opening_probe": sid, "direction": probe["direction"]})

        # ── 09:03 first completed 3m bar: confirm scale or flatten ───────────
        if (
            probe_enabled
            and op.get("awaiting_09_03_confirm")
            and not op.get("confirm_checked")
            and first_regular_3m_bar_closed(now)
            and held_symbol
            and held_qty > 0
            and pos.get("opening_probe")
            and not state.get("pending_signal_id")
        ):
            probe_dir = op.get("immediate_direction") or state.get("direction_episode", {}).get("direction")
            if probe_dir and opening_probe_b_confirms(eval_res, probe_dir):
                sid = f"OPEN_SCALE:{probe_dir}:{now.strftime('%Y%m%d%H%M%S')}"
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = probe_dir
                state["pending_signal_at"] = now.isoformat()
                state["pending_entry_kind"] = ENTRY_OPEN_SCALE
                state["pending_signal_source"] = SIGNAL_SOURCE_OPEN_IMMEDIATE
                state["pending_budget_fraction"] = OPEN_IMMEDIATE_BUDGET_FRACTION
                state["pending_open_scale"] = True
                om.begin_order_latency(
                    state,
                    signal_id=sid,
                    completed_3m_bar_at=eval_res.get("bar_close_ts"),
                    signal_detected_at=now.isoformat(),
                )
                result["actions"].append({"opening_scale": sid, "direction": probe_dir})
            else:
                flat_res = om.flatten_opening_probe_unconfirmed(
                    broker, mode=mode, quotes=quotes, state=state,
                )
                result["actions"].append({"opening_unconfirmed_exit": flat_res})
                op["confirm_checked"] = True
                pos = state.get("position") or {}
                flat = not pos.get("symbol") or int(pos.get("quantity") or 0) <= 0

        # Arm new MACD first-turn signal (Strategy B unchanged)
        # INITIAL only when flat — already holding target must not re-arm INITIAL.
        pos = state.get("position") or {}
        flat = not pos.get("symbol") or int(pos.get("quantity") or 0) <= 0
        if (
            flat
            and eval_res.get("new_signal")
            and eval_res.get("signal_id")
            and not opposite_pending
            and not (probe_enabled and op.get("awaiting_09_03_confirm") and not op.get("confirm_checked"))
        ):
            sid = eval_res["signal_id"]
            processed = set(state.get("processed_signal_ids") or [])
            if sid not in processed and sid != state.get("pending_signal_id"):
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = eval_res["signal_direction"]
                state["pending_signal_at"] = now.isoformat()
                state["armed_at"] = now.isoformat()
                state["signal_type"] = "INITIAL"
                state["duplicate_block_reason"] = None
                state["pending_entry_kind"] = ENTRY_INITIAL
                state["pending_signal_source"] = SIGNAL_SOURCE
                state["last_signal_at"] = now.isoformat()
                state["last_signal_bar_ts"] = eval_res.get("bar_ts")
                # Arm direction immediately so the same UP/DOWN streak cannot re-fire.
                state["last_signal_direction"] = eval_res["signal_direction"]
                state["last_signal_id"] = sid
                om.begin_order_latency(
                    state,
                    signal_id=sid,
                    completed_3m_bar_at=eval_res.get("bar_close_ts"),
                    signal_detected_at=now.isoformat(),
                )
                om.set_pipeline_stage(state, "Signal", True, sid)
                result["actions"].append({"signal": sid, "direction": eval_res["signal_direction"]})
            elif sid in processed:
                state["duplicate_block_reason"] = f"DUPLICATE_SIGNAL_ID:{sid}"
        elif (
            not flat
            and eval_res.get("new_signal")
            and eval_res.get("signal_id")
            and not opposite_pending
        ):
            # Same-dir flag while holding target — do not INITIAL
            tgt = target_symbol_for_direction(eval_res.get("signal_direction"))
            if tgt and tgt == (pos.get("symbol")):
                state["duplicate_block_reason"] = "ALREADY_HOLDING_TARGET_NO_INITIAL"

        # HARD GUARANTEE: flat + visible UP_RED/DOWN_BLUE + orders enabled → arm+buy
        # same tick unless holding target / signal_id processed / same-dir episode used.
        flag_pattern = flag_now if flag_now in (DIR_UP, DIR_DOWN) else None
        orders_on = bool(state.get("order_execution_enabled", True)) and not state.get("order_block_reason")
        if (
            flat
            and flag_pattern
            and orders_on
            and in_trading_session(now)
            and allow_new_switch(now)
            and not opposite_pending
            and not state.get("pending_signal_id")
            and not (probe_enabled and op.get("awaiting_09_03_confirm") and not op.get("confirm_checked"))
        ):
            target = target_symbol_for_direction(flag_pattern)
            processed = set(state.get("processed_signal_ids") or [])
            bar_ts = eval_res.get("bar_ts") or now.replace(second=0, microsecond=0).isoformat()
            force_sid = eval_res.get("signal_id") or f"MACD3M:{flag_pattern}:{bar_ts}"
            ep_now = state.get("direction_episode") or {}
            # Same-dir episode already used (entry, SL, or profit-lock exit) — no rebuy.
            same_dir_used = (
                state.get("last_signal_direction") == flag_pattern
                or (
                    ep_now.get("direction") == flag_pattern
                    and (
                        bool(ep_now.get("initial_entry_used"))
                        or bool(ep_now.get("sl_lock"))
                        or bool(ep_now.get("last_exit_reason"))
                        or bool(ep_now.get("continuation_reentry_used"))
                    )
                )
            )
            if force_sid in processed:
                state["duplicate_block_reason"] = f"DUPLICATE_SIGNAL_ID:{force_sid}"
            elif same_dir_used:
                state["duplicate_block_reason"] = state.get("duplicate_block_reason") or (
                    f"SAME_DIR_EPISODE_USED:{flag_pattern}"
                )
                # Ensure direction lock survives exits that never stamped last_signal_direction.
                if not state.get("last_signal_direction"):
                    state["last_signal_direction"] = flag_pattern
            elif target:
                state["pending_signal_id"] = force_sid
                state["pending_signal_direction"] = flag_pattern
                state["pending_signal_at"] = now.isoformat()
                state["armed_at"] = now.isoformat()
                state["signal_type"] = "INITIAL"
                state["duplicate_block_reason"] = None
                state["pending_entry_kind"] = ENTRY_INITIAL
                state["pending_signal_source"] = SIGNAL_SOURCE
                state["last_signal_at"] = now.isoformat()
                state["last_signal_bar_ts"] = bar_ts
                state["last_signal_direction"] = flag_pattern
                state["last_signal_id"] = force_sid
                if not eval_res.get("new_signal"):
                    result["actions"].append({
                        "force_arm": force_sid,
                        "direction": flag_pattern,
                        "reason": "FLAT_FLAG_MUST_ORDER",
                    })
                om.begin_order_latency(
                    state,
                    signal_id=force_sid,
                    completed_3m_bar_at=eval_res.get("bar_close_ts"),
                    signal_detected_at=now.isoformat(),
                )
                om.set_pipeline_stage(state, "Signal", True, force_sid)
                if not any(
                    isinstance(a, dict) and a.get("signal") == force_sid
                    for a in result["actions"]
                ):
                    result["actions"].append({"signal": force_sid, "direction": flag_pattern})

        # Arm continuation re-entry once (no new MACD color-flip required)
        elif (
            flat
            and reentry_enabled
            and cont.get("eligible")
            and cont.get("signal_id")
            and not state.get("pending_signal_id")
        ):
            sid = cont["signal_id"]
            processed = set(state.get("processed_signal_ids") or [])
            if sid not in processed:
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = ep.get("direction")
                state["pending_signal_at"] = now.isoformat()
                state["pending_entry_kind"] = ENTRY_CONTINUATION
                state["pending_signal_source"] = SIGNAL_SOURCE_CONTINUATION
                om.begin_order_latency(
                    state,
                    signal_id=sid,
                    completed_3m_bar_at=eval_res.get("bar_close_ts"),
                    signal_detected_at=now.isoformat(),
                )
                om.set_pipeline_stage(state, "Signal", True, sid)
                result["actions"].append({
                    "continuation_reentry": sid,
                    "direction": ep.get("direction"),
                })

        # Execute pending switch ASAP on the arming tick (no age gate).
        # Pending is idempotent via processed_signal_ids; delaying only caused missed buys.
        pending_id = state.get("pending_signal_id")
        pending_dir = state.get("pending_signal_direction")
        pending_at = state.get("pending_signal_at")
        pending_kind = state.get("pending_entry_kind") or ENTRY_INITIAL
        pending_src = state.get("pending_signal_source") or SIGNAL_SOURCE
        pending_frac = float(state.get("pending_budget_fraction") or 1.0)
        pending_scale = bool(state.get("pending_open_scale"))
        execute_now = bool(pending_id and pending_dir)
        orders_on = bool(state.get("order_execution_enabled", True)) and not state.get("order_block_reason")

        pos_now = state.get("position") or {}
        flat_now = not pos_now.get("symbol") or int(pos_now.get("quantity") or 0) <= 0
        trace: dict[str, Any] = {
            "at": now.isoformat(),
            "flag": flag_now,
            "new_signal": bool(eval_res.get("new_signal")),
            "pattern_reason": eval_res.get("reason"),
            "signal_id": eval_res.get("signal_id") or pending_id,
            "completed_bar_at": eval_res.get("bar_close_ts"),
            "bar_ts": eval_res.get("bar_ts"),
            "flat": flat_now,
            "would_arm": bool(execute_now or eval_res.get("new_signal") or (
                flat_now and flag_now in (DIR_UP, DIR_DOWN)
            )),
            "arm_blocked_reason": state.get("duplicate_block_reason")
            or state.get("order_block_reason")
            or state.get("primary_block_reason"),
            "pending_id": pending_id,
            "pending_dir": pending_dir,
            "execute_attempted": False,
            "broker_called": False,
            "broker_result": None,
            "pipeline_stages": {},
            "mode": mode,
            "real_gate_checked": False,  # mock must stay False
            "worker_code_sha": state.get("worker_code_sha"),
            "module_digest": state.get("module_digest"),
            "run_once_source_hash": _run_once_source_hash(),
            "stale_worker": state.get("stale_worker"),
            "tick_seq": (state.get("worker") or {}).get("tick_seq")
            or (state.get("worker") or {}).get("tick_n"),
        }
        if mode == "mock":
            trace["real_gate_checked"] = False
        else:
            trace["real_gate_checked"] = True
            trace["real_confirm_ok"] = bool(state.get("real_confirm_ok"))


        if execute_now and pending_id and pending_dir:
            trace["execute_attempted"] = True
            if not allow_new_switch(now):
                state["order_block_reason"] = "NO_NEW_SWITCH_AFTER_14:55"
                trace["arm_blocked_reason"] = "NO_NEW_SWITCH_AFTER_14:55"
                result["actions"].append({"blocked": "after_14:55"})
            else:
                # Opposite MACD while holding → sell reason OPPOSITE_SWITCH
                cur_pos = state.get("position") or {}
                cur_held = cur_pos.get("symbol")
                target = target_symbol_for_direction(pending_dir)
                sell_reason = None
                if cur_held and target and cur_held != target and pending_kind in (ENTRY_INITIAL, ENTRY_OPEN_IMMEDIATE):
                    sell_reason = EXIT_OPPOSITE
                try:
                    if pending_scale and pending_kind == ENTRY_OPEN_SCALE:
                        switch_res = om.scale_opening_probe(
                            broker,
                            pending_dir,
                            mode=mode,
                            budget=float(state.get("budget") or 10_000_000),
                            quotes=quotes,
                            signal_id=pending_id,
                            state=state,
                        )
                    else:
                        switch_res = om.switch_to_direction(
                            broker,
                            pending_dir,
                            mode=mode,
                            budget=float(state.get("budget") or 10_000_000),
                            quotes=quotes,
                            signal_id=pending_id,
                            state=state,
                            entry_kind=pending_kind,
                            signal_source=pending_src,
                            sell_reason=sell_reason,
                            budget_fraction=pending_frac,
                        )
                    trace["broker_called"] = True
                    trace["broker_result"] = {
                        "success": switch_res.get("success"),
                        "duplicate": switch_res.get("duplicate"),
                        "skipped_same_direction": switch_res.get("skipped_same_direction"),
                        "message": switch_res.get("message"),
                        "target": switch_res.get("target") or target,
                        "entry_kind": switch_res.get("entry_kind") or pending_kind,
                    }
                except Exception as exc:
                    logger.exception("[MACDHynix] switch_to_direction raised: %s", exc)
                    switch_res = {"success": False, "message": f"switch_exception: {exc}"}
                    trace["broker_called"] = True
                    trace["broker_result"] = {"success": False, "message": str(exc)}
                    state["last_order_error"] = str(exc)
                result["actions"].append({"switch": switch_res})
                state["last_order_attempt_at"] = now.isoformat()
                if switch_res.get("duplicate"):
                    state["duplicate_block_reason"] = f"DUPLICATE_SIGNAL_ID:{pending_id}"
                elif switch_res.get("skipped_same_direction"):
                    state["duplicate_block_reason"] = str(
                        switch_res.get("message") or "ALREADY_HOLDING_TARGET"
                    )
                elif switch_res.get("success"):
                    state["duplicate_block_reason"] = None
                if not switch_res.get("success") and not switch_res.get("duplicate") and not switch_res.get("skipped_same_direction"):
                    state["last_order_error"] = str(switch_res.get("message") or "switch_failed")
                else:
                    state["last_order_error"] = None
                if switch_res.get("success") or switch_res.get("duplicate") or switch_res.get("skipped_same_direction"):
                    if pending_kind == ENTRY_OPEN_IMMEDIATE:
                        op["immediate_fired_today"] = True
                        op["immediate_direction"] = pending_dir
                        op["immediate_signal_id"] = pending_id
                        op["immediate_at"] = now.isoformat()
                        op["awaiting_09_03_confirm"] = True
                        op["window_active"] = False
                        state["last_signal_direction"] = pending_dir
                        state["last_signal_id"] = pending_id
                    state["pending_signal_id"] = None
                    state["pending_signal_direction"] = None
                    state["pending_entry_kind"] = None
                    state["pending_signal_source"] = None
                    state["pending_budget_fraction"] = None
                    state["pending_open_scale"] = None
                    if pending_kind not in (ENTRY_OPEN_IMMEDIATE,):
                        state["last_signal_direction"] = pending_dir
                        state["last_signal_id"] = pending_id
                elif switch_res.get("order_data_invalid"):
                    state["order_block_reason"] = switch_res.get("message")
                # Same-tick pipeline snapshot keyed by signal_id
                trace["pipeline_stages"] = {
                    "signal_id": pending_id,
                    "signal": True,
                    "arm": True,
                    "switch_to_direction": bool(trace.get("broker_called")),
                    "order_success": bool(switch_res.get("success")),
                    "position_symbol": (state.get("position") or {}).get("symbol"),
                    "position_qty": (state.get("position") or {}).get("quantity"),
                    "kis_message": (switch_res.get("message") if isinstance(switch_res, dict) else None),
                }
        if flag_now in (DIR_UP, DIR_DOWN) or trace.get("execute_attempted") or trace.get("broker_called"):
            if (
                flat_now
                and flag_now in (DIR_UP, DIR_DOWN)
                and not trace.get("broker_called")
                and not trace.get("arm_blocked_reason")
            ):
                if state.get("last_signal_direction") == flag_now:
                    trace["arm_blocked_reason"] = f"SAME_DIR_EPISODE_USED:{flag_now}"
                elif not orders_on:
                    trace["arm_blocked_reason"] = "ORDER_EXECUTION_DISABLED"
                elif not allow_new_switch(now):
                    trace["arm_blocked_reason"] = "NO_NEW_SWITCH_AFTER_14:55"
                else:
                    trace["arm_blocked_reason"] = "NO_EXECUTE_UNEXPECTED"
            state["decision_trace"] = trace
            result["decision_trace"] = trace

        # Today's flag summary: unique onsets (signal_id / bar_ts+flag), not every 5s hold tick.
        if flag_now in (DIR_UP, DIR_DOWN):
            br = trace.get("broker_result") if isinstance(trace.get("broker_result"), dict) else {}
            ordered = bool(br.get("success")) and not br.get("duplicate") and not br.get("skipped_same_direction")
            # Treat successful same-direction skip as "ordered" for this signal (already in position).
            if br.get("skipped_same_direction"):
                ordered = True
            order_id = None
            if ordered:
                buy = br.get("buy") if isinstance(br.get("buy"), dict) else {}
                order_id = buy.get("order_id") or br.get("order_id")
            sid = (
                eval_res.get("signal_id")
                or pending_id
                or state.get("last_signal_id")
            )
            new_occ = bool(eval_res.get("new_signal"))
            for act in result.get("actions") or []:
                if not isinstance(act, dict):
                    continue
                if act.get("signal") or act.get("opposite_signal") or act.get("continuation_reentry"):
                    new_occ = True
                    sid = act.get("signal") or act.get("opposite_signal") or act.get("continuation_reentry") or sid
                    if act.get("direction") in (DIR_UP, DIR_DOWN):
                        flag_now = act["direction"]
                    break
            block_reason = None if ordered else om.resolve_macd_flag_block_reason(state, trace)
            om.record_macd_flag_event(
                state,
                ts=now.isoformat(),
                flag=flag_now,
                signal_id=sid,
                bar_ts=str(eval_res.get("bar_ts") or "") or None,
                new_occurrence=new_occ,
                ordered=ordered,
                block_reason=block_reason,
                order_id=order_id,
            )

        state["next_action"] = _next_action_label(state)
        try:
            total = round(time.monotonic() - tick_t0, 3)
            phases = state.setdefault("tick_phases", {})
            phases["total_sec"] = total
            if total > TICK_SECONDS:
                logger.warning(
                    "[MACDHynix] slow tick total=%.3fs phases=%s", total, phases
                )
        except Exception:
            pass
        om.refresh_runtime_status(state, worker_alive=True)
        om.save_state(state)
        return result
    finally:
        if own_broker:
            pass


def _worker_loop() -> None:
    logger.info("[MACDHynix] worker started (fixed %ss schedule)", TICK_SECONDS)
    global _tick_counter
    # Seed monotonic counter from disk so UI never appears to "reset" or cap at 40.
    try:
        prev_n = int((om.load_state().get("worker") or {}).get("tick_n") or 0)
        if prev_n > _tick_counter:
            _tick_counter = prev_n
    except Exception:
        pass
    with _status_lock:
        _status["alive"] = True
        _status["started_at"] = datetime.now().isoformat()
        _status["last_error"] = None
        _status["primary_error"] = None
        _status["stale_worker"] = False
        _status["stalled"] = False
        _status["stall_reason"] = None
        _status["run_once_source_hash"] = _run_once_source_hash()
        _status["tick_n"] = _tick_counter
        _status["tick_seq"] = _tick_counter

    # Align to wall-clock 5s grid
    next_tick = time.monotonic()
    last_mono = None
    exit_reason: Optional[str] = None
    while not _stop_event.is_set():
        now_mono = time.monotonic()
        if now_mono < next_tick:
            remaining = next_tick - now_mono
            # Negative timeout == infinite wait in threading.Event — never pass <0.
            if remaining > 0:
                _wake_event.wait(timeout=min(0.2, remaining))
            _wake_event.clear()
            continue

        tick_started = time.monotonic()
        if last_mono is not None:
            interval = tick_started - last_mono
            with _status_lock:
                intervals = list(_status.get("tick_intervals") or [])
                intervals.append(interval)
                # Display/cadence buffer only — must NOT stop the loop at 40.
                _status["tick_intervals"] = intervals[-INTERVAL_HISTORY_MAX:]
        last_mono = tick_started
        _tick_counter += 1
        tick_n = _tick_counter
        tick_error: Optional[str] = None

        # Heartbeat FIRST — never leave alive=True with a frozen last_tick_at.
        try:
            with _status_lock:
                intervals_snap = list(_status.get("tick_intervals") or [])
            _persist_heartbeat(tick_n=tick_n, intervals=intervals_snap)
        except Exception as hb_exc:
            logger.warning("[MACDHynix] early heartbeat failed: %s", hb_exc)
            with _status_lock:
                _status["alive"] = True
                _status["last_tick_at"] = datetime.now().isoformat()
                _status["tick_n"] = tick_n
                _status["tick_seq"] = tick_n

        try:
            if tick_n == 1 or (tick_n % _STALE_CHECK_EVERY_N_TICKS) == 0:
                ident = worker_identity()
                if ident.get("stale_worker"):
                    logger.error(
                        "[MACDHynix] STALE_WORKER detected (%s) at tick_seq=%s — exit for reload",
                        ident.get("stale_reason"),
                        tick_n,
                    )
                    try:
                        st = om.load_state()
                        st["stale_worker"] = True
                        st["stale_worker_reason"] = ident.get("stale_reason")
                        st["primary_block_reason"] = "STALE_WORKER"
                        om.save_state(st)
                    except Exception:
                        pass
                    exit_reason = str(ident.get("stale_reason") or "stale_tick")
                    # Do NOT reload/join from inside this thread — deferred recover does.
                    break

            state = om.load_state()
            state.setdefault("worker", {})
            state["worker"]["alive"] = True
            state["worker"]["last_tick_at"] = datetime.now().isoformat()
            state["worker"]["tick_n"] = tick_n
            state["worker"]["tick_seq"] = tick_n
            state["worker"]["intervals_buf_cap"] = INTERVAL_HISTORY_MAX
            state["worker_code_sha"] = _LOADED_GIT_SHA or "unknown"
            state["module_digest"] = _LOADED_MODULE_DIGEST
            state["stale_worker"] = False
            today = _now_kst().strftime("%Y-%m-%d")
            if state.get("session_date") != today:
                if state.get("session_date") and not state.get("auto_trade_on"):
                    om.clear_mutex(mode=str(state.get("mode") or "mock"), reason="day_change")
                om.apply_macd_session_day_rollover(state, session_date=today)
                with _history_lock:
                    _HISTORY_CACHE["session_date"] = None
                    _HISTORY_CACHE["df_1m"] = None
                    _HISTORY_CACHE["bootstrap_ok"] = False
            with _status_lock:
                intervals = list(_status.get("tick_intervals") or [])
                state["worker"]["tick_intervals"] = [
                    round(x, 3) for x in intervals[-INTERVAL_HISTORY_MAX:]
                ]
                state["worker"]["intervals_buf_len"] = len(state["worker"]["tick_intervals"])
                state["worker"]["avg_interval"] = _avg(intervals[-20:])
                state["worker"]["p95_interval"] = _p95(intervals[-20:])
                state["worker"]["main_cycle_3m_wait_count"] = 0
                state["worker"]["run_once_source_hash"] = _status.get("run_once_source_hash") or _run_once_source_hash()
                _status["alive"] = True
                _status["last_tick_at"] = state["worker"]["last_tick_at"]
                _status["tick_n"] = tick_n
                _status["tick_seq"] = tick_n
                _status["main_cycle_3m_wait_count"] = 0
            # quotes → MACD/flag → decision_trace → arm/execute (inside run_once)
            if state.get("auto_trade_on") or state.get("force_liquidate_pending"):
                run_once(state=state)
            else:
                om.refresh_runtime_status(state, worker_alive=True)
                om.save_state(state)
            if tick_n % 12 == 0:
                logger.info(
                    "[MACDHynix] tick_seq=%s alive intervals_buf=%s/%s",
                    tick_n,
                    len(intervals[-INTERVAL_HISTORY_MAX:]),
                    INTERVAL_HISTORY_MAX,
                )
        except Exception as exc:
            tick_error = str(exc)
            logger.exception(
                "[MACDHynix] tick error tick_seq=%s: %s", tick_n, exc
            )
            try:
                with _status_lock:
                    intervals_snap = list(_status.get("tick_intervals") or [])
                    _status["primary_error"] = tick_error
                _persist_heartbeat(
                    tick_n=tick_n,
                    intervals=intervals_snap,
                    error=tick_error,
                    partial=True,
                )
            except Exception:
                with _status_lock:
                    _status["last_error"] = tick_error
                    _status["primary_error"] = tick_error
                    _status["last_tick_at"] = datetime.now().isoformat()
                    _status["tick_n"] = tick_n
                    _status["tick_seq"] = tick_n

        next_tick += TICK_SECONDS
        behind = time.monotonic() - next_tick
        if behind > TICK_SECONDS:
            skipped = int(behind // TICK_SECONDS)
            next_tick += skipped * TICK_SECONDS

    intentional_stop = _stop_event.is_set() and exit_reason is None
    with _status_lock:
        _status["alive"] = False

    if intentional_stop:
        try:
            state = om.load_state()
            state.setdefault("worker", {})["alive"] = False
            om.save_state(state)
        except Exception:
            pass
    elif exit_reason:
        # Stale bytecode: deferred reload+restart (never self-join).
        recover_stalled_worker(reason=exit_reason)
    else:
        # Unexpected exit while strategy may still be on — try respawn.
        try:
            st = om.load_state()
            if st.get("auto_trade_on") or st.get("strategy_enabled") or st.get("force_liquidate_pending"):
                recover_stalled_worker(reason="WORKER_LOOP_EXIT")
            else:
                st.setdefault("worker", {})["alive"] = False
                om.save_state(st)
        except Exception:
            pass
    logger.info(
        "[MACDHynix] worker stopped reason=%s last_tick_seq=%s",
        exit_reason or ("stop" if intentional_stop else "exit"),
        _tick_counter,
    )


def ensure_worker_running(*, force_restart: bool = False) -> dict[str, Any]:
    """Start daemon worker; force_restart always kills the old thread.

    Modules are importlib-reloaded when disk/git identity diverges from what this
    process loaded (git pull without process restart). Thread kill alone is not
    enough to pick up new bytecode.

    Also detects stalled ticks (no heartbeat > TICK_STALL_SEC while strategy on)
    and auto-recovers with WORKER_STALLED reload+restart.
    """
    global _worker_thread
    _ensure_watchdog_running()
    ident = worker_identity()
    stale = bool(ident.get("stale_worker"))

    if not force_restart and not stale:
        stall = detect_worker_stall()
        if stall.get("stalled"):
            recover_stalled_worker(reason=str(stall.get("stall_reason") or "WORKER_STALLED"))
            import app.trading.macd_hynix_worker as wmod

            status = wmod.get_worker_status()
            status["recovered_stall"] = True
            status["stall_info"] = stall
            return status

    if force_restart or stale:
        if stale or force_restart:
            # Always join old thread on Start / stale (never join self).
            stop_worker()
            cur = threading.current_thread()
            if _worker_thread and _worker_thread.is_alive() and _worker_thread is not cur:
                _worker_thread.join(timeout=3.0)
            if _worker_thread is not cur:
                _worker_thread = None
            _stop_event.clear()
        if stale:
            reload_macd_trading_stack(reason=str(ident.get("stale_reason") or "stale"))
            import app.trading.macd_hynix_worker as wmod

            status = wmod._start_worker_thread_only()
            wmod._ensure_watchdog_running()
            return status
        # force_restart with matching digest: new thread, same loaded modules.
        status = _start_worker_thread_only()
        _ensure_watchdog_running()
        return status
    if _worker_thread and _worker_thread.is_alive():
        return get_worker_status()
    status = _start_worker_thread_only()
    _ensure_watchdog_running()
    return status


def _start_worker_thread_only() -> dict[str, Any]:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return get_worker_status()
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="macd-hynix-worker", daemon=True)
    _worker_thread.start()
    with _status_lock:
        _status["thread_ident"] = int(_worker_thread.ident) if _worker_thread.ident else None
        _status["force_restarted_at"] = datetime.now().isoformat()
        _status["stale_worker"] = False
        _status["stalled"] = False
    _ensure_watchdog_running()
    return get_worker_status()


def stop_worker() -> None:
    _stop_event.set()
    _wake_event.set()


def repair_phantom_initial_entry(state: dict[str, Any], broker) -> dict[str, Any]:
    """Clear phantom INITIAL_ENTRY lock when broker+local are flat (test/pollution / failed fill).

    Keeps real same-dir episode lock after a genuine exit. Only unlocks when last_event
    claims INITIAL_ENTRY but neither broker nor local book holds the target ETF.
    """
    out = {"repaired": False}
    pos = state.get("position") or {}
    local_qty = int(pos.get("quantity") or 0)
    if local_qty > 0 and pos.get("symbol"):
        return out
    if str(state.get("last_event") or "") != "INITIAL_ENTRY":
        return out
    sid = state.get("last_signal_id")
    direction = state.get("last_signal_direction")
    target = target_symbol_for_direction(direction)
    if not target or not sid:
        return out
    live = om.get_held_quantity(broker, target)
    if live is None:
        return out
    if int(live) > 0:
        return out
    other = opposite_symbol(target)
    other_qty = om.get_held_quantity(broker, other) if other else 0
    if other and other_qty is not None and int(other_qty) > 0:
        return out

    processed = [x for x in (state.get("processed_signal_ids") or []) if x != sid]
    state["processed_signal_ids"] = processed
    state["last_signal_direction"] = None
    state["last_signal_bar_ts"] = None
    state["last_signal_id"] = None
    state["last_event"] = None
    state["direction_episode"] = om.default_state()["direction_episode"]
    state["pipeline"] = om.default_state()["pipeline"]
    state["position"] = om.default_state()["position"]
    out.update({"repaired": True, "cleared_signal_id": sid, "cleared_direction": direction})
    logger.warning(
        "[MACDHynix] repaired phantom INITIAL_ENTRY lock sid=%s dir=%s (broker flat)",
        sid,
        direction,
    )
    return out


def start_auto_trade(
    *,
    mode: str = "mock",
    budget: float = 10_000_000,
    real_confirm_ok: bool = False,
    masked_account: str = "",
) -> dict[str, Any]:
    mode = "real" if mode == "real" else "mock"
    if mode == "real" and not real_confirm_ok:
        return {"ok": False, "message": "REAL requires confirm phrase"}

    # Force disk re-read of Enhanced auto_trade_on (no stale OR-scan of profile files).
    ok, reason = om.can_start_macd(mode)
    if not ok:
        state = om.load_state()
        state["primary_block_reason"] = reason
        state["legacy_truth_debug"] = om.legacy_auto_trade_truth(force_disk=True)
        om.refresh_runtime_status(state)
        om.save_state(state)
        return {"ok": False, "message": reason, "primary_block_reason": reason}

    # Kill old daemon + ALWAYS reload trading modules from disk on Start.
    # Thread restart alone cannot pick up git pulls into an already-imported process.
    reload_macd_trading_stack(reason="start_auto_trade")
    import app.trading.macd_hynix_order_manager as om_live
    import app.trading.macd_hynix_worker as w_live

    w_live._start_worker_thread_only()

    state = om_live.load_state()
    today = w_live._now_kst().strftime("%Y-%m-%d")
    # Must rollover BEFORE first run_once. Setting session_date alone used to skip the
    # worker day-change path and leave overnight last_signal_direction=UP/DOWN, so a
    # morning signed-B onset never armed despite UI showing RED/BLUE.
    om_live.apply_macd_session_day_rollover(state, session_date=today)
    if not state.get("session_date"):
        state["session_date"] = today
    state["auto_trade_on"] = True
    state["mode"] = mode
    state["budget"] = float(budget)
    state["stopped"] = False
    state["stopped_reason"] = None
    state["real_confirm_ok"] = bool(real_confirm_ok) if mode == "real" else False
    state["masked_account"] = masked_account
    state["primary_block_reason"] = None
    state["order_block_reason"] = None
    state["last_order_error"] = None
    state["stale_worker"] = False
    state["stale_worker_reason"] = None
    state["worker_code_sha"] = om_live._git_sha()
    state["git_sha"] = state["worker_code_sha"]
    state["module_digest"] = getattr(w_live, "_LOADED_MODULE_DIGEST", None)
    state["legacy_truth_debug"] = om_live.legacy_auto_trade_truth(force_disk=True)
    om_live.write_mutex(macd_on=True, mode=mode, reason="macd_started")
    om_live.refresh_runtime_status(state, worker_alive=True)
    om_live.save_state(state)

    # Bootstrap warm-up OFF the 5s hot path (prior+paged KIS → ≥100×3m).
    try:
        boot = w_live.bootstrap_macd_history(mode, now=w_live._now_kst(), state=state)
        state = om_live.load_state()
        logger.warning(
            "[MACDHynix] start bootstrap ok=%s 1m=%s 3m=%s elapsed=%s",
            boot.get("ok"),
            boot.get("received_1m_bars"),
            boot.get("completed_3m_count"),
            boot.get("elapsed_sec"),
        )
    except Exception as exc:
        logger.exception("[MACDHynix] bootstrap failed: %s", exc)
        state = om_live.load_state()
        state.setdefault("bootstrap", {})["status"] = "FAILED"
        state["bootstrap"]["reason"] = str(exc)
        state["order_block_reason"] = f"WARMUP_BOOTSTRAP:{exc}"
        om_live.save_state(state)

    # First tick immediately after start: fetch quotes + compute MACD (do not wait 5s).
    try:
        # Build broker early so phantom repair can see live holdings.
        if mode == "mock":
            broker = om_live.create_macd_broker("mock")
        else:
            real_ready = bool(state.get("real_confirm_ok"))
            confirm_text = ""
            if real_ready:
                from app.config import get_config

                confirm_text = str(get_config().real_confirm_text() or "")
            broker = om_live.create_macd_broker(
                mode,
                real_confirm_text=confirm_text,
                real_ready=real_ready,
            )
        w_live.repair_phantom_initial_entry(state, broker)
        om_live.save_state(state)
        w_live.run_once(broker=broker, state=state)
    except Exception as exc:
        logger.warning("[MACDHynix] immediate first tick failed: %s", exc)
        state = om_live.load_state()
        state["order_block_reason"] = state.get("order_block_reason") or f"first_tick_error: {exc}"
        state["last_order_error"] = str(exc)
        om_live.refresh_runtime_status(state, worker_alive=True)
        om_live.save_state(state)
    w_live._wake_event.set()
    return {"ok": True, "state": om_live.load_state()}


def stop_auto_trade(reason: str = "user_stop") -> dict[str, Any]:
    global _worker_thread
    state = om.load_state()
    state["auto_trade_on"] = False
    state["stopped"] = True
    state["stopped_reason"] = reason
    state["pending_signal_id"] = None
    state["pending_signal_direction"] = None
    om.clear_mutex(mode=str(state.get("mode") or "mock"), reason=reason)
    om.refresh_runtime_status(state, worker_alive=False)
    om.save_state(state)
    stop_worker()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=3.0)
    if _worker_thread and not _worker_thread.is_alive():
        _worker_thread = None
    return {"ok": True, "state": state}


def request_force_liquidate() -> dict[str, Any]:
    state = om.load_state()
    state["force_liquidate_pending"] = True
    om.save_state(state)
    ensure_worker_running()
    _wake_event.set()
    return {"ok": True}
