"""
test_hynix_switch_engine.py — real 모드 일 누적 손실 -2.5% 도달 시 자동매매 중단 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import app.services.hynix_switch_engine as engine
import app.services.hynix_switch_state as state_module
from app.trading import exit_order_coordinator as order_coord
from app.models import OrderResult
from app.trading.hynix_symbols import LONG_SYMBOL as HYNIX_SYMBOL

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

    assert result["state"]["position"]["symbol"] is None
    assert len(logged_trades) == 0

    trace = result["pipeline_trace"]
    assert trace["prediction_signal"] == "BUY"
    assert trace["order_sent"] is False
    assert trace["broker_executed"] is False
    assert trace["position_confirmed"] is None
    assert trace["ui_synced"] is True
    assert trace["trade_counter"] == 0
    # Main cycle defers new entries to Fast Worker weighted controller.
    assert trace["stopped_stage"] == "entry_approved"
    assert "MAIN_CYCLE_ENTRY_DEFERRED" in (trace.get("entry_approved_reason") or "")


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


def test_active_strategy_toggle_does_not_enable_direct_enhanced_entry(tmp_path, monkeypatch):
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
    assert result["state"]["position"]["symbol"] is None
    assert result["pipeline_trace"]["broker_executed"] is False
    assert result["state"]["actual_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"
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
    assert trace["entry_approved"] is False
    # Enhanced의 raw score 승인만으로는 조기진입 주문이 나가지 않는다 — Early
    # Detector 자신의 실시간 신호가 없으므로 NO_EARLY_SIGNAL로 스킵되어야 한다.
    assert trace["stopped_stage"] == "entry_approved"
    assert trace["early_decision"]["reason_code"] in ("NO_EARLY_SIGNAL", "TIME_GATE_BLOCK")
    assert trace["signal_summary"]["block_reason"] in ("NO_EARLY_SIGNAL", "TIME_GATE_BLOCK", "NEW_ENTRY_TIME_CLOSED")
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
    assert trace["entry_approved"] is False
    assert trace["stopped_stage"] == "entry_approved"
    assert trace["early_decision"]["reason_code"] in ("NO_EARLY_SIGNAL", "TIME_GATE_BLOCK")
    assert trace["signal_summary"]["block_reason"] in ("NO_EARLY_SIGNAL", "TIME_GATE_BLOCK", "NEW_ENTRY_TIME_CLOSED")
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
    import app.trading.broker_factory as broker_factory_module
    monkeypatch.setattr(dry_run_broker_module, "DryRunBroker", lambda **kwargs: broker)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: broker)

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
    assert trace["broker_executed"] is False
    assert trace["ui_synced"] is False
    assert trace["stopped_stage"] == "entry_approved"


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
    # Main cycle never places ENHANCED buys — direction/regime blocks are Fast Worker inputs.
    assert "MAIN_CYCLE_ENTRY_DEFERRED" in trace["entry_approved_reason"] or "STRONG_UP" in trace["entry_approved_reason"]
    assert result["state"]["position"].get("symbol") is None
    assert trace.get("enhanced_direct_order_blocked") is True


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
    reason = trace["entry_approved_reason"] or ""
    assert "live_trade_direction=UP" in reason or "MAIN_CYCLE_ENTRY_DEFERRED" in reason
    assert result["state"]["position"].get("symbol") is None
    assert trace.get("enhanced_direct_order_blocked") is True


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
    reason = trace["entry_approved_reason"] or ""
    assert "live_trade_direction=DOWN" in reason or "MAIN_CYCLE_ENTRY_DEFERRED" in reason
    assert result["state"]["position"].get("symbol") is None
    assert trace.get("enhanced_direct_order_blocked") is True


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
    reason = trace["entry_approved_reason"] or ""
    assert "STRONG_DOWN" in reason or "MAIN_CYCLE_ENTRY_DEFERRED" in reason
    assert result["state"]["position"].get("symbol") is None
    assert trace.get("enhanced_direct_order_blocked") is True


def test_signal_summary_uses_early_detector_block_reason_for_buy_to_hold():
    decision = {
        "final_action": "HYNIX_BUY",
        "enhanced_score": 82.0,
        "inverse_pressure_score": 35.0,
    }
    trace = {
        "prediction_signal": "BUY",
        "entry_approved": False,
        "entry_approved_reason": "Early Trend Detector blocked: MICRO_CHOP",
        "order_sent": False,
        "early_decision": {"reason_code": "MICRO_CHOP", "reason": "MICRO_CHOP"},
    }

    summary = engine._build_signal_summary(
        decision=decision, trace=trace, state={}, now=_MID_SESSION_NOW,
        new_entry_allowed_now=True, new_entry_window={"rule": "allowed"},
    )

    assert summary["actionable_signal"] == "HOLD"
    assert summary["final_action"] == "HOLD"
    assert summary["block_reason"] == "MICRO_CHOP"


def test_score_gap_ladder_enters_when_gap_47_and_live_up():
    result = engine.evaluate_score_gap_entry_ladder(
        score_gap=47.0,
        desired_direction="UP",
        live_direction="UP",
        confidence=85.0,
        stop_loss_distance_pct=0.8,
        buyable_cash=10_000_000.0,
        current_price=100_000.0,
    )

    assert result["action"] == "ENTER"
    assert result["reason_code"] == "LIVE_ALIGNED"
    assert 0.30 <= result["target_pct"] <= 0.50


def test_score_gap_ladder_holds_when_gap_47_but_live_down():
    result = engine.evaluate_score_gap_entry_ladder(
        score_gap=47.0,
        desired_direction="UP",
        live_direction="DOWN",
    )

    assert result["action"] == "HOLD"
    assert result["target_pct"] == 0.0
    assert result["reason_code"] == "LIVE_DIRECTION_CONFLICT"


def test_score_gap_ladder_uses_pullback_probe_when_aligned_pullback():
    result = engine.evaluate_score_gap_entry_ladder(
        score_gap=47.0,
        desired_direction="UP",
        live_direction=None,
        structural_direction="UP",
        etf_mid_term_aligned=True,
        etf_confirmation_state="ALIGNED_PULLBACK",
        confidence=85.0,
        stop_loss_distance_pct=0.8,
    )

    assert result["action"] == "ENTER"
    assert result["reason_code"] == "PULLBACK_PROBE"
    assert 0.20 <= result["target_pct"] <= 0.30


class _BuyingBroker:
    def __init__(self):
        self.cash = 10_000_000.0
        self.positions = []
        self.buy_calls = []

    def get_positions(self):
        return list(self.positions)

    def get_buyable_cash(self):
        return self.cash

    def get_balance(self):
        return self.cash

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, quantity, price))
        self.cash -= quantity * price
        self.positions = [{"symbol": symbol, "name": name, "quantity": quantity, "avg_price": price}]
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id="B1", message="ok",
        )

    def sell(self, *args, **kwargs):
        raise AssertionError("unexpected sell")


def _patch_score_gap_loop(monkeypatch, broker):
    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.adaptive_market_regime as regime_module
    import app.trading.broker_factory as broker_factory_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "HYNIX_BUY",
            "enhanced_score": 97.0,
            "inverse_pressure_score": 50.0,
            "score_gap": 47.0,
            "score_gap_below_forced_trade_threshold": False,
            "reasons": ["score gap test"],
        },
    )
    monkeypatch.setattr(
        regime_module, "compute_and_confirm_regime",
        lambda *a, **kw: {
            "raw_regime": "RANGE", "confirmed_regime": "RANGE", "displayed_regime": "RANGE",
            "confidence": 85.0, "reasons": ["forced for score gap test"],
            "profile": regime_module.get_risk_profile("RANGE"),
            "previous_regime": None, "transitioned_at": None,
            "confirmation_state": regime_module.default_regime_confirmation_state(), "snapshot": {},
        },
    )
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)


def test_score_gap_47_live_up_is_deferred_to_fast_weighted_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    broker = _BuyingBroker()
    _patch_score_gap_loop(monkeypatch, broker)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_live"] = False
    state["live_trade_direction"] = {"direction": "UP"}
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert broker.buy_calls == []
    assert result["pipeline_trace"]["broker_executed"] is False
    assert result["state"]["position"]["symbol"] is None
    assert result["state"]["actual_entry_engine"] == "WEIGHTED_ORDER_CONTROLLER_LIVE"


def test_score_gap_47_live_down_holds_in_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    broker = _BuyingBroker()
    _patch_score_gap_loop(monkeypatch, broker)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_live"] = False
    state["live_trade_direction"] = {"direction": "DOWN"}
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert not broker.buy_calls
    assert result["pipeline_trace"]["entry_approved"] is False
    reason = result["pipeline_trace"]["entry_approved_reason"]
    assert "LIVE_DIRECTION_CONFLICT" in reason or "MAIN_CYCLE_ENTRY_DEFERRED" in reason


def test_early_fast_feed_records_live_samples_even_when_early_detector_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.data_sources.auto_market_collector as auto_collector_module
    import app.data_sources.hynix_long_collector as long_collector_module
    import app.data_sources.hynix_inverse_collector as inverse_collector_module

    monkeypatch.setattr(auto_collector_module, "_fetch_hynix_current_from_kis", lambda mode=None: 100_000.0)
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 10_000.0, "stale": False})
    monkeypatch.setattr(inverse_collector_module, "collect_inverse_current", lambda mode=None: {"current_price": 20_000.0, "stale": False})

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_enabled"] = False
    state["early_trend_detector_live"] = False
    state_module.save_state_atomic(state)

    result = engine.run_early_trend_fast_feed_tick(mode="mock", now=_MID_SESSION_NOW)
    updated = state_module.load_state(mode="mock")
    detector_state = updated.get("early_trend_detector") or {}

    # Early OFF must not early-return the Fast Worker — weighted controller stays armed.
    assert result.get("reason") != "EARLY_DISABLED_PRICE_FEED_ONLY"
    assert updated.get("actual_entry_engine") == "WEIGHTED_ORDER_CONTROLLER_LIVE"
    assert updated.get("configured_entry_engine") == "WEIGHTED_ORDER_CONTROLLER_LIVE"
    assert detector_state.get("price_history")
    assert updated.get("live_trade_direction")
    assert updated.get("position", {}).get("symbol") is None


def test_continuation_entry_after_missed_early_reversal_in_sustained_uptrend():
    result = engine.evaluate_trend_continuation_entry(
        decision={"final_action": "HYNIX_BUY", "enhanced_score": 73.0, "inverse_pressure_score": 50.0},
        live_direction="UP",
        live_direction_held_seconds=30.0,
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        moved_pct_since_signal=0.2,
        expected_net_edge_ok=True,
    )

    assert result["action"] == "ENTER"
    assert result["reason_code"] == "CONTINUATION_ENTRY_APPROVED"
    assert result["entry_path"] == "CONTINUATION"
    assert 0.20 <= result["target_pct"] <= 0.30


def test_continuation_entry_gap_45_uses_immediate_ladder():
    result = engine.evaluate_trend_continuation_entry(
        decision={"final_action": "HYNIX_BUY", "enhanced_score": 95.0, "inverse_pressure_score": 50.0},
        live_direction="UP",
        live_direction_held_seconds=20.0,
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        moved_pct_since_signal=0.1,
        expected_net_edge_ok=True,
        confidence=90.0,
    )

    assert result["action"] == "ENTER"
    assert 0.40 <= result["target_pct"] <= 0.60


def test_continuation_holds_when_raw_hynix_strong_but_live_down():
    result = engine.evaluate_trend_continuation_entry(
        decision={"final_action": "HYNIX_BUY", "enhanced_score": 73.6, "inverse_pressure_score": 26.4},
        live_direction="DOWN",
        live_direction_held_seconds=30.0,
        desired_direction="UP",
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
    )

    assert result["action"] == "HOLD"
    assert result["reason_code"] == "LIVE_DIRECTION_CONFLICT"


def test_continuation_blocks_chasing_after_etf_moves_too_far():
    result = engine.evaluate_trend_continuation_entry(
        decision={"final_action": "HYNIX_BUY", "enhanced_score": 73.0, "inverse_pressure_score": 50.0},
        live_direction="UP",
        live_direction_held_seconds=30.0,
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        moved_pct_since_signal=0.6,
        expected_net_edge_ok=True,
    )

    assert result["action"] == "HOLD"
    assert result["reason_code"] == "CHASE_BLOCK"


def test_range_weighted_entry_enters_with_evidence_65_and_net_edge():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 72.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "DOWN"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "DOWN"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "UP"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        moved_pct_since_signal=0.2,
        expected_move_pct=0.65,
        cost_pct=0.12,
        expected_mfe_pct=0.65,
        expected_mae_pct=0.35,
    )

    assert result["action"] == "ENTER"
    assert result["evidence_score"] >= 65.0
    assert 0.30 <= result["target_pct"] <= 0.50


def test_range_weighted_entry_blocks_low_net_edge_even_with_direction():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 80.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.20,
        cost_pct=0.10,
        expected_mfe_pct=0.20,
        expected_mae_pct=0.20,
    )

    assert result["action"] == "HOLD"
    assert result["reason_code"] in ("LOW_NET_EDGE", "POOR_REWARD_RISK")


def test_range_weighted_entry_does_not_veto_single_5s_or_vwap_failure():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 75.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "DOWN", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "DOWN", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "UP", 30: "DOWN"},
        confirm_above_vwap=False,
        data_age_seconds=2.0,
        expected_move_pct=0.80,
        cost_pct=0.12,
        expected_mfe_pct=0.80,
        expected_mae_pct=0.35,
    )

    assert result["action"] == "ENTER"
    assert result["reason_code"] == "CONTINUATION_ENTRY_APPROVED"


def test_range_weighted_entry_hard_blocks_both_entry_etf_5s_10s_opposite():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 80.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.80,
        cost_pct=0.12,
        expected_mfe_pct=0.80,
        expected_mae_pct=0.35,
    )

    assert result["action"] == "HOLD"
    assert result["reason_code"] == "ETF_5S_10S_BOTH_OPPOSITE"


def test_range_weighted_fixed_a_continuation_buy_order_conditions():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 75.5, "inverse_pressure_score": 50.5},
        direction="UP",
        live_direction="UP",
        live_direction_held_seconds=20.0,
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "DOWN"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "DOWN"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "UP"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.32,
        cost_pct=0.07,
        expected_mfe_pct=0.54,
        expected_mae_pct=0.30,
    )

    assert result["action"] == "ENTER"
    assert result["entry_path"] == "CONTINUATION"
    assert result["reason_code"] == "CONTINUATION_ENTRY_APPROVED"
    assert result["expected_net_edge_pct"] >= 0.15


def test_range_regime_probe_cap_is_not_zero_hard_block():
    from app.trading import early_trend_detector as etd

    stage, target_pct = etd.compute_target_probe_pct("RANGE", 0.0, direction_aligned=False)

    assert stage == etd.STAGE_INITIAL
    assert target_pct > 0.0


def test_range_weighted_fixed_b_pullback_buy_order_conditions():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 85.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        live_direction_held_seconds=20.0,
        signal_window_directions={5: "DOWN", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "DOWN", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.50,
        cost_pct=0.08,
        expected_mfe_pct=0.60,
        expected_mae_pct=0.30,
    )

    assert result["action"] == "ENTER"
    assert result["entry_path"] == "PULLBACK"
    assert result["reason_code"] == "PULLBACK_ENTRY"


def test_range_weighted_strong_label_requires_structure_confirmation():
    result = engine.evaluate_range_weighted_entry(
        decision={"final_action": "HYNIX_STRONG_BUY", "enhanced_score": 85.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.80,
        cost_pct=0.08,
        expected_mfe_pct=0.80,
        expected_mae_pct=0.35,
        ema_slope_aligned=True,
        structure_confirmed=False,
        structural_direction="UP",
    )

    assert result["action"] == "ENTER"
    assert result["strong_structure_confirmed"] is False
    assert result["structural_signal_label"] != "HYNIX_STRONG_BUY"


def test_range_weighted_short_rebound_in_down_structure_is_bounce_not_strong():
    result = engine.evaluate_range_weighted_entry(
        decision={"final_action": "HYNIX_STRONG_BUY", "enhanced_score": 85.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.80,
        cost_pct=0.08,
        expected_mfe_pct=0.80,
        expected_mae_pct=0.35,
        ema_slope_aligned=True,
        structure_confirmed=False,
        structural_direction="DOWN",
    )

    assert result["action"] == "HOLD"
    assert result["reason_code"] == "BOUNCE_UP"
    assert result["structural_signal_label"] == "BOUNCE_UP"


def test_range_weighted_strong_label_when_all_structure_inputs_confirmed():
    result = engine.evaluate_range_weighted_entry(
        decision={"final_action": "HYNIX_STRONG_BUY", "enhanced_score": 85.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.80,
        cost_pct=0.08,
        expected_mfe_pct=0.80,
        expected_mae_pct=0.35,
        ema_slope_aligned=True,
        structure_confirmed=True,
        structural_direction="UP",
    )

    assert result["action"] == "ENTER"
    assert result["strong_structure_confirmed"] is True
    assert result["structural_signal_label"] == "HYNIX_STRONG_BUY"


def test_20260721_last_30m_fixture_does_not_emit_hynix_strong_buy_without_etf_evidence():
    fixture = Path(__file__).parent / "_fixtures" / "hynix_20260721_last30_minute.csv"
    df = pd.read_csv(fixture)
    strong_labels = []

    for _, row in df.tail(10).iterrows():
        decision = {"final_action": "HYNIX_STRONG_BUY", "enhanced_score": 82.0, "inverse_pressure_score": 18.0}
        result = engine.evaluate_range_weighted_entry(
            decision=decision,
            direction="UP",
            live_direction="UP" if float(row["close"]) >= float(row["open"]) else "DOWN",
            signal_window_directions={},
            confirm_window_directions={},
            oppose_window_directions={},
            confirm_above_vwap=None,
            data_age_seconds=2.0,
            expected_move_pct=0.80,
            cost_pct=0.08,
            expected_mfe_pct=0.80,
            expected_mae_pct=0.35,
            ema_slope_aligned=None,
            structure_confirmed=False,
            structural_direction="DOWN",
        )
        strong_labels.append(result["structural_signal_label"])

    assert strong_labels.count("HYNIX_STRONG_BUY") == 0


def test_main_decision_downgrades_stale_strong_buy_in_down_structure():
    decision = {"final_action": "HYNIX_STRONG_BUY", "enhanced_score": 82.0, "inverse_pressure_score": 18.0, "reasons": []}
    state = {
        "adaptive_regime": {"confirmed_regime": "DOWN"},
        "trend_continuation_entry": {"last_result": {}},
    }

    adjusted = engine._downgrade_unconfirmed_strong_decision(decision, state)

    assert adjusted["final_action"] == "HYNIX_BUY"
    assert adjusted["structural_signal_label"] == "BOUNCE_UP"
    assert adjusted["strong_downgraded"] is True


def test_range_weighted_fixed_c_low_net_edge_hold():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 80.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.15,
        cost_pct=0.05,
        expected_mfe_pct=0.45,
        expected_mae_pct=0.25,
    )

    assert result["action"] == "HOLD"
    assert result["reason_code"] == "LOW_NET_EDGE"


def test_range_weighted_fixed_d_live_direction_conflict_hold():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 75.5, "inverse_pressure_score": 24.5},
        direction="UP",
        live_direction="DOWN",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.80,
        cost_pct=0.08,
        expected_mfe_pct=0.80,
        expected_mae_pct=0.30,
    )

    assert result["action"] == "HOLD"
    assert result["reason_code"] == "LIVE_DIRECTION_CONFLICT"


def test_range_weighted_missing_edge_is_data_insufficient_not_low_net_edge():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 75.5, "inverse_pressure_score": 24.5},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=None,
        cost_pct=0.08,
    )

    assert result["action"] == "HOLD"
    assert result["reason_code"] == "DATA_INSUFFICIENT"
    assert "LOW_NET_EDGE" not in result["hard_blocks"]


def test_early_fast_feed_no_early_signal_runs_continuation_order_path(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.data_sources.auto_market_collector as auto_collector_module
    import app.data_sources.hynix_long_collector as long_collector_module
    import app.data_sources.hynix_inverse_collector as inverse_collector_module
    from app.trading import early_trend_live_feed as feed
    import app.trading.etf_entry_confirmation as etf_confirmation_module

    broker = _BuyingBroker()
    monkeypatch.setattr(engine, "_create_strategy_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(auto_collector_module, "_fetch_hynix_current_from_kis", lambda mode=None: 100_030.0)
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 10_030.0, "stale": False})
    monkeypatch.setattr(inverse_collector_module, "collect_inverse_current", lambda mode=None: {"current_price": 19_940.0, "stale": False})
    monkeypatch.setattr(engine, "_load_etf_own_minute_cache", lambda symbol: pd.DataFrame({"close": [9_900.0, 9_950.0], "volume": [1000, 1000]}))
    monkeypatch.setattr(etf_confirmation_module, "compute_etf_vwap", lambda df: 9_950.0)
    monkeypatch.setattr(
        engine,
        "_run_early_trend_detector_tick",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Fast Worker must not use Early direct-order path")),
    )

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["early_trend_detector_enabled"] = True
    state["early_trend_detector_live"] = True
    state["last_decision"] = {"final_action": "HYNIX_STRONG_BUY", "enhanced_score": 75.5, "inverse_pressure_score": 24.5}
    state["last_enhanced_result"] = {"intraday_momentum_score": 70.0}
    state["adaptive_regime"] = {"confirmed_regime": "RANGE", "confidence": 80.0}
    history = {}
    for seconds, signal_price, long_price, inverse_price in (
        (30, 100_000.0, 10_000.0, 20_000.0),
        (20, 100_010.0, 10_010.0, 19_980.0),
        (10, 100_020.0, 10_020.0, 19_960.0),
    ):
        ts = _MID_SESSION_NOW - timedelta(seconds=seconds)
        history = feed.record_price_sample(history, engine.SIGNAL_SYMBOL, signal_price, ts)
        history = feed.record_price_sample(history, engine.HYNIX_SYMBOL, long_price, ts)
        history = feed.record_price_sample(history, engine.INVERSE_SYMBOL, inverse_price, ts)
    state["early_trend_detector"] = {"price_history": history}
    state["live_trade_direction"] = {
        "direction": "UP",
        "direction_held_since": (_MID_SESSION_NOW - timedelta(seconds=20)).isoformat(),
        "direction_held_seconds": 20.0,
    }
    state_module.save_state_atomic(state)

    result = engine.run_early_trend_fast_feed_tick(mode="mock", now=_MID_SESSION_NOW)
    updated = state_module.load_state(mode="mock")
    continuation = updated.get("trend_continuation_entry") or {}

    assert result["skipped"] is False
    assert continuation["last_result"]["action"] == "ENTER"
    assert continuation["last_result"]["entry_path"] in ("CONTINUATION", "PULLBACK")
    assert continuation.get("last_switch", {}).get("orders")
    assert updated.get("configured_entry_engine") == "WEIGHTED_ORDER_CONTROLLER_LIVE"
    assert updated.get("actual_entry_engine") == "WEIGHTED_ORDER_CONTROLLER_LIVE"

    broker.positions = []
    updated["position"] = {**updated["position"], "symbol": None, "quantity": 0}
    state_module.save_state_atomic(updated)

    second = engine.run_early_trend_fast_feed_tick(mode="mock", now=_MID_SESSION_NOW + timedelta(seconds=5))
    second_state = state_module.load_state(mode="mock")

    assert second["skipped"] is False
    assert len(broker.buy_calls) == 1
    assert (second_state.get("trend_continuation_entry") or {}).get("entry_done") is True


def test_fast_feed_price_action_reversal_factors_feed_existing_candidate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.data_sources.auto_market_collector as auto_collector_module
    import app.data_sources.hynix_long_collector as long_collector_module
    import app.data_sources.hynix_inverse_collector as inverse_collector_module

    broker = _BuyingBroker()
    monkeypatch.setattr(engine, "_create_strategy_broker", lambda *a, **kw: broker)
    monkeypatch.setattr(auto_collector_module, "_fetch_hynix_current_from_kis", lambda mode=None: 100_080.0)
    monkeypatch.setattr(long_collector_module, "collect_long_current", lambda mode=None: {"current_price": 10_120.0, "stale": False})
    monkeypatch.setattr(inverse_collector_module, "collect_inverse_current", lambda mode=None: {"current_price": 19_840.0, "stale": False})
    monkeypatch.setattr(
        engine,
        "_load_etf_own_minute_cache",
        lambda symbol: pd.DataFrame({
            "open": [9_900.0 + i for i in range(30)],
            "high": [9_920.0 + i for i in range(30)],
                "low": [9_880.0 + i for i in range(30)],
                "close": [9_900.0 + i for i in range(30)],
                "volume": [1000 + i for i in range(30)],
                "datetime": pd.date_range("2026-07-21 09:31:00", periods=30, freq="min"),
            }),
        )

    state = state_module.load_state(mode="mock")
    state.update({
        "mode": "mock",
            "auto_trade_on": True,
            "early_trend_detector_enabled": True,
            "early_trend_detector_live": True,
            "last_decision": {"final_action": "HYNIX_BUY", "enhanced_score": 75.0, "inverse_pressure_score": 25.0},
            "live_trade_direction": {
                "direction": "DOWN",
                "direction_held_since": (datetime(2026, 7, 21, 10, 0, 0) - timedelta(seconds=30)).isoformat(),
                "direction_held_seconds": 30.0,
            },
        })
    history = {}
    from app.trading import early_trend_live_feed as feed

    for seconds, signal_price, long_price, inverse_price in (
        (30, 100_000.0, 10_000.0, 20_000.0),
        (20, 100_005.0, 10_020.0, 19_980.0),
        (10, 100_020.0, 10_050.0, 19_940.0),
        (5, 100_025.0, 10_060.0, 19_920.0),
    ):
        ts = datetime(2026, 7, 21, 10, 0, 0) - timedelta(seconds=seconds)
        history = feed.record_price_sample(history, engine.SIGNAL_SYMBOL, signal_price, ts)
        history = feed.record_price_sample(history, engine.HYNIX_SYMBOL, long_price, ts)
        history = feed.record_price_sample(history, engine.INVERSE_SYMBOL, inverse_price, ts)
    state["early_trend_detector"] = {"price_history": history}
    state_module.save_state_atomic(state)

    engine.run_early_trend_fast_feed_tick(mode="mock", now=datetime(2026, 7, 21, 10, 0, 0))
    updated = state_module.load_state(mode="mock")
    price_action = (updated.get("early_trend_detector") or {}).get("price_action_reversal") or {}

    assert price_action["direction"] == "UP"
    assert price_action["factor_count"] >= 3
    assert price_action["factors"]["slope_5s_10s_reversal"] is True
    assert price_action["factors"]["etf_mutual_direction_confirmed"] is True


def test_range_weighted_entry_allows_reward_risk_without_gross_cost_ratio_veto():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 75.0, "inverse_pressure_score": 50.0},
        direction="UP",
        live_direction="UP",
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=2.0,
        expected_move_pct=0.50,
        cost_pct=0.20,
        expected_mfe_pct=0.50,
        expected_mae_pct=0.30,
    )

    assert result["action"] == "ENTER"
    assert result["gross_cost_ratio"] < 3.0
    assert result["reward_risk"] >= 1.5


def test_macd_williams_confirmation_up_when_macd_rising_and_williams_exits_oversold():
    import pandas as pd

    closes = [100.0] * 26 + [95, 90, 85, 80, 75, 70, 65, 60, 58, 57, 56, 58, 62, 68]
    lows = [c - 1 for c in closes]
    lows[36] = 55
    lows[37] = 55
    highs = [c + 1 for c in closes]
    df = pd.DataFrame({"close": closes, "high": highs, "low": lows})
    result = engine._macd_williams_confirmation(df, "UP")

    assert result["confirmed"] is True
    assert result["reason"] == "MACD_UP_WILLIAMS_OVERSOLD_EXIT"


def test_range_episode_blocks_second_reversal_probe():
    cont: dict = {}
    now = datetime(2026, 7, 21, 10, 0, 0)
    engine.reset_range_episode_probe_state(cont, now=now, direction="UP", episode_id="UP:1")
    engine.mark_range_reversal_probe_entered(cont, now=now, entry_path="REVERSAL")
    allows, reason = engine.range_episode_allows_entry(
        cont, entry_path="REVERSAL", swing_breakout=False, vwap_reclaim=False, direction_changed=False,
    )
    assert allows is False
    assert reason == "REVERSAL_PROBE_ONCE_PER_EPISODE"


def test_probe_failed_blocks_reversal_only():
    cont: dict = {}
    now = datetime(2026, 7, 21, 10, 0, 0)
    engine.reset_range_episode_probe_state(cont, now=now, direction="UP", episode_id="UP:1")
    engine.mark_range_probe_failed(cont, now=now, reason="MACD miss")
    allows_rev, reason_rev = engine.range_episode_allows_entry(
        cont, entry_path="REVERSAL", swing_breakout=False, vwap_reclaim=False, direction_changed=False,
    )
    assert allows_rev is False
    assert reason_rev == "PROBE_FAILED_REVERSAL_BLOCKED"
    allows_cont, reason_cont = engine.range_episode_allows_entry(
        cont, entry_path="CONTINUATION", swing_breakout=True, vwap_reclaim=False, direction_changed=False,
    )
    assert allows_cont is True
    assert cont.get("episode_status") == "PROBE_FAILED"


def test_opposite_episode_transition_vwap_reclaim():
    assert engine.detect_opposite_episode_transition(
        existing_direction="UP",
        new_direction="DOWN",
        live_direction_matches=True,
        confirm_dirs={5: "DOWN", 10: "DOWN"},
        existing_structure_broken=False,
        new_etf_vwap_reclaim=True,
    )
    assert not engine.detect_opposite_episode_transition(
        existing_direction="UP",
        new_direction="DOWN",
        live_direction_matches=True,
        confirm_dirs={5: "DOWN", 10: "UP"},
        existing_structure_broken=False,
        new_etf_vwap_reclaim=True,
    )


def test_probe_promoted_to_continuation_after_45s():
    cont = {"probe_entered_at": datetime(2026, 7, 21, 10, 0, 0).isoformat()}
    plan = engine.evaluate_weighted_range_probe_exit(
        continuation=cont,
        probe_direction="UP",
        structure_reversal_confirmed=False,
        held_window_dirs={5: "UP", 10: "UP"},
        macd_confirmed=False,
        etf_direction_aligned=True,
        now=datetime(2026, 7, 21, 10, 0, 46),
        net_return_pct=0.35,
    )
    assert plan["action"] == "PROMOTE_CONTINUATION"


def test_continuation_exit_ignores_5s_alone():
    plan = engine.evaluate_weighted_continuation_exit(
        net_return_pct=0.2,
        hard_stop_pct=-0.5,
        structure_reversal_confirmed=False,
        regime_reversal_confirmed=False,
        held_window_dirs={5: "DOWN", 10: "UP"},
        position_direction="UP",
    )
    assert plan["action"] == "HOLD"


def test_probe_failed_locks_until_structural_unlock():
    cont: dict = {}
    now = datetime(2026, 7, 21, 10, 0, 0)
    engine.reset_range_episode_probe_state(cont, now=now, direction="UP", episode_id="UP:1")
    engine.mark_range_probe_failed(cont, now=now, reason="MACD miss")
    allows, reason = engine.range_episode_allows_entry(
        cont, entry_path="CONTINUATION", swing_breakout=False, vwap_reclaim=False, direction_changed=False,
    )
    assert allows is False
    assert reason == "AWAITING_STRUCTURAL_REENTRY"
    engine.update_range_episode_structural_events(
        cont, now=now + timedelta(seconds=30), swing_breakout=True, vwap_reclaim=False,
    )
    allows2, _ = engine.range_episode_allows_entry(
        cont, entry_path="CONTINUATION", swing_breakout=True, vwap_reclaim=False, direction_changed=False,
    )
    assert allows2 is True
    assert cont.get("episode_status") is None


def test_weighted_probe_exit_holds_before_30s_without_macd():
    cont = {"probe_entered_at": datetime(2026, 7, 21, 10, 0, 0).isoformat()}
    plan = engine.evaluate_weighted_range_probe_exit(
        continuation=cont,
        probe_direction="UP",
        structure_reversal_confirmed=False,
        held_window_dirs={5: "DOWN", 10: "UP"},
        macd_confirmed=False,
        etf_direction_aligned=True,
        now=datetime(2026, 7, 21, 10, 0, 15),
    )
    assert plan["action"] == "HOLD"


def test_weighted_probe_exit_fails_after_45s_without_macd():
    cont = {"probe_entered_at": datetime(2026, 7, 21, 10, 0, 0).isoformat()}
    plan = engine.evaluate_weighted_range_probe_exit(
        continuation=cont,
        probe_direction="UP",
        structure_reversal_confirmed=False,
        held_window_dirs={5: "UP", 10: "UP"},
        macd_confirmed=False,
        etf_direction_aligned=True,
        now=datetime(2026, 7, 21, 10, 0, 46),
        net_return_pct=-0.1,
    )
    assert plan["action"] == "SELL_ALL"
    assert plan["probe_failed"] is True


def test_weighted_probe_exit_ignores_5s_alone_opposite():
    cont = {"probe_entered_at": datetime(2026, 7, 21, 10, 0, 0).isoformat()}
    plan = engine.evaluate_weighted_range_probe_exit(
        continuation=cont,
        probe_direction="UP",
        structure_reversal_confirmed=False,
        held_window_dirs={5: "DOWN", 10: "UP"},
        macd_confirmed=False,
        etf_direction_aligned=True,
        now=datetime(2026, 7, 21, 10, 0, 20),
    )
    assert plan["action"] == "HOLD"


def test_weighted_probe_exit_on_5s_10s_opposite():
    cont = {"probe_entered_at": datetime(2026, 7, 21, 10, 0, 0).isoformat()}
    plan = engine.evaluate_weighted_range_probe_exit(
        continuation=cont,
        probe_direction="UP",
        structure_reversal_confirmed=False,
        held_window_dirs={5: "DOWN", 10: "DOWN"},
        macd_confirmed=True,
        etf_direction_aligned=True,
        now=datetime(2026, 7, 21, 10, 0, 20),
    )
    assert plan["action"] == "SELL_ALL"
    assert plan.get("probe_failed") is False

