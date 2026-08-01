"""TSLA_AUTO KIS overseas broker adapter.

This module is independent from domestic-stock broker code. MOCK routes to
KIS overseas paper-trading endpoints; REAL is still gated by the existing
TSLA_AUTO real-order safety flags.
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
    side: str
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
    mode: str

    def get_positions(self) -> list[Any]:
        from app.trading.tsla_auto.kis_overseas_adapter import (
            KisOverseasApiConfirmationRequired,
            fetch_overseas_balance,
        )

        positions, _cash, error = fetch_overseas_balance(self.mode)
        if error:
            raise KisOverseasApiConfirmationRequired(f"overseas balance read failed: {error}")
        return list(positions)

    def get_position(self, symbol: str) -> Optional[Any]:
        for pos in self.get_positions():
            if getattr(pos, "symbol", None) == symbol:
                return pos
        return None

    def get_open_orders(self) -> list[Any]:
        from app.trading.tsla_auto.kis_overseas_adapter import (
            KisOverseasApiConfirmationRequired,
            fetch_overseas_open_orders,
        )

        rows, error, _raw = fetch_overseas_open_orders(self.mode)
        if error:
            raise KisOverseasApiConfirmationRequired(f"overseas open orders read failed: {error}")
        return list(rows)

    def get_orderable_usd(self, symbol: str, *, price: float = 0.0) -> float:
        from app.trading.tsla_auto.kis_overseas_adapter import (
            KisOverseasApiConfirmationRequired,
            fetch_overseas_buyable_amount,
        )

        amount, error = fetch_overseas_buyable_amount(
            self.mode,
            symbol,
            exchange_code=config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "NASD"),
            price=price,
        )
        if error:
            raise KisOverseasApiConfirmationRequired(f"overseas buyable amount read failed: {error}")
        return float(amount or 0.0)

    def get_buy_sizing_quote(self, symbol: str, *, price: float) -> BuySizingQuote:
        from app.trading.tsla_auto.kis_overseas_adapter import (
            KisOverseasApiConfirmationRequired,
            fetch_overseas_buyable_quantity,
        )

        exchange = config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "NASD")
        if symbol == config.INVERSE_SYMBOL and not exchange:
            raise KisOverseasApiConfirmationRequired(config.TSLZ_EXCHANGE_UNRESOLVED)
        quote, error = fetch_overseas_buyable_quantity(self.mode, symbol, exchange_code=exchange, price=price)
        if error or quote is None:
            raise KisOverseasApiConfirmationRequired(f"overseas buyable quantity read failed: {error}")
        raw = quote.raw or {}
        return BuySizingQuote(
            symbol=symbol,
            order_type="limit",
            available_usd=quote.available_usd,
            available_qty=quote.available_qty,
            order_price=quote.order_price,
            rt_cd=str(raw.get("_rt_cd") or raw.get("rt_cd") or "0"),
            msg_cd=str(raw.get("_msg_cd") or raw.get("msg_cd") or ""),
            msg1=str(raw.get("_msg1") or raw.get("msg1") or ""),
            raw=raw,
        )

    def get_fresh_ask1(self, symbol: str) -> dict[str, Any]:
        from app.trading.tsla_auto.kis_overseas_adapter import (
            KisOverseasApiConfirmationRequired,
            fetch_overseas_asking_price,
        )

        exchange = config.QUOTE_EXCHANGE_BY_SYMBOL.get(symbol, config.EXCHANGE_CODE)
        if symbol == config.INVERSE_SYMBOL and not exchange:
            raise KisOverseasApiConfirmationRequired(config.TSLZ_EXCHANGE_UNRESOLVED)
        quote, error = fetch_overseas_asking_price(self.mode, symbol, exchange_code=exchange)
        if self.mode == "mock" and error == "MOCK_ASKING_PRICE_UNSUPPORTED_BY_KIS":
            from app.trading.tsla_auto.kis_overseas_adapter import fetch_overseas_current_price

            last, last_error = fetch_overseas_current_price(self.mode, symbol, exchange_code=exchange)
            if last is not None and last_error is None and last.price > 0:
                return {
                    "ok": True,
                    "ask1": last.price,
                    "bid1": last.price,
                    "source": "mock_current_price_fallback",
                    "msg1": error,
                    "raw": last.raw,
                }
        if error or quote is None:
            return {"ok": False, "ask1": 0.0, "bid1": 0.0, "msg1": error or "ask1 unavailable"}
        return quote

    def buy_limit(self, symbol: str, qty: int, price: float, client_order_id: str) -> BrokerOrderResult:
        from app.trading.tsla_auto.kis_overseas_adapter import place_overseas_limit_order

        del client_order_id
        res = place_overseas_limit_order(
            self.mode,
            symbol,
            "BUY",
            qty,
            price,
            exchange_code=config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "NASD"),
        )
        return BrokerOrderResult(
            res.success,
            res.order_id,
            res.symbol,
            res.side,
            res.requested_qty,
            res.executed_qty,
            res.executed_price,
            res.msg1 or res.msg_cd,
            raw=res.raw,
        )

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        from app.trading.tsla_auto.kis_overseas_adapter import (
            KisOverseasApiConfirmationRequired,
            place_overseas_limit_order,
        )

        del client_order_id
        quote = self.get_fresh_ask1(symbol)
        bid1 = float(quote.get("bid1") or 0.0)
        if bid1 <= 0:
            raise KisOverseasApiConfirmationRequired("fresh bid1 required for sell limit")
        price = round(max(bid1 - 0.01, 0.01), 2)
        res = place_overseas_limit_order(
            self.mode,
            symbol,
            "SELL",
            qty,
            price,
            exchange_code=config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "NASD"),
        )
        return BrokerOrderResult(
            res.success,
            res.order_id,
            res.symbol,
            res.side,
            res.requested_qty,
            res.executed_qty,
            res.executed_price or price,
            res.msg1 or res.msg_cd,
            raw=res.raw,
        )

    def cancel_order(self, order_id: str, symbol: str = "") -> BrokerOrderResult:
        from app.trading.tsla_auto.kis_overseas_adapter import cancel_overseas_order

        res = cancel_overseas_order(
            self.mode,
            order_id,
            symbol,
            exchange_code=config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "NASD"),
        )
        return BrokerOrderResult(
            res.success,
            res.order_id,
            symbol,
            "CANCEL",
            res.requested_qty,
            res.executed_qty,
            res.executed_price,
            res.msg1 or res.msg_cd,
            raw=res.raw,
        )

    def reconcile_position(self, symbol: str) -> int:
        pos = self.get_position(symbol)
        return int(getattr(pos, "quantity", 0)) if pos else 0

    def is_market_open(self) -> bool:
        from app.trading.tsla_auto import market_session

        return market_session.classify_session_status() == "REGULAR"


class MockBrokerAdapter(_BrokerAdapterBase):
    mode = "mock"


class RealBrokerAdapter(_BrokerAdapterBase):
    mode = "real"

    def __init__(self, *, confirm_text: str = "", runtime_allow_real_order: bool = False) -> None:
        if not runtime_allow_real_order or not config.ALLOW_REAL_ORDER:
            raise PermissionError(
                "TSLA_AUTO REAL order path is disabled by default "
                "(TSLA_AUTO_ALLOW_REAL_ORDER=false)."
            )
        self._confirm_text = confirm_text


def create_tsla_auto_broker(mode: str, **kwargs: Any) -> _BrokerAdapterBase:
    if mode == "mock":
        return MockBrokerAdapter()
    if mode == "real":
        return RealBrokerAdapter(**kwargs)
    raise ValueError(f"create_tsla_auto_broker: unknown mode {mode!r}")
