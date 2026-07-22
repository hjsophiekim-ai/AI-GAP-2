"""Unit tests for MACD order-latency math helpers and stamp helpers."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading import macd_hynix_order_manager as om
from app.trading.macd_hynix_ledger import (
    aggregate_latency_values,
    compute_latency_segments,
    evaluate_latency_verdict,
    percentile,
    seconds_between,
    summarize_order_latency,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    ledger_path = tmp_path / "macd_hynix_execution_ledger.csv"
    state_path = tmp_path / "macd_hynix_state.json"
    monkeypatch.setattr(om, "LEDGER_PATH", ledger_path)
    monkeypatch.setattr(om, "STATE_PATH", state_path)
    monkeypatch.setattr(om, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    yield


def test_seconds_between_and_percentile():
    t0 = datetime(2026, 7, 23, 10, 0, 0)
    t1 = t0 + timedelta(seconds=4.5)
    assert seconds_between(t0.isoformat(), t1.isoformat()) == 4.5
    assert seconds_between(None, t1.isoformat()) is None
    assert percentile([1, 2, 3, 4, 5], 0.95) == 5.0
    assert percentile([], 0.95) is None


def test_aggregate_latency_values_over_10s():
    stats = aggregate_latency_values([1.0, 2.0, 11.0, 3.0, 12.5])
    assert stats["n"] == 5
    assert stats["median"] == 3.0
    assert stats["maximum"] == 12.5
    assert stats["over_10s_count"] == 2
    assert aggregate_latency_values([])["n"] == 0


def test_compute_latency_segments():
    base = datetime(2026, 7, 23, 10, 3, 0)
    event = {
        "completed_3m_bar_at": base.isoformat(),
        "signal_detected_at": (base + timedelta(seconds=2)).isoformat(),
        "order_requested_at": (base + timedelta(seconds=7)).isoformat(),
        "kis_order_accepted_at": (base + timedelta(seconds=8)).isoformat(),
        "broker_executed_at": (base + timedelta(seconds=9)).isoformat(),
        "position_confirmed_at": (base + timedelta(seconds=9.5)).isoformat(),
    }
    segs = compute_latency_segments(event)
    assert segs["bar_complete_to_signal_detect"] == 2.0
    assert segs["signal_detect_to_order_request"] == 5.0
    assert segs["order_request_to_kis_accept"] == 1.0
    assert segs["kis_accept_to_fill_confirm"] == 1.0
    assert segs["signal_detect_to_final_fill"] == 7.5


def test_begin_stamp_finalize_order_latency():
    state = om.default_state()
    bar = "2026-07-23T10:03:00"
    detected = "2026-07-23T10:03:02"
    om.begin_order_latency(
        state,
        signal_id="MACD3M:UP:2026-07-23T10:00:00",
        completed_3m_bar_at=bar,
        signal_detected_at=detected,
    )
    assert state["worker"]["signal_detected_at"] == detected
    assert state["order_latency"]["completed_3m_bar_at"] == bar

    om.stamp_order_latency(state, "order_requested_at", "2026-07-23T10:03:06")
    om.stamp_order_latency(state, "kis_order_accepted_at", "2026-07-23T10:03:07")
    om.stamp_order_latency(state, "broker_executed_at", "2026-07-23T10:03:08")
    om.stamp_order_latency(state, "position_confirmed_at", "2026-07-23T10:03:08")
    final = om.finalize_order_latency(state)
    assert final is not None
    assert final["segments_sec"]["signal_detect_to_order_request"] == 4.0
    assert len(state["order_latency_history"]) == 1


def test_summarize_not_measured_without_samples():
    summary = summarize_order_latency(
        state=om.default_state(),
        tick_intervals=[5.0, 5.1, 4.9],
        main_cycle_3m_wait_count=0,
    )
    assert summary["sample_count"] == 0
    assert summary["verdict"] == "NOT_MEASURED"
    assert summary["main_cycle_3m_wait_count"] == 0
    assert summary["worker_tick"]["mean"] == pytest.approx(5.0, abs=0.05)


def test_summarize_pass_with_fast_samples():
    base = datetime(2026, 7, 23, 11, 0, 0)
    events = []
    for i in range(5):
        t0 = base + timedelta(minutes=i * 3)
        events.append({
            "signal_id": f"SIG-{i}",
            "completed_3m_bar_at": t0.isoformat(),
            "signal_detected_at": (t0 + timedelta(seconds=1)).isoformat(),
            "order_requested_at": (t0 + timedelta(seconds=3)).isoformat(),
            "kis_order_accepted_at": (t0 + timedelta(seconds=4)).isoformat(),
            "broker_executed_at": (t0 + timedelta(seconds=5)).isoformat(),
            "position_confirmed_at": (t0 + timedelta(seconds=5)).isoformat(),
            "segments_sec": compute_latency_segments({
                "completed_3m_bar_at": t0.isoformat(),
                "signal_detected_at": (t0 + timedelta(seconds=1)).isoformat(),
                "order_requested_at": (t0 + timedelta(seconds=3)).isoformat(),
                "kis_order_accepted_at": (t0 + timedelta(seconds=4)).isoformat(),
                "broker_executed_at": (t0 + timedelta(seconds=5)).isoformat(),
                "position_confirmed_at": (t0 + timedelta(seconds=5)).isoformat(),
            }),
        })
    state = om.default_state()
    state["order_latency_history"] = events
    summary = summarize_order_latency(
        state=state,
        tick_intervals=[5.0] * 20,
        main_cycle_3m_wait_count=0,
    )
    assert summary["sample_count"] >= 5
    assert summary["verdict"] == "PASS"
    assert evaluate_latency_verdict(summary) == "PASS"


def test_summarize_fail_slow_signal_to_request():
    t0 = datetime(2026, 7, 23, 12, 0, 0)
    event = {
        "signal_id": "SLOW-1",
        "completed_3m_bar_at": t0.isoformat(),
        "signal_detected_at": t0.isoformat(),
        "order_requested_at": (t0 + timedelta(seconds=20)).isoformat(),
        "kis_order_accepted_at": (t0 + timedelta(seconds=25)).isoformat(),
        "broker_executed_at": (t0 + timedelta(seconds=30)).isoformat(),
        "position_confirmed_at": (t0 + timedelta(seconds=30)).isoformat(),
    }
    event["segments_sec"] = compute_latency_segments(event)
    state = om.default_state()
    state["order_latency_history"] = [event]
    summary = summarize_order_latency(
        state=state,
        tick_intervals=[5.0] * 10,
        main_cycle_3m_wait_count=0,
    )
    assert summary["verdict"] == "FAIL"
