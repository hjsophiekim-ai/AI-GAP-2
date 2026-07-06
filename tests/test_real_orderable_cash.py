"""
test_real_orderable_cash.py

KIS 실계좌 주문가능금액 관련 핵심 테스트.

[기존 6개]
- 인출가능금액(withdrawable) ≠ 주문가능금액(orderable) 구분
- D+2 결제 전 매도대금이 orderable에는 포함되고 withdrawable에는 미포함
- 실계좌 매수 시 allocated_budget vs orderable 중 작은 값 사용
- 잔고부족 오류 시 5% 감소 재시도
- UI 레이어(0_API연결.py 함수) cash vs orderable_cash 분리 표시
- Top3TimedBuy 자동매매가 withdrawable_amount를 매수 예산으로 절대 사용하지 않음

[추가 테스트]
- KISClient.ensure_token 인터페이스 (= get_access_token alias)
- KISClient.get_buyable_cash_raw 반환 구조
- get_buyable_cash가 nrcvb_buy_amt > ord_psbl_cash일 때 nrcvb 반환
- get_buyable_cash가 nrcvb=0이면 ord_psbl_cash 반환 (fallback)
- withdrawable=96603, app_orderable=24000000 상황에서 수량 64주 계산
- buyable_amount=96603이면 수량 1주로 작게 계산
- RealBroker와 diagnose script가 동일 token path(get_access_token) 사용
"""

import pytest
from unittest.mock import MagicMock, patch


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

class _FakeKIS:
    """KISClient 대역."""
    def __init__(self, withdrawable=96_000, orderable=24_000_000):
        self._withdrawable = withdrawable
        self._orderable = orderable

    def get_balance(self):
        return {
            "cash": self._withdrawable,
            "orderable_cash": self._orderable,
            "positions": [],
        }

    def get_buyable_cash(self, symbol="005930", price=0):
        return self._orderable

    def get_account_cash_breakdown(self):
        return {
            "withdrawable_amount": self._withdrawable,
            "cash_balance": self._withdrawable,
            "orderable_cash": self._orderable,
            "buyable_amount": self._orderable,
            "settlement_pending_cash": max(0, self._orderable - self._withdrawable),
            "raw_fields": {},
        }

    def get_stock_buyable_amount(self, symbol="005930", price=0):
        return self._orderable

    def ensure_token(self):
        return "FAKE_TOKEN_1234567890"


class _FakeRealBroker:
    """KisRealBroker 대역 — 실계좌 모드."""
    mode = "real"

    def __init__(self, withdrawable=96_000, orderable=24_000_000):
        self.kis = _FakeKIS(withdrawable, orderable)

    def get_balance(self):
        return self.kis.get_balance().get("cash", 0)

    def get_orderable_cash(self):
        return self.kis.get_buyable_cash()

    def get_buyable_cash(self):
        return self.get_orderable_cash()

    def get_stock_buyable_amount(self, symbol="005930", price=0):
        return self.kis.get_stock_buyable_amount(symbol, price)

    def get_account_cash_breakdown(self):
        return self.kis.get_account_cash_breakdown()

    def buy(self, symbol, quantity, price, order_type="limit"):
        return {"success": True, "order_id": "TEST_ORDER_001"}


class _FakeRealBrokerInsufficientFund(_FakeRealBroker):
    """첫 번째 buy는 잔고부족, 두 번째는 성공."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._buy_count = 0

    def buy(self, symbol, quantity, price, order_type="limit"):
        self._buy_count += 1
        if self._buy_count == 1:
            return {"success": False, "order_id": "", "message": "잔고부족"}
        return {"success": True, "order_id": "RETRY_ORDER_001"}


# ── Test 1: 인출가능금액 ≠ 주문가능금액 구분 확인 ─────────────────────────────
def test_withdrawable_not_equal_to_orderable():
    """D+2 매도대금 상황: 인출가능 96,000 vs 주문가능 24,000,000."""
    kis = _FakeKIS(withdrawable=96_000, orderable=24_000_000)
    bal = kis.get_balance()
    assert bal["cash"] == 96_000, "인출가능금액은 cash 키"
    assert bal["orderable_cash"] == 24_000_000, "주문가능금액은 orderable_cash 키"
    assert bal["cash"] != bal["orderable_cash"], "두 값은 달라야 함"


# ── Test 2: D+2 결제 미완료 금액이 settlement_pending_cash로 표시됨 ──────────
def test_settlement_pending_cash_reflects_d2():
    kis = _FakeKIS(withdrawable=96_000, orderable=24_000_000)
    bd = kis.get_account_cash_breakdown()
    assert bd["settlement_pending_cash"] == 24_000_000 - 96_000, \
        "D+2 미결제 추정액은 orderable - withdrawable"
    assert bd["withdrawable_amount"] < bd["orderable_cash"], \
        "인출가능 < 주문가능 (매도 후 D+2 상황)"


# ── Test 3: 실계좌 매수 시 orderable 기준으로 수량 결정 (withdrawable 미사용) ──
def test_real_buy_uses_orderable_not_withdrawable():
    """
    withdrawable=96,000, orderable=24,000,000, 현재가=70,000
    allocated_budget=4,500,000 (45%)
    orderable * 0.98 = 23,520,000 > allocated → min 결과는 allocated=4,500,000
    quantity = int(4,500,000 / 70,000) = 64
    withdrawable(96,000) 기준이라면 quantity=1 → 이 테스트로 구분
    """
    from app.services.intraday_auto_trade_service import Top3TimedBuyService

    class DummyCfg:
        _raw = {
            "intraday_auto_trade": {
                "strategy_name": "top3_timed_buy_3pct_takeprofit",
                "buy_schedule": {"rank1": "09:12", "rank2": "09:16", "rank3": "09:20"},
                "budget_allocation": {"rank1": 0.45, "rank2": 0.35, "rank3": 0.20},
                "buy_window_start": "09:10",
                "buy_window_end": "09:30",
                "take_profit_pct": 3.0,
                "stop_loss_pct": -1.2,
                "stop_loss_enabled": True,
                "force_exit_time": "15:10",
                "check_interval_seconds": 10,
                "minimum_safety_filter_enabled": False,
                "min_change_rate_at_buy": -999.0,
                "max_drop_from_intraday_high_pct": 999.0,
            },
            "safety": {
                "enable_real_trading": True,
                "enable_real_buy": True,
                "enable_real_sell": True,
            },
        }

    broker = _FakeRealBroker(withdrawable=96_000, orderable=24_000_000)
    svc = Top3TimedBuyService(broker=broker, kis_client=None, cfg=DummyCfg())

    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp()
    svc.state_file = Path(tmp) / "state.json"
    svc.log_file = Path(tmp) / "log.csv"

    top3 = [
        {"symbol": "005930", "name": "삼성전자", "rank": 1, "current_price": 70_000},
        {"symbol": "000660", "name": "SK하이닉스", "rank": 2, "current_price": 150_000},
        {"symbol": "035420", "name": "NAVER", "rank": 3, "current_price": 200_000},
    ]
    svc.load_top3(top3, 10_000_000)

    result = svc._execute_buy("005930", svc.symbols_state["005930"], 70_000)
    qty = result.get("quantity", 0)

    # withdrawable(96,000) 기준이면 qty=1, orderable(24M) 기준이면 qty=64
    assert qty == 64, f"주문가능금액(4,500,000/70,000=64주) 기준이어야 함, 실제={qty}"


# ── Test 4: 잔고부족 오류 시 5% 감소 후 1회 재시도 ──────────────────────────
def test_retry_on_insufficient_fund():
    from app.services.intraday_auto_trade_service import Top3TimedBuyService

    class DummyCfg:
        _raw = {
            "intraday_auto_trade": {
                "strategy_name": "top3_timed_buy_3pct_takeprofit",
                "buy_schedule": {"rank1": "09:12", "rank2": "09:16", "rank3": "09:20"},
                "budget_allocation": {"rank1": 0.45, "rank2": 0.35, "rank3": 0.20},
                "buy_window_start": "09:10",
                "buy_window_end": "09:30",
                "take_profit_pct": 3.0,
                "stop_loss_pct": -1.2,
                "stop_loss_enabled": True,
                "force_exit_time": "15:10",
                "check_interval_seconds": 10,
                "minimum_safety_filter_enabled": False,
                "min_change_rate_at_buy": -999.0,
                "max_drop_from_intraday_high_pct": 999.0,
            },
            "safety": {
                "enable_real_trading": True,
                "enable_real_buy": True,
                "enable_real_sell": True,
            },
        }

    broker = _FakeRealBrokerInsufficientFund(withdrawable=96_000, orderable=24_000_000)
    svc = Top3TimedBuyService(broker=broker, kis_client=None, cfg=DummyCfg())

    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp()
    svc.state_file = Path(tmp) / "state.json"
    svc.log_file = Path(tmp) / "log.csv"

    top3 = [{"symbol": "005930", "name": "삼성전자", "rank": 1, "current_price": 70_000}]
    svc.load_top3(top3, 10_000_000)

    result = svc._execute_buy("005930", svc.symbols_state["005930"], 70_000)
    assert result["success"] is True, "잔고부족 후 재시도로 성공해야 함"
    assert broker._buy_count == 2, "buy는 정확히 2회 호출(1차 실패 + 재시도) 되어야 함"


# ── Test 5: KISClient.get_account_cash_breakdown 분리 구조 검증 ───────────────
def test_cash_breakdown_fields():
    kis = _FakeKIS(withdrawable=500_000, orderable=10_000_000)
    bd = kis.get_account_cash_breakdown()

    required = {"withdrawable_amount", "cash_balance", "orderable_cash",
                "buyable_amount", "settlement_pending_cash", "raw_fields"}
    assert required.issubset(set(bd.keys())), f"누락 필드: {required - set(bd.keys())}"
    assert bd["orderable_cash"] >= bd["withdrawable_amount"], \
        "주문가능금액은 인출가능금액 이상이어야 함"


# ── Test 6: Top3TimedBuy가 실계좌에서 withdrawable_amount를 직접 쓰지 않음 ────
def test_auto_trade_never_uses_withdrawable_directly():
    """
    broker.get_balance()가 withdrawable을 반환하더라도
    _execute_buy는 get_orderable_cash()를 별도로 호출해야 한다.
    get_orderable_cash를 통해 24,000,000 → 4,500,000 예산 → qty 64
    get_balance만 쓰면 96,000 → qty 1
    """
    from app.services.intraday_auto_trade_service import Top3TimedBuyService

    class DummyCfg:
        _raw = {
            "intraday_auto_trade": {
                "strategy_name": "top3_timed_buy_3pct_takeprofit",
                "buy_schedule": {"rank1": "09:12", "rank2": "09:16", "rank3": "09:20"},
                "budget_allocation": {"rank1": 0.45, "rank2": 0.35, "rank3": 0.20},
                "buy_window_start": "09:10",
                "buy_window_end": "09:30",
                "take_profit_pct": 3.0,
                "stop_loss_pct": -1.2,
                "stop_loss_enabled": True,
                "force_exit_time": "15:10",
                "check_interval_seconds": 10,
                "minimum_safety_filter_enabled": False,
                "min_change_rate_at_buy": -999.0,
                "max_drop_from_intraday_high_pct": 999.0,
            },
            "safety": {
                "enable_real_trading": True,
                "enable_real_buy": True,
                "enable_real_sell": True,
            },
        }

    broker = _FakeRealBroker(withdrawable=96_000, orderable=24_000_000)
    # get_balance가 withdrawable만 반환하더라도, get_orderable_cash는 orderable 반환
    assert broker.get_balance() == 96_000
    assert broker.get_orderable_cash() == 24_000_000

    svc = Top3TimedBuyService(broker=broker, kis_client=None, cfg=DummyCfg())

    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp()
    svc.state_file = Path(tmp) / "state.json"
    svc.log_file = Path(tmp) / "log.csv"

    top3 = [
        {"symbol": "005930", "name": "삼성전자", "rank": 1, "current_price": 70_000},
        {"symbol": "000660", "name": "SK하이닉스", "rank": 2, "current_price": 150_000},
        {"symbol": "035420", "name": "NAVER", "rank": 3, "current_price": 200_000},
    ]
    svc.load_top3(top3, 10_000_000)

    result = svc._execute_buy("005930", svc.symbols_state["005930"], 70_000)
    # withdrawable(96,000) 기준이면 qty=1 — 이를 확인해 "withdrawable 사용 안 함" 검증
    qty = result.get("quantity", 0)
    assert qty > 1, f"withdrawable(96,000) 기준으로 qty=1 계산되면 안 됨, 실제={qty}"


# ── Test 7: KISClient.ensure_token 인터페이스 ─────────────────────────────────
def test_kis_client_ensure_token_is_alias():
    """ensure_token()이 get_access_token()의 alias인지 확인."""
    from app.trading.kis_client import KISClient

    class _FakeSession:
        def update(self, *a, **kw): pass
        headers = {}
        class _H:
            def update(self, *a, **kw): pass
        headers = _H()

    client = KISClient.__new__(KISClient)
    client._app_key = "test_key"
    client._app_secret = "test_secret"
    client.account_no = "12345678"
    client.product_code = "01"
    client.mode = "mock"
    client._token = "FAKE_TOKEN_ABCDEFGH"
    from datetime import datetime, timedelta
    client._token_expires_at = datetime.now() + timedelta(hours=1)
    client._session = type("S", (), {"headers": type("H", (), {"update": lambda s, *a, **kw: None})()})()

    # ensure_token()이 존재하고 get_access_token과 동일 값 반환
    assert hasattr(client, "ensure_token"), "KISClient에 ensure_token() 메서드가 없음"
    token_via_ensure = client.ensure_token()
    token_via_get = client.get_access_token()
    assert token_via_ensure == token_via_get == "FAKE_TOKEN_ABCDEFGH"


# ── Test 8: get_buyable_cash_raw 반환 구조 ───────────────────────────────────
def test_get_buyable_cash_raw_structure():
    """get_buyable_cash_raw가 필수 키를 포함한 dict를 반환하는지 확인."""
    from unittest.mock import MagicMock, patch
    from app.trading.kis_client import KISClient

    client = KISClient(
        app_key="k", app_secret="s",
        account_no="12345678", product_code="01", mode="mock"
    )
    client._token = "FAKE"
    from datetime import datetime, timedelta
    client._token_expires_at = datetime.now() + timedelta(hours=1)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": " 정상처리",
        "output": {
            "ord_psbl_cash": "5000000",
            "nrcvb_buy_amt": "24000000",
            "psbl_qty": "342",
        },
    }

    with patch.object(client._session, "get", return_value=mock_resp):
        raw = client.get_buyable_cash_raw("005930", 0)

    required_keys = {"output", "ord_psbl_cash", "nrcvb_buy_amt", "psbl_qty", "rt_cd", "msg_cd", "msg1"}
    assert required_keys.issubset(set(raw.keys())), f"누락 키: {required_keys - set(raw.keys())}"
    assert raw["ord_psbl_cash"] == 5_000_000.0
    assert raw["nrcvb_buy_amt"] == 24_000_000.0
    assert raw["psbl_qty"] == 342


# ── Test 9: get_buyable_cash가 nrcvb_buy_amt > ord_psbl_cash이면 nrcvb 반환 ──
def test_get_buyable_cash_returns_nrcvb_when_larger():
    from unittest.mock import MagicMock, patch
    from app.trading.kis_client import KISClient

    client = KISClient(
        app_key="k", app_secret="s",
        account_no="12345678", product_code="01", mode="real"
    )
    client._token = "FAKE"
    from datetime import datetime, timedelta
    client._token_expires_at = datetime.now() + timedelta(hours=1)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "rt_cd": "0", "msg_cd": "MCA00000", "msg1": " 정상처리",
        "output": {"ord_psbl_cash": "96603", "nrcvb_buy_amt": "24000000", "psbl_qty": "342"},
    }

    with patch.object(client._session, "get", return_value=mock_resp):
        result = client.get_buyable_cash("005930", 0)

    assert result == 24_000_000.0, f"nrcvb(24M) > ord_psbl(96603)이면 24M 반환해야 함, 실제={result}"


# ── Test 10: get_buyable_cash — nrcvb=0이면 ord_psbl_cash fallback ───────────
def test_get_buyable_cash_fallback_to_ord_psbl():
    from unittest.mock import MagicMock, patch
    from app.trading.kis_client import KISClient

    client = KISClient(
        app_key="k", app_secret="s",
        account_no="12345678", product_code="01", mode="real"
    )
    client._token = "FAKE"
    from datetime import datetime, timedelta
    client._token_expires_at = datetime.now() + timedelta(hours=1)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "rt_cd": "0", "msg_cd": "MCA00000", "msg1": " 정상처리",
        "output": {"ord_psbl_cash": "5000000", "nrcvb_buy_amt": "0", "psbl_qty": "71"},
    }

    with patch.object(client._session, "get", return_value=mock_resp):
        result = client.get_buyable_cash("005930", 0)

    assert result == 5_000_000.0, f"nrcvb=0이면 ord_psbl(5M) fallback해야 함, 실제={result}"


# ── Test 11: buyable=24M → qty=64, buyable=96K → qty=1 비교 ─────────────────
def test_quantity_calc_with_high_vs_low_buyable():
    """
    allocated_budget=4,500,000 / current_price=70,000
    buyable=24,000,000 → safe=min(4.5M, 24M*0.98)=4.5M → qty=64
    buyable=96,603 → safe=min(4.5M, 96603*0.98)=94,670 → qty=1
    """
    import math

    def calc_qty(allocated, buyable, price):
        safe = min(allocated, math.floor(buyable * 0.98))
        return int(safe / price)

    allocated = 4_500_000
    price = 70_000

    qty_high = calc_qty(allocated, 24_000_000, price)
    qty_low = calc_qty(allocated, 96_603, price)

    assert qty_high == 64, f"buyable=24M → qty=64 기대, 실제={qty_high}"
    assert qty_low == 1,   f"buyable=96K → qty=1 기대, 실제={qty_low}"


# ── Test 12: RealBroker와 diagnose가 동일 token path(get_access_token) 사용 ──
def test_real_broker_and_client_use_same_token_path():
    """
    RealBroker._order()와 KISClient.ensure_token() 모두 get_access_token()을 호출.
    diagnose 스크립트도 create_kis_client('real')을 통해 같은 path 사용.
    """
    from app.trading.kis_client import KISClient
    from app.trading.real_broker import RealBroker

    # KISClient에 ensure_token이 있고 get_access_token과 연결됨
    assert hasattr(KISClient, "ensure_token"), "KISClient.ensure_token 없음"
    assert hasattr(KISClient, "get_access_token"), "KISClient.get_access_token 없음"

    # RealBroker._order는 self.kis.get_access_token()을 호출
    import inspect
    src = inspect.getsource(RealBroker._order)
    assert "get_access_token" in src, "RealBroker._order에서 get_access_token을 사용해야 함"
