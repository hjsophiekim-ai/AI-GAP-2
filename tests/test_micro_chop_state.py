"""
test_micro_chop_state.py — MICRO_CHOP TTL/해제조건/대칭성 검증
(2026-07-21 실측 버그 수정: 하이닉스 강한 상승 중에도 이전 횡보장에서 생성된
MICRO_CHOP 상태가 하루 종일 신규진입을 차단하던 문제).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.trading.early_trend_detector import (
    MICRO_CHOP_STATE_TTL_SECONDS,
    default_micro_chop_state,
    evaluate_micro_chop_release,
    reset_micro_chop_state_if_stale,
    update_micro_chop_state,
    update_vwap_alignment_tracker,
    vwap_alignment_seconds,
)

_BASE = datetime(2026, 7, 21, 10, 0, 0)


def _activate_via_real_chop(now):
    """실제 3개 기준을 충족시켜(방향전환 3회, VWAP교차 3회, 반전청산 2회) MICRO_CHOP을
    진짜로 활성화한다 — activated_at/expires_at을 실제 로직으로 채운 상태를 만든다."""
    state = None
    directions = ["UP", "DOWN", "UP", "DOWN"]
    for i, d in enumerate(directions):
        state = update_micro_chop_state(
            state, direction=d, vwap_crossed=True, reversal_exit=(i < 2), move_efficiency=0.1,
            now=now + timedelta(seconds=i),
        )
    return state


# ── 진짜 박스권은 계속 차단 유지 ─────────────────────────────────────────────

def test_real_chop_activates_and_stays_blocked_without_release_conditions():
    state = _activate_via_real_chop(_BASE)
    assert state["active"] is True
    assert state["criteria_met_count"] >= 3

    release = evaluate_micro_chop_release(
        live_direction=None, live_direction_held_seconds=None, structural_direction=None,
        confirm_window_directions={}, confirm_vwap_aligned_seconds=None,
        new_swing_breakout=False, actionable_signal=None, etf_mutual_confirmed=None,
        data_time_mismatch=False,
    )
    assert release["release"] is False


def test_single_criterion_no_longer_activates_micro_chop():
    """요구사항5 — 4개 기준 중 1개만 충족하면(예: vwap_crosses만 3회) 활성화되지
    않는다(기존에는 OR 1개로 활성화되던 버그)."""
    state = None
    for i in range(4):
        state = update_micro_chop_state(
            state, direction="UP", vwap_crossed=True, reversal_exit=False, move_efficiency=0.9,
            now=_BASE + timedelta(seconds=i),
        )
    assert state["vwap_crosses"] >= 3
    assert state["active"] is False


# ── 해제 조건: structural/live 15초 이상 정렬 ────────────────────────────────

def test_structural_live_alignment_15s_releases_chop():
    release = evaluate_micro_chop_release(
        live_direction="UP", live_direction_held_seconds=16.0, structural_direction="UP",
        confirm_window_directions={}, confirm_vwap_aligned_seconds=None,
        new_swing_breakout=False, actionable_signal=None, etf_mutual_confirmed=None,
    )
    assert release["release"] is True


def test_structural_live_alignment_under_15s_does_not_release():
    release = evaluate_micro_chop_release(
        live_direction="UP", live_direction_held_seconds=5.0, structural_direction="UP",
        confirm_window_directions={}, confirm_vwap_aligned_seconds=None,
        new_swing_breakout=False, actionable_signal=None, etf_mutual_confirmed=None,
    )
    assert release["release"] is False


# ── 해제 조건: 매수 ETF 5/10/20초 모두 방향 일치 ─────────────────────────────

def test_confirm_etf_5_10_20_agreement_releases_chop():
    release = evaluate_micro_chop_release(
        live_direction="UP", confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "DOWN"},
    )
    assert release["release"] is True


# ── 해제 조건: 이미 VWAP 위에서 상승 지속(신규 돌파 이벤트 없어도 해제) ──────

def test_sustained_vwap_alignment_releases_without_new_breakout_event():
    """요구사항3/5 — "이번 틱에 VWAP을 새로 돌파했는지"만 요구하지 않는다.
    신규 돌파 이벤트(new_swing_breakout=False)가 없어도, 15초 이상 VWAP
    정렬 유지만으로 해제돼야 한다."""
    tracker = None
    now = _BASE
    for i in range(4):
        now = _BASE + timedelta(seconds=i * 5)
        tracker = update_vwap_alignment_tracker(tracker, aligned=True, now=now)
    seconds_aligned = vwap_alignment_seconds(tracker, now)
    assert seconds_aligned is not None and seconds_aligned >= 15.0

    release = evaluate_micro_chop_release(
        live_direction="UP", confirm_window_directions={},
        confirm_vwap_aligned_seconds=seconds_aligned, new_swing_breakout=False,
    )
    assert release["release"] is True


def test_vwap_alignment_resets_when_no_longer_aligned():
    tracker = update_vwap_alignment_tracker(None, aligned=True, now=_BASE)
    tracker = update_vwap_alignment_tracker(tracker, aligned=False, now=_BASE + timedelta(seconds=5))
    assert vwap_alignment_seconds(tracker, _BASE + timedelta(seconds=5)) is None


# ── 해제 조건: STRONG signal + ETF 상호확인 ──────────────────────────────────

def test_strong_signal_with_mutual_etf_confirmation_releases_chop():
    release = evaluate_micro_chop_release(
        live_direction="UP", actionable_signal="HYNIX_STRONG_BUY", etf_mutual_confirmed=True,
    )
    assert release["release"] is True


def test_strong_signal_without_etf_confirmation_does_not_release():
    release = evaluate_micro_chop_release(
        live_direction="UP", actionable_signal="HYNIX_STRONG_BUY", etf_mutual_confirmed=False,
    )
    assert release["release"] is False


# ── 데이터 결측/시차초과는 절대 해제하지 않는다 ──────────────────────────────

def test_data_time_mismatch_never_releases_even_with_strong_alignment():
    release = evaluate_micro_chop_release(
        live_direction="UP", live_direction_held_seconds=100.0, structural_direction="UP",
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        actionable_signal="HYNIX_STRONG_BUY", etf_mutual_confirmed=True,
        data_time_mismatch=True,
    )
    assert release["release"] is False


# ── Persistent state 재시작 후 stale MICRO_CHOP 자동삭제 ────────────────────

def test_stale_persisted_state_from_yesterday_is_cleared_on_restart():
    yesterday_state = _activate_via_real_chop(_BASE - timedelta(days=1))
    assert yesterday_state["active"] is True

    cleared = reset_micro_chop_state_if_stale(yesterday_state, _BASE)
    assert cleared["active"] is False
    assert cleared == default_micro_chop_state()


def test_active_state_without_expires_at_is_cleared_as_legacy():
    legacy_state = {"active": True, "_state_date": _BASE.strftime("%Y%m%d")}  # 구버전 — expires_at 없음
    cleared = reset_micro_chop_state_if_stale(legacy_state, _BASE)
    assert cleared["active"] is False


def test_expired_ttl_state_is_cleared():
    state = _activate_via_real_chop(_BASE)
    expired_check_time = _BASE + timedelta(seconds=MICRO_CHOP_STATE_TTL_SECONDS + 30)
    cleared = reset_micro_chop_state_if_stale(state, expired_check_time)
    assert cleared["active"] is False


def test_fresh_active_state_within_ttl_is_kept():
    state = _activate_via_real_chop(_BASE)
    still_valid_time = _BASE + timedelta(seconds=10)
    kept = reset_micro_chop_state_if_stale(state, still_valid_time)
    assert kept["active"] is True


# ── 상승·하락 완전 대칭 ──────────────────────────────────────────────────────

def test_release_conditions_are_symmetric_up_and_down():
    up = evaluate_micro_chop_release(
        live_direction="UP", live_direction_held_seconds=16.0, structural_direction="UP",
    )
    down = evaluate_micro_chop_release(
        live_direction="DOWN", live_direction_held_seconds=16.0, structural_direction="DOWN",
    )
    assert up["release"] is True
    assert down["release"] is True

    up_window = evaluate_micro_chop_release(
        live_direction="UP", confirm_window_directions={5: "UP", 10: "UP", 20: "UP"},
    )
    down_window = evaluate_micro_chop_release(
        live_direction="DOWN", confirm_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN"},
    )
    assert up_window["release"] is True
    assert down_window["release"] is True
