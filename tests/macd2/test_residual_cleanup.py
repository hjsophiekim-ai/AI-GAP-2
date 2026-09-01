"""Regression tests for the 2026-09-01 real incident: a 09:32:48 809-share
leverage (0193T0) take-profit exit sold most of the position but reconciled
with 1 share still held at the broker afterward -- the trade-history UI then
showed only a stray 1-share SELL and the overall exit price/net P&L
aggregation broke.

Fix scope (per user instruction): fill reconciliation (order_executor.py)
and UI aggregation (app/ui/pages/11_MACD_자동매매2.py) ONLY -- TW2/TEG/
TW2 3-SLOT/legacy TP-SL DECISION logic (when/why an exit fires) is untouched
here and not exercised by these tests.
"""
from __future__ import annotations

from app.trading.macd2 import config, ledger, order_executor
from app.trading.macd2.models import SignalState
from app.models import Position
from tests.macd2.fake_broker import FakeBroker

# NOTE: the UI trade-history-table end-to-end check for this same incident
# lives in tests/macd2/test_trade_history_qty_dedup.py (reusing its existing
# `ui_page` fixture) rather than here -- a SECOND independent raw importlib
# exec_module() of the Streamlit page script in this file was found to
# corrupt Streamlit's internal per-thread form/container context tracking
# (StreamlitAPIException: "st.button() can't be used in an st.form()") for
# LATER, unrelated tests/macd2/test_ui_page.py AppTest-based tests run in the
# same pytest session -- a test-infrastructure hazard, not a bug in this
# fix's own logic. Keep this file's own tests to plain order_executor/ledger
# calls only; never add a second ui_page-loading fixture here.


def _seed_over_tracked_position(broker: FakeBroker, symbol: str, true_qty: int, avg_price: float) -> None:
    """Simulates state under-tracking the real holding by a few shares (the
    2026-09-01 incident's actual root shape: the broker genuinely held more
    than the exit decision believed) -- directly seeds the FakeBroker's own
    position rather than going through buy_market, so the discrepancy is
    exact and deterministic."""
    broker._positions[symbol] = Position(
        symbol=symbol, name=symbol, quantity=true_qty, avg_price=avg_price, current_price=avg_price,
    )


def test_809_share_exit_leaves_no_residual_and_sweeps_it_immediately():
    """Today's exact shape: exit decision believes 809 shares are held and
    requests execute_exit(quantity=809), but the broker actually holds 810
    (809 + a 1-share drift) -- after the main sell, reconcile shows 1 share
    still held. The fix must immediately market-sell that 1 share and still
    report the exit as a clean EXECUTED success, with the true full 810
    liquidated and zero shares left anywhere."""
    broker = FakeBroker(cash=100_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    _seed_over_tracked_position(broker, config.LONG_SYMBOL, true_qty=810, avg_price=14_000.0)

    outcome = order_executor.execute_exit(
        broker=broker, symbol=config.LONG_SYMBOL, quantity=809,
        exit_reason=config.EXIT_TW_TP2_FULL, entry_price=14_000.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.residual_cleanup_qty == 1
    assert broker.get_position(config.LONG_SYMBOL) is None  # fully flat, nothing left anywhere

    rows = ledger.load_execution_ledger()
    assert len(rows) == 2, "main leg + residual cleanup leg, both preserved in the raw ledger"
    main_row, residual_row = rows[0], rows[1]
    assert main_row["side"] == "SELL" and int(main_row["executed_qty"]) == 809
    assert int(main_row["position_before"]) == 810 and int(main_row["position_after"]) == 1
    assert residual_row["side"] == "SELL" and int(residual_row["executed_qty"]) == 1
    assert residual_row["source"] == config.RESIDUAL_CLEANUP_SOURCE
    assert int(residual_row["position_before"]) == 1 and int(residual_row["position_after"]) == 0
    assert residual_row["exit_reason"] == f"{config.EXIT_TW_TP2_FULL}_RESIDUAL_CLEANUP"
    # total real quantity sold across both raw rows == the true full holding
    assert int(main_row["executed_qty"]) + int(residual_row["executed_qty"]) == 810


def test_residual_cleanup_never_fires_for_a_large_unexpected_leftover():
    """A genuinely large mismatch (not "dust") must stay a real, surfaced
    failure -- never silently swept. RESIDUAL_CLEANUP_MAX_QTY default is 5;
    a 50-share leftover must still fail exactly like before this fix."""
    broker = FakeBroker(cash=100_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    _seed_over_tracked_position(broker, config.LONG_SYMBOL, true_qty=859, avg_price=14_000.0)

    outcome = order_executor.execute_exit(
        broker=broker, symbol=config.LONG_SYMBOL, quantity=809,
        exit_reason=config.EXIT_TW_TP2_FULL, entry_price=14_000.0,
    )

    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_SELL_NOT_CONFIRMED
    assert outcome.residual_cleanup_qty == 0
    assert broker.get_position(config.LONG_SYMBOL) is not None  # left untouched for manual/real reconcile


def test_residual_cleanup_when_sweep_itself_fails_still_reports_failure():
    """If the follow-up residual sell itself fails, execute_exit must still
    report FAILED (never silently pretend success) -- no ledger row for a
    sweep that didn't actually happen."""
    broker = FakeBroker(cash=100_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    _seed_over_tracked_position(broker, config.LONG_SYMBOL, true_qty=810, avg_price=14_000.0)

    real_sell = broker.sell_market
    calls = {"n": 0}

    def sell_with_second_call_failing(symbol, qty, client_order_id):
        calls["n"] += 1
        if calls["n"] == 2:
            broker.fail_next_sell = True
        return real_sell(symbol, qty, client_order_id)

    broker.sell_market = sell_with_second_call_failing

    outcome = order_executor.execute_exit(
        broker=broker, symbol=config.LONG_SYMBOL, quantity=809,
        exit_reason=config.EXIT_TW_TP2_FULL, entry_price=14_000.0,
    )

    assert outcome.final_state == SignalState.FAILED
    assert outcome.block_reason == order_executor.FAIL_SELL_NOT_CONFIRMED
    assert outcome.residual_cleanup_qty == 0
    rows = ledger.load_execution_ledger()
    assert len(rows) == 0, "no residual leg recorded when the sweep itself never actually filled"


def test_execute_partial_exit_sweeps_a_small_shortfall_too():
    """execute_partial_exit (TP1 ladder) gets the identical treatment: if
    the broker holds slightly MORE than the intended remaining_qty after
    the partial sell, sweep the tiny excess in one extra market sell."""
    broker = FakeBroker(cash=100_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    _seed_over_tracked_position(broker, config.LONG_SYMBOL, true_qty=101, avg_price=14_000.0)

    # Intend to sell 50, keep 50 -- but broker actually holds 101 (1 extra).
    outcome = order_executor.execute_partial_exit(
        broker=broker, symbol=config.LONG_SYMBOL, sell_qty=50, remaining_qty=50,
        exit_reason=config.EXIT_TW_TP1_PARTIAL, entry_price=14_000.0,
    )

    assert outcome.final_state == SignalState.EXECUTED
    assert outcome.residual_cleanup_qty == 1
    assert broker.get_position(config.LONG_SYMBOL).quantity == 50

    rows = ledger.load_execution_ledger()
    assert len(rows) == 2
    assert rows[1]["source"] == config.RESIDUAL_CLEANUP_SOURCE
    assert int(rows[1]["executed_qty"]) == 1


def test_residual_cleanup_is_idempotent_no_repeat_sells_of_the_same_share(monkeypatch):
    """Never sweeps the same residual twice: a second execute_exit-style call
    against an already-flat position must find nothing left to clean up (no
    extra sell_market call, no extra ledger row)."""
    broker = FakeBroker(cash=100_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0})
    _seed_over_tracked_position(broker, config.LONG_SYMBOL, true_qty=810, avg_price=14_000.0)

    outcome = order_executor.execute_exit(
        broker=broker, symbol=config.LONG_SYMBOL, quantity=809,
        exit_reason=config.EXIT_TW_TP2_FULL, entry_price=14_000.0,
    )
    assert outcome.final_state == SignalState.EXECUTED
    sell_calls_after_first_exit = len(broker.orders)

    # A later tick's own reconcile/backfill or a duplicate call against the
    # now-flat position must not find or re-sell anything.
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert order_executor._attempt_residual_cleanup(
        broker=broker, symbol=config.LONG_SYMBOL, residual_qty=1, target_qty=0, entry_price=14_000.0,
    ) is None  # nothing to sell -- FakeBroker.sell_market fails when quantity < requested qty
    assert len(broker.orders) == sell_calls_after_first_exit + 1  # the attempted (failed) sell only, no phantom fill
    assert len(ledger.load_execution_ledger()) == 2  # unchanged -- no new/duplicate row
