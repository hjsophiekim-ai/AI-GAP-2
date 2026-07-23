"""FakeBroker — in-memory, network-free broker double for tests/macd2.

Duck-types app.trading.macd2.broker_adapter's Mock/RealBrokerAdapter
interface (docs/MACD2_LOGIC.md §9). No network, no real broker construction —
entirely separate from the production adapters (docs §18).
"""
from __future__ import annotations

from typing import Optional

from app.models import Position
from app.trading.macd2.broker_adapter import BrokerOrderResult


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
            result = BrokerOrderResult(
                False, self._next_order_id(), symbol, "BUY", qty, 0, 0.0, "FAKE_BUY_FAILED",
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
        result = BrokerOrderResult(True, self._next_order_id(), symbol, "BUY", qty, fill_qty, price, "OK")
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
