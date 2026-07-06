"""
kis_market_collector.py

KIS API를 통한 개별 종목 현재가 조회 (하이닉스/삼성전자/한미반도체 등).
KIS 클라이언트가 없거나 조회 실패 시 success=False를 반환하며 예외를 던지지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_kis_client(mode: str = "mock"):
    """KISClient 인스턴스 생성. 실패 시 None."""
    try:
        from app.trading.kis_client import create_kis_client
        return create_kis_client(mode)
    except Exception as exc:
        logger.debug("[KisMarketCollector] KIS 클라이언트 생성 실패: %s", exc)
        return None


def fetch_stock_snapshot(symbol: str, name: str = "", kis_client=None) -> dict:
    """KIS 현재가 조회 스냅샷.

    Returns
    -------
    dict: symbol, name, current_price, open, high, low, prev_close,
          change_rate, volume, trade_value, source, timestamp, success, error
    """
    result = {
        "symbol": symbol, "name": name,
        "current_price": None, "open": None, "high": None, "low": None,
        "prev_close": None, "change_rate": None, "volume": None, "trade_value": None,
        "source": "kis", "timestamp": _now_iso(), "success": False, "error": None,
    }
    if kis_client is None:
        result["error"] = "no_kis_client"
        return result
    try:
        data = kis_client.get_current_price(symbol)
        if not data:
            result["error"] = "empty_response"
            return result
        result.update({
            "current_price": data.get("current_price"),
            "open": data.get("open"),
            "high": data.get("high"),
            "low": data.get("low"),
            "prev_close": data.get("prev_close"),
            "change_rate": data.get("change_rate"),
            "volume": data.get("volume"),
            "trade_value": data.get("trade_value"),
            "success": True,
        })
        return result
    except Exception as exc:
        logger.warning("[KisMarketCollector] %s 현재가 조회 실패: %s", symbol, exc)
        result["error"] = str(exc)
        return result


def fetch_recent_daily_returns(symbol: str, kis_client=None, days: int = 3) -> dict:
    """최근 N일 종가 기반 등락률 (전일/최근2일 누적하락률 판단용).

    Returns
    -------
    dict: closes(list[float] 최신순), day1_return, day2_cum_return,
          source, timestamp, success, error
    """
    result = {
        "closes": [], "day1_return": None, "day2_cum_return": None,
        "source": "kis", "timestamp": _now_iso(), "success": False, "error": None,
    }
    if kis_client is None:
        result["error"] = "no_kis_client"
        return result
    try:
        rows = kis_client.get_daily_prices(symbol, days=days + 1)
        if not rows or len(rows) < 2:
            result["error"] = "insufficient_data"
            return result
        closes = [r["close"] for r in rows]
        result["closes"] = closes
        day1_return = (closes[0] - closes[1]) / closes[1] * 100 if closes[1] else None
        result["day1_return"] = day1_return
        if len(closes) >= 3 and closes[2]:
            result["day2_cum_return"] = (closes[0] - closes[2]) / closes[2] * 100
        result["success"] = True
        return result
    except Exception as exc:
        logger.warning("[KisMarketCollector] %s 일별수익률 조회 실패: %s", symbol, exc)
        result["error"] = str(exc)
        return result
