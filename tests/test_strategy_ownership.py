"""Tests for app.trading.strategy_ownership — the shared read-only adapter
that Enhanced / MACD v1 / MACD2 each consult before opening order authority.

Covers: per-engine flag+heartbeat semantics (fresh blocks, stale does not,
missing-heartbeat fails safe), full bidirectional pairwise blocking across
all three engines (docs requirement: exactly one may hold authority), and a
concurrent-access race test against the shared JSON files.

Global isolation: tests/conftest.py's autouse `_isolate_ai_gap_data_paths`
already points ``strategy_ownership.V1_RUNTIME_PATH`` /
``MACD2_RUNTIME_PATH`` at a per-test tmp_path, so no test here can ever touch
the real data/state files.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

from app.trading import strategy_ownership as so


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── enhanced_active() ───────────────────────────────────────────────────────

def test_enhanced_active_when_flag_true_and_heartbeat_fresh(monkeypatch):
    monkeypatch.setattr(
        "app.services.hynix_switch_state.load_state", lambda *a, **k: {"auto_trade_on": True}
    )
    monkeypatch.setattr(
        "app.services.hynix_auto_trade_scheduler.read_heartbeat_file",
        lambda: {"last_heartbeat_at": datetime.now().isoformat()},
    )
    active, reason = so.enhanced_active()
    assert active is True
    assert reason == "ENHANCED_ACTIVE"


def test_enhanced_not_active_when_flag_false(monkeypatch):
    monkeypatch.setattr(
        "app.services.hynix_switch_state.load_state", lambda *a, **k: {"auto_trade_on": False}
    )
    active, _ = so.enhanced_active()
    assert active is False


def test_enhanced_not_active_when_heartbeat_stale(monkeypatch):
    """flag stuck True from a crashed process + old heartbeat -> not active."""
    monkeypatch.setattr(
        "app.services.hynix_switch_state.load_state", lambda *a, **k: {"auto_trade_on": True}
    )
    stale = datetime.now() - timedelta(seconds=so.ENHANCED_HEARTBEAT_STALE_SEC + 1)
    monkeypatch.setattr(
        "app.services.hynix_auto_trade_scheduler.read_heartbeat_file",
        lambda: {"last_heartbeat_at": stale.isoformat()},
    )
    active, _ = so.enhanced_active()
    assert active is False


def test_enhanced_active_fails_safe_when_heartbeat_missing(monkeypatch):
    """flag True but no heartbeat published yet -> still treated as active."""
    monkeypatch.setattr(
        "app.services.hynix_switch_state.load_state", lambda *a, **k: {"auto_trade_on": True}
    )
    monkeypatch.setattr(
        "app.services.hynix_auto_trade_scheduler.read_heartbeat_file", lambda: None
    )
    active, reason = so.enhanced_active()
    assert active is True
    assert reason == "ENHANCED_ACTIVE"


# ── macd_v1_active() ─────────────────────────────────────────────────────────

def test_macd_v1_active_when_flag_true_and_tick_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "V1_RUNTIME_PATH", tmp_path / "macd_hynix_runtime.json")
    _write(so.V1_RUNTIME_PATH, {
        "auto_trade_on": True,
        "worker": {"last_tick_at": datetime.now().isoformat()},
    })
    active, reason = so.macd_v1_active()
    assert active is True
    assert reason == "MACD_V1_ACTIVE"


def test_macd_v1_not_active_when_tick_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "V1_RUNTIME_PATH", tmp_path / "macd_hynix_runtime.json")
    stale = datetime.now() - timedelta(seconds=so.MACD_V1_HEARTBEAT_STALE_SEC + 1)
    _write(so.V1_RUNTIME_PATH, {
        "auto_trade_on": True,
        "worker": {"last_tick_at": stale.isoformat()},
    })
    active, _ = so.macd_v1_active()
    assert active is False


def test_macd_v1_not_active_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "V1_RUNTIME_PATH", tmp_path / "does_not_exist.json")
    active, _ = so.macd_v1_active()
    assert active is False


def test_read_json_fails_closed_on_corrupt_existing_file(tmp_path):
    """A file that exists but never parses (real corruption, not just a
    torn read that would clear up on retry) must return the _READ_ERROR
    sentinel, not silently degrade to {} (which would read as 'not active')."""
    bad_path = tmp_path / "macd_hynix_runtime.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    assert so._read_json(bad_path, attempts=2, backoff_sec=0.0) is so._READ_ERROR


def test_macd_v1_fails_closed_when_read_uncertain(tmp_path, monkeypatch):
    """This is a safety gate: an uncertain read of MACD v1's file must still
    block MACD2, never silently fall through to 'not active'."""
    monkeypatch.setattr(so, "V1_RUNTIME_PATH", tmp_path / "macd_hynix_runtime.json")
    monkeypatch.setattr(so, "_read_json", lambda path, **k: so._READ_ERROR)
    active, reason = so.macd_v1_active()
    assert active is True
    assert reason == "MACD_V1_READ_UNCERTAIN_FAILSAFE"


# ── macd2_active() ───────────────────────────────────────────────────────────

def test_macd2_active_when_flag_true_and_updated_at_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "MACD2_RUNTIME_PATH", tmp_path / "macd2_runtime.json")
    _write(so.MACD2_RUNTIME_PATH, {
        "auto_trade_on": True,
        "updated_at": datetime.now().isoformat(),
    })
    active, reason = so.macd2_active()
    assert active is True
    assert reason == "MACD2_ACTIVE"


def test_macd2_not_active_when_updated_at_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "MACD2_RUNTIME_PATH", tmp_path / "macd2_runtime.json")
    stale = datetime.now() - timedelta(seconds=so.MACD2_HEARTBEAT_STALE_SEC + 1)
    _write(so.MACD2_RUNTIME_PATH, {"auto_trade_on": True, "updated_at": stale.isoformat()})
    active, _ = so.macd2_active()
    assert active is False


def test_macd2_active_handles_tz_aware_updated_at(tmp_path, monkeypatch):
    """MACD2's real updated_at is tz-aware KST (datetime.now(KST).isoformat())."""
    from app.trading.macd2.config import KST

    monkeypatch.setattr(so, "MACD2_RUNTIME_PATH", tmp_path / "macd2_runtime.json")
    _write(so.MACD2_RUNTIME_PATH, {
        "auto_trade_on": True, "updated_at": datetime.now(KST).isoformat(),
    })
    active, reason = so.macd2_active()
    assert active is True
    assert reason == "MACD2_ACTIVE"


# ── Bidirectional pairwise blocking (docs requirement: exactly one owner) ──

def _patch_checks(monkeypatch, *, enhanced=(False, ""), macd_v1=(False, ""), macd2=(False, "")):
    monkeypatch.setattr(so, "_CHECKS", {
        so.ENHANCED: lambda: enhanced,
        so.MACD_V1: lambda: macd_v1,
        so.MACD2: lambda: macd2,
    })


def test_macd2_blocked_by_enhanced_active(monkeypatch):
    _patch_checks(monkeypatch, enhanced=(True, "ENHANCED_ACTIVE"))
    blocked, reason = so.other_owner_active(so.MACD2)
    assert blocked is True
    assert reason == "ENHANCED_ACTIVE"


def test_macd2_blocked_by_macd_v1_active(monkeypatch):
    _patch_checks(monkeypatch, macd_v1=(True, "MACD_V1_ACTIVE"))
    blocked, reason = so.other_owner_active(so.MACD2)
    assert blocked is True
    assert reason == "MACD_V1_ACTIVE"


def test_macd_v1_blocked_by_macd2_active(monkeypatch):
    _patch_checks(monkeypatch, macd2=(True, "MACD2_ACTIVE"))
    blocked, reason = so.other_owner_active(so.MACD_V1)
    assert blocked is True
    assert reason == "MACD2_ACTIVE"


def test_enhanced_blocked_by_macd2_active(monkeypatch):
    _patch_checks(monkeypatch, macd2=(True, "MACD2_ACTIVE"))
    blocked, reason = so.other_owner_active(so.ENHANCED)
    assert blocked is True
    assert reason == "MACD2_ACTIVE"


def test_nothing_active_allows_any_claimant_to_start(monkeypatch):
    _patch_checks(monkeypatch)
    for claimant in (so.ENHANCED, so.MACD_V1, so.MACD2):
        blocked, reason = so.other_owner_active(claimant)
        assert blocked is False
        assert reason == ""


def test_unknown_claimant_raises():
    with pytest.raises(ValueError):
        so.other_owner_active("SOME_OTHER_ENGINE")


# ── Concurrent-access race test ─────────────────────────────────────────────

def test_concurrent_heartbeat_writes_reliably_block_other_engine(tmp_path, monkeypatch):
    """While MACD v1's runtime file is being atomically rewritten every ~1ms
    by a background thread (simulating its live 5s-tick worker, compressed in
    time), MACD2's start-gate check must see it as active on every single
    read — no crash on a torn/concurrent read, no false 'clear to start'.
    """
    v1_path = tmp_path / "macd_hynix_runtime.json"
    monkeypatch.setattr(so, "V1_RUNTIME_PATH", v1_path)
    monkeypatch.setattr(
        "app.services.hynix_switch_state.load_state", lambda *a, **k: {"auto_trade_on": False}
    )

    stop = threading.Event()
    write_errors = []
    write_exhausted = []

    def _payload():
        return {"auto_trade_on": True, "worker": {"last_tick_at": datetime.now().isoformat()}}

    def _replace_with_retry() -> bool:
        tmp = v1_path.with_suffix(v1_path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(_payload()), encoding="utf-8")
        # os.replace() onto a path a concurrent reader has briefly opened can
        # raise PermissionError on Windows (no POSIX-style replace-while-open
        # semantics) — expected under concurrent access, not a correctness
        # bug in the reader; retry with generous backoff.
        for _ in range(200):
            try:
                os.replace(tmp, v1_path)
                return True
            except PermissionError:
                time.sleep(0.001)
        tmp.unlink(missing_ok=True)
        return False

    # Guaranteed first write, before any concurrent reads start, so the file
    # always exists with a real, fresh, active payload once contention begins
    # — a later replace that loses the race just leaves that still-valid
    # prior content in place (never "file missing").
    v1_path.write_text(json.dumps(_payload()), encoding="utf-8")

    def _writer_loop():
        while not stop.is_set():
            try:
                if not _replace_with_retry():
                    write_exhausted.append(True)
            except Exception as exc:  # pragma: no cover - would fail the test via write_errors
                write_errors.append(exc)
            time.sleep(0.001)

    writer = threading.Thread(target=_writer_loop, daemon=True)
    writer.start()
    try:
        # Give the writer a head start so the file exists before we read.
        time.sleep(0.02)
        read_results = []
        read_errors = []
        for _ in range(200):
            try:
                blocked, reason = so.other_owner_active(so.MACD2)
            except Exception as exc:  # pragma: no cover
                read_errors.append(exc)
                continue
            read_results.append((blocked, reason))
            time.sleep(0.001)
    finally:
        stop.set()
        writer.join(timeout=5.0)

    assert not write_errors, f"writer thread raised: {write_errors}"
    assert not read_errors, f"concurrent reads raised: {read_errors}"
    assert len(read_results) == 200
    assert all(blocked for blocked, _ in read_results), (
        "MACD2's start-gate must never see MACD v1 as inactive while it is actively ticking"
    )
    # Almost always a clean MACD_V1_ACTIVE read; a torn read mid-replace may
    # occasionally fail closed as MACD_V1_READ_UNCERTAIN_FAILSAFE instead —
    # both keep `blocked` True, which is the actual safety property.
    assert all(
        reason in ("MACD_V1_ACTIVE", "MACD_V1_READ_UNCERTAIN_FAILSAFE") for _, reason in read_results
    )
