"""tests/test_daily_equity_calculation.py

risk_manager의 일일손익 계산을 원장(실현손익) + 미실현손익 기준으로 통합한 회귀
테스트(2026-07-14). 이전에는 risk_manager가 "지금 이 순간의 계좌평가액(total_equity)
/ 시작자산" 비율로 손익을 계산했는데, KIS API 오류(레이트리밋 등) 시
get_buyable_cash()가 예외 없이 0.0을 반환하는 하위호환 계약 때문에 total_equity=0으로
읽혀 "일 누적 손실 -100%"로 오판, MOCK 자동매매가 실제로는 +0.3355% 수익 중인데도
잘못 정지되는 사고가 있었다.
"""
from __future__ import annotations

import app.services.hynix_switch_engine as engine


def _state(realized_pnl_krw=0.0, baseline=10_000_000.0):
    return {
        "realized_pnl_today_krw": realized_pnl_krw,
        "daily_pnl_baseline_equity": baseline,
    }


class _FakePosition:
    def __init__(self, symbol, quantity, market_value=0.0):
        self.symbol = symbol
        self.quantity = quantity
        self.market_value = market_value


# ---------------------------------------------------------------------------
# 1) 1,000만원 매수 후 전량매도, 33,546원 수익이면 +0.33546%
# ---------------------------------------------------------------------------

def test_realized_gain_after_full_exit_computes_correct_positive_pct():
    state = _state(realized_pnl_krw=33_545.89, baseline=10_000_000.0)
    result = engine.compute_net_daily_return(
        state, position=None, hynix_price=None, inverse_price=None,
        cash=10_033_545.89, positions_from_broker=[], cash_fetch_ok=True,  # baseline + net_realized_pnl(포지션 없음)
    )
    assert result["blocked_reason"] is None
    assert result["net_daily_return"] == round(33_545.89 / 10_000_000.0 * 100.0, 4)
    assert abs(result["net_daily_return"] - 0.3355) < 0.001


# ---------------------------------------------------------------------------
# 2) 보유 0주일 때 미실현손익은 0
# ---------------------------------------------------------------------------

def test_no_position_means_zero_unrealized_pnl():
    state = _state(realized_pnl_krw=10_000.0)
    empty_position = {
        "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
    }
    result = engine.compute_net_daily_return(
        state, position=empty_position, hynix_price=101_000.0, inverse_price=5_000.0,
        cash=10_010_000.0, positions_from_broker=[], cash_fetch_ok=True,
    )
    assert result["net_unrealized_pnl"] == 0.0
    assert result["blocked_reason"] is None


# ---------------------------------------------------------------------------
# 3) 잔고 API 빈 응답/실패를 -100%로 처리하지 않는다
# ---------------------------------------------------------------------------

def test_cash_fetch_failure_returns_unknown_not_minus_100():
    state = _state(realized_pnl_krw=33_545.89, baseline=10_000_000.0)
    result = engine.compute_net_daily_return(
        state, position=None, hynix_price=None, inverse_price=None,
        cash=None, positions_from_broker=None, cash_fetch_ok=False,
    )
    assert result["blocked_reason"] == engine.DAILY_RETURN_UNKNOWN
    assert result["net_daily_return"] is None
    # 실현손익 원장 자체는 훼손되지 않아야 한다.
    assert result["net_realized_pnl"] == 33_545.89


def test_cash_zero_with_existing_ledger_is_flagged_as_mismatch_not_minus_100():
    """계좌조회는 '성공'했다고 응답했지만(cash_fetch_ok=True) cash=0으로 실제로는
    API 글리치인 경우(2026-07-14 실측 시나리오) — -100%로 기록하면 안 되고
    ACCOUNT_EQUITY_MISMATCH로 표시해야 한다."""
    state = _state(realized_pnl_krw=33_545.89, baseline=9_998_580.0)
    result = engine.compute_net_daily_return(
        state, position=None, hynix_price=None, inverse_price=None,
        cash=0.0, positions_from_broker=[], cash_fetch_ok=True,
    )
    assert result["blocked_reason"] == engine.DAILY_RETURN_UNKNOWN
    assert result["net_daily_return"] != -100.0
    assert result["net_daily_return"] is None


def test_recent_order_mismatch_is_deferred_not_blocked():
    from datetime import datetime, timedelta

    now = datetime(2026, 7, 14, 10, 0, 30)
    state = _state(realized_pnl_krw=20_000.0, baseline=10_000_000.0)
    state["last_trade_time"] = (now - timedelta(seconds=30)).isoformat()
    result = engine.compute_net_daily_return(
        state, position=None, hynix_price=None, inverse_price=None,
        cash=9_500_000.0, positions_from_broker=[], cash_fetch_ok=True,
        settlement_grace_active=True,
    )
    assert result["blocked_reason"] is None
    assert result["mismatch_deferred"] is True


def test_three_consecutive_equity_mismatches_are_required_to_block(monkeypatch):
    from datetime import datetime

    class _FakeKis:
        def __init__(self):
            self.calls = 0

        def get_balance(self):
            self.calls += 1
            return {"cash": 9_000_000.0, "orderable_cash": 9_000_000.0, "positions": []}

    class _FakeBroker:
        def __init__(self):
            self.kis = _FakeKis()

    state = _state(realized_pnl_krw=50_000.0, baseline=10_000_000.0)
    result = engine._compute_net_daily_return_with_retries(
        state, _FakeBroker(), None, None, None, datetime(2026, 7, 14, 10, 0),
        attempts=3, delay_seconds=0,
    )
    assert result["blocked_reason"] == engine.ACCOUNT_EQUITY_MISMATCH
    assert result["equity_check_attempts"] == 3


def test_transient_equity_mismatch_resolved_by_retry_does_not_block():
    from datetime import datetime

    class _FakeKis:
        def __init__(self):
            self.calls = 0

        def get_balance(self):
            self.calls += 1
            if self.calls < 3:
                return {"cash": 9_000_000.0, "orderable_cash": 9_000_000.0, "positions": []}
            return {"cash": 10_050_000.0, "orderable_cash": 10_050_000.0, "positions": []}

    class _FakeBroker:
        def __init__(self):
            self.kis = _FakeKis()

    state = _state(realized_pnl_krw=50_000.0, baseline=10_000_000.0)
    result = engine._compute_net_daily_return_with_retries(
        state, _FakeBroker(), None, None, None, datetime(2026, 7, 14, 10, 0),
        attempts=3, delay_seconds=0,
    )
    assert result["blocked_reason"] is None
    assert result["equity_check_attempts"] == 3
    assert result["net_daily_return"] == 0.5


# ---------------------------------------------------------------------------
# 4) 잔고조회 실패 시 신규주문만 보류(정지 상태로 기록하지 않음) — 엔진 통합 시나리오
# ---------------------------------------------------------------------------

def test_engine_pauses_new_orders_without_setting_stopped_flag(tmp_path, monkeypatch):
    import app.services.hynix_switch_state as state_module

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    import app.models.hynix_enhanced_score as enhanced_score_module
    import app.models.hynix_action_decider as decider_module

    monkeypatch.setattr(
        enhanced_score_module, "calculate_enhanced_hynix_prediction_score",
        lambda mode=None: {
            "base_prediction_score": 50.0, "existing_micron_score": 50.0, "hynix_technical_score": 50.0,
            "intraday_momentum_score": 50.0, "inverse_pressure_score": 50.0, "enhanced_score": 50.0,
            "reason_top5": [], "data_valid": {"base_prediction": True, "hynix_technical": True},
            "hynix_current_price": 100_000.0, "inverse_current_price": 5_000.0, "inverse_price_stale": False,
            "market_data": {"hynix_minute": {"df_1min": None}}, "warnings": [],
        },
    )
    monkeypatch.setattr(
        decider_module, "decide_hynix_or_inverse_action",
        # HOLD가 아니라 방향성 신호를 줘야 한다 — _first_blocked_stage()는 prediction_signal이
        # HOLD면 항상 stopped_stage=None(정상)으로 처리하므로, risk_manager 차단이 실제로
        # stopped_stage에 반영되는지 검증하려면 신호 자체가 방향성이어야 한다.
        lambda enhanced, current_position=None: {
            "final_action": "INVERSE_BUY", "enhanced_score": 40.0, "inverse_pressure_score": 65.0,
            "score_gap": 25.0, "score_gap_below_forced_trade_threshold": False, "reasons": [],
        },
    )
    monkeypatch.setattr(engine, "log_trade", lambda record: None)
    monkeypatch.setattr(engine, "log_enhanced_prediction", lambda record: None)

    import app.services.hynix_prediction_tracker as tracker_module
    monkeypatch.setattr(tracker_module, "log_trade_decision", lambda *a, **kw: None)
    monkeypatch.setattr(tracker_module, "check_and_resolve_pending_outcomes", lambda *a, **kw: [])

    class _BadCashBroker:
        def get_positions(self):
            return []

        def get_buyable_cash(self):
            return 0.0  # API 오류 시 하위호환 폴백(예외 없이 0 반환)

    import app.trading.broker_factory as broker_factory_module
    monkeypatch.setattr(broker_factory_module, "create_broker", lambda *a, **kw: _BadCashBroker())

    state = state_module.load_state(mode="mock")
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["daily_pnl_baseline_equity"] = 10_000_000.0
    state["realized_pnl_today_krw"] = 33_545.89
    state_module.save_state_atomic(state)

    from datetime import datetime
    result = engine.update_hynix_auto_trade_loop(mode="mock", now=datetime(2026, 7, 14, 10, 0))

    final_state = result["state"]
    assert final_state.get("stopped") is not True, "일시적 계좌조회 이상을 영구 정지 상태로 기록하면 안 된다"
    trace = result["pipeline_trace"]
    assert trace["risk_manager_ok"] is False
    assert trace["stopped_stage"] == "risk_manager"
    assert "DAILY_RETURN_UNKNOWN" in (trace.get("risk_manager_reason") or "") or "ACCOUNT_EQUITY_MISMATCH" in (trace.get("risk_manager_reason") or "")
    # 실제 순손익(+0.3355%대)이 -100%로 뒤바뀌어 기록되지 않아야 한다.
    assert final_state.get("realized_pnl_today_pct", 0.0) > -1.0


# ---------------------------------------------------------------------------
# 5) 일손실 -2.5% "실제" 도달 시에만 신규주문 차단(정상 케이스 회귀 방지)
# ---------------------------------------------------------------------------

def test_real_minus_2_5_percent_loss_still_blocks_correctly():
    state = _state(realized_pnl_krw=-260_000.0, baseline=10_000_000.0)
    result = engine.compute_net_daily_return(
        state, position=None, hynix_price=None, inverse_price=None,
        cash=9_740_000.0, positions_from_broker=[], cash_fetch_ok=True,
    )
    assert result["blocked_reason"] is None
    assert result["net_daily_return"] <= -2.5


def test_small_real_loss_does_not_trigger_false_block():
    state = _state(realized_pnl_krw=-50_000.0, baseline=10_000_000.0)
    result = engine.compute_net_daily_return(
        state, position=None, hynix_price=None, inverse_price=None,
        cash=9_950_000.0, positions_from_broker=[], cash_fetch_ok=True,
    )
    assert result["blocked_reason"] is None
    assert result["net_daily_return"] > -2.5


# ---------------------------------------------------------------------------
# 6) UI와 risk_manager가 동일한 수익률을 사용한다(요구사항 5절)
# ---------------------------------------------------------------------------

def test_risk_manager_and_ui_use_identical_return_value():
    state = _state(realized_pnl_krw=33_545.89, baseline=10_000_000.0)
    result = engine.compute_net_daily_return(
        state, position=None, hynix_price=None, inverse_price=None,
        cash=10_033_545.89, positions_from_broker=[], cash_fetch_ok=True,  # baseline + net_realized_pnl(포지션 없음)
    )
    risk_manager_pct = result["net_daily_return"]
    # UI 표시 로직(엔진의 후반부)이 쓰는 것과 동일한 공식으로 직접 재계산해 일치를 검증.
    ui_pct = round(
        (state.get("realized_pnl_today_krw", 0.0) + result["net_unrealized_pnl"]) / result["starting_equity"] * 100.0, 4,
    )
    assert risk_manager_pct == ui_pct
    assert result["calculation_source"] == "ledger_unified"
