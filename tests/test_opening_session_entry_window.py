"""
test_opening_session_entry_window.py — 장초반 신규진입 시간창 규칙 검증(2026-07-20).

기존 09:00~09:10 관망(watch-only) 규칙은 완전히 삭제되고, 아래 3구간 규칙으로
대체됐다:
  09:00~09:15 신규진입 허용
  09:15~09:30 신규진입 금지
  09:30~14:50 신규진입 허용(기존과 동일)
기존 포지션의 손절/익절/반전청산/15:15 강제청산은 이 시간창과 무관하게 항상
실행되어야 한다.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.trading.hynix_switch_risk_gate import (
    is_new_entry_allowed, describe_new_entry_window, is_within_operating_window,
)


# ── 신규진입 허용/금지 3구간 ──────────────────────────────────────────────────

@pytest.mark.parametrize("hm,expected", [
    ((8, 59), False),   # 장 시작 전
    ((9, 0), True),     # 09:00 — 허용 시작
    ((9, 14), True),
    ((9, 15), False),   # 09:15 — 금지 시작
    ((9, 29), False),
    ((9, 30), True),    # 09:30 — 허용 재개
    ((10, 0), True),
    ((14, 49), True),
    ((14, 50), False),  # 기존 컷오프는 유지
    ((15, 0), False),
])
def test_is_new_entry_allowed_matches_three_window_rule(hm, expected):
    now = datetime(2026, 7, 20, *hm)
    assert is_new_entry_allowed(now) is expected


def test_old_0900_0910_watch_only_rule_is_gone():
    """요구사항 — 기존 09:00~09:10 관망 규칙은 완전히 삭제한다. 09:05는 이제
    신규진입이 허용되어야 한다(과거에는 관망 구간이라 금지였음)."""
    now = datetime(2026, 7, 20, 9, 5)
    assert is_new_entry_allowed(now) is True


def test_is_watch_only_no_longer_exists():
    import app.trading.hynix_switch_risk_gate as risk_gate

    assert not hasattr(risk_gate, "is_watch_only")


# ── UI 표시용 규칙 설명 ───────────────────────────────────────────────────────

def test_describe_new_entry_window_reports_allowed_and_rule_text():
    allowed_case = describe_new_entry_window(datetime(2026, 7, 20, 9, 5))
    assert allowed_case["allowed"] is True
    assert "09:00" in allowed_case["rule"] and "09:15" in allowed_case["rule"]

    blocked_case = describe_new_entry_window(datetime(2026, 7, 20, 9, 20))
    assert blocked_case["allowed"] is False
    assert "09:15" in blocked_case["rule"] and "09:30" in blocked_case["rule"]
    assert "청산" in blocked_case["rule"]  # 청산은 계속 실행된다는 안내 포함

    reopened_case = describe_new_entry_window(datetime(2026, 7, 20, 10, 0))
    assert reopened_case["allowed"] is True

    before_open_case = describe_new_entry_window(datetime(2026, 7, 20, 8, 30))
    assert before_open_case["allowed"] is False
    assert "장 시작 전" in before_open_case["rule"]

    after_cutoff_case = describe_new_entry_window(datetime(2026, 7, 20, 15, 0))
    assert after_cutoff_case["allowed"] is False
    assert "이후" in after_cutoff_case["rule"]


# ── 기존 포지션 청산은 시간창과 무관 ──────────────────────────────────────────

def test_liquidation_and_tp_sl_are_not_gated_by_new_entry_window(tmp_path, monkeypatch):
    """요구사항 — 09:15~09:30(신규진입 금지 구간)에도 보유 포지션의 손절/익절/
    반전청산/15:15 강제청산은 정상 실행되어야 한다. main 3분 사이클의
    trading_allowed(청산 실행 게이트)가 신규진입 시간창과 분리됐는지 확인한다."""
    import app.services.hynix_switch_state as state_module
    import app.services.hynix_switch_engine as engine
    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module
    from app.data_sources.hynix_long_collector import LONG_SYMBOL, LONG_NAME
    from app.models import Position, OrderResult

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    monkeypatch.setattr(
        enhanced_score_module, "calculate_enhanced_hynix_prediction_score",
        lambda mode=None: {
            "base_prediction_score": 50.0, "existing_micron_score": 50.0, "hynix_technical_score": 50.0,
            "intraday_momentum_score": 50.0, "inverse_pressure_score": 50.0, "enhanced_score": 50.0,
            "reason_top5": [], "data_valid": {"base_prediction": True, "existing_micron": True, "hynix_technical": True, "intraday_momentum": True},
            "hynix_current_price": 100_000, "inverse_current_price": 5_000, "inverse_price_stale": False,
            "micron_detail": {}, "tech_detail": {}, "momentum_detail": {}, "inverse_detail": {},
            "market_data": {"hynix_minute": {"df_1min": None}}, "warnings": [],
        },
    )
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        lambda enhanced, current_position=None: {
            "final_action": "HOLD", "enhanced_score": 50.0, "inverse_pressure_score": 50.0,
            "score_gap": 0.0, "score_gap_below_forced_trade_threshold": True, "reasons": [],
        },
    )
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)
    import app.services.hynix_prediction_tracker as tracker_module
    monkeypatch.setattr(tracker_module, "log_trade_decision", lambda *a, **kw: None)
    monkeypatch.setattr(tracker_module, "check_and_resolve_pending_outcomes", lambda *a, **kw: [])

    # -3%로 손절 임계를 확실히 넘는 가격으로 TP/SL이 실제 발동하게 한다.
    class _HeldLosingPositionBroker:
        def __init__(self):
            self._positions = [Position(symbol=LONG_SYMBOL, name=LONG_NAME, quantity=10, avg_price=100_000.0, current_price=97_000.0)]
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
            return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                                side="sell", quantity=quantity, price=price, order_type=order_type, order_id="S1", message="ok")

        def buy(self, *a, **k):
            raise AssertionError("이 테스트에서는 매수가 발생하면 안 됩니다.")

    broker = _HeldLosingPositionBroker()
    import app.trading.broker_factory as broker_factory_module
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: broker)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["position"] = {
        **state["position"], "symbol": LONG_SYMBOL, "name": LONG_NAME, "quantity": 10,
        "avg_price": 100_000.0, "entry_price": 100_000.0, "entry_time": "2026-07-20T09:00:00",
    }
    state_module.save_state_atomic(state)

    # 09:20 — 신규진입 금지 구간이지만 손절(TP/SL)은 정상 실행되어야 한다.
    now = datetime(2026, 7, 20, 9, 20, 0)
    result = engine.update_hynix_auto_trade_loop(mode="mock", now=now)

    assert len(broker.sell_calls) == 1, "09:15~09:30 신규진입 금지 구간에도 보유 포지션 손절은 실행되어야 한다"
    trace = result["pipeline_trace"]
    assert trace["risk_manager_ok"] is True, "신규진입 시간창은 risk_manager(청산 게이트)를 막으면 안 된다"


# ── 모든 신규진입 경로(Fast Watcher 포함)에 동일 적용 ─────────────────────────

def test_fast_watcher_skips_during_0915_0930_blackout(tmp_path, monkeypatch):
    import app.services.hynix_switch_state as state_module
    import app.services.hynix_switch_engine as engine

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state_module.save_state_atomic(state)

    result = engine.run_fast_trend_watcher_tick(mode="mock", now=datetime(2026, 7, 20, 9, 20, 0))
    assert result["skipped"] is True
    assert "09:15" in result["reason"] and "09:30" in result["reason"]


def test_fast_watcher_runs_during_0900_0915_window(tmp_path, monkeypatch):
    import app.services.hynix_switch_state as state_module
    import app.services.hynix_switch_engine as engine
    import app.models.hynix_enhanced_score as enhanced_score_module

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        enhanced_score_module, "calculate_enhanced_hynix_prediction_score",
        lambda mode=None: {
            "hynix_current_price": 100_000, "inverse_current_price": 5_000,
            "hynix_prev_close": 99_000, "market_data": {"hynix_minute": {"df_1min": None}},
        },
    )
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state_module.save_state_atomic(state)

    result = engine.run_fast_trend_watcher_tick(mode="mock", now=datetime(2026, 7, 20, 9, 5, 0))
    # 09:05는 신규진입 허용 구간이므로, "신규진입 금지 시간대" 사유로 스킵되면 안 된다
    # (데이터 부족 등 다른 사유로 스킵되는 것은 이 테스트의 관심사가 아니다).
    if result.get("skipped"):
        assert "09:15" not in (result.get("reason") or "")
