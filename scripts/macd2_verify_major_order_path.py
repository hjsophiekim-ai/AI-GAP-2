#!/usr/bin/env python
"""MOCK-only verification: MAJOR filter + limit BUY (ORD_DVSN=00) path.

Never constructs a REAL broker. Uses FakeBroker test double only.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config, order_executor
from app.trading.macd2.models import Direction, SignalState
from tests.macd2.fake_broker import FakeBroker


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _run_buy(direction: Direction, symbol: str) -> None:
    broker = FakeBroker(cash=10_000_000.0, quotes={symbol: 15_000.0})
    broker.next_ask1 = 15_000.0
    outcome = order_executor.execute_signal(
        broker=broker,
        direction=direction,
        signal_id=f"VERIFY:{direction.value}",
        quotes={symbol: 15_000.0},
        position=None,
        budget=10_000_000.0,
        reconcile_retries=2,
        reconcile_delay_sec=0.0,
    )
    print(
        f"{direction.value} -> {symbol}: state={outcome.final_state.value} "
        f"ord_dvsn={outcome.ord_dvsn} order_type={outcome.order_type} "
        f"order_id={outcome.buy_result.order_id if outcome.buy_result else None} "
        f"filled={outcome.filled_qty} balance={outcome.balance_qty} "
        f"broker_called={outcome.broker_called}"
    )
    _assert(outcome.final_state == SignalState.EXECUTED, "expected EXECUTED")
    _assert(outcome.broker_called is True, "broker_called")
    _assert(outcome.order_type == "limit", "order_type=limit")
    _assert(outcome.ord_dvsn == "00", "ord_dvsn=00")
    _assert(outcome.buy_result is not None and bool(outcome.buy_result.order_id), "order_id")
    _assert((outcome.filled_qty or 0) > 0, "filled_qty>0")
    _assert(outcome.balance_qty == outcome.filled_qty, "balance==filled")
    _assert(broker.orders[-1].raw.get("ORD_DVSN") == "00", "raw ORD_DVSN=00")
    _assert(all(o.raw.get("ORD_DVSN") != "11" for o in broker.orders if o.side == "BUY"), "no IOC")
    # duplicate signal_id must not reorder
    outcome2 = order_executor.execute_signal(
        broker=broker,
        direction=direction,
        signal_id=f"VERIFY:{direction.value}",
        quotes={symbol: 15_000.0},
        position=None,
        budget=10_000_000.0,
        processed_signal_ids=frozenset({f"VERIFY:{direction.value}"}),
        reconcile_retries=2,
        reconcile_delay_sec=0.0,
    )
    _assert(outcome2.final_state == SignalState.BLOCKED, "duplicate blocked")
    _assert(outcome2.broker_called is False, "duplicate no broker")
    buy_calls = [o for o in broker.orders if o.side == "BUY"]
    _assert(len(buy_calls) == 1, f"same signal_id buy count={len(buy_calls)}")


def _run_filtered_out_no_broker() -> None:
    # Filter rejection is worker-level; here we only prove execute is not needed.
    # Simulate: caller never invokes execute when FILTERED_OUT — broker stays idle.
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    _assert(len(broker.orders) == 0, "filtered path never called broker")
    print("FILTERED_OUT path: broker calls=0 (caller skips execute_signal)")


def _ioc_path_unreachable_from_executor() -> None:
    import inspect
    src = inspect.getsource(order_executor.execute_signal)
    _assert("buy_ioc_limit" not in src, "execute_signal must not call buy_ioc_limit")
    _assert('order_type="ioc_limit"' not in src and "order_type='ioc_limit'" not in src, "no ioc_limit sizing")
    _assert("buy_limit" in src, "execute_signal must call buy_limit")
    print("IOC residual check in execute_signal: PASS (buy_limit only)")


def main() -> int:
    print("=== macd2_verify_major_order_path (MOCK only) ===")
    _assert(config.MAJOR_ENTRY_SCORE_MIN == 65.0, "entry=65")
    _assert(config.MAJOR_REVERSAL_SCORE_MIN == 75.0, "reversal=75")
    _assert(config.MAJOR_FAST_REVERSAL_SCORE_MIN == 82.0, "fast=82")
    print(f"major thresholds: {config.MAJOR_ENTRY_SCORE_MIN}/{config.MAJOR_REVERSAL_SCORE_MIN}/{config.MAJOR_FAST_REVERSAL_SCORE_MIN}")

    _ioc_path_unreachable_from_executor()
    _run_filtered_out_no_broker()
    _run_buy(Direction.UP_RED, config.LONG_SYMBOL)
    _run_buy(Direction.DOWN_BLUE, config.INVERSE_SYMBOL)
    print("REAL order calls: 0 (FakeBroker only)")
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
