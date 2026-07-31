"""TSLA_AUTO broker adapter — thin wrapper over kis_overseas_adapter.py only.

Never imports app.trading.broker_factory / app.trading.broker_base / KisMockBroker
/ KisRealBroker (domestic, docs/TSLA_AUTO_COPY_MAP.md — DO_NOT_COPY) and never
imports app.trading.kis_client (domestic order/balance functions).

**REAL and MOCK order/balance calls are both currently unimplemented** — the
underlying KIS 해외주식 주문/잔고/주문가능금액·수량 TR_IDs were not confirmed
against official docs in this session (docs §KIS 해외주식 API). Calling any
order/balance method here raises
``kis_overseas_adapter.KisOverseasApiConfirmationRequired`` — it never
silently succeeds or returns a guessed value. Quote/minute-candle methods
(handled by market_data.py, not this file) ARE implemented against confirmed
TRs. Tests must use a FakeBroker test double (tests/tsla_auto/fake_broker.py)
— never this class — to exercise order_executor/worker logic; a green test
suite using FakeBroker is worker-logic validation only, never "실제 KIS MOCK
검증 완료" (docs §17).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.trading.tsla_auto import config


@dataclass(frozen=True)
class BrokerOrderResult:
    success: bool
    order_id: str
    symbol: str
    side: str  # "BUY" / "SELL"
    requested_qty: int
    executed_qty: int
    executed_price: float
    message: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BuySizingQuote:
    symbol: str
    order_type: str
    available_usd: float
    available_qty: int
    order_price: float
    rt_cd: str
    msg_cd: str
    msg1: str
    raw: dict[str, Any] = field(default_factory=dict)


class _BrokerAdapterBase:
    """Shared implementation for Mock/Real adapters (docs §14 주문 로직 interface)."""

    mode: str  # "mock" | "real"

    def get_positions(self) -> list[Any]:
        from app.trading.tsla_auto.kis_overseas_adapter import fetch_overseas_balance

        positions, _cash, error = fetch_overseas_balance(self.mode)
        if error:
            from app.trading.tsla_auto.kis_overseas_adapter import KisOverseasApiConfirmationRequired

            raise KisOverseasApiConfirmationRequired(f"overseas balance read failed: {error}")
        return list(positions)

    def get_position(self, symbol: str) -> Optional[Any]:
        for pos in self.get_positions():
            if getattr(pos, "symbol", None) == symbol:
                return pos
        return None

    def get_orderable_usd(self, symbol: str, *, price: float = 0.0) -> float:
        from app.trading.tsla_auto.kis_overseas_adapter import fetch_overseas_buyable_amount

        amount, error = fetch_overseas_buyable_amount(
            self.mode, symbol, exchange_code=config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "NASD"), price=price,
        )
        if error:
            from app.trading.tsla_auto.kis_overseas_adapter import KisOverseasApiConfirmationRequired

            raise KisOverseasApiConfirmationRequired(f"overseas buyable amount read failed: {error}")
        return float(amount or 0.0)

    def get_buy_sizing_quote(self, symbol: str, *, price: float) -> BuySizingQuote:
        from app.trading.tsla_auto.kis_overseas_adapter import fetch_overseas_buyable_quantity

        exchange = config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "NASD")
        if symbol == config.INVERSE_SYMBOL and not exchange:
            from app.trading.tsla_auto.kis_overseas_adapter import KisOverseasApiConfirmationRequired

            raise KisOverseasApiConfirmationRequired(config.TSLZ_EXCHANGE_UNRESOLVED)
        quote, error = fetch_overseas_buyable_quantity(self.mode, symbol, exchange_code=exchange, price=price)
        if error or quote is None:
            from app.trading.tsla_auto.kis_overseas_adapter import KisOverseasApiConfirmationRequired

            raise KisOverseasApiConfirmationRequired(f"overseas buyable quantity read failed: {error}")
        return BuySizingQuote(
            symbol=symbol, order_type="limit", available_usd=quote.available_usd,
            available_qty=quote.available_qty, order_price=quote.order_price,
            rt_cd="0", msg_cd="", msg1="", raw=quote.raw,
        )

    def get_fresh_ask1(self, symbol: str) -> dict[str, Any]:
        """MACD2와 달리, 해외주식 호가(bid1/ask1) 전용 TR도 이 세션에서
        미확인이다 — 현재가상세(HHDFS00000300) 응답에 별도 매도호가 필드가
        없다면 quote 가격을 임시 ask1 근사치로 쓰지 않고 명시적으로
        실패시켜야 한다(임의 시장가 전환 금지 원칙과 동일하게, 임의 호가
        근사도 금지)."""
        from app.trading.tsla_auto.kis_overseas_adapter import KisOverseasApiConfirmationRequired

        raise KisOverseasApiConfirmationRequired("TSLL/TSLZ 호가(매도1호가 ask1) 조회")

    def buy_limit(self, symbol: str, qty: int, price: float, client_order_id: str) -> BrokerOrderResult:
        from app.trading.tsla_auto.kis_overseas_adapter import place_overseas_limit_order

        del client_order_id
        place_overseas_limit_order(self.mode, symbol, "BUY", qty, price, exchange_code=config.EXCHANGE_CODE)
        raise AssertionError("unreachable")

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        """docs §14 반대전환: 기존 ETF 전량 SELL. 실제 TR 미확인이므로
        (§KIS_OVERSEAS_API_CONFIRMATION_REQUIRED) place_overseas_limit_order와
        동일하게 차단한다 — SELL도 임의 구현하지 않는다."""
        from app.trading.tsla_auto.kis_overseas_adapter import place_overseas_limit_order

        del client_order_id
        place_overseas_limit_order(self.mode, symbol, "SELL", qty, 0.0, exchange_code=config.EXCHANGE_CODE)
        raise AssertionError("unreachable")

    def cancel_order(self, order_id: str, symbol: str = "") -> BrokerOrderResult:
        from app.trading.tsla_auto.kis_overseas_adapter import cancel_overseas_order

        cancel_overseas_order(self.mode, order_id, symbol)
        raise AssertionError("unreachable")

    def reconcile_position(self, symbol: str) -> int:
        pos = self.get_position(symbol)
        return int(getattr(pos, "quantity", 0)) if pos else 0

    def is_market_open(self) -> bool:
        from app.trading.tsla_auto import market_session

        return market_session.classify_session_status() == "REGULAR"


class MockBrokerAdapter(_BrokerAdapterBase):
    """"MOCK" mode — intended to route to KIS's paper-trading (모의투자)
    overseas endpoint, but that TR path is unconfirmed (docs §KIS 해외주식 API
    "REAL·MOCK 지원 차이"). Order/balance methods raise
    KisOverseasApiConfirmationRequired exactly like RealBrokerAdapter — this
    is intentional, not a bug: MOCK here means "connect to KIS's mock
    servers", not "an in-memory fake". Use FakeBroker for tests."""

    mode = "mock"


class RealBrokerAdapter(_BrokerAdapterBase):
    """REAL mode. Gated by config.ALLOW_REAL_ORDER (default False) at the
    Service layer before this adapter is ever constructed for live use —
    this adapter itself does not duplicate that gate, matching MACD2's own
    docs §14 design (REAL safety gates live at construction time)."""

    mode = "real"

    def __init__(self, *, confirm_text: str = "", runtime_allow_real_order: bool = False) -> None:
        if not runtime_allow_real_order or not config.ALLOW_REAL_ORDER:
            raise PermissionError(
                "TSLA_AUTO REAL order path is disabled by default "
                "(TSLA_AUTO_ALLOW_REAL_ORDER=false) — this work item does not enable it."
            )
        self._confirm_text = confirm_text


def create_tsla_auto_broker(mode: str, **kwargs: Any) -> _BrokerAdapterBase:
    """docs §14 factory: mode in {"mock", "real"}. "READ_ONLY" never
    constructs a broker at all (service.py) — only market_data quotes."""
    if mode == "mock":
        return MockBrokerAdapter()
    if mode == "real":
        return RealBrokerAdapter(**kwargs)
    raise ValueError(f"create_tsla_auto_broker: unknown mode {mode!r}")
