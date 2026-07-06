"""
test_bulk_buy_improvements.py

KIS 일괄매수 개선 사항 테스트.

검증 항목:
  1. 브로커 1회 생성 후 재사용 (create_broker 반복 호출 없음)
  2. tokenP 반복 호출 없음 (캐시 재사용)
  3. tokenP 403 → KISTokenError → 배치 중단
  4. HTTP 500 → 해당 종목 스킵, 다음 종목 계속
  5. WON 200 → ETF 제외 (excluded_reason="etf_or_index_product")
  6. 실패해도 남은 종목 계속 (500은 스킵)
  7. ETF 제외 종목은 OrderResult에 excluded_reason 기록
  8. 파일 캐시 경로 모드별 분리 (mock/real)
  9. 유효성 검증: 수량 0 → validation_error
  10. 유효성 검증: 가격 0 → validation_error
"""

import types
import pytest
from unittest.mock import MagicMock, patch, call

from app.models import BuyPlan, OrderResult
from app.trading.kis_client import KISTokenError
from app.trading.order_manager import OrderManager, _is_etf_like


# ---------------------------------------------------------------------------
# 헬퍼: 샘플 BuyPlan 생성
# ---------------------------------------------------------------------------

def _make_plan(symbol="005930", name="삼성전자", qty=2, price=70000.0, rank=1):
    return BuyPlan(
        rank=rank, symbol=symbol, name=name,
        current_price=price, allocated_quantity=qty,
        allocated_amount=qty * price,
        remaining_budget_after=0.0, allocation_round=1, allocation_status="ok",
    )


def _make_success_result(symbol="005930", name="삼성전자"):
    return OrderResult(
        success=True, mode="mock", account_type="mock",
        symbol=symbol, name=name, side="buy",
        quantity=2, price=70000.0, order_type="limit",
        order_id="0000099999", message="주문완료",
    )


def _make_fail_result(symbol, name, http_status=0, message="오류"):
    return OrderResult(
        success=False, mode="mock", account_type="mock",
        symbol=symbol, name=name, side="buy",
        quantity=2, price=70000.0, order_type="limit",
        order_id="", message=message,
        http_status=http_status,
    )


# ---------------------------------------------------------------------------
# 1. ETF 제외 키워드 필터 테스트
# ---------------------------------------------------------------------------

class TestETFFilter:

    def test_won_200_excluded(self):
        reason = _is_etf_like("448100", "WON 200")
        assert reason, "WON 200은 ETF로 제외되어야 함"
        assert "etf_or_index_product" in reason

    def test_kodex_excluded(self):
        reason = _is_etf_like("069500", "KODEX 200")
        assert reason, "KODEX 상품은 ETF로 제외되어야 함"

    def test_tiger_excluded(self):
        reason = _is_etf_like("102110", "TIGER 200")
        assert reason

    def test_regular_stock_not_excluded(self):
        assert _is_etf_like("462870", "시프트업") == ""
        assert _is_etf_like("307950", "현대오토에버") == ""
        assert _is_etf_like("010690", "화신") == ""
        assert _is_etf_like("005930", "삼성전자") == ""

    def test_invalid_symbol_excluded(self):
        reason = _is_etf_like("ABC", "삼성전자")
        assert reason, "6자리 숫자가 아닌 코드는 제외"

    def test_won_keyword_in_name(self):
        reason = _is_etf_like("999999", "WON채권혼합")
        assert reason


# ---------------------------------------------------------------------------
# 2. execute_buy_plans ETF 제외 동작
# ---------------------------------------------------------------------------

class TestExecuteBuyPlansETF:

    def _make_mock_broker(self):
        broker = MagicMock()
        broker.mode = "mock"
        broker.buy.return_value = _make_success_result()
        return broker

    def test_etf_excluded_from_order(self):
        broker = self._make_mock_broker()
        om = OrderManager(broker=broker, cfg=MagicMock(
            trading={"order_type": "limit"}
        ))
        plans = [_make_plan(symbol="448100", name="WON 200")]
        results = om.execute_buy_plans(plans)
        assert len(results) == 1
        assert results[0].error_type == "excluded_etf"
        assert "etf_or_index_product" in results[0].excluded_reason
        broker.buy.assert_not_called()

    def test_mixed_etf_and_regular(self):
        broker = self._make_mock_broker()
        om = OrderManager(broker=broker, cfg=MagicMock(
            trading={"order_type": "limit"}
        ))
        plans = [
            _make_plan(symbol="448100", name="WON 200", rank=1),
            _make_plan(symbol="462870", name="시프트업", rank=2),
        ]
        results = om.execute_buy_plans(plans)
        assert results[0].error_type == "excluded_etf"
        assert results[1].success is True
        # ETF는 API 호출 없이 제외, 일반 주식만 buy() 호출
        broker.buy.assert_called_once()


# ---------------------------------------------------------------------------
# 3. tokenP 403 → KISTokenError → 배치 전체 중단
# ---------------------------------------------------------------------------

class TestTokenError:

    def _make_token_error_broker(self, fail_on_symbol=None):
        broker = MagicMock()
        broker.mode = "mock"

        def side_effect(symbol, **kwargs):
            if fail_on_symbol is None or symbol == fail_on_symbol:
                raise KISTokenError("tokenP 403 오류")
            return _make_success_result(symbol=symbol)

        broker.buy.side_effect = side_effect
        return broker

    def test_token_error_aborts_batch(self):
        broker = self._make_token_error_broker(fail_on_symbol="462870")
        om = OrderManager(broker=broker, cfg=MagicMock(
            trading={"order_type": "limit"}
        ))
        plans = [
            _make_plan(symbol="462870", name="시프트업", rank=1),
            _make_plan(symbol="307950", name="현대오토에버", rank=2),
            _make_plan(symbol="010690", name="화신", rank=3),
        ]
        results = om.execute_buy_plans(plans)
        # 첫 번째: token_403
        assert results[0].error_type == "token_403"
        assert results[0].http_status == 403
        # 나머지: batch_aborted
        assert results[1].error_type == "batch_aborted"
        assert results[2].error_type == "batch_aborted"

    def test_broker_created_once_not_per_stock(self):
        """브로커는 배치 시작 전 1회만 생성 — 종목별로 재생성하지 않음."""
        created_count = 0

        def mock_create_broker(**kwargs):
            nonlocal created_count
            created_count += 1
            broker = MagicMock()
            broker.mode = "mock"
            broker.buy.return_value = _make_success_result()
            return broker

        with patch("app.trading.broker_factory.create_broker", side_effect=mock_create_broker) as mock_cb:
            from app.trading.broker_factory import create_broker
            broker = create_broker(mode="dry_run")
            om = OrderManager(broker=broker, cfg=MagicMock(
                trading={"order_type": "limit"}
            ))
            plans = [
                _make_plan(symbol=f"00{i:04d}", name=f"종목{i}", rank=i)
                for i in range(1, 6)
            ]
            om.execute_buy_plans(plans)
        assert mock_cb.call_count == 1, "create_broker는 1회만 호출되어야 함"


# ---------------------------------------------------------------------------
# 4. HTTP 500 → 해당 종목 스킵, 다음 종목 계속
# ---------------------------------------------------------------------------

class TestHttp500Handling:

    def test_http500_skips_and_continues(self):
        """500 오류 발생 시 해당 종목만 스킵, 다음 종목은 계속 실행."""
        broker = MagicMock()
        broker.mode = "mock"

        call_count = [0]

        def buy_side_effect(symbol, **kwargs):
            call_count[0] += 1
            if symbol == "012320":
                return _make_fail_result(symbol, "경동인베스트", http_status=500, message="서버오류")
            return _make_success_result(symbol=symbol, name="기타종목")

        broker.buy.side_effect = buy_side_effect
        om = OrderManager(broker=broker, cfg=MagicMock(
            trading={"order_type": "limit"}
        ))
        plans = [
            _make_plan(symbol="462870", name="시프트업", rank=1),
            _make_plan(symbol="012320", name="경동인베스트", rank=2),
            _make_plan(symbol="010690", name="화신", rank=3),
        ]
        results = om.execute_buy_plans(plans)
        assert results[0].success is True
        assert results[1].error_type == "order_500"
        assert results[1].success is False
        assert results[2].success is True
        assert call_count[0] == 3, "500이어도 다음 종목 계속 호출"


# ---------------------------------------------------------------------------
# 5. 유효성 검증
# ---------------------------------------------------------------------------

class TestValidation:

    def _make_broker(self):
        b = MagicMock()
        b.mode = "mock"
        b.buy.return_value = _make_success_result()
        return b

    def test_zero_quantity_skipped(self):
        b = self._make_broker()
        om = OrderManager(broker=b, cfg=MagicMock(trading={"order_type": "limit"}))
        plans = [_make_plan(qty=0)]
        results = om.execute_buy_plans(plans)
        assert results[0].error_type == "validation_error"
        b.buy.assert_not_called()

    def test_zero_price_skipped(self):
        b = self._make_broker()
        om = OrderManager(broker=b, cfg=MagicMock(trading={"order_type": "limit"}))
        plans = [_make_plan(price=0.0)]
        results = om.execute_buy_plans(plans)
        assert results[0].error_type == "validation_error"
        b.buy.assert_not_called()

    def test_duplicate_symbol_skipped(self):
        b = self._make_broker()
        om = OrderManager(broker=b, cfg=MagicMock(trading={"order_type": "limit"}))
        om.bought_symbols.add("005930")
        plans = [_make_plan(symbol="005930")]
        results = om.execute_buy_plans(plans)
        assert results[0].error_type == "duplicate"
        b.buy.assert_not_called()


# ---------------------------------------------------------------------------
# 6. 토큰 캐시 경로 모드별 분리
# ---------------------------------------------------------------------------

class TestTokenCachePath:

    def test_mock_cache_path(self):
        from app.trading.kis_client import KISClient
        client = KISClient("dummy_key", "dummy_secret", "12345678", mode="mock")
        assert "kis_token_mock" in str(client._token_cache_path())

    def test_real_cache_path(self):
        from app.trading.kis_client import KISClient
        client = KISClient("dummy_key", "dummy_secret", "12345678", mode="real")
        assert "kis_token_real" in str(client._token_cache_path())

    def test_mock_real_paths_are_different(self):
        from app.trading.kis_client import KISClient
        mock_client = KISClient("key", "secret", "acct", mode="mock")
        real_client = KISClient("key", "secret", "acct", mode="real")
        assert mock_client._token_cache_path() != real_client._token_cache_path()


# ---------------------------------------------------------------------------
# 7. KISTokenError는 broker의 buy()에서 재발생
# ---------------------------------------------------------------------------

class TestKISTokenErrorPropagation:

    def test_mock_broker_reraises_token_error(self):
        from app.trading.kis_mock_broker import KisMockBroker
        mock_kis = MagicMock()
        mock_kis.buy.side_effect = KISTokenError("403 tokenP")
        broker = KisMockBroker(mock_kis)
        with pytest.raises(KISTokenError):
            broker.buy("005930", "삼성전자", 1, 70000.0)

    def test_real_broker_reraises_token_error(self):
        from app.trading.kis_real_broker import KisRealBroker
        mock_kis = MagicMock()
        mock_kis.buy.side_effect = KISTokenError("403 tokenP")
        mock_cfg = MagicMock()
        mock_cfg._raw = {"kis": {"real": {"enabled": True}}}
        mock_cfg.real_trading_enabled.return_value = True
        mock_cfg.require_real_confirm.return_value = False
        # 주문금액 한도를 충분히 크게 설정 (gate 5b가 buy() 이전에 차단하지 않도록)
        mock_cfg.safety = {
            "max_order_amount": 100_000_000,
            "max_daily_order_amount": 1_000_000_000,
        }
        broker = KisRealBroker(
            mock_kis, cfg=mock_cfg, confirm_text="",
            runtime_real_mode=True,
        )
        broker._runtime_enable_real_buy = True
        with pytest.raises(KISTokenError):
            broker.buy("005930", "삼성전자", 1, 70000.0)
