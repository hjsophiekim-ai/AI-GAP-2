"""Unit tests for app.trading.macd2.ledger — isolated to tmp_path via conftest.py."""
from __future__ import annotations

import pytest

from app.trading.macd2 import ledger


def _signal_row(signal_id: str, direction: str = "UP_RED", order_result: str = "EXECUTED", block_reason: str = ""):
    return {
        "trading_date": "20260106", "completed_bar_at": "090300", "signal_id": signal_id,
        "signal_type": "INITIAL", "direction": direction, "macd": 1.0, "signal": 0.5,
        "hist_last3": "(0.1,0.2,0.3)", "detected_at": "2026-01-06T09:03:05+09:00",
        "order_requested_at": "2026-01-06T09:03:05+09:00", "order_result": order_result,
        "block_reason": block_reason,
    }


def _current_signal_row(signal_id: str, direction: str = "UP_RED"):
    row = _signal_row(signal_id, direction=direction)
    row.update({
        "strategy_name": "MACD2",
        "strategy_version": "20260724_MACD_CROSSOVER_V1",
        "signal_rule": "MACD_CROSSOVER",
        "session_started_at": "2026-01-06T09:00:00+09:00",
    })
    return row


def _execution_row(order_id: str, side: str = "BUY", net_pnl: float = 0.0, gross_pnl: float = 0.0, fee: float = 0.0):
    return {
        "order_id": order_id, "signal_id": "sid-1", "timestamp": "20260106T090305",
        "mode": "mock", "symbol": "0193T0", "side": side, "requested_qty": 10, "executed_qty": 10,
        "requested_price": 15000.0, "executed_price": 15000.0, "position_before": 0, "position_after": 10,
        "gross_pnl": gross_pnl, "fee": fee, "slippage": 0.0, "net_pnl": net_pnl, "exit_reason": "",
        "broker_response": "{}",
    }


def test_ledger_paths_are_isolated_and_do_not_reference_v1():
    assert "macd_hynix" not in str(ledger.SIGNAL_LEDGER_PATH)
    assert "macd_hynix" not in str(ledger.EXECUTION_LEDGER_PATH)
    assert ledger.SIGNAL_LEDGER_PATH.name == "macd2_signal_ledger.csv"
    assert ledger.EXECUTION_LEDGER_PATH.name == "macd2_execution_ledger.csv"


def test_append_signal_writes_header_once():
    ledger.append_signal(_signal_row("sid-1"))
    ledger.append_signal(_signal_row("sid-2"))
    content = ledger.SIGNAL_LEDGER_PATH.read_text(encoding="utf-8")
    assert content.count("signal_id") == 1  # header appears exactly once
    rows = ledger.load_signal_ledger()
    assert len(rows) == 2


def test_append_signal_dedupes_by_signal_id():
    assert ledger.append_signal(_signal_row("sid-1")) is True
    assert ledger.append_signal(_signal_row("sid-1")) is False
    assert len(ledger.load_signal_ledger()) == 1


def test_append_signal_requires_signal_id():
    row = _signal_row("sid-1")
    row["signal_id"] = ""
    with pytest.raises(ValueError):
        ledger.append_signal(row)


def test_append_execution_dedupes_by_order_id():
    assert ledger.append_execution(_execution_row("ord-1")) is True
    assert ledger.append_execution(_execution_row("ord-1")) is False
    assert len(ledger.load_execution_ledger()) == 1


def test_summarize_signals_counts_and_unexecuted():
    ledger.append_signal(_signal_row("sid-1", direction="UP_RED", order_result="EXECUTED"))
    ledger.append_signal(_signal_row("sid-2", direction="DOWN_BLUE", order_result="BLOCKED", block_reason="QUOTE_STALE"))
    ledger.append_signal(_signal_row("sid-3", direction="UP_RED", order_result=""))

    summary = ledger.summarize_signals("20260106")
    assert summary["red_count"] == 2
    assert summary["blue_count"] == 1
    assert summary["signal_count"] == 3
    assert len(summary["unexecuted_signals"]) == 2
    reasons = {u["signal_id"]: u["reason"] for u in summary["unexecuted_signals"]}
    assert reasons["sid-2"] == "QUOTE_STALE"


def test_summarize_signals_filters_old_strategy_rows():
    for i in range(7):
        row = _signal_row(f"old-{i}", order_result="BLOCKED", block_reason="ORDER_DATA_INVALID")
        row.update({"strategy_version": "OLD", "signal_rule": "SIGNED_B_LEGACY"})
        ledger.append_signal(row)

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version="20260724_MACD_CROSSOVER_V1",
        signal_rule="MACD_CROSSOVER",
        session_started_at="2026-01-06T09:00:00+09:00",
    )
    assert summary["red_count"] == 0
    assert summary["blue_count"] == 0
    assert summary["signal_count"] == 0
    assert len(summary["excluded_signals"]) == 7


def test_summarize_signals_counts_current_strategy_only_and_latest():
    ledger.append_signal(_signal_row("old", direction="UP_RED"))
    ledger.append_signal(_current_signal_row("cur-red", direction="UP_RED"))
    ledger.append_signal(_current_signal_row("cur-blue", direction="DOWN_BLUE"))

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version="20260724_MACD_CROSSOVER_V1",
        signal_rule="MACD_CROSSOVER",
        session_started_at="2026-01-06T09:00:00+09:00",
    )
    assert summary["red_count"] == 1
    assert summary["blue_count"] == 1
    assert summary["latest_signal_id"] == "cur-blue"


def test_summarize_daily_trading_empty_ledger_does_not_raise():
    summary = ledger.summarize_daily_trading("20260106", budget=10_000_000)
    assert summary["has_data"] is False
    assert summary["round_trip_count"] == 0
    assert summary["net_pnl"] == 0.0


def test_summarize_daily_trading_computes_pnl_and_stats():
    ledger.append_execution(_execution_row("ord-1", side="BUY", net_pnl=0.0, gross_pnl=0.0, fee=100.0))
    ledger.append_execution(_execution_row("ord-2", side="SELL", net_pnl=5000.0, gross_pnl=5200.0, fee=200.0))
    ledger.append_execution(_execution_row("ord-3", side="BUY", net_pnl=0.0, gross_pnl=0.0, fee=100.0))
    ledger.append_execution(_execution_row("ord-4", side="SELL", net_pnl=-2000.0, gross_pnl=-1800.0, fee=200.0))

    summary = ledger.summarize_daily_trading("20260106", budget=10_000_000)
    assert summary["has_data"] is True
    assert summary["buy_count"] == 2
    assert summary["sell_count"] == 2
    assert summary["round_trip_count"] == 2
    assert summary["net_pnl"] == 3000.0
    assert summary["win_rate_pct"] == 50.0
    assert summary["profit_factor"] == pytest.approx(2.5)
    assert summary["max_drawdown"] == 2000.0  # peak 5000 -> trough 3000
