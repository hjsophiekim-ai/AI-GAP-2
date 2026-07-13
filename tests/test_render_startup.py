"""tests/test_render_startup.py

Render 배포 시 Streamlit 메인 화면이 흰 화면에서 무한 로딩되는 문제 회귀 방지.

검증 항목:
1. 백그라운드 스레드 부트스트랩에 필요한 모듈 임포트가 5초 이내 완료된다.
2. auto_trade_on=False이면 사이클/틱 어느 경로에서도 KIS API(브로커 생성)를 호출하지 않는다.
3. ensure_watcher_running()/ensure_cycle_thread_running()은 멱등(idempotent) — 반복 호출해도
   스레드가 중복 생성되지 않는다.
4. STARTUP_STEP_* 로그 헬퍼가 예외 메시지를 그대로 노출하지 않고 예외 타입만 남긴다
   (비밀키/전체 계좌번호 로그 미노출 원칙).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1) 앱 import 5초 이내
# ---------------------------------------------------------------------------

def test_bootstrap_modules_import_within_5_seconds():
    """streamlit_app.py가 부트스트랩에 사용하는 두 모듈의 import가 5초를 넘지 않아야 한다.

    이 두 모듈(dynamic_exit_watcher/hynix_auto_trade_scheduler)의 import 체인이
    Render 콜드스타트 시 첫 화면 렌더를 지연시키는 핵심 후보였다.
    """
    code = (
        "import app.trading.dynamic_exit_watcher, "
        "app.services.hynix_auto_trade_scheduler"
    )
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=30,
    )
    elapsed = time.time() - t0
    assert proc.returncode == 0, f"import 실패: {proc.stderr[-2000:]}"
    assert elapsed < 5.0, f"부트스트랩 모듈 import가 5초를 초과함: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 2) auto_trade_on=False → KIS API(브로커 생성) 호출 없음
# ---------------------------------------------------------------------------

def test_tick_never_creates_broker_when_auto_trade_off(tmp_path, monkeypatch):
    import app.services.hynix_switch_state as state_module
    import app.trading.dynamic_exit_watcher as watcher
    import app.trading.broker_factory as broker_factory_module

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = False
    state_module.save_state_atomic(state)

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("auto_trade_on=False인데 create_broker가 호출됨 — KIS API 호출 위험")

    monkeypatch.setattr(broker_factory_module, "create_broker", _fail_if_called)

    from datetime import datetime
    result = watcher.tick(now=datetime.now())
    assert result is None


def test_scheduler_cycle_never_creates_broker_when_auto_trade_off(tmp_path, monkeypatch):
    import app.services.hynix_switch_state as state_module
    import app.services.hynix_auto_trade_scheduler as scheduler_module
    import app.services.hynix_switch_engine as engine_module

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = False
    state_module.save_state_atomic(state)

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("auto_trade_on=False인데 update_hynix_auto_trade_loop가 브로커/KIS에 진입함")

    monkeypatch.setattr(engine_module, "update_hynix_auto_trade_loop", _fail_if_called)

    thread = scheduler_module.HynixAutoTradeCycleThread(interval_seconds=999)
    # _run_cycle_if_enabled는 auto_trade_on을 확인한 뒤에만 update_hynix_auto_trade_loop를
    # 호출해야 한다 — off 상태에서는 절대 호출되지 않아야(위 모킹이 터지지 않아야) 한다.
    thread._run_cycle_if_enabled()


# ---------------------------------------------------------------------------
# 3) 백그라운드 스레드 부트스트랩 멱등성 — 중복 생성 없음
# ---------------------------------------------------------------------------

def test_ensure_watcher_running_is_idempotent():
    import app.trading.dynamic_exit_watcher as watcher

    try:
        first = watcher.ensure_watcher_running(interval_seconds=999)
        second = watcher.ensure_watcher_running(interval_seconds=999)
        assert first is second, "이미 살아있는 감시 스레드를 중복 생성함"
    finally:
        watcher.stop_watcher()


def test_ensure_cycle_thread_running_is_idempotent():
    import app.services.hynix_auto_trade_scheduler as scheduler_module

    try:
        first = scheduler_module.ensure_cycle_thread_running(interval_seconds=999)
        second = scheduler_module.ensure_cycle_thread_running(interval_seconds=999)
        assert first is second, "이미 살아있는 사이클 스레드를 중복 생성함"
    finally:
        scheduler_module.stop_cycle_thread()


# ---------------------------------------------------------------------------
# 4) STARTUP_STEP 로그 — 예외 메시지(비밀값 포함 가능) 미노출, 타입만 기록
# ---------------------------------------------------------------------------

def test_startup_step_failed_log_excludes_exception_message(caplog):
    """streamlit_app.py가 실제로 사용하는 것과 동일한 app.utils.startup_log 모듈만
    단독 import해 검증한다 — app.ui.streamlit_app을 직접 import하면 Streamlit 스크립트
    전체(백그라운드 스레드 기동 포함)가 실행되어 테스트에서 실제 KIS 호출이 발생할
    위험이 있으므로 그렇게 하지 않는다."""
    from app.utils.startup_log import log_step_failed

    exc = RuntimeError("KIS_REAL_APP_SECRET=super-secret-value-should-not-leak")
    with caplog.at_level("ERROR"):
        log_step_failed("test_step", exc)

    logged_text = "\n".join(r.message for r in caplog.records)
    assert "super-secret-value-should-not-leak" not in logged_text
    assert "STARTUP_STEP_FAILED: test_step" in logged_text
    assert "RuntimeError" in logged_text
