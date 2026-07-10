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
