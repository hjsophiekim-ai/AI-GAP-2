"""Render regression test for the MACD 자동매매2 Streamlit page.

Uses streamlit.testing.v1.AppTest against the real page file. All MACD2
state/ledger paths are isolated to tmp_path via tests/macd2/conftest.py's
autouse fixtures — this test never touches real data/ paths, never calls
real KIS, and never starts a real background Worker (the page only ever
calls service.get_snapshot()/service.start()/service.stop(); we don't click
"시작" here, so no broker/market-data construction is attempted at all).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import config, ledger

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")


def _fresh_app() -> AppTest:
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["app_auth_authenticated"] = True
    return at


def test_page_renders_with_no_ledger():
    at = _fresh_app()
    at.run()
    assert not at.exception
    assert any("MACD 자동매매2" in t.value for t in at.title)


def test_page_renders_with_empty_ledger():
    ledger.ensure_paths()
    ledger.SIGNAL_LEDGER_PATH.write_text(",".join(ledger.SIGNAL_LEDGER_COLUMNS) + "\n", encoding="utf-8")
    ledger.EXECUTION_LEDGER_PATH.write_text(",".join(ledger.EXECUTION_LEDGER_COLUMNS) + "\n", encoding="utf-8")

    at = _fresh_app()
    at.run()
    assert not at.exception


def test_page_renders_with_populated_ledger():
    ledger.append_execution({
        "order_id": "ORD-1", "signal_id": "sid-1", "timestamp": "20260106T090305",
        "mode": "mock", "symbol": "0193T0", "side": "BUY", "requested_qty": 10, "executed_qty": 10,
        "requested_price": 15000.0, "executed_price": 15000.0, "position_before": 0, "position_after": 10,
        "gross_pnl": 0.0, "fee": 100.0, "slippage": 0.0, "net_pnl": 0.0, "exit_reason": "",
        "broker_response": "{}",
    })
    ledger.append_signal({
        "trading_date": "20260106", "completed_bar_at": "090300", "signal_id": "sid-1",
        "signal_type": "INITIAL", "direction": "UP_RED", "macd": 1.0, "signal": 0.5,
        "hist_last3": "(0.1,0.2,0.3)", "detected_at": "2026-01-06T09:03:05+09:00",
        "order_requested_at": "2026-01-06T09:03:05+09:00", "order_result": "EXECUTED", "block_reason": "",
    })

    at = _fresh_app()
    at.run()
    assert not at.exception
    metric_values = " ".join(str(m.value) for m in at.metric)
    assert metric_values  # at least some metrics rendered


def test_daily_stats_show_flag_times_and_order_status():
    trading_date = "20260724"
    for row in (
        {
            "trading_date": trading_date,
            "completed_bar_at": "092400",
            "forming_bar_start": "2026-07-24T09:24:00+09:00",
            "signal_id": "20260724_092400_UP_RED_PROVISIONAL",
            "signal_type": "INITIAL",
            "direction": "UP_RED",
            "detected_at": "2026-07-24T09:24:02+09:00",
            "order_requested_at": "2026-07-24T09:24:03+09:00",
            "order_result": "EXECUTED",
            "block_reason": "",
            "strategy_name": "MACD2",
            "strategy_version": config.STRATEGY_VERSION,
            "signal_rule": config.SIGNAL_RULE,
        },
        {
            "trading_date": trading_date,
            "completed_bar_at": "143300",
            "forming_bar_start": "2026-07-24T14:33:00+09:00",
            "signal_id": "20260724_143300_DOWN_BLUE_PROVISIONAL",
            "signal_type": "INITIAL",
            "direction": "DOWN_BLUE",
            "detected_at": "2026-07-24T14:33:04+09:00",
            "order_requested_at": "",
            "order_result": "BLOCKED",
            "block_reason": "QUOTE_STALE",
            "strategy_name": "MACD2",
            "strategy_version": config.STRATEGY_VERSION,
            "signal_rule": config.SIGNAL_RULE,
        },
    ):
        ledger.append_signal(row)

    at = _fresh_app()
    at.run()
    assert not at.exception
    metric_labels = [m.label for m in at.metric]
    assert "오늘 빨간 플래그" in metric_labels
    assert "오늘 파란 플래그" in metric_labels
    captions = "\n".join(str(c.value) for c in at.caption)
    assert "빨간 플래그 1건: 09:24:00" in captions
    assert "파란 플래그 1건: 14:33:00" in captions
    frames = [df.value for df in at.dataframe if getattr(df, "value", None) is not None]
    flag_frame = next(frame for frame in frames if "not_ordered_reason" in frame.columns)
    assert "order_requested" in flag_frame.columns
    assert "not_ordered_reason" in flag_frame.columns
    assert "QUOTE_STALE" in set(flag_frame["not_ordered_reason"])


def test_start_stop_buttons_render():
    at = _fresh_app()
    at.run()
    assert not at.exception
    labels = [b.label for b in at.button]
    assert "자동매매 시작" in labels
    assert "자동매매 중지" in labels
    assert "Bootstrap 재시도" in labels


def test_operational_diagnostics_panel_renders_before_start():
    """Worker/quote/bootstrap heartbeat diagnostics (docs §21 2026-07-24 UI
    addition) must render even with no Worker ever started — worker_status
    must read STOPPED (auto_trade_on is False), never crash on missing
    worker_stats fields."""
    at = _fresh_app()
    at.run()
    assert not at.exception
    assert any("운영 진단" in h.value for h in at.subheader)
    metric_labels = [m.label for m in at.metric]
    for expected in (
        "worker_status", "quote_updater_status", "active_worker_count",
        "worker_instance_id", "worker_started_at", "worker_code_sha", "tick_seq_total",
        "recent_tick_sample_count", "last_tick_at", "last_tick_age_sec", "next_tick_at",
        "bootstrap_last_attempt_at", "bootstrap_retry_count", "received_1m_bars",
        "completed_3m_bars", "warmup_ready",
    ):
        assert expected in metric_labels, f"missing diagnostic metric: {expected}"
    worker_status_metric = next(m for m in at.metric if m.label == "worker_status")
    assert worker_status_metric.value == "STOPPED"
