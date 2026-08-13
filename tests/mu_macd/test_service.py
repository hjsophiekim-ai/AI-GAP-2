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
