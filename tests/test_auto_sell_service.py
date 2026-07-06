"""
tests/test_auto_sell_service.py

자동매도 서비스 단위 테스트.
실제 KIS API 호출 없이 모두 Mock으로 처리.
"""
import json
import math
import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.services.auto_sell_service import AutoSellService
from app.models import OrderResult


# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------

def _make_order_result(success: bool, order_id: str = "ORD001", message: str = "") -> OrderResult:
    return OrderResult(
        success=success, mode="real", account_type="real",
        symbol="005930", name="삼성전자", side="sell",
        quantity=10, price=0, order_type="market",
        order_id=order_id, message=message,
    )


def _make_broker(sell_success: bool = True) -> MagicMock:
    broker = MagicMock()
    broker.sell.return_value = _make_order_result(success=sell_success)
    return broker


def _make_kis(balance_positions=None, current_price=75000.0):
    kis = MagicMock()
    kis.get_balance.return_value = {
        "cash": 1_000_000.0,
        "positions": balance_positions or [
            {"symbol": "005930", "name": "삼성전자",
             "quantity": 10, "avg_price": 70000.0, "current_price": current_price},
        ],
    }
    kis.get_current_price.return_value = {"current_price": current_price}
    return kis


def _make_cfg(require_real=True) -> MagicMock:
    cfg = MagicMock()
    cfg._raw = {
        "auto_sell": {
            "first_take_profit_rate": 3.0,
            "first_take_profit_sell_ratio": 0.5,
            "final_take_profit_rate": 5.0,
            "final_take_profit_sell_ratio": 1.0,
            "order_type": "market",
            "market_start": "00:00",  # 항상 장 시간으로 설정
            "market_end": "23:59",
            "state_file": "data/state/test_auto_sell_state.json",
            "log_file": "data/logs/test_auto_sell_orders.csv",
            "require_real_mode": require_real,
        }
    }
    return cfg


def _make_service(
    sell_success: bool = True,
    current_price: float = 75000.0,
    balance_positions=None,
    tmp_path: Path = None,
) -> AutoSellService:
    kis = _make_kis(balance_positions=balance_positions, current_price=current_price)
    broker = _make_broker(sell_success=sell_success)
    cfg = _make_cfg()

    svc = AutoSellService.__new__(AutoSellService)
    svc._kis = kis
    svc._broker = broker
    svc._cfg = cfg
    svc._first_tp_rate = 3.0
    svc._first_tp_ratio = 0.5
    svc._final_tp_rate = 5.0
    svc._stop_loss_rate = -2.0
    svc._order_type = "market"
    svc._market_start = "00:00"
    svc._market_end = "23:59"
    svc.state = {}
    svc._last_run_time = None

    # state/log 파일을 tmp_path로 오버라이드
    if tmp_path:
        svc._state_file = tmp_path / "auto_sell_state.json"
        svc._log_file = tmp_path / "auto_sell_orders.csv"
    else:
        svc._state_file = Path("data/state/test_auto_sell_state.json")
        svc._log_file = Path("data/logs/test_auto_sell_orders.csv")

    return svc


# ---------------------------------------------------------------------------
# 1. 수익률 계산
# ---------------------------------------------------------------------------

def test_profit_rate_below_3pct():
    """수익률 2.9% — 절반매도 조건 false."""
    svc = _make_service()
    profit = svc.calculate_profit_rate(70000, 72030)  # 2.9%
    assert profit < 3.0
    pos = {"profit_rate": profit}
    state = {"half_sold": False, "all_sold": False}
    assert not svc.should_sell_half(pos, state)
    assert not svc.should_sell_all(pos, state)


def test_profit_rate_at_3pct():
    """수익률 정확히 3.0% — 절반매도 조건 true."""
    svc = _make_service()
    profit = svc.calculate_profit_rate(70000, 72100)  # 3.0%
    assert profit >= 3.0
    pos = {"profit_rate": profit}
    state = {"half_sold": False, "all_sold": False}
    assert svc.should_sell_half(pos, state)
    assert not svc.should_sell_all(pos, state)


def test_profit_rate_at_5pct():
    """수익률 정확히 5.0% — 전량매도 조건 true."""
    svc = _make_service()
    profit = svc.calculate_profit_rate(70000, 73500)  # 5.0%
    assert profit >= 5.0
    pos = {"profit_rate": profit}
    state = {"half_sold": False, "all_sold": False}
    assert svc.should_sell_all(pos, state)


# ---------------------------------------------------------------------------
# 2. 우선순위: +5% 도달 시 전량매도가 절반매도보다 우선
# ---------------------------------------------------------------------------

def test_full_sell_priority_over_half(tmp_path):
    """+5% 도달 시 전량매도 실행, 절반매도는 실행하지 않음."""
    svc = _make_service(current_price=73500.0, tmp_path=tmp_path)  # 5.0% 수익
    results = svc.run_once()

    # 전량매도 1건만 실행
    assert len(results) == 1
    assert results[0]["sell_type"] == "full"
    assert svc.state["005930"]["all_sold"] is True
    assert svc.state["005930"]["half_sold"] is False  # 절반매도는 별도 실행 안 됨


# ---------------------------------------------------------------------------
# 3. 중복매도 방지
# ---------------------------------------------------------------------------

def test_no_duplicate_half_sell(tmp_path):
    """half_sold=True 이면 +3% 조건에서 중복매도 하지 않음."""
    svc = _make_service(current_price=72100.0, tmp_path=tmp_path)  # 3.0%
    svc.state["005930"] = AutoSellService._new_state("삼성전자", 70000.0)
    svc.state["005930"]["half_sold"] = True  # 이미 절반매도 완료

    results = svc.run_once()
    assert len(results) == 0  # 추가 매도 없음


def test_no_duplicate_full_sell(tmp_path):
    """all_sold=True 이면 +5% 조건에서 중복매도 하지 않음."""
    svc = _make_service(current_price=73500.0, tmp_path=tmp_path)  # 5.0%
    svc.state["005930"] = AutoSellService._new_state("삼성전자", 70000.0)
    svc.state["005930"]["all_sold"] = True  # 이미 전량매도 완료

    results = svc.run_once()
    assert len(results) == 0


# ---------------------------------------------------------------------------
# 4. 수량 1주 절반매도 처리
# ---------------------------------------------------------------------------

def test_half_sell_quantity_1_share(tmp_path):
    """보유수량 1주에서 절반매도 수량 = floor(1*0.5) = 0 → 최소 1주 보정 적용."""
    svc = _make_service(
        current_price=72100.0,
        balance_positions=[
            {"symbol": "005930", "name": "삼성전자",
             "quantity": 1, "avg_price": 70000.0, "current_price": 72100.0},
        ],
        tmp_path=tmp_path,
    )
    pos = {"symbol": "005930", "name": "삼성전자", "quantity": 1,
           "avg_buy_price": 70000.0, "current_price": 72100.0, "profit_rate": 3.0}

    raw_qty = math.floor(1 * svc._first_tp_ratio)  # = 0
    sell_qty = max(1, raw_qty)  # 최소 1주 보정
    assert sell_qty == 1

    result = svc.execute_half_sell(pos, 72100.0)
    assert result["sell_quantity"] == 1
    assert result["order_result"] in ("SUCCESS", "FAIL")  # 주문 실행은 됨


# ---------------------------------------------------------------------------
# 5. 이미 절반매도 후 +5% 도달 시 남은 수량만 전량매도
# ---------------------------------------------------------------------------

def test_full_sell_after_half_sell(tmp_path):
    """절반매도 완료 후 +5% 도달 시 run_once()에서 전량매도 실행."""
    svc = _make_service(current_price=73500.0, tmp_path=tmp_path)  # 5.0%
    svc.state["005930"] = AutoSellService._new_state("삼성전자", 70000.0)
    svc.state["005930"]["half_sold"] = True  # 절반매도 이미 완료

    results = svc.run_once()
    assert len(results) == 1
    assert results[0]["sell_type"] == "full"
    assert svc.state["005930"]["all_sold"] is True


# ---------------------------------------------------------------------------
# 6. should_sell_all / should_sell_half 직접 테스트
# ---------------------------------------------------------------------------

def test_should_sell_half_exact_threshold():
    svc = _make_service()
    state = {"half_sold": False, "all_sold": False}
    assert svc.should_sell_half({"profit_rate": 3.0}, state)
    assert not svc.should_sell_half({"profit_rate": 2.99}, state)


def test_should_sell_all_exact_threshold():
    svc = _make_service()
    state = {"half_sold": False, "all_sold": False}
    assert svc.should_sell_all({"profit_rate": 5.0}, state)
    assert not svc.should_sell_all({"profit_rate": 4.99}, state)


# ---------------------------------------------------------------------------
# 7. 장외 시간 → run_once() 실행 안 됨
# ---------------------------------------------------------------------------

def test_run_once_outside_market_hours(tmp_path):
    """장외 시간이면 run_once()가 빈 결과 반환."""
    svc = _make_service(current_price=73500.0, tmp_path=tmp_path)
    svc._market_start = "09:00"
    svc._market_end = "09:01"  # 매우 좁은 장 시간 → 현재 시각에서는 장외

    now = datetime.now()
    if not (9 * 60 <= now.hour * 60 + now.minute <= 9 * 60 + 1):
        results = svc.run_once()
        assert results == []


# ---------------------------------------------------------------------------
# 8. 매도 주문 실패 → state에 오류 기록, 중복 주문 방지 해제
# ---------------------------------------------------------------------------

def test_sell_failure_records_error(tmp_path):
    """매도 실패 시 last_error 기록, pending_order=False로 복구."""
    svc = _make_service(sell_success=False, current_price=73500.0, tmp_path=tmp_path)
    svc._broker.sell.return_value = _make_order_result(success=False, message="주문 실패 테스트")

    pos = {"symbol": "005930", "name": "삼성전자", "quantity": 10,
           "avg_buy_price": 70000.0, "current_price": 73500.0, "profit_rate": 5.0}
    result = svc.execute_full_sell(pos, 73500.0)

    assert result["order_result"] == "FAIL"
    assert svc.state["005930"]["pending_order"] is False
    assert svc.state["005930"]["last_error"] == "주문 실패 테스트"
    assert svc.state["005930"]["all_sold"] is False  # 실패했으므로 all_sold 유지


# ---------------------------------------------------------------------------
# 9. state 저장/복원 정상 여부
# ---------------------------------------------------------------------------

def test_state_save_and_load(tmp_path):
    """state 저장 후 새 인스턴스에서 복원 시 동일 데이터."""
    svc = _make_service(tmp_path=tmp_path)
    svc.state["005930"] = AutoSellService._new_state("삼성전자", 70000.0)
    svc.state["005930"]["half_sold"] = True
    svc.state["005930"]["last_profit_rate"] = 3.5
    svc.save_state()

    # 새 인스턴스에서 로드
    svc2 = _make_service(tmp_path=tmp_path)
    svc2._state_file = svc._state_file
    svc2.load_state()

    assert "005930" in svc2.state
    assert svc2.state["005930"]["half_sold"] is True
    assert svc2.state["005930"]["last_profit_rate"] == 3.5


# ---------------------------------------------------------------------------
# 10. run_once: 보유수량 0, avg_buy_price 0 → 스킵
# ---------------------------------------------------------------------------

def test_run_once_skips_invalid_positions(tmp_path):
    """보유수량=0 또는 avg_buy_price=0인 종목은 스킵."""
    svc = _make_service(
        current_price=75000.0,
        balance_positions=[
            {"symbol": "000001", "name": "테스트1",
             "quantity": 0, "avg_price": 70000.0, "current_price": 75000.0},
            {"symbol": "000002", "name": "테스트2",
             "quantity": 5, "avg_price": 0.0, "current_price": 75000.0},
        ],
        tmp_path=tmp_path,
    )
    results = svc.run_once()
    assert results == []


# ---------------------------------------------------------------------------
# 11. pending_order=True → 중복 주문 방지
# ---------------------------------------------------------------------------

def test_pending_order_blocks_new_order(tmp_path):
    """pending_order=True인 종목은 새 주문 발행 안 됨."""
    svc = _make_service(current_price=73500.0, tmp_path=tmp_path)
    svc.state["005930"] = AutoSellService._new_state("삼성전자", 70000.0)
    svc.state["005930"]["pending_order"] = True

    results = svc.run_once()
    assert results == []
    svc._broker.sell.assert_not_called()


# ---------------------------------------------------------------------------
# 12. 손절 조건: -2% 이하 → should_stop_loss True
# ---------------------------------------------------------------------------

def test_should_stop_loss_triggers_at_threshold():
    """수익률이 -2.0% 이하이면 손절 조건 충족."""
    svc = _make_service()
    state = {"stop_loss_executed": False, "all_sold": False}
    assert svc.should_stop_loss({"profit_rate": -2.0}, state)
    assert svc.should_stop_loss({"profit_rate": -2.5}, state)
    assert not svc.should_stop_loss({"profit_rate": -1.99}, state)


def test_should_stop_loss_false_if_already_executed():
    """stop_loss_executed=True이면 손절 조건 false."""
    svc = _make_service()
    state = {"stop_loss_executed": True, "all_sold": False}
    assert not svc.should_stop_loss({"profit_rate": -3.0}, state)


def test_should_stop_loss_false_if_all_sold():
    """all_sold=True이면 손절 조건 false."""
    svc = _make_service()
    state = {"stop_loss_executed": False, "all_sold": True}
    assert not svc.should_stop_loss({"profit_rate": -3.0}, state)


# ---------------------------------------------------------------------------
# 13. execute_stop_loss: 성공 시 state 업데이트
# ---------------------------------------------------------------------------

def test_execute_stop_loss_success(tmp_path):
    """손절매도 성공 시 stop_loss_executed=True, all_sold=True."""
    svc = _make_service(current_price=68600.0, tmp_path=tmp_path)  # -2.0%
    pos = {"symbol": "005930", "name": "삼성전자", "quantity": 10,
           "avg_buy_price": 70000.0, "current_price": 68600.0, "profit_rate": -2.0}

    result = svc.execute_stop_loss(pos, 68600.0)

    assert result["sell_type"] == "stop_loss"
    assert result["order_result"] == "SUCCESS"
    assert result["sell_quantity"] == 10
    assert svc.state["005930"]["stop_loss_executed"] is True
    assert svc.state["005930"]["all_sold"] is True
    assert svc.state["005930"]["pending_order"] is False


# ---------------------------------------------------------------------------
# 14. run_once: 손절이 익절보다 우선
# ---------------------------------------------------------------------------

def test_stop_loss_priority_over_take_profit(tmp_path):
    """-2% 손절 조건 충족 시 익절 체크 없이 손절 실행."""
    # avg=70000, price=68600 → -2.0% (손절 조건 충족; 익절 조건 미충족)
    svc = _make_service(current_price=68600.0, tmp_path=tmp_path)
    results = svc.run_once()

    assert len(results) == 1
    assert results[0]["sell_type"] == "stop_loss"
    assert svc.state["005930"]["stop_loss_executed"] is True


# ---------------------------------------------------------------------------
# 15. 손절 후 중복 실행 방지
# ---------------------------------------------------------------------------

def test_no_duplicate_stop_loss(tmp_path):
    """stop_loss_executed=True이면 재실행 안 됨."""
    svc = _make_service(current_price=68600.0, tmp_path=tmp_path)
    svc.state["005930"] = AutoSellService._new_state("삼성전자", 70000.0)
    svc.state["005930"]["stop_loss_executed"] = True
    svc.state["005930"]["all_sold"] = True

    results = svc.run_once()
    assert results == []
    svc._broker.sell.assert_not_called()
