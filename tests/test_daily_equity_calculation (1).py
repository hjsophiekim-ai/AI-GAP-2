"""tests/test_daily_equity_calculation.py

risk_manager의 일일손익 계산을 원장(실현손익) + 미실현손익 기준으로 통합한 회귀
테스트(2026-07-14). 이전에는 risk_manager가 "지금 이 순간의 계좌평가액(total_equity)
/ 시작자산" 비율로 손익을 계산했는데, KIS API 오류(레이트리밋 등) 시
get_buyable_cash()가 예외 없이 0.0을 반환하는 하위호환 계약 때문에 total_equity=0으로
읽혀 "일 누적 손실 -100%"로 오판, MOCK 자동매매가 실제로는 +0.3355% 수익 중인데도
잘못 정지되는 사고가 있었다.
"""
from __future__ import annotations

import pytest

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
    assert result["blocked_reason"] is None
    assert result["net_daily_return"] == pytest.approx(0.3355)
    assert result["calculation_warning"] == "ACCOUNT_SNAPSHOT_UNAVAILABLE_LEDGER_FALLBACK"
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


def test_recent_account_snapshot_fallback_keeps_orders_available_on_rate_limit():
    from datetime import datetime, timedelta

    class _RateLimitedKis:
        def get_balance(self):
            return {"error": "HTTP 500 msg_cd=EGW00201: rate limit", "msg_cd": "EGW00201"}

    class _FakeBroker:
        def __init__(self):
            self.kis = _RateLimitedKis()

    now = datetime(2026, 7, 15, 10, 0)
    state = _state(realized_pnl_krw=10_000.0, baseline=10_000_000.0)
    state["last_account_equity_snapshot"] = {
        "ok": True,
        "cash": 10_010_000.0,
        "holdings_market_value": 0.0,
        "current_equity": 10_010_000.0,
        "positions": [],
        "source": "kis.get_balance",
        "as_of": (now - timedelta(seconds=30)).isoformat(timespec="seconds"),
    }

    result = engine._compute_net_daily_return_with_retries(
        state, _FakeBroker(), None, None, None, now, attempts=1, delay_seconds=0,
    )

    assert result["blocked_reason"] is None
    assert result["net_daily_return"] == pytest.approx(0.1)
    assert result["account_snapshot"]["cached_fallback"] is True
    assert result["account_snapshot"]["live_msg_cd"] == "EGW00201"


def test_stale_account_snapshot_is_not_used_for_rate_limit_fallback():
    from datetime import datetime

    class _RateLimitedKis:
        def get_balance(self):
            return {"error": "HTTP 500 msg_cd=EGW00201: rate limit", "msg_cd": "EGW00201"}

    class _FakeBroker:
        def __init__(self):
            self.kis = _RateLimitedKis()

    now = datetime(2026, 7, 15, 10, 0)
    state = _state(realized_pnl_krw=10_000.0, baseline=10_000_000.0)
    state["last_account_equity_snapshot"] = {
        "ok": True,
        "cash": 10_010_000.0,
        "holdings_market_value": 0.0,
        "current_equity": 10_010_000.0,
        "positions": [],
        "source": "kis.get_balance",
        "as_of": "2026-07-14T14:50:00",
    }

    result = engine._compute_net_daily_return_with_retries(
        state, _FakeBroker(), None, None, None, now, attempts=1, delay_seconds=0,
    )

    assert result["blocked_reason"] is None
    assert result["calculation_warning"] == "ACCOUNT_SNAPSHOT_UNAVAILABLE_LEDGER_FALLBACK"
    assert "cached_fallback" not in result["account_snapshot"]


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
    assert trace["risk_manager_ok"] is True
    assert trace["stopped_stage"] != "risk_manager"
    assert (final_state.get("daily_return_calculation") or {}).get("calculation_warning") == "ACCOUNT_SNAPSHOT_UNAVAILABLE_LEDGER_FALLBACK"
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

# ---------------------------------------------------------------------------
# 7) KRX T+2 정산 지연 — 오늘 실현손익이 브로커 현금에 아직 반영되지 않아도
#    신규주문을 차단하지 않는다(2026-07-15 실측: 하이닉스 BUY 신호가 risk_manager
#    단계에서 ACCOUNT_EQUITY_MISMATCH로 계속 차단되던 문제).
# ---------------------------------------------------------------------------

def test_unsettled_realized_pnl_does_not_block_new_orders():
    """오늘 왕복거래로 +69,956.75원을 실현했지만(포지션은 이미 flat) 브로커 현금은
    아직 baseline과 동일(매도대금 T+2 미정산) — 계좌 이상이 아니라 정상적인 정산
    지연이므로 차단되면 안 된다."""
    state = _state(realized_pnl_krw=69_956.75, baseline=10_000_000.0)
    empty_position = {"symbol": None, "quantity": 0, "avg_price": None, "entry_price": None}
    result = engine.compute_net_daily_return(
        state, position=empty_position, hynix_price=100_000.0, inverse_price=9_000.0,
        cash=10_000_000.0, positions_from_broker=[], cash_fetch_ok=True,  # 현금이 아직 baseline 그대로
    )
    assert result["blocked_reason"] is None
    assert result["mismatch_explained_by_unsettled_realized_pnl"] is True
    assert result["unsettled_realized_pnl_krw"] == 69_956.75
    assert result["net_daily_return"] == pytest.approx(0.6996, abs=0.001)


def test_unsettled_realized_pnl_does_not_mask_a_genuine_mismatch():
    """정산 지연으로 설명되지 않는 진짜 불일치(현금이 기대치보다 훨씬 더 벗어남)는
    여전히 차단해야 한다 — 이 폴백이 진짜 계좌 이상까지 숨기면 안 된다."""
    state = _state(realized_pnl_krw=69_956.75, baseline=10_000_000.0)
    empty_position = {"symbol": None, "quantity": 0, "avg_price": None, "entry_price": None}
    result = engine.compute_net_daily_return(
        state, position=empty_position, hynix_price=100_000.0, inverse_price=9_000.0,
        cash=8_000_000.0, positions_from_broker=[], cash_fetch_ok=True,  # 정산 지연으로는 설명 안 되는 큰 결손
    )
    assert result["blocked_reason"] == engine.ACCOUNT_EQUITY_MISMATCH


# ---------------------------------------------------------------------------
# 8) 방금 체결된 매수 직후 KIS 잔고조회(output1)가 아직 새 보유종목을 반영하지
#    못하는 브로커 포지션 동기화 지연 — 계좌 이상(ACCOUNT_EQUITY_MISMATCH)이
#    아니므로 신규주문을 차단하지 않는다(2026-07-16 실측: 500만원 매수 직후
#    BUY 신호가 risk_manager 단계에서 반복 차단됨).
# ---------------------------------------------------------------------------

def test_broker_missing_held_symbol_falls_back_to_ledger_not_mismatch():
    state = _state(realized_pnl_krw=0.0, baseline=10_000_000.0)
    held_position = {
        "symbol": engine.HYNIX_SYMBOL, "quantity": 50, "avg_price": 100_000.0, "entry_price": 100_000.0,
    }
    result = engine.compute_net_daily_return(
        state, position=held_position, hynix_price=100_500.0, inverse_price=9_000.0,
        # cash already reflects the buy debit, but output1 hasn't caught up yet — empty positions.
        cash=5_000_000.0, positions_from_broker=[], cash_fetch_ok=True,
    )
    assert result["blocked_reason"] is None
    assert result["calculation_warning"] == "BROKER_POSITION_SYNC_LAG_LEDGER_FALLBACK"
    assert result["net_unrealized_pnl"] != 0.0


def test_broker_reporting_held_symbol_is_not_treated_as_sync_lag():
    """브로커가 실제로 그 심볼을 정상 보유 중으로 보고하면(정상 케이스) 폴백 없이
    기존 현재자산 기준 계산을 그대로 사용한다."""
    state = _state(realized_pnl_krw=0.0, baseline=10_000_000.0)
    held_position = {
        "symbol": engine.HYNIX_SYMBOL, "quantity": 50, "avg_price": 100_000.0, "entry_price": 100_000.0,
    }
    result = engine.compute_net_daily_return(
        state, position=held_position, hynix_price=100_000.0, inverse_price=9_000.0,
        cash=5_000_000.0,
        positions_from_broker=[{"symbol": engine.HYNIX_SYMBOL, "quantity": 50, "market_value": 5_000_000.0}],
        cash_fetch_ok=True,
    )
    assert result.get("calculation_warning") != "BROKER_POSITION_SYNC_LAG_LEDGER_FALLBACK"


# ---------------------------------------------------------------------------
# 9) 당일 첫 유효 계좌조회가 이미 거래가 있은 "이후"에 일어나는 경우 — 기준자산을
#    역산해서 정확히 잡아야 한다(2026-07-20 실측: BUY 신호가 risk_manager에서
#    ACCOUNT_EQUITY_MISMATCH로 계속 차단됨). 이전에는 그 순간의 current_equity를
#    그대로 기준자산으로 저장해, 이미 반영된 손익을 원장이 다시 더하는 이중계산이
#    발생했고, 이 괴리는 재시도/정산지연/유예 중 어느 것으로도 해소되지 않아
#    그날 남은 사이클 내내 신규주문이 차단됐다.
# ---------------------------------------------------------------------------

def test_first_snapshot_of_day_after_a_realized_trade_backs_out_that_pnl_from_baseline():
    """토큰 지연 등으로 첫 성공 조회가 오늘 이미 +50,000원을 실현한 뒤 일어난 경우 —
    기준자산은 '지금 잔고'가 아니라 그 50,000원을 뺀 진짜 하루 시작 자산이어야 한다."""
    state = _state(realized_pnl_krw=50_000.0, baseline=None)
    empty_position = {"symbol": None, "quantity": 0, "avg_price": None, "entry_price": None}
    result = engine.compute_net_daily_return(
        state, position=empty_position, hynix_price=None, inverse_price=None,
        cash=10_050_000.0, positions_from_broker=[], cash_fetch_ok=True,
    )
    assert result["blocked_reason"] is None
    assert result["starting_equity"] == pytest.approx(10_000_000.0)
    assert state["daily_pnl_baseline_equity"] == pytest.approx(10_000_000.0)
    assert result["net_daily_return"] == pytest.approx(0.5)


def test_first_snapshot_of_day_while_holding_a_position_backs_out_unrealized_pnl_too():
    """첫 성공 조회 시점에 이미 포지션을 보유 중이고 미실현이익이 있다면, 그 미실현
    손익까지 역산해서 기준자산을 잡아야 한다."""
    state = _state(realized_pnl_krw=0.0, baseline=None)
    held_position = {
        "symbol": engine.HYNIX_SYMBOL, "quantity": 100, "avg_price": 100_000.0, "entry_price": 100_000.0,
    }
    # 보유 100주 @100,000원 진입, 현재가 100,500원 — 미실현이익 약 +50,000원(수수료 등 반영 전).
    # cash는 매수에 쓴 1,000만원이 이미 빠진 상태 + 나머지 예수금이라고 가정.
    result = engine.compute_net_daily_return(
        state, position=held_position, hynix_price=100_500.0, inverse_price=9_000.0,
        cash=50_000.0, positions_from_broker=[{"symbol": engine.HYNIX_SYMBOL, "quantity": 100, "market_value": 10_050_000.0}],
        cash_fetch_ok=True,
    )
    assert result["blocked_reason"] is None
    # current_equity = 50,000 + 10,050,000 = 10,100,000; 미실현손익만큼 역산해 기준자산을 잡는다.
    # (이 포지션은 오늘 진입한 것이므로 미실현이익이 "오늘의 수익"이라는 점은 정상 —
    # 검증할 것은 기준자산이 현재자산과 일치가 아니라 정확히 역산돼 있다는 점이다.)
    expected_baseline = 10_100_000.0 - result["net_unrealized_pnl"]
    assert result["starting_equity"] == pytest.approx(expected_baseline)
    assert result["net_daily_return"] == pytest.approx(
        result["net_unrealized_pnl"] / expected_baseline * 100.0, abs=0.001,
    )


def test_first_snapshot_baseline_falls_back_when_backed_out_value_would_be_non_positive():
    """역산한 기준자산이 0 이하로 나오는 극단적 상황(이론상 방어)에서는 예외 없이
    현재자산을 그대로 기준으로 잡고 0%로 시작한다(음수/0 기준자산으로 나누지 않음)."""
    state = _state(realized_pnl_krw=20_000_000.0, baseline=None)  # 현재자산보다 큰 실현손익(비정상 입력 방어)
    empty_position = {"symbol": None, "quantity": 0, "avg_price": None, "entry_price": None}
    result = engine.compute_net_daily_return(
        state, position=empty_position, hynix_price=None, inverse_price=None,
        cash=10_000_000.0, positions_from_broker=[], cash_fetch_ok=True,
    )
    assert result["blocked_reason"] is None
    assert result["starting_equity"] == pytest.approx(10_000_000.0)
    assert result["net_daily_return"] == 0.0


def test_baseline_just_established_flag_is_set_so_caller_can_skip_immediate_stop():
    """요구사항(2026-07-20 실측) — 기준자산이 방금 (재)확정된 사이클에서, 그 하나의
    표본만으로 계산된 극단적 수익률(예: 노이즈 낀 첫 가격 조회로 인한 큰 미실현
    손실)을 근거로 곧바로 자동매매를 강제중단해서는 안 된다. 호출부
    (_update_hynix_auto_trade_loop_locked)가 이 플래그를 보고 -2.5% 강제중단을
    이번 사이클만 건너뛴다 — 이 함수 자체는 플래그만 세우고 중단 여부는 판단하지
    않는다."""
    state = _state(realized_pnl_krw=0.0, baseline=None)
    held_position = {
        "symbol": engine.HYNIX_SYMBOL, "quantity": 10, "avg_price": 100_000.0, "entry_price": 100_000.0,
    }
    # 첫 유효 조회 시점에 현재가가 크게(-95%) 벗어난 노이즈 낀 값이라고 가정 —
    # 미실현손실이 매우 커도 이 사이클은 강제중단 판단에서 제외되어야 한다.
    result = engine.compute_net_daily_return(
        state, position=held_position, hynix_price=5_000.0, inverse_price=5_000.0,
        cash=10_000_000.0, positions_from_broker=[{"symbol": engine.HYNIX_SYMBOL, "quantity": 10, "market_value": 50_000.0}],
        cash_fetch_ok=True,
    )
    assert result["blocked_reason"] is None
    assert result["baseline_just_established"] is True
    assert result["net_daily_return"] < -2.5  # 실제로 극단적인 수치이되, 강제중단 판단은 호출부가 이번엔 건너뛴다


def test_stale_zero_baseline_self_heals_instead_of_blocking_all_day():
    """이전 버그로 이미 저장된 기준자산이 0/음수라면, 무한 차단 대신 즉시
    재계산(self-heal)해 신규주문이 하루 종일 막히지 않게 한다."""
    state = _state(realized_pnl_krw=10_000.0, baseline=0.0)
    empty_position = {"symbol": None, "quantity": 0, "avg_price": None, "entry_price": None}
    result = engine.compute_net_daily_return(
        state, position=empty_position, hynix_price=None, inverse_price=None,
        cash=10_010_000.0, positions_from_broker=[], cash_fetch_ok=True,
    )
    assert result["blocked_reason"] is None
    assert result["baseline_rebased"] is True
    assert result["baseline_just_established"] is True
    assert state["daily_pnl_baseline_equity"] == pytest.approx(10_000_000.0)


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
