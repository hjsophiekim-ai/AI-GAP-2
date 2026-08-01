"""Unit tests for app.trading.tsla_auto.order_executor — FakeBroker only."""
from __future__ import annotations

from datetime import datetime

from app.trading.tsla_auto import config, ledger, market_session, order_executor
from app.trading.tsla_auto.models import Direction, PositionSnapshot, SignalState
from tests.tsla_auto.fake_broker import FakeBroker


def _regular_market_state():
    return market_session.get_us_market_state(datetime(2026, 8, 3, 10, 0, tzinfo=market_session.ET))


def test_compute_limit_buy_quantity_uses_smaller_of_budget_and_available():
    usable, budget_qty, final_qty, notional = order_executor.compute_limit_buy_quantity(
        ui_budget_usd=10_000.0, available_usd=5_000.0, order_price=30.0, available_qty=1000,
    )
    assert usable == 5_000.0
    assert budget_qty == int(5_000.0 * config.ORDER_USAGE_RATIO // 30.0)
    assert final_qty == budget_qty
    assert notional <= usable * config.ORDER_USAGE_RATIO + 1e-6


def test_compute_limit_buy_quantity_caps_at_available_qty():
    usable, budget_qty, final_qty, notional = order_executor.compute_limit_buy_quantity(
        ui_budget_usd=100_000.0, available_usd=100_000.0, order_price=10.0, available_qty=50,
    )
    assert final_qty == 50
    assert budget_qty > 50


def test_flat_entry_up_red_buys_tsll():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-1", quotes={config.LONG_SYMBOL: 30.0},
        position=None, budget_usd=10_000.0, market_state=_regular_market_state(),
    )
    assert outcome.final_state == SignalState.EXECUTED
    assert broker.orders[-1].symbol == config.LONG_SYMBOL
    assert broker.orders[-1].side == "BUY"
    assert outcome.filled_qty > 0
    assert ledger.load_execution_ledger()[0]["side"] == "BUY"


def test_flat_entry_down_blue_buys_tslz():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.INVERSE_SYMBOL: 12.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="sig-2", quotes={config.INVERSE_SYMBOL: 12.0},
        position=None, budget_usd=10_000.0, market_state=_regular_market_state(),
    )
    assert outcome.final_state == SignalState.EXECUTED
    assert broker.orders[-1].symbol == config.INVERSE_SYMBOL


def test_duplicate_signal_id_blocked_without_broker_call():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-dup", quotes={config.LONG_SYMBOL: 30.0},
        position=None, budget_usd=10_000.0, processed_signal_ids=frozenset({"sig-dup"}), market_state=_regular_market_state(),
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_DUPLICATE_SIGNAL
    assert broker.orders == []


def test_forbidden_symbol_never_reaches_broker(monkeypatch):
    """docs §4/§3 — a forbidden/legacy ticker (e.g. TSLT) must never be
    passed to the broker, even if target_symbol_for_direction is somehow
    monkeypatched to return it."""
    monkeypatch.setattr(order_executor, "target_symbol_for_direction", lambda direction: "TSLT")
    broker = FakeBroker(cash_usd=10_000.0, quotes={"TSLT": 30.0})
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-tslt", quotes={"TSLT": 30.0},
        position=None, budget_usd=10_000.0, market_state=_regular_market_state(),
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_NOT_TRADABLE_DIRECTION
    assert broker.orders == []


def test_already_holding_same_direction_blocks_without_broker_call():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0})
    position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=28.0)
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-3", quotes={config.LONG_SYMBOL: 30.0},
        position=position, budget_usd=10_000.0, market_state=_regular_market_state(),
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_ALREADY_HOLDING
    assert broker.orders == []


def test_opposite_switch_sells_before_buying():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    broker._positions[config.LONG_SYMBOL] = broker._positions.get(config.LONG_SYMBOL)
    # seed an existing TSLL position via a real buy first
    broker.buy_limit(config.LONG_SYMBOL, 10, 30.0, "seed")
    position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=30.0)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="sig-4", quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0},
        position=position, budget_usd=10_000.0, strategy_owned_qty=10, market_state=_regular_market_state(),
    )
    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.target_symbol == config.INVERSE_SYMBOL
    sides = [o.side for o in broker.orders]
    assert sides == ["BUY", "SELL", "BUY"]  # seed buy, then sell-then-buy switch
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert broker.get_position(config.INVERSE_SYMBOL) is not None


def test_strategy_ownership_mismatch_blocks_switch_before_sell():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    broker.buy_limit(config.LONG_SYMBOL, 10, 30.0, "seed")
    position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=30.0)

    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="sig-own", quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0},
        position=position, budget_usd=10_000.0, strategy_owned_qty=0, market_state=_regular_market_state(),
    )

    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.STRATEGY_OWNERSHIP_MISMATCH
    assert [o.side for o in broker.orders] == ["BUY"]


def test_never_holds_both_tsll_and_tslz_simultaneously():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    broker.buy_limit(config.LONG_SYMBOL, 10, 30.0, "seed")
    position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=30.0)
    order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="sig-5", quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0},
        position=position, budget_usd=10_000.0, strategy_owned_qty=10, market_state=_regular_market_state(),
    )
    held = [s for s in (config.LONG_SYMBOL, config.INVERSE_SYMBOL) if broker.get_position(s) is not None]
    assert len(held) <= 1


def test_ask_quote_stale_blocks_without_market_order_fallback():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0})
    broker.fail_next_ask = True
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-6", quotes={config.LONG_SYMBOL: 30.0},
        position=None, budget_usd=10_000.0, market_state=_regular_market_state(),
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == order_executor.BLOCK_ASK_QUOTE_FAILED
    assert broker.orders == []


def test_partial_fill_reflects_only_filled_qty_and_unfilled_qty_is_cancelled():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0})
    broker.next_buy_fill_qty = 3
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-7", quotes={config.LONG_SYMBOL: 30.0},
        position=None, budget_usd=10_000.0, reconcile_retries=2, reconcile_delay_sec=0.0, market_state=_regular_market_state(),
    )
    assert outcome.filled_qty == 3
    assert outcome.quantity == 3
    assert outcome.cancel_called is True
    assert broker.get_position(config.LONG_SYMBOL).quantity == 3


def test_buy_rejected_never_creates_position_without_order_id():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0})
    broker.fail_next_buy = True
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="sig-8", quotes={config.LONG_SYMBOL: 30.0},
        position=None, budget_usd=10_000.0, market_state=_regular_market_state(),
    )
    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_BUY
    assert broker.get_position(config.LONG_SYMBOL) is None


def test_market_phase_blocks_flat_buy_in_final_executor_gate():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0})
    for state in [
        market_session.get_us_market_state(datetime(2026, 8, 3, 8, 0, tzinfo=market_session.ET)),
        market_session.get_us_market_state(datetime(2026, 8, 3, 15, 45, tzinfo=market_session.ET)),
        market_session.get_us_market_state(datetime(2026, 8, 3, 15, 50, tzinfo=market_session.ET)),
        market_session.get_us_market_state(datetime(2026, 8, 3, 17, 0, tzinfo=market_session.ET)),
        market_session.get_us_market_state(datetime(2026, 1, 1, 12, 0, tzinfo=market_session.ET)),
    ]:
        outcome = order_executor.execute_signal(
            broker=broker, direction=Direction.UP_RED, signal_id=f"sig-block-{state.phase.value}",
            quotes={config.LONG_SYMBOL: 30.0}, position=None, budget_usd=10_000.0, market_state=state,
        )
        assert outcome.final_state == SignalState.BLOCKED
        assert outcome.block_reason == state.reason_code
    assert [o.side for o in broker.orders] == []


def test_entry_blocked_reversal_sells_existing_but_blocks_followup_buy():
    broker = FakeBroker(cash_usd=10_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    broker.buy_limit(config.LONG_SYMBOL, 10, 30.0, "seed")
    position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=30.0)
    blocked = market_session.get_us_market_state(datetime(2026, 8, 3, 15, 45, tzinfo=market_session.ET))
    outcome = order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="sig-reversal-blocked",
        quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0}, position=position,
        budget_usd=10_000.0, strategy_owned_qty=10, market_state=blocked,
    )
    assert outcome.final_state == SignalState.BLOCKED
    assert outcome.block_reason == market_session.MARKET_ENTRY_CUTOFF_BLOCK
    assert [o.side for o in broker.orders] == ["BUY", "SELL"]
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert broker.get_position(config.INVERSE_SYMBOL) is None
