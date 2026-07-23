"""Unit tests for MACD daily flag event counting / missed-order reasons."""
from __future__ import annotations

from app.trading import macd_hynix_order_manager as om
from app.trading.macd_hynix_strategy import DIR_DOWN, DIR_UP


def test_flag_events_unique_vs_held_ticks():
    state = om.default_state()
    sid = "MACD3M:UP_RED:2026-07-23T10:00:00"
    om.record_macd_flag_event(
        state,
        ts="2026-07-23T10:00:05",
        flag=DIR_UP,
        signal_id=sid,
        bar_ts="2026-07-23T10:00:00",
        new_occurrence=True,
        ordered=False,
        block_reason="ORDER_EXECUTION_DISABLED",
    )
    # Held ticks (same signal_id) must not inflate the count.
    for i in range(12):
        om.record_macd_flag_event(
            state,
            ts=f"2026-07-23T10:00:{10 + i:02d}",
            flag=DIR_UP,
            signal_id=sid,
            bar_ts="2026-07-23T10:00:00",
            new_occurrence=False,
            ordered=False,
            block_reason="SAME_DIR_EPISODE_USED:UP_RED",
        )
    summary = om.summarize_macd_flag_events(state)
    assert summary["red_count"] == 1
    assert summary["blue_count"] == 0
    assert summary["event_count"] == 1

    # New signal_id → second red occurrence.
    om.record_macd_flag_event(
        state,
        ts="2026-07-23T11:00:05",
        flag=DIR_UP,
        signal_id="MACD3M:UP_RED:2026-07-23T11:00:00",
        bar_ts="2026-07-23T11:00:00",
        new_occurrence=True,
        ordered=True,
        order_id="OID-1",
    )
    summary = om.summarize_macd_flag_events(state)
    assert summary["red_count"] == 2
    assert len(summary["missed_order_events"]) == 1


def test_blocked_flag_records_reason():
    state = om.default_state()
    state["primary_block_reason"] = "STALE_WORKER"
    state["stale_worker"] = True
    reason = om.resolve_macd_flag_block_reason(
        state,
        {"arm_blocked_reason": None},
    )
    assert reason == "STALE_WORKER"

    om.record_macd_flag_event(
        state,
        ts="2026-07-23T10:03:05",
        flag=DIR_DOWN,
        signal_id="MACD3M:DOWN_BLUE:2026-07-23T10:03:00",
        bar_ts="2026-07-23T10:03:00",
        new_occurrence=True,
        ordered=False,
        block_reason=reason,
    )
    summary = om.summarize_macd_flag_events(state)
    assert summary["blue_count"] == 1
    assert summary["missed_order_events"][0]["block_reason"] == "STALE_WORKER"
    assert summary["missed_order_events"][0]["flag"] == DIR_DOWN

    # Prefer decision_trace.arm_blocked_reason when present.
    assert (
        om.resolve_macd_flag_block_reason(
            state,
            {"arm_blocked_reason": "DUPLICATE_SIGNAL_ID:x"},
        )
        == "DUPLICATE_SIGNAL_ID:x"
    )


def test_day_rollover_clears_flag_events():
    state = om.default_state()
    state["session_date"] = "2026-07-22"
    om.record_macd_flag_event(
        state,
        ts="2026-07-22T14:00:00",
        flag=DIR_UP,
        signal_id="MACD3M:UP_RED:2026-07-22T14:00:00",
        bar_ts="2026-07-22T14:00:00",
        new_occurrence=True,
        ordered=False,
        block_reason="MARKET_CLOSED",
    )
    assert len(state["flag_events_today"]) == 1
    assert om.apply_macd_session_day_rollover(state, session_date="2026-07-23") is True
    assert state["flag_events_today"] == []
    summary = om.summarize_macd_flag_events(state)
    assert summary["red_count"] == 0
    assert summary["blue_count"] == 0
    assert summary["missed_order_events"] == []
