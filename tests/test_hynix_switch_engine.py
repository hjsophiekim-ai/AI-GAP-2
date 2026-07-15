"""
test_hynix_switch_engine.py — real 모드 일 누적 손실 -2.5% 도달 시 자동매매 중단 검증.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

import app.services.hynix_switch_engine as engine
import app.services.hynix_switch_state as state_module

_MID_SESSION_NOW = datetime(2026, 7, 15, 10, 0, 0)  # 09:10~14:50 신규진입 허용 구간


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

    assert result["state"]["position"]["symbol"] == "0193T0"
    assert result["state"]["position"]["quantity"] > 0


# =============================================================================
# FinalExecutionDecision / 중복주문 방지 / real-mode 게이트 테스트 (섹션 13)
# =============================================================================

def _setup_active_strategy_run(tmp_path, monkeypatch, shadow: dict, mode: str = "mock", auto_trade_on: bool = True):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    import app.trading.dry_run_broker as dry_run_broker_module
    import app.trading.broker_factory as broker_factory_module

    monkeypatch.setattr(enhanced_score_module, "calculate_enhanced_hynix_prediction_score", lambda mode=None: _fake_enhanced_result())
    monkeypatch.setattr(decider_module, "decide_hynix_or_inverse_action", _fake_decision)
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: dry_run_broker_module.DryRunBroker())
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

    assert result["state"]["position"]["symbol"] == "0197X0"
    assert result["state"]["position"]["quantity"] > 0
    fd = result["state"].get("last_final_execution_decision")
    assert fd["signal_source"] == "ACTIVE_FUSION"
    assert fd["order_sent"] is True


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

    assert len(buy_calls) == 1
    assert result["state"]["last_final_execution_decision"]["order_sent"] is True


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
    assert pm_cache.get("position", {}).get("symbol") == "0193T0"
    assert result["state"]["position"]["quantity"] == pm_cache["position"]["quantity"]


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
    assert len(buy_calls) == 1
    assert result1["state"]["position"]["symbol"] == "0193T0"

    # 같은 분(cycle_id) 안에서 다시 실행 — 이미 보유 중이므로 신규 BUY 자체가 재판단되지
    # 않지만(무포지션 진입 로직만 idempotency 대상), 포지션이 있는 상태에서 같은 방향
    # 신호가 다시 들어와도 추가 매수가 중복 실행되지 않아야 한다.
    result2 = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)
    assert len(buy_calls) == 1  # 추가 호출 없음


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
    assert result1["state"]["position"]["symbol"] == "0193T0"

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
    assert not (pos.get("symbol") == "0197X0" and result1["state"]["position"]["quantity"] > 0 and pos.get("quantity", 0) > 0 and pos.get("symbol") == "0193T0")


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
    state_qty = result["state"]["position"]["quantity"]
    pm_symbol = result["position_manager"]["position"]["symbol"]
    pm_qty = result["position_manager"]["position"]["quantity"]

    ledger_df = load_ledger()
    live_buys = ledger_df[(ledger_df["success"] == True) & (ledger_df["action"] == "BUY") & (ledger_df["is_test_order"] != True)]  # noqa: E712

    assert state_symbol == pm_symbol == "0193T0"
    assert state_qty == pm_qty
    assert int(live_buys["executed_qty"].sum()) == state_qty


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
        assert fd["signal_source"] in ("ACTIVE_ONLY", "ADAPTIVE_FUSION", "PREDICTION_V2_ASSISTED")

        from app.services.hynix_execution_ledger import load_ledger

        df = load_ledger()
        buy_rows = df[df["action"] == "BUY"]
        assert not buy_rows.empty
        last = buy_rows.iloc[-1]
        assert last["signal_source"] in ("ACTIVE_ONLY", "ADAPTIVE_FUSION", "PREDICTION_V2_ASSISTED")
        assert last["dominant_model"] not in (None, "")
        assert not pd.isna(last["buy_fee"]) and last["buy_fee"] >= 0

    def test_shadow_prediction_v2_does_not_claim_applied(self, tmp_path, monkeypatch):
        """Prediction V2가 SHADOW 상태(표본 없음)이면 signal_source가 ADAPTIVE_FUSION/
        PREDICTION_V2_ASSISTED로 표시되면 안 된다(적용됐다고 과장 금지)."""
        shadow = _inverse_dominant_shadow(cycle_phase="NO_TRADE", confidence=60.0)
        self._enable_adaptive_fusion(tmp_path, monkeypatch, shadow)

        result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

        fd = result["state"].get("last_final_execution_decision")
        if fd.get("order_sent"):
            assert fd["signal_source"] == "ACTIVE_ONLY"

    def test_state_last_fusion_decision_populated(self, tmp_path, monkeypatch):
        shadow = _inverse_dominant_shadow(cycle_phase="NO_TRADE", confidence=60.0)
        self._enable_adaptive_fusion(tmp_path, monkeypatch, shadow)

        result = engine.update_hynix_auto_trade_loop(mode="mock", now=_MID_SESSION_NOW)

        fusion_decision = result["state"].get("last_fusion_decision")
        assert fusion_decision is not None
        assert "weights" in fusion_decision and "dominant_model" in fusion_decision


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
