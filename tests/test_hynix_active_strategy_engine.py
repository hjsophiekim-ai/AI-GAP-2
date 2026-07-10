"""test_hynix_active_strategy_engine.py — 거래모드/조기진입/Scale-in/방향전환/재진입 테스트."""

from datetime import datetime, timedelta

import pytest

from app.trading.hynix_trading_mode import (
    MODE_ACTIVE, MODE_AGGRESSIVE, MODE_SAFE, MODE_BALANCED,
    calculate_entry_position_pct, calculate_scale_up_target_pct, daily_pnl_position_scale,
    mode_initial_threshold, mode_max_round_trips,
)
from app.trading.hynix_active_strategy_engine import (
    ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE, ACTION_HOLD, ACTION_SCALE_OUT_PARTIAL, ACTION_SWITCH,
    calculate_effective_threshold, decide_active_strategy_action, default_active_strategy_state,
    evaluate_scale_in, register_position_closed, register_position_opened, update_hold_streak,
)
from app.models.hynix_position_sizing_ai import PositionSizingAI, calculate_expected_value


_NOW = datetime(2026, 7, 13, 10, 0)


def _base_kwargs(**overrides):
    kwargs = dict(
        mode=MODE_ACTIVE, now=_NOW, buy_probability=50.0, inverse_probability=50.0, hold_probability=100.0,
        model_confidence=60.0, expected_move_pct=0.3, down_turn_probability_3m=40.0, up_turn_probability_3m=40.0,
        momentum_inflection_or_acceleration=60.0, cycle_phase="BASE_BUILDING", order_flow_confidence=None,
        atr_pct=1.0, consecutive_stop_losses=0, recent_pnl_pct=None, daily_return_pct=0.0,
        position_state={"symbol": None, "quantity": 0, "entry_price": None},
        strategy_state=default_active_strategy_state(MODE_ACTIVE),
    )
    kwargs.update(overrides)
    return kwargs


class TestTradingModeConfig:
    def test_mode_thresholds_ordered_safe_to_aggressive(self):
        assert mode_initial_threshold(MODE_SAFE) > mode_initial_threshold(MODE_BALANCED)
        assert mode_initial_threshold(MODE_BALANCED) > mode_initial_threshold(MODE_ACTIVE)
        assert mode_initial_threshold(MODE_ACTIVE) > mode_initial_threshold(MODE_AGGRESSIVE)

    def test_round_trip_caps_increase_with_aggressiveness(self):
        assert mode_max_round_trips(MODE_SAFE) < mode_max_round_trips(MODE_AGGRESSIVE)

    def test_early_entry_ladder_only_active_and_aggressive_at_54_59(self):
        assert calculate_entry_position_pct(56.0, MODE_ACTIVE) == 15.0
        assert calculate_entry_position_pct(56.0, MODE_AGGRESSIVE) == 15.0
        assert calculate_entry_position_pct(56.0, MODE_SAFE) == 0.0
        assert calculate_entry_position_pct(56.0, MODE_BALANCED) == 0.0

    def test_entry_ladder_scales_with_probability(self):
        assert calculate_entry_position_pct(62.0, MODE_ACTIVE) == 25.0
        assert calculate_entry_position_pct(70.0, MODE_ACTIVE) == 40.0
        assert calculate_entry_position_pct(78.0, MODE_ACTIVE) == 60.0
        assert calculate_entry_position_pct(90.0, MODE_ACTIVE) == 80.0

    def test_scale_up_targets(self):
        assert calculate_scale_up_target_pct(66.0) == 40.0
        assert calculate_scale_up_target_pct(73.0) == 60.0
        assert calculate_scale_up_target_pct(81.0) == 80.0
        assert calculate_scale_up_target_pct(91.0) == 90.0
        assert calculate_scale_up_target_pct(50.0) == 0.0

    def test_daily_loss_limit_forces_liquidation(self):
        result = daily_pnl_position_scale(-2.6)
        assert result["force_liquidate"] is True
        assert result["entries_allowed"] is False

    def test_daily_profit_reduces_position_and_raises_threshold(self):
        result = daily_pnl_position_scale(2.1)
        assert result["max_position_pct"] == 50.0
        assert result["threshold_add"] == 3.0


class TestHoldStreakRelief:
    def test_five_holds_in_30min_relaxes_threshold(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        for i in range(5):
            state = update_hold_streak(state, has_position=False, action=ACTION_HOLD, now=_NOW + timedelta(minutes=i))
        eff = calculate_effective_threshold(MODE_ACTIVE, state["hold_streak_no_position"], 0.3, "BASE_BUILDING", 0.0)
        assert eff["relief_applied"] == 3.0
        assert eff["threshold"] == mode_initial_threshold(MODE_ACTIVE) - 3.0

    def test_no_relief_when_expected_move_too_small(self):
        eff = calculate_effective_threshold(MODE_ACTIVE, hold_streak=8, expected_move_pct=0.1, cycle_phase="BASE_BUILDING", daily_return_pct=0.0)
        assert eff["relief_applied"] == 0.0

    def test_no_relief_in_no_trade_phase(self):
        eff = calculate_effective_threshold(MODE_ACTIVE, hold_streak=8, expected_move_pct=0.3, cycle_phase="NO_TRADE", daily_return_pct=0.0)
        assert eff["relief_applied"] == 0.0

    def test_threshold_never_below_mode_floor(self):
        eff = calculate_effective_threshold(MODE_AGGRESSIVE, hold_streak=8, expected_move_pct=0.3, cycle_phase="BASE_BUILDING", daily_return_pct=0.0)
        assert eff["threshold"] >= 52.0


class TestEarlyEntryDecision:
    def test_no_entry_when_below_threshold_and_early_conditions_unmet(self):
        result = decide_active_strategy_action(**_base_kwargs(buy_probability=53.0, model_confidence=40.0))
        assert result["action"] == ACTION_HOLD

    def test_early_test_entry_in_54_to_59_band(self):
        result = decide_active_strategy_action(**_base_kwargs(
            buy_probability=56.0, inverse_probability=20.0, model_confidence=60.0, expected_move_pct=0.3,
            momentum_inflection_or_acceleration=60.0,
        ))
        assert result["action"] == ACTION_ENTER_HYNIX
        assert result["recommended_position_pct"] == 15.0

    def test_full_entry_above_threshold(self):
        result = decide_active_strategy_action(**_base_kwargs(buy_probability=62.0, inverse_probability=15.0))
        assert result["action"] == ACTION_ENTER_HYNIX
        assert result["recommended_position_pct"] > 0

    def test_no_trade_phase_blocks_entry(self):
        result = decide_active_strategy_action(**_base_kwargs(buy_probability=90.0, cycle_phase="NO_TRADE"))
        assert result["action"] == ACTION_HOLD
        assert "NO_TRADE" in result["blocking_reason"]

    def test_after_1500_blocks_new_entry(self):
        result = decide_active_strategy_action(**_base_kwargs(buy_probability=90.0, now=datetime(2026, 7, 13, 15, 5)))
        assert result["action"] == ACTION_HOLD
        assert "15:00" in result["blocking_reason"]

    def test_daily_loss_limit_blocks_entry(self):
        result = decide_active_strategy_action(**_base_kwargs(buy_probability=90.0, daily_return_pct=-2.6))
        assert result["action"] == ACTION_HOLD

    def test_max_round_trips_blocks_entry(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        state["_state_date"] = _NOW.strftime("%Y%m%d")
        state["round_trip_count_today"] = mode_max_round_trips(MODE_ACTIVE)
        result = decide_active_strategy_action(**_base_kwargs(buy_probability=90.0, strategy_state=state))
        assert result["action"] == ACTION_HOLD
        assert "왕복거래" in result["blocking_reason"]


class TestReentryCooldown:
    def test_blocked_within_5min_of_normal_exit(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        state["_state_date"] = _NOW.strftime("%Y%m%d")
        state = register_position_closed(state, was_stop_loss=False, now=_NOW)
        result = decide_active_strategy_action(**_base_kwargs(
            buy_probability=90.0, now=_NOW + timedelta(minutes=2), strategy_state=state,
        ))
        assert result["action"] == ACTION_HOLD
        assert "재진입 쿨다운" in result["blocking_reason"]

    def test_blocked_15min_after_stop_loss_exit(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        state["_state_date"] = _NOW.strftime("%Y%m%d")
        state = register_position_closed(state, was_stop_loss=True, now=_NOW)
        result = decide_active_strategy_action(**_base_kwargs(
            buy_probability=90.0, now=_NOW + timedelta(minutes=10), strategy_state=state,
        ))
        assert result["action"] == ACTION_HOLD

    def test_allowed_after_cooldown_elapses(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        state["_state_date"] = _NOW.strftime("%Y%m%d")
        state = register_position_closed(state, was_stop_loss=False, now=_NOW)
        result = decide_active_strategy_action(**_base_kwargs(
            buy_probability=90.0, now=_NOW + timedelta(minutes=6), strategy_state=state,
        ))
        assert result["action"] == ACTION_ENTER_HYNIX


class TestFastSwitchAndExit:
    def test_strong_opposite_signal_switches_position(self):
        position_state = {"symbol": "000660", "quantity": 100, "entry_price": 100000.0}
        result = decide_active_strategy_action(**_base_kwargs(
            buy_probability=20.0, inverse_probability=80.0, position_state=position_state,
            down_turn_probability_3m=70.0,
        ))
        assert result["action"] in (ACTION_SWITCH, ACTION_SCALE_OUT_PARTIAL)

    def test_holding_with_no_strong_signal_stays_held(self):
        position_state = {"symbol": "000660", "quantity": 100, "entry_price": 100000.0}
        result = decide_active_strategy_action(**_base_kwargs(
            buy_probability=55.0, inverse_probability=45.0, position_state=position_state,
        ))
        assert result["action"] == ACTION_HOLD


class TestScaleIn:
    def test_rejected_before_90_seconds(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        state = register_position_opened(state, "000660", 100000.0, 15.0, _NOW)
        result = evaluate_scale_in(
            _NOW + timedelta(seconds=30), {"symbol": "000660"}, state,
            current_probability=70.0, opposite_probability=20.0, momentum_continuing=True, current_price=100200.0,
        )
        assert result["approved"] is False

    def test_approved_after_90_seconds_with_good_conditions(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        state = register_position_opened(state, "000660", 100000.0, 15.0, _NOW)
        result = evaluate_scale_in(
            _NOW + timedelta(seconds=100), {"symbol": "000660"}, state,
            current_probability=70.0, opposite_probability=20.0, momentum_continuing=True, current_price=100200.0,
        )
        assert result["approved"] is True
        assert result["target_pct"] == 40.0

    def test_rejected_on_adverse_move(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        state = register_position_opened(state, "000660", 100000.0, 15.0, _NOW)
        result = evaluate_scale_in(
            _NOW + timedelta(seconds=100), {"symbol": "000660"}, state,
            current_probability=70.0, opposite_probability=20.0, momentum_continuing=True, current_price=99100.0,
        )
        assert result["approved"] is False

    def test_rejected_when_max_scale_ins_reached(self):
        state = default_active_strategy_state(MODE_ACTIVE)
        state = register_position_opened(state, "000660", 100000.0, 15.0, _NOW)
        state["scale_in_count"] = 3
        result = evaluate_scale_in(
            _NOW + timedelta(seconds=200), {"symbol": "000660"}, state,
            current_probability=90.0, opposite_probability=10.0, momentum_continuing=True, current_price=100500.0,
        )
        assert result["approved"] is False


class TestPositionSizingAI:
    def test_expected_value_positive_for_high_probability(self):
        ev = calculate_expected_value(80.0, expected_profit_pct=1.0, expected_loss_pct=0.5)
        assert ev > 0

    def test_expected_value_negative_for_low_probability(self):
        ev = calculate_expected_value(30.0, expected_profit_pct=0.5, expected_loss_pct=1.0)
        assert ev < 0

    def test_zero_position_when_expected_value_non_positive(self):
        ai = PositionSizingAI()
        result = ai.recommend_position_size(
            buy_or_inverse_probability=20.0, confidence=50.0, expected_move_pct=0.1, base_position_pct=50.0,
        )
        assert result["recommended_position_pct"] == 0.0
        assert result["expected_value"] <= 0

    def test_consecutive_stop_losses_reduce_or_block_entry(self):
        ai = PositionSizingAI()
        normal = ai.recommend_position_size(
            buy_or_inverse_probability=80.0, confidence=70.0, expected_move_pct=1.0, base_position_pct=50.0,
        )
        after_two_sl = ai.recommend_position_size(
            buy_or_inverse_probability=80.0, confidence=70.0, expected_move_pct=1.0, base_position_pct=50.0,
            consecutive_stop_losses=2,
        )
        after_three_sl = ai.recommend_position_size(
            buy_or_inverse_probability=80.0, confidence=70.0, expected_move_pct=1.0, base_position_pct=50.0,
            consecutive_stop_losses=3,
        )
        assert after_two_sl["recommended_position_pct"] < normal["recommended_position_pct"]
        assert after_three_sl["recommended_position_pct"] == 0.0
