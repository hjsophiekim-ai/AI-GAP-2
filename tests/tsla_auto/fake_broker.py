"""FakeBroker — in-memory, network-free broker double for tests/tsla_auto.

Duck-types app.trading.tsla_auto.broker_adapter's Mock/RealBrokerAdapter
interface. No network, no real broker construction. A green test suite using
this double validates WORKER/ORDER_EXECUTOR LOGIC only — it is never "실제
KIS MOCK 검증 완료" (docs §17), since the real KIS overseas order/balance
TRs are unconfirmed (see broker_adapter.py/kis_overseas_adapter.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.trading.tsla_auto.broker_adapter import BrokerOrderResult, BuySizingQuote


class FakePosition:
    def __init__(self, symbol: str, quantity: int, avg_price: float) -> None:
        self.symbol = symbol
        self.quantity = quantity
        self.avg_price = avg_price


class FakeBroker:
    mode = "mock"

    def __init__(
        self, *, cash_usd: float = 100_000.0, quotes: Optional[dict[str, float]] = None,
        storage_path: Optional[Path | str] = None,
    ) -> None:
        if storage_path is not None:
            resolved = Path(storage_path).resolve()
            parts = {p.lower() for p in resolved.parts}
            if "data" in parts and "tsla_auto" in parts:
                raise RuntimeError(f"FakeBroker must not use an operational TSLA_AUTO path: {resolved}")
        self._cash = cash_usd
        self._quotes: dict[str, float] = dict(quotes or {})
        self._positions: dict[str, FakePosition] = {}
        self._order_seq = 0
        self.orders: list[BrokerOrderResult] = []
        self.fail_next_buy = False
        self.fail_next_sell = False
        self.next_ask1: Optional[float] = None
        self.fail_next_ask = False
        self.next_available_qty: Optional[int] = None
        self.buy_sizing_quotes: list[BuySizingQuote] = []
        self.next_buy_fill_qty: Optional[int] = None
        self.sell_fill_plan: dict[str, list[int]] = {}
        self.cancel_calls: list[tuple[str, str]] = []
        self.fail_next_cancel = False
        self.open_orders: list[object] = []

    def set_quote(self, symbol: str, price: float) -> None:
        self._quotes[symbol] = price

    def get_cash(self) -> float:
        return self._cash

    def get_orderable_usd(self, symbol: str, *, price: float = 0.0) -> float:
        del symbol, price
        return self._cash

    def get_buy_sizing_quote(self, symbol: str, *, price: float) -> BuySizingQuote:
        qty = int(self._cash // price) if price > 0 else 0
        if self.next_available_qty is not None:
            qty = self.next_available_qty
        quote = BuySizingQuote(
            symbol=symbol, order_type="limit", available_usd=self._cash, available_qty=qty,
            order_price=float(price), rt_cd="0", msg_cd="FAKE_OK", msg1="fake buyable ok", raw={},
        )
        self.buy_sizing_quotes.append(quote)
        return quote

    def get_fresh_ask1(self, symbol: str) -> dict:
        if self.fail_next_ask:
            self.fail_next_ask = False
            return {"ok": False, "symbol": symbol, "ask1": 0.0, "rt_cd": "1", "msg_cd": "FAKE_ASK", "msg1": "ask failed"}
        ask1 = self.next_ask1 if self.next_ask1 is not None else self._quotes.get(symbol)
        return {
            "ok": bool(ask1 and ask1 > 0), "symbol": symbol, "ask1": float(ask1 or 0.0), "bid1": float(ask1 or 0.0) - 0.01,
            "rt_cd": "0" if ask1 else "1", "msg_cd": "FAKE_ASK_OK" if ask1 else "FAKE_ASK_MISSING",
            "msg1": "fake ask ok" if ask1 else "ask missing",
        }

    def get_quote(self, symbol: str) -> Optional[float]:
        return self._quotes.get(symbol)

    def get_positions(self) -> list[FakePosition]:
        return list(self._positions.values())

    def get_open_orders(self) -> list:
        return []

    def get_position(self, symbol: str) -> Optional[FakePosition]:
        return self._positions.get(symbol)

    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"FAKE-{self._order_seq:06d}"

    def buy_limit(self, symbol: str, qty: int, price: float, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        if self.fail_next_buy or price <= 0 or qty < 1:
            self.fail_next_buy = False
            result = BrokerOrderResult(
                False, self._next_order_id(), symbol, "BUY", qty, 0, 0.0, "FAKE_BUY_FAILED",
                raw={"rt_cd": "1", "msg_cd": "FAKE_REJECT", "msg1": "fake rejected"},
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
                self._positions[symbol] = FakePosition(symbol, total_qty, new_avg)
            else:
                self._positions[symbol] = FakePosition(symbol, fill_qty, float(price))
        result = BrokerOrderResult(True, self._next_order_id(), symbol, "BUY", qty, fill_qty, float(price), "OK", raw={})
        self.orders.append(result)
        return result

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self._quotes.get(symbol)
        existing = self._positions.get(symbol)
        if self.fail_next_sell or price is None or existing is None or existing.quantity < qty:
            self.fail_next_sell = False
            result = BrokerOrderResult(False, self._next_order_id(), symbol, "SELL", qty, 0, 0.0, "FAKE_SELL_FAILED")
            self.orders.append(result)
            return result
        plan = self.sell_fill_plan.get(symbol) or []
        fill_qty = qty
        if plan:
            fill_qty = max(0, min(qty, int(plan.pop(0))))
            if plan:
                self.sell_fill_plan[symbol] = plan
            else:
                self.sell_fill_plan.pop(symbol, None)
        self._cash += price * fill_qty
        remaining = existing.quantity - fill_qty
        if remaining <= 0:
            del self._positions[symbol]
        else:
            self._positions[symbol] = FakePosition(symbol, remaining, existing.avg_price)
        result = BrokerOrderResult(True, self._next_order_id(), symbol, "SELL", qty, fill_qty, price, "OK")
        self.orders.append(result)
        return result

    def cancel_order(self, order_id: str, symbol: str = "") -> BrokerOrderResult:
        self.cancel_calls.append((str(order_id), str(symbol)))
        if self.fail_next_cancel:
            self.fail_next_cancel = False
            return BrokerOrderResult(False, str(order_id), symbol, "CANCEL", 0, 0, 0.0, "FAKE_CANCEL_FAILED")
        self.open_orders = [
            order for order in self.open_orders
            if str(getattr(order, "order_id", "") or getattr(order, "odno", "") or "") != str(order_id)
        ]
        return BrokerOrderResult(True, str(order_id), symbol, "CANCEL", 0, 0, 0.0, "OK")

    def get_open_orders(self) -> list[object]:
        return list(self.open_orders)

    def reconcile_position(self, symbol: str) -> int:
        pos = self._positions.get(symbol)
        return int(pos.quantity) if pos else 0

    def is_market_open(self) -> bool:
        return True
