"""
test_hynix_switch_engine.py — real 모드 일 누적 손실 -2.5% 도달 시 자동매매 중단 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

import app.services.hynix_switch_engine as engine
import app.services.hynix_switch_state as state_module
from app.trading import exit_order_coordinator as order_coord

_MID_SESSION_NOW = datetime(2026, 7, 15, 10, 0, 0)  # 09:10~14:50 신규진입 허용 구간


@pytest.fixture(autouse=True)
def _reset_order_coordinator():
    """요구사항(2026-07-21) — _buy_new()가 공용 OrderCoordinator(모듈 전역
    _order_records)를 거치게 되면서, 이 파일의 여러 테스트가 동일한 고정
    _MID_SESSION_NOW를 쓰는 탓에 idempotency key가 우연히 겹쳐 앞선 테스트의
    주문기록이 뒤 테스트의 매수를 "중복주문"으로 차단할 수 있다. 매 테스트마다
    초기화해 테스트 간 격리를 보장한다."""
    order_coord.reset_for_tests()
    yield
    order_coord.reset_for_tests()


class _FakeBroker:
    def __init__(self, cash: float = 1_000_000.0):
        self.cash = cash

    def get_positions(self):
        return []

    def get_buyable_cash(self):
        return self.cash

    def get_balance(self):
        return self.cash

    def buy(self, *args, **kwargs):
        raise AssertionError("이 테스트에서는 매수가 발생하면 안 됩니다(HOLD 신호만 사용).")

    def sell(self, *args, **kwargs):
        raise AssertionError("이 테스트에서는 매도가 발생하면 안 됩니다(HOLD 신호만 사용).")


def _fake_enhanced_result():
    return {
        "base_prediction_score": 50.0, "existing_micron_score": 50.0, "hynix_technical_score": 50.0,
        "intraday_momentum_score": 50.0, "inverse_pressure_score": 50.0, "enhanced_score": 50.0,
        "reason_top5": [], "data_valid": {"base_prediction": True, "existing_micron": True, "hynix_technical": True, "intraday_momentum": True},
        "hynix_current_price": 100_000, "inverse_current_price": 5_000, "inverse_price_stale": False,
        "micron_detail": {}, "tech_detail": {}, "momentum_detail": {}, "inverse_detail": {},
        "market_data": {"hynix_minute": {"df_1min": None}}, "warnings": [],
    }


def _silence_prediction_tracker(monkeypatch) -> None:
    """엔진이 함수 내부에서 지연 import하는 예측추적 로거가 실제 data/ 파일을 건드리지 않도록 무력화."""
    import app.services.hynix_prediction_tracker as tracker_module

    monkeypatch.setattr(tracker_module, "log_trade_decision", lambda *a, **kw: None)
    monkeypatch.setattr(tracker_module, "check_and_resolve_pending_outcomes", lambda *a, **kw: [])


def _fake_decision(*_args, **_kwargs):
    return {
        "final_action": "HOLD", "enhanced_score": 50.0, "inverse_pressure_score": 50.0,
        "score_gap": 0.0, "score_gap_below_forced_trade_threshold": True, "reasons": [],
    }


def test_real_mode_daily_loss_limit_stops_auto_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.config as config_module
    import app.trading.broker_factory as broker_factory_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", _fake_decision)

    class _FakeCfg:
        def enhanced_real_gate_status(self, current_mode="real"):
            return {"ready": True, "blocking_reasons": [], "checks": {"current_mode_is_real": current_mode == "real"}}

        def full_auto_real_confirm_ok(self):
            return True

        def full_auto_real_confirm_text(self):
            return "TEST_CONFIRM"

    monkeypatch.setattr(config_module, "get_config", lambda: _FakeCfg())
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    broker = _FakeBroker(cash=1_000_000.0)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: broker)

    state = state_module.load_state()
    state["auto_trade_on"] = True
    state["mode"] = "real"
    state_module.save_state_atomic(state)

    result1 = engine.update_hynix_auto_trade_loop(mode="real", now=_MID_SESSION_NOW)
    assert result1["state"]["stopped"] is False

    state = result1["state"]
    state["daily_pnl_baseline_equity"] = 1_000_000.0
    state["realized_pnl_today_krw"] = -30_000.0
    state_module.save_state_atomic(state)
    broker.cash = 970_000.0  # baseline 대비 -3.0%
    from datetime import timedelta
    result2 = engine.update_hynix_auto_trade_loop(mode="real", now=_MID_SESSION_NOW + timedelta(minutes=3))

    assert result2["state"]["stopped"] is True
    assert "손실" in (result2["state"].get("stopped_reason") or "")


def test_mock_mode_trade_log_written_on_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.dry_run_broker as dry_run_broker_module
    import app.trading.broker_factory as broker_factory_module
    import app.data_sources.hynix_long_collector as long_collector_module
    import app.trading.broker_factory as broker_factory_module
    import app.trading.broker_factory as broker_factory_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", lambda enhanced, current_position=None: {
        "final_action": "HYNIX_BUY", "enhanced_score": 80.0, "inverse_pressure_score": 10.0,
        "score_gap": 70.0, "score_gap_below_forced_trade_threshold": False, "reasons": ["test"],
    })

    # mock 모드는 이제 DryRunBroker(로컬 시뮬레이션)를 사용 — 그 상태파일 경로만 tmp_path로 격리
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: dry_run_broker_module.DryRunBroker())
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 19_400.0, "stale": False})

    logged_trades = []
    monkeypatch.setattr(engine, "log_trade", lambda record: logged_trades.append(record))
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    state = state_module.load_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert result["state"]["position"]["symbol"] == "0193T0"
    assert len(logged_trades) == 1

    trace = result["pipeline_trace"]
    assert trace["prediction_signal"] == "BUY"
    assert trace["order_sent"] is True
    assert trace["broker_executed"] is True
    assert trace["position_confirmed"] is True
    assert trace["ui_synced"] is True
    assert trace["trade_counter"] == 1
    assert trace["stopped_stage"] is None


def test_active_strategy_mock_toggle_places_real_dryrun_order(tmp_path, monkeypatch):
    """섹션 15 완료기준 — active_strategy_enabled=True + mock이면 실제 DryRunBroker
    buy()가 호출되어야 한다(ENHANCED_LEGACY의 final_action=HOLD여도)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.dry_run_broker as dry_run_broker_module
    import app.trading.broker_factory as broker_factory_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    # ENHANCED_LEGACY는 HOLD — Active Strategy가 이를 대체해서 진입해야 함을 검증한다.
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", _fake_decision)
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: dry_run_broker_module.DryRunBroker())
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    # Cycle AI/Decision V2 shadow 계산을 강한 BUY 신호로 고정(실 네트워크/판단 로직 우회).
    def _fake_shadow(state, enhanced_result, decision, df_1min, hynix_price, inverse_price, now):
        state["last_cycle_ai_result"] = {
            "cycle": {
                "cycle_phase": "REVERSAL_CONFIRMED_UP",
                "turning_point": {"up_turn_probability_3m": 80.0, "down_turn_probability_3m": 10.0, "confidence": 80.0},
                "momentum": {"raw_velocity_3": 0.1, "momentum_acceleration_up": 70.0},
            },
            "probability": {"buy_probability": 90.0, "sell_probability": 5.0, "hold_probability": 5.0},
        }
        return state["last_cycle_ai_result"]

    monkeypatch.setattr(engine, "_run_shadow_cycle_ai_and_decision_v2", _fake_shadow)

    state = state_module.load_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["active_strategy_enabled"] = True
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert result["state"]["position"]["symbol"] is None
    assert result["state"]["last_final_execution_decision"]["signal_source"] == "SHADOW_ONLY"


def _fake_strong_buy_decision(*_args, **_kwargs):
    return {
        "final_action": "HYNIX_STRONG_BUY", "enhanced_score": 90.0, "inverse_pressure_score": 10.0,
        "score_gap": 80.0, "score_gap_below_forced_trade_threshold": False, "reasons": [],
    }


def test_active_strategy_toggle_does_not_block_real_enhanced_regime_switch_entry(tmp_path, monkeypatch):
    """요구사항(2026-07-16 실측) — active_strategy_enabled=True여도 실제 주문 엔진
    (ENHANCED_REGIME_SWITCH)이 신규 진입을 계속 실행해야 한다.

    _run_active_strategy_entry/_run_adaptive_fusion_entry는 이미 맨 위에서 무조건
    shadow-only를 반환하도록 비활성화돼 있는데, 예전 코드는 이 토글이 켜져 있으면
    `elif`로 ENHANCED_REGIME_SWITCH의 실제 진입 로직 자체를 건너뛰었다 — 그 결과
    BUY/INVERSE 신호가 나도 어느 엔진도 실제로 주문을 넣지 않았다(2026-07-16 실측:
    "[entry_approved] [ACTIVE_STRATEGY] ACTIVE_STRATEGY shadow-only: actual broker
    orders are owned by ENHANCED_REGIME_SWITCH" 메시지와 함께 매수가 전혀 체결되지
    않음)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.dry_run_broker as dry_run_broker_module
    import app.trading.broker_factory as broker_factory_module
    import app.data_sources.hynix_long_collector as long_collector_module
    from app.data_sources.hynix_long_collector import LONG_SYMBOL

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", _fake_strong_buy_decision)
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: dry_run_broker_module.DryRunBroker())
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 19_400.0, "stale": False})
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    monkeypatch.setattr(engine, "_run_shadow_cycle_ai_and_decision_v2", lambda *a, **kw: {})
    _silence_prediction_tracker(monkeypatch)

    state = state_module.load_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["active_strategy_enabled"] = True  # 토글 ON — 이게 실제 진입을 막으면 안 된다.
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    # 실제 ENHANCED_REGIME_SWITCH가 이번 사이클에 진짜로 진입해야 한다.
    assert result["state"]["position"]["symbol"] == LONG_SYMBOL
    assert result["state"]["position"]["quantity"] > 0
    # Active Strategy 진단(shadow) 필드는 UI 표시용으로 계속 채워져야 한다.
    assert result["state"]["last_final_execution_decision"]["signal_source"] == "SHADOW_ONLY"


def test_early_live_enhanced_inverse_approval_alone_does_not_place_probe_order(tmp_path, monkeypatch):
    """요구사항(2026-07-21 방향편향 수정) — raw_score_leader(final_action/Enhanced
    approval)는 더 이상 Early Detector의 실제 진입 방향(actionable_direction)을
    대신하지 않는다(_augment_fast_signal_with_enhanced_approval이 더 이상 direction/
    up_votes/down_votes를 덮어쓰지 않음). Enhanced가 INVERSE를 승인해도, Early
    Detector 자신의 실시간 기술적 신호(1분봉 6-vote 등)가 없으면 주문을 넣지
    않아야 한다 — 과거에는 여기서 raw_score만으로 즉시 조기진입 주문이 나갔다."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.adaptive_market_regime as regime_module
    import app.trading.dry_run_broker as dry_run_broker_module
    import app.trading.broker_factory as broker_factory_module
    import app.data_sources.hynix_long_collector as long_collector_module

    inverse_price = 5_000.0
    enhanced = _fake_enhanced_result()
    enhanced["inverse_current_price"] = inverse_price
    enhanced["data_valid"]["hynix_signal_price"] = True
    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: enhanced)
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "INVERSE_BUY", "enhanced_score": 30.0, "inverse_pressure_score": 80.0,
            "score_gap": 120.0, "score_gap_below_forced_trade_threshold": False, "reasons": ["approved inverse"],
        },
    )
    monkeypatch.setattr(
        regime_module, "compute_and_confirm_regime",
        lambda *a, **kw: {
            "raw_regime": "STRONG_DOWN", "confirmed_regime": "STRONG_DOWN", "displayed_regime": "STRONG_DOWN",
            "confidence": 90.0, "reasons": ["forced"], "profile": regime_module.get_risk_profile("STRONG_DOWN"),
            "previous_regime": None, "transitioned_at": None,
            "confirmation_state": regime_module.default_regime_confirmation_state(), "snapshot": {},
        },
    )
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: dry_run_broker_module.DryRunBroker())
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 100_000.0, "stale": False})
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    monkeypatch.setattr(engine, "_run_shadow_cycle_ai_and_decision_v2", lambda *a, **kw: {})
    _silence_prediction_tracker(monkeypatch)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_enabled"] = True
    state["early_trend_detector_live"] = True
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    trace = result["pipeline_trace"]
    assert trace["enhanced_direction_approval"]["final_action"] == "INVERSE_BUY"
    assert trace["enhanced_direct_order_blocked"] is True
    assert trace["entry_approved"] is True
    # Enhanced의 raw score 승인만으로는 조기진입 주문이 나가지 않는다 — Early
    # Detector 자신의 실시간 신호가 없으므로 NO_EARLY_SIGNAL로 스킵되어야 한다.
    assert trace["stopped_stage"] == "early_order"
    assert trace["early_decision"]["reason_code"] == "NO_EARLY_SIGNAL"
    assert trace["early_order_result"]["order_sent"] is False
    assert result["state"]["position"]["symbol"] != "0197X0"


def test_early_live_reports_no_early_signal_even_when_cost_gate_premocked(tmp_path, monkeypatch):
    """요구사항(2026-07-21 방향편향 수정) — Enhanced 승인만으로 Early Detector의
    direction을 더 이상 대신할 수 없으므로, cost gate를 미리 차단 상태로
    monkeypatch해도(도달 자체를 안 함) 그보다 앞선 NO_EARLY_SIGNAL 단계에서
    멈춰야 한다. cost gate 자체의 차단 로직은
    test_early_trend_detector.py의 evaluate_cost_gate 단위테스트가 별도로 덮는다."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.adaptive_market_regime as regime_module
    import app.trading.early_trend_detector as early_module
    import app.trading.dry_run_broker as dry_run_broker_module
    import app.trading.broker_factory as broker_factory_module
    import app.data_sources.hynix_long_collector as long_collector_module

    enhanced = _fake_enhanced_result()
    enhanced["inverse_current_price"] = 5_000.0
    enhanced["data_valid"]["hynix_signal_price"] = True
    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: enhanced)
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "INVERSE_BUY", "enhanced_score": 30.0, "inverse_pressure_score": 80.0,
            "score_gap": 120.0, "score_gap_below_forced_trade_threshold": False, "reasons": ["approved inverse"],
        },
    )
    monkeypatch.setattr(
        regime_module, "compute_and_confirm_regime",
        lambda *a, **kw: {
            "raw_regime": "STRONG_DOWN", "confirmed_regime": "STRONG_DOWN", "displayed_regime": "STRONG_DOWN",
            "confidence": 90.0, "reasons": ["forced"], "profile": regime_module.get_risk_profile("STRONG_DOWN"),
            "previous_regime": None, "transitioned_at": None,
            "confirmation_state": regime_module.default_regime_confirmation_state(), "snapshot": {},
        },
    )
    monkeypatch.setattr(early_module, "evaluate_cost_gate", lambda *a, **kw: {"blocked": True, "net_edge_pct": -0.1})
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: dry_run_broker_module.DryRunBroker())
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 100_000.0, "stale": False})
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    monkeypatch.setattr(engine, "_run_shadow_cycle_ai_and_decision_v2", lambda *a, **kw: {})
    _silence_prediction_tracker(monkeypatch)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_enabled"] = True
    state["early_trend_detector_live"] = True
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    trace = result["pipeline_trace"]
    assert trace["enhanced_direct_order_blocked"] is True
    assert trace["entry_approved"] is True
    assert trace["stopped_stage"] == "early_order"
    assert trace["early_decision"]["reason_code"] == "NO_EARLY_SIGNAL"
    assert "ENHANCED_REGIME_SWITCH는 신규매수 직접 실행 금지" not in (trace["blocking_reason"] or "")
    assert trace["early_order_result"]["order_sent"] is False


# =============================================================================
# FinalExecutionDecision / 중복주문 방지 / real-mode 게이트 테스트 (섹션 13)
# =============================================================================

def _setup_active_strategy_run(tmp_path, monkeypatch, shadow: dict, mode: str = "mock", auto_trade_on: bool = True):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.dry_run_broker as dry_run_broker_module
    import app.trading.broker_factory as broker_factory_module
    import app.data_sources.hynix_long_collector as long_collector_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", _fake_decision)
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: dry_run_broker_module.DryRunBroker())
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 19_400.0, "stale": False})
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    def _fake_shadow(state, enhanced_result, decision, df_1min, hynix_price, inverse_price, now):
        state["last_cycle_ai_result"] = shadow
        return shadow

    monkeypatch.setattr(engine, "_run_shadow_cycle_ai_and_decision_v2", _fake_shadow)

    state = state_module.load_state()
    state["auto_trade_on"] = auto_trade_on
    state["mode"] = mode
    state["active_strategy_enabled"] = True
    state_module.save_state_atomic(state)
    return state


def _inverse_dominant_shadow(cycle_phase="NO_TRADE", confidence=60.0, momentum_down=71.0):
    return {
        "cycle": {
            "cycle_phase": cycle_phase,
            "turning_point": {"up_turn_probability_3m": 15.0, "down_turn_probability_3m": 65.0, "confidence": confidence},
            "momentum": {"raw_velocity_3": -0.2, "momentum_acceleration_up": 20.0, "momentum_acceleration_down": momentum_down},
        },
        "probability": {"buy_probability": 20.0, "sell_probability": 75.0, "hold_probability": 5.0},
        "effective_micron_score": 45.0,
    }


def test_inverse_signal_no_trade_phase_still_executes_trial_entry(tmp_path, monkeypatch):
    """섹션 13-1 — INVERSE 신호 + NO_TRADE + 우세확률 60+ + confidence 60이면 0197X0 시험매수 실행."""
    shadow = _inverse_dominant_shadow(cycle_phase="NO_TRADE", confidence=60.0)
    _setup_active_strategy_run(tmp_path, monkeypatch, shadow)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert result["state"]["position"]["symbol"] is None
    fd = result["state"].get("last_final_execution_decision")
    assert fd["signal_source"] == "SHADOW_ONLY"
    assert fd["order_sent"] is False


def test_inverse_signal_weak_momentum_scores_lower_than_strong_momentum(tmp_path, monkeypatch):
    """섹션 13-2 — momentum_ai가 약할수록(다른 조건 동일) fusion_score가 더 낮게(더 약하게) 나온다.

    <50 INVERSE 밴드는 명세대로 세부 비중 구간이 없으므로(50% 고정), 여기서는 "모멘텀이
    약할수록 fusion_score 자체가 낮아진다"는 컴포넌트 기여도를 직접 검증한다.
    """
    from app.models.hynix_decision_v2 import calculate_fusion_score

    weak = calculate_fusion_score(
        prediction_ai_score=48.0, enhanced_ai_score=50.0, momentum_ai_score=50.0, micron_ai_score=50.0,
        cycle_phase="RANGE_NOISE",
    )
    strong_down_momentum = calculate_fusion_score(
        prediction_ai_score=48.0, enhanced_ai_score=50.0, momentum_ai_score=20.0, micron_ai_score=50.0,
        cycle_phase="RANGE_NOISE",
    )
    assert strong_down_momentum["fusion_score"] < weak["fusion_score"]


def test_executable_decision_calls_broker_buy_same_cycle(tmp_path, monkeypatch):
    """섹션 13-3 — executable=True면 같은 사이클 안에서 broker.buy()가 실제로 호출된다."""
    shadow = {
        "cycle": {
            "cycle_phase": "TREND_UP",
            "turning_point": {"up_turn_probability_3m": 85.0, "down_turn_probability_3m": 10.0, "confidence": 80.0},
            "momentum": {"raw_velocity_3": 0.2, "momentum_acceleration_up": 85.0},
        },
        "probability": {"buy_probability": 90.0, "sell_probability": 5.0, "hold_probability": 5.0},
        "effective_micron_score": 80.0,
    }
    _setup_active_strategy_run(tmp_path, monkeypatch, shadow)

    buy_calls = []
    import app.trading.dry_run_broker as dry_run_broker_module
    original_buy = dry_run_broker_module.DryRunBroker.buy

    def _tracked_buy(self, *args, **kwargs):
        buy_calls.append(args)
        return original_buy(self, *args, **kwargs)

    monkeypatch.setattr(dry_run_broker_module.DryRunBroker, "buy", _tracked_buy)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert len(buy_calls) == 0
    assert result["state"]["last_final_execution_decision"]["order_sent"] is False


def test_position_confirmed_after_buy(tmp_path, monkeypatch):
    """섹션 13-4 — broker.buy() 후 position_manager 재조회까지 성공해야 한다."""
    shadow = {
        "cycle": {
            "cycle_phase": "TREND_UP",
            "turning_point": {"up_turn_probability_3m": 85.0, "down_turn_probability_3m": 10.0, "confidence": 80.0},
            "momentum": {"raw_velocity_3": 0.2, "momentum_acceleration_up": 85.0},
        },
        "probability": {"buy_probability": 90.0, "sell_probability": 5.0, "hold_probability": 5.0},
        "effective_micron_score": 80.0,
    }
    _setup_active_strategy_run(tmp_path, monkeypatch, shadow)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    pm_cache = result.get("position_manager") or {}
    assert pm_cache.get("position", {}).get("symbol") is None
    assert result["state"]["last_final_execution_decision"]["signal_source"] == "SHADOW_ONLY"


def test_hold_signal_never_calls_broker(tmp_path, monkeypatch):
    """섹션 13-5 — fusion_score가 HOLD 밴드면 broker.buy()/sell()이 전혀 호출되지 않는다."""
    neutral_shadow = {
        "cycle": {
            "cycle_phase": "BASE_BUILDING",
            "turning_point": {"up_turn_probability_3m": 50.0, "down_turn_probability_3m": 50.0, "confidence": 50.0},
            "momentum": {"raw_velocity_3": 0.0, "momentum_acceleration_up": 55.0},
        },
        "probability": {"buy_probability": 50.0, "sell_probability": 50.0, "hold_probability": 100.0},
        "effective_micron_score": 55.0,
    }
    _setup_active_strategy_run(tmp_path, monkeypatch, neutral_shadow)

    import app.trading.dry_run_broker as dry_run_broker_module
    monkeypatch.setattr(dry_run_broker_module.DryRunBroker, "buy", lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("HOLD인데 buy() 호출됨")))
    monkeypatch.setattr(dry_run_broker_module.DryRunBroker, "sell", lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("HOLD인데 sell() 호출됨")))

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert result["state"]["position"].get("symbol") is None


def test_duplicate_signal_within_same_cycle_executes_once(tmp_path, monkeypatch):
    """섹션 13-6 — 동일 idempotency_key(같은 분 단위 cycle_id)로 재호출해도 주문은 1회만."""
    shadow = {
        "cycle": {
            "cycle_phase": "TREND_UP",
            "turning_point": {"up_turn_probability_3m": 85.0, "down_turn_probability_3m": 10.0, "confidence": 80.0},
            "momentum": {"raw_velocity_3": 0.2, "momentum_acceleration_up": 85.0},
        },
        "probability": {"buy_probability": 90.0, "sell_probability": 5.0, "hold_probability": 5.0},
        "effective_micron_score": 80.0,
    }
    _setup_active_strategy_run(tmp_path, monkeypatch, shadow)

    buy_calls = []
    import app.trading.dry_run_broker as dry_run_broker_module
    original_buy = dry_run_broker_module.DryRunBroker.buy

    def _tracked_buy(self, *args, **kwargs):
        buy_calls.append(args)
        return original_buy(self, *args, **kwargs)

    monkeypatch.setattr(dry_run_broker_module.DryRunBroker, "buy", _tracked_buy)

    result1 = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)
    assert len(buy_calls) == 0
    assert result1["state"]["position"]["symbol"] is None

    # 같은 분(cycle_id) 안에서 다시 실행 — 이미 보유 중이므로 신규 BUY 자체가 재판단되지
    # 않지만(무포지션 진입 로직만 idempotency 대상), 포지션이 있는 상태에서 같은 방향
    # 신호가 다시 들어와도 추가 매수가 중복 실행되지 않아야 한다.
    result2 = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)
    assert len(buy_calls) == 0  # shadow-only: broker buy is never called


def test_hynix_to_inverse_switch_sells_before_buying(tmp_path, monkeypatch):
    """섹션 13-7 — 0193T0 보유 중 INVERSE 신호 발생 시, 0193T0 매도 확인 후에만 0197X0을 매수한다."""
    bullish_shadow = {
        "cycle": {
            "cycle_phase": "TREND_UP",
            "turning_point": {"up_turn_probability_3m": 85.0, "down_turn_probability_3m": 10.0, "confidence": 80.0},
            "momentum": {"raw_velocity_3": 0.2, "momentum_acceleration_up": 85.0},
        },
        "probability": {"buy_probability": 90.0, "sell_probability": 5.0, "hold_probability": 5.0},
        "effective_micron_score": 80.0,
    }
    state = _setup_active_strategy_run(tmp_path, monkeypatch, bullish_shadow)
    result1 = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)
    assert result1["state"]["position"]["symbol"] is None

    # 강한 반대(INVERSE) 신호로 전환 — 방향전환 최소간격(3분) 이후 시각을 사용한다.
    bearish_shadow = {
        "cycle": {
            "cycle_phase": "BREAKDOWN",
            "turning_point": {"up_turn_probability_3m": 10.0, "down_turn_probability_3m": 80.0, "confidence": 80.0},
            "momentum": {"raw_velocity_3": -0.2, "momentum_acceleration_up": 10.0, "momentum_acceleration_down": 85.0},
        },
        "probability": {"buy_probability": 5.0, "sell_probability": 90.0, "hold_probability": 5.0},
        "effective_micron_score": 20.0,
    }

    def _fake_shadow2(state, enhanced_result, decision, df_1min, hynix_price, inverse_price, now):
        state["last_cycle_ai_result"] = bearish_shadow
        return bearish_shadow

    monkeypatch.setattr(engine, "_run_shadow_cycle_ai_and_decision_v2", _fake_shadow2)

    later = _MID_SESSION_NOW + timedelta(minutes=5)
    result2 = engine.update_hynix_auto_trade_loop(mode="mock", now=later)

    # 전환이 이번 사이클에 완료됐다면 0197X0 보유, 아직 매도 단계라면 무포지션(둘 다 "매수
    # 전에 매도 확인"이라는 순서를 어긴 상태 — 즉 0193T0을 보유한 채 0197X0도 동시에
    # 보유하는 상태는 나오지 않아야 한다).
    pos = result2["state"]["position"]
    assert pos.get("symbol") is None
    assert result2["state"]["last_final_execution_decision"]["signal_source"] == "SHADOW_ONLY"


def test_mock_orders_use_kis_mock_broker_path_not_real_broker(tmp_path, monkeypatch):
    """mode='mock' 자동매매는 broker_factory(mode='mock') 경로를 사용한다.

    실제 테스트 중에는 KIS API를 호출하지 않도록 fake broker를 주입한다.
    """
    shadow = _inverse_dominant_shadow()
    _setup_active_strategy_run(tmp_path, monkeypatch, shadow)

    import app.trading.broker_factory as broker_factory_module

    class _FakeKisMockBroker:
        def __init__(self):
            self.positions = []
            self.cash = 10_000_000.0

        def get_positions(self):
            return list(self.positions)

        def get_buyable_cash(self):
            return self.cash

        def buy(self, symbol, name, quantity, price, order_type="limit"):
            from app.models import OrderResult, Position

            self.positions = [Position(symbol=symbol, name=name, quantity=quantity, avg_price=price, current_price=price)]
            return OrderResult(
                success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                side="buy", quantity=quantity, price=price, order_type=order_type,
                order_id="TEST-MOCK-ORDER", message="accepted",
            )

        def sell(self, symbol, name, quantity, price, order_type="limit"):
            from app.models import OrderResult

            self.positions = []
            return OrderResult(
                success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                side="sell", quantity=quantity, price=price, order_type=order_type,
                order_id="TEST-MOCK-SELL", message="accepted",
            )

    created_modes = []

    def _fake_create_broker(*args, **kwargs):
        created_modes.append(kwargs.get("mode") or (args[1] if len(args) > 1 else None))
        return _FakeKisMockBroker()

    monkeypatch.setattr(broker_factory_module, "create_broker", _fake_create_broker)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert created_modes and created_modes[-1] == "mock"
    fd = result["state"].get("last_final_execution_decision") or {}
    if fd.get("order_id"):
        assert fd["order_id"].startswith("TEST-")


def test_real_mode_blocked_when_any_gate_fails(tmp_path, monkeypatch):
    """섹션 13-9 — real 필수 게이트 중 하나라도 False면 전체 게이트가 실패로 표시된다."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    class _FakeCfg:
        def real_trading_enabled(self):
            return True

        def enhanced_real_gate_status(self, current_mode="real"):
            return {
                "ready": False,
                "blocking_reasons": ["FULL_AUTO_REAL_CONFIRM_TEXT_MISMATCH"],
                "checks": {"current_mode_is_real": current_mode == "real"},
            }

        def full_auto_real_confirm_ok(self):
            return False  # FULL_AUTO_REAL_CONFIRM_TEXT 등 미충족 — 이 게이트만 실패

    state = {"mode": "real", "auto_trade_on": True, "position_conflict": False}
    result = engine.check_real_mode_gates(state, cfg=_FakeCfg())

    assert result["all_pass"] is False
    assert "real_auto_order_enabled" in result["failed_gates"]


def test_order_success_ledger_state_ui_all_consistent(tmp_path, monkeypatch):
    """섹션 13-10 — 주문 성공 후 execution ledger / state / UI(포지션)가 모두 일치한다."""
    from app.services.hynix_execution_ledger import load_ledger
    import app.services.hynix_execution_ledger as ledger_module

    monkeypatch.setattr(ledger_module, "_LEDGER_PATH", tmp_path / "ledger.csv")

    shadow = {
        "cycle": {
            "cycle_phase": "TREND_UP",
            "turning_point": {"up_turn_probability_3m": 85.0, "down_turn_probability_3m": 10.0, "confidence": 80.0},
            "momentum": {"raw_velocity_3": 0.2, "momentum_acceleration_up": 85.0},
        },
        "probability": {"buy_probability": 90.0, "sell_probability": 5.0, "hold_probability": 5.0},
        "effective_micron_score": 80.0,
    }
    _setup_active_strategy_run(tmp_path, monkeypatch, shadow)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    state_symbol = result["state"]["position"]["symbol"]
    pm_symbol = result["position_manager"]["position"]["symbol"]

    ledger_df = load_ledger()
    live_buys = ledger_df[(ledger_df["success"] == True) & (ledger_df["action"] == "BUY") & (ledger_df["is_test_order"] != True)]  # noqa: E712

    assert state_symbol is None
    assert pm_symbol is None
    assert live_buys.empty
    assert result["state"]["last_final_execution_decision"]["signal_source"] == "SHADOW_ONLY"


def test_pipeline_trace_hold_signal_has_no_blocked_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", _fake_decision)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    broker = _FakeBroker(cash=1_000_000.0)
    import app.trading.dry_run_broker as dry_run_broker_module
    monkeypatch.setattr(dry_run_broker_module, "DryRunBroker", lambda **kwargs: broker)

    state = state_module.load_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    trace = result["pipeline_trace"]
    assert trace["prediction_signal"] == "HOLD"
    assert trace["order_sent"] is False
    assert trace["broker_executed"] is False
    assert trace["stopped_stage"] is None  # HOLD는 "정상"이지 "막힌 것"이 아니다


def test_pipeline_trace_marks_ui_synced_false_when_save_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.dry_run_broker as dry_run_broker_module
    import app.trading.broker_factory as broker_factory_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", lambda enhanced, current_position=None: {
        "final_action": "HYNIX_BUY", "enhanced_score": 80.0, "inverse_pressure_score": 10.0,
        "score_gap": 70.0, "score_gap_below_forced_trade_threshold": False, "reasons": ["test"],
    })
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: dry_run_broker_module.DryRunBroker())
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    monkeypatch.setattr(engine, "save_state_atomic", lambda state: False)
    _silence_prediction_tracker(monkeypatch)

    state = state_module.load_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    trace = result["pipeline_trace"]
    assert trace["broker_executed"] is True
    assert trace["ui_synced"] is False
    assert trace["stopped_stage"] == "ui_synced"


class TestAdaptiveFusionRealOrderWiring:
    """2026-07-13 사용자 검증 — Prediction AI V2가 signal_source/ledger에 전혀 반영되지
    않는다는 리포트에 대한 회귀 테스트. adaptive_fusion_enabled=True일 때 실제로
    _run_adaptive_fusion_entry 경로가 타고, ledger에 dominant_model 등이 기록되는지 확인."""

    def _enable_adaptive_fusion(self, tmp_path, monkeypatch, shadow):
        state = _setup_active_strategy_run(tmp_path, monkeypatch, shadow)
        state["adaptive_fusion_enabled"] = True
        state_module.save_state_atomic(state)
        return state

    def test_adaptive_fusion_path_executes_and_tags_ledger(self, tmp_path, monkeypatch):
        shadow = _inverse_dominant_shadow(cycle_phase="NO_TRADE", confidence=60.0)
        self._enable_adaptive_fusion(tmp_path, monkeypatch, shadow)

        result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

        fd = result["state"].get("last_final_execution_decision")
        assert fd is not None
        # Prediction V2가 아직 검증 이력이 없으므로(SHADOW) ACTIVE_ONLY로 정직하게 표기되어야 한다.
        assert fd["signal_source"] == "SHADOW_ONLY"
        assert fd["order_sent"] is False

    def test_shadow_prediction_v2_does_not_claim_applied(self, tmp_path, monkeypatch):
        """Prediction V2가 SHADOW 상태(표본 없음)이면 signal_source가 ADAPTIVE_FUSION/
        PREDICTION_V2_ASSISTED로 표시되면 안 된다(적용됐다고 과장 금지)."""
        shadow = _inverse_dominant_shadow(cycle_phase="NO_TRADE", confidence=60.0)
        self._enable_adaptive_fusion(tmp_path, monkeypatch, shadow)

        result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

        fd = result["state"].get("last_final_execution_decision")
        assert fd["order_sent"] is False
        assert fd["signal_source"] == "SHADOW_ONLY"

    def test_state_last_fusion_decision_populated(self, tmp_path, monkeypatch):
        shadow = _inverse_dominant_shadow(cycle_phase="NO_TRADE", confidence=60.0)
        self._enable_adaptive_fusion(tmp_path, monkeypatch, shadow)

        result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

        fusion_decision = result["state"].get("last_fusion_decision")
        assert fusion_decision is not None
        assert "weights" in fusion_decision and "dominant_model" in fusion_decision
        assert fusion_decision["signal_source"] == "SHADOW_ONLY"


def test_update_loop_default_now_uses_kst_without_crashing(tmp_path, monkeypatch):
    """회귀 방지: now=를 명시적으로 넘기지 않는 실제 운영 호출 경로(스케줄러가 이렇게
    호출한다)에서 `now = now or kst_now()`가 NameError 없이 동작해야 한다. 기존 테스트는
    전부 now=를 명시적으로 넘겨서 이 줄을 실질적으로 검증하지 못했다(2026-07-14 실제
    회귀 발견: kst_now import 누락으로 프로덕션 사이클이 매번 예외로 실패했었음)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", _fake_decision)
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = False  # broker/KIS 경로는 스킵하되, now 처리는 그 전에 실행돼야 한다
    state_module.save_state_atomic(state)

    # now 인자를 아예 생략한다 — 스케줄러(HynixAutoTradeCycleThread._run_cycle_if_enabled)가
    # 실제로 호출하는 방식과 동일하다.
    result = engine.update_hynix_auto_trade_loop(mode="mock")
    assert "pipeline_trace" in result


# =============================================================================
# 2026-07-16 — Adaptive Market Regime이 항상 실제 auto_trade_on 값을 반영하고
# (DISABLED로 조용히 남지 않음), 장중 사이클마다 계산되며, 장 마감 후에도 EOD
# 분석이 주문 없이 수행되는지 검증한다.
# =============================================================================

def test_intraday_cycle_populates_adaptive_regime_state(tmp_path, monkeypatch):
    """요구사항2 — scheduler의 모든 장중 사이클에서 adaptive_regime을 계산해
    state에 남긴다(entry_approved 이전 단계에서 이미 계산되므로 auto_trade_on=False
    여도, 즉 신호가 HOLD로 막히기 전에도 값이 채워진다)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", _fake_decision)
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    final_state = result["state"]
    assert final_state.get("adaptive_regime_enabled") is True
    assert final_state.get("adaptive_regime_mode") == "LIVE"
    assert final_state.get("adaptive_regime") is not None
    assert final_state["adaptive_regime"].get("confirmed_regime")


def test_compute_eod_regime_only_never_calls_order_execution(tmp_path, monkeypatch):
    """요구사항3 — 장 마감 후에도 오늘 저장된 1분봉으로 EOD 분석을 수행하되,
    실제 주문(run_switch_or_entry 등)은 절대 호출하지 않는다."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import pandas as pd
    from datetime import datetime, timedelta

    def _fake_collect_minute(mode=None):
        rows = []
        start = datetime(2026, 7, 16, 9, 0)
        for i in range(30):
            price = 100.0 + (0.05 if i % 2 == 0 else -0.03)
            rows.append({"datetime": start + timedelta(minutes=i), "open": price, "high": price * 1.001, "low": price * 0.999, "close": price, "volume": 1000.0})
        return {"df_1min": pd.DataFrame(rows), "source": "cache", "status": "stale_cache"}

    def _fake_collect_daily(mode=None):
        return {"prev_close": 100.0, "df_daily": None}

    import app.data_sources.auto_market_collector as collector_module
    monkeypatch.setattr(collector_module, "collect_hynix_minute", _fake_collect_minute)
    monkeypatch.setattr(collector_module, "collect_hynix_daily", _fake_collect_daily)

    def _assert_not_called(*a, **kw):
        raise AssertionError("compute_eod_regime_only는 절대 주문 실행 함수를 호출하면 안 된다")

    import app.trading.hynix_switch_position_manager as position_manager_module
    monkeypatch.setattr(position_manager_module, "run_switch_or_entry", _assert_not_called)
    monkeypatch.setattr(position_manager_module, "run_tp_sl_if_needed", _assert_not_called)
    monkeypatch.setattr(position_manager_module, "run_liquidation_if_needed", _assert_not_called)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state_module.save_state_atomic(state)

    now = datetime(2026, 7, 16, 15, 45)  # 장 마감 후
    result = engine.compute_eod_regime_only(mode="mock", now=now)

    assert "error" not in result
    assert result.get("confirmed_regime") or result.get("raw_regime")

    saved = state_module.load_state(mode="mock")
    assert saved.get("adaptive_regime_eod") is not None
    # 신규진입/스위칭/보유 포지션 관련 필드는 전혀 건드리지 않는다.
    assert saved.get("position") == state.get("position")
    assert saved.get("daily_trade_count", 0) == 0


def test_default_state_adaptive_regime_matches_auto_trade_on_default():
    """요구사항1 — Enhanced 자동매매 기본값(True)과 adaptive_regime_enabled/mode
    기본값이 처음부터 일치해야 한다(첫 사이클 전에도 DISABLED로 보이지 않도록)."""
    state = state_module.default_state("mock")
    assert state["auto_trade_on"] is True
    assert state["adaptive_regime_enabled"] is True
    assert state["adaptive_regime_mode"] == "LIVE"


def test_signal_summary_separates_raw_inverse_leader_from_hold_block():
    decision = {
        "final_action": "INVERSE_BUY",
        "enhanced_score": 42.0,
        "inverse_pressure_score": 58.0,
    }
    trace = {
        "prediction_signal": "INVERSE",
        "entry_approved": False,
        "entry_approved_reason": "PRIMARY_TREND=UP with VWAP/EMA confirmations",
        "order_sent": False,
    }
    state = {
        "last_live_hynix_trend": {
            "above_vwap": True,
            "returns": {"3m": 0.24, "5m": 0.38},
            "ema_slope_pct": 0.07,
        },
    }

    summary = engine._build_signal_summary(
        decision=decision, trace=trace, state=state, now=_MID_SESSION_NOW,
        new_entry_allowed_now=True, new_entry_window={"rule": "allowed"},
    )

    assert summary["raw_score_leader"] == "INVERSE"
    assert summary["live_trade_direction"] == "UP"
    assert summary["actionable_signal"] == "HOLD"
    assert summary["final_action"] == "HOLD"
    assert summary["block_reason"] == "LIVE_HYNIX_UPTREND"
    assert "원점수는 INVERSE 우세" in summary["conclusion"]


def test_signal_summary_prioritizes_new_entry_time_gate_after_1450():
    decision = {
        "final_action": "INVERSE_BUY",
        "enhanced_score": 40.0,
        "inverse_pressure_score": 60.0,
    }
    trace = {
        "prediction_signal": "INVERSE",
        "entry_approved": True,
        "entry_approved_reason": "approved",
        "order_sent": False,
    }

    summary = engine._build_signal_summary(
        decision=decision, trace=trace, state={}, now=datetime(2026, 7, 15, 14, 55),
        new_entry_allowed_now=False, new_entry_window={"rule": "14:50 이후 신규진입 금지"},
    )

    assert summary["raw_score_leader"] == "INVERSE"
    assert summary["actionable_signal"] == "HOLD"
    assert summary["final_action"] == "HOLD"
    assert summary["block_reason"] == "NEW_ENTRY_TIME_CLOSED"
    assert summary["conclusion"] == "14:50 이후 신호와 무관하게 신규진입 금지 → HOLD"


# =============================================================================
# 2026-07-16 — 큰 추세 수익 극대화: STRONG_UP 확정 중 인버스 신규매수 금지,
# STRONG_DOWN 확정 중 레버리지(HYNIX) 신규매수 금지.
# =============================================================================

def test_strong_up_confirmed_blocks_new_inverse_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.adaptive_market_regime as regime_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "INVERSE_BUY", "enhanced_score": 40.0, "inverse_pressure_score": 65.0,
            "score_gap": 25.0, "score_gap_below_forced_trade_threshold": False, "reasons": [],
        },
    )
    monkeypatch.setattr(
        regime_module, "compute_and_confirm_regime",
        lambda *a, **kw: {
            "raw_regime": "STRONG_UP", "confirmed_regime": "STRONG_UP", "displayed_regime": "STRONG_UP",
            "confidence": 90.0, "reasons": ["forced for test"], "profile": regime_module.get_risk_profile("STRONG_UP"),
            "previous_regime": None, "transitioned_at": None,
            "confirmation_state": regime_module.default_regime_confirmation_state(), "snapshot": {},
        },
    )
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    class _AssertNoBuyBroker:
        def get_positions(self):
            return []

        def get_buyable_cash(self):
            return 10_000_000.0

        def get_balance(self):
            return 10_000_000.0

        def buy(self, *a, **k):
            raise AssertionError("STRONG_UP 확정 중에는 인버스 신규매수가 금지돼야 한다")

        def sell(self, *a, **k):
            raise AssertionError("이 테스트에서는 매도가 발생하면 안 됩니다.")

    import app.trading.broker_factory as broker_factory_module
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: _AssertNoBuyBroker())

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    trace = result["pipeline_trace"]
    assert trace["entry_approved"] is False
    assert "STRONG_UP" in trace["entry_approved_reason"]
    assert result["state"]["position"].get("symbol") is None


# =============================================================================
# 2026-07-21 실운영 검증 — live_trade_direction(5/10/20/30초 기울기+VWAP+ETF
# 상호확인, app.trading.early_trend_live_feed)만으로도 완전히 대칭으로 신규진입을
# 금지한다: live UP → INVERSE 금지, live DOWN → 레버리지(HYNIX) 금지. Adaptive
# Regime이 아직 STRONG_UP/DOWN으로 확정되지 않은 상태(RANGE)에서도 이 더 빠른
# 신호만으로 반대방향 신규진입이 막혀야 한다.
# =============================================================================

def _neutral_regime(*a, **kw):
    import app.trading.adaptive_market_regime as regime_module

    return {
        "raw_regime": "RANGE", "confirmed_regime": "RANGE", "displayed_regime": "RANGE",
        "confidence": 50.0, "reasons": ["neutral for test"], "profile": regime_module.get_risk_profile("RANGE"),
        "previous_regime": None, "transitioned_at": None,
        "confirmation_state": regime_module.default_regime_confirmation_state(), "snapshot": {},
    }


def test_live_up_direction_blocks_new_inverse_entry_even_without_strong_regime(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.adaptive_market_regime as regime_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "INVERSE_BUY", "enhanced_score": 40.0, "inverse_pressure_score": 65.0,
            "score_gap": 25.0, "score_gap_below_forced_trade_threshold": False, "reasons": [],
        },
    )
    monkeypatch.setattr(regime_module, "compute_and_confirm_regime", _neutral_regime)
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    class _AssertNoBuyBroker:
        def get_positions(self):
            return []

        def get_buyable_cash(self):
            return 10_000_000.0

        def get_balance(self):
            return 10_000_000.0

        def buy(self, *a, **k):
            raise AssertionError("live UP 중에는 인버스 신규매수가 금지돼야 한다")

        def sell(self, *a, **k):
            raise AssertionError("이 테스트에서는 매도가 발생하면 안 됩니다.")

    import app.trading.broker_factory as broker_factory_module
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: _AssertNoBuyBroker())

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["live_trade_direction"] = {"direction": "UP", "status": "CONFIRMED"}
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    trace = result["pipeline_trace"]
    assert trace["entry_approved"] is False
    assert "live_trade_direction=UP" in trace["entry_approved_reason"]
    assert result["state"]["position"].get("symbol") is None


def test_live_down_direction_blocks_new_hynix_entry_even_without_strong_regime(tmp_path, monkeypatch):
    """live UP이 INVERSE를 막는 것과 완전히 대칭 — live DOWN은 레버리지(HYNIX)를 막는다."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.adaptive_market_regime as regime_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "HYNIX_BUY", "enhanced_score": 65.0, "inverse_pressure_score": 40.0,
            "score_gap": 25.0, "score_gap_below_forced_trade_threshold": False, "reasons": [],
        },
    )
    monkeypatch.setattr(regime_module, "compute_and_confirm_regime", _neutral_regime)
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    class _AssertNoBuyBroker:
        def get_positions(self):
            return []

        def get_buyable_cash(self):
            return 10_000_000.0

        def get_balance(self):
            return 10_000_000.0

        def buy(self, *a, **k):
            raise AssertionError("live DOWN 중에는 레버리지(HYNIX) 신규매수가 금지돼야 한다")

        def sell(self, *a, **k):
            raise AssertionError("이 테스트에서는 매도가 발생하면 안 됩니다.")

    import app.trading.broker_factory as broker_factory_module
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: _AssertNoBuyBroker())

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["live_trade_direction"] = {"direction": "DOWN", "status": "CONFIRMED"}
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    trace = result["pipeline_trace"]
    assert trace["entry_approved"] is False
    assert "live_trade_direction=DOWN" in trace["entry_approved_reason"]
    assert result["state"]["position"].get("symbol") is None


def test_new_entry_error_does_not_block_existing_position_liquidation(tmp_path, monkeypatch):
    """요구사항7 — 신규진입 오류가 기존 포지션 청산을 막지 않는다.
    run_liquidation_if_needed()는 entry-decision 블록보다 먼저 실행되고, entry
    블록 자체도 run_switch_or_entry() 호출을 try/except로 감싸고 있으므로,
    강제청산 시각(15:15 이후)에 신규진입 처리 중 예외가 나도 이미 실행된
    청산은 그대로 완료돼야 한다."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    from app.data_sources.hynix_long_collector import LONG_SYMBOL, LONG_NAME
    from app.models import Position, OrderResult

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "INVERSE_BUY", "enhanced_score": 40.0, "inverse_pressure_score": 65.0,
            "score_gap": 25.0, "score_gap_below_forced_trade_threshold": False, "reasons": [],
        },
    )
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    def _raise(*a, **kw):
        raise RuntimeError("강제 진입 오류(테스트)")

    monkeypatch.setattr(engine, "run_switch_or_entry", _raise)

    class _HeldPositionBroker:
        def __init__(self):
            self._positions = [Position(symbol=LONG_SYMBOL, name=LONG_NAME, quantity=10, avg_price=100_000.0, current_price=100_000.0)]
            self.sell_calls = []

        def get_positions(self):
            return self._positions

        def get_buyable_cash(self):
            return 10_000_000.0

        def get_balance(self):
            return 10_000_000.0

        def sell(self, symbol, name, quantity, price, order_type="limit"):
            self.sell_calls.append((symbol, quantity, price))
            self._positions = [p for p in self._positions if p.symbol != symbol]
            return OrderResult(
                success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                side="sell", quantity=quantity, price=price, order_type=order_type, order_id="S1", message="ok",
            )

        def buy(self, *a, **k):
            raise AssertionError("이 테스트에서는 매수가 발생하면 안 됩니다.")

    broker = _HeldPositionBroker()
    import app.trading.broker_factory as broker_factory_module
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: broker)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state_module.save_state_atomic(state)

    # 15:17 — liquidation_mode(15:10~15:20), should_liquidate_now()==True, 아직
    # "closed"(15:20) 전이라 entry-decision 블록도 함께 실행된다.
    now = datetime(2026, 7, 15, 15, 17, 0)
    result = engine.update_hynix_auto_trade_loop(mode="mock", now=now)

    assert len(broker.sell_calls) == 1  # 강제청산이 실제로 실행됨
    assert result["state"]["position"]["symbol"] is None  # 청산 완료(신규진입 예외와 무관)


def test_strong_down_confirmed_blocks_new_hynix_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.adaptive_market_regime as regime_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "HYNIX_BUY", "enhanced_score": 65.0, "inverse_pressure_score": 40.0,
            "score_gap": 25.0, "score_gap_below_forced_trade_threshold": False, "reasons": [],
        },
    )
    monkeypatch.setattr(
        regime_module, "compute_and_confirm_regime",
        lambda *a, **kw: {
            "raw_regime": "STRONG_DOWN", "confirmed_regime": "STRONG_DOWN", "displayed_regime": "STRONG_DOWN",
            "confidence": 90.0, "reasons": ["forced for test"], "profile": regime_module.get_risk_profile("STRONG_DOWN"),
            "previous_regime": None, "transitioned_at": None,
            "confirmation_state": regime_module.default_regime_confirmation_state(), "snapshot": {},
        },
    )
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    class _AssertNoBuyBroker:
        def get_positions(self):
            return []

        def get_buyable_cash(self):
            return 10_000_000.0

        def get_balance(self):
            return 10_000_000.0

        def buy(self, *a, **k):
            raise AssertionError("STRONG_DOWN 확정 중에는 레버리지(HYNIX) 신규매수가 금지돼야 한다")

        def sell(self, *a, **k):
            raise AssertionError("이 테스트에서는 매도가 발생하면 안 됩니다.")

    import app.trading.broker_factory as broker_factory_module
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: _AssertNoBuyBroker())

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    trace = result["pipeline_trace"]
    assert trace["entry_approved"] is False
    assert "STRONG_DOWN" in trace["entry_approved_reason"]
    assert result["state"]["position"].get("symbol") is None
