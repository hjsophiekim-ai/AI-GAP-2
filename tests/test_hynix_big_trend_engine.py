"""test_hynix_big_trend_engine.py — Big Trend Holding Engine 단위/통합 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

import app.trading.hynix_big_trend_engine as bte


def _downtrend_df_1min(n=30, start_price=10_000.0):
    rows = []
    now = datetime(2026, 7, 14, 9, 30)
    price = start_price
    for i in range(n):
        price *= 0.999
        rows.append({
            "datetime": now + timedelta(minutes=i), "open": price * 1.001, "high": price * 1.002,
            "low": price * 0.998, "close": price, "volume": 1000 + i * 5,
        })
    return pd.DataFrame(rows)


def _strong_bearish_features(inverse_probability=70.0):
    return {
        "minutes_below_vwap": 12, "ema5": 90.0, "ema10": 95.0, "ema20": 100.0,
        "lower_high_count_last3": 2, "lower_low_count_last3": 3,
        "macd_histogram": -0.5, "macd_histogram_prev": -0.3,
        "rsi_14": 35.0, "down_volume_increasing": True,
        "inverse_probability": inverse_probability, "hynix_probability": 100 - inverse_probability,
        "relative_volume": 1.8, "atr_pct": 1.2,
    }


def _strong_bullish_features(hynix_probability=70.0):
    return {
        "minutes_above_vwap": 12, "ema5": 110.0, "ema10": 105.0, "ema20": 100.0,
        "higher_high_count_last3": 2, "higher_low_count_last3": 3,
        "macd_histogram": 0.5, "macd_histogram_prev": 0.3,
        "rsi_14": 65.0, "up_volume_increasing": True,
        "hynix_probability": hynix_probability, "inverse_probability": 100 - hynix_probability,
        "relative_volume": 1.8, "atr_pct": 1.2,
    }


def _neutral_features():
    return {
        "minutes_below_vwap": 3, "ema5": 100.0, "ema10": 100.1, "ema20": 99.9,
        "lower_high_count_last3": 1, "lower_low_count_last3": 1,
        "macd_histogram": 0.01, "macd_histogram_prev": 0.02,
        "rsi_14": 50.0, "inverse_probability": 50.0, "hynix_probability": 50.0,
        "relative_volume": 1.0, "atr_pct": 0.8,
    }


class TestTrendStrengthAndDirection:
    def test_strong_bearish_features_yield_inverse_direction(self):
        result = bte.compute_trend_strength_score(_strong_bearish_features())
        assert result["dominant_direction"] == bte.DIRECTION_INVERSE
        assert result["trend_strength_score"] >= 65.0

    def test_strong_bullish_features_yield_hynix_direction(self):
        result = bte.compute_trend_strength_score(_strong_bullish_features())
        assert result["dominant_direction"] == bte.DIRECTION_HYNIX
        assert result["trend_strength_score"] >= 65.0

    def test_neutral_features_yield_neutral_direction(self):
        result = bte.compute_trend_strength_score(_neutral_features())
        assert result["dominant_direction"] == bte.DIRECTION_NEUTRAL

    def test_missing_data_conditions_are_excluded_not_assumed(self):
        result = bte.compute_trend_strength_score({"inverse_probability": 70.0, "hynix_probability": 30.0})
        assert result["bearish_total"] == 1  # 확률 조건 하나만 평가됨
        assert 0.0 <= result["trend_strength_score"] <= 100.0


class TestTrendPersistence:
    def test_persistent_downtrend_scores_high(self):
        score = bte.compute_trend_persistence_score(_strong_bearish_features(), bte.DIRECTION_INVERSE)
        assert score >= 60.0

    def test_fresh_neutral_scores_lower(self):
        score = bte.compute_trend_persistence_score(_neutral_features(), bte.DIRECTION_NEUTRAL)
        assert score == 30.0


class TestRegimeClassification:
    def test_strong_trend_when_strength_and_persistence_high(self):
        regime = bte.classify_trend_regime(80.0, 70.0, reversal_probability_5m=20.0, relative_volume=1.5, atr_pct=1.0)
        assert regime == bte.REGIME_STRONG_TREND

    def test_range_when_strength_low(self):
        regime = bte.classify_trend_regime(40.0, 40.0, reversal_probability_5m=20.0, relative_volume=1.0, atr_pct=0.5)
        assert regime == bte.REGIME_RANGE

    def test_whipsaw_when_direction_flips_repeatedly(self):
        regime = bte.classify_trend_regime(80.0, 70.0, reversal_probability_5m=20.0, relative_volume=1.5, atr_pct=1.0, recent_direction_flip_count=3)
        assert regime == bte.REGIME_WHIPSAW

    def test_reversal_risk_when_persistence_low_but_reversal_prob_high(self):
        regime = bte.classify_trend_regime(60.0, 40.0, reversal_probability_5m=60.0, relative_volume=1.0, atr_pct=1.0)
        assert regime == bte.REGIME_REVERSAL_RISK

    def test_panic_overrides_other_signals(self):
        regime = bte.classify_trend_regime(80.0, 70.0, reversal_probability_5m=10.0, relative_volume=3.0, atr_pct=2.0, is_panic_signal=True)
        assert regime == bte.REGIME_PANIC


class TestHysteresis:
    def test_entry_requires_high_probability_and_strength(self):
        assert bte.entry_gate_ok(62.0, 65.0) is True
        assert bte.entry_gate_ok(55.0, 65.0) is False

    def test_hold_survives_moderate_probability_drop(self):
        # 진입 시 62%였던 확률이 55%로 내려와도 유지 가능해야 한다(사용자 예시).
        assert bte.hold_gate_ok(55.0, 50.0) is True  # probability>=48
        assert bte.hold_gate_ok(40.0, 65.0) is True  # persistence>=60
        assert bte.hold_gate_ok(40.0, 50.0) is False

    def test_exit_gate_triggers_on_opposite_probability(self):
        assert bte.exit_gate_triggered(66.0, None, None) is True

    def test_exit_gate_triggers_on_reversal_probability_5m(self):
        assert bte.exit_gate_triggered(50.0, 70.0, None) is True

    def test_exit_gate_does_not_trigger_on_weak_signals(self):
        assert bte.exit_gate_triggered(50.0, 40.0, 50.0) is False


class TestReversalConfirmation:
    def test_confirmed_when_three_or_more_true(self):
        signals = {
            "opposite_probability_high": True, "vwap_opposite_break_confirmed": True,
            "structure_broken": True, "macd_flip_3_consecutive": False,
        }
        result = bte.count_reversal_confirmations(signals)
        assert result["confirmed"] is True
        assert result["matched"] == 3

    def test_not_confirmed_when_only_two_true(self):
        signals = {"opposite_probability_high": True, "vwap_opposite_break_confirmed": True, "structure_broken": False}
        result = bte.count_reversal_confirmations(signals)
        assert result["confirmed"] is False

    def test_missing_data_not_counted_as_confirmed_or_denied(self):
        result = bte.count_reversal_confirmations({})
        assert result["evaluated_count"] == 0
        assert result["confirmed"] is False


class TestProfitLock:
    @pytest.mark.parametrize("net_return,expected_floor", [
        (0.5, None), (0.7, -0.3), (1.2, 0.0), (2.0, 0.8), (3.0, 1.8),
        (5.0, 3.5), (8.0, 6.0), (12.0, 9.0), (15.0, 12.0), (20.0, 12.0),
    ])
    def test_profit_lock_ladder(self, net_return, expected_floor):
        assert bte.compute_profit_lock_floor_pct(net_return) == expected_floor


class TestAdaptiveTrailing:
    def test_strong_trend_trailing_wider_than_range(self):
        assert bte.regime_trailing_pct(bte.REGIME_STRONG_TREND) > bte.regime_trailing_pct(bte.REGIME_RANGE)

    def test_atr_based_trailing_scales_with_atr(self):
        assert bte.atr_based_trailing_pct(2.0) > bte.atr_based_trailing_pct(0.5)

    def test_effective_trailing_uses_max_of_regime_and_atr(self):
        result = bte.compute_effective_trailing_pct(bte.REGIME_RANGE, atr_pct=3.0, profit_lock_floor_pct=None, net_return_pct=5.0)
        assert result >= bte.atr_based_trailing_pct(3.0) - 0.001

    def test_trailing_capped_by_profit_lock_floor(self):
        # net_return=2.0, floor=0.8 → 하락 허용폭은 최대 1.2%p여야 한다.
        result = bte.compute_effective_trailing_pct(bte.REGIME_STRONG_TREND, atr_pct=0.1, profit_lock_floor_pct=0.8, net_return_pct=2.0)
        assert result <= 1.2 + 0.001


class TestStopLossLadder:
    def test_normal_sl_is_1_5pct(self):
        assert bte.effective_sl_pct("NORMAL") == -1.5

    def test_strong_trend_initial_uses_wider_sl(self):
        assert bte.effective_sl_pct("NORMAL", is_strong_trend_initial_phase=True) == -1.8

    def test_preemptive_reduce_requires_three_of_four_within_3min(self):
        assert bte.should_preemptive_reduce(held_minutes=2.0, warning_count=3) is True
        assert bte.should_preemptive_reduce(held_minutes=2.0, warning_count=2) is False
        assert bte.should_preemptive_reduce(held_minutes=5.0, warning_count=4) is False

    def test_early_reversal_warning_count(self):
        count = bte.count_early_reversal_warnings(
            opposite_probability_delta=25.0, trend_strength_drop=25.0, vwap_broken_opposite=True, order_flow_reversed=None,
        )
        assert count == 3


class TestProfitGiveback:
    def test_no_violation_within_allowed_band(self):
        assert bte.giveback_forced_exit_ratio(peak_net_return_pct=10.0, current_net_return_pct=9.0) == 0.0

    def test_violation_at_peak10_drop_to_7_5(self):
        ratio = bte.giveback_forced_exit_ratio(peak_net_return_pct=10.0, current_net_return_pct=7.4)
        assert ratio >= 0.50

    def test_violation_at_peak15_drop_to_12(self):
        ratio = bte.giveback_forced_exit_ratio(peak_net_return_pct=15.0, current_net_return_pct=11.9)
        assert ratio >= 0.70


class TestPositionSizing:
    @pytest.mark.parametrize("strength,expected", [(50.0, 0.0), (60.0, 20.0), (70.0, 40.0), (80.0, 60.0), (90.0, 80.0), (95.0, 90.0)])
    def test_position_sizing_ladder(self, strength, expected):
        assert bte.position_pct_from_trend_strength(strength) == expected

    def test_scale_in_blocked_when_losing(self):
        assert bte.scale_in_allowed(currently_profitable=False, trend_strengthening=True, pullback_then_rebreak=True, seconds_since_last_entry=300.0) is False

    def test_scale_in_blocked_within_3_minutes(self):
        assert bte.scale_in_allowed(currently_profitable=True, trend_strengthening=True, pullback_then_rebreak=False, seconds_since_last_entry=100.0) is False

    def test_scale_in_allowed_when_profitable_and_strengthening(self):
        assert bte.scale_in_allowed(currently_profitable=True, trend_strengthening=True, pullback_then_rebreak=False, seconds_since_last_entry=200.0) is True


class TestStagedSwitch:
    def test_first_confirmation_triggers_partial_close(self):
        signals = {
            "target_probability_high": True, "reversal_probability_5m_high": True,
            "vwap_recovered_2bars": True, "structure_confirmed": True,
        }
        now = datetime(2026, 7, 14, 10, 0)
        result = bte.evaluate_staged_switch(bte.default_switch_state(), signals, "HYNIX", now)
        assert result["action"] == "PARTIAL_CLOSE_50"
        assert result["state"]["awaiting_final_confirmation"] is True

    def test_second_confirmation_after_45s_triggers_full_close(self):
        signals = {
            "target_probability_high": True, "reversal_probability_5m_high": True,
            "vwap_recovered_2bars": True, "structure_confirmed": True,
        }
        now = datetime(2026, 7, 14, 10, 0)
        first = bte.evaluate_staged_switch(bte.default_switch_state(), signals, "HYNIX", now)
        second = bte.evaluate_staged_switch(first["state"], signals, "HYNIX", now + timedelta(seconds=50))
        assert second["action"] == "FULL_CLOSE_AND_TRIAL_ENTRY"

    def test_waiting_confirmation_returns_none_before_next_bar(self):
        signals = {
            "target_probability_high": True, "reversal_probability_5m_high": True,
            "vwap_recovered_2bars": True, "structure_confirmed": True,
        }
        now = datetime(2026, 7, 14, 10, 0)
        first = bte.evaluate_staged_switch(bte.default_switch_state(), signals, "HYNIX", now)
        second = bte.evaluate_staged_switch(first["state"], signals, "HYNIX", now + timedelta(seconds=10))
        assert second["action"] == "NONE"

    def test_only_two_conditions_does_not_trigger(self):
        signals = {"target_probability_high": True, "reversal_probability_5m_high": True}
        now = datetime(2026, 7, 14, 10, 0)
        result = bte.evaluate_staged_switch(bte.default_switch_state(), signals, "HYNIX", now)
        assert result["action"] == "NONE"


class TestDecideTrendHoldAction:
    def _base_kwargs(self, **overrides):
        base = dict(
            held_symbol="0197X0", net_return_pct=1.0, peak_net_return_pct=1.0,
            regime=bte.REGIME_STRONG_TREND, trend_strength_score=80.0, trend_persistence_score=70.0,
            probability_for_direction=70.0, opposite_probability=20.0,
            reversal_probability_5m=20.0, exit_confidence=30.0,
            profit_lock_floor_pct=None, hard_stop_triggered=False,
            reversal_confirmed=False, first_tp_taken=False,
        )
        base.update(overrides)
        return base

    def test_hard_stop_always_wins(self):
        result = bte.decide_trend_hold_action(**self._base_kwargs(hard_stop_triggered=True, net_return_pct=10.0))
        assert result["action"] == bte.ACTION_EXIT_ALL

    def test_profit_lock_violation_forces_exit(self):
        result = bte.decide_trend_hold_action(**self._base_kwargs(net_return_pct=0.5, profit_lock_floor_pct=0.8))
        assert result["action"] == bte.ACTION_EXIT_ALL

    def test_reversal_confirmed_forces_exit(self):
        result = bte.decide_trend_hold_action(**self._base_kwargs(reversal_confirmed=True))
        assert result["action"] == bte.ACTION_EXIT_ALL

    def test_strong_trend_partial_tp_at_3pct_is_only_25pct(self):
        result = bte.decide_trend_hold_action(**self._base_kwargs(net_return_pct=3.2))
        assert result["action"] == bte.ACTION_TAKE_PROFIT_25
        assert result["tp_ratio"] == pytest.approx(0.25)

    def test_no_fixed_full_exit_at_3pct_for_strong_trend(self):
        """핵심 요구사항 — 강한 추세장에서는 +3%에서 전량청산하지 않는다."""
        result = bte.decide_trend_hold_action(**self._base_kwargs(net_return_pct=3.2))
        assert result["action"] != bte.ACTION_EXIT_ALL

    def test_normal_trend_partial_tp_at_3pct_is_45pct(self):
        result = bte.decide_trend_hold_action(**self._base_kwargs(regime=bte.REGIME_NORMAL_TREND, net_return_pct=3.1))
        assert result["action"] == bte.ACTION_TAKE_PROFIT_50
        assert result["tp_ratio"] == pytest.approx(0.45)

    def test_range_regime_takes_profit_early_and_fully(self):
        result = bte.decide_trend_hold_action(**self._base_kwargs(regime=bte.REGIME_RANGE, net_return_pct=2.1))
        assert result["action"] == bte.ACTION_TAKE_PROFIT_50
        assert result["tp_ratio"] == pytest.approx(0.85)

    def test_holds_full_when_persistence_and_probability_strong(self):
        result = bte.decide_trend_hold_action(**self._base_kwargs(net_return_pct=1.0))
        assert result["action"] == bte.ACTION_HOLD_FULL

    def test_small_opposite_signal_does_not_exit_strong_trend(self):
        """핵심 요구사항 — 작은 반대 신호 하나만으로 청산하지 않는다."""
        result = bte.decide_trend_hold_action(**self._base_kwargs(
            net_return_pct=1.5, opposite_probability=50.0, reversal_probability_5m=40.0, exit_confidence=50.0,
        ))
        assert result["action"] != bte.ACTION_EXIT_ALL

    def test_panic_regime_has_no_fixed_tp(self):
        result = bte.decide_trend_hold_action(**self._base_kwargs(regime=bte.REGIME_PANIC, net_return_pct=10.0))
        assert result["action"] != bte.ACTION_TAKE_PROFIT_25
        assert result["action"] != bte.ACTION_TAKE_PROFIT_50


class TestRegimeStability:
    def test_first_regime_confirms_immediately(self):
        now = datetime(2026, 7, 14, 10, 0)
        state = bte.update_regime_state(bte.default_regime_state(), bte.REGIME_STRONG_TREND, confidence=90.0, now=now)
        assert state["confirmed_regime"] == bte.REGIME_STRONG_TREND
        assert state["transition_count"] == 0

    def test_single_bar_candidate_does_not_confirm_transition(self):
        now = datetime(2026, 7, 14, 10, 0)
        state = bte.update_regime_state(bte.default_regime_state(), bte.REGIME_STRONG_TREND, confidence=90.0, now=now)
        state2 = bte.update_regime_state(state, bte.REGIME_RANGE, confidence=50.0, now=now + timedelta(minutes=1))
        assert state2["confirmed_regime"] == bte.REGIME_STRONG_TREND  # 아직 전환 안됨
        assert state2["candidate_regime"] == bte.REGIME_RANGE
        assert state2["candidate_bar_count"] == 1

    def test_two_consecutive_bars_confirms_transition(self):
        now = datetime(2026, 7, 14, 10, 0)
        state = bte.update_regime_state(bte.default_regime_state(), bte.REGIME_STRONG_TREND, confidence=90.0, now=now)
        state = bte.update_regime_state(state, bte.REGIME_RANGE, confidence=50.0, now=now + timedelta(minutes=1))
        state = bte.update_regime_state(state, bte.REGIME_RANGE, confidence=50.0, now=now + timedelta(minutes=2))
        assert state["confirmed_regime"] == bte.REGIME_RANGE
        assert state["transition_count"] == 1

    def test_high_confidence_confirms_immediately(self):
        now = datetime(2026, 7, 14, 10, 0)
        state = bte.update_regime_state(bte.default_regime_state(), bte.REGIME_STRONG_TREND, confidence=90.0, now=now)
        state = bte.update_regime_state(state, bte.REGIME_RANGE, confidence=85.0, now=now + timedelta(minutes=1))
        assert state["confirmed_regime"] == bte.REGIME_RANGE

    def test_completed_3min_bar_confirms_immediately(self):
        now = datetime(2026, 7, 14, 10, 0)
        state = bte.update_regime_state(bte.default_regime_state(), bte.REGIME_STRONG_TREND, confidence=90.0, now=now)
        state = bte.update_regime_state(state, bte.REGIME_RANGE, confidence=50.0, now=now + timedelta(minutes=1), bar_completed_3min=True)
        assert state["confirmed_regime"] == bte.REGIME_RANGE

    def test_repeated_transition_within_2min_forces_whipsaw(self):
        now = datetime(2026, 7, 14, 10, 0)
        state = bte.update_regime_state(bte.default_regime_state(), bte.REGIME_STRONG_TREND, confidence=90.0, now=now)
        # 1차 전환(고confidence로 즉시 확정) — STRONG_TREND -> RANGE
        state = bte.update_regime_state(state, bte.REGIME_RANGE, confidence=85.0, now=now + timedelta(seconds=30))
        assert state["confirmed_regime"] == bte.REGIME_RANGE
        # 2분 이내 2차 전환 -> WHIPSAW로 강제 분류
        state = bte.update_regime_state(state, bte.REGIME_STRONG_TREND, confidence=85.0, now=now + timedelta(seconds=60))
        assert state["confirmed_regime"] == bte.REGIME_WHIPSAW

    def test_regime_duration_computed(self):
        now = datetime(2026, 7, 14, 10, 0)
        state = bte.update_regime_state(bte.default_regime_state(), bte.REGIME_STRONG_TREND, confidence=90.0, now=now)
        duration = bte.regime_duration_seconds(state, now + timedelta(minutes=5))
        assert duration == pytest.approx(300.0)


class TestRegimeTransitionAction:
    def test_strong_trend_to_range_reduces_position(self):
        action = bte.compute_regime_transition_action(bte.REGIME_STRONG_TREND, bte.REGIME_RANGE)
        assert action["action"] == "REDUCE_POSITION"
        assert action["reduce_ratio"] == pytest.approx(0.4)

    def test_range_to_strong_trend_holds_and_relaxes(self):
        action = bte.compute_regime_transition_action(bte.REGIME_RANGE, bte.REGIME_STRONG_TREND)
        assert action["action"] == "HOLD_AND_RELAX"
        assert action.get("remove_fixed_tp") is True

    def test_normal_trend_to_whipsaw_reduces_half(self):
        action = bte.compute_regime_transition_action(bte.REGIME_NORMAL_TREND, bte.REGIME_WHIPSAW)
        assert action["action"] == "REDUCE_POSITION"
        assert action["reduce_ratio"] == pytest.approx(0.5)

    def test_same_regime_no_action(self):
        action = bte.compute_regime_transition_action(bte.REGIME_STRONG_TREND, bte.REGIME_STRONG_TREND)
        assert action["action"] == "NONE"

    def test_none_old_regime_no_action(self):
        action = bte.compute_regime_transition_action(None, bte.REGIME_STRONG_TREND)
        assert action["action"] == "NONE"


class TestHynixBigTrendEngineIntegration:
    def test_strong_downtrend_produces_hold_full_with_low_position_when_no_position(self):
        engine = bte.HynixBigTrendEngine()
        result = engine.compute(
            features=_strong_bearish_features(inverse_probability=75.0),
            held_symbol=None, entry_price=None, current_price=10_000.0,
            net_return_pct=0.0, peak_net_return_pct=0.0,
            reversal_probability_3m=10.0, reversal_probability_5m=15.0, reversal_probability_15m=20.0,
            reversal_signals={},
        )
        assert result["dominant_direction"] == bte.DIRECTION_INVERSE
        assert result["max_position_pct"] > 0
        assert result["final_hold_action"] is None  # 무포지션 — 신규진입 판단은 이 엔진 밖(호출부)

    def test_holding_position_in_strong_trend_holds_full_below_first_tp(self):
        engine = bte.HynixBigTrendEngine()
        result = engine.compute(
            features=_strong_bearish_features(inverse_probability=75.0),
            held_symbol="0197X0", entry_price=10_000.0, current_price=10_150.0,
            net_return_pct=1.4, peak_net_return_pct=1.4,
            reversal_probability_3m=10.0, reversal_probability_5m=15.0, reversal_probability_15m=20.0,
            reversal_signals={},
        )
        assert result["trend_regime"] == bte.REGIME_STRONG_TREND
        assert result["final_hold_action"] == bte.ACTION_HOLD_FULL

    def test_log_big_trend_decision_writes_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bte, "_LOG_PATH", tmp_path / "hynix_big_trend_log.csv")
        bte.log_big_trend_decision({"timestamp": "2026-07-14T10:00:00", "symbol": "0197X0", "recommended_action": "HOLD_FULL"})
        assert (tmp_path / "hynix_big_trend_log.csv").exists()
        content = (tmp_path / "hynix_big_trend_log.csv").read_text(encoding="utf-8-sig")
        assert "HOLD_FULL" in content


class TestBuildFeaturesFromRealData:
    def test_downtrend_data_produces_bearish_features(self):
        df = _downtrend_df_1min()
        snapshot = {"macd_histogram": -0.3, "macd_histogram_prev": -0.1, "rsi_14": 35.0, "relative_volume": 1.5, "atr_14_pct": 1.0}
        features = bte.build_big_trend_features(df, snapshot, inverse_probability=70.0, hynix_probability=30.0)
        assert features["minutes_below_vwap"] is not None and features["minutes_below_vwap"] > 0
        assert features["ema5"] < features["ema20"]
        result = bte.compute_trend_strength_score(features)
        assert result["dominant_direction"] == bte.DIRECTION_INVERSE

    def test_empty_df_degrades_gracefully(self):
        snapshot = {"macd_histogram": None, "macd_histogram_prev": None, "rsi_14": None, "relative_volume": None, "atr_14_pct": None}
        features = bte.build_big_trend_features(None, snapshot, inverse_probability=None, hynix_probability=None)
        assert "ema5" not in features
        result = bte.compute_trend_strength_score(features)
        assert result["dominant_direction"] == bte.DIRECTION_NEUTRAL

    def test_build_reversal_signals_evaluates_available_conditions(self):
        df = _downtrend_df_1min()
        snapshot = {"macd_histogram": -0.3, "macd_histogram_prev": -0.1, "rsi_14": 35.0, "relative_volume": 1.5, "atr_14_pct": 1.0}
        features = bte.build_big_trend_features(df, snapshot, inverse_probability=70.0, hynix_probability=30.0)
        signals = bte.build_reversal_signals(features, bte.DIRECTION_INVERSE, prediction_v2_action="BUY", cycle_phase="TREND_UP")
        assert signals["order_flow_reversed"] is None  # 데이터 없음 — 미확인 유지
        assert signals["opposite_probability_high"] is not None
