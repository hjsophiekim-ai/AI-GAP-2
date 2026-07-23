"""MACD2 broker adapter — thin wrapper over the existing, shared broker layer.

Reuses app.trading.broker_factory.create_broker / KisMockBroker /
KisRealBroker / BrokerBase directly — these are generic trading
infrastructure shared by Enhanced and MACD v1, not MACD-v1 domain code (see
the 2026-07-23 code-reuse audit). This module does NOT import anything from
app.trading.macd_hynix_* or app.trading.macd_pipeline.* (docs/MACD2_LOGIC.md
§9/§14 — REAL confirm/gate lives entirely inside KisRealBroker.__init__;
MockBrokerAdapter never touches it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.models import Position
from app.trading.broker_base import BrokerBase


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


def _to_order_result(raw_result: Any, symbol: str, side: str, requested_qty: int) -> BrokerOrderResult:
    success = bool(getattr(raw_result, "success", False))
    return BrokerOrderResult(
        success=success,
        order_id=str(getattr(raw_result, "order_id", "") or ""),
        symbol=symbol,
        side=side,
        requested_qty=requested_qty,
        executed_qty=requested_qty if success else 0,
        executed_price=float(getattr(raw_result, "price", 0.0) or 0.0),
        message=str(getattr(raw_result, "message", "") or ""),
        raw=dict(getattr(raw_result, "raw", {}) or {}),
    )


class _BrokerAdapterBase:
    """Shared implementation for Mock/Real adapters (docs/MACD2_LOGIC.md §9 interface)."""

    mode: str
    _broker: BrokerBase

    def get_cash(self) -> float:
        return float(self._broker.get_balance())

    def get_orderable_cash(self, symbol: str) -> float:
        # The shared broker layer exposes account-level orderable cash; MACD2's
        # budget-vs-cash comparison (docs §9) only needs the account total.
        del symbol
        getter = getattr(self._broker, "get_orderable_cash", None) or self._broker.get_buyable_cash
        return float(getter())

    def get_quote(self, symbol: str) -> Optional[float]:
        price = self._broker.get_current_price(symbol)
        return float(price) if price is not None else None

    def get_positions(self) -> list[Position]:
        return list(self._broker.get_positions())

    def get_position(self, symbol: str) -> Optional[Position]:
        for pos in self.get_positions():
            if pos.symbol == symbol:
                return pos
        return None

    def buy_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id  # not accepted by the underlying broker layer; kept for interface parity
        result = self._broker.buy(symbol, symbol, int(qty), 0, order_type="market")
        return _to_order_result(result, symbol, "BUY", int(qty))

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        result = self._broker.sell(symbol, symbol, int(qty), 0, order_type="market")
        return _to_order_result(result, symbol, "SELL", int(qty))

    def wait_for_execution(self, order_id: str, timeout: float = 10.0) -> BrokerOrderResult:
        """buy_market/sell_market on this broker layer already confirm synchronously
        (a single HTTP round-trip) — there is no separate async order-status
        endpoint wired here. This method exists for interface completeness (a
        future real-async KIS path) and simply reports that no pending order
        tracking is needed at this layer.
        """
        del timeout
        raise NotImplementedError(
            "wait_for_execution: this broker layer confirms synchronously inside "
            "buy_market/sell_market — call reconcile_position(symbol) to verify "
            f"the resulting holdings instead (order_id={order_id!r})."
        )

    def reconcile_position(self, symbol: str) -> int:
        """Actual held quantity for ``symbol``, re-queried fresh from the broker
        (no cache). Retry/backoff for "did the sell actually clear to 0 yet"
        belongs to order_executor.py, not this thin adapter.
        """
        pos = self.get_position(symbol)
        return int(pos.quantity) if pos else 0

    def is_market_open(self) -> bool:
        # The shared broker layer has no explicit market-clock API; MACD2's own
        # session-time gating (docs §10/§11, 09:00-15:30 KST) is evaluated by
        # worker.py against wall-clock KST, not by asking the broker.
        return True


class MockBrokerAdapter(_BrokerAdapterBase):
    mode = "mock"

    def __init__(self, broker: Optional[BrokerBase] = None) -> None:
        if broker is None:
            from app.trading.broker_factory import create_broker

            broker = create_broker(mode="mock")
        self._broker = broker


class RealBrokerAdapter(_BrokerAdapterBase):
    """REAL-mode adapter. All REAL safety gates run inside broker_factory.create_broker
    -> KisRealBroker.__init__ at construction time (docs §14) — this adapter
    never duplicates or bypasses them.
    """

    mode = "real"

    def __init__(
        self,
        *,
        confirm_text: str,
        runtime_real_mode: bool = False,
        runtime_enable_real_buy: bool = False,
        runtime_enable_real_sell: bool = False,
        broker: Optional[BrokerBase] = None,
    ) -> None:
        if broker is None:
            from app.trading.broker_factory import create_broker

            broker = create_broker(
                mode="real",
                confirm_text=confirm_text,
                runtime_real_mode=runtime_real_mode,
                runtime_enable_real_buy=runtime_enable_real_buy,
                runtime_enable_real_sell=runtime_enable_real_sell,
            )
        self._broker = broker


def create_macd2_broker(mode: str, **kwargs: Any) -> _BrokerAdapterBase:
    """docs §9 factory: mode in {"mock", "real"}. REAL kwargs (confirm_text, ...)
    are required only for mode="real"; MOCK never receives or checks them.
    """
    if mode == "mock":
        return MockBrokerAdapter()
    if mode == "real":
        return RealBrokerAdapter(**kwargs)
    raise ValueError(f"create_macd2_broker: unknown mode {mode!r}")
