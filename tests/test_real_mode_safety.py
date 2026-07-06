"""
Tests for runtime real-mode safety gates.

검증 항목:
  1. 기본 config에서 실전 매수/매도 모두 차단
  2. runtime_real_mode=True이면 매수/매도 조건 통과
  3. enable_real_buy/sell 각각 독립 동작
  4. 확인 문구 틀리면 차단
  5. .env 키 없으면 실전모드 활성화 실패
  6. 매도 수량 > 보유수량이면 UI 레이어에서 차단 (브로커 레이어 검증)
  7. 주문금액 한도 초과 시 매수 차단
"""
import os
import pytest
from unittest.mock import MagicMock, patch

from app.trading.kis_real_broker import KisRealBroker
from app.models import OrderResult


# ---------------------------------------------------------------------------
# Config stubs
# ---------------------------------------------------------------------------

class _DefaultSafeCfg:
    """기본 안전 설정: 모든 실전 플래그 false."""
    _raw = {"kis": {"real": {"enabled": False}}}
    safety = {
        "enable_real_trading": False,
        "enable_real_buy": False,
        "enable_real_sell": False,
        "require_real_confirm": True,
        "real_confirm_text": "REAL_ORDER_CONFIRMED",
        "max_real_order_amount": 1_000_000,
        "max_real_daily_budget": 3_000_000,
    }

    def real_trading_enabled(self) -> bool:
        return False

    def real_buy_enabled(self) -> bool:
        return False

    def real_sell_enabled(self) -> bool:
        return False

    def require_real_confirm(self) -> bool:
        return True

    def real_confirm_text(self) -> str:
        return "REAL_ORDER_CONFIRMED"


class _BuyOnlyCfg(_DefaultSafeCfg):
    """enable_real_buy=True, enable_real_sell=False."""
    _raw = {"kis": {"real": {"enabled": True}}}
    safety = {
        **_DefaultSafeCfg.safety,
        "enable_real_trading": True,
        "enable_real_buy": True,
        "enable_real_sell": False,
    }

    def real_trading_enabled(self) -> bool:
        return True

    def real_buy_enabled(self) -> bool:
        return True

    def real_sell_enabled(self) -> bool:
        return False


class _SellOnlyCfg(_DefaultSafeCfg):
    """enable_real_buy=False, enable_real_sell=True."""
    _raw = {"kis": {"real": {"enabled": True}}}
    safety = {
        **_DefaultSafeCfg.safety,
        "enable_real_trading": True,
        "enable_real_buy": False,
        "enable_real_sell": True,
    }

    def real_trading_enabled(self) -> bool:
        return True

    def real_buy_enabled(self) -> bool:
        return False

    def real_sell_enabled(self) -> bool:
        return True


def _mock_kis():
    kis = MagicMock()
    kis.buy.return_value = {"success": True, "order_id": "T-001", "message": "ok", "raw": {}}
    kis.sell.return_value = {"success": True, "order_id": "T-002", "message": "ok", "raw": {}}
    kis.get_balance.return_value = {"cash": 5_000_000, "positions": []}
    return kis


# ---------------------------------------------------------------------------
# 1. 기본 config에서 실전 매수/매도 모두 차단
# ---------------------------------------------------------------------------

def test_default_config_blocks_real_buy():
    """기본 안전 설정(runtime_real_mode=False)에서 매수 차단."""
    # __init__ 자체가 kis.real.enabled=False → RuntimeError
    with pytest.raises(RuntimeError, match="비활성화"):
        KisRealBroker(
            _mock_kis(),
            cfg=_DefaultSafeCfg(),
            confirm_text="REAL_ORDER_CONFIRMED",
            runtime_real_mode=False,
        )


def test_default_config_blocks_real_sell():
    """기본 안전 설정에서 매도도 차단 (브로커 생성 자체가 차단)."""
    with pytest.raises(RuntimeError, match="비활성화"):
        KisRealBroker(
            _mock_kis(),
            cfg=_DefaultSafeCfg(),
            confirm_text="REAL_ORDER_CONFIRMED",
            runtime_real_mode=False,
        )


# ---------------------------------------------------------------------------
# 2. runtime_real_mode=True이면 gate2~3 우회 → 브로커 생성 + 주문 성공
# ---------------------------------------------------------------------------

def test_runtime_real_mode_allows_buy():
    """runtime_real_mode=True → gate2~3 우회, 매수 성공."""
    broker = KisRealBroker(
        _mock_kis(),
        cfg=_DefaultSafeCfg(),
        confirm_text="REAL_ORDER_CONFIRMED",
        runtime_real_mode=True,
    )
    result = broker.buy("005930", "삼성전자", quantity=1, price=70_000)
    assert result.success is True


def test_runtime_real_mode_allows_sell():
    """runtime_real_mode=True → 매도 성공."""
    broker = KisRealBroker(
        _mock_kis(),
        cfg=_DefaultSafeCfg(),
        confirm_text="REAL_ORDER_CONFIRMED",
        runtime_real_mode=True,
    )
    result = broker.sell("005930", "삼성전자", quantity=1, price=70_000)
    assert result.success is True


# ---------------------------------------------------------------------------
# 3. enable_real_buy / enable_real_sell 각각 독립 동작
# ---------------------------------------------------------------------------

def test_enable_real_buy_only_blocks_sell():
    """enable_real_buy=True, enable_real_sell=False → 매수는 OK, 매도는 차단."""
    broker = KisRealBroker(
        _mock_kis(),
        cfg=_BuyOnlyCfg(),
        confirm_text="REAL_ORDER_CONFIRMED",
        runtime_real_mode=False,
    )
    buy_result = broker.buy("005930", "삼성전자", quantity=1, price=70_000)
    sell_result = broker.sell("005930", "삼성전자", quantity=1, price=70_000)

    assert buy_result.success is True
    assert sell_result.success is False
    assert "실전모드" in sell_result.message


def test_enable_real_sell_only_blocks_buy():
    """enable_real_sell=True, enable_real_buy=False → 매도는 OK, 매수는 차단."""
    broker = KisRealBroker(
        _mock_kis(),
        cfg=_SellOnlyCfg(),
        confirm_text="REAL_ORDER_CONFIRMED",
        runtime_real_mode=False,
    )
    sell_result = broker.sell("005930", "삼성전자", quantity=1, price=70_000)
    buy_result = broker.buy("005930", "삼성전자", quantity=1, price=70_000)

    assert sell_result.success is True
    assert buy_result.success is False
    assert "실전모드" in buy_result.message


# ---------------------------------------------------------------------------
# 4. 확인 문구 틀리면 차단
# ---------------------------------------------------------------------------

def test_wrong_confirm_text_blocks_with_runtime_mode():
    """runtime_real_mode=True이어도 확인 문구 틀리면 gate4에서 차단."""
    with pytest.raises(RuntimeError, match="확인 문구"):
        KisRealBroker(
            _mock_kis(),
            cfg=_DefaultSafeCfg(),
            confirm_text="WRONG_TEXT",
            runtime_real_mode=True,
        )


def test_empty_confirm_text_blocks_with_runtime_mode():
    """빈 확인 문구도 차단."""
    with pytest.raises(RuntimeError, match="확인 문구"):
        KisRealBroker(
            _mock_kis(),
            cfg=_DefaultSafeCfg(),
            confirm_text="",
            runtime_real_mode=True,
        )


# ---------------------------------------------------------------------------
# 5. create_kis_client가 None 반환 시 RuntimeError
# ---------------------------------------------------------------------------

def test_missing_env_keys_fail_broker_creation():
    """create_kis_client가 None을 반환하면 broker_factory가 RuntimeError를 발생시킨다.

    실제로는 필수 환경변수 미설정 시 create_kis_client가 None을 반환하지만,
    .env 파일의 실제 값 유무와 무관하게 unit test에서 검증하기 위해 직접 모킹한다.
    """
    from app.trading.broker_factory import create_broker

    # broker_factory가 내부에서 lazy-import하므로 원본 모듈을 패치
    with patch("app.trading.kis_client.create_kis_client", return_value=None):
        with pytest.raises((RuntimeError, ValueError)):
            create_broker(
                cfg=None,
                mode="real",
                confirm_text="REAL_ORDER_CONFIRMED",
                runtime_real_mode=True,
            )


# ---------------------------------------------------------------------------
# 6. 매도 수량 > 보유 수량 → OrderManager 레이어 검증 (브로커 자체는 통과)
# ---------------------------------------------------------------------------

def test_broker_does_not_block_oversell_quantity():
    """브로커 자체는 보유수량 검증 안 함 (OrderManager 레이어에서 처리)."""
    broker = KisRealBroker(
        _mock_kis(),
        cfg=_BuyOnlyCfg(),   # sell_enabled이 True인 설정 사용 불가 → _SellOnlyCfg
        confirm_text="REAL_ORDER_CONFIRMED",
        runtime_real_mode=True,
    )
    # 브로커는 수량을 모름 → 그냥 통과
    result = broker.sell("005930", "삼성전자", quantity=9999, price=70_000)
    assert result.success is True  # 브로커 레이어는 통과


# ---------------------------------------------------------------------------
# 7. 주문금액 한도 초과 시 매수 차단
# ---------------------------------------------------------------------------

def test_order_amount_limit_blocks_buy():
    """주문금액 > max_order_amount → 매수 차단."""
    broker = KisRealBroker(
        _mock_kis(),
        cfg=_BuyOnlyCfg(),
        confirm_text="REAL_ORDER_CONFIRMED",
        runtime_real_mode=False,
    )
    # 2주 * 700,000 = 1,400,000 > max_real_order_amount(1,000,000)
    result = broker.buy("005930", "삼성전자", quantity=2, price=700_000)
    assert result.success is False
    assert "safety rule" in result.message


def test_daily_order_amount_limit_blocks_buy():
    """일일 누적 주문금액 초과 → 매수 차단."""
    broker = KisRealBroker(
        _mock_kis(),
        cfg=_BuyOnlyCfg(),
        confirm_text="REAL_ORDER_CONFIRMED",
        runtime_real_mode=False,
    )
    # 일일 누적이 max_real_daily_budget(3,000,000)에 근접한 상태
    broker._daily_ordered_amount = 2_900_000
    result = broker.buy("005930", "삼성전자", quantity=1, price=200_000)
    # 2,900,000 + 200,000 = 3,100,000 > 3,000,000
    assert result.success is False
    assert "일일 한도" in result.message
