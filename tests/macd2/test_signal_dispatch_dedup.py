"""Regression tests for the 2026-08-26 minimal fix: persistent-disk
signal_id/side dedup at order dispatch (no cross-process worker lock, no
market_data/quote/history/TW2/MACD changes -- see order_executor.py's
``execute_signal`` and ledger.py's ``signal_id_has_leg`` /
``try_claim_signal_dispatch`` / ``release_signal_dispatch_claim``).

Scope, exactly as specified:
  1) Same signal_id dispatched twice (sequential) -> exactly one real BUY.
  2) Two independent RuntimeState "processes" racing the SAME signal_id at
     the same instant (real threading, not just sequential calls) ->
     exactly one real BUY.
  3) A normal BUY -> SELL round trip is still fully recorded via the
     existing _record_leg path, byte-for-byte unchanged.
  4) A BUY whose broker response looks like a failure/exception, but whose
     fill is later discovered via reconcile_position_state, gets exactly
     one minimal RECONCILE_BACKFILL BUY leg (existing, untouched worker.py
     mechanism) -- and the position can still be sold normally afterward,
     with the full round trip present in the ledger.
  5) A duplicate reconcile of the same real position never creates a
     second backfill row.
  6) A signal blocked for a genuinely recoverable reason (explicit broker
     rejection on the first attempt) must still be able to succeed on a
     later retry -- the dedup must never permanently strand a legitimate
     signal that simply hasn't succeeded yet.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from app.models import Position
from app.trading.macd2 import config, ledger, order_executor, state_store, worker
from app.trading.macd2.models import Direction, PositionSnapshot
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _quotes(price: float = 15_000.0) -> dict[str, float]:
    return {config.LONG_SYMBOL: price}


# ─────────────────────── 1) same signal_id twice ────────────────────────

def test_same_signal_id_dispatched_twice_only_one_real_buy():
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    signal_id = "20260826_090900_UP_RED:TW_CONFIRM"

    first = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
        quotes=_quotes(), position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),
    )
    assert first.final_state.value == "EXECUTED"
    assert len(broker.orders) == 1

    # Second call deliberately uses an EMPTY processed_signal_ids too (as if
    # a second, independent process had no idea the first ever happened).
    second = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
        quotes=_quotes(), position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),
    )
    assert second.final_state.value == "BLOCKED"
    assert second.block_reason == order_executor.BLOCK_DUPLICATE_SIGNAL
    assert len(broker.orders) == 1, "the persistent-disk check must have refused the second dispatch before any broker call"


# ──────────────── 2) genuinely concurrent race, same signal_id ──────────

def test_two_independent_states_racing_the_same_signal_id_only_one_buy():
    """Real threading, not just two sequential calls -- exercises the
    atomic try_claim_signal_dispatch()/O_CREAT|O_EXCL path, not just
    signal_id_has_leg()'s post-hoc check (which alone cannot close a race
    where neither side has recorded a leg yet)."""
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    signal_id = "20260826_120300_UP_RED:TW_CONFIRM"
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def _dispatch(idx: int) -> None:
        barrier.wait(timeout=5.0)  # both threads reach execute_signal at the same instant
        results[idx] = order_executor.execute_signal(
            broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
            quotes=_quotes(), position=None, budget=1_000_000.0,
            processed_signal_ids=frozenset(),
        )

    t1 = threading.Thread(target=_dispatch, args=(0,))
    t2 = threading.Thread(target=_dispatch, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    outcomes = [r.final_state.value for r in results]
    assert sorted(outcomes) == ["BLOCKED", "EXECUTED"], f"expected exactly one winner, got {outcomes}"
    buy_orders = [o for o in broker.orders if o.side == "BUY" and o.success]
    assert len(buy_orders) == 1, f"expected exactly one real BUY order, got {len(buy_orders)}: {buy_orders}"


# ──────────────────── 3) normal BUY -> SELL, full ledger ────────────────

def test_normal_buy_then_sell_fully_recorded_in_execution_ledger():
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    buy_signal_id = "20260826_130000_UP_RED:TW_CONFIRM"

    buy_outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=buy_signal_id,
        quotes=_quotes(), position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),
    )
    assert buy_outcome.final_state.value == "EXECUTED"
    bought_qty = buy_outcome.quantity
    assert bought_qty > 0

    position = PositionSnapshot(
        symbol=config.LONG_SYMBOL, quantity=bought_qty, avg_price=buy_outcome.filled_avg_price, entry_at=datetime.now(KST),
    )
    sell_outcome = order_executor.execute_exit(
        broker=broker, symbol=config.LONG_SYMBOL, quantity=bought_qty,
        exit_reason=config.EXIT_STOP_LOSS, entry_price=position.avg_price,
    )
    assert sell_outcome.final_state.value == "EXECUTED"

    rows = ledger.load_execution_ledger()
    sides = sorted(str(r.get("side")) for r in rows)
    assert sides == ["BUY", "SELL"]
    buy_row = next(r for r in rows if r["side"] == "BUY")
    sell_row = next(r for r in rows if r["side"] == "SELL")
    assert buy_row["signal_id"] == buy_signal_id
    assert int(float(buy_row["executed_qty"])) == bought_qty
    assert int(float(sell_row["executed_qty"])) == bought_qty
    assert sell_row.get("exit_reason") == config.EXIT_STOP_LOSS


# ────── 4) BUY response failure -> reconcile discovers fill -> backfill ─

def test_buy_response_failure_then_reconcile_backfill_then_sell_fully_recorded():
    """Mirrors the real 2026-08-25 incident this backfill mechanism (already
    existing in worker.py/ledger.py, untouched by this fix) guards against:
    the broker's own response reports failure, but the order actually
    filled server-side. execute_signal() itself never fabricates a leg for
    a failed response -- reconcile_position_state() is what discovers the
    mismatch and worker.append_reconcile_backfill_buy backfills exactly
    what's confirmable (symbol/qty/avg_price), never an estimated time."""
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    signal_id = "20260826_140000_UP_RED:TW_CONFIRM"

    broker.fail_next_buy = True
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
        quotes=_quotes(), position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),
    )
    assert outcome.final_state.value == "FAILED"
    assert ledger.load_execution_ledger() == []  # no leg recorded for the failed response

    # The order actually filled at the broker despite the failure response
    # (simulates a lost/garbled response, exactly like the real incident).
    broker._positions[config.LONG_SYMBOL] = Position(
        symbol=config.LONG_SYMBOL, name=config.LONG_SYMBOL, quantity=500, avg_price=15_010.0, current_price=15_010.0,
    )

    state = state_store.default_state()
    state.auto_trade_on = True
    now = datetime.now(KST)
    result = worker.reconcile_position_state(broker, state, now, force=True)
    assert result == worker.RECOVERED_FROM_BROKER
    assert state.position is not None
    assert state.position.quantity == 500

    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    backfill_row = rows[0]
    assert backfill_row["side"] == "BUY"
    assert backfill_row["source"] == "RECONCILE_BACKFILL"
    assert int(float(backfill_row["executed_qty"])) == 500
    assert float(backfill_row["executed_price"]) == 15_010.0
    # Never a fabricated fill time -- the backfill timestamp is the
    # reconcile discovery moment, not an estimate.
    assert backfill_row["timestamp"]

    # The discovered position can still be sold normally afterward.
    sell_outcome = order_executor.execute_exit(
        broker=broker, symbol=config.LONG_SYMBOL, quantity=500,
        exit_reason=config.EXIT_STOP_LOSS, entry_price=15_010.0,
    )
    assert sell_outcome.final_state.value == "EXECUTED"

    rows = ledger.load_execution_ledger()
    sides = sorted(str(r.get("side")) for r in rows)
    assert sides == ["BUY", "SELL"]
    assert any(r["side"] == "BUY" and r["source"] == "RECONCILE_BACKFILL" for r in rows)
    sell_row = next(r for r in rows if r["side"] == "SELL")
    assert int(float(sell_row["executed_qty"])) == 500


# ───────────────────── 5) duplicate reconcile -> no dup backfill ────────

def test_duplicate_reconcile_never_creates_a_second_backfill_row():
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    broker._positions[config.LONG_SYMBOL] = Position(
        symbol=config.LONG_SYMBOL, name=config.LONG_SYMBOL, quantity=300, avg_price=14_500.0, current_price=14_500.0,
    )
    state = state_store.default_state()
    state.auto_trade_on = True
    now = datetime.now(KST)

    first = worker.reconcile_position_state(broker, state, now, force=True)
    assert first == worker.RECOVERED_FROM_BROKER
    assert len(ledger.load_execution_ledger()) == 1

    # A second reconcile a moment later (state now correctly reflects the
    # position, so this resolves as MATCH_POSITION, not another discovery)
    # -- and even a forced re-discovery (simulating a state reset back to
    # flat) must still resolve to the SAME deterministic backfill order_id.
    second = worker.reconcile_position_state(broker, state, now + timedelta(seconds=5), force=True)
    assert second != worker.RECOVERED_FROM_BROKER
    assert len(ledger.load_execution_ledger()) == 1

    state.position = None  # simulate a state reset that forgets it again
    third = worker.reconcile_position_state(broker, state, now + timedelta(seconds=10), force=True)
    assert third == worker.RECOVERED_FROM_BROKER
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1, f"a re-discovery of the SAME real position must never create a second backfill row: {rows}"


# ──────────── 6) a genuinely recoverable block must still succeed later ─

def test_explicit_broker_rejection_then_legitimate_retry_still_succeeds():
    """The dedup must never permanently strand a signal_id that simply
    hasn't succeeded yet -- 사용자 확인 요청: '주문이 안된 경우에는 체크해서
    다시 한번 확인해서 또 들어갈 수는 있음'. release_signal_dispatch_claim()
    is exactly what makes this retry possible after a DEFINITE rejection
    (as opposed to an ambiguous NO_ORDER_ID outcome, which deliberately
    stays claimed -- see ledger.try_claim_signal_dispatch's docstring)."""
    broker = FakeBroker(cash=10_000_000.0, quotes=_quotes())
    signal_id = "20260826_150000_UP_RED:TW_CONFIRM"

    broker.fail_next_buy = True
    first = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
        quotes=_quotes(), position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),
    )
    assert first.final_state.value == "FAILED"
    assert first.block_reason == order_executor.FAIL_BUY
    assert ledger.load_execution_ledger() == []

    # Legitimate retry, same signal_id, broker now accepts it for real.
    second = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
        quotes=_quotes(), position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),
    )
    assert second.final_state.value == "EXECUTED"
    buy_orders = [o for o in broker.orders if o.side == "BUY" and o.success]
    assert len(buy_orders) == 1

    # And now that a real leg exists, a THIRD attempt must be refused again.
    third = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id=signal_id,
        quotes=_quotes(), position=None, budget=1_000_000.0,
        processed_signal_ids=frozenset(),
    )
    assert third.final_state.value == "BLOCKED"
    assert third.block_reason == order_executor.BLOCK_DUPLICATE_SIGNAL
    assert len(buy_orders) == 1
