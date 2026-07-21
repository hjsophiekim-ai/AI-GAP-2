"""
test_etf_direction_confirmation.py — classify_etf_direction_confirmation() 검증
(2026-07-21 실측 버그 수정: 30분 이상 하이닉스 상승 중에도 ETF_DIRECTION_MISMATCH로
모든 레버리지 진입이 차단되던 문제).
"""

from __future__ import annotations

from app.trading.etf_entry_confirmation import (
    ALIGNED_PULLBACK,
    DATA_TIME_MISMATCH,
    ETF_CONFIRM_DOWN,
    ETF_CONFIRM_UP,
    ETF_CONFIRMATION_PENDING,
    ETF_DATA_INSUFFICIENT,
    ETF_DIRECTION_MISMATCH,
    classify_etf_direction_confirmation,
)

_FRESH_AGES = {"signal": 1.0, "confirm": 1.0, "oppose": 1.0}


def _classify(direction, confirm, oppose, **overrides):
    kwargs = dict(
        direction=direction,
        signal_direction=direction,
        confirm_window_directions=confirm,
        oppose_window_directions=oppose,
        confirm_above_vwap=True,
        confirm_swing_broken_against=False,
        structural_direction=direction,
        data_ages_seconds=dict(_FRESH_AGES),
        moved_pct_since_signal=0.1,
    )
    kwargs.update(overrides)
    return classify_etf_direction_confirmation(**kwargs)


# ── 1. 30분 상승 + 5초 눌림만 존재 → ALIGNED_PULLBACK 진입 허용 ───────────────

def test_thirty_minute_uptrend_with_only_5s_pullback_is_aligned_pullback():
    result = _classify(
        "UP",
        confirm={5: "DOWN", 10: "UP", 20: "UP", 30: "UP"},
        oppose={5: "UP", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
    )
    assert result["state"] == ALIGNED_PULLBACK


def test_downtrend_with_only_5s_pullback_is_aligned_pullback_symmetric():
    """상승 케이스와 완전 대칭 — 인버스가 진입대상일 때도 동일하게 적용된다."""
    result = _classify(
        "DOWN",
        confirm={5: "DOWN", 10: "UP", 20: "UP", 30: "UP"},
        oppose={5: "UP", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
    )
    assert result["state"] == ALIGNED_PULLBACK


# ── 2. 5/10/20초 UP, 30초 DOWN → 레버리지 확인 통과 ──────────────────────────

def test_5_10_20_up_30_down_passes_leverage_confirmation():
    result = _classify(
        "UP",
        confirm={5: "UP", 10: "UP", 20: "UP", 30: "DOWN"},
        oppose={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
    )
    assert result["state"] == ETF_CONFIRM_UP


def test_5_10_20_up_30_down_passes_inverse_confirmation_symmetric():
    result = _classify(
        "DOWN",
        confirm={5: "UP", 10: "UP", 20: "UP", 30: "DOWN"},
        oppose={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
    )
    assert result["state"] == ETF_CONFIRM_DOWN


# ── 3. 0193T0 5/10초 DOWN → 진입 차단 ────────────────────────────────────────

def test_confirm_etf_5s_10s_down_blocks_entry():
    result = _classify(
        "UP",
        confirm={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        oppose={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
    )
    assert result["state"] == ETF_DIRECTION_MISMATCH


def test_confirm_etf_5s_10s_down_blocks_entry_symmetric():
    result = _classify(
        "DOWN",
        confirm={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        oppose={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
    )
    assert result["state"] == ETF_DIRECTION_MISMATCH


# ── 4. stale/결측 데이터는 mismatch가 아니다 ─────────────────────────────────

def test_stale_data_is_not_direction_mismatch():
    result = _classify(
        "UP",
        confirm={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},  # 방향상 명백히 mismatch로 보일 조건
        oppose={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        data_ages_seconds={"signal": 1.0, "confirm": 12.0, "oppose": 1.0},  # confirm만 오래됨
    )
    assert result["state"] != ETF_DIRECTION_MISMATCH
    assert result["state"] == DATA_TIME_MISMATCH


def test_missing_data_is_data_insufficient_not_mismatch():
    result = _classify(
        "UP",
        confirm={5: "DOWN", 10: "DOWN"},
        oppose={5: "UP", 10: "UP"},
        data_ages_seconds={"signal": 1.0, "confirm": None, "oppose": 1.0},
    )
    assert result["state"] != ETF_DIRECTION_MISMATCH
    assert result["state"] == ETF_DATA_INSUFFICIENT


# ── 5. 상승·하락 방향 완전 대칭 ───────────────────────────────────────────────

def test_opposite_etf_strong_confirm_blocks_both_directions_symmetrically():
    """반대 ETF가 5초·10초 모두 강하게 반대방향을 확정하면 양쪽 모두 차단된다."""
    up_result = _classify(
        "UP",
        confirm={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose={5: "UP", 10: "UP", 20: "DOWN", 30: "DOWN"},  # 반대(인버스) 5·10초 모두 UP
    )
    assert up_result["state"] == ETF_DIRECTION_MISMATCH

    down_result = _classify(
        "DOWN",
        confirm={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose={5: "UP", 10: "UP", 20: "DOWN", 30: "DOWN"},  # 반대(레버리지) 5·10초 모두 UP
    )
    assert down_result["state"] == ETF_DIRECTION_MISMATCH


def test_chase_block_applies_symmetrically():
    up_result = _classify("UP", confirm={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
                           oppose={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
                           moved_pct_since_signal=0.8)
    down_result = _classify("DOWN", confirm={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
                             oppose={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
                             moved_pct_since_signal=0.8)
    assert up_result["state"] == ETF_DIRECTION_MISMATCH
    assert down_result["state"] == ETF_DIRECTION_MISMATCH


def test_vwap_plus_swing_break_blocks_symmetrically():
    up_result = _classify("UP", confirm={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
                           oppose={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
                           confirm_above_vwap=False, confirm_swing_broken_against=True)
    down_result = _classify("DOWN", confirm={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
                             oppose={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
                             confirm_above_vwap=False, confirm_swing_broken_against=True)
    assert up_result["state"] == ETF_DIRECTION_MISMATCH
    assert down_result["state"] == ETF_DIRECTION_MISMATCH


# ── 단일 30초 값 또는 VWAP 하나만으로 전체 차단하지 않는다(핵심 회귀 방지) ────

def test_single_30s_value_alone_does_not_block_entry():
    """요구사항 — 30초 값 하나 또는 VWAP 하나가 반대라는 이유만으로 전체 진입을
    차단하지 마라. 5·10·20초가 모두 확인되면 30초 하나만 반대여도 통과해야 한다."""
    result = _classify(
        "UP",
        confirm={5: "UP", 10: "UP", 20: "UP", 30: "DOWN"},
        oppose={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
    )
    assert result["state"] == ETF_CONFIRM_UP


def test_evidence_always_includes_full_diagnostic_fields():
    """요구사항1 — ETF_DIRECTION_MISMATCH 등 판정 시 근거(기울기/VWAP/시각/age)를
    반드시 기록한다."""
    result = _classify(
        "UP",
        confirm={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        oppose={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
    )
    evidence = result["evidence"]
    for key in (
        "signal_direction", "confirm_window_directions", "oppose_window_directions",
        "confirm_above_vwap", "confirm_swing_broken_against", "structural_direction",
        "data_ages_seconds", "moved_pct_since_signal",
    ):
        assert key in evidence


def test_unmet_confirmation_is_pending_not_hard_mismatch():
    result = _classify(
        "UP",
        confirm={5: "UP", 10: "FLAT", 20: "UP", 30: "FLAT"},
        oppose={5: "DOWN", 10: "FLAT", 20: "DOWN", 30: "FLAT"},
    )
    assert result["state"] == ETF_CONFIRMATION_PENDING
    assert result["state"] != ETF_DIRECTION_MISMATCH
