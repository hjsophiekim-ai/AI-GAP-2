"""test_hynix_auto_trade_scheduler.py — 장 마감 후 EOD regime 분석 스로틀 검증
(2026-07-16 요구사항3: 장 마감 후에도 오늘 저장된 1분봉으로 EOD 분석을 수행하되,
heartbeat마다(수 초~수 분 간격) 매번 재계산하지 않는다)."""

from __future__ import annotations

from datetime import datetime

import app.services.hynix_auto_trade_scheduler as scheduler


def test_maybe_refresh_eod_regime_computes_when_no_prior_result(monkeypatch):
    calls = []
    import app.services.hynix_switch_engine as engine_module
    monkeypatch.setattr(engine_module, "compute_eod_regime_only", lambda **kw: calls.append(kw))

    state = {"auto_trade_on": True, "mode": "mock", "adaptive_regime_eod": None}
    scheduler._maybe_refresh_eod_regime(datetime(2026, 7, 16, 15, 45), state)

    assert len(calls) == 1


def test_maybe_refresh_eod_regime_skips_when_recently_computed(monkeypatch):
    calls = []
    import app.services.hynix_switch_engine as engine_module
    monkeypatch.setattr(engine_module, "compute_eod_regime_only", lambda **kw: calls.append(kw))

    state = {
        "auto_trade_on": True, "mode": "mock",
        "adaptive_regime_eod": {"snapshot": {"computed_at": datetime(2026, 7, 16, 15, 40).isoformat(timespec="seconds")}},
    }
    # 5분 뒤(간격 900초=15분 미만) — 재계산하지 않는다.
    scheduler._maybe_refresh_eod_regime(datetime(2026, 7, 16, 15, 45), state)

    assert len(calls) == 0


def test_maybe_refresh_eod_regime_recomputes_after_interval_elapsed(monkeypatch):
    calls = []
    import app.services.hynix_switch_engine as engine_module
    monkeypatch.setattr(engine_module, "compute_eod_regime_only", lambda **kw: calls.append(kw))

    state = {
        "auto_trade_on": True, "mode": "mock",
        "adaptive_regime_eod": {"snapshot": {"computed_at": datetime(2026, 7, 16, 15, 40).isoformat(timespec="seconds")}},
    }
    # 20분 뒤(간격 900초=15분 초과) — 재계산한다.
    scheduler._maybe_refresh_eod_regime(datetime(2026, 7, 16, 16, 0), state)

    assert len(calls) == 1


def test_maybe_refresh_eod_regime_skips_when_auto_trade_off(monkeypatch):
    calls = []
    import app.services.hynix_switch_engine as engine_module
    monkeypatch.setattr(engine_module, "compute_eod_regime_only", lambda **kw: calls.append(kw))

    state = {"auto_trade_on": False, "mode": "mock", "adaptive_regime_eod": None}
    scheduler._maybe_refresh_eod_regime(datetime(2026, 7, 16, 15, 45), state)

    assert len(calls) == 0


def test_ensure_auto_trade_background_threads_starts_both_loops(monkeypatch):
    class _FakeThread:
        def __init__(self, interval_seconds=0):
            self.interval_seconds = interval_seconds
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

        def stop(self):
            self.started = False

    try:
        scheduler.stop_cycle_thread()
        scheduler.stop_fast_trend_watcher()
        monkeypatch.setattr(scheduler, "HynixAutoTradeCycleThread", _FakeThread)
        monkeypatch.setattr(scheduler, "HynixFastTrendWatcherThread", _FakeThread)

        result = scheduler.ensure_auto_trade_background_threads(
            cycle_interval_seconds=999,
            fast_interval_seconds=999,
        )
        second = scheduler.ensure_auto_trade_background_threads(
            cycle_interval_seconds=999,
            fast_interval_seconds=999,
        )

        assert result["cycle_thread_alive"] is True
        assert result["fast_thread_alive"] is True
        assert second["cycle_thread_alive"] is True
        assert second["fast_thread_alive"] is True
    finally:
        scheduler.stop_cycle_thread()
        scheduler.stop_fast_trend_watcher()


def test_fast_watcher_uses_five_second_cadence_when_auto_trade_on_even_if_early_live_off():
    watcher = scheduler.HynixFastTrendWatcherThread()

    assert watcher._fast_cadence_active({
        "auto_trade_on": True,
        "stopped": False,
        "early_trend_detector_enabled": False,
        "early_trend_detector_live": False,
    }) is True
    assert watcher._fast_cadence_active({
        "auto_trade_on": True,
        "stopped": True,
        "early_trend_detector_enabled": True,
        "early_trend_detector_live": True,
    }) is False


# 2026-08-23 회귀 테스트 — is_within_operating_window()의 요일 체크 누락으로
# 주말에도 HynixFastTrendWatcher가 force_fast_worker_tick/fast_cadence를 그대로
# 통과시켜 5초 이내(심하면 0초) 주기로 계속 돌던 버그. 평일 동작은 바뀌면 안 된다.

_SATURDAY_NOON = datetime(2026, 8, 22, 13, 0)  # 토요일 13:00 KST
_WEEKDAY_NOON = datetime(2026, 8, 24, 13, 0)  # 월요일 13:00 KST (운영창 내부)


def test_next_interval_ignores_force_flag_on_weekend(monkeypatch):
    watcher = scheduler.HynixFastTrendWatcherThread()
    monkeypatch.setattr(scheduler, "kst_now", lambda: _SATURDAY_NOON)
    monkeypatch.setattr(scheduler, "load_state", lambda: {
        "force_fast_worker_tick": True, "auto_trade_on": True, "stopped": False,
    })

    assert watcher._next_interval_seconds() == watcher.interval_seconds


def test_next_interval_ignores_active_cadence_on_weekend(monkeypatch):
    watcher = scheduler.HynixFastTrendWatcherThread()
    monkeypatch.setattr(scheduler, "kst_now", lambda: _SATURDAY_NOON)
    monkeypatch.setattr(scheduler, "load_state", lambda: {
        "force_fast_worker_tick": False, "auto_trade_on": True, "stopped": False,
    })

    assert watcher._next_interval_seconds() == watcher.interval_seconds


def test_next_interval_still_honors_force_flag_on_weekday(monkeypatch):
    """평일 회귀 확인 — 이번 수정으로 평일 동작이 바뀌면 안 된다."""
    watcher = scheduler.HynixFastTrendWatcherThread()
    monkeypatch.setattr(scheduler, "kst_now", lambda: _WEEKDAY_NOON)
    monkeypatch.setattr(scheduler, "load_state", lambda: {
        "force_fast_worker_tick": True, "auto_trade_on": True, "stopped": False,
    })

    assert watcher._next_interval_seconds() == 0.0


def test_next_interval_still_honors_active_cadence_on_weekday(monkeypatch):
    """평일 회귀 확인 — auto_trade_on 활성 시 여전히 5초 cadence를 써야 한다."""
    watcher = scheduler.HynixFastTrendWatcherThread()
    monkeypatch.setattr(scheduler, "kst_now", lambda: _WEEKDAY_NOON)
    monkeypatch.setattr(scheduler, "load_state", lambda: {
        "force_fast_worker_tick": False, "auto_trade_on": True, "stopped": False,
    })

    assert watcher._next_interval_seconds() == scheduler.EARLY_DETECTOR_FAST_INTERVAL_SECONDS
