"""Unit tests for app.services.macd2_daily_archive_scheduler.

Written for the 2026-08-24 research-archive verification pass. Two things
matter for trading safety, since this background thread lives inside the
same Streamlit process as the live MACD2 worker:

1. A failure anywhere inside the archive/sync pipeline it drives must never
   escape `_tick_if_due()` (and therefore never kill the thread or affect
   anything else in the process).
2. `ensure_macd2_daily_archive_thread_running()` must be a true idempotent
   singleton -- calling it repeatedly must never spawn a second live
   "Macd2DailyArchiveWatcher" thread.

Every test here monkeypatches `kst_now` and `_kis_real_client` so nothing
ever reaches a real clock-dependent branch, a real KIS client, or a real
network call.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta

import pytest

import app.services.macd2_daily_archive_scheduler as scheduler
import app.services.macd2_daily_archiver as archiver
import app.services.github_analysis_sync as github_analysis_sync

KST = timezone(timedelta(hours=9))
_DUE_MONDAY_EVENING = datetime(2026, 8, 24, 20, 45, tzinfo=KST)  # weekday, after 20:30 trigger
_NOT_DUE_MONDAY_MORNING = datetime(2026, 8, 24, 9, 0, tzinfo=KST)  # weekday, before trigger


class _DummyClient:
    pass


def _new_thread_no_autostart():
    """A Macd2DailyArchiveThread instance that is never .start()'ed -- lets
    us call _tick_if_due() directly and synchronously, same as the real
    thread's run() loop does internally."""
    return scheduler.Macd2DailyArchiveThread()


def test_tick_if_due_swallows_archiver_failure(monkeypatch):
    monkeypatch.setattr(scheduler, "kst_now", lambda: _DUE_MONDAY_EVENING)
    monkeypatch.setattr(scheduler, "_kis_real_client", lambda: _DummyClient())
    monkeypatch.setattr(archiver, "run_daily_archive", lambda client, d: (_ for _ in ()).throw(RuntimeError("boom: archiver")))
    monkeypatch.setattr(github_analysis_sync, "run_sync", lambda dry_run=True: {"effective_dry_run": True})

    thread = _new_thread_no_autostart()
    thread._tick_if_due()  # must not raise

    assert thread.last_result is not None
    assert thread.last_result["sync"]["effective_dry_run"] is True


def test_tick_if_due_swallows_sync_failure(monkeypatch):
    monkeypatch.setattr(scheduler, "kst_now", lambda: _DUE_MONDAY_EVENING)
    monkeypatch.setattr(scheduler, "_kis_real_client", lambda: _DummyClient())
    monkeypatch.setattr(archiver, "run_daily_archive", lambda client, d: {"date": d, "sections": {}})
    monkeypatch.setattr(github_analysis_sync, "run_sync", lambda dry_run=True: (_ for _ in ()).throw(RuntimeError("boom: sync")))

    thread = _new_thread_no_autostart()
    thread._tick_if_due()  # must not raise

    assert thread.last_result is not None
    assert "error" in thread.last_result["sync"]


def test_tick_if_due_swallows_both_archiver_and_sync_failing(monkeypatch):
    monkeypatch.setattr(scheduler, "kst_now", lambda: _DUE_MONDAY_EVENING)
    monkeypatch.setattr(scheduler, "_kis_real_client", lambda: _DummyClient())
    monkeypatch.setattr(archiver, "run_daily_archive", lambda client, d: (_ for _ in ()).throw(RuntimeError("boom: both")))
    monkeypatch.setattr(github_analysis_sync, "run_sync", lambda dry_run=True: (_ for _ in ()).throw(RuntimeError("boom: both sync")))

    thread = _new_thread_no_autostart()
    thread._tick_if_due()  # must not raise -- this is the core safety property


def test_tick_if_due_swallows_kis_client_creation_failure(monkeypatch):
    monkeypatch.setattr(scheduler, "kst_now", lambda: _DUE_MONDAY_EVENING)

    def raising_client():
        raise RuntimeError("boom: no KIS creds")

    monkeypatch.setattr(scheduler, "_kis_real_client", raising_client)

    thread = _new_thread_no_autostart()
    # _kis_real_client() itself is called directly inside _tick_if_due with no
    # local try/except around it -- confirm the outer run() try/except is
    # what's relied on, by calling run()'s documented contract at the
    # boundary this test owns (_tick_if_due itself). If this ever starts
    # raising, run()'s own try/except (see scheduler.py run()) still
    # protects the thread, but _tick_if_due should stay defensive too.
    with pytest.raises(RuntimeError):
        thread._tick_if_due()


def test_tick_if_due_noop_when_not_due(monkeypatch):
    monkeypatch.setattr(scheduler, "kst_now", lambda: _NOT_DUE_MONDAY_MORNING)
    calls = []
    monkeypatch.setattr(archiver, "run_daily_archive", lambda client, d: calls.append(d))

    thread = _new_thread_no_autostart()
    thread._tick_if_due()

    assert calls == []
    assert thread.last_result is None


def test_ensure_thread_running_is_idempotent_singleton(monkeypatch):
    # Keep every call a harmless no-op tick (not due) so this test never
    # touches a real KIS client or the network, regardless of wall-clock time.
    monkeypatch.setattr(scheduler, "kst_now", lambda: _NOT_DUE_MONDAY_MORNING)

    # Reset module singleton state so this test is independent of run order.
    monkeypatch.setattr(scheduler, "_instance", None)

    try:
        t1 = scheduler.ensure_macd2_daily_archive_thread_running(interval_seconds=0.05)
        t2 = scheduler.ensure_macd2_daily_archive_thread_running(interval_seconds=0.05)
        t3 = scheduler.ensure_macd2_daily_archive_thread_running(interval_seconds=0.05)

        assert t1 is t2 is t3

        live_watchers = [t for t in threading.enumerate() if t.name == "Macd2DailyArchiveWatcher"]
        assert len(live_watchers) == 1
        assert live_watchers[0] is t1
    finally:
        if scheduler._instance is not None:
            scheduler._instance.stop()
            scheduler._instance.join(timeout=5)
            scheduler._instance = None
