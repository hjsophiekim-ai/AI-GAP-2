"""
order_executor.py

브로커 주문(매수/매도)을 재시도 로직과 함께 실행하고, 모든 체결/실패를
logs/trades/YYYYMMDD.csv 에 기록한다.

재시도 정책: 1차 재시도 -> 2차 재시도 -> 그래도 실패하면 긴급 알림 로그.
PAPER(dry_run/mock)/REAL 여부에 따라 안전하게 처리하며, REAL 모드에서는
주문 결과(성공 여부/주문번호)를 명시적으로 검증 로그에 남긴다.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.models import OrderResult
from app.utils.data_paths import LOGS_DIR as _LOGS_DIR

_ROOT = Path(__file__).resolve().parent.parent.parent
_TRADE_LOG_DIR = _LOGS_DIR / "trades"

MAX_RETRIES = 2  # 최초 시도 + 2회 재시도 = 총 3회


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _order_mode_label(broker) -> str:
    mode = getattr(broker, "mode", "dry_run")
    return "REAL" if mode == "real" else "PAPER"


class OrderExecutor:
    def __init__(self, broker, cfg=None, sleep_fn: Callable[[float], None] = None):
        self.broker = broker
        self.cfg = cfg
        self._sleep = sleep_fn or (lambda s: __import__("time").sleep(s))

    # ------------------------------------------------------------------
    def buy(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        reason: str = "",
        source: str = "auto",
    ) -> OrderResult:
        return self._execute_with_retry("buy", symbol, name, quantity, price, reason, source)

    def sell(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        reason: str = "",
        source: str = "auto",
    ) -> OrderResult:
        return self._execute_with_retry("sell", symbol, name, quantity, price, reason, source)

    # ------------------------------------------------------------------
    def _execute_with_retry(
        self,
        side: str,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        reason: str,
        source: str,
    ) -> OrderResult:
        mode_label = _order_mode_label(self.broker)
        last_result: Optional[OrderResult] = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                fn = self.broker.buy if side == "buy" else self.broker.sell
                result = fn(symbol=symbol, name=name, quantity=quantity, price=price)
            except Exception as exc:
                logger.error("[OrderExecutor] %s %s 주문 예외(attempt=%d): %s", side, symbol, attempt, exc)
                result = OrderResult(
                    success=False, mode=getattr(self.broker, "mode", "dry_run"),
                    account_type=getattr(self.broker, "mode", "dry_run"),
                    symbol=symbol, name=name, side=side, quantity=quantity, price=price,
                    order_type="limit", order_id="", message=str(exc), error_type="exception",
                )

            last_result = result
            if result.success:
                if mode_label == "REAL":
                    logger.info(
                        "[OrderExecutor][REAL 확인] %s %s %d주 order_id=%s 성공 (attempt=%d)",
                        side, symbol, quantity, result.order_id, attempt + 1,
                    )
                self._log_trade(result, reason, source, mode_label, attempt + 1)
                return result

            logger.warning(
                "[OrderExecutor] %s %s 실패(attempt=%d/%d): %s",
                side, symbol, attempt + 1, MAX_RETRIES + 1, result.message,
            )
            if attempt < MAX_RETRIES:
                self._sleep(0.5 * (attempt + 1))

        logger.error(
            "[OrderExecutor][긴급알림] %s %s %d주 @ %.0f 주문 최종 실패 (mode=%s, source=%s): %s",
            side, symbol, quantity, price, mode_label, source,
            last_result.message if last_result else "unknown",
        )
        self._log_trade(last_result, reason, source, mode_label, MAX_RETRIES + 1, final_failure=True)
        return last_result

    # ------------------------------------------------------------------
    def _log_trade(
        self,
        result: OrderResult,
        reason: str,
        source: str,
        mode_label: str,
        attempts: int,
        final_failure: bool = False,
    ) -> None:
        try:
            _TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = _TRADE_LOG_DIR / f"{_today()}.csv"
            is_new = not path.exists()
            with open(path, "a", newline="", encoding="utf-8-sig") as f:
                fieldnames = [
                    "timestamp", "symbol", "name", "side", "quantity", "price",
                    "profit_rate", "reason", "source", "order_mode", "success",
                    "order_id", "attempts", "final_failure", "message",
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if is_new:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": result.timestamp,
                    "symbol": result.symbol,
                    "name": result.name,
                    "side": result.side,
                    "quantity": result.quantity,
                    "price": result.price,
                    "profit_rate": "",
                    "reason": reason,
                    "source": source,
                    "order_mode": mode_label,
                    "success": result.success,
                    "order_id": result.order_id,
                    "attempts": attempts,
                    "final_failure": final_failure,
                    "message": result.message,
                })
        except Exception as exc:
            logger.warning("[OrderExecutor] 거래 로그 저장 실패: %s", exc)
