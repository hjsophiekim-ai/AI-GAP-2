"""READ-ONLY/MOCK unit tests for MUMacdService.manual_entry()/manual_exit()
— the "현재시점 레드/블루 전량매수" and "현재 보유물량 전량청산" UI buttons
(2026-08-13). No network, no real broker/WS (see conftest._block_real_network)
-- MUMacdService is exercised directly with a FakeBroker, never via .start().
"""
from __future__ import annotations

import threading
from datetime import datetime

import pytest

from app.trading.mu_macd import config, ledger, service as mu_service, state_store, worker
from app.trading.mu_macd.config import KST
from app.trading.mu_macd.models import Direction
from app.trading.mu_macd.service import MUMacdService
from tests.macd2.fake_broker import FakeBroker


# Captured at import time, before the autouse fixture below ever patches it —
# lets test_other_strategy_active_checks_mu_macd_ownership restore the real
# function within its own test body.
_REAL_OTHER_STRATEGY_ACTIVE = mu_service.other_strategy_active


@pytest.fixture(autouse=True)
def _no_other_strategy_active(monkeypatch):
    """2026-08-15: MUMacdService.start() now refuses to start while MACD2
    (or Enhanced) is really active (see mu_service.other_strategy_active).
    Every test in this file wants "no other engine active" by default,
    the same way tests/macd2/test_service.py stubs its own
    other_strategy_active to (False, "") everywhere -- tests that
    specifically exercise the gate itself override this within their own
    body (monkeypatch's last-write-wins is fine here)."""
    monkeypatch.setattr(mu_service, "other_strategy_active", lambda: (False, ""))


def _alive_service(broker: FakeBroker, *, budget: float = 1_000_000.0) -> MUMacdService:
    svc = MUMacdService()
    svc._broker = broker
    # manual_entry/manual_exit only check is_alive() as a "service actually
    # started" guard -- never touch the real WS/worker threads in this test.
    svc.is_alive = lambda: True

    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state_store.save_state(state)
    return svc


def test_manual_entry_buys_leverage_etf_within_budget_when_flat():
    broker = FakeBroker(cash=1_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    svc = _alive_service(broker)

    result = svc.manual_entry(Direction.UP_RED.value)

    assert result["ok"] is True
    assert result["symbol"] == config.LONG_SYMBOL
    assert result["quantity"] > 0

    state = state_store.load_state()
    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL

    signal_rows = ledger.load_signal_ledger()
    assert len(signal_rows) == 1
    assert signal_rows[0]["signal_type"] == "MANUAL_ENTRY"
    assert signal_rows[0]["direction"] == "UP_RED"
    assert signal_rows[0]["order_result"] == "EXECUTED"

    exec_rows = ledger.load_execution_ledger()
    assert len(exec_rows) == 1
    assert exec_rows[0]["symbol"] == config.LONG_SYMBOL
    assert exec_rows[0]["side"] == "BUY"


def test_manual_entry_buys_inverse_etf_for_blue_direction():
    broker = FakeBroker(cash=1_000_000.0, quotes={config.INVERSE_SYMBOL: 8_000.0})
    svc = _alive_service(broker)

    result = svc.manual_entry(Direction.DOWN_BLUE.value)

    assert result["ok"] is True
    assert result["symbol"] == config.INVERSE_SYMBOL
    state = state_store.load_state()
    assert state.position.symbol == config.INVERSE_SYMBOL


def test_manual_entry_rejected_when_already_holding_position():
    broker = FakeBroker(cash=1_000_000.0, quotes={
        config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 8_000.0,
    })
    svc = _alive_service(broker)

    first = svc.manual_entry(Direction.UP_RED.value)
    assert first["ok"] is True

    second = svc.manual_entry(Direction.DOWN_BLUE.value)
    assert second["ok"] is False
    assert second["message"] == "ALREADY_HOLDING_POSITION"

    # no second BUY was placed -- still only the first position held
    state = state_store.load_state()
    assert state.position.symbol == config.LONG_SYMBOL


def test_manual_exit_sells_held_position_and_records_ledger():
    broker = FakeBroker(cash=1_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    svc = _alive_service(broker)
    svc.manual_entry(Direction.UP_RED.value)
    assert state_store.load_state().position is not None

    result = svc.manual_exit()

    assert result["ok"] is True
    assert result["symbol"] == config.LONG_SYMBOL

    state = state_store.load_state()
    assert state.position is None

    signal_rows = ledger.load_signal_ledger()
    assert any(r["signal_type"] == "MANUAL_LIQUIDATION" for r in signal_rows)

    exec_rows = ledger.load_execution_ledger()
    sell_rows = [r for r in exec_rows if r["side"] == "SELL"]
    assert len(sell_rows) == 1
    assert sell_rows[0]["exit_reason"] == config.EXIT_MANUAL_LIQUIDATION


def test_manual_exit_rejected_when_flat():
    broker = FakeBroker(cash=1_000_000.0)
    svc = _alive_service(broker)

    result = svc.manual_exit()

    assert result["ok"] is False
    assert result["message"] == "NO_POSITION_TO_SELL"


def test_manually_entered_position_is_still_stop_lossed_by_normal_worker_tick():
    """2026-08-13 explicit requirement: a manually-bought position must keep
    being watched by the normal stop-loss/quick-profit machinery every
    worker tick, exactly like an auto-entered one -- worker.run_once()'s
    held-position risk checks operate purely on state.position and must not
    care how it was populated."""
    broker = FakeBroker(cash=1_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    svc = _alive_service(broker)
    entry = svc.manual_entry(Direction.UP_RED.value)
    assert entry["ok"] is True

    state = state_store.load_state()
    assert state.position is not None

    # price breaches STOP_LOSS_NET_PCT
    breached_price = 15_000.0 * (1 + (config.STOP_LOSS_NET_PCT - 0.5) / 100.0)
    broker.set_quote(config.LONG_SYMBOL, breached_price)

    now = datetime.now(KST)
    worker.run_once(broker=broker, market_data=_FlatMarketData(), state=state, now=now)

    assert state.position is None  # stopped out by the ordinary per-tick risk check


class _FlatMarketData:
    """Minimal market_data stand-in -- this test only exercises the
    held-position stop-loss branch of run_once(), which never touches
    market_data beyond the ws_* attributes it copies into state."""

    ws_connected = True
    ws_last_error = None
    ws_last_tick_at = None
    last_price = None
    last_tvol = None

    def get_history_df(self):
        import pandas as pd

        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    def warmup_bars_1m_count(self) -> int:
        return 0


# ── set_entry_paused (2026-08-14) ────────────────────────────────────────────

def test_set_entry_paused_toggles_and_persists():
    state_store.save_state(state_store.default_state())  # entry_paused=False
    svc = MUMacdService()

    res_on = svc.set_entry_paused(True)
    assert res_on == {"ok": True, "entry_paused": True, "previous": False}
    assert state_store.load_state().entry_paused is True

    res_off = svc.set_entry_paused(False)
    assert res_off == {"ok": True, "entry_paused": False, "previous": True}
    assert state_store.load_state().entry_paused is False


def test_entry_paused_toggle_survives_concurrent_stale_worker_tick_save():
    """Same lost-update race set_quick_profit_enabled was fixed for
    (2026-08-13) -- set_entry_paused shares the exact same _LOCK-protected
    load-modify-save pattern, so reproduce the identical interleaving here
    too: a worker tick that loaded state BEFORE the toggle must not be
    allowed to silently clobber it back to the pre-toggle value once the
    tick's OWN (stale) save finally goes through."""
    state_store.save_state(state_store.default_state())  # entry_paused=False

    tick_started = threading.Event()
    proceed_with_stale_save = threading.Event()

    def fake_stale_tick():
        with mu_service._LOCK:
            stale_state = state_store.load_state()  # reads False, BEFORE the toggle below
            tick_started.set()
            proceed_with_stale_save.wait(timeout=2)
            state_store.save_state(stale_state)  # the lost-update: re-saves the stale False

    tick_thread = threading.Thread(target=fake_stale_tick)
    tick_thread.start()
    assert tick_started.wait(timeout=2)

    svc = MUMacdService()
    result_holder: dict = {}

    def do_toggle():
        result_holder["result"] = svc.set_entry_paused(True)

    toggle_thread = threading.Thread(target=do_toggle)
    toggle_thread.start()
    toggle_thread.join(timeout=0.2)
    assert toggle_thread.is_alive()  # blocked on _LOCK, held by the fake stale tick

    proceed_with_stale_save.set()
    tick_thread.join(timeout=2)
    toggle_thread.join(timeout=2)

    assert result_holder["result"]["ok"] is True
    assert state_store.load_state().entry_paused is True  # NOT clobbered


# ── set_quick_profit_enabled vs _run_loop lost-update race (2026-08-13 fix) ──

def test_quick_profit_toggle_survives_concurrent_stale_worker_tick_save():
    """Real incident: user turns Quick Profit ON, then a later page refresh
    shows it OFF again. Root cause: _run_loop's own load-run_once-save cycle
    (every WORKER_INTERVAL_SEC, on a separate thread) had no locking against
    set_quick_profit_enabled's load-modify-save -- a tick that loaded state
    BEFORE the toggle, then saved AFTER it, silently clobbered the toggle
    back to whatever it saw at its own load time. This reproduces that
    exact interleaving with a real background thread holding _LOCK, and
    asserts the toggle is no longer lost."""
    state_store.save_state(state_store.default_state())  # quick_profit_enabled=False

    tick_started = threading.Event()
    proceed_with_stale_save = threading.Event()

    def fake_stale_tick():
        with mu_service._LOCK:
            stale_state = state_store.load_state()  # reads False, BEFORE the toggle below
            tick_started.set()
            proceed_with_stale_save.wait(timeout=2)
            state_store.save_state(stale_state)  # the lost-update: re-saves the stale False

    tick_thread = threading.Thread(target=fake_stale_tick)
    tick_thread.start()
    assert tick_started.wait(timeout=2)

    svc = MUMacdService()
    result_holder: dict = {}

    def do_toggle():
        result_holder["result"] = svc.set_quick_profit_enabled(True)

    toggle_thread = threading.Thread(target=do_toggle)
    toggle_thread.start()
    toggle_thread.join(timeout=0.2)
    assert toggle_thread.is_alive()  # blocked on _LOCK, held by the fake stale tick

    proceed_with_stale_save.set()
    tick_thread.join(timeout=2)
    toggle_thread.join(timeout=2)

    assert result_holder["result"]["ok"] is True
    assert state_store.load_state().quick_profit_enabled is True  # NOT clobbered


# ── auto-recovery after a process restart (2026-08-13 fix) — mirrors ────────
# macd2's own auto-recovery for the exact same incident class: a real held
# position rode a loss past STOP_LOSS_NET_PCT and a confirmed flag with
# neither ever acting, because the process had restarted (Render idle-sleep
# or a redeploy) and nobody had clicked "시작" again -- run_once() simply
# never executed. status() must now recover on its own instead of silently
# doing nothing until a human notices.

def test_start_restarts_cleanly_when_already_alive():
    """2026-08-14 fix (real incident: after every commit/redeploy the user
    has to click "자동매매 시작" again, but a still-alive worker -- e.g.
    MOCK's own auto-recovery already having kicked in from an earlier
    status() poll -- used to make start() refuse with ALREADY_RUNNING, with
    no way to force a clean restart from the UI). start() must now tear
    down the existing worker and spin up a genuinely fresh one instead."""
    svc = MUMacdService()
    first = svc.start(mode="mock", budget=1_000_000.0)
    assert first["ok"] is True
    assert svc.is_alive()
    first_instance_id = state_store.load_state().worker_instance_id

    second = svc.start(mode="mock", budget=2_000_000.0)

    assert second["ok"] is True
    assert svc.is_alive()
    state = state_store.load_state()
    assert state.worker_instance_id != first_instance_id  # genuinely fresh, not a no-op
    assert state.budget == 2_000_000.0
    svc.stop()  # cleanup


def test_status_auto_recovers_worker_when_auto_trade_on_but_dead():
    state = state_store.default_state()
    state.mode = "mock"
    state.budget = 1_000_000.0
    state.auto_trade_on = True
    state_store.save_state(state)

    svc = MUMacdService()
    assert not svc.is_alive()

    status = svc.status()

    assert status["worker_alive"] is True
    assert svc.is_alive()
    svc.stop()  # cleanup: stop the real threads this recovery spun up


# ── broker-less "flags only" shadow for REAL mode (2026-08-14) — REAL's
# KisRealBroker refuses to even construct without the human re-entering the
# confirm phrase, so unlike MOCK, _auto_recover_worker can never bring the
# real worker back on its own. MU price collection/MACD flag detection don't
# need that broker at all, though, so status() must still keep them alive.

def test_status_starts_flags_only_shadow_when_real_mode_worker_dead():
    state = state_store.default_state()
    state.mode = "real"
    state.auto_trade_on = True
    state_store.save_state(state)

    svc = MUMacdService()
    assert not svc.is_alive()

    status = svc.status()

    assert status["worker_alive"] is False  # REAL never auto-recovers the broker itself
    assert status["flags_only_active"] is True
    assert svc._flags_only_alive()
    svc.stop()  # cleanup: joins the shadow thread before the test ends


def test_start_real_mode_broker_failure_returns_clean_error_without_leaving_auto_trade_on(monkeypatch):
    """2026-08-14 REAL-mode readiness check: start() persists
    auto_trade_on=True BEFORE constructing the broker, with no try/except
    around create_macd2_broker() -- a wrong confirm phrase (or any other
    KisRealBroker safety-gate RuntimeError) would otherwise propagate
    uncaught out of start() AND leave auto_trade_on=True/worker_started_at
    stamped despite no worker ever actually starting. Mirrors macd2's own
    service.start(), which already wraps this exact call in try/except."""
    def _boom(mode, **kwargs):
        raise RuntimeError("실전투자 확인 문구가 틀립니다. 'LIVE'를 정확히 입력하세요.")

    monkeypatch.setattr(mu_service, "create_macd2_broker", _boom)

    svc = MUMacdService()
    result = svc.start(mode="real", budget=1_000_000.0, confirm_text="wrong")

    assert result["ok"] is False
    assert "확인 문구가 틀립니다" in result["message"]
    state = state_store.load_state()
    assert state.auto_trade_on is False
    assert not svc.is_alive()


def test_starting_real_worker_stops_flags_only_shadow():
    """Once the human re-authenticates and the real worker actually starts,
    the broker-less shadow must be torn down -- no duplicate MU WS
    subscription running alongside the real one."""
    state = state_store.default_state()
    state.mode = "real"
    state.auto_trade_on = True
    state_store.save_state(state)

    svc = MUMacdService()
    svc.status()  # brings up the flags-only shadow
    assert svc._flags_only_alive()

    result = svc.start(mode="mock", budget=1_000_000.0)  # simulates re-auth

    assert result["ok"] is True
    assert svc.is_alive()
    assert not svc._flags_only_alive()
    svc.stop()


def test_auto_recover_worker_respects_cooldown():
    state = state_store.default_state()
    state.mode = "mock"
    state.auto_trade_on = True
    state.last_auto_recover_attempt_at = datetime.now(KST).isoformat()  # just attempted
    state_store.save_state(state)

    svc = MUMacdService()
    recovered = svc._auto_recover_worker(state_store.load_state())

    assert recovered is False  # cooldown still active -- no new attempt
    assert not svc.is_alive()


def test_auto_recover_worker_never_triggers_for_real_mode():
    state = state_store.default_state()
    state.mode = "real"
    state.auto_trade_on = True
    state_store.save_state(state)

    svc = MUMacdService()
    recovered = svc._auto_recover_worker(state_store.load_state())

    assert recovered is False
    assert not svc.is_alive()


# ── Cross-strategy ownership gate (2026-08-15) — MACD2 and MU_MACD trade the
# identical two ETFs (0193T0/0197X0) in the same KIS account; start() must
# refuse whenever the OTHER engine is really active, mirroring macd2.service
# .start()'s own existing gate (see app.trading.strategy_ownership).

def test_start_refuses_when_other_strategy_is_active(monkeypatch):
    monkeypatch.setattr(mu_service, "other_strategy_active", lambda: (True, "MACD2_ACTIVE"))

    svc = MUMacdService()
    result = svc.start(mode="mock", budget=1_000_000.0)

    assert result["ok"] is False
    assert result["message"] == "MACD2_ACTIVE"
    assert not svc.is_alive()
    state = state_store.load_state()
    assert state.auto_trade_on is False
    assert state.order_block_reason == "MACD2_ACTIVE"


def test_start_proceeds_when_no_other_strategy_active(monkeypatch):
    monkeypatch.setattr(mu_service, "other_strategy_active", lambda: (False, ""))

    svc = MUMacdService()
    result = svc.start(mode="mock", budget=1_000_000.0)

    assert result["ok"] is True
    assert svc.is_alive()
    svc.stop()  # cleanup


def test_other_strategy_active_checks_mu_macd_ownership(monkeypatch):
    """other_strategy_active() itself must be wired to strategy_ownership's
    MU_MACD claimant, not some hand-rolled duplicate check. Restores the
    REAL function first -- the autouse _no_other_strategy_active fixture
    above stubs mu_service.other_strategy_active for every other test in
    this file, which would otherwise make this specific wiring check
    vacuous (it would just call its own stub)."""
    from app.trading import strategy_ownership

    monkeypatch.setattr(mu_service, "other_strategy_active", _REAL_OTHER_STRATEGY_ACTIVE)
    calls = []
    monkeypatch.setattr(
        strategy_ownership, "other_owner_active",
        lambda claimant: (calls.append(claimant) or (False, "")),
    )
    mu_service.other_strategy_active()
    assert calls == [strategy_ownership.MU_MACD]
