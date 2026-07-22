"""Tests for MACD Hynix daily ledger aggregation."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.trading import macd_hynix_order_manager as om
from app.trading.macd_hynix_ledger import summarize_daily_trading


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    ledger_path = tmp_path / "macd_hynix_execution_ledger.csv"
    state_path = tmp_path / "macd_hynix_state.json"
    monkeypatch.setattr(om, "LEDGER_PATH", ledger_path)
    monkeypatch.setattr(om, "STATE_PATH", state_path)
    monkeypatch.setattr(om, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    yield


def _append_row(**kwargs):
    row = {col: "" for col in om.LEDGER_COLUMNS}
    row.update(kwargs)
    om._append_ledger(row)


def test_summarize_daily_empty():
    summary = summarize_daily_trading(trading_date="2026-07-22", budget=10_000_000)
    assert summary["has_data"] is False
    assert summary["round_trip_count"] == 0
    assert summary["total_cost"] == 0.0
    assert summary["return_pct"] == 0.0


def test_summarize_daily_round_trip_profit_and_costs():
    today = "2026-07-22T10:15:00"
    _append_row(
        timestamp=today,
        action="BUY",
        success=True,
        executed_qty=10,
        gross_pnl=0,
        cost=1500,
        net_pnl=-1500,
    )
    _append_row(
        timestamp="2026-07-22T11:00:00",
        action="SELL",
        success=True,
        executed_qty=10,
        gross_pnl=50000,
        cost=2500,
        net_pnl=47500,
    )
    _append_row(
        timestamp="2026-07-21T15:00:00",
        action="SELL",
        success=True,
        executed_qty=5,
        gross_pnl=1000,
        cost=100,
        net_pnl=900,
    )

    summary = summarize_daily_trading(trading_date="20260722", budget=10_000_000)
    assert summary["has_data"] is True
    assert summary["round_trip_count"] == 1
    assert summary["buy_fill_count"] == 1
    assert summary["sell_fill_count"] == 1
    assert summary["total_cost"] == 4000.0
    assert summary["profit_amount"] == 47500.0
    assert summary["loss_amount"] == 1500.0
    assert summary["net_pnl"] == 46000.0
    assert summary["return_pct"] == pytest.approx(0.46, rel=1e-4)


def test_summarize_daily_loss_only():
    _append_row(
        timestamp=datetime(2026, 7, 22, 14, 0, 0).isoformat(),
        action="BUY",
        success=True,
        executed_qty=3,
        gross_pnl=0,
        cost=800,
        net_pnl=-800,
    )
    _append_row(
        timestamp=datetime(2026, 7, 22, 14, 30, 0).isoformat(),
        action="SELL",
        success=True,
        executed_qty=3,
        gross_pnl=-12000,
        cost=900,
        net_pnl=-12900,
    )

    summary = summarize_daily_trading(trading_date="2026-07-22", budget=5_000_000)
    assert summary["profit_amount"] == 0.0
    assert summary["loss_amount"] == 13700.0
    assert summary["net_pnl"] == -13700.0
    assert summary["return_pct"] == pytest.approx(-0.274, rel=1e-4)
