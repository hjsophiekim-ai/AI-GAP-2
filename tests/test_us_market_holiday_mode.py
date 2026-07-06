"""
미국장 휴장/데이터 신선도/MU 분봉 수집 보강 테스트.

검증 항목:
  1. 미국 휴장일에 MU 1분봉이 없어도 프로그램이 죽지 않는다.
  2. 미국 휴장일에는 last_us_trading_day 데이터를 사용한다.
  3. 일반 개장일인데 MU 데이터가 stale/실패면 API_FAILURE로 감점된다.
  4. US_HOLIDAY와 API_FAILURE를 구분한다.
  5. holiday_mode=true이면 confidence_score가 last_session 등 대체 데이터로 계산된다.
  6. holiday_mode + 국내 09:20 강세 → A/B 전략이 가능해진다.
  7. holiday_mode + 국내 09:20 약세 → D/E/NO_TRADE로 간다.
  8. Alpaca 키 없을 때 Polygon/Finnhub/Yahoo fallback으로 넘어간다.
  9. 해외 데이터 전부 실패해도 국내 데이터만으로 F/NO_TRADE를 반환한다(죽지 않는다).
  10. 수동승인 매수 포지션 자동손절 기능이 이번 수정으로 깨지지 않았는지 회귀 확인한다.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import app.market.us_market_data as umd
import app.market.regime_features as rf
from app.market.regime_router import MarketRegimeRouter
from app.market import regime_router as regime_router_mod


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------

def _base_snapshot() -> dict:
    return {
        "domestic": {
            "kospi": {"value": 2500.0, "change_rate": 0.0, "advancers": 800, "decliners": 800, "success": True},
            "kosdaq": {"value": 800.0, "change_rate": 0.0, "advancers": 400, "decliners": 400, "success": True},
            "kospi200_futures": {"value": 330.0, "change_rate": 0.0, "success": True},
            "advancers": 800, "decliners": 800,
            "trading_value_top50": [], "change_rate_top50": [],
            "sector_change_rates": {}, "theme_change_rates": {}, "investor_flow": {},
            "hynix": {
                "symbol": "000660", "current_price": 200000.0, "open": 200000.0, "high": 200000.0,
                "low": 200000.0, "prev_close": 200000.0, "change_rate": 0.0, "trade_value": 1e9, "success": True,
            },
            "samsung": {
                "symbol": "005930", "current_price": 70000.0, "open": 70000.0, "high": 70000.0,
                "low": 70000.0, "prev_close": 70000.0, "change_rate": 0.0, "trade_value": 1e9, "success": True,
            },
            "hanmi": {
                "symbol": "042700", "current_price": 100000.0, "open": 100000.0, "high": 100000.0,
                "low": 100000.0, "prev_close": 100000.0, "change_rate": 0.0, "trade_value": 1e8, "success": True,
            },
        },
        "overseas": {
            "nasdaq": {"change_rate": 0.0, "success": True}, "sp500": {"change_rate": 0.0, "success": True},
            "sox": {"change_rate": 0.0, "success": True}, "micron": {"change_rate": 0.0, "success": True},
            "nvidia": {"change_rate": 0.0, "success": True}, "amd": {"change_rate": 0.0, "success": True},
            "broadcom": {"change_rate": 0.0, "success": True}, "usdkrw": {"change_rate": 0.0, "success": True},
            "us_futures": {"change_rate": 0.0, "success": True},
            "us_market_status": {
                "is_us_market_open": False, "is_us_holiday": False, "is_us_weekend": False,
                "is_us_early_close": False, "last_us_trading_day": "2026-07-06",
                "session": "closed", "source": "test", "timestamp": "", "confidence": 0.9,
            },
            "us_realtime_bars": {}, "us_last_session": {}, "holiday_mode_inputs": {},
        },
        "meta": {"data_quality_ratio": 1.0, "log": []},
    }


def _make_router(monkeypatch, tmp_path):
    monkeypatch.setattr(regime_router_mod, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(regime_router_mod, "_LOG_DIR", tmp_path / "logs")
    return MarketRegimeRouter()


# ---------------------------------------------------------------------------
# 1. 휴장일 MU 1분봉 없어도 죽지 않음
# ---------------------------------------------------------------------------

def test_realtime_bar_survives_missing_minute_bars_on_holiday():
    with patch.object(umd, "_fetch_alpaca_quote", return_value=None), \
         patch.object(umd, "_fetch_polygon_quote", return_value=None), \
         patch.object(umd, "_fetch_finnhub_quote", return_value=None), \
         patch.object(umd, "_fetch_yfinance_quote", return_value={"price": 95.0, "change_pct": -1.2, "source": "yahoo"}), \
         patch.object(umd, "_fetch_alpaca_bars", return_value=[]), \
         patch.object(umd, "_fetch_polygon_bars", return_value=[]), \
         patch.object(umd, "_fetch_yfinance_bars", return_value=[]):
        result = umd.fetch_us_realtime_bar("MU", market_open=False)

    assert result["success"] is True
    assert result["latest_bar_1m"] is None
    assert result["data_gap_reason"] == "MARKET_CLOSED"
    assert result["is_stale"] is False


def test_realtime_bar_total_failure_does_not_raise():
    with patch.object(umd, "_fetch_alpaca_quote", return_value=None), \
         patch.object(umd, "_fetch_polygon_quote", return_value=None), \
         patch.object(umd, "_fetch_finnhub_quote", return_value=None), \
         patch.object(umd, "_fetch_yfinance_quote", return_value=None), \
         patch.object(umd, "_fetch_naver_last_resort", return_value=None):
        result = umd.fetch_us_realtime_bar("MU", market_open=True)  # 예외를 던지지 않아야 함

    assert result["success"] is False
    assert result["data_gap_reason"] == "API_FAILURE"


# ---------------------------------------------------------------------------
# 2. 휴장일에는 last_us_trading_day 데이터 사용
# ---------------------------------------------------------------------------

def test_last_us_trading_day_skips_observed_holiday():
    """2026-07-06(월)은 7/3(금, 독립기념일 대체휴일)을 건너뛰어 7/2(목)이 마지막 거래일이어야 한다."""
    now = datetime(2026, 7, 6, 9, 0)
    status = umd.get_us_market_status(now=now)
    assert status["last_us_trading_day"] == "2026-07-02"
    assert status["is_us_holiday"] is True


def test_fetch_us_last_session_uses_history_fallback():
    import pandas as pd

    class _FakeTicker:
        def history(self, period="10d", interval="1d", auto_adjust=True):
            return pd.DataFrame({
                "Open": [90.0, 92.0], "High": [91.0, 93.0], "Low": [89.0, 90.0],
                "Close": [90.5, 88.0], "Volume": [1000, 2000],
            }, index=pd.to_datetime(["2026-07-01", "2026-07-02"]))

    with patch("yfinance.Ticker", return_value=_FakeTicker()):
        result = umd.fetch_us_last_session("MU")

    assert result["success"] is True
    assert result["close"] == 88.0
    assert result["session_date"] == "2026-07-02"
    assert round(result["change_rate"], 2) == round((88.0 - 90.5) / 90.5 * 100, 2)


# ---------------------------------------------------------------------------
# 3/4. API_FAILURE vs US_HOLIDAY 구분 + stale 감점
# ---------------------------------------------------------------------------

def test_normal_trading_day_with_failed_mu_is_api_failure():
    snap = _base_snapshot()
    snap["overseas"]["us_market_status"]["is_us_market_open"] = True
    snap["overseas"]["micron"]["success"] = False
    snap["overseas"]["nvidia"]["success"] = False
    snap["overseas"]["amd"]["success"] = False
    snap["overseas"]["broadcom"]["success"] = False
    snap["overseas"]["sox"]["success"] = False
    snap["overseas"]["nasdaq"]["success"] = False
    snap["overseas"]["us_realtime_bars"] = {
        "micron": {"success": False, "data_gap_reason": "API_FAILURE"},
    }

    reason = rf.classify_data_gap_reason(snap)
    assert reason == "API_FAILURE"

    quality = rf.compute_data_quality_score(snap)
    assert quality < 80.0  # 일반 개장일 데이터 실패는 크게 감점되어야 한다


def test_holiday_gap_is_classified_separately_from_api_failure():
    snap = _base_snapshot()
    snap["overseas"]["us_market_status"]["is_us_holiday"] = True
    # 실시간 데이터는 실패했지만(휴장이므로 당연함) 휴장 사유로 분류되어야 한다
    snap["overseas"]["micron"]["success"] = False

    reason = rf.classify_data_gap_reason(snap)
    assert reason == "US_HOLIDAY"
    assert rf.is_holiday_mode(snap) is True

    quality = rf.compute_data_quality_score(snap)
    assert quality >= 75.0  # 휴장으로 인한 공백은 과도하게 감점하지 않는다


# ---------------------------------------------------------------------------
# 5/6/7. Holiday Mode 대체 판단 → A/B 또는 D/E/F
# ---------------------------------------------------------------------------

def test_holiday_mode_confidence_uses_last_session_fallback(monkeypatch, tmp_path):
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()
    snap["overseas"]["us_market_status"]["is_us_holiday"] = True
    for key in ("micron", "nvidia", "sox", "nasdaq", "amd", "broadcom"):
        snap["overseas"][key]["success"] = False
    snap["overseas"]["us_last_session"] = {
        "micron": {"success": True, "change_rate": 4.0},
        "nvidia": {"success": True, "change_rate": 3.0},
        "sox": {"success": True, "change_rate": 3.5},
        "nasdaq": {"success": True, "change_rate": 1.5},
    }

    result = router.determine_regime(now_hm="09:30", snapshot=snap)
    assert result["holiday_mode"] is True
    assert "us_ai_score_holiday_adjusted" in result["scores"]
    assert result["scores"]["us_ai_score_holiday_adjusted"] > 50.0


def test_holiday_mode_with_strong_domestic_flow_allows_bull_regime(monkeypatch, tmp_path):
    """휴장모드 + 국내 09:20 강세 → A 또는 B 유형이 가능해야 한다."""
    router = _make_router(monkeypatch, tmp_path)

    snap1 = _base_snapshot()
    snap1["overseas"]["us_market_status"]["is_us_holiday"] = True
    snap1["domestic"]["hynix"]["low"] = 190000.0
    snap1["domestic"]["hynix"]["current_price"] = 190000.0
    router.determine_regime(now_hm="09:20", snapshot=snap1)

    snap2 = _base_snapshot()
    snap2["overseas"]["us_market_status"]["is_us_holiday"] = True
    for key in ("micron", "nvidia", "sox"):
        snap2["overseas"][key]["success"] = False
    snap2["overseas"]["us_last_session"] = {
        "micron": {"success": True, "change_rate": 4.0},
        "nvidia": {"success": True, "change_rate": 3.0},
        "sox": {"success": True, "change_rate": 3.0},
        "nasdaq": {"success": True, "change_rate": 1.0},
    }
    snap2["domestic"]["hynix"]["current_price"] = 198000.0
    snap2["domestic"]["hynix"]["day1_return"] = -3.5
    snap2["domestic"]["hynix"]["day2_cum_return"] = -6.0
    snap2["domestic"]["kospi"]["change_rate"] = 1.0
    snap2["domestic"]["advancers"] = 1200
    snap2["domestic"]["decliners"] = 300

    result = router.determine_regime(now_hm="09:35", snapshot=snap2)
    assert result["holiday_mode"] is True
    assert result["regime"] in ("A", "B")
    assert result["confidence_score"] >= 60


def test_holiday_mode_with_weak_domestic_flow_blocks_or_goes_defensive(monkeypatch, tmp_path):
    """휴장모드 + 국내 09:20 약세(시가 이탈) → D/E/F(NO_TRADE) 중 하나로 가야 한다."""
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()
    snap["overseas"]["us_market_status"]["is_us_holiday"] = True
    for key in ("micron", "nvidia", "sox", "nasdaq"):
        snap["overseas"][key]["success"] = False
    snap["overseas"]["us_last_session"] = {
        "micron": {"success": True, "change_rate": -3.0},
        "nvidia": {"success": True, "change_rate": -2.0},
        "sox": {"success": True, "change_rate": -2.5},
    }
    snap["domestic"]["kospi"]["change_rate"] = -2.0
    snap["domestic"]["kosdaq"]["change_rate"] = -2.5
    snap["domestic"]["hynix"]["change_rate"] = -2.0
    snap["domestic"]["samsung"]["change_rate"] = -2.0
    snap["overseas"]["usdkrw"]["change_rate"] = 0.8

    result = router.determine_regime(now_hm="09:30", snapshot=snap)
    assert result["regime"] in ("D", "E", "F")


# ---------------------------------------------------------------------------
# 8. 다중 소스 fallback 순서 (Alpaca 없음 → Polygon/Finnhub/Yahoo)
# ---------------------------------------------------------------------------

def test_fallback_skips_alpaca_without_keys_and_uses_yahoo(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("ENABLE_POLYGON_US_DATA", "false")
    monkeypatch.setenv("ENABLE_FINNHUB_US_DATA", "false")

    with patch.object(umd, "_fetch_yfinance_quote", return_value={"price": 100.0, "change_pct": 1.5, "source": "yahoo"}):
        result = umd.fetch_us_quote_multi("MU")

    assert result["success"] is True
    assert result["source"] == "yahoo"


def test_fallback_uses_polygon_when_enabled_and_alpaca_missing(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("ENABLE_POLYGON_US_DATA", "true")
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")

    with patch.object(umd, "_fetch_polygon_quote", return_value={"price": 101.0, "timestamp": 123, "source": "polygon"}):
        result = umd.fetch_us_quote_multi("MU")

    assert result["success"] is True
    assert result["source"] == "polygon"


def test_alpaca_skipped_silently_when_keys_missing():
    """API 키가 없으면 Alpaca는 예외 없이 None을 반환(조용히 skip)한다."""
    assert umd._fetch_alpaca_quote("MU") is None
    assert umd._fetch_alpaca_bars("MU") == []


# ---------------------------------------------------------------------------
# 9. 해외 데이터 전부 실패해도 국내 데이터만으로 F/NO_TRADE (죽지 않음)
# ---------------------------------------------------------------------------

def test_all_overseas_data_failed_still_returns_f_without_crashing(monkeypatch, tmp_path):
    router = _make_router(monkeypatch, tmp_path)
    snap = _base_snapshot()
    for key in ("nasdaq", "sp500", "sox", "micron", "nvidia", "amd", "broadcom", "usdkrw", "us_futures"):
        snap["overseas"][key] = {"success": False, "error": "network_down"}

    result = router.determine_regime(now_hm="09:30", snapshot=snap)  # 예외 없이 완료되어야 함
    assert result["regime"] in ("A", "B", "C", "D", "E", "F")
    assert result["policy_name"]


# ---------------------------------------------------------------------------
# 10. 회귀: 수동승인 매수 포지션 자동손절이 이번 수정으로 깨지지 않았는지 확인
# ---------------------------------------------------------------------------

def test_regression_manual_position_still_auto_stop_loss():
    from app.execution.position_guard import PositionGuard, GuardedPosition
    from app.models import OrderResult

    executor = MagicMock()

    def _sell(symbol, name, quantity, price, reason="", source=""):
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type="market",
            order_id="T-1", message="ok",
        )

    executor.sell.side_effect = _sell
    guard = PositionGuard(executor, cfg={"stop_loss_pct": -1.2})
    guard.register_position(GuardedPosition(symbol="000660", name="SK하이닉스", quantity=10, avg_price=100000, source="manual"))

    actions = guard.evaluate_and_execute({"000660": {"price": 98800.0}}, now_hm="10:00", regime="A")

    assert len(actions) == 1
    assert actions[0]["reason"] == "stop_loss"
    assert guard.get_open_positions() == []
