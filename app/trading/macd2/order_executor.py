"""MACD2 order executor — idempotent per-signal_id execution (docs §8/§9/§11).

Combines budget/cash-based quantity sizing, the sell-then-confirm-then-
reconcile-then-buy direction-switch sequence, and duplicate-order
prevention. Reuses TradeCostEngine (generic shared trading infra, not
MACD-v1 domain code — see the 2026-07-23 code-reuse audit) for fee/net-PnL
calculation only. Never imports from app.trading.macd_hynix_* or
app.trading.macd_pipeline.*.

Writes a confirmed leg to the execution ledger only after both KIS execution
success AND position reconciliation succeed (docs §17) — never before.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.trading.macd2 import config, ledger
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import Direction, PositionSnapshot, SignalState
from app.trading.trading_cost_engine import TradeCostEngine
from app.utils.stock_utils import get_tick_size

KST = config.KST

BLOCK_DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL_BLOCKED"
BLOCK_ALREADY_HOLDING = "ALREADY_HOLDING_SAME_DIRECTION"
BLOCK_ORDER_DATA_INVALID = "ORDER_DATA_INVALID"
BLOCK_INSUFFICIENT_QTY = "INSUFFICIENT_QTY"
BLOCK_NOT_TRADABLE_DIRECTION = "NOT_A_TRADABLE_DIRECTION"
FAIL_SELL = "SELL_FAILED"
FAIL_SELL_NOT_CONFIRMED = "SELL_NOT_CONFIRMED_QTY_NONZERO"
FAIL_BUY = "BUY_FAILED"
FAIL_BUY_NOT_CONFIRMED = "BUY_NOT_CONFIRMED_QTY_ZERO"


@dataclass
class ExecutionOutcome:
    signal_id: str
    direction: Direction
    target_symbol: Optional[str]
    final_state: SignalState
    block_reason: Optional[str] = None
    sell_result: Optional[BrokerOrderResult] = None
    buy_result: Optional[BrokerOrderResult] = None
    sell_qty_after: Optional[int] = None
    quantity: int = 0
    filled_avg_price: Optional[float] = None
    timestamps: dict[str, str] = field(default_factory=dict)


def target_symbol_for_direction(direction: Direction) -> Optional[str]:
    if direction == Direction.UP_RED:
        return config.LONG_SYMBOL
    if direction == Direction.DOWN_BLUE:
        return config.INVERSE_SYMBOL
    return None


def compute_order_safety_margin_pct(price: float, symbol: str) -> float:
    """docs §9/§21: real fee + tick-size (호가단위) safety margin, as a percent
    of price — replaces the old fixed placeholder ratio.

    Two components, both already-real inputs used elsewhere in this codebase
    (nothing new invented here):
      - buy fee rate for this symbol from config.yaml trading_cost (via
        TradeCostEngine, the same engine used for net-PnL/ledger recording).
      - one KRX tick (app.utils.stock_utils.get_tick_size) expressed as a
        percent of price, covering the case where the ask ticks up by one
        increment between the quote used to size the order and the market
        order actually filling.
    """
    if price <= 0:
        return 0.0
    fee_rate_pct = TradeCostEngine().fee_rate(symbol, "BUY") * 100.0
    tick_pct = get_tick_size(price) / price * 100.0
    return fee_rate_pct + tick_pct


def compute_order_quantity(
    available_cash: float,
    budget: float,
    price: float,
    *,
    symbol: str = config.LONG_SYMBOL,
    safety_margin_pct: Optional[float] = None,
) -> int:
    """docs §9: min(budget, orderable cash), with a fee/price-move safety margin.

    ``safety_margin_pct`` defaults to the real fee+tick calculation
    (:func:`compute_order_safety_margin_pct`) — pass an explicit value only to
    override it (e.g. in tests exercising the sizing formula itself).
    """
    if price <= 0:
        return 0
    margin_pct = (
        safety_margin_pct if safety_margin_pct is not None
        else compute_order_safety_margin_pct(price, symbol)
    )
    usable = min(float(available_cash), float(budget)) * (1 - margin_pct / 100.0)
    return max(int(usable // price), 0)


def _now_iso() -> str:
    return datetime.now(KST).isoformat()


def _record_leg(
    *, broker_mode: str, signal_id: str, symbol: str, side: str, qty: int,
    price: float, position_before: int, position_after: int, exit_reason: str,
    order_result: BrokerOrderResult, entry_price: float, confirmed_at: str,
    requested_qty: Optional[int] = None,
) -> None:
    """``qty`` is the REAL (reconciled) quantity that changed hands — used for
    fee/PnL math and the ledger's own ``executed_qty`` column (never the
    order response's own ``executed_qty``, which the broker layer cannot
    distinguish from a requested-and-accepted qty on a partial fill).
    ``requested_qty`` (defaults to ``qty`` — true for every SELL/exit leg,
    which already reconciles to the exact held quantity) records the
    originally-requested BUY size separately so a partial fill stays visible
    in the ledger."""
    cost_engine = TradeCostEngine()
    if side == "SELL":
        cost = cost_engine.compute_net_pnl(
            symbol, entry_price, price, qty, buy_order_type="market", sell_order_type="market",
        )
        gross_pnl, fee, slippage, net_pnl = cost["gross_pnl"], cost["sell_fee"], cost["slippage"], cost["net_pnl"]
    else:
        cost = cost_engine.compute_trade_cost(symbol, "BUY", price, qty, order_type="market")
        gross_pnl, fee, slippage, net_pnl = 0.0, cost["fee"], 0.0, 0.0

    ledger.append_execution({
        "order_id": order_result.order_id, "signal_id": signal_id, "timestamp": confirmed_at,
        "mode": broker_mode, "symbol": symbol, "side": side,
        "requested_qty": requested_qty if requested_qty is not None else qty,
        "executed_qty": qty,
        "requested_price": price, "executed_price": price,
        "position_before": position_before, "position_after": position_after,
        "gross_pnl": gross_pnl, "fee": fee, "slippage": slippage, "net_pnl": net_pnl,
        "exit_reason": exit_reason, "broker_response": str(order_result.raw),
    })


def _reconcile_to_zero(broker, symbol: str, *, retries: int, delay_sec: float) -> int:
    qty_after = -1
    for attempt in range(max(1, retries)):
        qty_after = broker.reconcile_position(symbol)
        if qty_after == 0:
            return 0
        if attempt < retries - 1:
            time.sleep(delay_sec)
    return qty_after


def _reconcile_buy_fill(broker, symbol: str, *, retries: int, delay_sec: float) -> tuple[int, float]:
    """Real (qty, avg_price) actually held for ``symbol`` after a BUY order
    reported ``success=True`` — never trust order acceptance as fill success
    (docs: 주문 접수 성공 != 체결 성공). Re-queried fresh from the broker on
    every attempt (no cache), so a partial fill naturally reports a quantity
    below what was requested. Still 0 after all retries means the order was
    accepted but nothing actually landed in the account.
    """
    for attempt in range(max(1, retries)):
        pos = broker.get_position(symbol)
        qty = int(pos.quantity) if pos else 0
        if qty > 0:
            return qty, float(pos.avg_price)
        if attempt < retries - 1:
            time.sleep(delay_sec)
    return 0, 0.0


def execute_signal(
    *,
    broker,
    direction: Direction,
    signal_id: str,
    quotes: dict[str, float],
    position: Optional[PositionSnapshot],
    budget: float,
    processed_signal_ids: frozenset[str] = frozenset(),
    reconcile_retries: int = 5,
    reconcile_delay_sec: float = 0.5,
) -> ExecutionOutcome:
    """Idempotent signal_id execution: entry (flat) or direction switch.

    Never places a BUY before a required SELL has been confirmed AND the
    resulting holdings reconcile to 0 (docs §8). Never re-executes a
    signal_id already in ``processed_signal_ids`` (docs §6/§11), and never
    adds to an already-held same-direction position (docs §8).
    """
    timestamps: dict[str, str] = {"evaluated_at": _now_iso()}
    target_symbol = target_symbol_for_direction(direction)

    if signal_id in processed_signal_ids:
        return ExecutionOutcome(
            signal_id, direction, target_symbol, SignalState.BLOCKED,
            block_reason=BLOCK_DUPLICATE_SIGNAL, timestamps=timestamps,
        )
    if target_symbol is None:
        return ExecutionOutcome(
            signal_id, direction, None, SignalState.BLOCKED,
            block_reason=BLOCK_NOT_TRADABLE_DIRECTION, timestamps=timestamps,
        )

    held_symbol = position.symbol if position and position.quantity > 0 else None
    held_qty = int(position.quantity) if position and position.quantity > 0 else 0

    if held_symbol == target_symbol:
        return ExecutionOutcome(
            signal_id, direction, target_symbol, SignalState.BLOCKED,
            block_reason=BLOCK_ALREADY_HOLDING, timestamps=timestamps,
        )

    outcome = ExecutionOutcome(signal_id, direction, target_symbol, SignalState.DETECTED, timestamps=timestamps)

    if held_symbol is not None:
        timestamps["sell_requested_at"] = _now_iso()
        sell_result = broker.sell_market(held_symbol, held_qty, f"{signal_id}:SELL:{held_symbol}")
        outcome.sell_result = sell_result
        if not sell_result.success:
            outcome.final_state = SignalState.FAILED
            outcome.block_reason = FAIL_SELL
            return outcome
        timestamps["sell_confirmed_at"] = _now_iso()

        qty_after = _reconcile_to_zero(
            broker, held_symbol, retries=reconcile_retries, delay_sec=reconcile_delay_sec,
        )
        outcome.sell_qty_after = qty_after
        timestamps["sell_reconciled_at"] = _now_iso()
        if qty_after != 0:
            outcome.final_state = SignalState.FAILED
            outcome.block_reason = FAIL_SELL_NOT_CONFIRMED
            return outcome

        _record_leg(
            broker_mode=broker.mode, signal_id=signal_id, symbol=held_symbol, side="SELL",
            qty=held_qty, price=sell_result.executed_price or (position.avg_price if position else 0.0),
            position_before=held_qty, position_after=0, exit_reason=config.EXIT_OPPOSITE_SIGNAL,
            order_result=sell_result, entry_price=position.avg_price if position else 0.0,
            confirmed_at=timestamps["sell_confirmed_at"],
        )

    price = quotes.get(target_symbol)
    if price is None or price <= 0:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ORDER_DATA_INVALID
        return outcome

    cash = broker.get_orderable_cash(target_symbol)
    requested_qty = compute_order_quantity(cash, budget, price, symbol=target_symbol)
    outcome.quantity = requested_qty
    if requested_qty < 1:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_INSUFFICIENT_QTY
        return outcome

    timestamps["buy_requested_at"] = _now_iso()
    buy_result = broker.buy_market(target_symbol, requested_qty, f"{signal_id}:BUY:{target_symbol}")
    outcome.buy_result = buy_result
    if not buy_result.success:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_BUY
        return outcome
    timestamps["buy_confirmed_at"] = _now_iso()

    # Order acceptance is never treated as fill success (docs) — re-query the
    # real holding before recording anything. A partial fill reports a real
    # qty below requested_qty; nothing actually landed reports 0.
    filled_qty, filled_avg_price = _reconcile_buy_fill(
        broker, target_symbol, retries=reconcile_retries, delay_sec=reconcile_delay_sec,
    )
    timestamps["buy_reconciled_at"] = _now_iso()
    if filled_qty <= 0:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_BUY_NOT_CONFIRMED
        return outcome

    outcome.quantity = filled_qty
    outcome.filled_avg_price = filled_avg_price
    _record_leg(
        broker_mode=broker.mode, signal_id=signal_id, symbol=target_symbol, side="BUY",
        qty=filled_qty, price=filled_avg_price or buy_result.executed_price or price,
        position_before=0, position_after=filled_qty,
        exit_reason="", order_result=buy_result,
        entry_price=filled_avg_price or buy_result.executed_price or price,
        confirmed_at=timestamps["buy_confirmed_at"], requested_qty=requested_qty,
    )
    outcome.final_state = SignalState.EXECUTED
    return outcome


def execute_exit(
    *,
    broker,
    symbol: str,
    quantity: int,
    exit_reason: str,
    entry_price: float,
    reconcile_retries: int = 5,
    reconcile_delay_sec: float = 0.5,
) -> ExecutionOutcome:
    """Sell-only exit (STOP_LOSS / PROFIT_LOCK / FORCED_LIQUIDATION) — no follow-up BUY.
    Confirms execution then reconciles the holding to 0 before recording it.
    """
    timestamps = {"sell_requested_at": _now_iso()}
    outcome = ExecutionOutcome("", Direction.HOLD, symbol, SignalState.DETECTED, timestamps=timestamps)

    client_order_id = f"EXIT:{exit_reason}:{symbol}:{timestamps['sell_requested_at']}"
    sell_result = broker.sell_market(symbol, quantity, client_order_id)
    outcome.sell_result = sell_result
    if not sell_result.success:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_SELL
        return outcome
    timestamps["sell_confirmed_at"] = _now_iso()

    qty_after = _reconcile_to_zero(broker, symbol, retries=reconcile_retries, delay_sec=reconcile_delay_sec)
    outcome.sell_qty_after = qty_after
    timestamps["sell_reconciled_at"] = _now_iso()
    if qty_after != 0:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_SELL_NOT_CONFIRMED
        return outcome

    _record_leg(
        broker_mode=broker.mode, signal_id="", symbol=symbol, side="SELL", qty=quantity,
        price=sell_result.executed_price or entry_price, position_before=quantity, position_after=0,
        exit_reason=exit_reason, order_result=sell_result, entry_price=entry_price,
        confirmed_at=timestamps["sell_confirmed_at"],
    )
    outcome.final_state = SignalState.EXECUTED
    outcome.block_reason = exit_reason
    return outcome
