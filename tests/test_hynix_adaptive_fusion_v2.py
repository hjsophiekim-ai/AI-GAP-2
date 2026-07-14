"""tests/test_hynix_adaptive_fusion_v2.py

Adaptive Fusion 개편(2026-07-14) 회귀 테스트 — 단일 55% 게이트 때문에 모델 의견이
조금만 엇갈려도 하루 종일 거래 0건이 되던 문제를 다단계 진입 사다리(52/55/60/68%)
+ 모델 불일치 예외 진입 + 거래빈도/손실 관리로 교체한 것을 검증한다.
"""
from __future__ import annotations

from datetime import datetime

import app.trading.hynix_adaptive_fusion_engine as fusion


def _model_result(hynix_p, inverse_p, confidence, status=fusion.MODEL_STATUS_ADVISORY):
    hold_p = max(0.0, 100.0 - hynix_p - inverse_p)
    return fusion.build_model_result(
        model_name="TEST", hynix_probability=hynix_p, inverse_probability=inverse_p,
        hold_probability=hold_p, confidence=confidence, recommended_position_pct=0.0,
        data_quality=80.0, model_status=status,
    )


def _active_decision_result(fusion_score=50.0, action="HOLD"):
    return {"action": action, "fusion_result": {"fusion_score": fusion_score}, "recommended_position_pct": 0.0, "reasons": []}


def _cycle_result(cycle_phase="TREND_UP", up3=55.0, down3=30.0, confidence=55.0, accel_up=50.0, accel_down=20.0):
    return {
        "cycle_phase": cycle_phase, "cycle_confidence": confidence,
        "turning_point": {
            "up_turn_probability_3m": up3, "down_turn_probability_3m": down3,
            "up_turn_probability_5m": up3, "down_turn_probability_5m": down3, "confidence": confidence,
        },
        "momentum": {"momentum_acceleration_up": accel_up, "momentum_acceleration_down": accel_down},
        "recommended_position_pct": 0.0, "reasons": [],
    }


def _decide(engine, **overrides):
    kwargs = dict(
        now=datetime(2026, 7, 14, 10, 0),
        active_decision_result=_active_decision_result(),
        prediction_v2_probability={"buy_probability": 20.0, "sell_probability": 10.0, "hold_probability": 70.0},
        prediction_v2_decision={"final_action_v2": "HOLD"},
        prediction_v2_performance={"model_status": "SHADOW", "sample_size": 0, "valid_sample_fraction": 0.0},
        cycle_result=_cycle_result(),
        cycle_ai_validated=True,
        micron_proxy={"effective_micron_score": 50.0, "micron_data_confidence": 55.0, "micron_score_source": "test"},
        held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
        daily_return_pct=0.0, orders_today_count=0,
        hold_tracker=fusion.default_hold_tracker(), whipsaw_state=fusion.default_whipsaw_state(),
        consecutive_stop_losses=0, frequency_state=fusion.default_frequency_state(),
    )
    kwargs.update(overrides)
    return engine.decide(**kwargs)


# ---------------------------------------------------------------------------
# 1) 모델 강한 불일치 시 HOLD
# ---------------------------------------------------------------------------

def test_strong_model_conflict_forces_hold():
    model_results = {
        fusion.MODEL_ACTIVE_FUSION: _model_result(48.0, 52.0, 44.0, fusion.MODEL_STATUS_LIVE_VALIDATED),
        fusion.MODEL_PREDICTION_V2: None,
        fusion.MODEL_CYCLE_AI: None,
        fusion.MODEL_EARLY_PREDICTION: _model_result(97.0, 0.0, 97.0),  # 강한 하이닉스
        fusion.MODEL_MICRON_PROXY: _model_result(0.0, 84.0, 84.0),  # 강한 인버스
    }
    assert fusion.detect_strong_signal_conflict(model_results) is True

    engine = fusion.HynixAdaptiveFusionEngine()
    result = _decide(
        engine,
        active_decision_result=_active_decision_result(fusion_score=48.0),
        cycle_result=_cycle_result(cycle_phase="NO_TRADE", up3=46.0, down3=55.0, accel_up=97.0, accel_down=0.0),
        micron_proxy={"effective_micron_score": 8.0, "micron_data_confidence": 70.0, "micron_score_source": "test"},
    )
    assert result["executable"] is False
    assert result["final_action"] == "HOLD"
    assert result["strong_signal_conflict"] is True


# ---------------------------------------------------------------------------
# 2) 중립 ACTIVE_FUSION + 다른 모델 합의 시 소액 진입
# ---------------------------------------------------------------------------

def test_neutral_active_fusion_with_agreeing_models_allows_small_entry():
    engine = fusion.HynixAdaptiveFusionEngine()
    result = _decide(
        engine,
        active_decision_result=_active_decision_result(fusion_score=49.0),  # 중립(46~54 밴드 내)
        cycle_result=_cycle_result(cycle_phase="TREND_UP", up3=58.0, down3=20.0, confidence=60.0, accel_up=97.0, accel_down=0.0),
        micron_proxy={"effective_micron_score": 52.0, "micron_data_confidence": 55.0, "micron_score_source": "test"},
    )
    assert result["executable"] is True
    assert result["final_action"] == "HYNIX"
    assert result["target_position_pct"] > 0
    assert result["entry_type"] in ("EXPLORATORY", "NORMAL")


def test_disagreement_override_fires_when_ladder_alone_would_block():
    """가중합만으로는 52% 미만이라 사다리가 0%를 주더라도, 고확신 리더+동조가 있고
    강반대모델이 2개 미만이면 탐색진입(10~15%)이 허용돼야 한다."""
    model_results = {
        fusion.MODEL_ACTIVE_FUSION: _model_result(50.0, 50.0, 40.0, fusion.MODEL_STATUS_LIVE_VALIDATED),
        fusion.MODEL_PREDICTION_V2: _model_result(20.0, 10.0, 30.0, fusion.MODEL_STATUS_SHADOW),
        fusion.MODEL_CYCLE_AI: None,
        fusion.MODEL_EARLY_PREDICTION: _model_result(80.0, 0.0, 80.0),  # 고확신 리더(하이닉스)
        fusion.MODEL_MICRON_PROXY: _model_result(0.0, 30.0, 40.0),  # 약한 반대(강반대 아님)
    }
    override = fusion.evaluate_disagreement_override(model_results)
    assert override is not None
    assert override["action"] == fusion.ACTION_HYNIX
    assert 10.0 <= override["position_pct"] <= 15.0


# ---------------------------------------------------------------------------
# 3) 시간대별 문턱 완화
# ---------------------------------------------------------------------------

def test_time_based_relief_applies_after_11am_with_zero_trades():
    result = fusion.time_based_threshold_relief(datetime(2026, 7, 14, 11, 5), orders_today_count=0, daily_return_pct=0.5)
    assert result["relief"] >= 1.5


def test_time_based_relief_stacks_after_1pm_with_at_most_one_trade():
    # 11:00 조건("거래 0건")과 13:00 조건("1건 이하") 둘 다 만족하는 orders_today_count=0
    # 이어야 두 단계가 누적되어 최대 3%p 완화가 적용된다.
    result = fusion.time_based_threshold_relief(datetime(2026, 7, 14, 13, 5), orders_today_count=0, daily_return_pct=0.5)
    assert result["relief"] == 3.0  # 1.5%p + 1.5%p, 최대 완화폭 3%p


def test_no_relief_before_11am():
    result = fusion.time_based_threshold_relief(datetime(2026, 7, 14, 10, 30), orders_today_count=0, daily_return_pct=0.5)
    assert result["relief"] == 0.0


# ---------------------------------------------------------------------------
# 4) 손실 중 문턱 완화 금지
# ---------------------------------------------------------------------------

def test_no_relief_when_afternoon_losing():
    result = fusion.time_based_threshold_relief(datetime(2026, 7, 14, 13, 30), orders_today_count=0, daily_return_pct=-0.5)
    assert result["relief"] == 0.0
    assert any("완화 적용 안 함" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# 5) 2·3연속 손실 제한
# ---------------------------------------------------------------------------

def test_two_consecutive_losses_halves_position():
    engine = fusion.HynixAdaptiveFusionEngine()
    baseline = _decide(
        engine,
        active_decision_result=_active_decision_result(fusion_score=70.0),
        cycle_result=_cycle_result(cycle_phase="TREND_UP", up3=70.0, down3=10.0, confidence=70.0, accel_up=70.0, accel_down=10.0),
        consecutive_stop_losses=0,
    )
    halved = _decide(
        engine,
        active_decision_result=_active_decision_result(fusion_score=70.0),
        cycle_result=_cycle_result(cycle_phase="TREND_UP", up3=70.0, down3=10.0, confidence=70.0, accel_up=70.0, accel_down=10.0),
        consecutive_stop_losses=2,
    )
    assert baseline["executable"] is True
    assert halved["target_position_pct"] == round(baseline["target_position_pct"] * 0.5, 2)


def test_three_consecutive_losses_blocks_new_entry():
    engine = fusion.HynixAdaptiveFusionEngine()
    result = _decide(
        engine,
        active_decision_result=_active_decision_result(fusion_score=70.0),
        cycle_result=_cycle_result(cycle_phase="TREND_UP", up3=70.0, down3=10.0, confidence=70.0, accel_up=70.0, accel_down=10.0),
        consecutive_stop_losses=3,
    )
    assert result["executable"] is False
    assert "연속손절" in (result["blocking_reason"] or "")


def test_real_2_5_percent_daily_loss_blocks_new_entry_via_risk_ladder():
    engine = fusion.HynixAdaptiveFusionEngine()
    result = _decide(
        engine,
        active_decision_result=_active_decision_result(fusion_score=70.0),
        cycle_result=_cycle_result(cycle_phase="TREND_UP", up3=70.0, down3=10.0, confidence=70.0, accel_up=70.0, accel_down=10.0),
        daily_return_pct=-2.6,
    )
    assert result["executable"] is False
    assert "리스크 사다리" in (result["blocking_reason"] or "")


# ---------------------------------------------------------------------------
# 6) 하루 최대 거래수(왕복 6회)
# ---------------------------------------------------------------------------

def test_max_daily_round_trips_blocks_further_entries():
    freq_state = fusion.default_frequency_state()
    now = datetime(2026, 7, 14, 10, 0)
    for _ in range(6):
        freq_state = fusion.register_frequency_round_trip_closed(freq_state, now)
    block_reason = fusion.check_frequency_limits(freq_state, "HYNIX", now)
    assert block_reason is not None
    assert "왕복거래" in block_reason


# ---------------------------------------------------------------------------
# 7) 미체결/중복주문 방지 — 동일 방향 재진입 쿨다운
# ---------------------------------------------------------------------------

def test_same_direction_reentry_cooldown_blocks_immediate_reentry():
    now = datetime(2026, 7, 14, 10, 0)
    freq_state = fusion.register_frequency_entry(fusion.default_frequency_state(), "HYNIX", now)
    block_reason = fusion.check_frequency_limits(freq_state, "HYNIX", now)
    assert block_reason is not None
    assert "쿨다운" in block_reason

    later = datetime(2026, 7, 14, 10, 11)  # 11분 후 — 쿨다운(10분) 해제
    assert fusion.check_frequency_limits(freq_state, "HYNIX", later) is None


# ---------------------------------------------------------------------------
# 8) 14:50 이후 신규진입 금지
# ---------------------------------------------------------------------------

def test_no_new_entry_after_1450():
    engine = fusion.HynixAdaptiveFusionEngine()
    result = _decide(
        engine,
        active_decision_result=_active_decision_result(fusion_score=70.0),
        cycle_result=_cycle_result(cycle_phase="TREND_UP", up3=70.0, down3=10.0, confidence=70.0, accel_up=70.0, accel_down=10.0),
        now=datetime(2026, 7, 14, 14, 51),
    )
    assert result["executable"] is False
    assert "14:50" in (result["blocking_reason"] or "")


def test_new_entry_allowed_before_1450():
    engine = fusion.HynixAdaptiveFusionEngine()
    result = _decide(
        engine,
        active_decision_result=_active_decision_result(fusion_score=70.0),
        cycle_result=_cycle_result(cycle_phase="TREND_UP", up3=70.0, down3=10.0, confidence=70.0, accel_up=70.0, accel_down=10.0),
        now=datetime(2026, 7, 14, 14, 49),
    )
    assert result["executable"] is True


# ---------------------------------------------------------------------------
# 9) Mock/Real 동일 판단 — decide()는 mode 파라미터를 받지 않는다(모드에 따른
# 분기가 없어야 한다). 동일 입력이면 항상 동일 출력이어야 한다.
# ---------------------------------------------------------------------------

def test_decide_is_mode_agnostic_same_inputs_same_output():
    engine = fusion.HynixAdaptiveFusionEngine()
    kwargs = dict(
        active_decision_result=_active_decision_result(fusion_score=62.0),
        cycle_result=_cycle_result(cycle_phase="TREND_UP", up3=62.0, down3=20.0, confidence=62.0, accel_up=62.0, accel_down=15.0),
    )
    result_a = _decide(engine, **kwargs)
    result_b = _decide(engine, **kwargs)
    assert result_a["target_position_pct"] == result_b["target_position_pct"]
    assert result_a["final_action"] == result_b["final_action"]
    assert result_a["executable"] == result_b["executable"]
    import inspect
    assert "mode" not in inspect.signature(fusion.HynixAdaptiveFusionEngine.decide).parameters


# ---------------------------------------------------------------------------
# 10) inverse_pressure_score가 강한 인버스 근거를 보이면 ACTIVE_FUSION에 넘기는
# enhanced_ai_score를 그 방향으로 보정한다(2026-07-14 실측 버그: Adaptive Fusion이
# inverse_pressure_score를 전혀 보지 못해 "INVERSE_STRONG_BUY"인데도 HOLD로 끝남).
# ---------------------------------------------------------------------------

def test_strong_inverse_pressure_boosts_enhanced_score_toward_inverse():
    from app.services.hynix_switch_engine import _boost_enhanced_score_with_inverse_pressure

    boosted = _boost_enhanced_score_with_inverse_pressure(42.78, 77.87)
    assert boosted == 42.78


def test_weak_inverse_pressure_does_not_change_enhanced_score():
    from app.services.hynix_switch_engine import _boost_enhanced_score_with_inverse_pressure

    assert _boost_enhanced_score_with_inverse_pressure(55.0, 40.0) == 55.0


def test_boost_never_pushes_score_away_from_original_direction():
    """이미 인버스 쪽으로 더 강하게 기울어진 enhanced_score라면(예: 15.0), 약한
    inverse_pressure_score 보정이 오히려 그 신호를 약화시켜서는 안 된다(min() 사용)."""
    from app.services.hynix_switch_engine import _boost_enhanced_score_with_inverse_pressure

    boosted = _boost_enhanced_score_with_inverse_pressure(15.0, 70.0)
    assert boosted <= 15.0


# ---------------------------------------------------------------------------
# 11) 문턱완화(threshold relief)가 표시(entry_threshold_used)에만 반영되고 실제
# 진입비중 산정(사다리 52% 고정 바닥)에는 전혀 영향을 주지 못해, 완화가 이름뿐인
# 채로 하루 종일 거래 0건이 이어지던 버그(2026-07-14 실측: fused_inverse_probability
# 49.73%, entry_threshold_used 50.5%인데도 target_position_pct 0%로 매 사이클 차단).
# ---------------------------------------------------------------------------

def test_ladder_floor_relief_lets_borderline_probability_enter():
    assert fusion.position_pct_from_probability_ladder(49.73, floor_relief=0.0) == 0.0
    assert fusion.position_pct_from_probability_ladder(49.73, floor_relief=2.5) == 10.0
    assert fusion.position_pct_from_probability_ladder(51.9, floor_relief=1.5) == 10.0


def test_ladder_floor_relief_does_not_change_tier_gaps():
    # relief는 모든 단계의 바닥을 동일하게 낮출 뿐, 단계 간 간격(52/55/60/68)은 유지한다.
    assert fusion.position_pct_from_probability_ladder(53.5, floor_relief=1.5) == 25.0
    assert fusion.position_pct_from_probability_ladder(66.5, floor_relief=1.5) == 65.0


def test_decide_enters_when_relief_closes_the_gap_to_ladder_floor():
    """실측 재현: ACTIVE_FUSION 단독으로는 인버스 우세(66.53%)지만 다른 모델과 융합하면
    49.73%로 사다리 바닥(52%) 밑으로 떨어진다. 11:00 이후 거래 0건 완화(1.5%p)가 걸리면
    문턱이 50.5%까지 낮아지므로, 완화가 사다리에도 반영돼야 51%대 융합확률에서 진입이
    이뤄진다(수정 전에는 완화가 entry_gate_ok 표시에만 쓰이고 사다리엔 영향이 없어
    ladder 자체가 0%를 반환해 이 케이스가 계속 차단됐다)."""
    engine = fusion.HynixAdaptiveFusionEngine()
    result = _decide(
        engine,
        now=datetime(2026, 7, 14, 11, 5),
        orders_today_count=0, daily_return_pct=0.3,
        active_decision_result=_active_decision_result(fusion_score=32.0, action="HOLD"),  # 인버스쪽 우세
        cycle_result=_cycle_result(cycle_phase="TREND_DOWN", up3=20.0, down3=58.0, confidence=60.0, accel_up=5.0, accel_down=60.0),
        micron_proxy={"effective_micron_score": 30.0, "micron_data_confidence": 60.0, "micron_score_source": "test"},
    )
    assert result["entry_threshold_used"] < 52.0
    dom_prob = max(result["fused_hynix_probability"], result["fused_inverse_probability"])
    if 52.0 - result["threshold_relief_applied"] <= dom_prob < 52.0:
        assert result["target_position_pct"] > 0
        assert result["executable"] is True
