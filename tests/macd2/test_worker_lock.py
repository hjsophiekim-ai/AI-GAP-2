"""Regression tests for the 2026-08-26 cross-process duplicate-worker fix.

Real incident: a Render redeploy/restart left TWO live Macd2Worker-owning
processes running concurrently for several minutes against the SAME mock
KIS account, both dispatching the same 09:09 UP_RED TW confirmation and
snowballing into a 994+542-share position (see app/trading/macd2/
worker_lock.py's own docstring for the full writeup).

Covers, in order:
  1) worker_lock.py's lease primitives in isolation (acquire/renew/stale/
     takeover/release/is_current_owner) -- no threads, no broker.
  2) LockGuardedBroker -- the "주문 직전 재확인" pre-order re-check.
  3) ledger.signal_id_has_leg -- the persistent-disk signal_id/side dedup
     order_executor.execute_signal now also checks.
  4) Real Macd2Worker background threads: two processes starting
     concurrently, an already-healthy owner refusing a second starter, a
     dead owner's lease timing out and being taken over, and a rolling-
     deploy-style overlap that never produces two simultaneously ACTIVE
     owners.
  5) A full run_once()-level reproduction of the actual 2026-08-26 09:09
     UP_RED TW2 incident shape: two independent RuntimeState "processes"
     dispatching against ONE shared broker -- exactly one real BUY order
     must land, both with the lock gate wired in (the real fix) and with
     it deliberately bypassed (proving the persistent-ledger dedup alone
     is still a correct second line of defense).
  6) Three explicit pre-commit checks requested 2026-08-26: the lock file
     genuinely lives under the Persistent Disk state directory (not an
     ephemeral/local path); ANY lock read/write error fails CLOSED (never
     a "couldn't check, order anyway" fallback); and every real
     order-placing broker method (not just BUY) -- including a lock
     TAKEOVER's ability to still liquidate a pre-existing position -- goes
     through LockGuardedBroker's ownership re-check.
"""
from __future__ import annotations

import math
import time as time_module
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, order_executor, state_store, worker_lock
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.worker import Macd2Worker, run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


# ─────────────────────────── 1) lease primitives ────────────────────────────

def test_acquire_new_lock_when_unheld():
    now = datetime(2026, 8, 26, 9, 0, tzinfo=KST)
    result = worker_lock.try_acquire_or_renew("proc-A", now=now, stale_after_sec=60.0)
    assert result.owned is True
    assert result.reason == "ACQUIRED_NEW"
    on_disk = worker_lock.read_lock()
    assert on_disk is not None and on_disk.instance_id == "proc-A"


def test_renew_keeps_ownership_and_advances_heartbeat():
    t0 = datetime(2026, 8, 26, 9, 0, 0, tzinfo=KST)
    worker_lock.try_acquire_or_renew("proc-A", now=t0, stale_after_sec=60.0)
    t1 = t0 + timedelta(seconds=5)
    result = worker_lock.try_acquire_or_renew("proc-A", now=t1, stale_after_sec=60.0)
    assert result.owned is True
    assert result.reason == "RENEWED"
    assert result.current.last_heartbeat_at == t1.isoformat()


def test_second_instance_blocked_while_first_lease_is_fresh():
    now = datetime(2026, 8, 26, 9, 0, tzinfo=KST)
    worker_lock.try_acquire_or_renew("proc-A", now=now, stale_after_sec=60.0)
    result = worker_lock.try_acquire_or_renew("proc-B", now=now + timedelta(seconds=1), stale_after_sec=60.0)
    assert result.owned is False
    assert result.reason == "HELD_BY_OTHER"
    assert result.current.instance_id == "proc-A"
    # proc-A must still be the one on disk -- proc-B's failed attempt must
    # never have mutated the lock.
    assert worker_lock.read_lock().instance_id == "proc-A"


def test_stale_lease_is_taken_over_by_a_new_instance():
    t0 = datetime(2026, 8, 26, 9, 0, tzinfo=KST)
    worker_lock.try_acquire_or_renew("proc-A", now=t0, stale_after_sec=30.0)
    still_fresh = worker_lock.try_acquire_or_renew("proc-B", now=t0 + timedelta(seconds=10), stale_after_sec=30.0)
    assert still_fresh.owned is False

    past_stale = t0 + timedelta(seconds=31)
    takeover = worker_lock.try_acquire_or_renew("proc-B", now=past_stale, stale_after_sec=30.0)
    assert takeover.owned is True
    assert takeover.reason == "TAKEOVER"
    assert worker_lock.read_lock().instance_id == "proc-B"
    # proc-A must no longer believe it owns the lease.
    assert worker_lock.is_current_owner("proc-A") is False
    assert worker_lock.is_current_owner("proc-B") is True


def test_a_still_alive_owner_renewing_in_time_never_gets_taken_over():
    """A slow-but-alive owner (e.g. mid a long KIS retry chain) that renews
    its heartbeat just before the stale threshold must never be pre-empted."""
    t0 = datetime(2026, 8, 26, 9, 0, tzinfo=KST)
    worker_lock.try_acquire_or_renew("proc-A", now=t0, stale_after_sec=30.0)
    # proc-A renews right at the edge, before proc-B ever gets to check.
    worker_lock.try_acquire_or_renew("proc-A", now=t0 + timedelta(seconds=25), stale_after_sec=30.0)
    result = worker_lock.try_acquire_or_renew("proc-B", now=t0 + timedelta(seconds=40), stale_after_sec=30.0)
    assert result.owned is False
    assert worker_lock.read_lock().instance_id == "proc-A"


def test_release_only_removes_the_callers_own_lock():
    now = datetime(2026, 8, 26, 9, 0, tzinfo=KST)
    worker_lock.try_acquire_or_renew("proc-A", now=now, stale_after_sec=60.0)
    assert worker_lock.release("proc-B") is False  # not the owner -- no-op
    assert worker_lock.read_lock() is not None
    assert worker_lock.release("proc-A") is True
    assert worker_lock.read_lock() is None


def test_is_current_owner_false_when_no_lock_exists():
    assert worker_lock.is_current_owner("proc-A") is False


# ─────────────────────────── 2) LockGuardedBroker ────────────────────────────

def test_lock_guarded_broker_refuses_orders_when_not_owner():
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    worker_lock.try_acquire_or_renew("proc-OTHER", now=datetime.now(KST), stale_after_sec=60.0)
    guarded = worker_lock.LockGuardedBroker(broker, "proc-A")  # proc-A does NOT own the lock

    result = guarded.buy_limit(config.LONG_SYMBOL, 10, 15_000.0, "sig:BUY:x")
    assert isinstance(result, BrokerOrderResult)
    assert result.success is False
    assert result.message == "WORKER_LOCK_NOT_OWNED"
    assert broker.orders == []  # the real broker must never have been called

    sell_result = guarded.sell_market(config.LONG_SYMBOL, 10, "EXIT:x")
    assert sell_result.success is False
    assert sell_result.message == "WORKER_LOCK_NOT_OWNED"
    assert broker.orders == []


def test_lock_guarded_broker_passes_through_when_owner():
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    worker_lock.try_acquire_or_renew("proc-A", now=datetime.now(KST), stale_after_sec=60.0)
    guarded = worker_lock.LockGuardedBroker(broker, "proc-A")

    result = guarded.buy_limit(config.LONG_SYMBOL, 10, 15_000.0, "sig:BUY:x")
    assert result.success is True
    assert len(broker.orders) == 1


def test_lock_guarded_broker_never_touches_read_only_methods():
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    guarded = worker_lock.LockGuardedBroker(broker, "proc-A")  # no lock acquired at all
    assert guarded.get_quote(config.LONG_SYMBOL) == 15_000.0
    assert guarded.get_positions() == []
    assert guarded.mode == "mock"


# ─────────────────────────── 3) persistent-disk signal_id dedup ─────────────

def test_signal_id_has_leg_distinguishes_side_and_signal_id():
    assert ledger.signal_id_has_leg("SIG1", "BUY") is False
    ledger.append_execution({
        "order_id": "O1", "signal_id": "SIG1", "timestamp": datetime.now(KST).isoformat(),
        "mode": "mock", "symbol": config.LONG_SYMBOL, "side": "BUY",
        "requested_qty": 10, "executed_qty": 10, "requested_price": 100.0, "executed_price": 100.0,
        "position_before": 0, "position_after": 10,
        "gross_pnl": 0.0, "fee": 0.0, "slippage": 0.0, "net_pnl": 0.0,
        "exit_reason": "", "broker_response": "",
    })
    assert ledger.signal_id_has_leg("SIG1", "BUY") is True
    assert ledger.signal_id_has_leg("SIG1", "buy") is True  # case-insensitive
    assert ledger.signal_id_has_leg("SIG1", "SELL") is False  # a legitimate sell-leg retry must not be blocked
    assert ledger.signal_id_has_leg("SIG2", "BUY") is False


def test_execute_signal_blocks_a_buy_already_recorded_on_disk_for_this_signal_id():
    """The exact mechanism of the 2026-08-26 incident's duplicate BUY,
    reproduced directly at the order_executor level: TWO independent
    processed_signal_ids sets (simulating two processes' own in-memory
    state) both lack this signal_id, so the in-memory guard alone would let
    both through -- the persistent-ledger check must catch the second one
    regardless."""
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    signal_id = "20260826_090900_UP_RED:TW_CONFIRM"

    first = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
        quotes={config.LONG_SYMBOL: 15_000.0}, position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),  # "process A" -- never saw this signal_id before
    )
    assert first.final_state.value == "EXECUTED"
    assert len(broker.orders) == 1

    second = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
        quotes={config.LONG_SYMBOL: 15_000.0}, position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),  # "process B" -- ALSO never saw this signal_id (own memory)
    )
    assert second.final_state.value == "BLOCKED"
    assert second.block_reason == order_executor.BLOCK_DUPLICATE_SIGNAL
    assert len(broker.orders) == 1, "the persistent-disk check must have refused the second dispatch before any broker call"


# ─────────────────────────── 4) real Macd2Worker threads ────────────────────

def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    period = max(n_minutes // 2, 1)
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 10}
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


def _fresh_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.major_filter_enabled = False
    return state


def _make_worker(broker, market_data, *, tick_interval_sec: float = 0.05) -> Macd2Worker:
    holder = {"state": _fresh_state()}
    return Macd2Worker(
        broker=broker, market_data=market_data,
        get_state=lambda: holder["state"], save_state=lambda s: holder.__setitem__("state", s),
        tick_interval_sec=tick_interval_sec,
    )


@pytest.fixture
def two_process_market_data():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    df_1m = _1m_frame(prior_day, _sine_1m_closes(300))
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}))
    svc.bootstrap(now=prior_day + timedelta(minutes=300, seconds=5))
    return svc


def test_two_workers_started_concurrently_only_one_becomes_active(monkeypatch, two_process_market_data):
    monkeypatch.setattr(config, "WORKER_LOCK_STALE_AFTER_SEC", 5.0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    w1 = _make_worker(broker, two_process_market_data)
    w2 = _make_worker(broker, two_process_market_data)
    try:
        w1.start()
        w2.start()
        deadline = time_module.time() + 3.0
        while time_module.time() < deadline and (w1.tick_stats()["tick_n"] < 1 or w2.tick_stats()["tick_n"] < 1):
            time_module.sleep(0.02)

        owners = [w1.tick_stats()["order_lock_owned"], w2.tick_stats()["order_lock_owned"]]
        assert sorted(owners) == [False, True], f"expected exactly one ACTIVE owner, got {owners}"
    finally:
        w1.stop(join_timeout=2.0)
        w2.stop(join_timeout=2.0)


def test_second_worker_cannot_dispatch_while_first_owners_heartbeat_is_healthy(monkeypatch, two_process_market_data):
    monkeypatch.setattr(config, "WORKER_LOCK_STALE_AFTER_SEC", 5.0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    w1 = _make_worker(broker, two_process_market_data)
    try:
        w1.start()
        deadline = time_module.time() + 2.0
        while time_module.time() < deadline and w1.tick_stats()["order_lock_owned"] is not True:
            time_module.sleep(0.02)
        assert w1.tick_stats()["order_lock_owned"] is True

        w2 = _make_worker(broker, two_process_market_data)
        try:
            w2.start()
            time_module.sleep(0.3)  # several ticks -- w1's heartbeat stays fresh throughout
            stats2 = w2.tick_stats()
            assert stats2["order_lock_owned"] is False
            assert stats2["order_lock_status"] == "HELD_BY_OTHER"
            assert stats2["order_lock_holder_instance_id"] == w1.instance_id
        finally:
            w2.stop(join_timeout=2.0)
    finally:
        w1.stop(join_timeout=2.0)


def test_dead_owners_lease_times_out_and_a_new_worker_takes_over(monkeypatch, two_process_market_data):
    """Simulates a crashed process: the first Worker's tick thread is killed
    WITHOUT going through stop()'s graceful release (mirrors an abrupt
    container kill on Render, which never runs any shutdown code)."""
    monkeypatch.setattr(config, "WORKER_LOCK_STALE_AFTER_SEC", 0.2)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    w1 = _make_worker(broker, two_process_market_data, tick_interval_sec=0.02)
    w1.start()
    deadline = time_module.time() + 2.0
    while time_module.time() < deadline and w1.tick_stats()["order_lock_owned"] is not True:
        time_module.sleep(0.01)
    assert w1.tick_stats()["order_lock_owned"] is True
    w1_instance_id = w1.instance_id

    # Abrupt kill -- NOT w1.stop(): the loop exits on its own next check but
    # release() (only inside stop()) never runs, leaving the lease behind.
    w1._stop_event.set()
    time_module.sleep(0.05)
    assert w1.is_alive() is False

    w2 = _make_worker(broker, two_process_market_data, tick_interval_sec=0.02)
    try:
        w2.start()
        deadline = time_module.time() + 3.0
        while time_module.time() < deadline and w2.tick_stats()["order_lock_owned"] is not True:
            time_module.sleep(0.02)
        stats2 = w2.tick_stats()
        assert stats2["order_lock_owned"] is True
        assert stats2["order_lock_status"] == "TAKEOVER"
        assert worker_lock.is_current_owner(w1_instance_id) is False
    finally:
        w2.stop(join_timeout=2.0)


def test_rolling_deploy_overlap_never_produces_two_simultaneously_active_owners(monkeypatch, two_process_market_data):
    """Render rolling-deploy shape: the NEW process (w2) boots and starts
    ticking WHILE the OLD process (w1) is still alive and healthy, then the
    old one is torn down gracefully a bit later. At every sampled instant
    during the overlap, at most one of the two is ever ACTIVE."""
    monkeypatch.setattr(config, "WORKER_LOCK_STALE_AFTER_SEC", 5.0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    w_old = _make_worker(broker, two_process_market_data, tick_interval_sec=0.02)
    w_old.start()
    deadline = time_module.time() + 2.0
    while time_module.time() < deadline and w_old.tick_stats()["order_lock_owned"] is not True:
        time_module.sleep(0.01)
    assert w_old.tick_stats()["order_lock_owned"] is True

    w_new = _make_worker(broker, two_process_market_data, tick_interval_sec=0.02)
    try:
        w_new.start()  # overlap window begins -- both threads alive
        samples = []
        overlap_deadline = time_module.time() + 0.3
        while time_module.time() < overlap_deadline:
            samples.append((w_old.tick_stats()["order_lock_owned"], w_new.tick_stats()["order_lock_owned"]))
            time_module.sleep(0.01)
        assert any(s == (True, False) for s in samples), "old process should have stayed active during the overlap"
        assert not any(s == (True, True) for s in samples), f"two simultaneously ACTIVE owners observed: {samples}"

        # Old container fully torn down (graceful stop -- releases the lease).
        w_old.stop(join_timeout=2.0)
        deadline = time_module.time() + 2.0
        while time_module.time() < deadline and w_new.tick_stats()["order_lock_owned"] is not True:
            time_module.sleep(0.01)
        assert w_new.tick_stats()["order_lock_owned"] is True
    finally:
        w_new.stop(join_timeout=2.0)
        w_old.stop(join_timeout=2.0)


def test_normal_held_by_other_standby_never_reports_an_exception(monkeypatch, two_process_market_data):
    """A healthy standby instance (another live process genuinely owns the
    lease) must NEVER surface anything in last_exception -- tick_n/
    last_tick_at already look identical whether owned or not, so this is the
    one remaining signal that must stay quiet during ordinary operation."""
    monkeypatch.setattr(config, "WORKER_LOCK_STALE_AFTER_SEC", 60.0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    worker_lock.try_acquire_or_renew("someone-else", now=datetime.now(KST), stale_after_sec=60.0)

    w = _make_worker(broker, two_process_market_data, tick_interval_sec=0.02)
    w.start()
    try:
        deadline = time_module.time() + 1.0
        while time_module.time() < deadline and w.tick_stats()["tick_n"] < 3:
            time_module.sleep(0.01)
        stats = w.tick_stats()
        assert stats["order_lock_owned"] is False
        assert stats["order_lock_status"] == "HELD_BY_OTHER"
        assert stats["last_exception"] is None
        assert stats["tick_n"] >= 3  # the loop keeps ticking normally, just standing by
    finally:
        w.stop(join_timeout=2.0)


def test_persistent_lock_error_is_surfaced_as_an_exception_not_silently_swallowed(monkeypatch, two_process_market_data):
    """The 2026-08-26 observability gap this fix closes: a lock ERROR/
    undetermined-state result must show up in last_exception (the existing
    'Worker 마지막 예외' dashboard panel) so 'zero orders even with every
    filter off' is diagnosable without a code change -- previously
    last_exception was unconditionally reset to None every tick regardless
    of lock state, so a persistently fail-closed worker looked identical to
    a perfectly healthy one on the dashboard."""
    monkeypatch.setattr(config, "WORKER_LOCK_STALE_AFTER_SEC", 60.0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})

    def _always_erroring_acquire(*_a, **_k):
        return worker_lock.LockResult(False, "ERROR:OSError('simulated Persistent Disk failure')", None)

    monkeypatch.setattr(worker_lock, "try_acquire_or_renew", _always_erroring_acquire)

    w = _make_worker(broker, two_process_market_data, tick_interval_sec=0.02)
    w.start()
    try:
        deadline = time_module.time() + 1.0
        while time_module.time() < deadline and w.tick_stats()["tick_n"] < 2:
            time_module.sleep(0.01)
        stats = w.tick_stats()
        assert stats["order_lock_owned"] is False
        assert stats["last_exception"] is not None
        assert "lock" in stats["last_exception"].lower()
        assert broker.orders == [], "a fail-closed worker must never place any order regardless of filter config"
    finally:
        w.stop(join_timeout=2.0)


# ─────────────── 5) full 09:09 UP_RED / TW2 incident reproduction ───────────

def _tw2_rally_1m(start: datetime) -> pd.DataFrame:
    """A clean, monotonic rally producing a real, confirmable UP_RED
    crossover with TW2 on -- same recipe as tests/macd2/test_time_window_2.py's
    _warmup_then_rally, expressed directly in 1-minute bars (worker.run_once's
    actual input) instead of pre-resampled 3-minute bars."""
    closes: list[float] = [100.0] * (26 * 3)  # EMA_SLOW(26) warm-up on flat price
    price = 100.0
    for _ in range(90 * 3):
        price += 0.05
        closes.append(round(price, 4))
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": c - 0.01, "high": c + 0.05, "low": c - 0.05, "close": c, "volume": 1000,
        })
    return pd.DataFrame(rows)


def _tw2_state(*, budget: float = 10_000_000.0) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = budget
    state.time_window_2_filter_enabled = True
    return state


def _run_two_processes(broker, svc, now0, *, guarded: bool, monkeypatch=None):
    """Walks state_a/state_b tick-by-tick against the SAME shared broker
    (the single real account both processes in the real incident shared),
    exactly reproducing "two independent in-memory RuntimeState objects,
    one account" -- with the lock gate wired in (``guarded=True``, the real
    Macd2Worker._run_loop shape) or deliberately bypassed
    (``guarded=False``, pre-fix shape) to isolate which layer is doing the
    protecting."""
    state_a, state_b = _tw2_state(), _tw2_state()
    instance_a, instance_b = "render-old", "render-new"
    for step in range(140):
        now = now0 + timedelta(minutes=3 * step)
        for state, instance_id in ((state_a, instance_a), (state_b, instance_b)):
            state.worker_instance_id = instance_id
            if guarded:
                lock_result = worker_lock.try_acquire_or_renew(instance_id, now=now, stale_after_sec=180.0)
                if not lock_result.owned:
                    continue
                dispatch_broker = worker_lock.LockGuardedBroker(broker, instance_id)
            else:
                dispatch_broker = broker
            run_once(broker=dispatch_broker, market_data=svc, state=state, now=now)
        if any(o.side == "BUY" and o.success for o in broker.orders):
            break
    return state_a, state_b


@pytest.fixture
def tw2_incident_market_data():
    start = datetime(2026, 8, 26, 9, 0, tzinfo=KST)
    df_1m = _tw2_rally_1m(start)
    quote_prices = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0, config.WATCH_SYMBOL: 100.0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    result = svc.bootstrap(now=start + timedelta(days=2))
    assert result.ok, f"fixture bootstrap failed unexpectedly: {result.reason}"
    return svc, start + timedelta(minutes=3 * (config.SIGNAL_MIN_BAR_INDEX + 1))


def test_incident_reproduction_persistent_ledger_alone_still_limits_to_one_order(tw2_incident_market_data):
    """Lock gate deliberately bypassed (pre-2026-08-26-fix shape: both
    processes call run_once() directly against the shared broker with no
    lease check at all) -- the persistent-disk signal_id/side dedup inside
    order_executor.execute_signal must still be the backstop that limits
    the account to exactly one real BUY."""
    svc, now0 = tw2_incident_market_data
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    state_a, state_b = _run_two_processes(broker, svc, now0, guarded=False)

    buy_orders = [o for o in broker.orders if o.side == "BUY" and o.success]
    assert len(buy_orders) == 1, f"expected exactly one real BUY order, got {len(buy_orders)}: {buy_orders}"
    # Process B's own BUY attempt for the same signal_id was refused by the
    # persistent-ledger dedup (not by reconcile racing ahead of it) -- the
    # broker itself only ever saw one order, which is the property that
    # actually matters (state_b separately, correctly adopting the resulting
    # real position via the existing reconcile_position_state RECOVERED_
    # FROM_BROKER path is expected/desired, not a bug: a genuine second
    # process must still learn about the real held position to manage its
    # risk, it just must never be the one that PLACED it).
    assert buy_orders[0].executed_qty == 662
    assert state_a.position is not None and state_a.position.quantity == 662
    assert state_b.position is not None and state_b.position.quantity == 662


def test_incident_reproduction_with_lock_gate_only_the_owner_ever_dispatches(tw2_incident_market_data):
    """Full fix wired in (mirrors Macd2Worker._run_loop): the SECOND process
    never even reaches run_once()'s order dispatch once the first has
    claimed the lease -- exactly one real BUY order, and it is the lock
    owner's own state that reflects the resulting position."""
    svc, now0 = tw2_incident_market_data
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    state_a, state_b = _run_two_processes(broker, svc, now0, guarded=True)

    buy_orders = [o for o in broker.orders if o.side == "BUY" and o.success]
    assert len(buy_orders) == 1, f"expected exactly one real BUY order, got {len(buy_orders)}: {buy_orders}"
    # Exactly one signal-ledger row for the confirmed TW_CONFIRM entry --
    # no RECONCILE_DISCOVERED duplicate spam from a second process
    # rediscovering the first process's own fill.
    rows = ledger.load_signal_ledger()
    reconcile_rows = [r for r in rows if r.get("signal_type") == "RECONCILE_DISCOVERED"]
    assert reconcile_rows == [], f"lock-gated run must never need a reconcile-discovered backfill: {reconcile_rows}"


# ─────────────── 6) three pre-commit checks (2026-08-26 review) ─────────────

def test_lock_path_lives_under_the_same_persistent_disk_directory_as_state_store():
    """worker_lock.py must never invent its own path -- it has to resolve
    under app.utils.data_paths.STATE_DIR, the SAME constant state_store.py
    already uses for macd2_runtime.json. That file surviving a Render
    redeploy (auto_trade_on=True read back by the freshly-booted process)
    is exactly the precondition that let today's incident happen in the
    first place -- proof this directory really is the Persistent Disk
    mount in production, not container-local ephemeral storage. Co-locating
    the lock file in the identical directory means it inherits that same
    guarantee, with zero new configuration to get wrong."""
    assert worker_lock.STATE_DIR_PATH == state_store.STATE_DIR_PATH
    assert worker_lock.LOCK_PATH.parent == state_store.STATE_PATH.parent
    assert worker_lock.LOCK_FILENAME != config.RUNTIME_STATE_FILENAME  # distinct file, same directory


def test_lock_directory_resolves_under_ai_gap_data_dir_env_var(monkeypatch, tmp_path):
    """Direct proof that app.utils.data_paths' own path-resolution honors
    AI_GAP_DATA_DIR (the documented Render Persistent Disk mount env var,
    docs/deploy_render.md) -- the same function worker_lock.STATE_DIR_PATH
    (imported from app.utils.data_paths.STATE_DIR) is built from."""
    from app.utils import data_paths

    monkeypatch.setenv(data_paths.DATA_ROOT_ENV_VAR, str(tmp_path))
    resolved_root = data_paths._resolve_data_root()
    assert resolved_root == tmp_path
    assert (resolved_root / "state").parent == tmp_path


def test_acquire_never_raises_and_fails_closed_on_a_read_error(monkeypatch):
    """FAIL-CLOSED CONTRACT: an unexpected error while reading lock state
    must never be silently treated as 'go ahead, place the order' -- it
    must come back as owned=False, and try_acquire_or_renew must never let
    the exception escape to the caller (see its own docstring)."""
    def _boom():
        raise OSError("simulated Persistent Disk read failure")

    monkeypatch.setattr(worker_lock, "read_lock", _boom)
    result = worker_lock.try_acquire_or_renew("proc-A", now=datetime.now(KST), stale_after_sec=60.0)
    assert result.owned is False
    assert result.reason.startswith("ERROR:")


def test_acquire_never_raises_and_fails_closed_on_a_write_error(monkeypatch):
    """Same contract, but the failure is on the WRITE side (e.g. disk full,
    permission denied, or the Persistent Disk simply not mounted) -- must
    still resolve to owned=False, never raise, never fall back to assuming
    ownership."""
    def _boom(*_a, **_k):
        raise OSError("simulated Persistent Disk write failure")

    monkeypatch.setattr(worker_lock, "_write_lock_atomic", _boom)
    result = worker_lock.try_acquire_or_renew("proc-A", now=datetime.now(KST), stale_after_sec=60.0)
    assert result.owned is False
    assert result.reason.startswith("ERROR:")
    # A second instance querying right after must also see no lock claimed --
    # the failed write must never have left a partial/inconsistent lease.
    assert worker_lock.read_lock() is None


def test_ensure_paths_failure_also_fails_closed(monkeypatch):
    """The Persistent Disk mount itself being unavailable (mkdir on the
    state directory fails outright) is the single most realistic real-world
    version of 'lock file cannot be read or written' -- must still resolve
    to owned=False, not raise."""
    def _boom():
        raise OSError("simulated: Persistent Disk not mounted")

    monkeypatch.setattr(worker_lock, "ensure_paths", _boom)
    result = worker_lock.try_acquire_or_renew("proc-A", now=datetime.now(KST), stale_after_sec=60.0)
    assert result.owned is False
    assert result.reason.startswith("ERROR:")


def test_is_current_owner_fails_closed_on_read_error(monkeypatch):
    """The 'right before the order' re-check LockGuardedBroker relies on
    must ALSO fail closed -- a read error here must refuse the order, not
    let it through."""
    def _boom():
        raise OSError("simulated read failure")

    monkeypatch.setattr(worker_lock, "LOCK_PATH", worker_lock.LOCK_PATH.parent / "does" / "not" / "exist" / "x.json")
    # A path whose parent directories don't exist raises OSError (or a
    # subclass) from Path.read_text -- read_lock's own try/except already
    # catches this without needing the monkeypatch above; this test exists
    # to pin that guarantee explicitly for is_current_owner specifically.
    assert worker_lock.is_current_owner("proc-A") is False


def test_all_four_order_placing_broker_methods_are_guarded():
    """Point 3 of the 2026-08-26 review: not just buy_limit/sell_market
    (already covered above) -- buy_market and buy_ioc_limit (legacy paths
    still reachable via broker_adapter.py) must be refused identically when
    not the lock owner."""
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    guarded = worker_lock.LockGuardedBroker(broker, "proc-A")  # lock never acquired

    for call in (
        lambda: guarded.buy_market(config.LONG_SYMBOL, 10, "sig:BUY:x"),
        lambda: guarded.buy_ioc_limit(config.LONG_SYMBOL, 10, 15_000.0, "sig:BUY:x"),
        lambda: guarded.buy_limit(config.LONG_SYMBOL, 10, 15_000.0, "sig:BUY:x"),
        lambda: guarded.sell_market(config.LONG_SYMBOL, 10, "EXIT:x"),
    ):
        result = call()
        assert result.success is False
        assert result.message == "WORKER_LOCK_NOT_OWNED"
    assert broker.orders == []


def test_cancel_order_is_never_guarded():
    """cancel_order cleans up THIS tick's own just-placed unfilled remainder
    (order_executor._cancel_unfilled) -- it must pass straight through
    regardless of lock ownership; refusing it would leave a real resting
    order un-cancelled at the broker."""
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    guarded = worker_lock.LockGuardedBroker(broker, "proc-A")  # lock never acquired
    result = guarded.cancel_order("SOME-ORDER-ID", config.LONG_SYMBOL)
    assert result.success is True
    assert broker.cancel_calls == [("SOME-ORDER-ID", config.LONG_SYMBOL)]


def test_new_owner_after_takeover_can_still_liquidate_a_preexisting_position():
    """Point 3's explicit requirement: a lock TAKEOVER must not strand an
    existing position with no risk management. proc-A opens a real
    position and then goes dark (simulated crash, no clean release,
    heartbeat never renewed again); proc-B takes over the lease once it
    goes stale and its OWN sell_market call against that SAME broker-held
    position must be allowed through LockGuardedBroker -- never refused as
    WORKER_LOCK_NOT_OWNED just because proc-A was the one who opened it."""
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})

    # proc-A: acquire the lease and open a real position (stands in for "a
    # confirmed entry proc-A dispatched before it died").
    now0 = datetime(2026, 8, 26, 9, 0, tzinfo=KST)
    worker_lock.try_acquire_or_renew("proc-A", now=now0, stale_after_sec=30.0)
    guarded_a = worker_lock.LockGuardedBroker(broker, "proc-A")
    buy_result = guarded_a.buy_market(config.LONG_SYMBOL, 100, "manual:BUY:seed")
    assert buy_result.success is True
    assert broker.get_position(config.LONG_SYMBOL).quantity == 100
    # proc-A goes dark -- never releases, never renews again.

    # proc-A itself must now be refused (its own lease already expired) --
    # the exact "not the current owner" case, not a false positive because
    # nobody has claimed the lease at all.
    stale_now = now0 + timedelta(seconds=31)
    assert worker_lock.is_current_owner("proc-A") is True  # lease record still says proc-A until someone takes over

    # proc-B takes over once the lease is genuinely stale.
    takeover = worker_lock.try_acquire_or_renew("proc-B", now=stale_now, stale_after_sec=30.0)
    assert takeover.owned is True
    assert takeover.reason == "TAKEOVER"
    assert worker_lock.is_current_owner("proc-A") is False
    assert worker_lock.is_current_owner("proc-B") is True

    # The actual point: proc-B, the new legitimate owner, can sell the
    # position proc-A opened -- LockGuardedBroker must never block this.
    guarded_b = worker_lock.LockGuardedBroker(broker, "proc-B")
    sell_result = guarded_b.sell_market(config.LONG_SYMBOL, 100, "EXIT:STOP_LOSS:seed")
    assert sell_result.success is True
    assert sell_result.message != "WORKER_LOCK_NOT_OWNED"
    assert broker.get_position(config.LONG_SYMBOL) is None  # fully liquidated

    # And proc-A, no longer the owner, must now be refused if it somehow
    # tried to act again (e.g. a delayed/zombie retry after waking back up).
    zombie_result = guarded_a.sell_market(config.LONG_SYMBOL, 1, "EXIT:zombie")
    assert zombie_result.success is False
    assert zombie_result.message == "WORKER_LOCK_NOT_OWNED"
