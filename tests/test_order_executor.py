"""
order_executor.py 테스트.

검증 항목:
  - 주문 실패 시 1차/2차 재시도 후 성공하면 성공 반환.
  - 재시도 모두 실패하면 최종 실패 + 로그 기록 (긴급 알림).
  - PAPER(mock)/REAL 모드 라벨링이 로그에 올바르게 기록된다.
"""

from unittest.mock import MagicMock

from app.execution.order_executor import OrderExecutor, MAX_RETRIES
from app.models import OrderResult


def _result(success, order_id="T-1"):
    return OrderResult(
        success=success, mode="mock", account_type="mock", symbol="000660", name="SK하이닉스",
        side="buy", quantity=1, price=200000.0, order_type="limit",
        order_id=order_id if success else "", message="ok" if success else "일시적 오류",
    )


def test_retry_succeeds_on_second_attempt():
    broker = MagicMock()
    broker.mode = "mock"
    broker.buy.side_effect = [_result(False), _result(True)]

    executor = OrderExecutor(broker, sleep_fn=lambda s: None)
    result = executor.buy("000660", "SK하이닉스", 1, 200000.0, reason="test", source="auto")

    assert result.success is True
    assert broker.buy.call_count == 2


def test_all_retries_fail_returns_failure_and_logs(tmp_path, monkeypatch):
    import app.execution.order_executor as oe_mod
    monkeypatch.setattr(oe_mod, "_TRADE_LOG_DIR", tmp_path)

    broker = MagicMock()
    broker.mode = "mock"
    broker.buy.return_value = _result(False)

    executor = OrderExecutor(broker, sleep_fn=lambda s: None)
    result = executor.buy("000660", "SK하이닉스", 1, 200000.0, reason="test", source="auto")

    assert result.success is False
    assert broker.buy.call_count == MAX_RETRIES + 1

    log_files = list(tmp_path.glob("*.csv"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8-sig")
    assert "final_failure" in content
    assert "True" in content


def test_paper_mode_label_in_log(tmp_path, monkeypatch):
    import app.execution.order_executor as oe_mod
    monkeypatch.setattr(oe_mod, "_TRADE_LOG_DIR", tmp_path)

    broker = MagicMock()
    broker.mode = "mock"
    broker.sell.return_value = _result(True)

    executor = OrderExecutor(broker, sleep_fn=lambda s: None)
    executor.sell("000660", "SK하이닉스", 1, 200000.0, reason="take_profit1", source="position_guard")

    content = list(tmp_path.glob("*.csv"))[0].read_text(encoding="utf-8-sig")
    assert "PAPER" in content


def test_real_mode_label_in_log(tmp_path, monkeypatch):
    import app.execution.order_executor as oe_mod
    monkeypatch.setattr(oe_mod, "_TRADE_LOG_DIR", tmp_path)

    broker = MagicMock()
    broker.mode = "real"
    broker.sell.return_value = _result(True)

    executor = OrderExecutor(broker, sleep_fn=lambda s: None)
    executor.sell("000660", "SK하이닉스", 1, 200000.0, reason="stop_loss", source="position_guard")

    content = list(tmp_path.glob("*.csv"))[0].read_text(encoding="utf-8-sig")
    assert "REAL" in content
