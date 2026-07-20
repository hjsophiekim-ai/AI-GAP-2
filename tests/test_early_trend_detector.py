"""
test_early_trend_detector.py — Early Trend Detector(제한적 탐색진입 엔진) 단위테스트.

Adaptive Regime 하위의 판단 로직만 검증한다(순수 함수) — 실제 주문 실행은
hynix_switch_engine.run_fast_trend_watcher_tick 통합 테스트에서 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading import early_trend_detector as etd


def _bars(prices: list[float], volumes: list[float] | None = None, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 7, 20, 9, 30)
    volumes = volumes or [1000.0] * len(prices)
    rows = []
    for i, (p, v) in enumerate(zip(prices, volumes)):
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": p, "high": p * 1.002, "low": p * 0.998, "close": p, "volume": v,
        })
    return pd.DataFrame(rows)


# ── 조기신호 점수(요구사항1 대체 heuristic) ──────────────────────────────────

def test_compute_early_signal_returns_none_direction_when_flat():
    signal = etd.compute_early_signal({"direction": "FLAT", "up_votes": 3, "down_votes": 3, "top_factors": []})
    assert signal["direction"] is None
    assert signal["score"] == 0.0


def test_compute_early_signal_scores_higher_with_wider_vote_margin_and_volume():
    weak = etd.compute_early_signal({"direction": "UP", "up_votes": 4, "down_votes": 2, "volume_ratio": 1.0, "top_factors": []})
    strong = etd.compute_early_signal({"direction": "UP", "up_votes": 6, "down_votes": 0, "volume_ratio": 2.5, "top_factors": []})
    assert strong["score"] > weak["score"]


def test_compute_early_signal_discounts_score_on_symbol_disagreement():
    agree = etd.compute_early_signal({"direction": "UP", "up_votes": 5, "down_votes": 1, "top_factors": []}, signal_symbol_agreement=True)
    disagree = etd.compute_early_signal({"direction": "UP", "up_votes": 5, "down_votes": 1, "top_factors": []}, signal_symbol_agreement=False)
    assert disagree["score"] < agree["score"]


def test_is_opposite_change_point_detects_direction_flip():
    assert etd.is_opposite_change_point("UP", {"direction": "DOWN"}) is True
    assert etd.is_opposite_change_point("UP", {"direction": "UP"}) is False
    assert etd.is_opposite_change_point(None, {"direction": "DOWN"}) is False
    assert etd.is_opposite_change_point("UP", {"direction": None}) is False


def test_is_opposite_live_slope_reversal_detects_flip_independent_of_vote():
    """요구사항1/4(2026-07-20 최종) — 1분봉 vote와 별개로 ETF 자체 실시간
    기울기가 반대로 확정되면 그 자체로 반대 change-point로 본다."""
    assert etd.is_opposite_live_slope_reversal("UP", "DOWN") is True
    assert etd.is_opposite_live_slope_reversal("UP", "UP") is False
    assert etd.is_opposite_live_slope_reversal(None, "DOWN") is False
    assert etd.is_opposite_live_slope_reversal("UP", None) is False


def test_apply_opposite_change_point_reaction_decays_score_and_blocks_old_direction():
    now = datetime(2026, 7, 20, 10, 27, 0)
    freq = etd.default_frequency_state()
    freq = etd.apply_opposite_change_point_reaction(freq, "UP", 80.0, now)

    assert freq["last_opposite_change_point_decayed_score"] == pytest.approx(80.0 * 0.30)
    # 기존(UP) 방향으로의 재진입은 지금부터 다시 쿨다운된다(요구사항4).
    assert etd.is_same_direction_cooldown_active(freq, "UP", now + timedelta(seconds=10)) is True
    assert etd.is_same_direction_cooldown_active(freq, "DOWN", now + timedelta(seconds=10)) is False


def test_compute_composite_early_signal_uses_live_slope_when_vote_undecided():
    """요구사항1(2026-07-20 최종) — 1분봉 vote가 아직 방향을 확정하지 못했어도
    실시간 기울기 단독으로 최소 신뢰도 후보를 만든다."""
    flat_fast_signal = {"direction": "FLAT", "up_votes": 3, "down_votes": 3, "top_factors": []}
    signal = etd.compute_composite_early_signal(fast_signal=flat_fast_signal, live_direction="DOWN")
    assert signal["direction"] == "DOWN"
    assert signal["score"] >= 50.0


def test_compute_composite_early_signal_boosts_score_on_full_agreement():
    weak_up = {"direction": "UP", "up_votes": 4, "down_votes": 2, "volume_ratio": 1.0, "top_factors": []}
    base = etd.compute_early_signal(weak_up)
    boosted = etd.compute_composite_early_signal(
        fast_signal=weak_up, live_direction="UP", etf_vwap_breakout=True,
        etf_structure_breakout=True, etf_volume_surge=True,
    )
    assert boosted["score"] > base["score"]
    assert boosted["direction"] == "UP"


# ── 단계별 진입(요구사항2) ───────────────────────────────────────────────────

def test_stage_for_elapsed_seconds_matches_spec_ladder():
    """요구사항(2026-07-20 최종) — 10~15%(중간값 12%, 즉시)→30%(15초 유지)→
    50%(30초 유지 + 방향 일치). 처음부터/조건 없이 50%로 들어가지 않는다."""
    assert etd.stage_for_elapsed_seconds(0.0) == (etd.STAGE_INITIAL, 0.12)
    assert etd.stage_for_elapsed_seconds(14.9) == (etd.STAGE_INITIAL, 0.12)
    assert etd.stage_for_elapsed_seconds(15.0) == (etd.STAGE_HOLD_15S, 0.30)
    assert etd.stage_for_elapsed_seconds(29.9) == (etd.STAGE_HOLD_15S, 0.30)
    assert etd.stage_for_elapsed_seconds(30.0) == (etd.STAGE_HOLD_15S, 0.30)  # 방향 일치 없이는 50%로 승격하지 않음
    assert etd.stage_for_elapsed_seconds(30.0, direction_aligned=True) == (etd.STAGE_HOLD_30S_ALIGNED, 0.50)
    assert etd.stage_for_elapsed_seconds(600.0, direction_aligned=True) == (etd.STAGE_HOLD_30S_ALIGNED, 0.50)
    assert etd.stage_for_elapsed_seconds(600.0, direction_aligned=False) == (etd.STAGE_HOLD_15S, 0.30)


def test_target_probe_pct_never_exceeds_50_percent():
    for elapsed in (0.0, 15.0, 45.0, 600.0):
        _, pct = etd.compute_target_probe_pct("STRONG_UP", elapsed, direction_aligned=True)
        assert pct <= 0.50


def test_regime_probe_cap_blocks_range_and_data_insufficient():
    assert etd.regime_probe_cap("RANGE") == 0.0
    assert etd.regime_probe_cap("DATA_INSUFFICIENT") == 0.0


def test_regime_probe_cap_limits_volatile_range_and_panic():
    assert etd.regime_probe_cap("VOLATILE_RANGE") == 0.50
    assert etd.regime_probe_cap("PANIC") == 0.10


def test_compute_target_probe_pct_applies_regime_cap_even_at_late_stage():
    stage, pct = etd.compute_target_probe_pct("PANIC", 60.0)
    assert stage == etd.STAGE_HOLD_15S  # direction_aligned 없이는 50% 단계로 승격하지 않음
    assert pct == 0.10  # PANIC 상한이 단계값(0.30)보다 우선한다


def test_compute_target_probe_pct_is_zero_in_range_regardless_of_elapsed():
    for elapsed in (0.0, 30.0, 600.0):
        _, pct = etd.compute_target_probe_pct("RANGE", elapsed)
        assert pct == 0.0


def test_expansion_target_pct_only_after_confirmed_strong_trend_matching_direction():
    assert etd.expansion_target_pct("STRONG_UP", "UP", holding_inverse=False) == 0.50
    assert etd.expansion_target_pct("STRONG_DOWN", "DOWN", holding_inverse=True) == 0.50
    assert etd.expansion_target_pct("STRONG_UP", "DOWN", holding_inverse=True) is None
    assert etd.expansion_target_pct("VOLATILE_RANGE", "UP", holding_inverse=False) is None
    assert etd.expansion_target_pct("STRONG_DOWN", "UP", holding_inverse=False) is None


# ── CHASE_BLOCK(요구사항4) ───────────────────────────────────────────────────

def test_chase_block_triggers_when_moved_past_fixed_threshold_in_regime_without_own_setting():
    result = etd.evaluate_chase_block(
        signal_reference_price=10_000.0, current_price=10_080.0,  # +0.8% > 0.7%
        confirmed_regime="STRONG_UP", df_1min=None, direction="UP",
    )
    assert result["blocked"] is True


def test_chase_block_allows_entry_within_fixed_threshold():
    result = etd.evaluate_chase_block(
        signal_reference_price=10_000.0, current_price=10_050.0,  # +0.5% < 0.7%
        confirmed_regime="STRONG_UP", df_1min=None, direction="UP",
    )
    assert result["blocked"] is False


def test_chase_block_uses_regime_own_threshold_when_present():
    # VOLATILE_RANGE는 자체 chase_block_move_pct(0.7)를 이미 갖고 있다 — 그 값을 그대로 쓴다.
    result = etd.evaluate_chase_block(
        signal_reference_price=10_000.0, current_price=10_060.0,  # +0.6%, 0.7% 미만
        confirmed_regime="VOLATILE_RANGE", df_1min=None, direction="UP",
    )
    assert result["blocked"] is False


def test_chase_block_at_recent_extreme_blocks_buy():
    df = _bars([100.0, 100.1, 100.2, 105.0])  # 최근 고점이 105
    result = etd.evaluate_chase_block(
        signal_reference_price=100.0, current_price=105.0,
        confirmed_regime="STRONG_UP", df_1min=df, direction="UP",
    )
    assert result["blocked"] is True


# ── 거래비용 게이트(요구사항6) ────────────────────────────────────────────────

def test_cost_gate_blocks_when_expected_net_edge_below_threshold():
    result = etd.evaluate_cost_gate("0193T0", expected_move_pct=0.1)
    assert result["blocked"] is True
    assert result["net_edge_pct"] < etd.COST_GATE_MIN_NET_EDGE_PCT


def test_cost_gate_allows_when_expected_net_edge_clears_threshold():
    result = etd.evaluate_cost_gate("0193T0", expected_move_pct=2.0)
    assert result["blocked"] is False
    assert result["net_edge_pct"] >= etd.COST_GATE_MIN_NET_EDGE_PCT


# ── 조기진입 철수(요구사항3, 2026-07-20 최종 — dict 반환) ────────────────────

def test_should_exit_probe_on_fixed_stop_loss():
    plan = etd.should_exit_probe(
        net_return_pct=-0.5, seconds_since_last_reconfirmation=5, signal_still_valid=True, opposite_change_point=False,
    )
    assert plan["action"] == "SELL_ALL" and plan["ratio"] == 1.0
    assert "고정손절" in plan["reason"]


def test_should_exit_probe_on_opposite_change_point():
    plan = etd.should_exit_probe(
        net_return_pct=0.1, seconds_since_last_reconfirmation=5, signal_still_valid=True, opposite_change_point=True,
    )
    assert plan["action"] == "SELL_ALL"
    assert "변화점" in plan["reason"]


def test_should_exit_probe_on_signal_decay():
    plan = etd.should_exit_probe(
        net_return_pct=0.1, seconds_since_last_reconfirmation=5, signal_still_valid=False, opposite_change_point=False,
    )
    assert plan["action"] == "SELL_ALL"
    assert "소멸" in plan["reason"]


def test_should_exit_probe_on_reconfirmation_timeout():
    """요구사항(2026-07-20 최종) — 재확인 시한이 60초에서 30초로 단축됐다."""
    plan = etd.should_exit_probe(
        net_return_pct=0.1, seconds_since_last_reconfirmation=31, signal_still_valid=True, opposite_change_point=False,
    )
    assert plan["action"] == "SELL_ALL"
    assert "30초" in plan["reason"]


def test_should_exit_probe_holds_when_all_conditions_clear():
    plan = etd.should_exit_probe(
        net_return_pct=0.1, seconds_since_last_reconfirmation=20, signal_still_valid=True, opposite_change_point=False,
    )
    assert plan["action"] == "HOLD"


def test_should_exit_probe_volatile_range_tp1_partial_then_tp2_full():
    """요구사항(2026-07-20 최종) — VOLATILE_RANGE 확정 중에는 TP1(+0.8%)에서
    50%+ 부분익절, TP2(+1.5~2.0%)에서 전량익절, SL(-0.5%)에서 손절한다."""
    tp1 = etd.should_exit_probe(
        net_return_pct=0.9, seconds_since_last_reconfirmation=5, signal_still_valid=True,
        opposite_change_point=False, confirmed_regime="VOLATILE_RANGE",
    )
    assert tp1["action"] == "SELL_PARTIAL"
    assert tp1["ratio"] >= 0.5

    tp2 = etd.should_exit_probe(
        net_return_pct=1.8, seconds_since_last_reconfirmation=5, signal_still_valid=True,
        opposite_change_point=False, confirmed_regime="VOLATILE_RANGE", tp1_taken=True,
    )
    assert tp2["action"] == "SELL_ALL"
    assert "TP2" in tp2["reason"]

    sl = etd.should_exit_probe(
        net_return_pct=-0.6, seconds_since_last_reconfirmation=5, signal_still_valid=True,
        opposite_change_point=False, confirmed_regime="VOLATILE_RANGE",
    )
    assert sl["action"] == "SELL_ALL"
    assert "손절" in sl["reason"]


def test_should_exit_probe_volatile_range_signal_weakening_ignores_pnl():
    """요구사항(2026-07-20 최종) — VOLATILE_RANGE에서 신호가 약해지면(반대
    change-point/신호소멸) 수익 중이어도 손익과 무관하게 즉시 전량청산한다."""
    plan = etd.should_exit_probe(
        net_return_pct=1.0, seconds_since_last_reconfirmation=5, signal_still_valid=True,
        opposite_change_point=True, confirmed_regime="VOLATILE_RANGE",
    )
    assert plan["action"] == "SELL_ALL"
    assert "신호약화" in plan["reason"]


def test_should_exit_probe_volatile_range_max_hold_time():
    plan = etd.should_exit_probe(
        net_return_pct=0.1, seconds_since_last_reconfirmation=5, signal_still_valid=True,
        opposite_change_point=False, confirmed_regime="VOLATILE_RANGE",
        held_minutes=etd.VOLATILE_RANGE_MAX_HOLD_MINUTES,
    )
    assert plan["action"] == "SELL_ALL"
    assert "최대보유시간" in plan["reason"]


# ── 쿨다운/서킷브레이커/일일 한도(요구사항6) ─────────────────────────────────

def test_same_direction_cooldown_blocks_reentry_within_three_minutes():
    now = datetime(2026, 7, 20, 10, 0, 0)
    freq = etd.register_probe_entry(etd.default_frequency_state(), "UP", now)
    assert etd.is_same_direction_cooldown_active(freq, "UP", now + timedelta(seconds=100)) is True
    assert etd.is_same_direction_cooldown_active(freq, "UP", now + timedelta(seconds=181)) is False
    assert etd.is_same_direction_cooldown_active(freq, "DOWN", now + timedelta(seconds=10)) is False


def test_two_consecutive_fake_signal_losses_halt_for_twenty_minutes():
    now = datetime(2026, 7, 20, 10, 0, 0)
    freq = etd.default_frequency_state()
    freq = etd.register_probe_round_trip_closed(freq, now, was_fake_signal_loss=True)
    halted, _ = etd.is_halted(freq, now)
    assert halted is False  # 1회는 아직 중단 아님

    freq = etd.register_probe_round_trip_closed(freq, now, was_fake_signal_loss=True)
    halted, remaining = etd.is_halted(freq, now)
    assert halted is True
    assert remaining == pytest.approx(etd.FAKE_SIGNAL_HALT_MINUTES * 60, abs=1)

    halted_later, _ = etd.is_halted(freq, now + timedelta(minutes=etd.FAKE_SIGNAL_HALT_MINUTES + 1))
    assert halted_later is False


def test_winning_round_trip_resets_consecutive_fake_signal_counter():
    now = datetime(2026, 7, 20, 10, 0, 0)
    freq = etd.default_frequency_state()
    freq = etd.register_probe_round_trip_closed(freq, now, was_fake_signal_loss=True)
    freq = etd.register_probe_round_trip_closed(freq, now, was_fake_signal_loss=False)
    assert freq["consecutive_fake_signal_losses"] == 0
    halted, _ = etd.is_halted(freq, now)
    assert halted is False


def test_frequency_state_resets_round_trip_count_on_new_day():
    freq = {"date": "20260719", "round_trips_today": 4, "consecutive_fake_signal_losses": 0, "halted_until": None,
            "last_entry_at": None, "last_entry_direction": None}
    reset = etd.reset_frequency_state_if_new_day(freq, "20260720")
    assert reset["round_trips_today"] == 0
    assert reset["date"] == "20260720"
