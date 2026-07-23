"""Isolated MACD Hynix order manager: sell-confirm-then-buy, own ledger, locks.

Shared only: KIS broker via create_broker + exit_order_coordinator serialization.
Does not write Enhanced ledger / state / episode files.
"""
from __future__ import annotations

import csv
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.logger import logger
from app.trading import exit_order_coordinator as order_coord
from app.trading.macd_hynix_strategy import (
    CONTINUATION_REENTRY_ENABLED,
    DIR_DOWN,
    DIR_UP,
    ENTRY_CONTINUATION,
    ENTRY_INITIAL,
    ENTRY_OPEN_IMMEDIATE,
    ENTRY_OPEN_SCALE,
    EXIT_OPPOSITE,
    EXIT_OPEN_UNCONFIRMED,
    EXIT_PROFIT_LOCK,
    EXIT_SESSION,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    OPEN_IMMEDIATE_BUDGET_FRACTION,
    OPENING_PROBE_ENABLED,
    SIGNAL_SOURCE_CONTINUATION,
    SIGNAL_SOURCE_OPEN_IMMEDIATE,
    SYMBOL_NAME,
    TRADE_SYMBOLS,
    make_direction_episode_id,
    opposite_symbol,
    target_symbol_for_direction,
)
from app.trading.trading_cost_engine import TradeCostEngine
from app.utils.data_paths import LOGS_DIR, STATE_DIR

STRATEGY_NAME = "MACD_HYNIX_3M"
SIGNAL_SOURCE = "MACD_HIST_3M_B"
# Single runtime store (dual-path: runtime.json is canonical; legacy state.json kept in sync).
STATE_PATH = STATE_DIR / "macd_hynix_runtime.json"
LEGACY_STATE_PATH = STATE_DIR / "macd_hynix_state.json"
MUTEX_PATH = STATE_DIR / "macd_hynix_mutex.json"
LEDGER_PATH = LOGS_DIR / "macd_hynix_execution_ledger.csv"
SIGNAL_LEDGER_PATH = LOGS_DIR / "macd_hynix_signal_ledger.csv"
STATE_LOCK_PATH = STATE_DIR / "macd_hynix_runtime.lock"

_FILE_LOCK = threading.RLock()
_ORDER_PROCESS_LOCK = threading.RLock()  # process-wide MACD order lock

MAX_ORDER_ATTEMPTS = 3
CONFIRM_ATTEMPTS = 5
CONFIRM_DELAY_SEC = 1.0
QUOTE_STALE_SEC = 10.0

LEDGER_COLUMNS = [
    "trade_id", "timestamp", "mode", "macd_signal", "action", "symbol",
    "requested_qty", "executed_qty", "order_price", "executed_price", "order_id",
    "hold_seconds", "gross_pnl", "cost", "net_pnl", "exit_reason", "success",
    "position_confirmed", "signal_id", "idempotency_key", "pipeline_stage",
    "git_sha", "message", "entry_kind", "direction_episode_id", "signal_source",
    "peak_net_return", "current_net_return", "giveback_pct", "profit_lock_active",
    # Order latency instrumentation (ISO timestamps + segment seconds)
    "completed_3m_bar_at", "signal_detected_at", "order_requested_at",
    "kis_order_accepted_at", "broker_executed_at", "position_confirmed_at",
    "lat_bar_to_signal_s", "lat_signal_to_request_s", "lat_request_to_kis_s",
    "lat_kis_to_fill_s", "lat_signal_to_fill_s",
]

LATENCY_TS_KEYS = (
    "completed_3m_bar_at",
    "signal_detected_at",
    "order_requested_at",
    "kis_order_accepted_at",
    "broker_executed_at",
    "position_confirmed_at",
)

PIPELINE_STAGES = [
    "Signal",
    "Sell Requested",
    "Sell Executed",
    "Buy Requested",
    "Buy Executed",
    "Position Confirmed",
    "Ledger Recorded",
]


def _git_sha() -> str:
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return out.strip()
    except Exception:
        return ""


def default_state() -> dict[str, Any]:
    return {
        "auto_trade_on": False,
        "mode": "mock",
        "budget": 10_000_000,
        "stopped": False,
        "stopped_reason": None,
        "display_direction": "HOLD",
        "last_signal_direction": None,
        "last_signal_bar_ts": None,
        "last_signal_id": None,
        "last_signal_at": None,
        "pending_signal_id": None,
        "pending_signal_direction": None,
        "pending_signal_at": None,
        "pending_entry_kind": None,
        "pending_signal_source": None,
        "order_requested_at": None,
        "kis_order_accepted_at": None,
        "broker_executed_at": None,
        "position_confirmed_at": None,
        "completed_3m_bar_at": None,
        "order_latency": {},
        "order_latency_last": None,
        "order_latency_history": [],
        "last_order_at": None,
        "position": {
            "symbol": None,
            "quantity": 0,
            "avg_price": 0.0,
            "entry_at": None,
            "signal_id": None,
            "entry_kind": None,
            "direction_episode_id": None,
            "size_fraction": 1.0,
            "opening_probe": False,
        },
        "direction_episode": {
            "id": None,
            "direction": None,
            "started_at": None,
            "initial_entry_used": False,
            "continuation_reentry_used": False,
            "sl_lock": False,
            "tp_at": None,
            "tp_bar_ts": None,
            "tp_hist_max_abs": 0.0,
            "tp_hynix_price": None,
            "tp_pivot_price": None,
            "last_exit_reason": None,
        },
        "reentry": {
            "eligible": False,
            "block_reason": None,
            "bars_since_tp": 0,
            "hist_contracted": False,
            "hist_last3": [],
            "enabled": CONTINUATION_REENTRY_ENABLED,
        },
        "profit_lock": {
            "peak_net_return": 0.0,
            "current_net_return": 0.0,
            "giveback_pct": 0.0,
            "profit_lock_active": False,
        },
        "last_event": None,
        "prices": {
            "hynix": None,
            "long": None,
            "inverse": None,
            "updated_at": None,
        },
        "macd": {
            "macd": None,
            "signal": None,
            "hist": None,
            "hist_last3": [],
            "hist_deltas": [],
            "reason": None,
        },
        "quote_errors": [],
        "pipeline": {stage: {"ok": None, "at": None, "message": ""} for stage in PIPELINE_STAGES},
        "order_block_reason": None,
        "next_action": "대기",
        "processed_signal_ids": [],
        "worker": {
            "alive": False,
            "last_tick_at": None,
            "tick_n": 0,
            "tick_seq": 0,
            "tick_intervals": [],
            "intervals_buf_len": 0,
            "intervals_buf_cap": 40,
            "avg_interval": None,
            "p95_interval": None,
            # MACD module is 5s-worker only — never defers to Enhanced 3m main cycle.
            "main_cycle_3m_wait_count": 0,
            "completed_3m_bar_at": None,
            "signal_detected_at": None,
            "order_requested_at": None,
            "kis_order_accepted_at": None,
            "broker_executed_at": None,
            "position_confirmed_at": None,
            "stalled": False,
            "stall_reason": None,
            "last_error": None,
            "run_once_source_hash": None,
        },
        # Clear status split — worker alive ≠ strategy running
        "scheduler_alive": False,
        "strategy_enabled": False,
        "market_data_active": False,
        "signal_calculation_active": False,
        "order_execution_enabled": False,
        "bootstrap_status": None,
        "quote_status": None,
        "macd_status": None,
        "order_status": None,
        "quote_source": None,
        "primary_block_reason": None,
        "legacy_truth_debug": {},
        "last_macd_bars_ok": False,
        "last_flag": None,
        "current_flag": None,
        # Unique UP_RED/DOWN_BLUE onsets today (not every 5s held tick).
        "flag_events_today": [],
        "signal_type": None,  # INITIAL | REVERSAL
        "armed_at": None,
        "duplicate_block_reason": None,
        "last_new_signal": False,
        "last_signal_eval": {},
        "last_order_attempt_at": None,
        "last_order_error": None,
        "worker_code_sha": None,
        "force_liquidate_pending": False,
        "force_liquidate_done_date": None,
        "session_date": None,
        "real_confirm_ok": False,
        "masked_account": "",
        "continuation_reentry_enabled": CONTINUATION_REENTRY_ENABLED,
        "opening_probe_enabled": OPENING_PROBE_ENABLED,
        "opening_probe": {
            "warmup_ready": False,
            "warmup_reason": None,
            "warmup_hist_last2": [],
            "warmup_hist_deltas": [],
            "day_open_price": None,
            "window_active": False,
            "window_abandoned": False,
            "immediate_fired_today": False,
            "immediate_direction": None,
            "immediate_signal_id": None,
            "immediate_at": None,
            "awaiting_09_03_confirm": False,
            "scaled_to_full": False,
            "unconfirmed_exit_at": None,
            "last_eval_at": None,
            "last_eval_reason": None,
            "last_eval_signal": None,
            "price_samples_5s": [],
            "confirm_checked": False,
        },
        "updated_at": None,
        "git_sha": _git_sha(),
    }


def ensure_paths() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _write_state_unlocked(state: dict[str, Any]) -> None:
    ensure_paths()
    state = dict(state)
    state["updated_at"] = datetime.now().isoformat()
    state["git_sha"] = _git_sha()
    # Keep snapshot aliases in sync
    if state.get("completed_signal_snapshot") is not None:
        state["completed_signal"] = state.get("completed_signal_snapshot")
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)
    # Dual-write legacy path for tools still pointing at macd_hynix_state.json
    try:
        legacy_tmp = LEGACY_STATE_PATH.with_suffix(".tmp")
        legacy_tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        legacy_tmp.replace(LEGACY_STATE_PATH)
    except Exception:
        pass


def load_state() -> dict[str, Any]:
    ensure_paths()
    with _FILE_LOCK:
        path = STATE_PATH
        if not path.exists() and LEGACY_STATE_PATH.exists():
            path = LEGACY_STATE_PATH
        if not path.exists():
            state = default_state()
            _write_state_unlocked(state)
            return state
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            base = default_state()
            base.update(raw if isinstance(raw, dict) else {})
            if not isinstance(base.get("pipeline"), dict):
                base["pipeline"] = default_state()["pipeline"]
            if not isinstance(base.get("position"), dict):
                base["position"] = default_state()["position"]
            else:
                merged_pos = default_state()["position"]
                merged_pos.update(base["position"])
                base["position"] = merged_pos
            if not isinstance(base.get("worker"), dict):
                base["worker"] = default_state()["worker"]
            else:
                merged_w = default_state()["worker"]
                merged_w.update(base["worker"])
                base["worker"] = merged_w
            if not isinstance(base.get("order_latency"), dict):
                base["order_latency"] = {}
            if not isinstance(base.get("order_latency_history"), list):
                base["order_latency_history"] = []
            if not isinstance(base.get("direction_episode"), dict):
                base["direction_episode"] = default_state()["direction_episode"]
            else:
                merged_ep = default_state()["direction_episode"]
                merged_ep.update(base["direction_episode"])
                base["direction_episode"] = merged_ep
            if not isinstance(base.get("reentry"), dict):
                base["reentry"] = default_state()["reentry"]
            else:
                merged_re = default_state()["reentry"]
                merged_re.update(base["reentry"])
                base["reentry"] = merged_re
            if not isinstance(base.get("profit_lock"), dict):
                base["profit_lock"] = default_state()["profit_lock"]
            else:
                merged_pl = default_state()["profit_lock"]
                merged_pl.update(base["profit_lock"])
                base["profit_lock"] = merged_pl
            if "continuation_reentry_enabled" not in base:
                base["continuation_reentry_enabled"] = CONTINUATION_REENTRY_ENABLED
            if "opening_probe_enabled" not in base:
                base["opening_probe_enabled"] = OPENING_PROBE_ENABLED
            if not isinstance(base.get("opening_probe"), dict):
                base["opening_probe"] = default_state()["opening_probe"]
            else:
                merged_op = default_state()["opening_probe"]
                merged_op.update(base["opening_probe"])
                base["opening_probe"] = merged_op
            if base.get("completed_signal") and not base.get("completed_signal_snapshot"):
                base["completed_signal_snapshot"] = base.get("completed_signal")
            return base
        except Exception as exc:
            logger.error("[MACDHynix] state load failed: %s", exc)
            return default_state()


def save_state(state: dict[str, Any]) -> dict[str, Any]:
    with _FILE_LOCK:
        _write_state_unlocked(state)
        return state


def update_state(**kwargs) -> dict[str, Any]:
    with _FILE_LOCK:
        state = load_state()
        state.update(kwargs)
        _write_state_unlocked(state)
        return state


def set_pipeline_stage(state: dict[str, Any], stage: str, ok: bool, message: str = "") -> None:
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    pipe = state.setdefault("pipeline", {})
    pipe[stage] = {
        "ok": bool(ok),
        "at": datetime.now(kst).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "message": str(message or ""),
    }


def begin_order_latency(
    state: dict[str, Any],
    *,
    signal_id: str,
    completed_3m_bar_at: Optional[str] = None,
    signal_detected_at: Optional[str] = None,
) -> dict[str, Any]:
    """Start a per-signal latency event (instrumentation only — no trading side effects)."""
    detected = signal_detected_at or datetime.now().isoformat()
    event = {
        "signal_id": str(signal_id),
        "completed_3m_bar_at": completed_3m_bar_at,
        "signal_detected_at": detected,
        "order_requested_at": None,
        "kis_order_accepted_at": None,
        "broker_executed_at": None,
        "position_confirmed_at": None,
        "segments_sec": {},
    }
    state["order_latency"] = event
    state["completed_3m_bar_at"] = completed_3m_bar_at
    state["signal_detected_at"] = detected
    for k in ("order_requested_at", "kis_order_accepted_at", "broker_executed_at", "position_confirmed_at"):
        state[k] = None
    w = state.setdefault("worker", {})
    w["completed_3m_bar_at"] = completed_3m_bar_at
    w["signal_detected_at"] = detected
    for k in ("order_requested_at", "kis_order_accepted_at", "broker_executed_at", "position_confirmed_at"):
        w[k] = None
    # MACD never waits on Enhanced 3-minute main cycle.
    w["main_cycle_3m_wait_count"] = int(w.get("main_cycle_3m_wait_count") or 0)
    return event


def stamp_order_latency(
    state: dict[str, Any],
    key: str,
    at: Optional[str] = None,
    *,
    overwrite: bool = False,
) -> str:
    """Stamp one latency timestamp onto the in-flight event + worker mirrors."""
    if key not in LATENCY_TS_KEYS:
        raise ValueError(f"unknown latency key: {key}")
    ts = at or datetime.now().isoformat()
    event = state.setdefault("order_latency", {})
    if overwrite or not event.get(key):
        event[key] = ts
    state[key] = event.get(key) or ts
    state.setdefault("worker", {})[key] = state[key]
    return str(state[key])


def latency_fields_from_state(state: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Ledger column payload for the current (or last) order-latency event."""
    from app.trading.macd_hynix_ledger import compute_latency_segments

    event = dict((state or {}).get("order_latency") or {})
    if not event.get("signal_id"):
        event = dict((state or {}).get("order_latency_last") or {})
    segs = compute_latency_segments(event) if event else {}
    return {
        "completed_3m_bar_at": event.get("completed_3m_bar_at") or "",
        "signal_detected_at": event.get("signal_detected_at") or "",
        "order_requested_at": event.get("order_requested_at") or "",
        "kis_order_accepted_at": event.get("kis_order_accepted_at") or "",
        "broker_executed_at": event.get("broker_executed_at") or "",
        "position_confirmed_at": event.get("position_confirmed_at") or "",
        "lat_bar_to_signal_s": segs.get("bar_complete_to_signal_detect")
        if segs.get("bar_complete_to_signal_detect") is not None
        else "",
        "lat_signal_to_request_s": segs.get("signal_detect_to_order_request")
        if segs.get("signal_detect_to_order_request") is not None
        else "",
        "lat_request_to_kis_s": segs.get("order_request_to_kis_accept")
        if segs.get("order_request_to_kis_accept") is not None
        else "",
        "lat_kis_to_fill_s": segs.get("kis_accept_to_fill_confirm")
        if segs.get("kis_accept_to_fill_confirm") is not None
        else "",
        "lat_signal_to_fill_s": segs.get("signal_detect_to_final_fill")
        if segs.get("signal_detect_to_final_fill") is not None
        else "",
    }


def finalize_order_latency(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Close the in-flight latency event into history (after fill confirm)."""
    from app.trading.macd_hynix_ledger import compute_latency_segments

    event = dict(state.get("order_latency") or {})
    if not event.get("signal_id") or not event.get("signal_detected_at"):
        return None
    event["segments_sec"] = compute_latency_segments(event)
    event["finalized_at"] = datetime.now().isoformat()
    hist = list(state.get("order_latency_history") or [])
    hist.append(event)
    state["order_latency_history"] = hist[-100:]
    state["order_latency_last"] = event
    return event


def write_mutex(*, macd_on: bool, mode: str, reason: str = "") -> None:
    """Write mutual-exclusion record. enabled=False clears ownership (file may remain)."""
    ensure_paths()
    payload = {
        "owner": "MACD" if macd_on else "NONE",
        "enabled": bool(macd_on),
        # Backward-compatible alias for older readers
        "macd_auto_trade_on": bool(macd_on),
        "mode": mode,
        "updated_at": datetime.now().isoformat(),
        "git_sha": _git_sha(),
        "reason": reason,
    }
    MUTEX_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_mutex(*, mode: str = "mock", reason: str = "cleared") -> None:
    """Release MACD ownership. Do not treat a leftover file as 'MACD ON'."""
    write_mutex(macd_on=False, mode=mode, reason=reason)


def read_mutex() -> dict[str, Any]:
    ensure_paths()
    if not MUTEX_PATH.exists():
        return {"owner": "NONE", "enabled": False, "macd_auto_trade_on": False}
    try:
        data = json.loads(MUTEX_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"owner": "NONE", "enabled": False, "macd_auto_trade_on": False}
        enabled = bool(data.get("enabled", data.get("macd_auto_trade_on", False)))
        return {
            "owner": str(data.get("owner") or ("MACD" if enabled else "NONE")),
            "enabled": enabled,
            "macd_auto_trade_on": enabled,
            "mode": data.get("mode"),
            "updated_at": data.get("updated_at"),
            "git_sha": data.get("git_sha"),
            "reason": data.get("reason"),
        }
    except Exception:
        return {"owner": "NONE", "enabled": False, "macd_auto_trade_on": False}


def is_macd_strategy_on() -> bool:
    """True only when MACD strategy is actually enabled (state or live mutex)."""
    try:
        state = load_state()
        if bool(state.get("auto_trade_on")):
            return True
    except Exception:
        pass
    mutex = read_mutex()
    return bool(mutex.get("enabled"))


def _file_mtime_iso(path: Path) -> Optional[str]:
    try:
        if path.exists():
            return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except Exception:
        pass
    return None


def legacy_auto_trade_truth(*, force_disk: bool = True) -> dict[str, Any]:
    """Single source of truth for Enhanced auto_trade_on (orders actually enabled).

    Enhanced UI saves via ``hynix_switch_engine.set_control`` →
    ``hynix_switch_state.save_state_atomic`` which writes:
      - ``STATE_DIR/hynix_auto_state_{mode}.json`` (mode-specific full state)
      - ``STATE_DIR/hynix_strategy_profile_common.json`` (shared keys overlay)

    Enhanced runtime / UI reads via ``hynix_switch_state.load_state()`` which:
      1. loads ``hynix_auto_state_{active_mode}.json``
      2. overlays common profile keys (including auto_trade_on)

    MACD MUST use that same helper — never OR-scan arbitrary state files
    (stale mode file with True while common/effective is False caused
    LEGACY_STRATEGY_ACTIVE false positives).

    Debug dump includes absolute paths, mtimes, AI_GAP_DATA_DIR, and values.
    """
    import os

    from app.utils.data_paths import DATA_ROOT, DATA_ROOT_ENV_VAR
    from app.services import hynix_switch_state as hss

    # force_disk: load_state always reads from disk (no in-process cache today).
    _ = force_disk
    mode = hss.get_active_mode()
    mode_path = (STATE_DIR / f"hynix_auto_state_{mode}.json").resolve()
    common_path = (STATE_DIR / "hynix_strategy_profile_common.json").resolve()
    active_mode_path = (STATE_DIR / "hynix_auto_state_active_mode.json").resolve()

    state = hss.load_state(mode=mode)
    auto_on = bool(state.get("auto_trade_on"))

    mode_raw_on = None
    common_raw_on = None
    try:
        if mode_path.exists():
            raw = json.loads(mode_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "auto_trade_on" in raw:
                mode_raw_on = bool(raw.get("auto_trade_on"))
    except Exception:
        pass
    try:
        if common_path.exists():
            raw = json.loads(common_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "auto_trade_on" in raw:
                common_raw_on = bool(raw.get("auto_trade_on"))
    except Exception:
        pass

    dump = {
        "auto_trade_on": auto_on,
        "active_mode": mode,
        "truth_helper": "app.services.hynix_switch_state.load_state",
        "enhanced_save_path": str(mode_path),
        "enhanced_save_mtime": _file_mtime_iso(mode_path),
        "enhanced_common_path": str(common_path),
        "enhanced_common_mtime": _file_mtime_iso(common_path),
        "active_mode_pointer_path": str(active_mode_path),
        "mode_file_auto_trade_on": mode_raw_on,
        "common_file_auto_trade_on": common_raw_on,
        "macd_read_helper": "app.trading.macd_hynix_order_manager.legacy_auto_trade_truth",
        "macd_state_path": str(STATE_PATH.resolve()),
        "macd_mutex_path": str(MUTEX_PATH.resolve()),
        "STATE_DIR": str(STATE_DIR.resolve()),
        "DATA_ROOT": str(DATA_ROOT.resolve()),
        "AI_GAP_DATA_DIR": os.environ.get(DATA_ROOT_ENV_VAR),
        "AI_GAP_DATA_DIR_env_var": DATA_ROOT_ENV_VAR,
        "read_at": datetime.now().isoformat(),
    }
    debug_path = STATE_DIR / "macd_legacy_truth_debug.json"
    try:
        ensure_paths()
        debug_path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        dump["debug_dump_path"] = str(debug_path.resolve())
    except Exception as exc:
        dump["debug_dump_error"] = str(exc)
    return dump


def read_old_auto_trade_on() -> tuple[bool, str]:
    """Read Enhanced auto_trade_on via the same load_state() Enhanced uses."""
    dump = legacy_auto_trade_truth(force_disk=True)
    if dump.get("auto_trade_on"):
        return True, str(dump.get("enhanced_save_path") or "hynix_switch_state.load_state")
    return False, ""


def can_start_macd(mode: str = "mock") -> tuple[bool, str]:
    """Return (ok, reason). reason uses stable codes for UI primary_block_reason.

    Force-reads Enhanced truth from disk via ``legacy_auto_trade_truth`` /
    ``hynix_switch_state.load_state`` — never treats mutex file existence as
    legacy ON.
    """
    dump = legacy_auto_trade_truth(force_disk=True)
    if dump.get("auto_trade_on"):
        # Only block when Enhanced *actually* auto_trade_on=True after disk read.
        return False, (
            f"LEGACY_STRATEGY_ACTIVE: Enhanced auto_trade_on=True "
            f"(truth={dump.get('truth_helper')}, path={dump.get('enhanced_save_path')})"
        )
    # Legacy OFF: allow start. Clear stale MACD mutex only when MACD state is also off
    # (do not treat leftover mutex file as legacy ON; do not wipe an active MACD run).
    state = load_state()
    if not state.get("auto_trade_on"):
        mutex = read_mutex()
        if mutex.get("enabled"):
            try:
                clear_mutex(mode=mode, reason="stale_mutex_macd_off_legacy_off")
            except Exception:
                pass
    if state.get("force_liquidate_pending"):
        return False, "FORCE_LIQUIDATE_PENDING: 15:00 강제청산 진행 중"
    return True, ""


def refresh_runtime_status(state: dict[str, Any], *, worker_alive: Optional[bool] = None) -> dict[str, Any]:
    """Derive clear status split fields onto state (in-place)."""
    prices = state.get("prices") or {}
    macd = state.get("macd") or {}

    def _num(v: Any) -> bool:
        try:
            return v is not None and float(v) == float(v)  # not NaN
        except Exception:
            return False

    strategy_on = bool(state.get("auto_trade_on"))
    alive = bool(worker_alive) if worker_alive is not None else bool(
        (state.get("worker") or {}).get("alive") or state.get("scheduler_alive")
    )
    # Signal path needs hynix; orders need at least one ETF. Inverse-only blip must not
    # freeze the whole morning when long (+ hynix) quotes are healthy.
    hynix_ok = _num(prices.get("hynix"))
    long_ok = _num(prices.get("long"))
    inv_ok = _num(prices.get("inverse"))
    market_ok = bool(hynix_ok and (long_ok or inv_ok))
    warmup_ready = bool(
        (state.get("opening_probe") or {}).get("warmup_ready")
        or (state.get("bootstrap") or {}).get("ok")
    )
    cs = state.get("completed_signal") or {}
    if str(state.get("macd_status") or "") == "NOT_READY" or str(cs.get("flag") or "") == "NOT_READY":
        signal_ok = False
    elif not warmup_ready:
        signal_ok = False
    else:
        signal_ok = all(_num(macd.get(k)) for k in ("macd", "signal", "hist"))
    order_ok = strategy_on and not state.get("stopped") and not state.get("order_block_reason")

    primary = None
    if not strategy_on:
        primary = "STRATEGY_OFF"
    elif state.get("force_liquidate_pending"):
        primary = "FORCE_LIQUIDATE_PENDING"
    elif state.get("order_block_reason"):
        primary = str(state.get("order_block_reason"))
    elif not market_ok:
        errs = state.get("quote_errors") or []
        primary = "QUOTE_ERROR" if errs else "MARKET_DATA_INACTIVE"
    elif not warmup_ready:
        primary = "NOT_READY"
    elif not signal_ok:
        primary = str((macd.get("reason") if isinstance(macd, dict) else None) or "SIGNAL_INACTIVE")

    boot = state.get("bootstrap") or {}
    boot_status = str(boot.get("status") or state.get("bootstrap_status") or "")
    quote_status = str(state.get("quote_status") or ("OK" if market_ok else "FAILED"))
    if state.get("quote_errors") and not market_ok:
        quote_status = "FAILED"
    elif state.get("quote_errors") and market_ok:
        quote_status = "PARTIAL"
    if not warmup_ready:
        macd_status = "NOT_READY"
    else:
        macd_status = "OK" if signal_ok else str(
            (macd.get("reason") if isinstance(macd, dict) else None) or "SIGNAL_INACTIVE"
        )
    order_enabled = bool(order_ok and market_ok and warmup_ready)
    order_status = "ENABLED" if order_enabled else "BLOCKED"

    state["scheduler_alive"] = alive
    state["strategy_enabled"] = strategy_on
    state["market_data_active"] = market_ok
    state["signal_calculation_active"] = bool(signal_ok and warmup_ready)
    state["order_execution_enabled"] = order_enabled
    state["bootstrap_status"] = boot_status or None
    state["quote_status"] = quote_status
    state["macd_status"] = macd_status
    state["order_status"] = order_status
    state["primary_block_reason"] = primary
    return state


def get_ledger_path() -> Path:
    ensure_paths()
    return LEDGER_PATH


def _append_ledger(row: dict[str, Any]) -> str:
    ensure_paths()
    trade_id = row.get("trade_id") or uuid.uuid4().hex[:16]
    row = dict(row)
    row["trade_id"] = trade_id
    is_new = not LEDGER_PATH.exists()
    with _FILE_LOCK:
        with LEDGER_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in LEDGER_COLUMNS})
    return trade_id


def load_ledger(limit: int = 200) -> list[dict[str, Any]]:
    path = get_ledger_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return rows[-limit:]
    except Exception as exc:
        logger.error("[MACDHynix] ledger load failed: %s", exc)
        return []


SIGNAL_LEDGER_COLUMNS = [
    "timestamp", "trading_date", "flag", "signal_id", "completed_bar_at",
    "bar_ts", "hist_last3", "reason", "ordered", "block_reason", "lifecycle",
]


def append_signal_ledger(row: dict[str, Any]) -> None:
    """Daily signal ledger — separate from execution ledger."""
    ensure_paths()
    is_new = not SIGNAL_LEDGER_PATH.exists()
    with _FILE_LOCK:
        with SIGNAL_LEDGER_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SIGNAL_LEDGER_COLUMNS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in SIGNAL_LEDGER_COLUMNS})


def load_signal_ledger(limit: int = 200) -> list[dict[str, Any]]:
    ensure_paths()
    if not SIGNAL_LEDGER_PATH.exists():
        return []
    try:
        with SIGNAL_LEDGER_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return rows[-limit:]
    except Exception as exc:
        logger.error("[MACDHynix] signal ledger load failed: %s", exc)
        return []


def _pos_qty(pos: Any, symbol: str) -> int:
    pos_symbol = pos.get("symbol") if isinstance(pos, dict) else getattr(pos, "symbol", None)
    if pos_symbol != symbol:
        return 0
    qty = pos.get("quantity") if isinstance(pos, dict) else getattr(pos, "quantity", 0)
    try:
        return int(qty or 0)
    except Exception:
        return 0


def _pos_avg(pos: Any) -> float:
    val = pos.get("avg_price") if isinstance(pos, dict) else getattr(pos, "avg_price", 0)
    try:
        return float(val or 0)
    except Exception:
        return 0.0


def get_held_quantity(broker, symbol: str) -> Optional[int]:
    try:
        positions = broker.get_positions()
    except Exception:
        return None
    total = 0
    found = False
    for pos in positions or []:
        q = _pos_qty(pos, symbol)
        if q > 0 or (isinstance(pos, dict) and pos.get("symbol") == symbol):
            found = True
            total += q
        elif getattr(pos, "symbol", None) == symbol:
            found = True
            total += q
    return total if found else 0


def get_position_snapshot(broker, symbol: str) -> dict[str, Any]:
    qty = get_held_quantity(broker, symbol)
    if qty is None:
        return {"ok": False, "quantity": None, "avg_price": None}
    avg = 0.0
    try:
        for pos in broker.get_positions() or []:
            if _pos_qty(pos, symbol) > 0 or (
                (pos.get("symbol") if isinstance(pos, dict) else getattr(pos, "symbol", None)) == symbol
            ):
                avg = _pos_avg(pos)
                break
    except Exception:
        pass
    return {"ok": True, "quantity": int(qty or 0), "avg_price": avg}


def confirm_quantity(
    broker,
    symbol: str,
    *,
    retry_while_qty_equals: Optional[int] = None,
    attempts: int = CONFIRM_ATTEMPTS,
    delay_seconds: float = CONFIRM_DELAY_SEC,
) -> dict[str, Any]:
    last_error = None
    for idx in range(max(1, attempts)):
        try:
            snap = get_position_snapshot(broker, symbol)
            if not snap.get("ok"):
                raise RuntimeError("broker position query failed")
            qty = int(snap["quantity"] or 0)
            if (
                retry_while_qty_equals is not None
                and qty == retry_while_qty_equals
                and idx < attempts - 1
            ):
                time.sleep(delay_seconds)
                continue
            return {
                "ok": True,
                "quantity": qty,
                "avg_price": snap.get("avg_price"),
                "attempts": idx + 1,
            }
        except Exception as exc:
            last_error = str(exc)
            if idx < attempts - 1:
                time.sleep(delay_seconds)
    return {"ok": False, "quantity": None, "avg_price": None, "error": last_error}


def validate_etf_quotes(
    quotes: dict[str, Any],
    *,
    required_symbols: Optional[list[str]] = None,
) -> tuple[bool, str]:
    """Safety checks on ETF quotes. Does NOT flip MACD direction — only blocks orders.

    When ``required_symbols`` is set, only those ETF slots are checked so a transient
    inverse quote failure cannot block a long-only BUY (and vice versa).
    """
    now = datetime.now()
    want = set(required_symbols or [LONG_SYMBOL, INVERSE_SYMBOL])
    slots = []
    if LONG_SYMBOL in want:
        slots.append(("long", LONG_SYMBOL))
    if INVERSE_SYMBOL in want:
        slots.append(("inverse", INVERSE_SYMBOL))
    for key, symbol in slots:
        q = quotes.get(key) or {}
        price = q.get("price")
        try:
            price_f = float(price)
        except Exception:
            return False, f"ORDER_DATA_INVALID: {symbol} price missing"
        if price_f <= 0:
            return False, f"ORDER_DATA_INVALID: {symbol} price={price_f}"
        updated = q.get("updated_at")
        if updated:
            try:
                ts = datetime.fromisoformat(str(updated))
                if (now - ts).total_seconds() > QUOTE_STALE_SEC:
                    return False, f"ORDER_DATA_INVALID: {symbol} stale quote"
            except Exception:
                pass
        if q.get("bid") is not None and q.get("ask") is not None:
            try:
                if float(q["bid"]) <= 0 or float(q["ask"]) <= 0:
                    return False, f"ORDER_DATA_INVALID: {symbol} bad bid/ask"
            except Exception:
                return False, f"ORDER_DATA_INVALID: {symbol} bid/ask parse"
    # Both ETFs abnormal same-direction spike (only when both quotes present)
    try:
        long_q = quotes.get("long") or {}
        inv_q = quotes.get("inverse") or {}
        if long_q.get("price") and inv_q.get("price"):
            long_chg = float(long_q.get("change_pct") or 0)
            inv_chg = float(inv_q.get("change_pct") or 0)
            if abs(long_chg) >= 3.0 and abs(inv_chg) >= 3.0 and (long_chg * inv_chg) > 0:
                return False, "ORDER_DATA_INVALID: both ETFs abnormal same-direction move"
    except Exception:
        pass
    return True, ""


def create_macd_broker(mode: str, *, real_confirm_text: str = "", real_ready: bool = False):
    """Create broker for MACD worker.

    MOCK never consults REAL confirm phrase / real safety gates — only mock account.
    REAL still requires confirm_text + real_ready.
    """
    from app.trading.broker_factory import create_broker

    mode = "real" if str(mode).lower() == "real" else "mock"
    if mode == "mock":
        # Explicitly ignore any real_confirm_* kwargs leaked from UI/state.
        return create_broker(mode="mock")
    if not real_ready:
        raise RuntimeError(
            "REAL mode requires real_confirm_ok / real_ready — refusing broker create"
        )
    return create_broker(
        mode="real",
        confirm_text=real_confirm_text,
        runtime_real_mode=True,
        runtime_enable_real_buy=True,
        runtime_enable_real_sell=True,
    )


def _order_to_dict(order: Any) -> dict:
    if hasattr(order, "to_dict"):
        return order.to_dict()
    if isinstance(order, dict):
        return dict(order)
    return {
        "success": bool(getattr(order, "success", False)),
        "order_id": getattr(order, "order_id", ""),
        "message": getattr(order, "message", ""),
        "price": getattr(order, "price", None),
        "quantity": getattr(order, "quantity", None),
    }


def reset_profit_lock(state: dict[str, Any]) -> dict[str, Any]:
    """Clear peak/current/giveback tracker (new entry or flat)."""
    pl = {
        "peak_net_return": 0.0,
        "current_net_return": 0.0,
        "giveback_pct": 0.0,
        "profit_lock_active": False,
    }
    state["profit_lock"] = pl
    return pl


def snapshot_profit_lock(state: dict[str, Any]) -> dict[str, Any]:
    pl = state.get("profit_lock") or {}
    return {
        "peak_net_return": float(pl.get("peak_net_return") or 0.0),
        "current_net_return": float(pl.get("current_net_return") or 0.0),
        "giveback_pct": float(pl.get("giveback_pct") or 0.0),
        "profit_lock_active": bool(pl.get("profit_lock_active")),
    }


def _record_fill(
    *,
    mode: str,
    macd_signal: str,
    action: str,
    symbol: str,
    requested_qty: int,
    executed_qty: int,
    order_price: float,
    executed_price: float,
    order_id: str,
    success: bool,
    position_confirmed: bool,
    signal_id: str,
    idempotency_key: str,
    pipeline_stage: str,
    exit_reason: str = "",
    hold_seconds: float = 0.0,
    entry_price: Optional[float] = None,
    message: str = "",
    entry_kind: str = "",
    direction_episode_id: str = "",
    signal_source: str = SIGNAL_SOURCE,
    peak_net_return: Optional[float] = None,
    current_net_return: Optional[float] = None,
    giveback_pct: Optional[float] = None,
    profit_lock_active: Optional[bool] = None,
    latency_fields: Optional[dict[str, Any]] = None,
) -> str:
    """Record ledger only after broker confirmation when success=True."""
    cost_engine = TradeCostEngine()
    gross = 0.0
    cost = 0.0
    net = 0.0
    if action == "SELL" and entry_price and executed_qty > 0:
        breakdown = cost_engine.compute_net_pnl(
            symbol=symbol,
            entry_price=float(entry_price),
            exit_price=float(executed_price),
            quantity=int(executed_qty),
            buy_order_type="market",
            sell_order_type="market",
        )
        gross = float(breakdown.get("gross_pnl") or 0.0)
        cost = float(breakdown.get("total_cost") or 0.0)
        net = float(breakdown.get("net_pnl") or (gross - cost))
    elif action == "BUY" and executed_qty > 0:
        breakdown = cost_engine.compute_trade_cost(
            symbol=symbol,
            side="BUY",
            executed_price=float(executed_price),
            quantity=int(executed_qty),
            order_type="market",
        )
        cost = float(breakdown.get("total_cost") or 0.0)
        net = -cost
        gross = 0.0

    if success and not position_confirmed:
        # Hard rule: never mark success without confirmation
        success = False
        message = (message or "") + " | LEDGER_BLOCKED_UNCONFIRMED"

    row = {
        "timestamp": datetime.now().isoformat(),
        "mode": mode,
        "macd_signal": macd_signal,
        "action": action,
        "symbol": symbol,
        "requested_qty": requested_qty,
        "executed_qty": executed_qty if (success and position_confirmed) else 0,
        "order_price": order_price,
        "executed_price": executed_price if (success and position_confirmed) else "",
        "order_id": order_id,
        "hold_seconds": round(hold_seconds, 1),
        "gross_pnl": round(gross, 2) if (success and position_confirmed) else 0,
        "cost": round(cost, 2) if (success and position_confirmed) else 0,
        "net_pnl": round(net, 2) if (success and position_confirmed) else 0,
        "exit_reason": exit_reason,
        "success": bool(success and position_confirmed),
        "position_confirmed": bool(position_confirmed),
        "signal_id": signal_id,
        "idempotency_key": idempotency_key,
        "pipeline_stage": pipeline_stage,
        "git_sha": _git_sha(),
        "message": message,
        "entry_kind": entry_kind,
        "direction_episode_id": direction_episode_id,
        "signal_source": signal_source,
        "peak_net_return": "" if peak_net_return is None else round(float(peak_net_return), 6),
        "current_net_return": "" if current_net_return is None else round(float(current_net_return), 6),
        "giveback_pct": "" if giveback_pct is None else round(float(giveback_pct), 6),
        "profit_lock_active": "" if profit_lock_active is None else bool(profit_lock_active),
    }
    if latency_fields:
        row.update(latency_fields)
    return _append_ledger(row)


def execute_sell_all(
    broker,
    symbol: str,
    price: float,
    *,
    mode: str,
    signal_id: str,
    macd_signal: str,
    reason: str,
    entry_price: Optional[float] = None,
    entry_at: Optional[str] = None,
    attempt: int = 1,
    entry_kind: str = "",
    direction_episode_id: str = "",
    signal_source: str = SIGNAL_SOURCE,
    peak_net_return: Optional[float] = None,
    current_net_return: Optional[float] = None,
    giveback_pct: Optional[float] = None,
    profit_lock_active: Optional[bool] = None,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if symbol not in TRADE_SYMBOLS:
        return {"success": False, "message": f"invalid trade symbol {symbol}"}
    if attempt > MAX_ORDER_ATTEMPTS:
        return {"success": False, "message": f"MAX_ORDER_ATTEMPTS exceeded ({MAX_ORDER_ATTEMPTS})"}

    account = order_coord.infer_account_id(broker, mode)
    before = get_held_quantity(broker, symbol)
    if before is None:
        return {"success": False, "message": "broker held quantity query failed before sell"}
    before = int(before)
    if before <= 0:
        return {
            "success": True,
            "already_flat": True,
            "sold_quantity": 0,
            "remaining_quantity": 0,
            "message": "already flat",
            "fill_confirmed": True,
        }

    with order_coord.coordinated_order(
        mode=mode,
        account=account,
        symbol=symbol,
        side="SELL",
        episode_id=direction_episode_id or signal_id,
        exit_event_id=f"MACD_SELL:{symbol}:{signal_id}:{attempt}",
        target_qty=before,
        source=signal_source or SIGNAL_SOURCE,
        reason=reason,
    ) as coordinated:
        if coordinated.blocked:
            return {
                "success": False,
                "message": coordinated.block_reason,
                "blocked_by_coordinator": True,
                "idempotency_key": coordinated.idempotency_key,
            }
        order = broker.sell(symbol, SYMBOL_NAME.get(symbol, symbol), before, float(price), order_type="market")
        od = _order_to_dict(order)
        kis_accepted_at = None
        if od.get("success"):
            kis_accepted_at = datetime.now().isoformat()
            if state is not None:
                stamp_order_latency(state, "kis_order_accepted_at", kis_accepted_at, overwrite=False)
        if not od.get("success"):
            coordinated.mark(order_coord.ORDER_FAILED, broker_error=od.get("message"))
            return {
                "success": False,
                "message": od.get("message") or "sell failed",
                "idempotency_key": coordinated.idempotency_key,
                "order": od,
                "kis_order_accepted_at": kis_accepted_at,
            }
        confirmed = confirm_quantity(broker, symbol, retry_while_qty_equals=before)
        remaining = confirmed.get("quantity") if confirmed.get("ok") else None
        sold = None if remaining is None else max(0, before - int(remaining))
        hold_seconds = 0.0
        if entry_at:
            try:
                hold_seconds = max(0.0, (datetime.now() - datetime.fromisoformat(str(entry_at))).total_seconds())
            except Exception:
                hold_seconds = 0.0

        if confirmed.get("ok") and int(remaining or 0) == 0:
            coordinated.mark(
                order_coord.ORDER_FILLED,
                sent_qty=before,
                filled_qty=sold,
                remaining_quantity=remaining,
                broker_order_id=od.get("order_id"),
            )
            broker_at = datetime.now().isoformat()
            _record_fill(
                mode=mode,
                macd_signal=macd_signal,
                action="SELL",
                symbol=symbol,
                requested_qty=before,
                executed_qty=int(sold or 0),
                order_price=float(price),
                executed_price=float(od.get("price") or price),
                order_id=str(od.get("order_id") or ""),
                success=True,
                position_confirmed=True,
                signal_id=signal_id,
                idempotency_key=coordinated.idempotency_key,
                pipeline_stage="Sell Executed",
                exit_reason=reason,
                hold_seconds=hold_seconds,
                entry_price=entry_price,
                entry_kind=entry_kind,
                direction_episode_id=direction_episode_id,
                signal_source=signal_source,
                peak_net_return=peak_net_return,
                current_net_return=current_net_return,
                giveback_pct=giveback_pct,
                profit_lock_active=profit_lock_active,
                latency_fields=latency_fields_from_state(state) if state is not None else None,
            )
            return {
                "success": True,
                "sold_quantity": int(sold or 0),
                "remaining_quantity": 0,
                "fill_confirmed": True,
                "idempotency_key": coordinated.idempotency_key,
                "order": od,
                "kis_order_accepted_at": kis_accepted_at,
                "broker_executed_at": broker_at,
            }

        coordinated.mark(
            order_coord.ORDER_ACCEPTED if od.get("success") else order_coord.ORDER_FAILED,
            sent_qty=before,
            broker_order_id=od.get("order_id"),
            remaining_quantity=remaining,
        )
        _append_ledger({
            "timestamp": datetime.now().isoformat(),
            "mode": mode,
            "macd_signal": macd_signal,
            "action": "SELL",
            "symbol": symbol,
            "requested_qty": before,
            "executed_qty": 0,
            "order_price": price,
            "executed_price": "",
            "order_id": od.get("order_id") or "",
            "hold_seconds": 0,
            "gross_pnl": 0,
            "cost": 0,
            "net_pnl": 0,
            "exit_reason": reason,
            "success": False,
            "position_confirmed": False,
            "signal_id": signal_id,
            "idempotency_key": coordinated.idempotency_key,
            "pipeline_stage": "Sell Requested",
            "git_sha": _git_sha(),
            "message": "sell accepted but qty not confirmed flat",
            "entry_kind": entry_kind,
            "direction_episode_id": direction_episode_id,
            "signal_source": signal_source,
            "peak_net_return": "" if peak_net_return is None else round(float(peak_net_return), 6),
            "current_net_return": "" if current_net_return is None else round(float(current_net_return), 6),
            "giveback_pct": "" if giveback_pct is None else round(float(giveback_pct), 6),
            "profit_lock_active": "" if profit_lock_active is None else bool(profit_lock_active),
        })
        return {
            "success": False,
            "message": "sell not confirmed flat",
            "remaining_quantity": remaining,
            "fill_confirmed": False,
            "idempotency_key": coordinated.idempotency_key,
            "order": od,
        }


def execute_buy(
    broker,
    symbol: str,
    price: float,
    budget: float,
    *,
    mode: str,
    signal_id: str,
    macd_signal: str,
    reason: str,
    attempt: int = 1,
    entry_kind: str = ENTRY_INITIAL,
    direction_episode_id: str = "",
    signal_source: str = SIGNAL_SOURCE,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if symbol not in TRADE_SYMBOLS:
        return {"success": False, "message": f"invalid trade symbol {symbol}"}
    if attempt > MAX_ORDER_ATTEMPTS:
        return {"success": False, "message": f"MAX_ORDER_ATTEMPTS exceeded ({MAX_ORDER_ATTEMPTS})"}
    if price <= 0:
        return {"success": False, "message": "ORDER_DATA_INVALID: buy price <= 0"}

    # Never buy opposite ETF before opposite is flat
    other = opposite_symbol(symbol)
    if other:
        other_qty = get_held_quantity(broker, other)
        if other_qty is None:
            return {"success": False, "message": "cannot verify opposite flat before buy"}
        if int(other_qty) > 0:
            return {
                "success": False,
                "message": f"opposite position still held ({other} qty={other_qty}); buy blocked",
                "opposite_qty": int(other_qty),
            }

    qty = int(float(budget) // float(price))
    if qty < 1:
        return {"success": False, "message": "budget too small for 1 share"}

    account = order_coord.infer_account_id(broker, mode)
    before = get_held_quantity(broker, symbol)
    if before is None:
        return {"success": False, "message": "broker held quantity query failed before buy"}
    before = int(before)

    with order_coord.coordinated_order(
        mode=mode,
        account=account,
        symbol=symbol,
        side="BUY",
        episode_id=direction_episode_id or signal_id,
        exit_event_id=f"MACD_BUY:{symbol}:{signal_id}:{attempt}",
        target_qty=qty,
        source=signal_source or SIGNAL_SOURCE,
        reason=reason,
    ) as coordinated:
        if coordinated.blocked:
            return {
                "success": False,
                "message": coordinated.block_reason,
                "blocked_by_coordinator": True,
                "idempotency_key": coordinated.idempotency_key,
            }
        order = broker.buy(symbol, SYMBOL_NAME.get(symbol, symbol), qty, float(price), order_type="market")
        od = _order_to_dict(order)
        kis_accepted_at = None
        if od.get("success"):
            kis_accepted_at = datetime.now().isoformat()
            if state is not None:
                # Prefer buy accept when sell did not already stamp; else keep first.
                stamp_order_latency(state, "kis_order_accepted_at", kis_accepted_at, overwrite=False)
        if not od.get("success"):
            coordinated.mark(order_coord.ORDER_FAILED, broker_error=od.get("message"))
            return {
                "success": False,
                "message": od.get("message") or "buy failed",
                "idempotency_key": coordinated.idempotency_key,
                "order": od,
                "kis_order_accepted_at": kis_accepted_at,
            }
        confirmed = confirm_quantity(broker, symbol, retry_while_qty_equals=before)
        after = confirmed.get("quantity") if confirmed.get("ok") else None
        filled = None if after is None else max(0, int(after) - before)
        if confirmed.get("ok") and filled and filled > 0:
            coordinated.mark(
                order_coord.ORDER_FILLED,
                sent_qty=qty,
                filled_qty=filled,
                remaining_quantity=after,
                broker_order_id=od.get("order_id"),
            )
            fill_at = datetime.now().isoformat()
            if state is not None:
                stamp_order_latency(state, "broker_executed_at", fill_at, overwrite=True)
                stamp_order_latency(state, "position_confirmed_at", fill_at, overwrite=True)
            _record_fill(
                mode=mode,
                macd_signal=macd_signal,
                action="BUY",
                symbol=symbol,
                requested_qty=qty,
                executed_qty=int(filled),
                order_price=float(price),
                executed_price=float(od.get("price") or price),
                order_id=str(od.get("order_id") or ""),
                success=True,
                position_confirmed=True,
                signal_id=signal_id,
                idempotency_key=coordinated.idempotency_key,
                pipeline_stage="Buy Executed",
                exit_reason=reason,
                entry_kind=entry_kind,
                direction_episode_id=direction_episode_id,
                signal_source=signal_source,
                latency_fields=latency_fields_from_state(state) if state is not None else None,
            )
            return {
                "success": True,
                "bought_quantity": int(filled),
                "after_quantity": int(after or 0),
                "avg_price": confirmed.get("avg_price") or float(od.get("price") or price),
                "fill_confirmed": True,
                "idempotency_key": coordinated.idempotency_key,
                "order": od,
                "kis_order_accepted_at": kis_accepted_at,
                "broker_executed_at": fill_at,
                "position_confirmed_at": fill_at,
            }

        coordinated.mark(order_coord.ORDER_ACCEPTED, sent_qty=qty, broker_order_id=od.get("order_id"))
        _append_ledger({
            "timestamp": datetime.now().isoformat(),
            "mode": mode,
            "macd_signal": macd_signal,
            "action": "BUY",
            "symbol": symbol,
            "requested_qty": qty,
            "executed_qty": 0,
            "order_price": price,
            "executed_price": "",
            "order_id": od.get("order_id") or "",
            "hold_seconds": 0,
            "gross_pnl": 0,
            "cost": 0,
            "net_pnl": 0,
            "exit_reason": reason,
            "success": False,
            "position_confirmed": False,
            "signal_id": signal_id,
            "idempotency_key": coordinated.idempotency_key,
            "pipeline_stage": "Buy Requested",
            "git_sha": _git_sha(),
            "message": "buy accepted but fill not confirmed",
            "entry_kind": entry_kind,
            "direction_episode_id": direction_episode_id,
            "signal_source": signal_source,
            **(latency_fields_from_state(state) if state is not None else {}),
        })
        return {
            "success": False,
            "message": "buy fill not confirmed",
            "fill_confirmed": False,
            "idempotency_key": coordinated.idempotency_key,
            "order": od,
            "kis_order_accepted_at": kis_accepted_at,
        }


def start_direction_episode(state: dict[str, Any], direction: str, bar_ts: Optional[str] = None) -> dict[str, Any]:
    """Begin a new direction_episode (resets re-entry rights)."""
    ep_id = make_direction_episode_id(direction, bar_ts)
    state["direction_episode"] = {
        "id": ep_id,
        "direction": direction,
        "started_at": datetime.now().isoformat(),
        "initial_entry_used": False,
        "continuation_reentry_used": False,
        "sl_lock": False,
        "tp_at": None,
        "tp_bar_ts": None,
        "tp_hist_max_abs": 0.0,
        "tp_hynix_price": None,
        "tp_pivot_price": None,
        "last_exit_reason": None,
    }
    return state["direction_episode"]


def mark_episode_after_exit(
    state: dict[str, Any],
    reason: str,
    *,
    tp_context: Optional[dict[str, Any]] = None,
) -> None:
    """Update episode bookkeeping after a full exit (SL/profit-lock/opposite/session)."""
    ep = state.setdefault("direction_episode", default_state()["direction_episode"])
    ep["last_exit_reason"] = reason
    if reason == EXIT_TP:
        # Legacy TP path (replay / disabled live); kept for continuation bookkeeping.
        ep["tp_at"] = datetime.now().isoformat()
        ctx = tp_context or {}
        ep["tp_bar_ts"] = ctx.get("tp_bar_ts") or ep.get("tp_bar_ts")
        ep["tp_hist_max_abs"] = float(ctx.get("tp_hist_max_abs") or ep.get("tp_hist_max_abs") or 0.0)
        ep["tp_hynix_price"] = ctx.get("tp_hynix_price")
        direction = ep.get("direction")
        if direction == "DOWN_BLUE":
            ep["tp_pivot_price"] = ctx.get("tp_pivot_low") or ctx.get("tp_hynix_price")
        else:
            ep["tp_pivot_price"] = ctx.get("tp_pivot_high") or ctx.get("tp_hynix_price")
    elif reason == EXIT_PROFIT_LOCK:
        # Winning trail exit — no continuation arm (re-entry stays OFF by default).
        ep["tp_at"] = None
    elif reason == EXIT_SL:
        # Same episode: forbid continuation re-entry; do not unlock by time alone.
        ep["sl_lock"] = True
        ep["tp_at"] = None
    elif reason in (EXIT_OPPOSITE, EXIT_SESSION, "15:00_FORCE_LIQUIDATE", "EOD_FLAT"):
        # Opposite / session ends episode rights
        state["direction_episode"] = default_state()["direction_episode"]
        state["direction_episode"]["last_exit_reason"] = reason
    reset_profit_lock(state)


def exit_position_full(
    broker,
    *,
    mode: str,
    quotes: dict[str, Any],
    state: dict[str, Any],
    reason: str,
    signal_id: Optional[str] = None,
    tp_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Full flatten of current MACD position (SL/profit-lock/session). No buy."""
    with _ORDER_PROCESS_LOCK:
        pos = state.get("position") or {}
        symbol = pos.get("symbol")
        if not symbol or int(pos.get("quantity") or 0) <= 0:
            return {"success": True, "already_flat": True}
        price_key = "long" if symbol == LONG_SYMBOL else "inverse"
        price = float((quotes.get(price_key) or {}).get("price") or 0)
        if price <= 0:
            price = float(pos.get("avg_price") or 0)
        if price <= 0:
            return {"success": False, "message": "ORDER_DATA_INVALID: missing exit price", "order_data_invalid": True}

        sid = signal_id or f"{reason}:{datetime.now().strftime('%Y%m%d%H%M%S')}"
        ep = state.get("direction_episode") or {}
        pl = snapshot_profit_lock(state)
        set_pipeline_stage(state, "Sell Requested", True, reason)
        sell_res = None
        for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
            sell_res = execute_sell_all(
                broker,
                symbol,
                price,
                mode=mode,
                signal_id=sid,
                macd_signal=str(ep.get("direction") or state.get("display_direction") or ""),
                reason=reason,
                entry_price=float(pos.get("avg_price") or 0) or None,
                entry_at=pos.get("entry_at"),
                attempt=attempt,
                entry_kind=str(pos.get("entry_kind") or ""),
                direction_episode_id=str(pos.get("direction_episode_id") or ep.get("id") or ""),
                signal_source=SIGNAL_SOURCE,
                peak_net_return=pl.get("peak_net_return"),
                current_net_return=pl.get("current_net_return"),
                giveback_pct=pl.get("giveback_pct"),
                profit_lock_active=pl.get("profit_lock_active"),
            )
            if sell_res.get("success") and (sell_res.get("fill_confirmed") or sell_res.get("already_flat")):
                break
        if not sell_res or not sell_res.get("success"):
            msg = (sell_res or {}).get("message") or "exit sell failed"
            set_pipeline_stage(state, "Sell Executed", False, msg)
            return {"success": False, "message": msg, "sell": sell_res}

        set_pipeline_stage(state, "Sell Executed", True, reason)
        state["position"] = {
            "symbol": None,
            "quantity": 0,
            "avg_price": 0.0,
            "entry_at": None,
            "signal_id": None,
            "entry_kind": None,
            "direction_episode_id": None,
        }
        mark_episode_after_exit(state, reason, tp_context=tp_context)
        state["last_event"] = reason
        state["last_order_at"] = datetime.now().isoformat()
        return {"success": True, "sell": sell_res, "reason": reason}


def switch_to_direction(
    broker,
    direction: str,
    *,
    mode: str,
    budget: float,
    quotes: dict[str, Any],
    signal_id: str,
    state: dict[str, Any],
    entry_kind: str = ENTRY_INITIAL,
    signal_source: str = SIGNAL_SOURCE,
    sell_reason: Optional[str] = None,
    budget_fraction: float = 1.0,
    allow_same_direction_add: bool = False,
) -> dict[str, Any]:
    """Full switch: sell opposite (if any) → confirm 0 → buy target. Never buy before sell confirm."""
    with _ORDER_PROCESS_LOCK:
        target = target_symbol_for_direction(direction)
        if not target:
            return {"success": False, "message": "unknown direction"}

        # Peek holdings first so we only require quotes for symbols we will trade.
        live_target = get_held_quantity(broker, target)
        live_other_sym = opposite_symbol(target)
        live_other = get_held_quantity(broker, live_other_sym) if live_other_sym else 0
        if live_target is None or (live_other_sym and live_other is None):
            return {"success": False, "message": "ORDER_DATA_INVALID: holdings query failed"}

        need_syms = [target]
        if live_other_sym and int(live_other or 0) > 0:
            need_syms.append(live_other_sym)
        held0 = (state.get("position") or {}).get("symbol")
        if held0 and held0 != target and held0 not in need_syms:
            need_syms.append(held0)

        ok_q, q_reason = validate_etf_quotes(quotes, required_symbols=need_syms)
        if not ok_q:
            state["order_block_reason"] = q_reason
            state["last_order_error"] = q_reason
            set_pipeline_stage(state, "Sell Requested", False, q_reason)
            return {"success": False, "message": q_reason, "order_data_invalid": True}

        processed = list(state.get("processed_signal_ids") or [])
        if signal_id in processed:
            return {"success": False, "message": "duplicate signal_id blocked", "duplicate": True}

        pos = state.get("position") or {}
        held_symbol = pos.get("symbol")
        if (
            held_symbol == target
            and int(pos.get("quantity") or 0) > 0
            and not allow_same_direction_add
        ):
            return {"success": True, "message": "same direction — no add", "skipped_same_direction": True}

        if (
            int(live_target or 0) > 0
            and int(live_other or 0) == 0
            and not allow_same_direction_add
        ):
            # Already on target
            state["processed_signal_ids"] = (processed + [signal_id])[-50:]
            return {"success": True, "message": "already holding target", "skipped_same_direction": True}

        # Continuation re-entry: must be flat and same episode direction
        if entry_kind == ENTRY_CONTINUATION:
            ep = state.get("direction_episode") or {}
            if ep.get("sl_lock"):
                return {"success": False, "message": "SL_LOCK blocks continuation re-entry"}
            if ep.get("continuation_reentry_used"):
                return {"success": False, "message": "continuation re-entry already used"}
            if int(live_other or 0) > 0 or int(live_target or 0) > 0:
                return {"success": False, "message": "continuation requires flat book"}
            if str(ep.get("direction") or "") != direction:
                return {"success": False, "message": "continuation direction mismatch"}

        macd_signal = direction
        set_pipeline_stage(state, "Signal", True, signal_id)
        stamp_order_latency(state, "order_requested_at", overwrite=True)
        state["last_order_attempt_at"] = datetime.now().isoformat()
        state["last_order_error"] = None

        ep = state.get("direction_episode") or {}
        ep_id = str(ep.get("id") or "")
        exit_reason = sell_reason or (
            EXIT_OPPOSITE if (held_symbol and held_symbol != target) else f"SWITCH_TO_{direction}"
        )

        # 1) Sell opposite / any non-target holdings first (skip when scaling same dir)
        if not allow_same_direction_add:
            sell_symbols = []
            if live_other_sym and int(live_other or 0) > 0:
                sell_symbols.append(live_other_sym)
            for sell_sym in sell_symbols:
                price_key = "long" if sell_sym == LONG_SYMBOL else "inverse"
                sell_price = float((quotes.get(price_key) or {}).get("price") or 0)
                if sell_price <= 0:
                    msg = f"ORDER_DATA_INVALID: missing sell price for {sell_sym}"
                    set_pipeline_stage(state, "Sell Requested", False, msg)
                    return {"success": False, "message": msg, "order_data_invalid": True}
                set_pipeline_stage(state, "Sell Requested", True, sell_sym)
                sell_res = None
                pl = snapshot_profit_lock(state)
                for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
                    sell_res = execute_sell_all(
                        broker,
                        sell_sym,
                        sell_price,
                        mode=mode,
                        signal_id=signal_id,
                        macd_signal=macd_signal,
                        reason=exit_reason,
                        entry_price=float(pos.get("avg_price") or 0) or None,
                        entry_at=pos.get("entry_at"),
                        attempt=attempt,
                        entry_kind=str(pos.get("entry_kind") or ""),
                        direction_episode_id=str(pos.get("direction_episode_id") or ep_id),
                        signal_source=signal_source,
                        peak_net_return=pl.get("peak_net_return"),
                        current_net_return=pl.get("current_net_return"),
                        giveback_pct=pl.get("giveback_pct"),
                        profit_lock_active=pl.get("profit_lock_active"),
                        state=state,
                    )
                    if sell_res.get("success") and (sell_res.get("fill_confirmed") or sell_res.get("already_flat")):
                        break
                if not sell_res or not sell_res.get("success"):
                    msg = (sell_res or {}).get("message") or "sell failed"
                    set_pipeline_stage(state, "Sell Executed", False, msg)
                    return {"success": False, "message": msg, "sell": sell_res}
                set_pipeline_stage(state, "Sell Executed", True, f"{sell_sym} flat")
                confirm = confirm_quantity(broker, sell_sym)
                if not confirm.get("ok") or int(confirm.get("quantity") or 0) != 0:
                    msg = f"sell confirm failed; remaining={confirm.get('quantity')}"
                    set_pipeline_stage(state, "Sell Executed", False, msg)
                    return {"success": False, "message": msg, "sell": sell_res}
                if exit_reason == EXIT_OPPOSITE:
                    mark_episode_after_exit(state, EXIT_OPPOSITE)

            state["position"] = {
                "symbol": None,
                "quantity": 0,
                "avg_price": 0.0,
                "entry_at": None,
                "signal_id": None,
                "entry_kind": None,
                "direction_episode_id": None,
                "size_fraction": 1.0,
                "opening_probe": False,
            }

        # New episode on initial MACD first-turn (not continuation / not scale add)
        if entry_kind == ENTRY_INITIAL or entry_kind == ENTRY_OPEN_IMMEDIATE:
            ep = start_direction_episode(state, direction, bar_ts=signal_id.split(":")[-1] if signal_id else None)
            ep_id = str(ep.get("id") or "")
        elif entry_kind == ENTRY_OPEN_SCALE:
            ep = state.get("direction_episode") or {}
            ep_id = str(ep.get("id") or "")
        else:
            ep = state.get("direction_episode") or {}
            ep_id = str(ep.get("id") or "")

        # 2) Buy target
        price_key = "long" if target == LONG_SYMBOL else "inverse"
        buy_price = float((quotes.get(price_key) or {}).get("price") or 0)
        if buy_price <= 0:
            msg = f"ORDER_DATA_INVALID: missing buy price for {target}"
            set_pipeline_stage(state, "Buy Requested", False, msg)
            return {"success": False, "message": msg, "order_data_invalid": True}

        buy_reason = ENTRY_CONTINUATION if entry_kind == ENTRY_CONTINUATION else ENTRY_INITIAL
        if entry_kind == ENTRY_OPEN_IMMEDIATE:
            buy_reason = ENTRY_OPEN_IMMEDIATE
        elif entry_kind == ENTRY_OPEN_SCALE:
            buy_reason = ENTRY_OPEN_SCALE
        effective_budget = float(budget) * max(0.0, min(1.0, float(budget_fraction)))
        set_pipeline_stage(state, "Buy Requested", True, target)
        buy_res = None
        for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
            buy_res = execute_buy(
                broker,
                target,
                buy_price,
                effective_budget,
                mode=mode,
                signal_id=signal_id,
                macd_signal=macd_signal,
                reason=buy_reason,
                attempt=attempt,
                entry_kind=entry_kind,
                direction_episode_id=ep_id,
                signal_source=signal_source,
                state=state,
            )
            if buy_res.get("success") and buy_res.get("fill_confirmed"):
                break
            if buy_res.get("opposite_qty"):
                break

        if not buy_res or not buy_res.get("success"):
            msg = (buy_res or {}).get("message") or "buy failed"
            set_pipeline_stage(state, "Buy Executed", False, msg)
            return {"success": False, "message": msg, "buy": buy_res}

        set_pipeline_stage(state, "Buy Executed", True, target)
        set_pipeline_stage(state, "Position Confirmed", True, f"qty={buy_res.get('bought_quantity')}")
        set_pipeline_stage(state, "Ledger Recorded", True, signal_id)

        now_iso = (
            buy_res.get("position_confirmed_at")
            or buy_res.get("broker_executed_at")
            or datetime.now().isoformat()
        )
        if buy_res.get("kis_order_accepted_at"):
            stamp_order_latency(
                state, "kis_order_accepted_at", buy_res["kis_order_accepted_at"], overwrite=False
            )
        stamp_order_latency(state, "broker_executed_at", now_iso, overwrite=True)
        stamp_order_latency(state, "position_confirmed_at", now_iso, overwrite=True)
        finalize_order_latency(state)
        prev_qty = int(pos.get("quantity") or 0) if allow_same_direction_add else 0
        prev_avg = float(pos.get("avg_price") or 0) if allow_same_direction_add else 0.0
        bought = int(buy_res.get("bought_quantity") or 0)
        new_avg = float(buy_res.get("avg_price") or buy_price)
        if allow_same_direction_add and prev_qty > 0 and bought > 0:
            total_qty = prev_qty + bought
            new_avg = (prev_avg * prev_qty + new_avg * bought) / total_qty
        else:
            total_qty = bought
        prev_frac = float(pos.get("size_fraction") or 0.0) if allow_same_direction_add else 0.0
        add_frac = max(0.0, min(1.0, float(budget_fraction)))
        size_fraction = min(1.0, prev_frac + add_frac) if allow_same_direction_add else add_frac
        state["position"] = {
            "symbol": target,
            "quantity": int(total_qty),
            "avg_price": float(new_avg),
            "entry_at": pos.get("entry_at") or now_iso,
            "signal_id": signal_id,
            "entry_kind": entry_kind,
            "direction_episode_id": ep_id,
            "size_fraction": size_fraction if size_fraction > 0 else 1.0,
            "opening_probe": entry_kind in (ENTRY_OPEN_IMMEDIATE, ENTRY_OPEN_SCALE)
            or bool(pos.get("opening_probe")),
        }
        if not allow_same_direction_add:
            reset_profit_lock(state)
        ep = state.setdefault("direction_episode", default_state()["direction_episode"])
        if entry_kind == ENTRY_CONTINUATION:
            ep["continuation_reentry_used"] = True
            ep["tp_at"] = None  # consumed TP window
        elif entry_kind in (ENTRY_INITIAL, ENTRY_OPEN_IMMEDIATE):
            ep["initial_entry_used"] = True
        elif entry_kind == ENTRY_OPEN_SCALE:
            op = state.setdefault("opening_probe", default_state()["opening_probe"])
            op["scaled_to_full"] = True
            op["awaiting_09_03_confirm"] = False
        state["broker_executed_at"] = now_iso
        state["last_order_at"] = now_iso
        state["last_event"] = entry_kind
        state.setdefault("worker", {})["broker_executed_at"] = now_iso
        state.setdefault("worker", {})["position_confirmed_at"] = now_iso
        state["processed_signal_ids"] = (processed + [signal_id])[-50:]
        state["order_block_reason"] = None
        state["last_signal_direction"] = direction
        state["last_signal_id"] = signal_id
        return {
            "success": True,
            "buy": buy_res,
            "target": target,
            "entry_kind": entry_kind,
            "message": "switch complete",
            "order_latency": state.get("order_latency_last"),
        }


def scale_opening_probe(
    broker,
    direction: str,
    *,
    mode: str,
    budget: float,
    quotes: dict[str, Any],
    signal_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Add remaining 50% after 09:03 B confirm (same direction)."""
    return switch_to_direction(
        broker,
        direction,
        mode=mode,
        budget=budget,
        quotes=quotes,
        signal_id=signal_id,
        state=state,
        entry_kind=ENTRY_OPEN_SCALE,
        signal_source=SIGNAL_SOURCE_OPEN_IMMEDIATE,
        budget_fraction=OPEN_IMMEDIATE_BUDGET_FRACTION,
        allow_same_direction_add=True,
    )


def flatten_opening_probe_unconfirmed(
    broker,
    *,
    mode: str,
    quotes: dict[str, Any],
    state: dict[str, Any],
    signal_id: Optional[str] = None,
) -> dict[str, Any]:
    """Flatten partial opening-probe position when 09:03 B does not confirm."""
    op = state.setdefault("opening_probe", default_state()["opening_probe"])
    pos = state.get("position") or {}
    if not pos.get("opening_probe") or float(pos.get("size_fraction") or 1.0) >= 1.0:
        op["awaiting_09_03_confirm"] = False
        op["confirm_checked"] = True
        return {"success": True, "skipped": "not_partial_probe"}
    sid = signal_id or f"OPEN_UNCONF:{datetime.now().strftime('%Y%m%d%H%M%S')}"
    res = exit_position_full(
        broker,
        mode=mode,
        quotes=quotes,
        state=state,
        reason=EXIT_OPEN_UNCONFIRMED,
        signal_id=sid,
    )
    if res.get("success"):
        op["awaiting_09_03_confirm"] = False
        op["confirm_checked"] = True
        op["unconfirmed_exit_at"] = datetime.now().isoformat()
        op["scaled_to_full"] = False
        # Keep last_signal_direction — wait for new B flip
    return res


FLAG_EVENT_MAX = 200


def flag_occurrence_key(
    flag: str,
    *,
    signal_id: Optional[str] = None,
    bar_ts: Optional[str] = None,
) -> Optional[str]:
    """Stable unique key for one flag occurrence (signal_id preferred, else bar_ts+flag)."""
    sid = str(signal_id or "").strip()
    if sid:
        return sid
    bts = str(bar_ts or "").strip()
    fl = str(flag or "").strip()
    if bts and fl:
        return f"{bts}|{fl}"
    return None


def resolve_macd_flag_block_reason(
    state: dict[str, Any],
    decision_trace: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Best-effort reason a flag did not produce an order."""
    trace = decision_trace if isinstance(decision_trace, dict) else (state.get("decision_trace") or {})
    for candidate in (
        trace.get("arm_blocked_reason"),
        state.get("duplicate_block_reason"),
        state.get("primary_block_reason"),
        state.get("order_block_reason"),
        state.get("last_order_error"),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    if state.get("stale_worker"):
        return "STALE_WORKER"
    return None


def record_macd_flag_event(
    state: dict[str, Any],
    *,
    ts: str,
    flag: str,
    signal_id: Optional[str] = None,
    bar_ts: Optional[str] = None,
    new_occurrence: bool = False,
    ordered: bool = False,
    block_reason: Optional[str] = None,
    order_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Persist one unique UP_RED/DOWN_BLUE flag occurrence for today's UI summary.

    Dedupes by ``signal_id`` (preferred) or ``bar_ts|flag``. Held 5s ticks with the
    same key do not inflate the count; they may only refresh block/order fields.
    """
    fl = str(flag or "").strip()
    if fl not in (DIR_UP, DIR_DOWN):
        return None
    key = flag_occurrence_key(fl, signal_id=signal_id, bar_ts=bar_ts)
    if not key:
        return None

    events = state.setdefault("flag_events_today", [])
    if not isinstance(events, list):
        events = []
        state["flag_events_today"] = events

    existing: Optional[dict[str, Any]] = None
    sid = str(signal_id or "").strip() or None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("occurrence_key") == key:
            existing = ev
            break
        if sid and ev.get("signal_id") == sid:
            existing = ev
            break

    if existing is None:
        if not new_occurrence:
            # Held tick / late order update: refresh latest same-flag unordered row.
            if ordered or block_reason:
                for ev in reversed(events):
                    if not isinstance(ev, dict) or ev.get("flag") != fl:
                        continue
                    if ordered:
                        ev["ordered"] = True
                        ev["block_reason"] = None
                        if order_id:
                            ev["order_id"] = str(order_id)
                        if sid and not ev.get("signal_id"):
                            ev["signal_id"] = sid
                        return ev
                    if not ev.get("ordered"):
                        ev["block_reason"] = str(block_reason)
                        return ev
            return None
        ev = {
            "ts": str(ts),
            "flag": fl,
            "signal_id": sid,
            "bar_ts": str(bar_ts) if bar_ts else None,
            "occurrence_key": key,
            "ordered": bool(ordered),
            "block_reason": None if ordered else (str(block_reason) if block_reason else None),
            "order_id": str(order_id) if order_id else None,
        }
        events.append(ev)
        if len(events) > FLAG_EVENT_MAX:
            del events[0 : len(events) - FLAG_EVENT_MAX]
        try:
            trading_date = str(state.get("session_date") or "")[:10]
            append_signal_ledger({
                "timestamp": str(ts),
                "trading_date": trading_date,
                "flag": fl,
                "signal_id": sid or "",
                "completed_bar_at": str(bar_ts or ""),
                "bar_ts": str(bar_ts or ""),
                "hist_last3": "",
                "reason": "NEW_OCCURRENCE",
                "ordered": bool(ordered),
                "block_reason": ev.get("block_reason") or "",
                "lifecycle": "DETECTED",
            })
        except Exception as exc:
            logger.warning("[MACDHynix] signal ledger append failed: %s", exc)
        return ev

    existing["occurrence_key"] = key
    if sid and not existing.get("signal_id"):
        existing["signal_id"] = sid
    if bar_ts and not existing.get("bar_ts"):
        existing["bar_ts"] = str(bar_ts)
    if ordered:
        existing["ordered"] = True
        existing["block_reason"] = None
        if order_id:
            existing["order_id"] = str(order_id)
    elif block_reason and not existing.get("ordered"):
        existing["block_reason"] = str(block_reason)
    return existing


def summarize_macd_flag_events(state: Optional[dict[str, Any]]) -> dict[str, Any]:
    """UI helper: today's red/blue flag counts + unordered (missed) events."""
    events = [
        e for e in list((state or {}).get("flag_events_today") or [])
        if isinstance(e, dict) and e.get("flag") in (DIR_UP, DIR_DOWN)
    ]
    missed = [
        {
            "ts": e.get("ts"),
            "flag": e.get("flag"),
            "signal_id": e.get("signal_id"),
            "block_reason": e.get("block_reason") or "UNKNOWN",
        }
        for e in events
        if not e.get("ordered")
    ]
    return {
        "red_count": sum(1 for e in events if e.get("flag") == DIR_UP),
        "blue_count": sum(1 for e in events if e.get("flag") == DIR_DOWN),
        "event_count": len(events),
        "missed_order_events": missed,
        "events": events,
    }


def reset_opening_probe_daily(state: dict[str, Any], *, session_date: str) -> None:
    """Clear per-day opening probe flags (retain enabled flag)."""
    op = default_state()["opening_probe"]
    op["day_open_price"] = state.get("opening_probe", {}).get("day_open_price")
    state["opening_probe"] = op
    state["session_date"] = session_date


def apply_macd_session_day_rollover(state: dict[str, Any], *, session_date: str) -> bool:
    """KST calendar-day rollover: clear prior-day runtime UI + flat direction_state.

    Clears: last_event, pipeline stages, signal/order/position timestamps,
    position-confirmed display, in-flight latency, pending order arming,
    opening-probe daily flags, and ``flag_events_today``.

    When flat (normal after 15:00 liquidate): also clears ``last_signal_direction``
    so today's first valid signed-B onset after 09:00 can enter. Warm-up bars
    still cannot arm (``session_date`` gate on evaluate). Does **not** delete
    broker holdings or permanent ledger rows. When still holding overnight,
    keeps direction_state and local position tracking.

    Returns True when a day change was applied.
    """
    prev = state.get("session_date")
    if prev == session_date:
        return False

    pos = state.get("position") or {}
    held = bool(pos.get("symbol") and int(pos.get("quantity") or 0) > 0)

    reset_opening_probe_daily(state, session_date=session_date)

    # Runtime / UI clear (always)
    state["last_event"] = None
    state["pipeline"] = default_state()["pipeline"]
    state["order_latency"] = {}
    state["order_latency_last"] = None
    state["order_block_reason"] = None
    state["flag_events_today"] = []
    state["pending_signal_id"] = None
    state["pending_signal_direction"] = None
    state["pending_signal_at"] = None
    state["pending_entry_kind"] = None
    state["pending_signal_source"] = None
    state["pending_budget_fraction"] = None
    state["pending_open_scale"] = None
    state["armed_at"] = None
    state["decision_trace"] = None
    state["completed_signal"] = None
    state["duplicate_block_reason"] = None
    state["last_order_error"] = None
    state["primary_error"] = None
    for k in (
        "order_requested_at",
        "kis_order_accepted_at",
        "broker_executed_at",
        "position_confirmed_at",
        "completed_3m_bar_at",
        "signal_detected_at",
        "last_signal_at",
        "last_order_at",
    ):
        state[k] = None
    w = state.setdefault("worker", {})
    for k in (
        "completed_3m_bar_at",
        "signal_detected_at",
        "order_requested_at",
        "kis_order_accepted_at",
        "broker_executed_at",
        "position_confirmed_at",
    ):
        w[k] = None

    if not held:
        # Flat overnight (normal after 15:00 force liquidate): clear local position
        # display + episode exit banner so UI does not show yesterday's event.
        # Also clear direction_state so today's first valid signed-B onset can enter
        # (warm-up bars still cannot arm via session_date gate on evaluate).
        state["position"] = default_state()["position"]
        state["direction_episode"] = default_state()["direction_episode"]
        state["direction_episode"]["last_exit_reason"] = None
        state["last_signal_direction"] = None
        state["last_signal_bar_ts"] = None
        state["last_signal_id"] = None
        state["profit_lock"] = default_state()["profit_lock"]
        state["reentry"] = default_state()["reentry"]
        state["force_liquidate_done_date"] = None
        state["force_liquidate_pending"] = False
        state["processed_signal_ids"] = []
        state["display_direction"] = "HOLD"
        state["last_flag"] = None
        state["current_flag"] = None
        state["signal_type"] = None

    state["session_date"] = session_date
    return True


def force_liquidate_all(
    broker,
    *,
    mode: str,
    quotes: dict[str, Any],
    state: dict[str, Any],
    reason: str = EXIT_SESSION,
) -> dict[str, Any]:
    """Priority flatten of both ETFs. Independent of MACD signal."""
    with _ORDER_PROCESS_LOCK:
        signal_id = f"FORCE_LIQ:{datetime.now().strftime('%Y%m%d')}"
        results = {}
        all_ok = True
        for symbol in TRADE_SYMBOLS:
            qty = get_held_quantity(broker, symbol)
            if qty is None:
                all_ok = False
                results[symbol] = {"success": False, "message": "qty query failed"}
                continue
            if int(qty) <= 0:
                results[symbol] = {"success": True, "already_flat": True}
                continue
            price_key = "long" if symbol == LONG_SYMBOL else "inverse"
            price = float((quotes.get(price_key) or {}).get("price") or 0)
            if price <= 0:
                # Still attempt with last known avg
                price = float((state.get("position") or {}).get("avg_price") or 1)
            pos = state.get("position") or {}
            ep = state.get("direction_episode") or {}
            pl = snapshot_profit_lock(state)
            res = execute_sell_all(
                broker,
                symbol,
                price,
                mode=mode,
                signal_id=signal_id,
                macd_signal="FORCE",
                reason=reason,
                entry_price=float(pos.get("avg_price") or 0) or None,
                entry_at=pos.get("entry_at"),
                entry_kind=str(pos.get("entry_kind") or ""),
                direction_episode_id=str(pos.get("direction_episode_id") or ep.get("id") or ""),
                signal_source=SIGNAL_SOURCE,
                peak_net_return=pl.get("peak_net_return"),
                current_net_return=pl.get("current_net_return"),
                giveback_pct=pl.get("giveback_pct"),
                profit_lock_active=pl.get("profit_lock_active"),
            )
            results[symbol] = res
            if not res.get("success"):
                all_ok = False
        if all_ok:
            state["position"] = {
                "symbol": None,
                "quantity": 0,
                "avg_price": 0.0,
                "entry_at": None,
                "signal_id": None,
                "entry_kind": None,
                "direction_episode_id": None,
            }
            mark_episode_after_exit(state, reason)
            state["last_event"] = reason
            state["force_liquidate_pending"] = False
            state["force_liquidate_done_date"] = datetime.now().strftime("%Y-%m-%d")
            set_pipeline_stage(state, "Position Confirmed", True, reason)
        else:
            state["force_liquidate_pending"] = True
        return {"success": all_ok, "results": results}
