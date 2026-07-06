"""tests/test_real_broker_safety.py — 실계좌 안전한도 테스트"""
import os
import sys
from pathlib import Path
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)


# ── 픽스처: 최소 KISClient 모의 객체 ───────────────────────────────────────

class _FakeKIS:
    mode = "real"

    def get_buyable_cash(self, symbol="", price=0):
        return 5_000_000.0

    def get_buyable_cash_raw(self, symbol="", price=0, **kw):
        return {
            "ord_psbl_cash": 5_000_000.0,
            "nrcvb_buy_amt": 5_000_000.0,
            "psbl_qty": 10,
        }

    def get_balance(self):
        return {"cash": 5_000_000.0, "orderable_cash": 5_000_000.0, "positions": []}

    def buy(self, symbol, quantity, price, order_type="limit"):
        return {
            "success": True,
            "order_id": "TEST001",
            "message": "OK",
            "raw": {},
            "http_status": 200,
        }


def _make_broker(
    max_order=5_000_000,
    max_daily=30_000_000,
    max_symbol=10_000_000,
    auto_reduce=True,
):
    from app.config import Config
    cfg = Config()
    cfg._raw.setdefault("safety", {})
    cfg._raw["safety"]["max_order_amount"] = max_order
    cfg._raw["safety"]["max_daily_order_amount"] = max_daily
    cfg._raw["safety"]["max_position_amount_per_symbol"] = max_symbol
    cfg._raw["safety"]["enable_real_trading"] = True
    cfg._raw["safety"]["enable_real_buy"] = True
    cfg._raw["safety"]["require_real_order_confirm_text"] = False
    cfg._raw["safety"]["real_order_confirm_text"] = "REAL_ORDER_CONFIRMED"
    cfg._raw.setdefault("kis", {}).setdefault("real", {})["enabled"] = True

    os.environ["AUTO_REDUCE_QUANTITY_ON_SAFETY_LIMIT"] = "true" if auto_reduce else "false"

    from app.trading.kis_real_broker import KisRealBroker
    broker = KisRealBroker(
        kis_client=_FakeKIS(),
        cfg=cfg,
        confirm_text="REAL_ORDER_CONFIRMED",
        runtime_real_mode=True,
        runtime_enable_real_buy=True,
    )
    return broker


# ── Tests ───────────────────────────────────────────────────────────────────

class TestDefaultLimits:
    def test_default_per_order_is_5m(self):
        from app.config import Config
        cfg = Config()
        limits = cfg.get_real_order_limits()
        assert limits["per_order"] >= 5_000_000, (
            f"기본 1회 주문한도가 5M 미만: {limits['per_order']}"
        )

    def test_default_daily_is_30m(self):
        from app.config import Config
        cfg = Config()
        limits = cfg.get_real_order_limits()
        assert limits["daily"] >= 30_000_000, (
            f"기본 일일한도가 30M 미만: {limits['daily']}"
        )


class TestSafetyLimitPass:
    def test_skhynix_2901000_passes(self):
        broker = _make_broker(max_order=5_000_000)
        err, etype = broker._check_order_limits(1, 2_901_000, symbol="000660")
        assert err is None, f"SK하이닉스 차단됨: {err}"

    def test_lge_2970500_passes(self):
        broker = _make_broker(max_order=5_000_000)
        err, etype = broker._check_order_limits(13, 228_500, symbol="066570")
        assert err is None, f"LG전자 차단됨: {err}"

    def test_lgcns_1877400_passes(self):
        broker = _make_broker(max_order=5_000_000)
        err, etype = broker._check_order_limits(21, 89_400, symbol="064400")
        assert err is None, f"LG씨엔에스 차단됨: {err}"


class TestSafetyLimitBlock:
    def test_1m_limit_blocks_skhynix(self):
        broker = _make_broker(max_order=1_000_000)
        err, etype = broker._check_order_limits(1, 2_901_000, symbol="000660")
        assert err is not None
        assert etype == "safety_per_order_limit_exceeded"

    def test_daily_limit_blocks_cumulative(self):
        broker = _make_broker(max_order=5_000_000, max_daily=5_000_000)
        broker._daily_ordered_amount = 4_500_000
        err, etype = broker._check_order_limits(1, 2_901_000, symbol="000660")
        assert err is not None
        assert etype == "safety_daily_limit_exceeded"

    def test_symbol_limit_blocks_large_order(self):
        broker = _make_broker(max_order=5_000_000, max_symbol=2_000_000)
        err, etype = broker._check_order_limits(1, 2_901_000, symbol="000660")
        assert err is not None
        assert etype == "safety_symbol_limit_exceeded"


class TestAutoReduceQuantity:
    def test_auto_reduce_adjusts_quantity(self):
        broker = _make_broker(max_order=2_000_000, auto_reduce=True)
        new_qty, reason = broker._auto_reduce_quantity(13, 228_500, symbol="066570")
        assert new_qty >= 1, "자동조정 후 수량이 0이어서는 안됨"
        assert new_qty < 13, f"수량이 줄어야 함: {new_qty}"
        expected = int(2_000_000 * 0.98 / 228_500)
        assert new_qty == expected, f"예상수량 {expected}주 != 실제 {new_qty}주"

    def test_auto_reduce_skip_when_disabled(self):
        broker = _make_broker(max_order=1_000_000, auto_reduce=False)
        result = broker.buy("000660", "SK하이닉스", 1, 2_901_000)
        assert not result.success
        assert result.error_type == "safety_per_order_limit_exceeded"


class TestErrorTypeSeparation:
    def test_safety_limit_within_range_returns_none(self):
        broker = _make_broker(max_order=5_000_000)
        err, etype = broker._check_order_limits(1, 2_901_000)
        assert err is None
        assert etype is None


class TestBuyIntegration:
    def test_buy_skhynix_succeeds_with_default_limits(self):
        broker = _make_broker(max_order=5_000_000)
        result = broker.buy("000660", "SK하이닉스", 1, 2_901_000)
        assert result.success, f"매수 실패: {result.message}"

    def test_buy_lge_succeeds_with_default_limits(self):
        broker = _make_broker(max_order=5_000_000)
        result = broker.buy("066570", "LG전자", 13, 228_500)
        assert result.success, f"매수 실패: {result.message}"

    def test_buy_lgcns_succeeds_with_default_limits(self):
        broker = _make_broker(max_order=5_000_000)
        result = broker.buy("064400", "LG씨엔에스", 21, 89_400)
        assert result.success, f"매수 실패: {result.message}"
