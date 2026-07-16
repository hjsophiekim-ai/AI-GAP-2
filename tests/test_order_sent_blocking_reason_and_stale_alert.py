"""
test_order_sent_blocking_reason_and_stale_alert.py — (2026-07-16 사용자 리포트)

1) BUY 신호가 떴는데 "[order_sent] 주문이 브로커로 전송되지 않음(가격 조회 실패/
   쿨다운/허용 시간대 아님 등)"이라는 뭉뚱그린 문구만 보여, 실제 원인(예: 매수
   가능금액 산정 0원)을 알 수 없었다 — entry_approved_reason(run_switch_or_entry가
   실제로 보고한 message)을 최우선으로 쓰도록 수정했는지 검증한다.
2) _buy_new()가 "invalid price/amount"라는 문구로 가격 문제와 현금(매수가능금액)
   문제를 뭉뚱그리던 것을 분리했는지 검증한다.
3) POSITION_SYNC_PENDING이 과거 1회성 실패로 세팅된 뒤 브로커 동기화가 회복돼도
   critical_alert(🔴 CRITICAL 배너)가 영구히 남아있던 버그가 고쳐졌는지 검증한다.
"""
from __future__ import annotations

from app.data_sources.hynix_long_collector import LONG_SYMBOL


# ---------------------------------------------------------------------------
# 1) blocking_reason이 order_sent 단계에서 구체적 사유를 우선한다
# ---------------------------------------------------------------------------

def test_build_blocking_reason_order_sent_prefers_specific_reason():
    from app.services.hynix_switch_engine import _build_blocking_reason, _blank_pipeline_trace

    trace = _blank_pipeline_trace()
    trace["stopped_stage"] = "order_sent"
    trace["entry_approved_reason"] = "sized cash amount is 0 (buyable cash query returned 0/unavailable)"

    reason = _build_blocking_reason(trace)

    assert "sized cash amount is 0" in reason
    assert "가격 조회 실패/쿨다운/허용 시간대 아님 등" not in reason


def test_build_blocking_reason_order_sent_falls_back_to_generic_when_empty():
    from app.services.hynix_switch_engine import _build_blocking_reason, _blank_pipeline_trace

    trace = _blank_pipeline_trace()
    trace["stopped_stage"] = "order_sent"
    trace["entry_approved_reason"] = ""

    reason = _build_blocking_reason(trace)

    assert "가격 조회 실패/쿨다운/허용 시간대 아님 등" in reason


# ---------------------------------------------------------------------------
# 2) _buy_new: 가격 문제와 현금(매수가능금액) 문제를 구분
# ---------------------------------------------------------------------------

def test_buy_new_reports_price_failure_distinctly():
    from app.trading.hynix_switch_position_manager import _buy_new

    orders = []
    result = _buy_new(None, LONG_SYMBOL, current_price=None, cash_amount=1_000_000.0, reason="test", orders=orders)

    assert result["success"] is False
    assert "no valid current price" in result["message"]


def test_buy_new_reports_zero_cash_distinctly_from_price_failure():
    from app.trading.hynix_switch_position_manager import _buy_new

    orders = []
    result = _buy_new(None, LONG_SYMBOL, current_price=50_000.0, cash_amount=0.0, reason="test", orders=orders)

    assert result["success"] is False
    assert "buyable cash query returned 0" in result["message"]
    assert "no valid current price" not in result["message"]


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
