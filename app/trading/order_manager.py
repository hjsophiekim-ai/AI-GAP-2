import csv
import random
import time
from pathlib import Path
from datetime import datetime

from app.models import BuyPlan, OrderResult, Position
from app.trading.broker_base import BrokerBase
from app.trading.kis_client import KISTokenError
from app.config import get_config
from app.logger import logger

# ── repo root (app/trading → app → repo root) ─────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent

# ── ETF / 지수 상품 사전 제외 키워드 ─────────────────────────────────────
_ETF_ORDER_KEYWORDS = [
    "KODEX", "TIGER", "ACE", "SOL", "PLUS", "KBSTAR", "KOSEF",
    "HANARO", "ARIRANG", "ETN", "ETF", "레버리지", "인버스", "선물",
    "합성", "RISE", "FOCUS", "TREX", "TIMEFOLIO", "WOORI",
    "WON", "채권", "국고채", "코스피200", "코스닥150", "나스닥", "S&P",
    "미국", "선진국", "신흥국", "글로벌", "달러",
]


def _is_etf_like(symbol: str, name: str) -> str:
    """ETF/ETN/지수 상품 여부 판단. 해당되면 이유 문자열, 아니면 빈 문자열."""
    upper_name = name.upper()
    for kw in _ETF_ORDER_KEYWORDS:
        if kw.upper() in upper_name:
            return f"etf_or_index_product ({kw})"
    if not symbol.isdigit() or len(symbol) != 6:
        return f"invalid_symbol_format ({symbol!r})"
    return ""


class OrderManager:
    def __init__(self, broker: BrokerBase, cfg=None):
        self.broker = broker
        self.cfg = cfg or get_config()
        self.bought_symbols: set[str] = set()

    # ── 배치 매수 실행 ────────────────────────────────────────────────────

    def execute_buy_plans(self, plans: list[BuyPlan]) -> list[OrderResult]:
        """
        계획된 매수를 순차 실행합니다.

        안전 규칙:
          - ETF/ETN/지수 상품 → 주문 전 사전 제외
          - 수량 < 1, 가격 <= 0 → validation_error 기록
          - 중복 종목 → 스킵
          - KISTokenError → 배치 전체 즉시 중단
          - HTTP 500 → 해당 종목 스킵, 다음 종목 계속
          - 종목 간 0.3~0.5 초 sleep
        """
        results: list[OrderResult] = []
        order_type = self.cfg.trading.get("order_type", "limit")
        mode = getattr(self.broker, "mode", "dry_run")
        token_aborted = False

        for idx, plan in enumerate(plans):
            if token_aborted:
                results.append(OrderResult(
                    success=False, mode=mode, account_type=mode,
                    symbol=plan.symbol, name=plan.name, side="buy",
                    quantity=plan.allocated_quantity,
                    price=plan.current_price, order_type=order_type,
                    order_id="", message="tokenP 403으로 배치 중단됨",
                    error_type="batch_aborted",
                ))
                continue

            symbol = plan.symbol
            name = plan.name
            price = plan.current_price
            qty = plan.allocated_quantity

            # 1. ETF / 지수 상품 사전 제외
            etf_reason = _is_etf_like(symbol, name)
            if etf_reason:
                logger.info(f"[ETF제외] {symbol} {name}: {etf_reason}")
                results.append(OrderResult(
                    success=False, mode=mode, account_type=mode,
                    symbol=symbol, name=name, side="buy",
                    quantity=qty, price=price, order_type=order_type,
                    order_id="", message=f"ETF/지수상품 제외: {etf_reason}",
                    error_type="excluded_etf",
                    excluded_reason=etf_reason,
                ))
                continue

            # 2. 기본 유효성 검증
            if qty < 1:
                logger.debug(f"[매수스킵] {symbol} {name}: qty={qty}")
                results.append(OrderResult(
                    success=False, mode=mode, account_type=mode,
                    symbol=symbol, name=name, side="buy",
                    quantity=qty, price=price, order_type=order_type,
                    order_id="", message=f"수량 부족: {qty}주",
                    error_type="validation_error",
                ))
                continue

            if price <= 0:
                logger.warning(f"[매수스킵] {symbol} {name}: price={price}")
                results.append(OrderResult(
                    success=False, mode=mode, account_type=mode,
                    symbol=symbol, name=name, side="buy",
                    quantity=qty, price=price, order_type=order_type,
                    order_id="", message=f"가격 오류: {price}",
                    error_type="validation_error",
                ))
                continue

            # 3. 중복 제외
            if symbol in self.bought_symbols:
                logger.info(f"[중복스킵] {symbol} {name}: 이미 매수됨")
                results.append(OrderResult(
                    success=False, mode=mode, account_type=mode,
                    symbol=symbol, name=name, side="buy",
                    quantity=qty, price=price, order_type=order_type,
                    order_id="", message="중복 종목 스킵",
                    error_type="duplicate",
                ))
                continue

            # 4. 종목 간 sleep (첫 유효 종목 제외)
            order_idx = sum(
                1 for r in results
                if r.error_type not in ("excluded_etf", "validation_error", "duplicate")
            )
            if order_idx > 0:
                time.sleep(random.uniform(0.3, 0.5))

            logger.info(
                f"[매수시도] {symbol} {name} {qty}주 "
                f"@ {price:,.0f}원 ({order_type})"
            )

            # 5. 매수 실행
            try:
                result = self.broker.buy(
                    symbol=symbol,
                    name=name,
                    quantity=qty,
                    price=price,
                    order_type=order_type,
                )
            except KISTokenError as e:
                logger.error(f"[토큰오류] 배치 중단: {e}")
                results.append(OrderResult(
                    success=False, mode=mode, account_type=mode,
                    symbol=symbol, name=name, side="buy",
                    quantity=qty, price=price, order_type=order_type,
                    order_id="", message=str(e),
                    error_type="token_403",
                    http_status=403,
                ))
                token_aborted = True
                continue

            # HTTP 500 → 스킵
            if result.http_status == 500:
                logger.warning(f"[HTTP500스킵] {symbol} {name}: {result.message}")
                result.error_type = "order_500"
                results.append(result)
                continue

            if result.success:
                self.bought_symbols.add(symbol)
                logger.info(
                    f"[매수성공] {symbol} {name} {result.quantity}주 "
                    f"order_id={result.order_id}"
                )
            else:
                logger.warning(f"[매수실패] {symbol} {name}: {result.message}")

            results.append(result)

        return results

    # ── 전체 매도 실행 ────────────────────────────────────────────────────

    def execute_sell_all(
        self,
        positions: list[Position],
        prices: dict[str, float] = None,
    ) -> list[OrderResult]:
        results: list[OrderResult] = []
        order_type = self.cfg.trading.get("order_type", "limit")

        for position in positions:
            symbol = position.symbol
            price = (
                prices.get(symbol, position.current_price)
                if prices
                else position.current_price
            )

            logger.info(
                f"[매도시도] {symbol} {position.name} {position.quantity}주 "
                f"@ {price:,.0f}원 ({order_type})"
            )

            result = self.broker.sell(
                symbol=symbol,
                name=position.name,
                quantity=position.quantity,
                price=price,
                order_type=order_type,
            )

            if result.success:
                logger.info(
                    f"[매도성공] {symbol} {position.name} {result.quantity}주 "
                    f"order_id={result.order_id}"
                )
            else:
                logger.warning(
                    f"[매도실패] {symbol} {position.name}: {result.message}"
                )

            results.append(result)

        return results

    # ── 부분 매도 ─────────────────────────────────────────────────────────

    def execute_sell_partial(
        self,
        position: Position,
        quantity: int,
        price: float,
    ) -> OrderResult:
        order_type = self.cfg.trading.get("order_type", "limit")
        symbol = position.symbol

        logger.info(
            f"[부분매도시도] {symbol} {position.name} {quantity}주 "
            f"@ {price:,.0f}원 ({order_type})"
        )

        result = self.broker.sell(
            symbol=symbol,
            name=position.name,
            quantity=quantity,
            price=price,
            order_type=order_type,
        )

        if result.success:
            logger.info(
                f"[부분매도성공] {symbol} {position.name} {result.quantity}주 "
                f"order_id={result.order_id}"
            )
        else:
            logger.warning(
                f"[부분매도실패] {symbol} {position.name}: {result.message}"
            )

        return result

    # ── 주문 로그 저장 ────────────────────────────────────────────────────

    def save_order_log(
        self,
        results: list[OrderResult],
        date_str: str = None,
        label: str = "bulk_buy_orders",
    ) -> str:
        """
        주문 결과를 CSV로 저장합니다.
        path: data/logs/{label}_{YYYYMMDD_HHMMSS}.csv
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = _ROOT / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_path = log_dir / f"{label}_{ts}.csv"

        fieldnames = [
            "timestamp", "mode", "symbol", "name",
            "quantity", "price", "order_amount",
            "status", "order_no",
            "http_status", "kis_rt_cd", "kis_msg_cd", "kis_msg1",
            "error_type", "error_message", "excluded_reason",
        ]

        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in results:
                raw = r.raw or {}
                kis_rt_cd = str(raw.get("rt_cd", ""))
                kis_msg_cd = str(raw.get("msg_cd", ""))
                kis_msg1 = str(raw.get("msg1", ""))

                if r.excluded_reason:
                    status = "제외"
                elif r.error_type in ("duplicate", "validation_error", "batch_aborted"):
                    status = "스킵"
                elif r.success:
                    status = "성공"
                else:
                    status = "실패"

                writer.writerow({
                    "timestamp": r.timestamp,
                    "mode": r.mode,
                    "symbol": r.symbol,
                    "name": r.name,
                    "quantity": r.quantity,
                    "price": r.price,
                    "order_amount": int(r.quantity * r.price),
                    "status": status,
                    "order_no": r.order_id,
                    "http_status": r.http_status,
                    "kis_rt_cd": kis_rt_cd,
                    "kis_msg_cd": kis_msg_cd,
                    "kis_msg1": kis_msg1,
                    "error_type": r.error_type,
                    "error_message": r.message if not r.success else "",
                    "excluded_reason": r.excluded_reason,
                })

        logger.info(f"주문 로그 저장: {file_path} ({len(results)}건)")
        return str(file_path)
