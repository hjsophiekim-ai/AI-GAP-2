"""
price_watcher.py

장중 현재가를 지속 조회한다. KIS API 우선, 실패 시 네이버증권 fallback,
그마저 실패하면 마지막 가격을 위험 플래그(stale=True)와 함께 반환한다.

절대 하지 않는 것:
  - 현재가 조회 실패로 프로그램 종료 (모든 예외를 흡수한다)
  - 오래된 가격을 신선한 것처럼 신규매수에 사용 (is_data_fresh로 차단)
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

DEFAULT_STALE_AFTER_SECONDS = 30
DEFAULT_FAILURE_BLOCK_THRESHOLD = 5


def _now() -> datetime:
    return datetime.now()


def fetch_price_once(symbol: str, kis_client=None, last_known: dict = None) -> dict:
    """단일 종목 현재가 1회 조회. 항상 dict를 반환하며 예외를 던지지 않는다.

    Returns
    -------
    dict: symbol, price, source, timestamp(datetime), success, stale, error
    """
    if kis_client is not None:
        try:
            data = kis_client.get_current_price(symbol)
            if data and data.get("current_price"):
                return {
                    "symbol": symbol, "price": float(data["current_price"]),
                    "source": "kis", "timestamp": _now(), "success": True,
                    "stale": False, "error": None,
                }
        except Exception as exc:
            logger.debug("[PriceWatcher] KIS 현재가 실패 %s: %s", symbol, exc)

    try:
        from app.data.naver_stock_collector import fetch_naver_current_price
        naver_result = fetch_naver_current_price(symbol)
        if naver_result.get("status") == "success":
            return {
                "symbol": symbol, "price": float(naver_result["current_price"]),
                "source": "naver", "timestamp": _now(), "success": True,
                "stale": False, "error": None,
            }
    except Exception as exc:
        logger.debug("[PriceWatcher] Naver 현재가 실패 %s: %s", symbol, exc)

    if last_known and last_known.get("price"):
        return {
            "symbol": symbol, "price": last_known["price"], "source": "last_known",
            "timestamp": last_known.get("timestamp", _now()), "success": False,
            "stale": True, "error": "kis_and_naver_failed",
        }

    return {
        "symbol": symbol, "price": None, "source": "none", "timestamp": _now(),
        "success": False, "stale": True, "error": "no_data_available",
    }


class PriceWatcher:
    """포지션 감시용 현재가 캐시 + 연속 실패 카운터."""

    def __init__(self, kis_client=None, stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS):
        self.kis_client = kis_client
        self.stale_after_seconds = stale_after_seconds
        self._cache: dict[str, dict] = {}
        self._consecutive_failures = 0

    def get_price(self, symbol: str) -> dict:
        last_known = self._cache.get(symbol)
        result = fetch_price_once(symbol, kis_client=self.kis_client, last_known=last_known)
        if result["success"]:
            self._cache[symbol] = {"price": result["price"], "timestamp": result["timestamp"]}
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            logger.warning(
                "[PriceWatcher] %s 현재가 조회 실패 (연속 %d회): %s",
                symbol, self._consecutive_failures, result.get("error"),
            )
        return result

    def get_prices(self, symbols: list[str]) -> dict[str, dict]:
        return {s: self.get_price(s) for s in symbols}

    def is_data_fresh(self, symbol: str) -> bool:
        cached = self._cache.get(symbol)
        if not cached:
            return False
        age = (_now() - cached["timestamp"]).total_seconds()
        return age <= self.stale_after_seconds

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def should_block_new_entries(self, threshold: int = DEFAULT_FAILURE_BLOCK_THRESHOLD) -> bool:
        return self._consecutive_failures >= threshold

    def run_loop(
        self,
        symbols_fn: Callable[[], list[str]],
        on_tick: Callable[[dict[str, dict]], None],
        interval_seconds: float = 2.0,
        max_iterations: Optional[int] = None,
        sleep_fn: Callable[[float], None] = None,
    ) -> None:
        """장중 반복 조회 루프 (선택적 사용 — Streamlit 페이지의 자체 rerun 루프 대신
        독립 스크립트/스레드에서 사용할 때만 필요)."""
        import time
        sleep_fn = sleep_fn or time.sleep
        i = 0
        while max_iterations is None or i < max_iterations:
            symbols = symbols_fn()
            if symbols:
                prices = self.get_prices(symbols)
                try:
                    on_tick(prices)
                except Exception as exc:
                    logger.error("[PriceWatcher] on_tick 처리 중 오류(무시하고 계속): %s", exc)
            sleep_fn(interval_seconds)
            i += 1
