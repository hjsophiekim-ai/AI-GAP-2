"""Cross-strategy order-ownership adapter (Enhanced / MACD v1 / MACD2).

Real orders for this KIS account may be placed by exactly one of three
independent engines at a time: Enhanced (app.services.hynix_switch_engine),
MACD v1 (app.trading.macd_hynix_order_manager / macd_hynix_worker), and
MACD2 (app.trading.macd2.*). Before this module existed, MACD2's own start
gate (app.trading.macd2.service.other_strategy_active) checked both Enhanced
and MACD v1, but neither Enhanced nor MACD v1 had any way to see MACD2 —
so a MACD2 run plus either of the other two could hold order authority at
the same time. This module is the one place that answers "is a given other
engine really placing orders right now?", and both MACD v1's
``macd_hynix_order_manager.can_start_macd`` and Enhanced's start gate
(``app/ui/pages/9_SK하이닉스_자동매매.py``) now also check MACD2 through it.

Each ``*_active()`` check starts from that engine's own persisted
auto_trade_on-equivalent flag, then looks for POSITIVE evidence the flag is
stale: a heartbeat/tick timestamp older than a documented threshold (a small
multiple of that engine's own already-existing tick/heartbeat cadence, cited
next to each constant). Only a heartbeat *older* than the threshold flips an
otherwise-True flag to "not active" — a *missing* heartbeat (e.g. the engine
only just flipped its flag and hasn't ticked yet) is fail-safe: it does not
by itself prove the engine crashed, so it still blocks. This is what lets a
genuinely crashed process (flag stuck True, heartbeat aging past the
threshold) stop blocking every other engine forever, without ever allowing
a "no heartbeat yet" startup race to slip through (see
``other_owner_active``'s bidirectional-block tests).

Read-only and additive: this module never writes another engine's state
file, and never imports another engine's production order-execution code —
only Enhanced's own published state-reading helper, or a plain JSON read of
MACD v1 / MACD2's own state file (the same pattern MACD2's service.py
already used for its one-way check).

This is a safety gate, so every read is fail-CLOSED, not fail-open: a
transient read error on a file that another engine is concurrently
atomic-replacing (tmp-write + os.replace, same pattern this module's own
files use) is retried a few times, and if it still can't be read, that is
treated the same as "flag True" — i.e. still blocks — rather than silently
falling back to "file absent -> not active" (see
``test_concurrent_heartbeat_writes_reliably_block_other_engine``, which
caught an earlier version doing the unsafe fail-open thing).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.utils.data_paths import STATE_DIR

ENHANCED = "ENHANCED"
MACD_V1 = "MACD_V1"
MACD2 = "MACD2"
_ALL_CLAIMANTS = (ENHANCED, MACD_V1, MACD2)

# Module-level (not function-local) so tests can monkeypatch these, the same
# way app.trading.macd2.state_store.STATE_PATH is monkeypatched.
V1_RUNTIME_PATH: Path = STATE_DIR / "macd_hynix_runtime.json"
MACD2_RUNTIME_PATH: Path = STATE_DIR / "macd2_runtime.json"

# 3x FAST_WATCHER_INTERVAL_SECONDS(30s) — the fastest cadence Enhanced's own
# heartbeat file is rewritten at, even outside market hours (see
# app/services/hynix_auto_trade_scheduler.py's _write_heartbeat_file calls).
ENHANCED_HEARTBEAT_STALE_SEC = 90.0
# 2x TICK_STALL_SEC(15s) — the same margin macd_hynix_worker.detect_worker_stall
# already uses to call a MACD v1 tick "stalled".
MACD_V1_HEARTBEAT_STALE_SEC = 30.0
# 2x WORKER_STALL_AGE_SEC(15s) — the same margin app.trading.macd2.config /
# Macd2Worker.tick_stats() already uses to call a MACD2 tick "stalled".
MACD2_HEARTBEAT_STALE_SEC = 30.0


def _age_sec(iso_ts: Optional[str]) -> Optional[float]:
    """Seconds since ``iso_ts``, or None if missing/unparseable.

    Handles both naive (MACD v1 / Enhanced, ``datetime.now().isoformat()``)
    and tz-aware (MACD2, ``datetime.now(KST).isoformat()``) timestamps.
    """
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(iso_ts))
    except ValueError:
        return None
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    return max(0.0, (now - ts).total_seconds())


_READ_ERROR = object()  # sentinel: file exists but could not be read even after retries


def _read_json(path: Path, *, attempts: int = 5, backoff_sec: float = 0.002):
    """Plain JSON read with a fail-CLOSED contract: a missing file confidently
    means "no state yet" (``{}``), but a file that exists and still can't be
    read/parsed after a few short retries (e.g. read mid-way through another
    process's atomic tmp-write + os.replace) returns ``_READ_ERROR`` rather
    than silently degrading to ``{}`` — callers must treat that as "assume
    active", not "assume absent"."""
    if not path.exists():
        return {}
    for attempt in range(attempts):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            if attempt + 1 < attempts:
                time.sleep(backoff_sec)
    return _READ_ERROR


def enhanced_active() -> tuple[bool, str]:
    """True only if Enhanced's own auto_trade_on is True AND its scheduler
    heartbeat file is fresh — a crashed process with a stuck flag is not
    treated as active."""
    try:
        from app.services import hynix_switch_state as enhanced_state
        from app.services.hynix_auto_trade_scheduler import read_heartbeat_file
    except ImportError:
        return False, ""  # Enhanced module not present in this environment

    try:
        flag_on = bool(enhanced_state.load_state().get("auto_trade_on"))
    except Exception:
        return True, "ENHANCED_READ_UNCERTAIN_FAILSAFE"
    if not flag_on:
        return False, ""

    try:
        heartbeat = read_heartbeat_file() or {}
    except Exception:
        return True, "ENHANCED_ACTIVE"  # flag True + heartbeat unreadable -> same as missing: still blocks

    age = _age_sec(heartbeat.get("last_heartbeat_at"))
    if age is not None and age > ENHANCED_HEARTBEAT_STALE_SEC:
        return False, ""
    return True, "ENHANCED_ACTIVE"


def macd_v1_active() -> tuple[bool, str]:
    """True only if MACD v1's own auto_trade_on is True AND its worker's
    last_tick_at is fresh. Plain JSON read — never imports MACD v1 code."""
    raw = _read_json(V1_RUNTIME_PATH)
    if raw is _READ_ERROR:
        return True, "MACD_V1_READ_UNCERTAIN_FAILSAFE"
    if not bool(raw.get("auto_trade_on")):
        return False, ""
    age = _age_sec((raw.get("worker") or {}).get("last_tick_at"))
    if age is not None and age > MACD_V1_HEARTBEAT_STALE_SEC:
        return False, ""
    return True, "MACD_V1_ACTIVE"


def macd2_active() -> tuple[bool, str]:
    """True only if MACD2's own auto_trade_on is True AND its state
    updated_at is fresh (rewritten every Worker tick). Plain JSON read —
    never imports MACD2 code."""
    raw = _read_json(MACD2_RUNTIME_PATH)
    if raw is _READ_ERROR:
        return True, "MACD2_READ_UNCERTAIN_FAILSAFE"
    if not bool(raw.get("auto_trade_on")):
        return False, ""
    age = _age_sec(raw.get("updated_at"))
    if age is not None and age > MACD2_HEARTBEAT_STALE_SEC:
        return False, ""
    return True, "MACD2_ACTIVE"


_CHECKS = {
    ENHANCED: enhanced_active,
    MACD_V1: macd_v1_active,
    MACD2: macd2_active,
}


def other_owner_active(claimant: str) -> tuple[bool, str]:
    """For ``claimant`` about to start, check whether either OTHER engine is
    really active right now. Returns (blocked, reason)."""
    if claimant not in _ALL_CLAIMANTS:
        raise ValueError(f"unknown claimant: {claimant!r}")
    for name in _ALL_CLAIMANTS:
        if name == claimant:
            continue
        active, reason = _CHECKS[name]()
        if active:
            return True, reason
    return False, ""
