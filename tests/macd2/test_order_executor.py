"""Unit tests for app.trading.macd2.order_executor — FakeBroker only, no real network."""
from __future__ import annotations

import pytest

from app.trading.macd2 import config, ledger, order_executor
from app.trading.macd2.models import Direction, PositionSnapshot, SignalState
from app.trading.trading_cost_engine import TradeCostEngine
from app.utils.stock_utils import get_tick_size
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


# ── Real fee/tick-size safety margin (docs §9/§21 — replaces the old fixed
# placeholder ratio) ─────────────────────────────────────────────────────────

def test_safety_margin_equals_real_fee_rate_plus_one_tick():
    price = 15_000.0
    margin = order_executor.compute_order_safety_margin_pct(price, config.LONG_SYMBOL)
    expected_fee_pct = TradeCostEngine().fee_rate(config.LONG_SYMBOL, "BUY") * 100.0
    expected_tick_pct = get_tick_size(price) / price * 100.0
    assert margin == pytest.approx(expected_fee_pct + expected_tick_pct)


def test_safety_margin_zero_price_returns_zero():
    assert order_executor.compute_order_safety_margin_pct(0.0, config.LONG_SYMBOL) == 0.0


def test_compute_order_quantity_uses_real_margin_by_default():
    price = 15_000.0
    expected_margin = order_executor.compute_order_safety_margin_pct(price, config.LONG_SYMBOL)
    qty = order_executor.compute_order_quantity(
        available_cash=10_000_000, budget=10_000_000, price=price, symbol=config.LONG_SYMBOL,
    )
    usable = 10_000_000 * (1 - expected_margin / 100.0)
    assert qty == int(usable // price)


@pytest.mark.parametrize("price", [1_500, 4_000, 15_000, 45_000, 150_000, 450_000, 900_000])
@pytest.mark.parametrize("available_cash", [1_000, 50_000, 1_000_000, 10_000_000, 10_000_000_000])
@pytest.mark.parametrize("budget", [1_000, 50_000, 1_000_000, 10_000_000])
def test_order_never_exceeds_budget_or_cash_across_tick_bands(price, available_cash, budget):
    """예산 초과 주문 0건: every KRX tick band x cash/budget combination must
    size an order whose notional never exceeds min(cash, budget)."""
    qty = order_executor.compute_order_quantity(
        available_cash=available_cash, budget=budget, price=price, symbol=config.LONG_SYMBOL,
    )
    notional = qty * price
    usable_cap = min(available_cash, budget)
    assert notional <= usable_cap


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


def test_execute_signal_records_sizing_diagnostics():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-sizing",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
    )
    assert outcome.orderable_cash_at_sizing == 10_000_000.0
    assert outcome.ask1 == 15_000.0
    assert outcome.order_type == "limit"
    assert outcome.ord_dvsn == "00"
    assert outcome.order_price == 15_010.0
    assert outcome.expected_amount <= outcome.usable_cash * 0.995


def test_orderable_cash_smaller_than_budget_shrinks_requested_qty():
    """2026-07-27 fix: budget 9.2M but the REAL (symbol-scoped) orderable
    cash is smaller — the order must size off the smaller real figure, never
    the UI budget, and the resulting notional must never exceed it."""
    price = 15_000.0
    real_orderable_cash = 4_000_000.0
    budget = 9_200_000.0
    broker = FakeBroker(cash=real_orderable_cash, quotes={"0193T0": price})

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-shrink",
        quotes={"0193T0": price}, position=None, budget=budget,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.orderable_cash_at_sizing == real_orderable_cash
    assert outcome.expected_amount <= real_orderable_cash * 0.995
    assert outcome.quantity * price < budget  # sized off cash, not the larger budget


def test_ui_budget_9200000_uses_limit_buyable_qty_cap():
    price = 15_000.0
    broker = FakeBroker(cash=9_254_852.0, quotes={"0193T0": price})
    broker.next_nrcvb_buy_qty = 100

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-kis-qty-cap",
        quotes={"0193T0": price}, position=None, budget=9_200_000.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.budget_qty == int((9_200_000.0 * 0.995) // outcome.order_price)
    assert outcome.nrcvb_buy_qty == 100
    assert outcome.limit_buyable_qty == 100
    assert outcome.final_qty <= 100
    assert outcome.buy_result.requested_qty == outcome.final_qty
    assert broker.orders[-1].requested_qty <= 100


def test_limit_buyable_query_matches_limit_order_type():
    broker = FakeBroker(cash=9_254_852.0, quotes={"0193T0": 15_000.0})

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-limit-ord-dvsn",
        quotes={"0193T0": 15_000.0}, position=None, budget=9_200_000.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert broker.buy_sizing_quotes[-1].order_type == "limit"
    assert broker.buy_sizing_quotes[-1].ord_dvsn == "00"
    assert broker.orders[-1].raw.get("ORD_DVSN") == "00"
    assert outcome.ord_dvsn == "00"


def test_budget_9200000_ask1_11410_sizes_near_800_not_market_cap_480():
    broker = FakeBroker(cash=9_254_852.0, quotes={"0193T0": 11_410.0})
    broker.next_nrcvb_buy_qty = 2_000

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-ask-11410",
        quotes={"0193T0": 11_410.0}, position=None, budget=9_200_000.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.ask1 == 11_410.0
    assert outcome.order_price == 11_420.0
    assert outcome.final_qty == int((9_200_000.0 * 0.995) // 11_420.0)
    assert outcome.final_qty > 780
    assert outcome.final_qty != 480
    assert outcome.expected_amount <= outcome.usable_cash * 0.995


def test_ioc_partial_fill_reflects_only_filled_qty_in_position_and_ledger():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    broker.next_buy_fill_qty = 7

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-ioc-partial",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
        reconcile_retries=1, reconcile_delay_sec=0.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.buy_result.requested_qty > 7
    assert outcome.filled_qty == 7
    assert outcome.quantity == 7
    assert broker.get_position("0193T0").quantity == 7
    rows = ledger.load_execution_ledger()
    assert rows[-1]["requested_qty"] == str(outcome.buy_result.requested_qty)
    assert rows[-1]["executed_qty"] == "7"


def test_ioc_unfilled_keeps_position_zero_and_duplicate_signal_blocks_reorder():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    broker.next_buy_fill_qty = 0

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-ioc-unfilled",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
        reconcile_retries=1, reconcile_delay_sec=0.0,
    )
    order_count = len(broker.orders)
    blocked = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-ioc-unfilled",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
        processed_signal_ids=frozenset({"sig-ioc-unfilled"}),
    )

    assert outcome.final_state == SignalState.FAILED
    assert outcome.filled_qty == 0
    assert broker.get_position("0193T0") is None
    assert blocked.block_reason == order_executor.BLOCK_DUPLICATE_SIGNAL
    assert len(broker.orders) == order_count


def test_final_qty_zero_blocks_before_broker_order():
    broker = FakeBroker(cash=9_254_852.0, quotes={"0193T0": 15_000.0})
    broker.next_nrcvb_buy_qty = 0

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-zero-nrcvb",
        quotes={"0193T0": 15_000.0}, position=None, budget=9_200_000.0,
    )

    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_NRCVB_BUY_QTY_ZERO
    assert outcome.broker_called is False
    assert broker.orders == []


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


def test_switch_target_ask_failure_blocks_after_exhausting_all_retries():
    """2026-08-11 fix: a GENUINELY persistent ask1 failure (every retry
    fails, not just one) must still block -- this is the "real outage"
    case the retry loop is not meant to paper over."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    broker.buy_market("0197X0", 20, "seed")
    broker.fail_ask_count = order_executor.ASK1_FETCH_RETRIES
    position = PositionSnapshot(symbol="0197X0", quantity=20, avg_price=10_000.0)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-switch-ask-fail",
        quotes={"0193T0": 15_000.0, "0197X0": 10_000.0}, position=position, budget=10_000_000.0,
    )

    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_ASK_QUOTE_FAILED
    assert broker.get_position("0197X0").quantity == 20
    assert [(o.side, o.symbol) for o in broker.orders] == [("BUY", "0197X0")]
    assert ledger.load_execution_ledger() == []


def test_switch_target_ask_transient_failure_is_retried_and_succeeds():
    """2026-08-11 fix (real incident: a confirmed REVERSAL never placed
    either leg because get_fresh_ask1() was tried exactly once) -- a
    single transient ask1 failure must be retried and, once it succeeds,
    the switch (sell held ETF + buy the new one) must go through exactly
    as if the first call had never failed."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    broker.buy_market("0197X0", 20, "seed")
    broker.fail_ask_count = 1  # fewer failures than ASK1_FETCH_RETRIES -- must recover
    position = PositionSnapshot(symbol="0197X0", quantity=20, avg_price=10_000.0)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-switch-ask-retry-ok",
        quotes={"0193T0": 15_000.0, "0197X0": 10_000.0}, position=position, budget=10_000_000.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert broker.get_position("0197X0") is None
    assert broker.get_position("0193T0") is not None


def test_insufficient_cash_blocks_qty_lt_1():
    broker = FakeBroker(cash=50.0, quotes={"0193T0": 15_000.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-7",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_NRCVB_BUY_QTY_ZERO
    assert outcome.broker_called is False


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


def test_reversal_sell_settles_to_zero_on_recheck_after_slow_reconcile(monkeypatch):
    """2026-08-13 fix (real MU_MACD incident: a BLUE flag while holding the
    leverage ETF should SELL it then BUY the inverse ETF; the SELL cleared
    at the broker but reconcile lagged past the primary window, aborting
    the whole reversal with block_reason=SELL_NOT_CONFIRMED_QTY_NONZERO --
    no execution-ledger row for the real sell, and the inverse BUY never
    even attempted). execute_exit already had this exact recheck (see
    test_sell_settles_to_zero_on_recheck_after_slow_reconcile above) --
    execute_signal's own reversal-SELL leg did not, until this fix."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    broker.buy_market("0193T0", 20, "seed")  # holding leverage
    position = PositionSnapshot(symbol="0193T0", quantity=20, avg_price=15_000.0)
    real_reconcile = broker.reconcile_position
    call_count = {"n": 0}

    def flaky_reconcile(symbol):
        call_count["n"] += 1
        return 20 if call_count["n"] <= 2 else real_reconcile(symbol)

    monkeypatch.setattr(broker, "reconcile_position", flaky_reconcile)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="sig-reversal",
        quotes={"0193T0": 15_000.0, "0197X0": 10_000.0}, position=position, budget=10_000_000.0,
        reconcile_retries=2, reconcile_delay_sec=0.0,
    )

    assert outcome.final_state == SignalState.EXECUTED  # reversal completed -- BUY leg also ran
    assert outcome.target_symbol == "0197X0"
    rows = ledger.load_execution_ledger()
    sides = {r["symbol"]: r["side"] for r in rows}
    assert sides.get("0193T0") == "SELL"  # the real sell is now recorded
    assert sides.get("0197X0") == "BUY"   # and the follow-up buy actually ran


def test_sell_settles_to_zero_on_recheck_after_slow_reconcile(monkeypatch):
    """2026-08-10 fix: reconcile_position() lagging past the first poll
    window is not the same as a genuine sell failure (KIS mock server lag
    observed directly today) -- one more re-check must still record a
    real, fully-settled exit instead of discarding it."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    broker.buy_market("0197X0", 20, "seed")
    real_reconcile = broker.reconcile_position
    call_count = {"n": 0}

    def flaky_reconcile(symbol):
        call_count["n"] += 1
        return 20 if call_count["n"] <= 2 else real_reconcile(symbol)

    monkeypatch.setattr(broker, "reconcile_position", flaky_reconcile)

    outcome = order_executor.execute_exit(
        broker=broker, symbol="0197X0", quantity=20, exit_reason="STOP_LOSS",
        entry_price=10_000.0, reconcile_retries=2, reconcile_delay_sec=0.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1 and rows[0]["side"] == "SELL"


def test_sell_genuinely_never_confirmed_stays_failed(monkeypatch):
    """The post-timeout recheck must not turn a genuine non-settlement into
    a false positive -- unchanged existing behavior when reconcile never
    clears."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    broker.buy_market("0197X0", 20, "seed")
    monkeypatch.setattr(broker, "reconcile_position", lambda symbol: 20)

    outcome = order_executor.execute_exit(
        broker=broker, symbol="0197X0", quantity=20, exit_reason="STOP_LOSS",
        entry_price=10_000.0, reconcile_retries=2, reconcile_delay_sec=0.0,
    )

    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_SELL_NOT_CONFIRMED
    assert ledger.load_execution_ledger() == []


def test_buy_accepted_but_unfilled_never_recorded_as_executed():
    """주문 접수 성공 != 체결 성공: broker.buy_market() returns success=True but
    the account never actually shows the position (order accepted, 0 filled)."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    broker.next_buy_fill_qty = 0  # accepted, but reconciles to 0 filled

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-unfilled",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
        reconcile_retries=2, reconcile_delay_sec=0.0,
    )
    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_BUY_NOT_CONFIRMED
    assert outcome.order_failure_stage == order_executor.FILL_TIMEOUT
    assert outcome.filled_qty == 0
    assert outcome.fill_poll_result == "TIMEOUT"
    assert outcome.balance_qty == 0
    assert outcome.cancel_called is True
    assert broker.get_position("0193T0") is None
    assert ledger.load_execution_ledger() == []  # never recorded as a confirmed leg


def test_buy_fills_anyway_after_cancel_is_still_recorded():
    """2026-08-10 fix (real incident): cancel_order() reports success, but
    the order was actually filling right through the cancel attempt -- the
    ledger must still record the real fill instead of silently discarding
    it (previously: FAILED, zero ledger rows, position only rediscovered
    much later via reconcile_position_state with no BUY trace at all)."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    broker.next_buy_fill_qty = 0  # nothing visible during the poll window
    broker.fill_on_next_cancel_qty = 300
    broker.fill_on_next_cancel_price = 15_000.0

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-fills-after-cancel",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
        reconcile_retries=2, reconcile_delay_sec=0.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.filled_qty == 300
    assert broker.get_position("0193T0").quantity == 300
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY" and int(rows[0]["executed_qty"]) == 300


def test_buy_still_genuinely_unfilled_after_cancel_recheck_stays_failed():
    """The post-cancel recheck must not turn a genuine non-fill into a
    false positive -- unchanged existing behavior when nothing ever fills."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    broker.next_buy_fill_qty = 0

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-genuinely-unfilled",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
        reconcile_retries=2, reconcile_delay_sec=0.0,
    )

    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_BUY_NOT_CONFIRMED
    assert broker.get_position("0193T0") is None
    assert ledger.load_execution_ledger() == []


def test_buy_rejected_is_classified_order_rejected_with_kis_fields():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    broker.fail_next_buy = True

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-rejected",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
    )

    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_BUY
    assert outcome.order_failure_stage == order_executor.ORDER_REJECTED
    assert outcome.buy_result.raw["rt_cd"] == "1"
    assert outcome.buy_result.raw["msg_cd"] == "FAKE_REJECT"
    assert outcome.buy_result.raw["msg1"] == "fake rejected"
    assert outcome.fill_poll_result == "NOT_POLLED_ORDER_REJECTED"


def test_buy_success_without_order_id_is_classified_no_order_id():
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    broker.next_buy_order_id = ""

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-no-order-id",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
    )

    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_BUY
    assert outcome.order_failure_stage == order_executor.NO_ORDER_ID
    assert outcome.buy_result.success is True
    assert outcome.buy_result.order_id == ""
    assert outcome.fill_poll_result == "NOT_POLLED_NO_ORDER_ID"
    assert ledger.load_execution_ledger() == []


def test_buy_partial_fill_records_real_filled_qty_not_requested():
    """부분체결: broker fills fewer shares than requested — the recorded qty
    and resulting state must reflect the REAL fill, not the request."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0})
    broker.next_buy_fill_qty = 3  # cap the actual fill below whatever qty is requested

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-partial",
        quotes={"0193T0": 15_000.0}, position=None, budget=10_000_000.0,
        reconcile_retries=2, reconcile_delay_sec=0.0,
    )
    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.order_failure_stage == order_executor.BALANCE_MISMATCH
    assert outcome.fill_poll_result == "FILLED"
    assert outcome.balance_qty == 3
    assert outcome.quantity == 3
    assert broker.get_position("0193T0").quantity == 3

    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert int(rows[0]["executed_qty"]) == 3
    assert int(rows[0]["requested_qty"]) > 3


def test_switch_sell_succeeds_buy_fails_outcome_carries_flat_sell_state():
    """docs: 스위칭 부분실패 — SELL clears to 0 but the follow-up BUY then
    fails outright. The outcome must expose enough for the caller
    (worker._apply_switch_outcome) to know the account is really flat."""
    broker = FakeBroker(cash=10_000_000.0, quotes={"0193T0": 15_000.0, "0197X0": 10_000.0})
    broker.buy_market("0197X0", 20, "seed")
    broker.fail_next_buy = True
    position = PositionSnapshot(symbol="0197X0", quantity=20, avg_price=10_000.0)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-switch-fail",
        quotes={"0193T0": 15_000.0, "0197X0": 10_000.0}, position=position, budget=10_000_000.0,
    )
    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_BUY
    assert outcome.sell_result is not None and outcome.sell_result.success is True
    assert outcome.sell_qty_after == 0
    assert broker.get_position("0197X0") is None
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
