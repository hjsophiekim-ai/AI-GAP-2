"""MACD2 headless supervisor (2026-11-16) — UI 세션 없이 worker 감시/복구.

2026-11-13 09:19 장중 재배포 후 10:17 에 누군가 UI 를 열 때까지 worker 가 안 떴던
사고의 재발 방지. 시나리오:

  A. UI(get_snapshot) 를 한 번도 부르지 않아도 감시 스레드가 worker 를 띄운다
  B. worker 강제 종료/정체 -> 감지·복구, 3분 이상 비정상이면 경보
  C. REAL + 보유 포지션 -> 신규진입 OFF 상태로 먼저 재기동, 안전할 때만 진입 복원, 주문 0건
  D. 재배포(새 Macd2Service) -> 저장된 runtime/position 을 그대로 읽고 이어감
  E. 감시 스레드가 오래(수백 tick, tick 예외 포함) 살아 있음

worker/시세/브로커는 전부 가짜다 -- 실제 KIS·네트워크 호출 없음(conftest 가 막는다).
"""
from __future__ import annotations

import csv
import itertools
import json
import os
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.trading.macd2 import config, entry_arming, service, state_store, supervisor
from app.trading.macd2.models import PositionSnapshot

KST = config.KST
_ids = itertools.count(1)


# ── 가짜 부품 ──────────────────────────────────────────────────────────────

class FakeWorker:
    instances: list["FakeWorker"] = []
    stop_confirms = True          # False 면 stop() 이 정지 확인 실패(옛 스레드가 안 죽음)

    def __init__(self, *, broker, market_data, get_state, save_state, **_kw):
        self.instance_id = f"fake{next(_ids):04d}"
        self.broker = broker
        self._stop = threading.Event()
        self._frozen = threading.Event()
        self._thread = None
        self._started_at = None
        self._last_tick_at = None
        FakeWorker.instances.append(self)

    def start(self):
        self._started_at = datetime.now(KST)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            if not self._frozen.is_set():
                self._last_tick_at = datetime.now(KST)
            time.sleep(0.01)

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def stop(self, join_timeout=5.0):
        if not FakeWorker.stop_confirms:
            return False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        return True

    def kill(self):        # 스레드가 예외로 죽은 것과 같은 상태
        self._stop.set()
        self._thread.join(timeout=2)

    def freeze(self):      # 살아 있지만 tick 이 멈춤
        self._frozen.set()

    def tick_stats(self):
        return {"instance_id": self.instance_id,
                "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "tick_n": 1}


class FakeMarketData:
    boot_ok = True

    def __init__(self, mode="mock"):
        self.mode = mode

    def refresh_quotes(self):
        pass

    def start_quote_updater(self, interval_sec=1.0):
        pass

    def start_history_updater(self, interval_sec=5.0):
        pass

    def stop_quote_updater(self, join_timeout=2.0):
        return True

    def stop_history_updater(self, join_timeout=2.0):
        return True

    def quote_updater_alive(self):
        return True

    def history_updater_alive(self):
        return True

    def history_stale_age_sec(self, now=None):
        return 0.0

    def recover_history_updater(self, now=None):
        return "NOOP"

    def bootstrap(self, now=None):
        return SimpleNamespace(ok=FakeMarketData.boot_ok, reason="" if FakeMarketData.boot_ok else "FAKE_BOOT_FAIL")


class FakeBroker:
    created: list["FakeBroker"] = []
    positions: list = []
    positions_error: Exception | None = None

    def __init__(self, mode, **kwargs):
        self.mode = mode
        self.kwargs = kwargs
        self.orders: list = []
        FakeBroker.created.append(self)

    def get_positions(self):
        if FakeBroker.positions_error is not None:
            raise FakeBroker.positions_error
        return [SimpleNamespace(symbol=s, quantity=q) for s, q in FakeBroker.positions]

    def buy(self, *a, **k):            # 어떤 경로든 주문이 나가면 기록된다
        self.orders.append(("BUY", a, k))

    def sell(self, *a, **k):
        self.orders.append(("SELL", a, k))


@pytest.fixture
def env(monkeypatch):
    FakeWorker.instances = []
    FakeWorker.stop_confirms = True
    FakeMarketData.boot_ok = True
    FakeBroker.created = []
    FakeBroker.positions = []
    FakeBroker.positions_error = None
    catchup = []

    def _fake_run_once(*, broker, market_data, state, now):
        # 재기동 직후 catch-up tick 시점의 신규진입 허용 상태를 기록한다.
        catchup.append({"entry_enabled": bool(getattr(state, "entry_enabled", False)),
                        "mode": broker.mode, "position": state.position})

    monkeypatch.setattr(service, "Macd2Worker", FakeWorker)
    monkeypatch.setattr(service, "MarketDataService", FakeMarketData)
    monkeypatch.setattr(service, "create_macd2_broker", lambda mode, **kw: FakeBroker(mode, **kw))
    monkeypatch.setattr(service, "initialize_strategy_session", lambda state, md, now, worker_instance_id: state)
    monkeypatch.setattr(service, "run_once", _fake_run_once)
    monkeypatch.setattr(service, "other_strategy_active", lambda: (False, ""))
    monkeypatch.setattr(config, "WORKER_AUTO_RECOVER_COOLDOWN_SEC", 0.0)
    import app.config as app_config
    monkeypatch.setattr(app_config, "get_config", lambda: SimpleNamespace(real_confirm_text=lambda: "LIVE-TEST"))
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv(config.SUPERVISOR_ENABLED_ENV, raising=False)
    yield SimpleNamespace(catchup=catchup)
    inst = supervisor.current()
    if inst is not None:
        inst.stop()
    monkeypatch.setattr(supervisor, "_instance", None)
    for w in FakeWorker.instances:
        w._stop.set()


def _save(**fields):
    st = state_store.load_state()
    for k, v in fields.items():
        setattr(st, k, v)
    state_store.save_state(st)
    return st


def _ledger_rows():
    p = supervisor.ledger_path()
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _no_snapshot(monkeypatch, svc):
    def _boom(*a, **k):
        raise AssertionError("UI 경로(get_snapshot)가 불렸다 -- headless 감시가 UI 에 의존하면 안 된다")
    monkeypatch.setattr(svc, "get_snapshot", _boom)


# ── A. UI 없이 부팅 직후 감시 ───────────────────────────────────────────────

def test_A_supervisor_starts_worker_without_any_ui_call(env, monkeypatch):
    svc = service.Macd2Service()
    _save(auto_trade_on=True, mode="mock", budget=1_000_000.0)
    _no_snapshot(monkeypatch, svc)
    assert svc._worker is None, "부팅 직후에는 worker 가 없다(재배포 직후 상태)"

    sup = supervisor.ensure_running("process_start", force=True, interval_sec=0.05, boot_delay_sec=0.0,
                                    service_getter=lambda: svc)
    assert _wait(lambda: svc._worker is not None and svc._worker.is_alive()), "UI 없이 worker 가 안 떴다"
    assert _wait(lambda: supervisor.heartbeat_path().exists())
    hb = json.loads(supervisor.heartbeat_path().read_text(encoding="utf-8"))
    assert hb["pid"] == os.getpid() and hb["tick_n"] >= 1 and hb["boot_reason"] == "process_start"
    events = [r["event"] for r in _ledger_rows()]
    assert supervisor.EV_BOOT in events and supervisor.EV_RECOVER_RESULT in events
    assert sup.is_alive()
    # 두 번째 ensure_running 은 같은 스레드(프로세스당 1개)
    assert supervisor.ensure_running("ui_session", force=True) is sup


def test_A_supervisor_only_monitors_when_auto_trade_off(env, monkeypatch):
    svc = service.Macd2Service()
    _save(auto_trade_on=False, mode="mock")
    sup = supervisor.SupervisorThread(boot_reason="t", service_getter=lambda: svc)
    out = sup.tick_once()
    assert out["verdict"] == supervisor.VERDICT_IDLE
    assert svc._worker is None and FakeBroker.created == [], "자동매매 OFF 면 아무것도 켜지 않는다"


def test_A_enabled_by_default_only_on_render(monkeypatch):
    monkeypatch.delenv(config.SUPERVISOR_ENABLED_ENV, raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    assert supervisor.supervisor_enabled() is False, "로컬은 기본 OFF (실수로 KIS 토큰 발급 방지)"
    monkeypatch.setenv("RENDER", "true")
    assert supervisor.supervisor_enabled() is True
    monkeypatch.setenv(config.SUPERVISOR_ENABLED_ENV, "0")
    assert supervisor.supervisor_enabled() is False


def test_A_web_main_starts_supervisor_before_streamlit(env, monkeypatch):
    from app import web_main
    calls = []
    monkeypatch.setattr(supervisor, "ensure_running", lambda boot_reason, **k: calls.append(boot_reason) or object())
    import streamlit.web.cli as stcli
    seen = {}

    def _fake_main():
        seen["argv"] = list(__import__("sys").argv)
        seen["supervisor_started_first"] = list(calls)
        return 0

    monkeypatch.setattr(stcli, "main", _fake_main)
    monkeypatch.setattr("sys.argv", ["x"])
    with pytest.raises(SystemExit):
        web_main.main(["--server.port", "1234"])
    assert seen["supervisor_started_first"] == ["process_start"]
    assert seen["argv"][1] == "run" and seen["argv"][2].endswith("streamlit_app.py")
    assert seen["argv"][3:] == ["--server.port", "1234"]


# ── B. 강제 종료 / 정체 ─────────────────────────────────────────────────────

def test_B_killed_worker_is_detected_and_recovered(env, monkeypatch):
    svc = service.Macd2Service()
    _save(auto_trade_on=True, mode="mock")
    _no_snapshot(monkeypatch, svc)
    sup = supervisor.SupervisorThread(boot_reason="t", service_getter=lambda: svc)
    sup.tick_once()
    first = svc._worker
    assert first.is_alive()
    first.kill()
    assert not first.is_alive()
    out = sup.tick_once()
    assert out["verdict"] == supervisor.VERDICT_DEAD and out["recover_result"] is True
    assert svc._worker is not first and svc._worker.is_alive()


def test_B_stalled_alive_worker_is_restarted(env, monkeypatch):
    svc = service.Macd2Service()
    _save(auto_trade_on=True, mode="mock")
    sup = supervisor.SupervisorThread(boot_reason="t", service_getter=lambda: svc)
    sup.tick_once()
    first = svc._worker
    first.freeze()
    later = datetime.now(KST) + timedelta(seconds=config.SUPERVISOR_WORKER_STALE_SEC + 5)
    out = sup.tick_once(now=later)
    assert out["verdict"] == supervisor.VERDICT_STALLED and out["recover_result"] is True
    assert not first.is_alive(), "정체된 옛 worker 는 정지돼야 한다"
    assert svc._worker is not first and svc._worker.is_alive()
    alive = [w for w in FakeWorker.instances if w.is_alive()]
    assert len(alive) == 1, "worker 가 둘 이상 살아 있으면 안 된다"


def test_B_alert_after_three_minutes_then_recovered_record(env):
    svc = service.Macd2Service()
    _save(auto_trade_on=True, mode="mock")
    FakeMarketData.boot_ok = False                  # 복구가 계속 실패하는 상황
    sup = supervisor.SupervisorThread(boot_reason="t", service_getter=lambda: svc)
    t0 = datetime.now(KST)
    sup.tick_once(now=t0)
    sup.tick_once(now=t0 + timedelta(seconds=60))
    assert not [r for r in _ledger_rows() if r["event"] == supervisor.EV_ABNORMAL], "3분 전에는 경보 없음"
    sup.tick_once(now=t0 + timedelta(seconds=config.SUPERVISOR_ALERT_AFTER_SEC + 1))
    alerts = [r for r in _ledger_rows() if r["event"] == supervisor.EV_ABNORMAL]
    assert len(alerts) == 1 and alerts[0]["level"] == "ALERT"
    assert json.loads(alerts[0]["detail"])["duration_sec"] >= config.SUPERVISOR_ALERT_AFTER_SEC
    sup.tick_once(now=t0 + timedelta(seconds=config.SUPERVISOR_ALERT_AFTER_SEC + 20))
    assert len([r for r in _ledger_rows() if r["event"] == supervisor.EV_ABNORMAL]) == 1, "반복 간격 전 재경보 금지"
    FakeMarketData.boot_ok = True
    sup.tick_once(now=t0 + timedelta(seconds=300))   # 이 tick 에서 복구(판정은 복구 전 관측 DEAD)
    sup.tick_once()                                   # 실시간 기준 다음 tick -> OK -> RECOVERED 기록
    rec = [r for r in _ledger_rows() if r["event"] == supervisor.EV_RECOVERED]
    assert len(rec) == 1 and svc._worker.is_alive()


def test_B_tick_exception_never_kills_supervisor(env):
    calls = {"n": 0}

    class _Boom:
        def supervisor_tick(self, now=None):
            calls["n"] += 1
            raise RuntimeError("boom")

    sup = supervisor.SupervisorThread(boot_reason="t", interval_sec=0.01, boot_delay_sec=0.0,
                                      service_getter=lambda: _Boom())
    sup.start()
    assert _wait(lambda: calls["n"] >= 5)
    assert sup.is_alive()
    sup.stop()
    assert any(r["event"] == supervisor.EV_TICK_ERROR for r in _ledger_rows())


# ── C. REAL 안전 복구 ───────────────────────────────────────────────────────

def _real_position_state(now, *, armed_today=True, **extra):
    armed = now - timedelta(hours=1) if armed_today else now - timedelta(days=1)
    return _save(auto_trade_on=True, mode="real", budget=9_000_000.0,
                 position=PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=1000, avg_price=5000.0,
                                           entry_at=now - timedelta(minutes=20)),
                 entry_enabled=True, entry_armed_at=armed.isoformat(), entry_changed_by="ui", **extra)


def _real_tick(svc, now=None):
    return supervisor.SupervisorThread(boot_reason="t", service_getter=lambda: svc).tick_once(now=now)


@pytest.mark.entry_arming
def test_C_real_with_position_recovers_safely_and_restores_entry(env, monkeypatch):
    svc = service.Macd2Service()
    now = datetime.now(KST)
    supervisor.record_real_confirmation(runtime_real_mode=True, runtime_enable_real_buy=True,
                                        runtime_enable_real_sell=True, now=now - timedelta(hours=2))
    _real_position_state(now)
    FakeBroker.positions = [(config.LONG_SYMBOL, 1000)]
    _no_snapshot(monkeypatch, svc)

    out = _real_tick(svc)
    assert out["verdict"] == supervisor.VERDICT_DEAD and out["recover_result"] is True
    broker = FakeBroker.created[-1]
    assert broker.mode == "real"
    assert broker.kwargs == {"confirm_text": "LIVE-TEST", "runtime_real_mode": True,
                             "runtime_enable_real_buy": True, "runtime_enable_real_sell": True}
    assert env.catchup and all(c["entry_enabled"] is False for c in env.catchup), \
        "재기동 직후 catch-up tick 은 신규진입 OFF 상태여야 한다"
    st = state_store.load_state()
    assert st.position.symbol == config.LONG_SYMBOL and st.position.quantity == 1000, "보유 포지션 그대로"
    assert entry_arming.is_enabled(st) and st.entry_changed_by == "supervisor:real_recover_safe"
    assert datetime.fromisoformat(st.entry_armed_at) >= now, "armed_at=지금 -> 공백 중 신호 소급 진입 없음"
    assert all(b.orders == [] for b in FakeBroker.created), "복구 과정에서 주문 0건"
    assert supervisor.EV_REAL_ENTRY_RESTORED in [r["event"] for r in _ledger_rows()]


@pytest.mark.entry_arming
@pytest.mark.parametrize("case", ["pending_order", "position_mismatch", "broker_query_fail",
                                  "recent_order", "armed_yesterday"])
def test_C_real_unsafe_cases_recover_monitoring_only(env, case):
    svc = service.Macd2Service()
    now = datetime.now(KST)
    supervisor.record_real_confirmation(runtime_real_mode=True, runtime_enable_real_buy=True,
                                        runtime_enable_real_sell=True, now=now - timedelta(hours=2))
    extra = {}
    FakeBroker.positions = [(config.LONG_SYMBOL, 1000)]
    if case == "pending_order":
        extra["pending_buy_order"] = {"symbol": config.LONG_SYMBOL, "order_id": "X1"}
    elif case == "position_mismatch":
        FakeBroker.positions = [(config.LONG_SYMBOL, 1000), (config.INVERSE_SYMBOL, 5)]
    elif case == "broker_query_fail":
        FakeBroker.positions_error = RuntimeError("EGW00201")
    elif case == "recent_order":
        extra["last_broker_order_at"] = (now - timedelta(seconds=30)).isoformat()
    _real_position_state(now, armed_today=(case != "armed_yesterday"), **extra)

    out = _real_tick(svc)
    assert out["worker_alive"] is True, "감시(worker)는 복구된다"
    st = state_store.load_state()
    assert not entry_arming.is_enabled(st), f"{case}: 신규진입은 OFF 유지"
    assert st.position.quantity == 1000
    rows = [r for r in _ledger_rows() if r["event"] == supervisor.EV_REAL_MONITOR_ONLY]
    assert len(rows) == 1 and rows[0]["level"] == "ALERT", "켜져 있던 진입을 못 살렸으면 경보"
    assert all(b.orders == [] for b in FakeBroker.created)


@pytest.mark.entry_arming
def test_C_real_without_human_start_record_is_never_started(env):
    svc = service.Macd2Service()
    now = datetime.now(KST)
    _real_position_state(now)
    out = _real_tick(svc)
    assert out["worker_alive"] is False and FakeBroker.created == [], "확인 기록 없으면 REAL 은 시작하지 않는다"
    rows = [r for r in _ledger_rows() if r["event"] == supervisor.EV_REAL_BLOCKED]
    assert rows and rows[0]["level"] == "ALERT" and "NO_HUMAN_REAL_START_RECORD" in rows[0]["detail"]


@pytest.mark.entry_arming
def test_C_real_record_expires_and_kill_switch(env, monkeypatch):
    svc = service.Macd2Service()
    now = datetime.now(KST)
    supervisor.record_real_confirmation(runtime_real_mode=True, runtime_enable_real_buy=True,
                                        runtime_enable_real_sell=True,
                                        now=now - timedelta(days=config.REAL_RECOVER_CONFIRM_MAX_AGE_DAYS + 1))
    _real_position_state(now)
    assert _real_tick(svc)["worker_alive"] is False
    supervisor.record_real_confirmation(runtime_real_mode=True, runtime_enable_real_buy=True,
                                        runtime_enable_real_sell=True, now=now)
    monkeypatch.setattr(config, "REAL_AUTO_RECOVER_ENABLED", False)
    assert _real_tick(svc)["worker_alive"] is False
    assert FakeBroker.created == []


@pytest.mark.entry_arming
def test_C_real_never_stacks_a_second_worker_when_old_one_wont_stop(env):
    svc = service.Macd2Service()
    now = datetime.now(KST)
    supervisor.record_real_confirmation(runtime_real_mode=True, runtime_enable_real_buy=True,
                                        runtime_enable_real_sell=True, now=now)
    _real_position_state(now)
    FakeBroker.positions = [(config.LONG_SYMBOL, 1000)]
    _real_tick(svc)
    old = svc._worker
    old.freeze()
    FakeWorker.stop_confirms = False                # 옛 스레드가 정지 확인에 실패
    later = datetime.now(KST) + timedelta(seconds=config.SUPERVISOR_WORKER_STALE_SEC + 5)
    for k in range(3):
        _real_tick(svc, now=later + timedelta(seconds=k * (config.QUOTE_UPDATER_FORCE_REPLACE_AGE_SEC + 10)))
    assert svc._worker is old and len(FakeWorker.instances) == 1, "REAL 은 강제 통과 없이 절대 겹쳐 띄우지 않는다"
    assert not entry_arming.is_enabled(state_store.load_state()), "정체 중 신규진입은 OFF"


@pytest.mark.entry_arming
def test_C_human_start_records_and_stop_clears_real_confirmation(env):
    svc = service.Macd2Service()
    res = svc.start(mode="real", budget=1.0, real_kwargs={"confirm_text": "LIVE-TEST", "runtime_real_mode": True,
                                                          "runtime_enable_real_buy": True,
                                                          "runtime_enable_real_sell": False})
    assert res["ok"]
    conf = supervisor.load_real_confirmation()
    assert conf and conf["runtime_enable_real_sell"] is False and "confirm_text" not in conf
    svc.stop()
    assert supervisor.load_real_confirmation() is None
    # 사람이 중지한 뒤에는 auto_trade_on=False 라 감시도 다시 켜지 않는다
    assert _real_tick(svc)["verdict"] == supervisor.VERDICT_IDLE


# ── D. 재배포/재시작 ────────────────────────────────────────────────────────

def test_D_redeploy_resumes_from_persisted_state_without_orders(env, monkeypatch):
    now = datetime.now(KST)
    old = service.Macd2Service()
    old.start(mode="mock", budget=2_000_000.0)
    _save(position=PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=321, avg_price=8800.0,
                                    entry_at=now - timedelta(minutes=5)))
    old._worker.kill()                               # 재배포로 옛 프로세스 종료
    monkeypatch.setattr(service, "_service_instance", None)

    new = service.get_service()                      # 새 프로세스의 싱글턴
    assert new is not old and new._worker is None
    _no_snapshot(monkeypatch, new)
    out = supervisor.SupervisorThread(boot_reason="process_start", service_getter=service.get_service).tick_once()
    assert out["recover_result"] is True and new._worker.is_alive()
    st = state_store.load_state()
    assert (st.mode, st.budget, st.auto_trade_on) == ("mock", 2_000_000.0, True)
    assert st.position.symbol == config.INVERSE_SYMBOL and st.position.quantity == 321
    assert env.catchup[-1]["position"].quantity == 321, "새 worker 의 첫 tick 이 저장된 포지션을 본다"
    assert all(b.orders == [] for b in FakeBroker.created)


@pytest.mark.entry_arming
def test_D_real_redeploy_with_position_is_safe(env, monkeypatch):
    now = datetime.now(KST)
    supervisor.record_real_confirmation(runtime_real_mode=True, runtime_enable_real_buy=True,
                                        runtime_enable_real_sell=True, now=now - timedelta(hours=1))
    _real_position_state(now)
    FakeBroker.positions = [(config.LONG_SYMBOL, 1000)]
    new = service.get_service()
    out = supervisor.SupervisorThread(boot_reason="process_start", service_getter=service.get_service).tick_once()
    assert out["worker_alive"] and entry_arming.is_enabled(state_store.load_state())
    assert all(c["entry_enabled"] is False for c in env.catchup)
    assert all(b.orders == [] for b in FakeBroker.created)


# ── E. 장시간 무UI 감시 ─────────────────────────────────────────────────────

def test_E_supervisor_keeps_running_for_many_ticks_without_ui(env, monkeypatch):
    svc = service.Macd2Service()
    _save(auto_trade_on=True, mode="mock")
    _no_snapshot(monkeypatch, svc)
    sup = supervisor.ensure_running("process_start", force=True, interval_sec=0.005, boot_delay_sec=0.0,
                                    service_getter=lambda: svc)
    assert _wait(lambda: sup.tick_n >= 60, timeout=30), f"tick_n={sup.tick_n}"
    killed = svc._worker
    killed.kill()
    assert _wait(lambda: svc._worker is not killed and svc._worker.is_alive())
    n = sup.tick_n
    assert _wait(lambda: sup.tick_n >= n + 30, timeout=30)
    assert sup.is_alive() and sup.error_n == 0
    assert len([w for w in FakeWorker.instances if w.is_alive()]) == 1
