"""
test_top3_timed_buy_service.py
전략: top3_timed_buy_3pct_takeprofit
14개 핵심 케이스 테스트
"""
import json
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch


# ── 더미 Broker ───────────────────────────────────────────────────────────────
class DummyBroker:
    mode = "mock"

    def __init__(self, buy_success=True, sell_success=True):
        self.buy_success = buy_success
        self.sell_success = sell_success
        self.buy_calls = []
        self.sell_calls = []

    def buy(self, symbol, quantity, price):
        self.buy_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        return {"success": self.buy_success, "order_id": "TEST001"}

    def sell(self, symbol, quantity, price):
        self.sell_calls.append({"symbol": symbol, "quantity": quantity, "price": price})
        return {"success": self.sell_success, "order_id": "TEST002"}


# ── 더미 Config ───────────────────────────────────────────────────────────────
class DummyConfig:
    def __init__(self):
        self._raw = {
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
                "minimum_safety_filter_enabled": True,
                "min_change_rate_at_buy": -1.0,
                "max_drop_from_intraday_high_pct": 5.0,
            },
            "safety": {
                "enable_real_trading": False,
                "enable_real_buy": False,
                "enable_real_sell": False,
            },
        }


_TOP3 = [
    {"symbol": "005930", "name": "삼성전자", "rank": 1, "current_price": 70000},
    {"symbol": "000660", "name": "SK하이닉스", "rank": 2, "current_price": 150000},
    {"symbol": "035420", "name": "NAVER", "rank": 3, "current_price": 200000},
]
_BUDGET = 10_000_000


def _make_svc(broker=None, kis_client=None, now_str="09:15:00"):
    """서비스 인스턴스 생성 + 상태파일 경로 임시 처리."""
    from app.services.intraday_auto_trade_service import Top3TimedBuyService
    cfg = DummyConfig()
    svc = Top3TimedBuyService(broker=broker or DummyBroker(), kis_client=kis_client, cfg=cfg)
    # 상태파일 임시 경로
    import tempfile, os
    from pathlib import Path
    tmp = tempfile.mkdtemp()
    svc.state_file = Path(tmp) / "state.json"
    svc.log_file = Path(tmp) / "log.csv"
    svc.state_file.parent.mkdir(parents=True, exist_ok=True)
    return svc


def _run_once_at(svc, time_str):
    """특정 시각에 run_once를 실행."""
    dt = datetime.now().replace(
        hour=int(time_str[:2]), minute=int(time_str[3:5]), second=0, microsecond=0
    )
    with patch("app.services.intraday_auto_trade_service.datetime") as mock_dt:
        mock_dt.now.return_value = dt
        mock_dt.fromisoformat = datetime.fromisoformat
        result = svc.run_once()
    return result


# ── Test 1: 09:11에는 rank1 매수 안 됨 ───────────────────────────────────────
def test_no_buy_before_scheduled_time():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    _run_once_at(svc, "09:11:00")
    assert len(broker.buy_calls) == 0, "09:11에는 rank1 매수가 없어야 함"


# ── Test 2: 09:12에는 rank1 매수됨 ───────────────────────────────────────────
def test_buy_rank1_at_scheduled_time():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    # kis_client 없으므로 현재가는 초기값(70000) 사용
    _run_once_at(svc, "09:12:00")
    assert any(c["symbol"] == "005930" for c in broker.buy_calls), "09:12에 rank1(005930) 매수 필요"


# ── Test 3: 09:16에는 rank2 매수됨 ───────────────────────────────────────────
def test_buy_rank2_at_scheduled_time():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    # rank1을 이미 매수한 상태로 만들기
    svc.symbols_state["005930"]["bought_today"] = True
    _run_once_at(svc, "09:16:00")
    assert any(c["symbol"] == "000660" for c in broker.buy_calls), "09:16에 rank2(000660) 매수 필요"


# ── Test 4: 09:20에는 rank3 매수됨 ───────────────────────────────────────────
def test_buy_rank3_at_scheduled_time():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    svc.symbols_state["005930"]["bought_today"] = True
    svc.symbols_state["000660"]["bought_today"] = True
    _run_once_at(svc, "09:20:00")
    assert any(c["symbol"] == "035420" for c in broker.buy_calls), "09:20에 rank3(035420) 매수 필요"


# ── Test 5: 09:30 이후 신규매수 안 됨 ────────────────────────────────────────
def test_no_buy_after_buy_window():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    _run_once_at(svc, "09:30:00")
    assert len(broker.buy_calls) == 0, "09:30 이후에는 매수 없어야 함"


# ── Test 6: Top3가 아닌 종목은 매수 안 함 ────────────────────────────────────
def test_only_top3_symbols_are_managed():
    svc = _make_svc()
    svc.load_top3(_TOP3, _BUDGET)
    assert "999999" not in svc.symbols_state, "Top3 외 종목이 상태에 포함되면 안 됨"


# ── Test 7: bought_today=True인 종목은 중복매수 안 함 ────────────────────────
def test_no_duplicate_buy_if_already_bought():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    svc.symbols_state["005930"]["bought_today"] = True
    svc.symbols_state["005930"]["avg_buy_price"] = 70000.0
    svc.symbols_state["005930"]["buy_quantity"] = 64
    svc.symbols_state["005930"]["status"] = "HOLDING"
    _run_once_at(svc, "09:13:00")
    buys_for_005930 = [c for c in broker.buy_calls if c["symbol"] == "005930"]
    assert len(buys_for_005930) == 0, "이미 매수한 종목은 중복매수 금지"


# ── Test 8: 수익률 +3.0%에서 전량매도 ────────────────────────────────────────
def test_take_profit_at_3pct():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    state = svc.symbols_state["005930"]
    state["bought_today"] = True
    state["avg_buy_price"] = 70000.0
    state["buy_quantity"] = 64
    state["current_price"] = 72100.0  # +3.0%
    state["profit_rate"] = 3.0
    state["status"] = "HOLDING"
    _run_once_at(svc, "10:00:00")
    assert any(c["symbol"] == "005930" for c in broker.sell_calls), "+3% 도달 시 매도 필요"
    assert svc.symbols_state["005930"]["sold_today"], "매도 후 sold_today=True 필요"


# ── Test 9: 수익률 -1.2%에서 손절 ────────────────────────────────────────────
def test_stop_loss_at_minus_1_2pct():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    state = svc.symbols_state["005930"]
    state["bought_today"] = True
    state["avg_buy_price"] = 70000.0
    state["buy_quantity"] = 64
    state["current_price"] = 69160.0  # -1.2%
    state["profit_rate"] = -1.2
    state["status"] = "HOLDING"
    _run_once_at(svc, "11:00:00")
    assert any(c["symbol"] == "005930" for c in broker.sell_calls), "-1.2% 손절 필요"


# ── Test 10: stop_loss_enabled=False이면 -1.2%에서 손절 안 함 ────────────────
def test_stop_loss_disabled():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.stop_loss_enabled = False
    svc.load_top3(_TOP3, _BUDGET)
    state = svc.symbols_state["005930"]
    state["bought_today"] = True
    state["avg_buy_price"] = 70000.0
    state["buy_quantity"] = 64
    state["current_price"] = 69160.0
    state["profit_rate"] = -1.2
    state["status"] = "HOLDING"
    _run_once_at(svc, "11:00:00")
    assert len(broker.sell_calls) == 0, "stop_loss_enabled=False면 손절 없어야 함"


# ── Test 11: 15:10 이후 강제청산 ─────────────────────────────────────────────
def test_force_exit_after_1510():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    state = svc.symbols_state["005930"]
    state["bought_today"] = True
    state["avg_buy_price"] = 70000.0
    state["buy_quantity"] = 64
    state["current_price"] = 70000.0
    state["profit_rate"] = 0.0
    state["status"] = "HOLDING"
    _run_once_at(svc, "15:10:00")
    assert any(c["symbol"] == "005930" for c in broker.sell_calls), "15:10 이후 강제청산 필요"


# ── Test 12: sold_today=True인 종목은 중복매도 안 함 ─────────────────────────
def test_no_duplicate_sell_if_already_sold():
    broker = DummyBroker()
    svc = _make_svc(broker)
    svc.load_top3(_TOP3, _BUDGET)
    state = svc.symbols_state["005930"]
    state["bought_today"] = True
    state["sold_today"] = True
    state["profit_rate"] = 5.0
    state["buy_quantity"] = 64
    state["avg_buy_price"] = 70000.0
    state["current_price"] = 73500.0
    state["status"] = "SOLD"
    _run_once_at(svc, "10:30:00")
    assert len(broker.sell_calls) == 0, "이미 매도한 종목은 중복매도 금지"


# ── Test 13: 1분봉 데이터 없어도 시간분산 매수 가능 ──────────────────────────
def test_buy_without_candle_data():
    broker = DummyBroker()
    svc = _make_svc(broker, kis_client=None)  # kis_client=None → 1분봉 없음
    svc.load_top3(_TOP3, _BUDGET)
    _run_once_at(svc, "09:12:00")
    assert any(c["symbol"] == "005930" for c in broker.buy_calls), \
        "1분봉 데이터 없어도 시간분산 매수 가능해야 함"


# ── Test 14: 현재가 조회 실패 시 해당 run_once만 skip ─────────────────────────
def test_price_fetch_failure_skips_only_current_run():
    class FailingKisClient:
        def get_current_price(self, symbol):
            raise RuntimeError("API 오류")

    broker = DummyBroker()
    svc = _make_svc(broker, kis_client=FailingKisClient())
    svc.load_top3(_TOP3, _BUDGET)
    # 현재가 초기값 > 0이면 기존 캐시 사용
    svc.symbols_state["005930"]["current_price"] = 70000.0
    # 예외 발생해도 run_once 자체는 예외를 올리지 않아야 함
    try:
        _run_once_at(svc, "09:12:00")
    except RuntimeError:
        pytest.fail("현재가 조회 실패 시 run_once가 중단되면 안 됨")
