"""test_hynix_decision_v2.py — BUY/SELL/HOLD 확률, Adaptive Threshold, 실현수익률 Accuracy,
Profit Factor 테스트."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.models.hynix_decision_v2 import (
    ACTION_BUY, ACTION_HOLD, ACTION_INVERSE,
    REALIZED_OUTCOME_FAILURE, REALIZED_OUTCOME_SUCCESS, REALIZED_OUTCOME_VOID,
    DEFAULT_BUY_THRESHOLD, RELAXED_THRESHOLD, TIGHTENED_THRESHOLD,
    adaptive_threshold_update, classify_realized_outcome, compute_buy_sell_hold_probability,
    compute_profit_factor, decide_final_action_v2, default_threshold_state,
    recommend_profit_factor_weights, PROFIT_FACTOR_MIN_SAMPLES,
    calculate_fusion_score, decide_fusion_based_action, calculate_prediction_ai_directional_score,
    calculate_momentum_ai_directional_score, FUSION_BAND_BUY, FUSION_BAND_TRIAL_ENTRY,
    FUSION_BAND_HOLD, FUSION_BAND_INVERSE, FUSION_BAND_NO_TRADE_OVERRIDE,
)


class TestBuySellHoldProbability:
    def test_probabilities_sum_to_100(self):
        tp = {"up_turn_probability_3m": 80.0, "up_turn_probability_5m": 75.0, "up_turn_probability_10m": 70.0,
              "down_turn_probability_3m": 20.0, "down_turn_probability_5m": 25.0, "down_turn_probability_10m": 30.0}
        result = compute_buy_sell_hold_probability(tp)
        total = result["buy_probability"] + result["sell_probability"] + result["hold_probability"]
        assert abs(total - 100.0) < 0.5

    def test_strong_up_turn_yields_high_buy_probability(self):
        tp = {"up_turn_probability_3m": 90.0, "up_turn_probability_5m": 85.0, "up_turn_probability_10m": 80.0,
              "down_turn_probability_3m": 15.0, "down_turn_probability_5m": 15.0, "down_turn_probability_10m": 15.0}
        result = compute_buy_sell_hold_probability(tp)
        assert result["buy_probability"] > result["sell_probability"]
        assert result["buy_probability"] > 50.0

    def test_neutral_turning_points_yield_high_hold_probability(self):
        tp = {"up_turn_probability_3m": 50.0, "up_turn_probability_5m": 50.0, "up_turn_probability_10m": 50.0,
              "down_turn_probability_3m": 50.0, "down_turn_probability_5m": 50.0, "down_turn_probability_10m": 50.0}
        result = compute_buy_sell_hold_probability(tp)
        assert result["hold_probability"] >= 90.0

    def test_enhanced_score_is_reference_only_not_a_gate(self):
        """turning point가 완전히 중립이면 enhanced_score가 아무리 높아도 HOLD 확률이 여전히 압도적이어야 한다."""
        tp = {"up_turn_probability_3m": 50.0, "up_turn_probability_5m": 50.0, "up_turn_probability_10m": 50.0,
              "down_turn_probability_3m": 50.0, "down_turn_probability_5m": 50.0, "down_turn_probability_10m": 50.0}
        result = compute_buy_sell_hold_probability(tp, enhanced_score=95.0)
        assert result["buy_probability"] < DEFAULT_BUY_THRESHOLD


class TestFinalActionThreshold:
    def test_buy_above_threshold(self):
        state = default_threshold_state()
        prob = {"buy_probability": 73.0, "sell_probability": 18.0, "hold_probability": 9.0}
        decision = decide_final_action_v2(prob, state)
        assert decision["final_action_v2"] == ACTION_BUY

    def test_sell_above_threshold_yields_inverse(self):
        state = default_threshold_state()
        prob = {"buy_probability": 10.0, "sell_probability": 70.0, "hold_probability": 20.0}
        decision = decide_final_action_v2(prob, state)
        assert decision["final_action_v2"] == ACTION_INVERSE

    def test_both_below_threshold_holds(self):
        state = default_threshold_state()
        prob = {"buy_probability": 55.0, "sell_probability": 40.0, "hold_probability": 5.0}
        decision = decide_final_action_v2(prob, state)
        assert decision["final_action_v2"] == ACTION_HOLD


class TestAdaptiveThreshold:
    def test_five_consecutive_holds_within_30min_relaxes_threshold(self):
        state = default_threshold_state()
        now = datetime(2026, 7, 13, 10, 0)
        for i in range(5):
            state = adaptive_threshold_update(state, ACTION_HOLD, now + timedelta(minutes=i * 2))
        assert state["buy_threshold"] == RELAXED_THRESHOLD
        assert state["sell_threshold"] == RELAXED_THRESHOLD

    def test_whipsaw_raises_threshold_back(self):
        state = default_threshold_state()
        now = datetime(2026, 7, 13, 10, 0)
        actions = [ACTION_BUY, ACTION_INVERSE, ACTION_BUY, ACTION_INVERSE, ACTION_BUY, ACTION_INVERSE]
        for i, a in enumerate(actions):
            state = adaptive_threshold_update(state, a, now + timedelta(minutes=i))
        assert state["whipsaw_flips"] >= 5
        assert state["buy_threshold"] == TIGHTENED_THRESHOLD

    def test_holds_outside_30min_window_do_not_count(self):
        state = default_threshold_state()
        now = datetime(2026, 7, 13, 10, 0)
        # 4 holds far in the past (outside window), then 1 recent — should NOT relax.
        for i in range(4):
            state = adaptive_threshold_update(state, ACTION_HOLD, now - timedelta(minutes=60 - i))
        state = adaptive_threshold_update(state, ACTION_HOLD, now)
        assert state["buy_threshold"] == DEFAULT_BUY_THRESHOLD


class TestRealizedOutcomeClassification:
    def test_buy_success_at_0_3pct_or_more(self):
        assert classify_realized_outcome(ACTION_BUY, 0.35, None) == REALIZED_OUTCOME_SUCCESS

    def test_buy_void_below_threshold(self):
        assert classify_realized_outcome(ACTION_BUY, 0.1, None) == REALIZED_OUTCOME_VOID

    def test_buy_failure_opposite_direction(self):
        assert classify_realized_outcome(ACTION_BUY, -0.4, None) == REALIZED_OUTCOME_FAILURE

    def test_inverse_uses_inverse_return(self):
        assert classify_realized_outcome(ACTION_INVERSE, None, 0.5) == REALIZED_OUTCOME_SUCCESS
        assert classify_realized_outcome(ACTION_INVERSE, None, -0.5) == REALIZED_OUTCOME_FAILURE

    def test_hold_is_not_applicable(self):
        assert classify_realized_outcome(ACTION_HOLD, 1.0, 1.0) == "NOT_APPLICABLE"


class TestProfitFactor:
    def test_profit_factor_basic(self):
        trades = [{"pnl_pct": 1.0}, {"pnl_pct": 0.5}, {"pnl_pct": -0.3}, {"pnl_pct": -0.2}]
        pf = compute_profit_factor(trades)
        assert pf["gross_profit"] == pytest.approx(1.5)
        assert pf["gross_loss"] == pytest.approx(0.5)
        assert pf["profit_factor"] == pytest.approx(3.0)
        assert pf["win_rate"] == 50.0

    def test_profit_factor_no_losses_is_infinite(self):
        pf = compute_profit_factor([{"pnl_pct": 1.0}, {"pnl_pct": 0.5}])
        assert pf["profit_factor"] == float("inf")

    def test_empty_trades(self):
        pf = compute_profit_factor([])
        assert pf["trade_count"] == 0
        assert pf["profit_factor"] == 0.0

    def test_recommend_skips_below_min_sample(self):
        df = pd.DataFrame({
            "horizon_minutes": ["5"] * 10, "predicted_action": ["HYNIX_BUY"] * 10,
            "hynix_return_pct": [0.4] * 10, "inverse_return_pct": [None] * 10,
        })
        assert len(df) < PROFIT_FACTOR_MIN_SAMPLES
        result = recommend_profit_factor_weights(df)
        assert result["skipped"] is True
        assert result["recommended_horizon_weights"] is None

    def test_recommend_runs_when_enough_samples(self):
        n = PROFIT_FACTOR_MIN_SAMPLES + 5
        df = pd.DataFrame({
            "horizon_minutes": ["5"] * n, "predicted_action": ["HYNIX_BUY"] * n,
            "hynix_return_pct": [0.4] * n, "inverse_return_pct": [None] * n,
        })
        result = recommend_profit_factor_weights(df)
        assert result["skipped"] is False
        assert result["recommended_horizon_weights"] is not None


class TestFusionScore:
    """Cycle AI는 Entry Gate가 아니라 fusion_score의 작은 보조 feature(cycle_bonus)일 뿐이다."""

    def test_directional_score_conversion(self):
        assert calculate_prediction_ai_directional_score(80.0, 20.0) == 80.0
        assert calculate_prediction_ai_directional_score(50.0, 50.0) == 50.0
        assert calculate_momentum_ai_directional_score(70.0, 30.0) == 70.0

    def test_fusion_score_formula_matches_literal_weights(self):
        result = calculate_fusion_score(
            prediction_ai_score=100.0, enhanced_ai_score=90.0, momentum_ai_score=90.0,
            micron_ai_score=90.0, cycle_bonus=15.0,
        )
        expected = 0.35 * 100.0 + 0.25 * 90.0 + 0.20 * 90.0 + 0.10 * 90.0 + 0.10 * 15.0
        assert result["fusion_score"] == pytest.approx(expected, abs=0.01)

    def test_cycle_bonus_is_a_small_nudge_not_dominant(self):
        """NO_TRADE(-8)와 TREND_UP(+15) 차이가 나머지 컴포넌트 대비 작아야 한다(단독 게이트 아님)."""
        base_kwargs = dict(prediction_ai_score=70.0, enhanced_ai_score=70.0, momentum_ai_score=70.0, micron_ai_score=70.0)
        trend_up = calculate_fusion_score(**base_kwargs, cycle_bonus=15.0)
        no_trade = calculate_fusion_score(**base_kwargs, cycle_bonus=-8.0)
        # 두 cycle_bonus 차이(23점)의 10% 가중치만 반영되므로 fusion_score 차이는 2.3점 이하다.
        assert abs(trend_up["fusion_score"] - no_trade["fusion_score"]) <= 2.5

    def test_no_trade_phase_does_not_block_by_itself(self):
        """NO_TRADE라도 fusion_score가 밴드를 충족하면(또는 override 조건이면) BUY/INVERSE가 나올 수 있다."""
        strong_bullish = calculate_fusion_score(
            prediction_ai_score=95.0, enhanced_ai_score=90.0, momentum_ai_score=85.0, micron_ai_score=80.0,
            cycle_phase="NO_TRADE",
        )
        decision = decide_fusion_based_action(strong_bullish, cycle_phase="NO_TRADE")
        assert decision["action"] == ACTION_BUY
        assert decision["band"] in (FUSION_BAND_BUY, FUSION_BAND_NO_TRADE_OVERRIDE)

    def test_no_trade_override_allows_15pct_trial_entry(self):
        """NO_TRADE + PredictionAI>=65면 fusion_score 밴드와 무관하게 15% 시험진입 허용."""
        result = calculate_fusion_score(
            prediction_ai_score=70.0, enhanced_ai_score=40.0, momentum_ai_score=30.0, micron_ai_score=30.0,
            cycle_phase="NO_TRADE",
        )
        decision = decide_fusion_based_action(result, cycle_phase="NO_TRADE")
        assert decision["band"] == FUSION_BAND_NO_TRADE_OVERRIDE
        assert decision["action"] == ACTION_BUY
        assert decision["position_pct"] == 15.0

    def test_band_buy_at_68_or_above(self):
        result = calculate_fusion_score(prediction_ai_score=100.0, enhanced_ai_score=100.0, momentum_ai_score=100.0, micron_ai_score=100.0, cycle_bonus=15.0)
        decision = decide_fusion_based_action(result, cycle_phase="TREND_UP")
        assert decision["band"] == FUSION_BAND_BUY
        assert decision["action"] == ACTION_BUY

    def test_band_trial_entry_between_58_and_67(self):
        result = calculate_fusion_score(prediction_ai_score=85.0, enhanced_ai_score=70.0, momentum_ai_score=60.0, micron_ai_score=55.0, cycle_bonus=6.0)
        assert 58.0 <= result["fusion_score"] < 68.0
        decision = decide_fusion_based_action(result, cycle_phase="BASE_BUILDING")
        assert decision["band"] == FUSION_BAND_TRIAL_ENTRY
        assert decision["action"] == ACTION_BUY
        assert 0 < decision["position_pct"] < 50.0

    def test_band_hold_between_50_and_57(self):
        result = calculate_fusion_score(prediction_ai_score=60.0, enhanced_ai_score=60.0, momentum_ai_score=60.0, micron_ai_score=60.0, cycle_bonus=0.0)
        assert 50.0 <= result["fusion_score"] < 58.0
        decision = decide_fusion_based_action(result, cycle_phase="RANGE_NOISE")
        assert decision["band"] == FUSION_BAND_HOLD
        assert decision["action"] == ACTION_HOLD

    def test_band_inverse_below_50(self):
        result = calculate_fusion_score(prediction_ai_score=20.0, enhanced_ai_score=20.0, momentum_ai_score=20.0, micron_ai_score=20.0, cycle_bonus=-4.0)
        decision = decide_fusion_based_action(result, cycle_phase="DISTRIBUTION")
        assert decision["band"] == FUSION_BAND_INVERSE
        assert decision["action"] == ACTION_INVERSE
