"""FakeBroker — in-memory, network-free broker double for tests/macd2.

Duck-types app.trading.macd2.broker_adapter's Mock/RealBrokerAdapter
interface (docs/MACD2_LOGIC.md §9). No network, no real broker construction —
entirely separate from the production adapters (docs §18).
"""
from __future__ import annotations

from typing import Optional

from app.models import Position
from app.trading.macd2.broker_adapter import BrokerOrderResult, BuySizingQuote


class FakeBroker:
    mode = "mock"

    def __init__(self, *, cash: float = 10_000_000.0, quotes: Optional[dict[str, float]] = None) -> None:
        self._cash = cash
        self._quotes: dict[str, float] = dict(quotes or {})
        self._positions: dict[str, Position] = {}
        self._order_seq = 0
        self.orders: list[BrokerOrderResult] = []
        self.fail_next_buy = False
        self.fail_next_sell = False
        self.next_buy_order_id: Optional[str] = None
        self.next_nrcvb_buy_qty: Optional[int] = None
        self.next_ask1: Optional[float] = None
        self.fail_next_ask = False
        self.buy_sizing_quotes: list[BuySizingQuote] = []
        # Partial/zero-fill simulation: caps the NEXT buy's actual fill below
        # the requested qty (docs: 부분체결 / BUY 후 보유 0). None means "fill
        # the full requested qty" (the default, existing behavior).
        self.next_buy_fill_qty: Optional[int] = None

    def set_quote(self, symbol: str, price: float) -> None:
        self._quotes[symbol] = price

    def get_cash(self) -> float:
        return self._cash

    def get_orderable_cash(self, symbol: str) -> float:
        del symbol
        return self._cash

    def get_buy_sizing_quote(self, symbol: str, *, price: float, order_type: str = "market") -> BuySizingQuote:
        ord_dvsn = "01" if order_type == "market" else ("11" if order_type == "ioc_limit" else "00")
        qty = int(self._cash // price) if price > 0 else 0
        if self.next_nrcvb_buy_qty is not None:
            qty = self.next_nrcvb_buy_qty
        quote = BuySizingQuote(
            symbol=symbol,
            order_type=order_type,
            ord_dvsn=ord_dvsn,
            orderable_cash=self._cash,
            nrcvb_buy_amt=self._cash,
            nrcvb_buy_qty=qty,
            psbl_qty_calc_unpr=price,
            psbl_qty=qty,
            rt_cd="0",
            msg_cd="FAKE_OK",
            msg1="fake buyable ok",
            ask1=float(self.next_ask1 or self._quotes.get(symbol) or 0.0),
            order_price=float(price),
            usable_cash=self._cash,
            limit_buyable_qty=qty if order_type == "ioc_limit" else 0,
            raw={"rt_cd": "0", "msg_cd": "FAKE_OK", "msg1": "fake buyable ok", "ORD_DVSN": ord_dvsn},
        )
        self.buy_sizing_quotes.append(quote)
        return quote

    def get_fresh_ask1(self, symbol: str) -> dict:
        if self.fail_next_ask:
            self.fail_next_ask = False
            return {"ok": False, "symbol": symbol, "ask1": 0.0, "rt_cd": "1", "msg_cd": "FAKE_ASK", "msg1": "ask failed"}
        ask1 = self.next_ask1 if self.next_ask1 is not None else self._quotes.get(symbol)
        return {
            "ok": bool(ask1 and ask1 > 0),
            "symbol": symbol,
            "ask1": float(ask1 or 0.0),
            "rt_cd": "0" if ask1 else "1",
            "msg_cd": "FAKE_ASK_OK" if ask1 else "FAKE_ASK_MISSING",
            "msg1": "fake ask ok" if ask1 else "ask missing",
        }

    def get_quote(self, symbol: str) -> Optional[float]:
        return self._quotes.get(symbol)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"FAKE-{self._order_seq:06d}"

    def buy_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self._quotes.get(symbol)
        if self.fail_next_buy or price is None or qty < 1:
            self.fail_next_buy = False
            order_id = self.next_buy_order_id if self.next_buy_order_id is not None else self._next_order_id()
            self.next_buy_order_id = None
            result = BrokerOrderResult(
                False, order_id, symbol, "BUY", qty, 0, 0.0, "FAKE_BUY_FAILED",
                raw={"rt_cd": "1", "msg_cd": "FAKE_REJECT", "msg1": "fake rejected"},
            )
            self.orders.append(result)
            return result
        fill_qty = qty if self.next_buy_fill_qty is None else max(0, min(qty, self.next_buy_fill_qty))
        self.next_buy_fill_qty = None
        self._cash -= price * fill_qty
        if fill_qty > 0:
            existing = self._positions.get(symbol)
            if existing:
                total_qty = existing.quantity + fill_qty
                new_avg = (existing.avg_price * existing.quantity + price * fill_qty) / total_qty
                self._positions[symbol] = Position(
                    symbol=symbol, name=symbol, quantity=total_qty, avg_price=new_avg, current_price=price,
                )
            else:
                self._positions[symbol] = Position(
                    symbol=symbol, name=symbol, quantity=fill_qty, avg_price=price, current_price=price,
                )
        order_id = self.next_buy_order_id if self.next_buy_order_id is not None else self._next_order_id()
        self.next_buy_order_id = None
        result = BrokerOrderResult(True, order_id, symbol, "BUY", qty, fill_qty, price, "OK", raw={"ORD_DVSN": "01"})
        self.orders.append(result)
        return result

    def buy_ioc_limit(self, symbol: str, qty: int, price: float, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        if self.fail_next_buy or price <= 0 or qty < 1:
            self.fail_next_buy = False
            order_id = self.next_buy_order_id if self.next_buy_order_id is not None else self._next_order_id()
            self.next_buy_order_id = None
            result = BrokerOrderResult(
                False, order_id, symbol, "BUY", qty, 0, 0.0, "FAKE_BUY_FAILED",
                raw={"rt_cd": "1", "msg_cd": "FAKE_REJECT", "msg1": "fake rejected", "ORD_DVSN": "11"},
            )
            self.orders.append(result)
            return result
        fill_qty = qty if self.next_buy_fill_qty is None else max(0, min(qty, self.next_buy_fill_qty))
        self.next_buy_fill_qty = None
        self._cash -= float(price) * fill_qty
        if fill_qty > 0:
            existing = self._positions.get(symbol)
            if existing:
                total_qty = existing.quantity + fill_qty
                new_avg = (existing.avg_price * existing.quantity + float(price) * fill_qty) / total_qty
                self._positions[symbol] = Position(
                    symbol=symbol, name=symbol, quantity=total_qty, avg_price=new_avg, current_price=float(price),
                )
            else:
                self._positions[symbol] = Position(
                    symbol=symbol, name=symbol, quantity=fill_qty, avg_price=float(price), current_price=float(price),
                )
        order_id = self.next_buy_order_id if self.next_buy_order_id is not None else self._next_order_id()
        self.next_buy_order_id = None
        result = BrokerOrderResult(True, order_id, symbol, "BUY", qty, fill_qty, float(price), "OK", raw={"ORD_DVSN": "11"})
        self.orders.append(result)
        return result

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self._quotes.get(symbol)
        existing = self._positions.get(symbol)
        if self.fail_next_sell or price is None or existing is None or existing.quantity < qty:
            self.fail_next_sell = False
            result = BrokerOrderResult(
                False, self._next_order_id(), symbol, "SELL", qty, 0, 0.0, "FAKE_SELL_FAILED",
            )
            self.orders.append(result)
            return result
        self._cash += price * qty
        remaining = existing.quantity - qty
        if remaining <= 0:
            del self._positions[symbol]
        else:
            self._positions[symbol] = Position(
                symbol=symbol, name=symbol, quantity=remaining, avg_price=existing.avg_price, current_price=price,
            )
        result = BrokerOrderResult(True, self._next_order_id(), symbol, "SELL", qty, qty, price, "OK")
        self.orders.append(result)
        return result

    def wait_for_execution(self, order_id: str, timeout: float = 10.0) -> BrokerOrderResult:
        del timeout
        for order in reversed(self.orders):
            if order.order_id == order_id:
                return order
        raise LookupError(f"FakeBroker: unknown order_id {order_id!r}")

    def reconcile_position(self, symbol: str) -> int:
        pos = self._positions.get(symbol)
        return int(pos.quantity) if pos else 0

    def is_market_open(self) -> bool:
        return True
