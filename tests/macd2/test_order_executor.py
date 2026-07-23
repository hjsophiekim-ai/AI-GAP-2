"""Unit tests for app.trading.macd2.order_executor — FakeBroker only, no real network."""
from __future__ import annotations

from app.trading.macd2 import ledger, order_executor
from app.trading.macd2.models import Direction, PositionSnapshot, SignalState
from tests.macd2.fake_broker import FakeBroker


def test_compute_order_quantity_uses_smaller_of_cash_and_budget():
    # price=1000, budget=10000 (10 shares), cash=3000 (3 shares) -> cash wins
    qty = order_executor.compute_order_quantity(available_cash=3000, budget=10000, price=1000, safety_margin_pct=0)
    assert qty == 3


def test_compute_order_quantity_applies_safety_margin():
    qty = order_executor.compute_order_quantity(available_cash=10_000, budget=10_000, price=100, safety_margin_pct=5.0)
    assert qty == 95  # 10000*0.95=9500 / 100 = 95


def test_compute_order_quantity_blocks_below_one_share():
    qty = order_executor.compute_order_quantity(available_cash=50, budget=10_000, price=100)
    assert qty == 0


def test_flat_entry_up_red_buys_long_symbol():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-1",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
    )
    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.buy_result.success is True
    assert broker.get_position("0193T0").quantity == outcome.quantity
    assert outcome.quantity > 0

    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY"
    assert rows[0]["signal_id"] == "sig-1"


def test_flat_entry_down_blue_buys_inverse_symbol():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0197X0": 10_000.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="sig-2",
        quotes={"0197X0": 10_000.0}, position=None, budget=10_000_000.0,
    )
    assert outcome.final_state == SignalState.EXECUTED
    assert broker.get_position("0197X0") is not None


def test_opposite_switch_sells_before_buying():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    # Seed a held inverse position directly on the fake broker.
    broker.set_quote("0197X0", 10_000.0)
    broker.buy_market("0197X0", 20, "seed")
    position = PositionSnapshot(symbol="0197X0", quantity=20, avg_price=10_000.0)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-3",
        quotes={"0193T0": 15_000.0, "0197X0": 10_000.0}, position=position, budget=10_000_000.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.sell_result.success is True
    assert outcome.sell_qty_after == 0
    assert broker.get_position("0197X0") is None  # fully sold
    assert broker.get_position("0193T0") is not None  # new long entered

    rows = ledger.load_execution_ledger()
    sides = [r["side"] for r in rows]
    assert sides == ["SELL", "BUY"]  # sell recorded before buy


def test_duplicate_signal_id_blocked_before_any_order():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-4",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
        processed_signal_ids=frozenset({"sig-4"}),
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_DUPLICATE_SIGNAL
    assert broker.orders == []


def test_already_holding_same_direction_blocks_additional_buy():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    position = PositionSnapshot(symbol="0193T0", quantity=10, avg_price=15_000.0)
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-5",
        quotes={"0193T0": 15_000.0}, position=position, budget=10_000_000.0,
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_ALREADY_HOLDING
    assert broker.orders == []


def test_stale_or_missing_quote_blocks_order_data_invalid():
    broker = FakeBroker(cash=10_000_000.0)
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-6",
        quotes={}, position=None, budget=10_000_000.0,
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_ORDER_DATA_INVALID


def test_insufficient_cash_blocks_qty_lt_1():
    broker = FakeBroker(cash=50.0, quotes={"0193T0": 15_000.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-7",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_INSUFFICIENT_QTY


def test_sell_failure_blocks_before_any_buy_attempt():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    broker.buy_market("0197X0", 20, "seed")
    broker.fail_next_sell = True
    position = PositionSnapshot(symbol="0197X0", quantity=20, avg_price=10_000.0)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-8",
        quotes={"0193T0": 15_000.0, "0197X0": 10_000.0}, position=position, budget=10_000_000.0,
    )
    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_SELL
    assert broker.get_position("0193T0") is None  # never reached the buy step
    assert ledger.load_execution_ledger() == []  # nothing recorded for a failed leg


def test_reconcile_failure_blocks_before_buy(monkeypatch):
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    broker.buy_market("0197X0", 20, "seed")
    position = PositionSnapshot(symbol="0197X0", quantity=20, avg_price=10_000.0)

    # Force reconcile_position to keep reporting a nonzero residual regardless of the real sell.
    monkeypatch.setattr(broker, "reconcile_position", lambda symbol: 5)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-9",
        quotes={"0193T0": 15_000.0, "0197X0": 10_000.0}, position=position, budget=10_000_000.0,
        reconcile_retries=2, reconcile_delay_sec=0.0,
    )
    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_SELL_NOT_CONFIRMED
    assert broker.get_position("0193T0") is None


def test_execute_exit_records_stop_loss_and_no_buy():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 14_000.0})
    broker.set_quote("0193T0", 15_000.0)
    broker.buy_market("0193T0", 10, "seed")
    broker.set_quote("0193T0", 14_000.0)  # price dropped -> loss

    from app.trading.macd2 import config

    outcome = order_executor.execute_exit(
        broker=broker, symbol="0193T0", quantity=10,
        exit_reason=config.EXIT_STOP_LOSS, entry_price=15_000.0,
    )
    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.block_reason == config.EXIT_STOP_LOSS
    assert broker.get_position("0193T0") is None

    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["side"] == "SELL"
    assert rows[0]["exit_reason"] == config.EXIT_STOP_LOSS
    assert float(rows[0]["net_pnl"]) < 0
