"""Unit tests for app.trading.tsla_auto.ledger — isolated to tmp_path via conftest.py."""
from __future__ import annotations

from app.trading.tsla_auto import config, ledger


def _signal_row(signal_id: str, direction: str = "UP_RED", order_result: str = "EXECUTED", **overrides):
    row = {
        "trading_date": "20260730", "completed_bar_at": "104200", "signal_id": signal_id,
        "signal_type": "INITIAL", "direction": direction, "origin": config.ORIGIN_LIVE_CONFIRMED,
        "macd": 1.0, "signal": 0.5, "hist_last3": "(0.1,0.2,0.3)",
        "detected_at_et": "2026-07-30T10:42:05-04:00", "order_result": order_result, "block_reason": "",
        "strategy_name": config.STRATEGY_NAME, "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE, "worker_code_sha": "abc1234",
    }
    row.update(overrides)
    return row


def test_ledger_paths_are_isolated_and_do_not_reference_macd2():
    assert "macd2" not in str(ledger.SIGNAL_LEDGER_PATH).lower()
    assert "macd2" not in str(ledger.EXECUTION_LEDGER_PATH).lower()
    assert ledger.SIGNAL_LEDGER_PATH.name == "tsla_auto_signal_ledger.csv"
    assert ledger.EXECUTION_LEDGER_PATH.name == "tsla_auto_execution_ledger.csv"


def test_append_signal_dedups_by_signal_id():
    ledger.append_signal(_signal_row("sid-1"))
    written_again = ledger.append_signal(_signal_row("sid-1"))
    assert written_again is False
    assert len(ledger.load_signal_ledger()) == 1


def test_append_execution_dedups_by_order_id():
    row = {
        "order_id": "ord-1", "signal_id": "sid-1", "timestamp": "2026-07-30T10:42:05-04:00",
        "mode": "mock", "symbol": config.LONG_SYMBOL, "side": "BUY", "requested_qty": 10, "executed_qty": 10,
        "requested_price": 30.0, "executed_price": 30.0, "position_before": 0, "position_after": 10,
        "gross_pnl_usd": 0.0, "fee_usd": 0.1, "net_pnl_usd": 0.0, "exit_reason": "", "broker_response": "{}",
    }
    assert ledger.append_execution(row) is True
    assert ledger.append_execution(row) is False
    assert len(ledger.load_execution_ledger()) == 1


def test_summarize_signals_excludes_old_worker_sha():
    ledger.append_signal(_signal_row("sid-old-sha", direction="UP_RED", worker_code_sha="0000000"))
    ledger.append_signal(_signal_row("sid-new-sha", direction="DOWN_BLUE", worker_code_sha="1111111"))

    summary = ledger.summarize_signals(
        "20260730", strategy_version=config.STRATEGY_VERSION, signal_rule=config.SIGNAL_RULE, worker_code_sha="1111111",
    )
    assert summary["red_count"] == 0
    assert summary["blue_count"] == 1
    reasons = {r["signal_id"]: r["excluded_reason"] for r in summary["excluded_signals"]}
    assert reasons["sid-old-sha"] == "OLD_WORKER_SHA"


def test_summarize_signals_excludes_historical_replay_only_origin():
    ledger.append_signal(_signal_row("sid-hist", direction="UP_RED", origin=config.ORIGIN_HISTORICAL_REPLAY_ONLY))
    ledger.append_signal(_signal_row("sid-live", direction="DOWN_BLUE", origin=config.ORIGIN_LIVE_CONFIRMED))

    summary = ledger.summarize_signals("20260730", strategy_version=config.STRATEGY_VERSION, signal_rule=config.SIGNAL_RULE)
    assert summary["red_count"] == 0
    assert summary["blue_count"] == 1
    reasons = {r["signal_id"]: r["excluded_reason"] for r in summary["excluded_signals"]}
    assert reasons["sid-hist"] == "HISTORICAL_REPLAY_ONLY"


def test_summarize_signals_excludes_old_strategy_version():
    row = _signal_row("sid-old-strat", direction="UP_RED")
    row["strategy_version"] = "OLD_VERSION"
    ledger.append_signal(row)
    summary = ledger.summarize_signals("20260730", strategy_version=config.STRATEGY_VERSION, signal_rule=config.SIGNAL_RULE)
    assert summary["red_count"] == 0
    reasons = {r["signal_id"]: r["excluded_reason"] for r in summary["excluded_signals"]}
    assert reasons["sid-old-strat"] == "OLD_STRATEGY"


def test_summarize_daily_trading_empty_ledger_never_raises():
    summary = ledger.summarize_daily_trading("20260730", budget_usd=10_000.0)
    assert summary["has_data"] is False
    assert summary["net_pnl_usd"] == 0.0


def test_summarize_daily_trading_cost_breakdown_from_closed_trades():
    ledger.append_execution({
        "order_id": "buy-1", "signal_id": "sid-1", "timestamp": "2026-07-30T10:00:00-04:00",
        "mode": "mock", "symbol": config.LONG_SYMBOL, "side": "BUY", "requested_qty": 10, "executed_qty": 10,
        "requested_price": 30.0, "executed_price": 30.0, "position_before": 0, "position_after": 10,
        "gross_pnl_usd": 0.0, "buy_fee_usd": 0.75, "sell_fee_usd": 0.0, "slippage_usd": 0.15,
        "fx_cost_usd": 0.15, "sec_fee_usd": 0.0, "finra_taf_usd": 0.0, "total_cost_usd": 1.05,
        "fee_usd": 1.05, "net_pnl_usd": 0.0, "exit_reason": "", "broker_response": "{}",
    })
    ledger.append_execution({
        "order_id": "sell-1", "signal_id": "sid-1", "timestamp": "2026-07-30T10:30:00-04:00",
        "mode": "mock", "symbol": config.LONG_SYMBOL, "side": "SELL", "requested_qty": 10, "executed_qty": 10,
        "requested_price": 32.0, "executed_price": 32.0, "position_before": 10, "position_after": 0,
        "gross_pnl_usd": 20.0, "buy_fee_usd": 0.75, "sell_fee_usd": 0.8, "slippage_usd": 0.31,
        "fx_cost_usd": 0.31, "sec_fee_usd": 0.0026, "finra_taf_usd": 0.0017, "total_cost_usd": 2.1743,
        "fee_usd": 2.1743, "net_pnl_usd": 17.8257, "exit_reason": config.EXIT_PROFIT_LOCK,
        "broker_response": "{}",
    })

    summary = ledger.summarize_daily_trading("20260730", budget_usd=1_000.0)
    assert summary["gross_pnl_usd"] == 20.0
    assert summary["total_commission_usd"] == 1.55
    assert summary["total_slippage_usd"] == 0.31
    assert summary["total_fx_cost_usd"] == 0.31
    assert summary["total_cost_usd"] == 2.1743
    assert summary["net_pnl_usd"] == 17.8257
