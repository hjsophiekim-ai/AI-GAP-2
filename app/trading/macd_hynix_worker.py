"""Isolated MACD Hynix Worker — single 5s loop owns the real trading flow.

Architecture (FORBIDDEN: signal queues, pending timers, signal consumers,
module reload, nested quote executors, UI-owned lifecycle):

  MarketDataService  → quotes/bars cache ONLY
  MacdSignalEngine   → pure calculate_signed_b_signal()
  MacdOrderExecutor  → switch_to_direction / broker ONLY
  Worker loop        → sole controller (steps 1–6 below)
  UI                 → read snapshot + start/stop commands ONLY

Worker tick order:
  1. Read latest cache (MarketDataService)
  2. Check new completed 3m bar
  3. calculate_signed_b_signal()
  4. If new signal_id → immediately switch_to_direction()
  5. KIS order / fill / holdings confirm (inside executor)
  6. Write snapshot + ledger
"""
from __future__ import annotations

import hashlib
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.logger import logger
from app.trading import macd_hynix_order_manager as om
from app.trading.macd_hynix_strategy import (
    CONTINUATION_REENTRY_ENABLED,
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    ENTRY_INITIAL,
    EXIT_OPPOSITE,
    EXIT_PROFIT_LOCK,
    EXIT_SESSION,
    EXIT_SL,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    OPENING_PROBE_ENABLED,
    SIGNAL_SYMBOL,
    WARMUP_1M_BARS,
    WARMUP_3M_BARS,
    evaluate_macd_direction,
    evaluate_position_exits,
    in_open_probe_window,
    open_probe_window_expired,
    opposite_symbol,
    resample_completed_3m,
    target_symbol_for_direction,
)
from app.trading.macd_pipeline import market_data as md
from app.trading.macd_pipeline import order_executor as ox
from app.trading.macd_pipeline import runtime_store as rs
from app.trading.macd_pipeline.signal_engine import (
    build_completed_signal_snapshot,
    calculate_signed_b_signal,
    completed_3m_bar_key,
)

KST = ZoneInfo("Asia/Seoul")
TICK_SECONDS = 5.0
TICK_STALL_SEC = 15.0
INTERVAL_HISTORY_MAX = 40
_STALE_CHECK_EVERY_N_TICKS = 6
# Compat: no nested KIS executor (MarketData serializes I/O).
_kis_executor = None
SESSION_START = (9, 0)
NO_NEW_SWITCH_AFTER = (14, 55)
FORCE_LIQUIDATE_AT = (15, 0)
QUOTE_ORDER_MAX_AGE_SEC = md.QUOTE_ORDER_MAX_AGE_SEC

# Re-exports for tests / UI
HOT_QUOTE_TIMEOUT_SEC = md.HOT_QUOTE_TIMEOUT_SEC
QUOTE_PHASE_CAP_SEC = 30.0
KIS_PAGE_SIZE = md.KIS_PAGE_SIZE
KIS_CALL_TIMEOUT_SEC = 12.0
ENABLE_PARALLEL_QUOTES = False
_QUOTE_SLOTS = md._QUOTE_SLOTS
WARMUP_LOOKBACK_DAYS = md.WARMUP_LOOKBACK_DAYS

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
    "worker_instance_id": None,
    "worker_started_at": None,
    "executor_alive": False,
    "worker_code_sha": None,
    "last_exception": None,
    "last_exception_traceback": None,
    "stale_worker": False,
    "stalled": False,
    "stall_reason": None,
    "run_once_source_hash": None,
}
_tick_counter = 0
_worker_instance_id: Optional[str] = None
_worker_started_at: Optional[str] = None
_LOADED_MODULE_DIGEST = ""
_LOADED_GIT_SHA = ""
_RUN_ONCE_SRC_HASH = ""
_last_seen_bar_close: Optional[str] = None
_recover_lock = threading.Lock()
_last_stall_recover_mono = 0.0
_WATCHDOG_INTERVAL_SEC = 5.0


def _now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    except Exception:
        return ""


def _stack_module_digest() -> str:
    base = Path(__file__).resolve().parent
    parts = [
        _file_digest(base / "macd_hynix_strategy.py"),
        _file_digest(base / "macd_hynix_order_manager.py"),
        _file_digest(base / "macd_hynix_worker.py"),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _capture_loaded_identity() -> dict[str, str]:
    global _LOADED_MODULE_DIGEST, _LOADED_GIT_SHA
    _LOADED_MODULE_DIGEST = _stack_module_digest()
    _LOADED_GIT_SHA = om._git_sha() or "unknown"
    return {"module_digest": _LOADED_MODULE_DIGEST, "git_sha": _LOADED_GIT_SHA}


def _run_once_source_hash() -> str:
    global _RUN_ONCE_SRC_HASH
    if _RUN_ONCE_SRC_HASH:
        return _RUN_ONCE_SRC_HASH
    try:
        import inspect
        _RUN_ONCE_SRC_HASH = hashlib.sha1(inspect.getsource(run_once).encode()).hexdigest()[:12]
        return _RUN_ONCE_SRC_HASH
    except Exception:
        return ""


def worker_identity() -> dict[str, Any]:
    """Report digests — SHA change does NOT reload modules (new process/Worker only)."""
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
        "stale_reason": "DISK_NEWER_THAN_LOADED_MODULE" if stale else None,
    }


def _avg(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 3) if xs else None


def _p95(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    ordered = sorted(xs)
    idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _count_macd_threads() -> int:
    n = 0
    for t in threading.enumerate():
        name = str(t.name or "")
        if name.startswith("macd-hynix") or name.startswith("macd-quote"):
            n += 1
    return n


def in_trading_session(now: datetime) -> bool:
    hm = (now.hour, now.minute)
    return hm >= SESSION_START and hm < FORCE_LIQUIDATE_AT


def allow_new_switch(now: datetime) -> bool:
    return (now.hour, now.minute) < NO_NEW_SWITCH_AFTER


def should_force_liquidate(now: datetime, done_date: Optional[str]) -> bool:
    today = now.strftime("%Y-%m-%d")
    if done_date == today:
        return False
    return (now.hour, now.minute) >= FORCE_LIQUIDATE_AT


def tick_age_sec(last_tick_at: Any = None) -> Optional[float]:
    if last_tick_at is None:
        with _status_lock:
            last_tick_at = _status.get("last_tick_at")
    if not last_tick_at:
        return None
    try:
        ts = datetime.fromisoformat(str(last_tick_at))
    except Exception:
        return None
    return max(0.0, (datetime.now() - ts).total_seconds())


def get_worker_status() -> dict[str, Any]:
    with _status_lock:
        intervals = list(_status.get("tick_intervals") or [])
        snap = dict(_status)
        last_tick = snap.get("last_tick_at")
        tid = int(_worker_thread.ident) if _worker_thread and _worker_thread.ident else None
    age = tick_age_sec(last_tick)
    tick_n = int(snap.get("tick_n") or 0)
    return {
        **snap,
        **worker_identity(),
        "tick_intervals": intervals[-INTERVAL_HISTORY_MAX:],
        "intervals_buf_len": len(intervals[-INTERVAL_HISTORY_MAX:]),
        "intervals_buf_cap": INTERVAL_HISTORY_MAX,
        "avg_interval": _avg(intervals[-20:]),
        "p95_interval": _p95(intervals[-20:]),
        "tick_n": tick_n,
        "tick_seq": int(snap.get("tick_seq") or tick_n),
        "thread_alive": bool(_worker_thread and _worker_thread.is_alive()),
        "thread_ident": tid,
        "thread_name": _worker_thread.name if _worker_thread else None,
        "watchdog_alive": bool(_watchdog_thread and _watchdog_thread.is_alive()),
        "tick_age_sec": round(age, 3) if age is not None else None,
        "worker_instance_id": _worker_instance_id or snap.get("worker_instance_id"),
        "worker_started_at": _worker_started_at or snap.get("worker_started_at"),
        "worker_thread_count": _count_macd_threads(),
        "executor_alive": False,  # no nested KIS executor
        "quote_updater_alive": md.quote_updater_alive(),
        "worker_code_sha": _LOADED_GIT_SHA or "unknown",
        "run_once_source_hash": snap.get("run_once_source_hash") or _run_once_source_hash(),
    }


def detect_worker_stall(
    *,
    state: Optional[dict[str, Any]] = None,
    status: Optional[dict[str, Any]] = None,
    stall_sec: float = TICK_STALL_SEC,
) -> dict[str, Any]:
    st = state if state is not None else om.load_state()
    wst = status if status is not None else get_worker_status()
    strategy_on = bool(st.get("strategy_enabled") or st.get("auto_trade_on") or st.get("force_liquidate_pending"))
    age = tick_age_sec(wst.get("last_tick_at"))
    stalled = bool(strategy_on and (age is None or age > stall_sec))
    if not strategy_on:
        reason = None
    elif not wst.get("thread_alive"):
        reason = "WORKER_THREAD_DEAD"
    elif age is None:
        reason = "WORKER_NO_HEARTBEAT"
    elif age > stall_sec:
        reason = "WORKER_TICK_STALE"
    else:
        reason = None
    return {
        "stalled": stalled,
        "stall_reason": reason if stalled else None,
        "tick_age_sec": age,
        "strategy_on": strategy_on,
        "thread_alive": bool(wst.get("thread_alive")),
    }


def bootstrap_macd_history(
    mode: str = "mock",
    *,
    now: Optional[datetime] = None,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    state = state if state is not None else om.load_state()
    state.setdefault("bootstrap", {})["status"] = "RUNNING"
    rs.set_ui_mode(state, "BOOTSTRAPPING")
    om.save_state(state)
    boot = md.bootstrap_history(mode, now=now or _now_kst())
    state["bootstrap"] = boot
    state["warmup_ready"] = bool(boot.get("ok"))
    state.setdefault("opening_probe", {})["warmup_ready"] = bool(boot.get("ok"))
    state["bootstrap_status"] = boot.get("status")
    if boot.get("ok"):
        state["order_block_reason"] = None
        state["macd_status"] = "OK"
        rs.set_ui_mode(state, "READY" if not state.get("auto_trade_on") else "RUNNING")
    else:
        state["order_block_reason"] = f"WARMUP_BOOTSTRAP:{boot.get('reason')}"
        state["macd_status"] = "NOT_READY"
        state["signal_calculation_active"] = False
        rs.set_ui_mode(state, "DATA_ERROR")
    om.refresh_runtime_status(state, worker_alive=True)
    om.save_state(state)
    return boot


def load_macd_minute_history(
    mode: str,
    *,
    count: int = 30,
    now: Optional[datetime] = None,
    force_bootstrap: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    now = now or _now_kst()
    today = now.strftime("%Y-%m-%d")
    df = md.get_history_df()
    if force_bootstrap or df.empty:
        boot = bootstrap_macd_history(mode, now=now)
        df = md.get_history_df()
        return df, {**(boot or {}), "from_cache": False}
    df = md.merge_incremental_1m(mode, now=now)
    bars3 = resample_completed_3m(df, now=now) if not df.empty else pd.DataFrame()
    return df, {
        "from_cache": True,
        "incremental": True,
        "received_1m_bars": int(len(df)),
        "completed_3m_count": int(len(bars3)),
        "session_date": today,
    }


# ── Compat aliases used by older tests ─────────────────────────────────────
_HISTORY_CACHE = md._HISTORY  # type: ignore[attr-defined]
_history_lock = md._history_lock
_quote_cache_lock = md._quote_lock
_QUOTE_CACHE = md._QUOTE_CACHE


def _fetch_kis_minute_1m(mode, count, today, *, hour1="", timeout_sec=10.0):
    return md.fetch_kis_minute_page(mode, today, hour1=hour1, count=count, timeout_sec=timeout_sec)


def _fetch_kis_minute_paged(mode, today, *, target_bars=120, timeout_sec=10.0, require_prior_day=False):
    return md.fetch_kis_minute_paged(mode, today, target_bars=target_bars, require_prior_day=require_prior_day)


def _load_prior_history_1m(now):
    return md.load_prior_history_1m(now)


def _load_prior_day_minute_df(mode, day):
    """Compat alias for tests — prior-day 1m via MarketDataService caches."""
    day_s = str(day)[:10]
    try:
        # load_prior_history uses "now" as the trading day; ask for day+1 so `day` is included.
        probe = datetime.strptime(day_s, "%Y-%m-%d") + timedelta(days=1)
        df, _ = md.load_prior_history_1m(probe)
        if not df.empty and "datetime" in df.columns:
            mask = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d") == day_s
            part = df.loc[mask]
            if not part.empty:
                return part.reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


def _load_minute_df(*args, **kwargs):
    """Compat: return cached history (tests monkeypatch this)."""
    return md.get_history_df()


def _ensure_macd_warmup(state, df_1m, now, load_diag=None):
    bars3 = resample_completed_3m(df_1m, now=now)
    ok = len(bars3) >= WARMUP_3M_BARS
    op = state.setdefault("opening_probe", {})
    op["warmup_ready"] = ok or bool(state.get("warmup_ready"))
    op["warmup_reason"] = "WARMUP_READY" if op["warmup_ready"] else f"WARMUP_LT_{WARMUP_3M_BARS}"
    state["warmup_ready"] = bool(op["warmup_ready"])
    state["macd_warmup_diagnostics"] = {
        **(load_diag or {}),
        "completed_3m_count": int(len(bars3)),
        "requested_3m_bars": WARMUP_3M_BARS,
        "received_1m_bars": int(len(df_1m)) if df_1m is not None else 0,
        "requested_1m_bars": WARMUP_1M_BARS,
    }
    return {"ok": op["warmup_ready"], "reason": op["warmup_reason"]}


def _refresh_quotes(broker, state: dict[str, Any]) -> dict[str, Any]:
    """Worker hot path: cache only. Cold/tests: one sequential fill into cache."""
    phase_t0 = time.monotonic()
    cached = md.read_quote_cache(max_age_sec=QUOTE_ORDER_MAX_AGE_SEC)
    meta = cached.pop("_meta", {})
    if any((cached.get(k) or {}).get("ok") for k, _ in _QUOTE_SLOTS):
        _apply_quotes_to_state(state, cached, source="QUOTE_CACHE", age=meta.get("age_sec"))
        state.setdefault("tick_phases", {})["quotes_sec"] = round(time.monotonic() - phase_t0, 3)
        return cached
    # Cold path (unit tests / empty cache)
    quotes = {}
    for key, sym in _QUOTE_SLOTS:
        quotes[key] = md._quote_one(broker, sym)
    md.store_quotes(quotes, mode=str(state.get("mode") or "mock"))
    _apply_quotes_to_state(state, quotes, source="SEQUENTIAL_COLD", age=0.0)
    state.setdefault("tick_phases", {})["quotes_sec"] = round(time.monotonic() - phase_t0, 3)
    return quotes


def _refresh_quotes_sequential(broker, state, *, source_label="SEQUENTIAL", phase_deadline=None):
    quotes = {}
    for key, sym in _QUOTE_SLOTS:
        quotes[key] = md._quote_one(broker, sym)
    md.store_quotes(quotes, mode=str(state.get("mode") or "mock"))
    _apply_quotes_to_state(state, quotes, source=source_label, age=0.0)
    return quotes


def _refresh_quotes_parallel(broker, state):
    """Removed nested executor path — sequential only (compat stub)."""
    return _refresh_quotes_sequential(broker, state, source_label="SEQUENTIAL")


def _quote_from_broker(broker, symbol, *, retries=2, timeout_sec=10.0):
    return md._quote_one(broker, symbol)


def _quote_from_local_cache(symbol: str):
    return None


def _failed_quote(symbol, **kwargs):
    return {"ok": False, "price": None, "symbol": str(symbol), **kwargs}


def _apply_quotes_to_state(state, quotes, *, source: str, age: Optional[float]):
    state["prices"] = {
        "hynix": (quotes.get("hynix") or {}).get("price"),
        "long": (quotes.get("long") or {}).get("price"),
        "inverse": (quotes.get("inverse") or {}).get("price"),
        "updated_at": datetime.now().isoformat(),
    }
    state["quote_source"] = source
    state["quote_cache_age_sec"] = age
    state["quotes"] = {
        "hynix": quotes.get("hynix"),
        "long": quotes.get("long"),
        "inverse": quotes.get("inverse"),
        "updated_at": datetime.now().isoformat(),
        "age_sec": age,
        "status": None,
    }
    errors = []
    for key, q in quotes.items():
        if key.startswith("_"):
            continue
        if not q.get("ok"):
            errors.append({
                "slot": key,
                "symbol": q.get("symbol"),
                "api_function": q.get("api_function") or "broker.get_current_price",
                "error_message": q.get("error_message"),
                "exception_class": q.get("exception_class"),
                "exception_repr": q.get("exception_repr"),
                "traceback": q.get("traceback"),
                "http_status": q.get("http_status"),
                "rt_cd": q.get("rt_cd"),
                "elapsed_sec": q.get("elapsed_sec"),
                "retry_count": q.get("retry_count") if q.get("retry_count") is not None else 1,
            })
    state["quote_errors"] = errors
    if errors:
        state["quote_status"] = "FAILED" if len(errors) >= 2 else "PARTIAL"
        if not str(state.get("order_block_reason") or "").startswith("WARMUP"):
            e0 = errors[0]
            state["order_block_reason"] = (
                f"QUOTE_ERROR: {e0.get('symbol')}: {e0.get('error_message')}"
            )
            rs.set_ui_mode(state, "ORDER_BLOCKED")
    else:
        state["quote_status"] = "OK"
        state["quotes"]["status"] = "OK"
        if str(state.get("order_block_reason") or "").startswith("QUOTE"):
            state["order_block_reason"] = None


def validate_quote_payload(q: dict) -> tuple[bool, str]:
    if not q.get("ok") or not q.get("price"):
        return False, q.get("error_message") or "bad quote"
    return True, "OK"


# ── Core Worker tick (single loop body) ─────────────────────────────────────

def run_once(
    *,
    broker=None,
    now: Optional[datetime] = None,
    df_1m: Optional[pd.DataFrame] = None,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """One Worker cycle — immediate signal→order, no pending timer."""
    global _last_seen_bar_close
    now = now or _now_kst()
    state = state if state is not None else om.load_state()
    mode = str(state.get("mode") or "mock")
    result: dict[str, Any] = {"ok": True, "actions": [], "macd": None}

    if not state.get("auto_trade_on") and not state.get("force_liquidate_pending"):
        result["skipped"] = "auto_trade_off"
        return result

    own_broker = False
    if broker is None:
        try:
            broker = ox.create_broker(mode, real_confirm_ok=bool(state.get("real_confirm_ok")))
            own_broker = True
        except Exception as exc:
            state["order_block_reason"] = f"broker create failed: {exc}"
            rs.set_ui_mode(state, "ORDER_BLOCKED")
            om.save_state(state)
            return {"ok": False, "error": str(exc), "actions": []}

    try:
        # 1) Read latest cache (quotes) — never block on live HTTP
        quotes = _refresh_quotes(broker, state)
        order_quotes = ox.quotes_for_order(quotes)

        # Force liquidate — highest priority (even outside session window)
        if should_force_liquidate(now, state.get("force_liquidate_done_date")) or state.get("force_liquidate_pending"):
            state["force_liquidate_pending"] = True
            liq = ox.force_liquidate(broker, mode=mode, quotes=order_quotes, state=state)
            result["actions"].append({"force_liquidate": liq})
            om.save_state(state)
            return result

        if not in_trading_session(now):
            result["skipped"] = "outside_session"
            state["order_block_reason"] = None
            om.refresh_runtime_status(state, worker_alive=True)
            state["primary_block_reason"] = "MARKET_CLOSED"
            om.save_state(state)
            return result

        # History: use provided df, monkeypatchable _load_minute_df, or cache
        load_diag: dict[str, Any] = {}
        if df_1m is None:
            patched = _load_minute_df(mode, now=now)
            if patched is not None and getattr(patched, "empty", True) is False:
                df_1m = patched
                load_diag = {"from": "_load_minute_df"}
            else:
                df_1m, load_diag = load_macd_minute_history(mode, count=30, now=now)
        warm = _ensure_macd_warmup(state, df_1m, now, load_diag=load_diag)
        boot = state.get("bootstrap") or {}
        warmup_ready = bool(
            state.get("warmup_ready")
            or (state.get("opening_probe") or {}).get("warmup_ready")
            or boot.get("ok")
            or warm.get("ok")
        )
        # Injected feeds / unit tests: enough completed 3m + probe disabled → allow signal path
        if not warmup_ready and state.get("opening_probe_enabled") is False:
            n3 = len(resample_completed_3m(df_1m, now=now)) if df_1m is not None else 0
            if n3 > 26:
                warmup_ready = True
                state.setdefault("opening_probe", {})["warmup_ready"] = True
                state["warmup_ready"] = True
        state["warmup_ready"] = warmup_ready

        # 2–3) New completed 3m → signed-B (module-level evaluate_macd_direction
        # so tests can monkeypatch worker.evaluate_macd_direction).
        if not warmup_ready:
            ev = {
                "ok": False,
                "display_direction": DIR_HOLD,
                "flag": "NOT_READY",
                "new_signal": False,
                "signal_direction": None,
                "signal_id": None,
                "macd": None,
                "signal": None,
                "hist": None,
                "hist_last3": [],
                "hist_deltas": [],
                "bar_ts": None,
                "bar_close_ts": None,
                "completed_3m_count": 0,
                "reason": "NOT_READY",
                "signal_calculation_active": False,
            }
        else:
            ev = evaluate_macd_direction(
                df_1m,
                now=now,
                last_signal_direction=state.get("last_signal_direction"),
                last_signal_bar_ts=state.get("last_signal_bar_ts"),
                session_date=now.strftime("%Y-%m-%d"),
            )
            ev = {
                **ev,
                "flag": ev.get("display_direction") or DIR_HOLD,
                "signal_calculation_active": bool(ev.get("ok")),
            }
        snap = build_completed_signal_snapshot(ev, at=now)
        state["completed_signal_snapshot"] = snap
        state["completed_signal"] = snap  # legacy alias
        state["macd"] = {
            "macd": ev.get("macd"),
            "signal": ev.get("signal"),
            "hist": ev.get("hist"),
            "hist_last3": ev.get("hist_last3") or [],
            "hist_deltas": ev.get("hist_deltas") or [],
            "reason": ev.get("reason"),
            "bar_ts": ev.get("bar_ts"),
        }
        state["display_direction"] = ev.get("display_direction") or DIR_HOLD
        state["last_flag"] = snap.get("flag")
        state["current_flag"] = snap.get("flag")
        state["last_macd_bars_ok"] = bool(ev.get("ok"))
        state["last_new_signal"] = bool(ev.get("new_signal"))
        state["signal_calculation_active"] = bool(ev.get("signal_calculation_active"))
        # UI / audit mirrors (Worker is sole writer; UI read-only)
        state["last_signal_eval"] = {
            "flag": snap.get("flag") or ev.get("display_direction") or DIR_HOLD,
            "display_direction": ev.get("display_direction"),
            "new_signal": bool(ev.get("new_signal")),
            "signal_id": ev.get("signal_id"),
            "reason": ev.get("reason"),
            "bar_ts": ev.get("bar_ts"),
            "bar_close_ts": ev.get("bar_close_ts"),
            "completed_3m_count": ev.get("completed_3m_count"),
            "at": now.isoformat(),
        }
        state["decision_trace"] = {
            "real_gate_checked": False if mode != "real" else True,
            "mode": mode,
            "flag": snap.get("flag") or ev.get("display_direction") or DIR_HOLD,
            "completed_bar_at": ev.get("bar_close_ts"),
            "signal_id": ev.get("signal_id"),
            "new_signal": bool(ev.get("new_signal")),
            "broker_called": False,
            "at": now.isoformat(),
        }
        state["macd_status"] = "NOT_READY" if not warmup_ready else ("OK" if ev.get("ok") else str(ev.get("reason") or "SIGNAL_ERROR"))
        if not warmup_ready:
            rs.set_ui_mode(state, "DATA_ERROR" if state.get("auto_trade_on") else "STOPPED")
            om.refresh_runtime_status(state, worker_alive=True)
            om.save_state(state)
            result["macd"] = ev
            return result

        bar_key = completed_3m_bar_key(ev)
        new_bar = bool(bar_key and bar_key != _last_seen_bar_close)
        if bar_key:
            _last_seen_bar_close = str(bar_key)

        # Position management while holding:
        # opposite new_signal switch has priority over Profit Lock; SL still applies if no opposite.
        pos = state.get("position") or {}
        held_sym = pos.get("symbol")
        held_qty = int(pos.get("quantity") or 0)
        if held_sym and held_qty > 0:
            slot = "long" if held_sym == LONG_SYMBOL else "inverse"
            cur = (quotes.get(slot) or {}).get("price")
            pl = state.get("profit_lock") or {}
            exit_ev = None
            if cur:
                exit_ev = evaluate_position_exits(
                    held_sym,
                    float(pos.get("avg_price") or 0),
                    float(cur),
                    held_qty,
                    peak_net_return=float(pl.get("peak_net_return") or 0),
                    profit_lock_active=bool(pl.get("profit_lock_active")),
                )
                state["profit_lock"] = {
                    "profit_lock_active": bool(exit_ev.get("profit_lock_active")),
                    "peak_net_return": float(exit_ev.get("peak_net_return") or 0),
                    "current_net_return": float(exit_ev.get("current_net_return") or 0),
                    "giveback_pct": float(exit_ev.get("giveback_pct") or 0),
                }
            # Opposite MACD while holding → same-tick switch (before Profit Lock exit)
            flag = ev.get("display_direction")
            target = target_symbol_for_direction(flag)
            if (
                ev.get("new_signal")
                and flag in (DIR_UP, DIR_DOWN)
                and target
                and held_sym != target
                and allow_new_switch(now)
            ):
                sid = ev.get("signal_id")
                if sid and sid not in (state.get("processed_signal_ids") or []):
                    state["signal_lifecycle"] = "DETECTED"
                    state["completed_3m_bar_at"] = ev.get("bar_close_ts")
                    om.record_macd_flag_event(
                        state, ts=now.isoformat(), flag=flag, signal_id=sid,
                        bar_ts=ev.get("bar_ts"), new_occurrence=True,
                    )
                    sw = ox.execute_switch(
                        broker, flag, mode=mode,
                        budget=float(state.get("budget") or 10_000_000),
                        quotes=order_quotes, signal_id=sid, state=state,
                        sell_reason=EXIT_OPPOSITE,
                    )
                    result["actions"].append({"switch": sw, "opposite_signal": True})
                    state.setdefault("decision_trace", {})["broker_called"] = True
                    om.save_state(state)
                    result["macd"] = ev
                    return result
            reason = (exit_ev or {}).get("exit_reason")
            if reason in (EXIT_SL, EXIT_PROFIT_LOCK):
                ex = ox.execute_exit(broker, mode=mode, quotes=order_quotes, state=state, reason=reason)
                result["actions"].append({"exit": ex, "reason": reason})
                om.save_state(state)
                return result

        # 4) New signal_id → immediate same-tick order (NO pending / multi-tick defer)
        if ev.get("new_signal") and ev.get("signal_id") and allow_new_switch(now):
            sid = str(ev["signal_id"])
            direction = ev.get("signal_direction") or ev.get("display_direction")
            processed = list(state.get("processed_signal_ids") or [])
            if sid in processed:
                state["duplicate_block_reason"] = f"SIGNAL_ID_ALREADY_PROCESSED:{sid}"
            elif direction in (DIR_UP, DIR_DOWN):
                # Quote age gate for orders (cache-only; never wait for UI)
                meta_age = (quotes.get("hynix") or {}).get("quote_age_sec")
                if meta_age is not None and meta_age > QUOTE_ORDER_MAX_AGE_SEC:
                    state["order_block_reason"] = f"QUOTE_STALE age={meta_age}"
                    rs.set_ui_mode(state, "ORDER_BLOCKED")
                else:
                    state["signal_lifecycle"] = "DETECTED"
                    state["completed_3m_bar_at"] = ev.get("bar_close_ts")
                    state["armed_at"] = now.isoformat()
                    state["pending_signal_id"] = None
                    state["pending_signal_direction"] = None
                    state["pending_signal_at"] = None
                    om.record_macd_flag_event(
                        state, ts=now.isoformat(), flag=direction, signal_id=sid,
                        bar_ts=ev.get("bar_ts"), new_occurrence=True,
                    )
                    sw = ox.execute_switch(
                        broker, direction, mode=mode,
                        budget=float(state.get("budget") or 10_000_000),
                        quotes=order_quotes, signal_id=sid, state=state,
                    )
                    result["actions"].append({"switch": sw})
                    state.setdefault("decision_trace", {})["broker_called"] = True
                    lat = state.get("order_latency") or state.get("order_latency_last") or {}
                    seg = (lat.get("segments_sec") or {})
                    detect_to_req = seg.get("signal_detect_to_order_request")
                    if detect_to_req is None and lat.get("signal_detected_at") and lat.get("order_requested_at"):
                        try:
                            detect_to_req = (
                                datetime.fromisoformat(str(lat["order_requested_at"]))
                                - datetime.fromisoformat(str(lat["signal_detected_at"]))
                            ).total_seconds()
                        except Exception:
                            detect_to_req = None
                    if detect_to_req is not None and detect_to_req > 5.0:
                        logger.error(
                            "[MACDHynix] LATENCY_BREACH signal_detect→order_request=%.3fs sid=%s",
                            detect_to_req, sid,
                        )
                        state["order_block_reason"] = (
                            f"LATENCY_BREACH_SIGNAL_TO_ORDER:{detect_to_req:.3f}s"
                        )
                    if sw.get("success"):
                        rs.set_ui_mode(state, "RUNNING")
                    elif sw.get("duplicate"):
                        state["duplicate_block_reason"] = f"SIGNAL_ID_ALREADY_PROCESSED:{sid}"
                    else:
                        om.record_macd_flag_event(
                            state, ts=now.isoformat(), flag=direction, signal_id=sid,
                            bar_ts=ev.get("bar_ts"), ordered=False,
                            block_reason=sw.get("message") or state.get("order_block_reason"),
                        )

        rs.set_ui_mode(state, "RUNNING" if state.get("auto_trade_on") else "READY")
        om.refresh_runtime_status(state, worker_alive=True)
        om.save_state(state)
        result["macd"] = ev
        result["new_bar"] = new_bar
        return result
    except Exception as exc:
        tb = traceback.format_exc()
        logger.exception("[MACDHynix] run_once error: %s", exc)
        with _status_lock:
            _status["last_exception"] = repr(exc)
            _status["last_exception_traceback"] = tb
        state["last_order_error"] = repr(exc)
        om.save_state(state)
        return {"ok": False, "error": repr(exc), "actions": []}
    finally:
        if own_broker:
            pass


def _persist_heartbeat(*, tick_n: int, intervals: list, error: Optional[str] = None, partial: bool = False) -> None:
    now_iso = datetime.now().isoformat()
    with _status_lock:
        _status["alive"] = True
        _status["last_tick_at"] = now_iso
        _status["tick_n"] = tick_n
        _status["tick_seq"] = tick_n
        if error:
            _status["last_error"] = error
            _status["primary_error"] = error
    try:
        state = om.load_state()
        ww = state.setdefault("worker", {})
        ww.update({
            "alive": True,
            "last_tick_at": now_iso,
            "tick_n": tick_n,
            "tick_seq": tick_n,
            "tick_intervals": [round(x, 3) for x in intervals[-INTERVAL_HISTORY_MAX:]],
            "intervals_buf_len": len(intervals[-INTERVAL_HISTORY_MAX:]),
            "intervals_buf_cap": INTERVAL_HISTORY_MAX,
            "avg_interval": _avg(intervals[-20:]),
            "p95_interval": _p95(intervals[-20:]),
            "instance_id": _worker_instance_id,
            "started_at": _worker_started_at,
            "thread_count": _count_macd_threads(),
            "code_sha": _LOADED_GIT_SHA,
            "run_once_source_hash": _run_once_source_hash(),
        })
        state["worker_code_sha"] = _LOADED_GIT_SHA
        if not partial:
            om.save_state(state)
    except Exception as exc:
        logger.warning("[MACDHynix] heartbeat persist failed: %s", exc)


def _worker_loop() -> None:
    global _tick_counter
    logger.warning("[MACDHynix] worker loop start instance=%s", _worker_instance_id)
    next_tick = time.monotonic()
    last_mono = next_tick
    exit_reason = None
    while not _stop_event.is_set():
        now_m = time.monotonic()
        if now_m < next_tick:
            _wake_event.wait(timeout=min(TICK_SECONDS, next_tick - now_m))
            _wake_event.clear()
            if _stop_event.is_set():
                break
            continue
        tick_started = time.monotonic()
        interval = tick_started - last_mono
        with _status_lock:
            intervals = list(_status.get("tick_intervals") or [])
            intervals.append(interval)
            _status["tick_intervals"] = intervals[-INTERVAL_HISTORY_MAX:]
        last_mono = tick_started
        _tick_counter += 1
        tick_n = _tick_counter
        try:
            with _status_lock:
                intervals_snap = list(_status.get("tick_intervals") or [])
            _persist_heartbeat(tick_n=tick_n, intervals=intervals_snap)
            state = om.load_state()
            today = _now_kst().strftime("%Y-%m-%d")
            if state.get("session_date") != today:
                om.apply_macd_session_day_rollover(state, session_date=today)
                md.clear_history()
                global _last_seen_bar_close
                _last_seen_bar_close = None
            # Process UI commands inside Worker (not a separate consumer)
            cmd = state.get("command") or {}
            if isinstance(cmd, dict) and cmd.get("action") == "force_liquidate":
                state["force_liquidate_pending"] = True
                rs.ack_command(state, ok=True, message="force_liquidate accepted")
            if state.get("auto_trade_on") or state.get("force_liquidate_pending"):
                run_once(state=state)
            else:
                om.refresh_runtime_status(state, worker_alive=True)
                om.save_state(state)
        except Exception as exc:
            tb = traceback.format_exc()
            logger.exception("[MACDHynix] tick error: %s", exc)
            with _status_lock:
                _status["last_exception"] = repr(exc)
                _status["last_exception_traceback"] = tb
                _status["primary_error"] = str(exc)
            try:
                _persist_heartbeat(tick_n=tick_n, intervals=list(_status.get("tick_intervals") or []), error=str(exc), partial=True)
            except Exception:
                pass
        next_tick += TICK_SECONDS
        behind = time.monotonic() - next_tick
        if behind > TICK_SECONDS:
            next_tick += int(behind // TICK_SECONDS) * TICK_SECONDS

    with _status_lock:
        _status["alive"] = False
    try:
        st = om.load_state()
        st.setdefault("worker", {})["alive"] = False
        om.save_state(st)
    except Exception:
        pass
    logger.warning("[MACDHynix] worker loop exit instance=%s reason=%s", _worker_instance_id, exit_reason)


def _ensure_watchdog_running() -> None:
    global _watchdog_thread

    def _watch():
        while True:
            time.sleep(_WATCHDOG_INTERVAL_SEC)
            try:
                stall = detect_worker_stall()
                if stall.get("stalled"):
                    recover_stalled_worker(reason=str(stall.get("stall_reason") or "WORKER_STALLED"))
            except Exception:
                pass

    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_thread = threading.Thread(target=_watch, name="macd-hynix-watchdog", daemon=True)
    _watchdog_thread.start()


def recover_stalled_worker(*, reason: str = "WORKER_STALLED") -> dict[str, Any]:
    """Restart Worker object only — NO module reload / code-swap."""
    global _last_stall_recover_mono
    with _recover_lock:
        now_m = time.monotonic()
        if now_m - _last_stall_recover_mono < 15.0:
            return {"ok": False, "message": "recover_cooldown"}
        _last_stall_recover_mono = now_m
    logger.error("[MACDHynix] recovering stalled worker reason=%s", reason)
    stop_worker(join_timeout=3.0)
    state = om.load_state()
    state.setdefault("worker", {})["stalled"] = True
    state["worker"]["stall_reason"] = reason
    rs.set_ui_mode(state, "WORKER_STALLED")
    om.save_state(state)
    if state.get("auto_trade_on") or state.get("force_liquidate_pending"):
        status = _start_worker_thread_only()
        md.start_quote_updater(str(state.get("mode") or "mock"))
        return {"ok": True, "status": status}
    return {"ok": True, "status": get_worker_status()}


def _start_worker_thread_only() -> dict[str, Any]:
    global _worker_thread, _worker_instance_id, _worker_started_at, _tick_counter
    if _worker_thread and _worker_thread.is_alive():
        return get_worker_status()
    _capture_loaded_identity()
    _stop_event.clear()
    _worker_instance_id = uuid.uuid4().hex[:12]
    _worker_started_at = datetime.now().isoformat()
    _tick_counter = 0
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name=f"macd-hynix-worker-{_worker_instance_id}",
        daemon=True,
    )
    _worker_thread.start()
    with _status_lock:
        _status.update({
            "thread_ident": int(_worker_thread.ident) if _worker_thread.ident else None,
            "started_at": _worker_started_at,
            "worker_instance_id": _worker_instance_id,
            "worker_started_at": _worker_started_at,
            "worker_code_sha": _LOADED_GIT_SHA,
            "alive": True,
            "stalled": False,
            "last_exception": None,
            "last_exception_traceback": None,
            "run_once_source_hash": _run_once_source_hash(),
        })
    _ensure_watchdog_running()
    return get_worker_status()


def stop_worker(*, join_timeout: float = 5.0) -> None:
    global _worker_thread
    md.stop_quote_updater(join_timeout=min(2.0, join_timeout))
    _stop_event.set()
    _wake_event.set()
    t = _worker_thread
    cur = threading.current_thread()
    if t is not None and t.is_alive() and t is not cur:
        t.join(timeout=join_timeout)
    if t is not None and not t.is_alive():
        _worker_thread = None
    with _status_lock:
        _status["alive"] = False
        _status["executor_alive"] = False


def ensure_worker_running(*, force_restart: bool = False) -> dict[str, Any]:
    """Status / stall recover only. UI must NOT use this to own lifecycle.

    Does not importlib-reload. SHA drift is reported as stale_worker.
    """
    _ensure_watchdog_running()
    ident = worker_identity()
    if force_restart:
        stop_worker()
        return _start_worker_thread_only()
    stall = detect_worker_stall()
    if stall.get("stalled"):
        recover_stalled_worker(reason=str(stall.get("stall_reason") or "WORKER_STALLED"))
        status = get_worker_status()
        status["recovered_stall"] = True
        status["stall_info"] = stall
        return status
    if _worker_thread and _worker_thread.is_alive():
        status = get_worker_status()
        status["stale_worker"] = bool(ident.get("stale_worker"))
        status["stale_reason"] = ident.get("stale_reason")
        return status
    # Do not auto-start on Streamlit rerun — only return dead status.
    status = get_worker_status()
    status["stale_worker"] = bool(ident.get("stale_worker"))
    return status


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
    ok, reason = om.can_start_macd(mode)
    if not ok:
        state = om.load_state()
        state["primary_block_reason"] = reason
        om.save_state(state)
        return {"ok": False, "message": reason, "primary_block_reason": reason}

    # Stop old Worker; start NEW Worker object — NO module reload / code-swap.
    stop_worker(join_timeout=5.0)
    if _worker_thread is not None and _worker_thread.is_alive():
        return {"ok": False, "message": "OLD_WORKER_THREAD_STILL_ALIVE — stop and retry"}

    status = _start_worker_thread_only()
    md.start_quote_updater(mode)

    state = om.load_state()
    today = _now_kst().strftime("%Y-%m-%d")
    om.apply_macd_session_day_rollover(state, session_date=today)
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
    state["worker_instance_id"] = status.get("worker_instance_id")
    state["worker_started_at"] = status.get("worker_started_at")
    state["worker_code_sha"] = _LOADED_GIT_SHA or om._git_sha()
    # Clear pending leftovers (architecture forbids pending timers)
    state["pending_signal_id"] = None
    state["pending_signal_direction"] = None
    state["pending_signal_at"] = None
    om.write_mutex(macd_on=True, mode=mode, reason="macd_started")
    rs.set_ui_mode(state, "BOOTSTRAPPING")
    om.save_state(state)

    try:
        boot = bootstrap_macd_history(mode, now=_now_kst(), state=state)
        state = om.load_state()
        logger.warning(
            "[MACDHynix] start bootstrap ok=%s 1m=%s 3m=%s",
            boot.get("ok"), boot.get("received_1m_bars"), boot.get("completed_3m_count"),
        )
    except Exception as exc:
        logger.exception("[MACDHynix] bootstrap failed: %s", exc)
        state = om.load_state()
        state.setdefault("bootstrap", {})["status"] = "FAILED"
        state["bootstrap"]["reason"] = str(exc)
        state["order_block_reason"] = f"WARMUP_BOOTSTRAP:{exc}"
        rs.set_ui_mode(state, "DATA_ERROR")
        om.save_state(state)

    state = om.load_state()
    if state.get("warmup_ready") or (state.get("bootstrap") or {}).get("ok"):
        rs.set_ui_mode(state, "RUNNING")
    om.refresh_runtime_status(state, worker_alive=True)
    om.save_state(state)
    _wake_event.set()
    # First tick immediately
    try:
        run_once(state=om.load_state())
    except Exception as exc:
        logger.warning("[MACDHynix] first tick: %s", exc)
    return {"ok": True, "state": om.load_state()}


def stop_auto_trade(reason: str = "user_stop") -> dict[str, Any]:
    global _worker_thread
    state = om.load_state()
    state["auto_trade_on"] = False
    state["stopped"] = True
    state["stopped_reason"] = reason
    state["pending_signal_id"] = None
    state["pending_signal_direction"] = None
    state["pending_signal_at"] = None
    rs.set_ui_mode(state, "STOPPED")
    om.clear_mutex(mode=str(state.get("mode") or "mock"), reason=reason)
    om.refresh_runtime_status(state, worker_alive=False)
    om.save_state(state)
    stop_worker(join_timeout=5.0)
    if _worker_thread and _worker_thread.is_alive():
        return {"ok": False, "message": "WORKER_JOIN_TIMEOUT", "state": state}
    _worker_thread = None
    return {"ok": True, "state": state}


def request_force_liquidate() -> dict[str, Any]:
    state = om.load_state()
    state["force_liquidate_pending"] = True
    state["command"] = {"action": "force_liquidate", "at": datetime.now().isoformat()}
    om.save_state(state)
    _wake_event.set()
    return {"ok": True}


def repair_phantom_initial_entry(state: dict[str, Any], broker) -> dict[str, Any]:
    out = {"repaired": False}
    pos = state.get("position") or {}
    if int(pos.get("quantity") or 0) > 0 and pos.get("symbol"):
        return out
    if str(state.get("last_event") or "") != "INITIAL_ENTRY":
        return out
    sid = state.get("last_signal_id")
    direction = state.get("last_signal_direction")
    target = target_symbol_for_direction(direction)
    if not target or not sid:
        return out
    live = om.get_held_quantity(broker, target)
    if live is None or int(live) > 0:
        return out
    other = opposite_symbol(target)
    other_qty = om.get_held_quantity(broker, other) if other else 0
    if other and other_qty is not None and int(other_qty) > 0:
        return out
    state["processed_signal_ids"] = [x for x in (state.get("processed_signal_ids") or []) if x != sid]
    state["last_signal_direction"] = None
    state["last_signal_bar_ts"] = None
    state["last_signal_id"] = None
    state["last_event"] = None
    state["direction_episode"] = om.default_state()["direction_episode"]
    state["pipeline"] = om.default_state()["pipeline"]
    state["position"] = om.default_state()["position"]
    out.update({"repaired": True, "cleared_signal_id": sid})
    return out


# Removed: reload_macd_trading_stack — SHA change requires new process / new Worker only.
def reload_macd_trading_stack(*, reason: str = "manual") -> dict[str, Any]:
    """DEPRECATED no-op. Module reload code-swap is forbidden."""
    logger.warning("[MACDHynix] reload_macd_trading_stack ignored (forbidden) reason=%s", reason)
    return {"ok": False, "message": "MODULE_RELOAD_FORBIDDEN", "reason": reason}


_capture_loaded_identity()
