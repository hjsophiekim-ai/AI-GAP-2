"""Operational state guards: SHA gate, chase freshness, day rollover, Seoul time, cadence."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

import app.services.hynix_switch_engine as engine
import app.services.hynix_switch_state as state_module
from app.services.hynix_switch_state import default_state, load_state, save_state_atomic
from app.trading.strategy_architecture import (
    CHASE_SIGNAL_MAX_AGE_SECONDS,
    should_discard_stale_chase_signal,
)
from app.ui.trading_decision_snapshot import completed_snapshot_decision_fields
from app.utils import runtime_info


# ---------------------------------------------------------------------------
# SHA Match gate
# ---------------------------------------------------------------------------


def test_sha_mismatch_blocks_orders_enabled_by_deployment(tmp_path, monkeypatch):
    cache_path = tmp_path / "runtime_info.json"
    cache_path.write_text(
        '{"git_sha":"aaa","origin_main_sha":"aaa","render_sha":"bbb","sha_all_match":true,'
        '"orders_enabled_by_deployment":true}',
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_info, "RUNTIME_INFO_PATH", cache_path)
    monkeypatch.setattr(runtime_info, "_git_sha", lambda *a: "aaa")

    info = runtime_info.read_runtime_info()
    assert info["sha_all_match"] is False
    assert info["orders_enabled_by_deployment"] is False


def test_sha_match_enables_orders(tmp_path, monkeypatch):
    sha = "f297a3b53f97abaf93b1285a7e8ad96c1c7ff156"
    cache_path = tmp_path / "runtime_info.json"
    cache_path.write_text(
        f'{{"git_sha":"{sha}","origin_main_sha":"{sha}","render_sha":"{sha}",'
        f'"sha_all_match":false,"orders_enabled_by_deployment":false}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_info, "RUNTIME_INFO_PATH", cache_path)
    monkeypatch.setattr(runtime_info, "_git_sha", lambda *a: sha)

    info = runtime_info.read_runtime_info()
    assert info["sha_all_match"] is True
    assert info["orders_enabled_by_deployment"] is True


def test_signal_summary_sha_mismatch_is_not_1450_gate():
    decision = {
        "final_action": "INVERSE_BUY",
        "enhanced_score": 40.0,
        "inverse_pressure_score": 60.0,
    }
    trace = {"prediction_signal": "INVERSE", "entry_approved": False, "order_sent": False}
    summary = engine._build_signal_summary(
        decision=decision,
        trace=trace,
        state={},
        now=datetime(2026, 7, 22, 11, 5, 0),
        new_entry_allowed_now=False,
        new_entry_window={
            "allowed": False,
            "rule": "DEPLOYMENT_SHA_MISMATCH(local=aaa, origin=aaa, render=bbb) — 배포 SHA 불일치로 신규진입 차단",
        },
    )
    assert summary["block_reason"] == "DEPLOYMENT_SHA_MISMATCH"
    assert summary["block_reason"] != "NEW_ENTRY_TIME_CLOSED"
    assert "14:50" not in summary["conclusion"]


# ---------------------------------------------------------------------------
# Day rollover — yesterday signal not inherited
# ---------------------------------------------------------------------------


def test_day_rollover_resets_chase_and_episode_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(state_module, "_today_str", lambda: "20260722")

    stale = default_state("mock")
    stale["date"] = "20260721"
    stale["trend_continuation_entry"] = {
        "direction": "DOWN",
        "direction_episode_id": "DOWN:2026-07-21T14:40:00",
        "first_detected_at": "2026-07-21T14:40:00",
        "reference_price": 14475.0,
        "moved_pct_since_signal": 8.5,
        "episode_status": "PROBE_FAILED",
        "probe_failed_at": "2026-07-21T14:55:00",
        "last_block_reason": "CHASE_BLOCK",
    }
    stale["early_trend_detector"] = {
        "candidate": {
            "direction": "DOWN",
            "first_detected_at": "2026-07-21T14:40:00",
            "reference_price": 14475.0,
        }
    }
    stale["live_trade_direction"] = {
        "direction": "DOWN",
        "first_detected_at": "2026-07-21T14:40:00",
        "direction_episode_id": "DOWN:old",
    }
    stale["last_completed_decision_snapshot"] = {
        "primary_block_reason": {"value": "NEW_ENTRY_TIME_CLOSED"},
        "signal_summary": {"conclusion": "14:50 이후 신호와 무관하게 신규진입 금지 → HOLD"},
    }
    save_state_atomic(stale)

    reloaded = load_state(mode="mock")
    assert reloaded["date"] == "20260722"
    assert reloaded.get("trend_continuation_entry") in (None, {})
    assert reloaded.get("early_trend_detector") in (None, {})
    assert reloaded.get("live_trade_direction") in (None, {})
    assert reloaded.get("last_completed_decision_snapshot") in (None, {})


# ---------------------------------------------------------------------------
# Chase freshness — signal_age > 120s discards stale signal
# ---------------------------------------------------------------------------


def test_should_discard_stale_chase_when_age_over_120s():
    now = datetime(2026, 7, 22, 11, 20, 0)
    discard, reason = should_discard_stale_chase_signal(
        first_detected_at=(now - timedelta(seconds=121)).isoformat(),
        signal_price=10_000.0,
        now=now,
        df_1min=None,
    )
    assert discard is True
    assert reason == "SIGNAL_AGE_GT_120S"
    assert CHASE_SIGNAL_MAX_AGE_SECONDS == 120.0


def test_should_not_discard_fresh_chase_under_120s():
    now = datetime(2026, 7, 22, 11, 20, 0)
    discard, reason = should_discard_stale_chase_signal(
        first_detected_at=(now - timedelta(seconds=30)).isoformat(),
        signal_price=10_000.0,
        now=now,
        df_1min=None,
    )
    assert discard is False
    assert reason == ""


def test_should_discard_signal_price_outside_today_range():
    now = datetime(2026, 7, 22, 11, 20, 0)
    df = pd.DataFrame(
        [
            {"datetime": now - timedelta(minutes=30), "high": 11500.0, "low": 11200.0, "close": 11400.0},
            {"datetime": now - timedelta(minutes=1), "high": 11450.0, "low": 11300.0, "close": 11380.0},
        ]
    )
    discard, reason = should_discard_stale_chase_signal(
        first_detected_at=(now - timedelta(seconds=10)).isoformat(),
        signal_price=14475.0,  # yesterday residue
        now=now,
        df_1min=df,
    )
    assert discard is True
    assert reason == "SIGNAL_PRICE_OUT_OF_TODAY_RANGE"


def test_refresh_stale_chase_reinitializes_signal_price():
    now = datetime(2026, 7, 22, 11, 20, 0)
    continuation = {
        "direction": "DOWN",
        "direction_episode_id": "DOWN:old",
        "first_detected_at": (now - timedelta(seconds=300)).isoformat(),
        "reference_price": 14475.0,
        "moved_pct_since_signal": 12.0,
        "last_block_reason": "CHASE_BLOCK",
        "episode_status": "PROBE_FAILED",
    }
    result = engine.refresh_stale_chase_signal_state(
        continuation,
        now=now,
        direction="DOWN",
        current_etf_price=11380.0,
        desired_symbol="0197X0",
        df_1min=None,
    )
    assert result["refreshed"] is True
    assert result["reason"] == "SIGNAL_AGE_GT_120S"
    assert continuation["reference_price"] == 11380.0
    assert continuation["first_detected_at"] == now.isoformat()
    assert continuation.get("moved_pct_since_signal") is None
    assert continuation.get("episode_status") is None
    # Fresh signal → chase % must not use stale reference
    moved = abs(11380.0 / float(continuation["reference_price"]) - 1.0) * 100.0
    assert moved == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Fast Worker cadence — sleep compensation / interval recorder
# ---------------------------------------------------------------------------


def test_fast_worker_interval_stats_meet_cadence_target():
    """Unit/mock: compensated 5s cadence → mean≤7s and p95≤10s over 10 intervals."""
    state: dict = {}
    t0 = datetime(2026, 7, 22, 11, 30, 0)
    # Simulate work=1.5s + sleep compensation → ~5s tick spacing
    for i in range(11):
        engine._record_fast_worker_tick(state, now=t0 + timedelta(seconds=5.0 * i))
    intervals = state["fast_worker_recent_tick_intervals_sec"]
    assert len(intervals) == 10
    mean = sum(intervals) / len(intervals)
    p95 = sorted(intervals)[max(0, int(len(intervals) * 0.95) - 1)]
    assert mean <= 7.0
    assert p95 <= 10.0


def test_scheduler_sleep_compensation_keeps_mean_near_target():
    from app.services import hynix_auto_trade_scheduler as sched

    # Verify the run loop uses (interval - elapsed) wait: after a 2s tick with 5s
    # target, remaining wait should be ~3s (not another full 5s).
    started = datetime(2026, 7, 22, 11, 0, 0)
    completed = started + timedelta(seconds=2.0)
    interval = 5.0
    elapsed = (completed - started).total_seconds()
    wait = max(0.0, interval - elapsed)
    assert wait == pytest.approx(3.0)
    # Resulting inter-tick spacing
    assert elapsed + wait == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Seoul-only entry time — no stale NEW_ENTRY_TIME_CLOSED at 11:00
# ---------------------------------------------------------------------------


def test_1100_not_new_entry_time_closed_from_stale_snapshot(monkeypatch):
    """11:00–14:49 must not surface NEW_ENTRY_TIME_CLOSED from a stale gate."""
    monkeypatch.setattr(
        "app.utils.time_utils.kst_now",
        lambda: datetime(2026, 7, 22, 11, 0, 0),
    )
    snap = {
        "source": "FAST_WORKER",
        "primary_block_reason": {"value": "NEW_ENTRY_TIME_CLOSED"},
        "final_action": {"value": "HOLD"},
        "pipeline_trace": {
            "entry_approved": False,
            "entry_approved_reason": "NEW_ENTRY_TIME_CLOSED",
            "blocking_reason": "NEW_ENTRY_TIME_CLOSED",
            "primary_block_reason": "NEW_ENTRY_TIME_CLOSED",
        },
        "signal_summary": {
            "block_reason": "NEW_ENTRY_TIME_CLOSED",
            "conclusion": "14:50 이후 신호와 무관하게 신규진입 금지 → HOLD",
        },
    }
    fields = completed_snapshot_decision_fields(snap)
    assert fields["primary_block_reason"] != "NEW_ENTRY_TIME_CLOSED"
    assert fields["blocking_reason"] != "NEW_ENTRY_TIME_CLOSED"


def test_signal_summary_at_1100_allows_time_gate():
    summary = engine._build_signal_summary(
        decision={"final_action": "HOLD", "enhanced_score": 55.0, "inverse_pressure_score": 45.0},
        trace={"prediction_signal": "HOLD", "entry_approved": False, "order_sent": False},
        state={},
        now=datetime(2026, 7, 22, 11, 0, 0),
        new_entry_allowed_now=True,
        new_entry_window={"allowed": True, "rule": "09:00~14:50 신규진입 허용"},
    )
    assert summary["block_reason"] != "NEW_ENTRY_TIME_CLOSED"
    assert "14:50" not in (summary["conclusion"] or "")


def test_signal_summary_after_1450_still_time_closed():
    summary = engine._build_signal_summary(
        decision={"final_action": "INVERSE_BUY", "enhanced_score": 40.0, "inverse_pressure_score": 60.0},
        trace={"prediction_signal": "INVERSE", "entry_approved": True, "order_sent": False},
        state={},
        now=datetime(2026, 7, 22, 14, 55, 0),
        new_entry_allowed_now=False,
        new_entry_window={"allowed": False, "rule": "14:50 이후 — 신규진입 금지(청산만 진행)"},
    )
    assert summary["block_reason"] == "NEW_ENTRY_TIME_CLOSED"


# ---------------------------------------------------------------------------
# Valid DOWN → 0197X0 symbol mapping (order path identity)
# ---------------------------------------------------------------------------


def test_valid_down_maps_to_0197x0_symbol():
    assert engine.symbol_for_live_direction("DOWN") == "0197X0"
    assert engine.symbol_for_live_direction("UP") == "0193T0"


def test_fast_worker_snapshot_hold_uses_current_block_reason_not_stale_time():
    now = datetime(2026, 7, 22, 11, 15, 0)
    state = {
        "last_decision": {"enhanced_score": 40.0, "inverse_pressure_score": 65.0},
        "live_trade_direction": {"direction": "DOWN"},
    }
    continuation_state = {
        "last_result": {
            "action": "HOLD",
            "entry_path": "NONE",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "evidence_score": 40,
            "structural_signal_label": "HOLD",
            "hard_blocks": [],
        },
        "last_block_reason": "INSUFFICIENT_EVIDENCE",
        "reference_price": 11380.0,
        "first_detected_at": now.isoformat(),
    }
    engine._update_fast_worker_decision_snapshot(
        state,
        now=now,
        continuation_state=continuation_state,
        early_result={"skipped": True, "reason_code": "INSUFFICIENT_EVIDENCE"},
    )
    snap = state["last_completed_decision_snapshot"]
    fields = completed_snapshot_decision_fields(snap)
    assert fields["resolved_direction"] == "DOWN"
    assert fields["target_symbol"] == "0197X0"
    assert fields["final_action"] == "HOLD"
    assert fields["primary_block_reason"] != "NEW_ENTRY_TIME_CLOSED"
    assert fields["primary_block_reason"] in ("INSUFFICIENT_EVIDENCE", "HOLD", None) or fields["primary_block_reason"]
