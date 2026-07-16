"""
test_order_sent_blocking_reason_and_stale_alert.py — (2026-07-16 사용자 리포트)

1) BUY 신호가 떴는데 "[order_sent] 주문이 브로커로 전송되지 않음(가격 조회 실패/
   쿨다운/허용 시간대 아님 등)"이라는 뭉뚱그린 문구만 보여, 실제 원인(예: 매수
   가능금액 산정 0원)을 알 수 없었다 — 이제는 run_switch_or_entry()의 실제 실행
   결과를 execution_message/order_failure_code/broker_error라는 전용 필드에 담고,
   order_sent 단계의 blocking_reason은 그 전용 필드만 사용한다(entry_approved_reason
   은 절대 재사용하지 않는다 — "Entry Approved=YES"였던 승인 문구가 그대로 실패
   사유로 남는 사고를 재발 방지, 2026-07-16 후속 리포트).
2) _buy_new()가 "invalid price/amount"라는 문구로 가격 문제와 현금(매수가능금액)
   문제를 뭉뚱그리던 것을 분리했는지, 그리고 각 실패에 정확한 failure_code
   (PRICE_UNAVAILABLE/BUYABLE_CASH_ZERO/ORDER_QTY_ZERO)가 붙는지 검증한다.
3) POSITION_SYNC_PENDING이 과거 1회성 실패로 세팅된 뒤 브로커 동기화가 회복돼도
   critical_alert(🔴 CRITICAL 배너)가 영구히 남아있던 버그가 고쳐졌는지 검증한다.
"""
from __future__ import annotations

from datetime import datetime

from app.data_sources.hynix_long_collector import LONG_SYMBOL
from app.services.hynix_switch_state import default_state


# ---------------------------------------------------------------------------
# 0) 핵심 회귀 — Entry Approved=YES인데 브로커가 주문을 거부하면(action=BUY,
#    success=False), run_switch_or_entry()의 반환값에 진짜 실패사유(failure_code/
#    broker_error)가 담겨야 한다. 과거에는 이 값이 없어(또는 무시돼) blocking_reason
#    이 "EXPLORATORY 30% 진입 승인" 같은 approval 문구를 그대로 보여줬다.
# ---------------------------------------------------------------------------

def test_order_result_accepts_rt_cd_msg_cd_msg1_without_crashing():
    """회귀(2026-07-16 발견) — app/models.py(더 이상 존재하지 않는 사장 파일)와
    app/models/__init__.py(실제로 import되는 패키지)에 OrderResult가 중복 정의돼
    있었는데, 패키지 쪽엔 rt_cd/msg_cd/msg1 필드가 없어 KisMockBroker.buy()/sell()이
    그 필드를 넘기자마자 매번 TypeError로 죽었다(성공/실패 응답 모두, 심지어
    예외 처리 분기의 재구성 OrderResult()도 함께 죽어 예외가 그대로 전파됐다) —
    즉 모의투자 주문이 사실상 전부 실패했다. app/models.py를 삭제하고
    app/models/__init__.py에 그 필드를 추가해 단일 정의로 통합했다."""
    from app.models import OrderResult

    result = OrderResult(
        success=False, mode="mock", account_type="mock", symbol="0197X0", name="SOL 인버스",
        side="buy", quantity=10, price=9_000.0, order_type="limit", order_id="",
        message="주문가능금액을 확인해주세요", rt_cd="1", msg_cd="40240000",
        msg1="주문가능금액을 확인해주세요",
    )
    assert result.to_dict()["rt_cd"] == "1"
    assert result.to_dict()["msg_cd"] == "40240000"


def test_kis_mock_broker_buy_rejection_does_not_crash_on_order_result_construction():
    from app.trading.kis_mock_broker import KisMockBroker

    class _FakeKis:
        mode = "mock"

        def buy(self, symbol, qty, price, order_type):
            return {
                "success": False, "order_id": "", "message": "주문가능금액을 확인해주세요",
                "rt_cd": "1", "msg_cd": "40240000", "msg1": "주문가능금액을 확인해주세요",
            }

    broker = KisMockBroker(_FakeKis())
    result = broker.buy("0197X0", "SOL 인버스", 10, 9_000.0)

    assert result.success is False
    assert result.rt_cd == "1"
    assert result.msg_cd == "40240000"


def test_run_switch_or_entry_surfaces_broker_rejection_not_approval_text():
    from app.trading.hynix_switch_position_manager import run_switch_or_entry, ORDER_FAILURE_BROKER_REJECTED

    class _RejectingBroker:
        def get_buyable_cash(self):
            return 1_000_000.0

        def get_buyable_cash_status(self, symbol="005930", price=0):
            return {"value": 1_000_000.0, "ok": True, "status": "OK"}

        def buy(self, symbol, name, quantity, price, order_type="limit"):
            from app.models import OrderResult
            return OrderResult(
                success=False, mode="mock", account_type="mock", symbol=symbol, name=name,
                side="buy", quantity=quantity, price=price, order_type=order_type,
                order_id="", message="주문가능금액을 확인해주세요",
                rt_cd="1", msg_cd="40240000", msg1="주문가능금액을 확인해주세요",
            )

        def sell(self, *a, **k):
            raise AssertionError("이 테스트에서는 매도가 필요 없습니다.")

        def get_positions(self):
            return []

    state = default_state("mock")
    now = datetime(2026, 7, 16, 10, 0, 0)

    result = run_switch_or_entry(
        state, _RejectingBroker(), "INVERSE_BUY", hynix_price=100_000.0, inverse_price=9_000.0,
        now=now, reason="TrendSwitchAccel 즉시 진입: EXPLORATORY 30%",
    )

    assert result["stage"] == "order_sent"
    assert result["failure_code"] == ORDER_FAILURE_BROKER_REJECTED
    assert "40240000" in result["broker_error"]
    assert "주문가능금액을 확인해주세요" in result["broker_error"]
    # 승인 문구(reason)가 실패사유 필드에 섞여 나오면 안 된다.
    assert "EXPLORATORY 30%" not in (result.get("broker_error") or "")
    assert "EXPLORATORY 30%" not in (result.get("failure_code") or "")


# ---------------------------------------------------------------------------
# 1) blocking_reason이 order_sent 단계에서 전용 필드(execution_message/
#    order_failure_code/broker_error)만 사용하고 entry_approved_reason(진입 승인
#    문구)은 절대 재사용하지 않는다.
# ---------------------------------------------------------------------------

def test_build_blocking_reason_order_sent_uses_execution_message_not_entry_approval():
    from app.services.hynix_switch_engine import _build_blocking_reason, _blank_pipeline_trace

    trace = _blank_pipeline_trace()
    trace["stopped_stage"] = "order_sent"
    # Entry Approved=YES였던 승인 문구 — 이 필드는 order_sent 실패사유로 절대 쓰이면 안 된다.
    trace["entry_approved_reason"] = "TrendSwitchAccel 즉시 진입: EXPLORATORY 30% 진입 승인"
    trace["execution_message"] = "sized cash amount is 0 (buyable cash query returned 0/unavailable)"
    trace["order_failure_code"] = "BUYABLE_CASH_ZERO"

    reason = _build_blocking_reason(trace)

    assert "BUYABLE_CASH_ZERO" in reason
    assert "매수가능금액" in reason
    assert "EXPLORATORY 30% 진입 승인" not in reason


def test_build_blocking_reason_order_sent_shows_broker_error_when_rejected():
    from app.services.hynix_switch_engine import _build_blocking_reason, _blank_pipeline_trace

    trace = _blank_pipeline_trace()
    trace["stopped_stage"] = "order_sent"
    trace["entry_approved_reason"] = "가속 진입 확인: CONFIRMED 50%"
    trace["order_failure_code"] = "BROKER_REJECTED"
    trace["broker_error"] = "rt_cd=1, msg_cd=40240000, msg1=주문가능금액을 확인해주세요"

    reason = _build_blocking_reason(trace)

    assert "BROKER_REJECTED" in reason
    assert "주문가능금액을 확인해주세요" in reason
    assert "CONFIRMED 50%" not in reason


def test_build_blocking_reason_order_sent_falls_back_to_generic_when_empty():
    from app.services.hynix_switch_engine import _build_blocking_reason, _blank_pipeline_trace

    trace = _blank_pipeline_trace()
    trace["stopped_stage"] = "order_sent"
    trace["entry_approved_reason"] = "가속 진입 확인: EXPLORATORY 30% 진입 승인"

    reason = _build_blocking_reason(trace)

    assert "가격 조회 실패/쿨다운/허용 시간대 아님 등" in reason
    assert "EXPLORATORY 30% 진입 승인" not in reason


# ---------------------------------------------------------------------------
# 2) _buy_new: 가격 문제와 현금(매수가능금액) 문제를 구분
# ---------------------------------------------------------------------------

def test_buy_new_reports_price_failure_distinctly():
    from app.trading.hynix_switch_position_manager import _buy_new, ORDER_FAILURE_PRICE_UNAVAILABLE

    orders = []
    result = _buy_new(None, LONG_SYMBOL, current_price=None, cash_amount=1_000_000.0, reason="test", orders=orders)

    assert result["success"] is False
    assert "no valid current price" in result["message"]
    assert result["failure_code"] == ORDER_FAILURE_PRICE_UNAVAILABLE
    assert result["requested_qty"] == 0


def test_buy_new_reports_zero_cash_distinctly_from_price_failure():
    from app.trading.hynix_switch_position_manager import _buy_new, ORDER_FAILURE_BUYABLE_CASH_ZERO

    orders = []
    result = _buy_new(None, LONG_SYMBOL, current_price=50_000.0, cash_amount=0.0, reason="test", orders=orders)

    assert result["success"] is False
    assert "buyable cash query returned 0" in result["message"]
    assert "no valid current price" not in result["message"]
    assert result["failure_code"] == ORDER_FAILURE_BUYABLE_CASH_ZERO


def test_buy_new_reports_order_qty_zero_with_calc_inputs():
    """요구사항4 — 수량 0이면 계산식과 입력값(가격/투입현금/계산된 수량)을 그대로
    반환해 UI가 원인을 보여줄 수 있게 한다."""
    from app.trading.hynix_switch_position_manager import _buy_new, ORDER_FAILURE_ORDER_QTY_ZERO

    orders = []
    result = _buy_new(None, LONG_SYMBOL, current_price=100_000.0, cash_amount=50_000.0, reason="test", orders=orders)

    assert result["success"] is False
    assert result["failure_code"] == ORDER_FAILURE_ORDER_QTY_ZERO
    assert result["requested_qty"] == 0
    assert result["order_price"] == 100_000.0
    assert result["sized_cash"] == 50_000.0


def test_buy_new_calls_broker_buy_when_quantity_at_least_one():
    """요구사항4 — 수량 계산 결과가 1주 이상이면 실제 broker.buy()를 호출한다."""
    from app.trading.hynix_switch_position_manager import _buy_new

    calls = []

    class _Broker:
        def buy(self, symbol, name, quantity, price, order_type="limit"):
            calls.append((symbol, quantity, price))
            return {"success": True, "order_id": "ORD-1", "message": "ok", "rt_cd": "0"}

    orders = []
    result = _buy_new(_Broker(), LONG_SYMBOL, current_price=100_000.0, cash_amount=350_000.0, reason="test", orders=orders)

    assert len(calls) == 1
    assert calls[0][1] == 3  # 350,000 // 100,000 = 3주
    assert result["success"] is True
    assert result["requested_qty"] == 3


def test_buy_new_surfaces_broker_rejection_rt_cd_msg():
    """요구사항3/6 — 브로커가 실제로 주문을 거부하면 rt_cd/msg_cd/msg1이 broker_error에 남는다."""
    from app.trading.hynix_switch_position_manager import _buy_new, ORDER_FAILURE_BROKER_REJECTED

    class _RejectingBroker:
        def buy(self, symbol, name, quantity, price, order_type="limit"):
            return {
                "success": False, "order_id": "", "message": "주문가능금액을 확인해주세요",
                "rt_cd": "1", "msg_cd": "40240000", "msg1": "주문가능금액을 확인해주세요",
            }

    orders = []
    result = _buy_new(_RejectingBroker(), LONG_SYMBOL, current_price=100_000.0, cash_amount=350_000.0, reason="test", orders=orders)

    assert result["success"] is False
    assert result["failure_code"] == ORDER_FAILURE_BROKER_REJECTED
    assert "40240000" in result["broker_error"]
    assert "주문가능금액을 확인해주세요" in result["broker_error"]


def test_buy_new_wraps_broker_exception_as_execution_exception():
    from app.trading.hynix_switch_position_manager import _buy_new, ORDER_FAILURE_EXECUTION_EXCEPTION

    class _ExplodingBroker:
        def buy(self, symbol, name, quantity, price, order_type="limit"):
            raise RuntimeError("connection reset")

    orders = []
    result = _buy_new(_ExplodingBroker(), LONG_SYMBOL, current_price=100_000.0, cash_amount=350_000.0, reason="test", orders=orders)

    assert result["success"] is False
    assert result["failure_code"] == ORDER_FAILURE_EXECUTION_EXCEPTION
    assert "connection reset" in result["broker_error"]


# ---------------------------------------------------------------------------
# 3) POSITION_SYNC_PENDING stale critical_alert가 회복 후 지워진다
# ---------------------------------------------------------------------------

def test_apply_position_manager_to_state_clears_stale_pending_alert_on_recovery():
    from app.services.hynix_switch_state import default_state
    from app.trading.hynix_switch_position_manager import apply_position_manager_to_state

    state = default_state("mock")
    state["critical_alert"] = (
        "POSITION_SYNC_PENDING - broker position sync failed; keeping previous local position"
    )
    state["position_sync_status"] = None
    state["position_sync_error"] = None

    class _RecoveredPositionManager:
        last_sync_ok = True
        current_position = {"symbol": None, "quantity": 0, "conflict": False}
        broker = object()

    apply_position_manager_to_state(state, _RecoveredPositionManager())

    assert state["position_sync_status"] == "SYNCED"
    assert state["critical_alert"] is None


def test_apply_position_manager_to_state_includes_cause_in_new_pending_alert():
    from app.services.hynix_switch_state import default_state
    from app.trading.hynix_switch_position_manager import apply_position_manager_to_state

    state = default_state("mock")

    class _FailedPositionManager:
        last_sync_ok = False
        last_sync_error = "KIS 모의계좌 잔고 조회 실패: rt_cd=1: 초당 거래건수를 초과하였습니다"
        current_position = {"symbol": None, "quantity": 0}

    apply_position_manager_to_state(state, _FailedPositionManager())

    assert "POSITION_SYNC_PENDING" in state["critical_alert"]
    assert "초당 거래건수를 초과하였습니다" in state["critical_alert"]


def test_sanitize_position_sync_flags_clears_stale_critical_alert_when_synced():
    from app.services.hynix_switch_state import _sanitize_position_sync_flags

    state = {
        "position": {"symbol": None, "quantity": 0},
        "position_sync_status": "SYNCED",
        "position_sync_block_new_orders": False,
        "critical_alert": "POSITION_SYNC_PENDING - broker position sync failed; keeping previous local position",
    }
    _sanitize_position_sync_flags(state)
    assert state["critical_alert"] is None


def test_sanitize_position_sync_flags_clears_stale_critical_alert_on_optimistic_recovery():
    from app.services.hynix_switch_state import _sanitize_position_sync_flags

    state = {
        "position": {"symbol": None, "quantity": 0},
        "position_sync_status": None,
        "position_sync_block_new_orders": True,
        "residual_position_error": False,
        "critical_alert": "POSITION_SYNC_PENDING - broker balance confirmation failed after sell",
    }
    _sanitize_position_sync_flags(state)
    assert state["position_sync_block_new_orders"] is False
    assert state["critical_alert"] is None


def test_sanitize_position_sync_flags_preserves_unrelated_critical_alert():
    """critical_alert가 POSITION_SYNC_PENDING류가 아니면(예: 다른 원인) 건드리지 않는다."""
    from app.services.hynix_switch_state import _sanitize_position_sync_flags

    state = {
        "position": {"symbol": None, "quantity": 0},
        "position_sync_status": "SYNCED",
        "position_sync_block_new_orders": False,
        "critical_alert": "0193T0과 0197X0을 동시 보유 중 — 포지션 동기화 필요",
    }
    _sanitize_position_sync_flags(state)
    assert state["critical_alert"] == "0193T0과 0197X0을 동시 보유 중 — 포지션 동기화 필요"
