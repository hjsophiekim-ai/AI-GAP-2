"""Single MACD runtime store — data/state/macd_hynix_runtime.json.

Atomic write + file lock. One snapshot for MarketData / Signal / Order / UI.
Mutex (macd_hynix_mutex.json) remains ONLY for MACD vs Enhanced ownership.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.utils.data_paths import STATE_DIR

RUNTIME_PATH = STATE_DIR / "macd_hynix_runtime.json"
RUNTIME_LOCK_PATH = STATE_DIR / "macd_hynix_runtime.lock"
LEGACY_STATE_PATH = STATE_DIR / "macd_hynix_state.json"

_FILE_LOCK = threading.RLock()

UI_MODES = (
    "STOPPED",
    "BOOTSTRAPPING",
    "READY",
    "RUNNING",
    "DATA_ERROR",
    "SIGNAL_ERROR",
    "ORDER_BLOCKED",
    "WORKER_STALLED",
)


def default_runtime() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ui_mode": "STOPPED",
        "auto_trade_on": False,
        "mode": "mock",
        "budget": 10_000_000,
        "session_date": None,
        "stopped": False,
        "stopped_reason": None,
        "real_confirm_ok": False,
        "masked_account": "",
        "bootstrap": {
            "status": None,
            "ok": False,
            "reason": None,
            "received_1m_bars": 0,
            "prior_day_1m_bars": 0,
            "today_1m_bars": 0,
            "completed_3m_count": 0,
            "kis_requests": 0,
            "time_range": None,
            "elapsed_sec": None,
        },
        "warmup_ready": False,
        "quotes": {
            "hynix": None,
            "long": None,
            "inverse": None,
            "updated_at": None,
            "age_sec": None,
            "status": None,
        },
        "quote_errors": [],
        "signal_calculation_active": False,
        "completed_signal_snapshot": None,
        "macd_status": "NOT_READY",
        "last_signal_direction": None,
        "last_signal_bar_ts": None,
        "last_signal_id": None,
        "processed_signal_ids": [],
        "flag_events_today": [],
        "signal_lifecycle": "IDLE",
        "position": {
            "symbol": None,
            "quantity": 0,
            "avg_price": 0.0,
            "entry_at": None,
            "entry_kind": None,
            "signal_id": None,
        },
        "direction_episode": {
            "id": None,
            "direction": None,
            "continuation_reentry_used": False,
            "sl_lock": False,
            "tp_at": None,
            "last_exit_reason": None,
        },
        "profit_lock": {
            "profit_lock_active": False,
            "peak_net_return": 0.0,
            "current_net_return": 0.0,
            "giveback_pct": 0.0,
        },
        "pipeline": {},
        "order_latency": {},
        "order_latency_last": None,
        "order_latency_history": [],
        "order_block_reason": None,
        "primary_block_reason": None,
        "last_order_error": None,
        "last_event": None,
        "force_liquidate_pending": False,
        "force_liquidate_done_date": None,
        "worker": {
            "alive": False,
            "instance_id": None,
            "started_at": None,
            "thread_count": 0,
            "executor_alive": False,
            "code_sha": None,
            "last_tick_at": None,
            "tick_seq": 0,
            "tick_intervals": [],
            "avg_interval": None,
            "p95_interval": None,
            "last_exception": None,
            "last_exception_traceback": None,
            "stalled": False,
            "stall_reason": None,
        },
        "command": None,
        "command_ack": None,
        "updated_at": None,
        "git_sha": None,
        "continuation_reentry_enabled": False,
        "opening_probe_enabled": False,
        "opening_probe": {"warmup_ready": False},
        # Compat aliases read by older UI/tests
        "display_direction": "HOLD",
        "current_flag": None,
        "last_flag": None,
        "last_macd_bars_ok": False,
        "macd": {},
        "prices": {},
        "completed_signal": None,
        "strategy_enabled": False,
        "scheduler_alive": False,
        "market_data_active": False,
        "order_execution_enabled": False,
        "quote_status": None,
        "bootstrap_status": None,
        "order_status": None,
    }


def _git_sha() -> str:
    try:
        import subprocess

        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[3]),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return out.strip()
    except Exception:
        return ""


def ensure_paths() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _acquire_file_lock(timeout_sec: float = 5.0):
    ensure_paths()
    deadline = time.monotonic() + timeout_sec
    fh = None
    while time.monotonic() < deadline:
        try:
            fh = open(RUNTIME_LOCK_PATH, "a+b")
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except Exception:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
                fh = None
            time.sleep(0.05)
    raise TimeoutError("macd runtime file lock timeout")


def _release_file_lock(fh) -> None:
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    ensure_paths()
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    data = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate_legacy_if_needed() -> None:
    if RUNTIME_PATH.exists():
        return
    if not LEGACY_STATE_PATH.exists():
        return
    try:
        raw = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
        rt = _deep_merge(default_runtime(), raw if isinstance(raw, dict) else {})
        if raw.get("completed_signal") and not rt.get("completed_signal_snapshot"):
            rt["completed_signal_snapshot"] = raw.get("completed_signal")
        if (raw.get("opening_probe") or {}).get("warmup_ready"):
            rt["warmup_ready"] = True
        rt["updated_at"] = datetime.now().isoformat()
        _atomic_write(RUNTIME_PATH, rt)
    except Exception:
        pass


def load_runtime() -> dict[str, Any]:
    with _FILE_LOCK:
        _migrate_legacy_if_needed()
        ensure_paths()
        if not RUNTIME_PATH.exists():
            rt = default_runtime()
            rt["git_sha"] = _git_sha()
            _atomic_write(RUNTIME_PATH, rt)
            return rt
        try:
            raw = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return default_runtime()
            return _deep_merge(default_runtime(), raw)
        except Exception:
            return default_runtime()


def save_runtime(state: dict[str, Any]) -> None:
    payload = dict(state)
    payload["updated_at"] = datetime.now().isoformat()
    payload["git_sha"] = payload.get("git_sha") or _git_sha()
    # Keep legacy aliases in sync for UI/tests
    snap = payload.get("completed_signal_snapshot")
    if snap is not None:
        payload["completed_signal"] = snap
    op = payload.setdefault("opening_probe", {})
    op["warmup_ready"] = bool(payload.get("warmup_ready"))
    payload["bootstrap_status"] = (payload.get("bootstrap") or {}).get("status")
    payload["strategy_enabled"] = bool(payload.get("auto_trade_on"))
    fh = None
    with _FILE_LOCK:
        try:
            fh = _acquire_file_lock()
            _atomic_write(RUNTIME_PATH, payload)
            # Dual-write legacy path so existing OM tests/tools keep working.
            try:
                _atomic_write(LEGACY_STATE_PATH, payload)
            except Exception:
                pass
        finally:
            _release_file_lock(fh)


def write_command(action: str, **kwargs: Any) -> dict[str, Any]:
    rt = load_runtime()
    rt["command"] = {"action": action, "at": datetime.now().isoformat(), **kwargs}
    save_runtime(rt)
    return rt


def ack_command(rt: dict[str, Any], *, ok: bool, message: str = "") -> None:
    cmd = rt.get("command")
    rt["command_ack"] = {
        "action": (cmd or {}).get("action"),
        "ok": ok,
        "message": message,
        "at": datetime.now().isoformat(),
    }
    rt["command"] = None


def set_ui_mode(rt: dict[str, Any], mode: str) -> None:
    if mode in UI_MODES:
        rt["ui_mode"] = mode
