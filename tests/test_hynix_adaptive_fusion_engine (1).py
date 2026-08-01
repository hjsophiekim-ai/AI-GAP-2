"""test_hynix_adaptive_fusion_engine.py — Adaptive Fusion Engine 단위/통합 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

import app.trading.hynix_adaptive_fusion_engine as afe


@pytest.fixture(autouse=True)
def _isolate_pv2_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(afe, "_PV2_LOG_PATH", tmp_path / "prediction_v2_snapshot_log.csv")
    monkeypatch.setattr(afe, "_PV2_PENDING_PATH", tmp_path / "hynix_prediction_v2_pending.json")


def _model(hynix=50.0, inverse=50.0, hold=0.0, confidence=60.0, pct=20.0, quality=80.0, status=afe.MODEL_STATUS_LIVE_VALIDATED):
    return afe.build_model_result(
        model_name="X", hynix_probability=hynix, inverse_probability=inverse, hold_probability=hold,
        confidence=confidence, recommended_position_pct=pct, data_quality=quality, model_status=status,
    )


class TestBuildModelResult:
    def test_none_when_probabilities_missing(self):
        assert afe.build_model_result("X", None, None, None, 50, 0, 50, afe.MODEL_STATUS_SHADOW) is None

    def test_fills_hold_probability_when_missing(self):
        r = afe.build_model_result("X", 70.0, 20.0, None, 60.0, 30.0, 80.0, afe.MODEL_STATUS_LIVE_VALIDATED)
        assert r["hold_probability"] == pytest.approx(10.0)


class TestRenormalizeWeights:
    def test_missing_models_excluded_and_renormalized(self):
        results = {
            afe.MODEL_ACTIVE_FUSION: _model(status=afe.MODEL_STATUS_LIVE_VALIDATED),
            afe.MODEL_PREDICTION_V2: None,
            afe.MODEL_CYCLE_AI: None,
            afe.MODEL_EARLY_PREDICTION: None,
            afe.MODEL_MICRON_PROXY: None,
        }
        weights = afe.renormalize_weights(results)
        assert weights == {afe.MODEL_ACTIVE_FUSION: 1.0}

    def test_degraded_prediction_v2_gets_zero_weight_and_excluded(self):
        results = {
            afe.MODEL_ACTIVE_FUSION: _model(),
            afe.MODEL_PREDICTION_V2: _model(status=afe.MODEL_STATUS_DEGRADED),
        }
        weights = afe.renormalize_weights(results)
        assert afe.MODEL_PREDICTION_V2 not in weights
        assert weights[afe.MODEL_ACTIVE_FUSION] == pytest.approx(1.0)

    def test_all_five_present_sums_to_one(self):
        results = {
            afe.MODEL_ACTIVE_FUSION: _model(),
            afe.MODEL_PREDICTION_V2: _model(status=afe.MODEL_STATUS_LIVE_VALIDATED),
            afe.MODEL_CYCLE_AI: _model(status=afe.MODEL_STATUS_LIVE_VALIDATED),
            afe.MODEL_EARLY_PREDICTION: _model(status=afe.MODEL_STATUS_ADVISORY),
            afe.MODEL_MICRON_PROXY: _model(status=afe.MODEL_STATUS_ADVISORY),
        }
        weights = afe.renormalize_weights(results)
        assert sum(weights.values()) == pytest.approx(1.0)
        # 초기 가중치 그대로면 재정규화해도 비율 유지(ACTIVE 40% 최대)
        assert weights[afe.MODEL_ACTIVE_FUSION] == max(weights.values())

    def test_no_models_returns_empty(self):
        assert afe.renormalize_weights({afe.MODEL_ACTIVE_FUSION: None}) == {}


class TestFuseModelResults:
    def test_single_model_dominates(self):
        results = {afe.MODEL_ACTIVE_FUSION: _model(hynix=80.0, inverse=0.0, hold=20.0)}
        fused = afe.fuse_model_results(results)
        assert fused["fused_hynix_probability"] == pytest.approx(80.0)
        assert fused["final_action"] == afe.ACTION_HYNIX
        assert fused["dominant_model"] == afe.MODEL_ACTIVE_FUSION
        assert fused["model_agreement"] == pytest.approx(100.0)

    def test_disagreement_lowers_model_agreement(self):
        results = {
            afe.MODEL_ACTIVE_FUSION: _model(hynix=80.0, inverse=0.0, hold=20.0),
            afe.MODEL_PREDICTION_V2: _model(hynix=0.0, inverse=80.0, hold=20.0, status=afe.MODEL_STATUS_LIVE_VALIDATED),
        }
        fused = afe.fuse_model_results(results)
        assert fused["model_agreement"] < 100.0

    def test_empty_results_returns_hold(self):
        fused = afe.fuse_model_results({})
        assert fused["final_action"] == afe.ACTION_HOLD
        assert fused["weights"] == {}


class TestConflictResolution:
    def test_case_a_prediction_hold_reduces_but_allows(self):
        fused = {"final_action": afe.ACTION_INVERSE, "fused_hynix_probability": 20.0, "fused_inverse_probability": 80.0}
        result = afe.apply_conflict_resolution(fused, active_action=afe.ACTION_INVERSE, prediction_v2_action=afe.ACTION_HOLD, cycle_phase=None, base_position_pct=40.0)
        assert result["position_pct"] == pytest.approx(30.0)
        assert not result["force_hold"]

    def test_case_b_small_diff_forces_hold(self):
        fused = {"final_action": afe.ACTION_INVERSE, "fused_hynix_probability": 47.0, "fused_inverse_probability": 53.0}
        result = afe.apply_conflict_resolution(fused, active_action=afe.ACTION_INVERSE, prediction_v2_action=afe.ACTION_HYNIX, cycle_phase=None, base_position_pct=40.0)
        assert result["force_hold"] is True

    def test_case_b_medium_diff_trial_entry_only(self):
        fused = {"final_action": afe.ACTION_INVERSE, "fused_hynix_probability": 42.0, "fused_inverse_probability": 58.0}
        result = afe.apply_conflict_resolution(fused, active_action=afe.ACTION_INVERSE, prediction_v2_action=afe.ACTION_HYNIX, cycle_phase=None, base_position_pct=40.0)
        assert result["position_pct"] == pytest.approx(10.0)
        assert not result["force_hold"]

    def test_case_b_large_diff_allows_20_to_30(self):
        fused = {"final_action": afe.ACTION_INVERSE, "fused_hynix_probability": 20.0, "fused_inverse_probability": 80.0}
        result = afe.apply_conflict_resolution(fused, active_action=afe.ACTION_INVERSE, prediction_v2_action=afe.ACTION_HYNIX, cycle_phase=None, base_position_pct=40.0)
        assert 20.0 <= result["position_pct"] <= 30.0

    def test_case_c_same_direction_expands(self):
        fused = {"final_action": afe.ACTION_INVERSE, "fused_hynix_probability": 20.0, "fused_inverse_probability": 80.0}
        result = afe.apply_conflict_resolution(fused, active_action=afe.ACTION_INVERSE, prediction_v2_action=afe.ACTION_INVERSE, cycle_phase=None, base_position_pct=40.0)
        assert result["position_pct"] > 40.0

    def test_case_d_no_trade_reduces_and_penalizes_confidence(self):
        fused = {"final_action": afe.ACTION_HYNIX, "fused_hynix_probability": 70.0, "fused_inverse_probability": 30.0}
        result = afe.apply_conflict_resolution(fused, active_action=None, prediction_v2_action=None, cycle_phase="NO_TRADE", base_position_pct=40.0)
        assert result["position_pct"] == pytest.approx(28.0)
        assert result["confidence_adjust"] == pytest.approx(-8.0)

    def test_case_e_gap_failure_boosts_inverse(self):
        fused = {"final_action": afe.ACTION_INVERSE, "fused_hynix_probability": 20.0, "fused_inverse_probability": 80.0}
        result = afe.apply_conflict_resolution(fused, active_action=None, prediction_v2_action=None, cycle_phase="GAP_FAILURE", base_position_pct=40.0)
        assert result["position_pct"] == pytest.approx(44.0)


class TestEntryLadder:
    # 2026-07-14 개편 — 단일 55% 게이트가 모델 의견 불일치 시 하루 종일 거래 0건을
    # 유발하는 문제가 실측되어, 52%부터 4단계로 세분화한 사다리로 교체됐다
    # (52~55%:10%, 55~60%:25%, 60~68%:45%, 68%+:65%, 52% 미만:0%=HOLD).
    @pytest.mark.parametrize("prob,expected", [
        (90.0, 65.0), (70.0, 65.0), (62.0, 45.0), (57.0, 25.0), (53.0, 10.0), (50.0, 0.0),
    ])
    def test_ladder_bands(self, prob, expected):
        assert afe.position_pct_from_probability_ladder(prob) == expected

    def test_entry_gate_blocks_low_probability(self):
        assert afe.entry_gate_ok(50.0, 30.0, 60.0, 0.3) is not None

    def test_entry_gate_blocks_high_opposite(self):
        assert afe.entry_gate_ok(60.0, 45.0, 60.0, 0.3) is not None

    def test_entry_gate_blocks_weak_expected_move(self):
        assert afe.entry_gate_ok(60.0, 30.0, 60.0, 0.05) is not None

    def test_entry_gate_passes(self):
        assert afe.entry_gate_ok(60.0, 30.0, 60.0, 0.3) is None


class TestHoldRelief:
    def test_relief_after_four_hold_cycles(self):
        tracker = afe.default_hold_tracker()
        now = datetime(2026, 7, 14, 10, 0)
        for i in range(4):
            tracker = afe.update_hold_tracker(tracker, has_position=False, action=afe.ACTION_HOLD, now=now + timedelta(minutes=i))
        result = afe.compute_threshold_relief(tracker, cycle_phase=None, confidence=60.0, opposite_probability=30.0, expected_move_5m_pct=0.3, data_quality=80.0, consecutive_stop_losses=0)
        assert result["relief"] == 2.0

    def test_relief_doubles_after_seven_holds(self):
        tracker = afe.default_hold_tracker()
        now = datetime(2026, 7, 14, 10, 0)
        for i in range(7):
            tracker = afe.update_hold_tracker(tracker, has_position=False, action=afe.ACTION_HOLD, now=now + timedelta(minutes=i))
        result = afe.compute_threshold_relief(tracker, cycle_phase=None, confidence=60.0, opposite_probability=30.0, expected_move_5m_pct=0.3, data_quality=80.0, consecutive_stop_losses=0)
        assert result["relief"] == 4.0

    def test_no_relief_when_two_consecutive_stop_losses(self):
        tracker = afe.default_hold_tracker()
        now = datetime(2026, 7, 14, 10, 0)
        for i in range(7):
            tracker = afe.update_hold_tracker(tracker, has_position=False, action=afe.ACTION_HOLD, now=now + timedelta(minutes=i))
        result = afe.compute_threshold_relief(tracker, cycle_phase=None, confidence=60.0, opposite_probability=30.0, expected_move_5m_pct=0.3, data_quality=80.0, consecutive_stop_losses=2)
        assert result["relief"] == 0.0
        assert result["relief_blocked"] is True

    def test_no_relief_when_data_quality_low(self):
        tracker = afe.default_hold_tracker()
        now = datetime(2026, 7, 14, 10, 0)
        for i in range(7):
            tracker = afe.update_hold_tracker(tracker, has_position=False, action=afe.ACTION_HOLD, now=now + timedelta(minutes=i))
        result = afe.compute_threshold_relief(tracker, cycle_phase=None, confidence=60.0, opposite_probability=30.0, expected_move_5m_pct=0.3, data_quality=40.0, consecutive_stop_losses=0)
        assert result["relief"] == 0.0

    def test_exploratory_entry_allowed_before_1330_with_zero_orders(self):
        tracker = afe.default_hold_tracker()
        now = datetime(2026, 7, 14, 13, 0)
        reason = afe.should_allow_exploratory_entry(tracker, now, orders_today_count=0, dominant_probability=56.0, confidence=56.0, expected_move_5m_pct=0.2, expected_value=0.05)
        assert reason is not None

    def test_exploratory_entry_blocked_after_1330(self):
        tracker = afe.default_hold_tracker()
        now = datetime(2026, 7, 14, 13, 31)
        reason = afe.should_allow_exploratory_entry(tracker, now, orders_today_count=0, dominant_probability=56.0, confidence=56.0, expected_move_5m_pct=0.2, expected_value=0.05)
        assert reason is None

    def test_exploratory_entry_blocked_if_orders_already_exist(self):
        tracker = afe.default_hold_tracker()
        now = datetime(2026, 7, 14, 13, 0)
        reason = afe.should_allow_exploratory_entry(tracker, now, orders_today_count=1, dominant_probability=56.0, confidence=56.0, expected_move_5m_pct=0.2, expected_value=0.05)
        assert reason is None


class TestExpectedValueSizing:
    def test_negative_ev_blocks_entry(self):
        ev = afe.calculate_expected_value(win_probability_pct=40.0, expected_profit_pct=1.0, expected_loss_pct=2.0)
        assert ev <= 0
        assert afe.position_pct_from_expected_value(ev) == 0.0

    def test_high_ev_gives_large_position(self):
        ev = afe.calculate_expected_value(win_probability_pct=80.0, expected_profit_pct=3.0, expected_loss_pct=1.0)
        assert ev >= 0.60
        assert afe.position_pct_from_expected_value(ev) == 85.0

    def test_final_position_takes_lower_of_probability_and_ev(self):
        result = afe.calculate_final_position_pct(probability_ladder_pct=85.0, expected_value=0.05)
        assert result["final_pct"] == pytest.approx(10.0)
        result2 = afe.calculate_final_position_pct(probability_ladder_pct=10.0, expected_value=0.9)
        assert result2["final_pct"] == pytest.approx(10.0)


class TestEarlyEntry:
    def test_hynix_early_entry_triggers_when_all_conditions_met(self):
        result = afe.evaluate_early_entry_hynix(
            momentum_inflection_up=65.0, up_probability_3m=60.0, up_probability_5m=62.0,
            down_probability_3m=35.0, recent_low_not_renewed=True, acceleration_improving=True,
            expected_move_5m_pct=0.2,
        )
        assert result is not None
        assert result["symbol"] == "0193T0"

    def test_hynix_early_entry_blocked_when_down_probability_too_high(self):
        result = afe.evaluate_early_entry_hynix(
            momentum_inflection_up=65.0, up_probability_3m=60.0, up_probability_5m=62.0,
            down_probability_3m=45.0, recent_low_not_renewed=True, acceleration_improving=True,
            expected_move_5m_pct=0.2,
        )
        assert result is None

    def test_inverse_early_entry_triggers(self):
        result = afe.evaluate_early_entry_inverse(
            momentum_inflection_down=65.0, down_probability_3m=60.0, down_probability_5m=62.0,
            up_probability_3m=35.0, recent_high_not_renewed_or_vwap_broken=True, expected_move_5m_pct=0.2,
        )
        assert result is not None
        assert result["symbol"] == "0197X0"

    def test_reevaluation_ladder_promotes_at_68(self):
        state = afe.default_early_entry_state()
        state["current_pct"] = 15.0
        now = datetime(2026, 7, 14, 10, 0)
        result = afe.reevaluate_early_entry(state, now, current_probability=70.0)
        assert result["target_pct"] == 35.0

    def test_reevaluation_ladder_exits_below_55(self):
        state = afe.default_early_entry_state()
        state["current_pct"] = 35.0
        now = datetime(2026, 7, 14, 10, 0)
        result = afe.reevaluate_early_entry(state, now, current_probability=50.0)
        assert result["target_pct"] == 0.0

    def test_reevaluation_skips_within_90_seconds(self):
        now = datetime(2026, 7, 14, 10, 0, 0)
        state = afe.default_early_entry_state()
        state["current_pct"] = 15.0
        state["last_reeval_at"] = now.isoformat()
        result = afe.reevaluate_early_entry(state, now + timedelta(seconds=30), current_probability=90.0)
        assert result["changed"] is False


class TestPreemptiveExit:
    def test_hynix_profit_lock_at_1_2_pct(self):
        result = afe.evaluate_preemptive_exit(
            held_symbol="0193T0", inverse_probability=60.0, hynix_probability=40.0,
            down_turn_probability_3m=50.0, up_turn_probability_3m=50.0,
            momentum_inflection_down=50.0, momentum_inflection_up=50.0, current_profit_pct=1.3,
        )
        assert result is not None
        assert result.get("profit_lock") is True

    def test_hynix_full_exit_on_high_exit_probability(self):
        result = afe.evaluate_preemptive_exit(
            held_symbol="0193T0", inverse_probability=80.0, hynix_probability=20.0,
            down_turn_probability_3m=50.0, up_turn_probability_3m=50.0,
            momentum_inflection_down=50.0, momentum_inflection_up=50.0,
        )
        assert result["ratio"] == 1.0

    def test_hynix_partial_exit_on_moderate_signals(self):
        result = afe.evaluate_preemptive_exit(
            held_symbol="0193T0", inverse_probability=59.0, hynix_probability=41.0,
            down_turn_probability_3m=62.0, up_turn_probability_3m=38.0,
            momentum_inflection_down=61.0, momentum_inflection_up=39.0,
        )
        assert result["ratio"] == pytest.approx(0.25)

    def test_no_action_when_signals_weak(self):
        result = afe.evaluate_preemptive_exit(
            held_symbol="0193T0", inverse_probability=45.0, hynix_probability=55.0,
            down_turn_probability_3m=40.0, up_turn_probability_3m=60.0,
            momentum_inflection_down=30.0, momentum_inflection_up=70.0,
        )
        assert result is None


class TestReentryCooldown:
    def test_tp_cooldown_180s_default(self):
        now = datetime(2026, 7, 14, 10, 5, 0)
        exit_time = (now - timedelta(seconds=100)).isoformat()
        reason = afe.check_reentry_cooldown(exit_time, was_take_profit=True, now=now, dominant_probability=60.0, confidence=60.0)
        assert reason is not None

    def test_tp_cooldown_shortens_to_90s_at_high_probability(self):
        now = datetime(2026, 7, 14, 10, 5, 0)
        exit_time = (now - timedelta(seconds=100)).isoformat()
        reason = afe.check_reentry_cooldown(exit_time, was_take_profit=True, now=now, dominant_probability=85.0, confidence=60.0)
        assert reason is None

    def test_sl_cooldown_600s_default(self):
        now = datetime(2026, 7, 14, 10, 5, 0)
        exit_time = (now - timedelta(seconds=200)).isoformat()
        reason = afe.check_reentry_cooldown(exit_time, was_take_profit=False, now=now, dominant_probability=60.0, confidence=60.0)
        assert reason is not None

    def test_sl_cooldown_exception_at_180s_with_all_conditions(self):
        now = datetime(2026, 7, 14, 10, 5, 0)
        exit_time = (now - timedelta(seconds=200)).isoformat()
        reason = afe.check_reentry_cooldown(
            exit_time, was_take_profit=False, now=now, dominant_probability=90.0, confidence=85.0,
            trend_rebreak_confirmed=True,
        )
        assert reason is None

    def test_no_exit_time_means_no_cooldown(self):
        now = datetime(2026, 7, 14, 10, 5, 0)
        assert afe.check_reentry_cooldown(None, was_take_profit=True, now=now, dominant_probability=60.0, confidence=60.0) is None


class TestWhipsaw:
    def test_dampens_after_two_flips_in_window(self):
        now = datetime(2026, 7, 14, 10, 0, 0)
        state = afe.default_whipsaw_state()
        state = afe.register_direction_flip(state, now)
        state = afe.register_direction_flip(state, now + timedelta(seconds=60))
        state = afe.register_direction_flip(state, now + timedelta(seconds=120))
        assert afe.is_whipsaw_dampened(state, now + timedelta(seconds=121))

    def test_dampening_scales_position_and_raises_threshold(self):
        now = datetime(2026, 7, 14, 10, 0, 0)
        state = {"flip_history": [], "dampened_until": (now + timedelta(seconds=100)).isoformat()}
        result = afe.apply_whipsaw_dampening(40.0, 60.0, state, now)
        assert result["position_pct"] == pytest.approx(20.0)
        assert result["threshold"] == pytest.approx(65.0)
        assert result["dampened"] is True


class TestDailyRiskLadder:
    def test_profit_1pct_caps_at_70(self):
        assert afe.adaptive_fusion_daily_risk_ladder(1.2)["max_position_pct"] == 70.0

    def test_profit_2pct_caps_at_50_and_adds_threshold(self):
        r = afe.adaptive_fusion_daily_risk_ladder(2.2)
        assert r["max_position_pct"] == 50.0 and r["threshold_add"] == 3.0

    def test_profit_3pct_enters_profit_protection_mode_not_full_halt(self):
        # 2026-07-14 개편(요구사항 4절) — +3% 이후는 완전 차단이 아니라 "수익보호
        # 모드"로 비중을 크게 줄이고(최대 20%) 문턱을 높인다(+6).
        r = afe.adaptive_fusion_daily_risk_ladder(3.1)
        assert r["entries_allowed"] is True
        assert r["max_position_pct"] == 20.0
        assert r["threshold_add"] == 6.0

    def test_loss_2_5pct_forces_liquidate(self):
        r = afe.adaptive_fusion_daily_risk_ladder(-2.6)
        assert r["force_liquidate"] is True and r["entries_allowed"] is False

    def test_loss_2pct_halts_entries_no_liquidate(self):
        # 2026-07-14 개편 — 신규진입 중단 기준이 -1.8%에서 요구사항 4절의 -2.0%로 조정됨.
        r = afe.adaptive_fusion_daily_risk_ladder(-2.1)
        assert r["entries_allowed"] is False and r["force_liquidate"] is False

    def test_neutral_day_full_size(self):
        r = afe.adaptive_fusion_daily_risk_ladder(0.1)
        assert r["max_position_pct"] == 100.0 and r["entries_allowed"] is True


class TestPredictionV2Degradation:
    def _seed(self, action, hynix_ret, inverse_ret, n=1, day=None):
        day = day or datetime(2026, 7, 1, 10, 0)
        for i in range(n):
            afe.record_prediction_v2_snapshot(
                day + timedelta(minutes=i), action, buy_probability=70.0 if action == "BUY" else 30.0,
                sell_probability=70.0 if action == "INVERSE" else 30.0, hynix_price=100.0, inverse_price=50.0,
            )
        resolve_time = day + timedelta(minutes=5 + n)
        hynix_price_out = 100.0 * (1 + hynix_ret / 100.0)
        inverse_price_out = 50.0 * (1 + inverse_ret / 100.0)
        afe.resolve_prediction_v2_outcomes(resolve_time, hynix_price_out, inverse_price_out)

    def test_no_data_returns_shadow(self):
        result = afe.evaluate_prediction_v2_performance(datetime(2026, 7, 14))
        assert result["model_status"] == afe.MODEL_STATUS_SHADOW

    def test_degrades_on_negative_average_return(self):
        for i in range(25):
            self._seed("BUY", hynix_ret=-1.0, inverse_ret=0.0, day=datetime(2026, 7, 1, 9, 0) + timedelta(minutes=i * 20))
        result = afe.evaluate_prediction_v2_performance(datetime(2026, 8, 1))
        assert result["model_status"] == afe.MODEL_STATUS_DEGRADED

    def test_low_sample_stays_shadow(self):
        for i in range(5):
            self._seed("BUY", hynix_ret=1.0, inverse_ret=0.0, day=datetime(2026, 7, 1, 9, 0) + timedelta(minutes=i * 20))
        result = afe.evaluate_prediction_v2_performance(datetime(2026, 8, 1))
        assert result["model_status"] == afe.MODEL_STATUS_SHADOW


class TestHynixAdaptiveFusionEngineDecide:
    def _cycle_result(self, up3=60.0, down3=30.0, up5=60.0, down5=30.0, up_accel=60.0, down_accel=30.0, phase="TREND_UP", conf=70.0):
        return {
            "cycle_phase": phase,
            "turning_point": {
                "up_turn_probability_3m": up3, "down_turn_probability_3m": down3,
                "up_turn_probability_5m": up5, "down_turn_probability_5m": down5,
                "confidence": conf,
            },
            "momentum": {"momentum_acceleration_up": up_accel, "momentum_acceleration_down": down_accel},
            "cycle_confidence": conf, "recommended_position_pct": 30.0, "reasons": ["test"],
        }

    def _active_decision(self, action="ENTER_HYNIX", pct=30.0, fusion_score=75.0):
        return {
            "action": action, "recommended_position_pct": pct,
            "fusion_result": {"fusion_score": fusion_score}, "reasons": ["active reasons"],
        }

    def _uptrend(self, streak=1):
        return {
            "available": True, "direction": afe.ACTION_HYNIX, "is_stale": False,
            "returns": {"1m": 0.2, "3m": 0.6, "5m": 1.0, "15m": 1.4},
            "above_vwap": True, "ema_slope_pct": 0.1,
            "higher_highs": True, "higher_lows": True,
            "hynix_uptrend_confirmed": True, "hynix_downtrend_confirmed": False,
            "hynix_uptrend_streak": streak, "top_factors": ["test uptrend"],
        }

    def _downtrend(self, streak=2):
        return {
            "available": True, "direction": afe.ACTION_INVERSE, "is_stale": False,
            "returns": {"1m": -0.2, "3m": -0.6, "5m": -1.0, "15m": -1.4},
            "above_vwap": False, "ema_slope_pct": -0.1,
            "lower_highs": True, "lower_lows": True,
            "hynix_uptrend_confirmed": False, "hynix_downtrend_confirmed": True,
            "hynix_downtrend_streak": streak, "top_factors": ["test downtrend"],
        }

    def test_strong_aligned_signals_produce_executable_hynix_buy(self):
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 10, 0)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(),
            prediction_v2_probability={"buy_probability": 75.0, "sell_probability": 15.0, "hold_probability": 10.0},
            prediction_v2_decision={"final_action_v2": "BUY"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_ADVISORY, "sample_size": 30, "valid_sample_fraction": 0.9},
            cycle_result=self._cycle_result(), cycle_ai_validated=False, micron_proxy={"effective_micron_score": 65.0, "micron_data_confidence": 70.0, "micron_score_source": "REAL"},
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
            daily_return_pct=0.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0,
        )
        assert result["final_action"] == afe.ACTION_HYNIX
        assert result["symbol"] == "0193T0"
        assert result["executable"] is True
        assert result["target_position_pct"] > 0

    def test_conflicting_signals_block_or_shrink(self):
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 10, 0)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(action="ENTER_INVERSE", pct=40.0, fusion_score=30.0),
            prediction_v2_probability={"buy_probability": 70.0, "sell_probability": 15.0, "hold_probability": 15.0},
            prediction_v2_decision={"final_action_v2": "BUY"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_LIVE_VALIDATED, "sample_size": 300, "valid_sample_fraction": 0.9},
            cycle_result=self._cycle_result(up3=30.0, down3=60.0, up5=30.0, down5=60.0, up_accel=30.0, down_accel=60.0, phase="RANGE_NOISE"),
            cycle_ai_validated=True, micron_proxy=None,
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
            daily_return_pct=0.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0,
        )
        # ACTIVE=INVERSE, Prediction V2=BUY(HYNIX) — 반대방향 충돌이므로 either HOLD 강제되거나 비중이 크게 축소되어야 한다.
        assert result["executable"] is False or result["target_position_pct"] <= 30.0

    def test_missing_models_still_produces_decision_from_active_only(self):
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 10, 0)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(fusion_score=80.0),
            prediction_v2_probability={"buy_probability": None, "sell_probability": None, "hold_probability": None},
            prediction_v2_decision={"final_action_v2": "HOLD"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_SHADOW, "sample_size": 0, "valid_sample_fraction": 0.0},
            cycle_result={"cycle_phase": None, "turning_point": {}, "momentum": {}, "cycle_confidence": 50.0, "recommended_position_pct": 0.0, "reasons": []},
            cycle_ai_validated=False, micron_proxy=None,
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
            daily_return_pct=0.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0,
        )
        assert result["weights"] == {afe.MODEL_ACTIVE_FUSION: 1.0}
        assert result["final_action"] == afe.ACTION_HYNIX

    def test_stale_price_blocks_execution(self):
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 10, 0)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(fusion_score=80.0),
            prediction_v2_probability={"buy_probability": 75.0, "sell_probability": 15.0, "hold_probability": 10.0},
            prediction_v2_decision={"final_action_v2": "BUY"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_ADVISORY, "sample_size": 30, "valid_sample_fraction": 0.9},
            cycle_result=self._cycle_result(), cycle_ai_validated=False, micron_proxy=None,
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=True,
            daily_return_pct=0.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0,
        )
        assert result["executable"] is False
        assert "stale" in result["blocking_reason"]

    def test_daily_loss_limit_blocks_new_entries(self):
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 10, 0)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(fusion_score=80.0),
            prediction_v2_probability={"buy_probability": 75.0, "sell_probability": 15.0, "hold_probability": 10.0},
            prediction_v2_decision={"final_action_v2": "BUY"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_ADVISORY, "sample_size": 30, "valid_sample_fraction": 0.9},
            cycle_result=self._cycle_result(), cycle_ai_validated=False, micron_proxy=None,
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
            daily_return_pct=-2.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0,
        )
        assert result["executable"] is False

    def test_after_1450_blocks_new_entries(self):
        # 2026-07-14 개편 — 플랫폼 공통 규칙(14:50 이후 신규매수 금지)과 일치시킴.
        # 과거 "15:00"은 ENHANCED_LEGACY/강제청산 경로의 14:50 컷오프와 어긋나는 버그였다.
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 14, 51)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(fusion_score=80.0),
            prediction_v2_probability={"buy_probability": 75.0, "sell_probability": 15.0, "hold_probability": 10.0},
            prediction_v2_decision={"final_action_v2": "BUY"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_ADVISORY, "sample_size": 30, "valid_sample_fraction": 0.9},
            cycle_result=self._cycle_result(), cycle_ai_validated=False, micron_proxy=None,
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
            daily_return_pct=0.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0,
        )
        assert result["executable"] is False
        assert "14:50" in result["blocking_reason"]

    def test_live_hynix_uptrend_blocks_inverse_new_entry(self):
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 10, 0)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(action="ENTER_INVERSE", fusion_score=35.0),
            prediction_v2_probability={"buy_probability": 20.0, "sell_probability": 70.0, "hold_probability": 10.0},
            prediction_v2_decision={"final_action_v2": "INVERSE"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_ADVISORY, "sample_size": 30, "valid_sample_fraction": 0.9},
            cycle_result=self._cycle_result(up3=25, down3=70, up5=25, down5=70, up_accel=25, down_accel=70, phase="TREND_DOWN"),
            cycle_ai_validated=False, micron_proxy={"effective_micron_score": 20.0, "micron_data_confidence": 70.0},
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
            daily_return_pct=0.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0, live_hynix_trend=self._uptrend(streak=1),
        )
        assert result["final_action"] == afe.ACTION_HOLD
        assert result["symbol"] is None
        assert result["executable"] is False

    def test_live_hynix_uptrend_two_confirmations_switches_to_hynix(self):
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 10, 0)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(action="ENTER_INVERSE", fusion_score=35.0),
            prediction_v2_probability={"buy_probability": 20.0, "sell_probability": 70.0, "hold_probability": 10.0},
            prediction_v2_decision={"final_action_v2": "INVERSE"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_ADVISORY, "sample_size": 30, "valid_sample_fraction": 0.9},
            cycle_result=self._cycle_result(up3=25, down3=70, up5=25, down5=70, up_accel=25, down_accel=70, phase="TREND_DOWN"),
            cycle_ai_validated=False, micron_proxy={"effective_micron_score": 20.0, "micron_data_confidence": 70.0},
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
            daily_return_pct=0.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0, live_hynix_trend=self._uptrend(streak=2),
        )
        assert result["final_action"] == afe.ACTION_HYNIX
        assert result["symbol"] == "0193T0"

    def test_live_hynix_downtrend_two_confirmations_switches_to_inverse(self):
        engine = afe.HynixAdaptiveFusionEngine()
        now = datetime(2026, 7, 14, 10, 0)
        result = engine.decide(
            now=now, active_decision_result=self._active_decision(action="ENTER_HYNIX", fusion_score=70.0),
            prediction_v2_probability={"buy_probability": 70.0, "sell_probability": 20.0, "hold_probability": 10.0},
            prediction_v2_decision={"final_action_v2": "BUY"},
            prediction_v2_performance={"model_status": afe.MODEL_STATUS_ADVISORY, "sample_size": 30, "valid_sample_fraction": 0.9},
            cycle_result=self._cycle_result(), cycle_ai_validated=False, micron_proxy=None,
            held_symbol=None, position_conflict=False, data_ok=True, price_is_stale=False,
            daily_return_pct=0.0, orders_today_count=1,
            hold_tracker=afe.default_hold_tracker(), whipsaw_state=afe.default_whipsaw_state(),
            consecutive_stop_losses=0, live_hynix_trend=self._downtrend(streak=2),
        )
        assert result["final_action"] == afe.ACTION_INVERSE
        assert result["symbol"] == "0197X0"


class TestLiveTrendAndStaleData:
    def test_compute_live_hynix_trend_detects_uptrend(self):
        now = datetime(2026, 7, 14, 10, 5)
        rows = []
        prices = [100, 101, 102, 103, 104, 105]
        for i, price in enumerate(prices):
            rows.append({
                "datetime": now - timedelta(minutes=len(prices) - 1 - i),
                "close": price, "high": price + 1, "low": price - 0.5, "volume": 1000 + i,
            })
        trend = afe.compute_live_hynix_trend(pd.DataFrame(rows), now=now)
        assert trend["hynix_uptrend_confirmed"] is True
        assert trend["direction"] == afe.ACTION_HYNIX

    def test_stale_micron_data_gets_zero_weight_status(self):
        result = afe.model_result_from_micron_proxy({
            "effective_micron_score": 10.0,
            "micron_data_confidence": 80.0,
            "micron_score_source": "synthetic_micron",
            "age_minutes": 30.0,
        })
        assert result["model_status"] == afe.MODEL_STATUS_DEGRADED
        assert result["confidence"] == 0.0
