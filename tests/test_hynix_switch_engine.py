"""
test_hynix_switch_engine.py — real 모드 일 누적 손실 -2.5% 도달 시 자동매매 중단 검증.
"""

from __future__ import annotations

from datetime import datetime

import app.services.hynix_switch_engine as engine
import app.services.hynix_switch_state as state_module

_MID_SESSION_NOW = datetime(2026, 7, 9, 10, 0, 0)  # 09:10~14:50 신규진입 허용 구간


class _FakeBroker:
    def __init__(self, cash: float = 1_000_000.0):
        self.cash = cash

    def get_positions(self):
        return []

    def get_buyable_cash(self):
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
        def full_auto_real_confirm_ok(self):
            return True

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

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", lambda enhanced, current_position=None: {
        "final_action": "HYNIX_BUY", "enhanced_score": 80.0, "inverse_pressure_score": 10.0,
        "score_gap": 70.0, "score_gap_below_forced_trade_threshold": False, "reasons": ["test"],
    })

    # mock 모드는 이제 DryRunBroker(로컬 시뮬레이션)를 사용 — 그 상태파일 경로만 tmp_path로 격리
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)

    logged_trades = []
    monkeypatch.setattr(engine, "log_trade", lambda record: logged_trades.append(record))
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    _silence_prediction_tracker(monkeypatch)

    state = state_module.load_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state_module.save_state_atomic(state)

    result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

    assert result["state"]["position"]["symbol"] == "000660"
    assert len(logged_trades) == 1

    trace = result["pipeline_trace"]
    assert trace["prediction_signal"] == "BUY"
    assert trace["order_sent"] is True
    assert trace["broker_executed"] is True
    assert trace["position_confirmed"] is True
    assert trace["ui_synced"] is True
    assert trace["trade_counter"] == 1
    assert trace["stopped_stage"] is None


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

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", lambda enhanced, current_position=None: {
        "final_action": "HYNIX_BUY", "enhanced_score": 80.0, "inverse_pressure_score": 10.0,
        "score_gap": 70.0, "score_gap_below_forced_trade_threshold": False, "reasons": ["test"],
    })
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
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
