"""TSLA_AUTO order executor — idempotent per-signal_id execution (docs §13/§14).

Structure mirrors app/trading/macd2/order_executor.py (docs
TSLA_AUTO_COPY_MAP.md — COPY_WITH_US_MARKET_CHANGE): budget/cash-based USD
quantity sizing, the sell-then-confirm-then-reconcile-then-buy
direction-switch sequence, and duplicate-order prevention. Re-implemented
here independently — never imports app.trading.macd2.order_executor.

Writes a confirmed leg to the execution ledger only after both broker
execution success AND position reconciliation succeed — never before.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.trading.tsla_auto import config, ledger, market_session
from app.trading.tsla_auto.broker_adapter import BrokerOrderResult, BuySizingQuote
from app.trading.tsla_auto.cost_engine import OverseasTradeCostEngine
from app.trading.tsla_auto.models import Direction, PositionSnapshot, SignalState

ET = config.ET

BLOCK_DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL_BLOCKED"
BLOCK_ALREADY_HOLDING = "ALREADY_HOLDING_SAME_DIRECTION"
BLOCK_ORDER_DATA_INVALID = "ORDER_DATA_INVALID"
BLOCK_INSUFFICIENT_QTY = "INSUFFICIENT_QTY"
BLOCK_BUYABLE_QUERY_FAILED = "BUYABLE_QUERY_FAILED"
BLOCK_ASK_QUOTE_FAILED = "ASK_QUOTE_FAILED"
BLOCK_ASK_QUOTE_STALE = "ASK_QUOTE_STALE"
BLOCK_NOT_TRADABLE_DIRECTION = "NOT_A_TRADABLE_DIRECTION"
FAIL_SELL = "SELL_FAILED"
FAIL_SELL_NOT_CONFIRMED = "SELL_NOT_CONFIRMED_QTY_NONZERO"
FAIL_BUY = "BUY_FAILED"
FAIL_BUY_NOT_CONFIRMED = "BUY_NOT_CONFIRMED_QTY_ZERO"
ORDER_REJECTED = "ORDER_REJECTED"
NO_ORDER_ID = "NO_ORDER_ID"
FILL_TIMEOUT_CANCELLED = "FILL_TIMEOUT_CANCELLED"
CANCEL_FAILED = "CANCEL_FAILED"
BALANCE_MISMATCH = "BALANCE_MISMATCH"
STRATEGY_OWNERSHIP_MISMATCH = "STRATEGY_OWNERSHIP_MISMATCH"
ORDER_BLOCKED_BY_MARKET_PHASE = "ORDER_BLOCKED_BY_MARKET_PHASE"
BUY_FILL_POLL_MAX_SEC = config.ORDER_FILL_POLL_MAX_SEC
BUY_FILL_POLL_INTERVAL_SEC = config.ORDER_FILL_POLL_INTERVAL_SEC


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
    available_usd: Optional[float] = None
    usable_usd: Optional[float] = None
    bid1: Optional[float] = None
    ask1: Optional[float] = None
    order_price: Optional[float] = None
    budget_qty: Optional[int] = None
    available_qty: Optional[int] = None
    final_qty: Optional[int] = None
    expected_notional_usd: Optional[float] = None
    expected_fee_usd: Optional[float] = None
    broker_called: bool = False
    order_failure_stage: Optional[str] = None
    filled_qty: Optional[int] = None
    fill_poll_result: Optional[str] = None
    balance_qty: Optional[int] = None
    unfilled_qty: Optional[int] = None
    cancel_called: bool = False
    cancel_result: Optional[str] = None
    rt_cd: Optional[str] = None
    msg_cd: Optional[str] = None
    msg1: Optional[str] = None


def target_symbol_for_direction(direction: Direction) -> Optional[str]:
    if direction == Direction.UP_RED:
        return config.LONG_SYMBOL
    if direction == Direction.DOWN_BLUE:
        return config.INVERSE_SYMBOL
    return None


def compute_limit_buy_quantity(
    *, ui_budget_usd: float, available_usd: float, order_price: float, available_qty: int,
    usage_ratio: float = config.ORDER_USAGE_RATIO,
) -> tuple[float, int, int, float]:
    """docs §13: usable_usd = min(UI 예산, 실제 가능금액); budget_qty =
    floor(usable_usd*usage_ratio/order_price); final_qty = min(budget_qty,
    available_qty). 소수점 주식 거래 금지(정수 수량만)."""
    if order_price <= 0:
        return 0.0, 0, 0, 0.0
    usable_usd = min(float(ui_budget_usd or 0.0), float(available_usd or 0.0))
    max_notional = usable_usd * float(usage_ratio)
    budget_qty = max(int(max_notional // float(order_price)), 0)
    final_qty = min(budget_qty, max(int(available_qty or 0), 0))
    expected_notional = round(float(order_price) * final_qty, 4)
    if final_qty > 0 and expected_notional > max_notional:
        final_qty = max(final_qty - 1, 0)
        expected_notional = round(float(order_price) * final_qty, 4)
    return usable_usd, budget_qty, final_qty, expected_notional


def _now_iso() -> str:
    return datetime.now(ET).isoformat()


def _record_leg(
    *, broker_mode: str, signal_id: str, symbol: str, side: str, qty: int, price: float,
    position_before: int, position_after: int, exit_reason: str, order_result: BrokerOrderResult,
    entry_price: float, confirmed_at: str, requested_qty: Optional[int] = None,
    requested_price: Optional[float] = None,
) -> None:
    cost_engine = OverseasTradeCostEngine()
    requested = float(requested_price) if requested_price is not None else float(price)
    if side == "SELL":
        cost = cost_engine.compute_net_pnl_usd(
            entry_price, price, qty, buy_order_type="limit", sell_order_type="limit",
            exit_requested_price=requested,
        )
        gross_pnl = cost["gross_pnl_usd"]
        buy_fee = cost["buy_cost_usd"]["fee_usd"]
        sell_fee = cost["sell_cost_usd"]["fee_usd"]
        slippage = cost["slippage_usd"]
        fx_cost = cost["buy_cost_usd"]["fx_cost_usd"] + cost["sell_cost_usd"]["fx_cost_usd"]
        sec_fee = cost["sell_cost_usd"]["sec_fee_usd"]
        finra_taf = cost["sell_cost_usd"]["finra_taf_usd"]
        total_cost = cost["total_cost_usd"]
        fee, net_pnl = total_cost, cost["net_pnl_usd"]
    else:
        cost = cost_engine.compute_trade_cost_usd("BUY", price, qty, order_type="limit")
        buy_fee = cost["fee_usd"]
        sell_fee = 0.0
        slippage = round(cost_engine.compute_slippage_usd(
            requested_price=requested, executed_price=price, quantity=qty, order_type="limit",
        ), 4)
        fx_cost = cost["fx_cost_usd"]
        sec_fee = 0.0
        finra_taf = 0.0
        total_cost = round(float(cost["total_cost_usd"]) + float(slippage), 4)
        gross_pnl, fee, net_pnl = 0.0, total_cost, 0.0

    ledger.append_execution({
        "order_id": order_result.order_id, "signal_id": signal_id, "timestamp": confirmed_at,
        "mode": broker_mode, "symbol": symbol, "side": side,
        "requested_qty": requested_qty if requested_qty is not None else qty, "executed_qty": qty,
        "requested_price": requested, "executed_price": price,
        "position_before": position_before, "position_after": position_after,
        "gross_pnl_usd": gross_pnl, "buy_fee_usd": buy_fee, "sell_fee_usd": sell_fee,
        "slippage_usd": slippage, "fx_cost_usd": round(float(fx_cost), 4),
        "sec_fee_usd": sec_fee, "finra_taf_usd": finra_taf,
        "total_cost_usd": total_cost, "fee_usd": fee, "net_pnl_usd": net_pnl,
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


def _cancel_unfilled(broker, outcome: ExecutionOutcome, order_id: str, symbol: str) -> None:
    cancel_fn = getattr(broker, "cancel_order", None)
    outcome.cancel_called = True
    if cancel_fn is None:
        outcome.cancel_result = CANCEL_FAILED
        return
    try:
        cancel_res = cancel_fn(order_id, symbol)
    except TypeError:
        cancel_res = cancel_fn(order_id)
    except Exception as exc:
        outcome.cancel_result = f"{CANCEL_FAILED}:{exc}"
        return
    ok = bool(getattr(cancel_res, "success", False))
    outcome.cancel_result = "OK" if ok else CANCEL_FAILED


def _reconcile_buy_fill(broker, symbol: str, *, retries: int, delay_sec: float) -> tuple[int, float, str, int]:
    """주문 접수 성공 != 체결 성공 — 매 시도마다 실제 잔고를 다시 조회한다."""
    last_qty = 0
    for attempt in range(max(1, retries)):
        try:
            pos = broker.get_position(symbol)
        except Exception as exc:
            return 0, 0.0, f"ERROR:{exc}", last_qty
        qty = int(getattr(pos, "quantity", 0)) if pos else 0
        last_qty = qty
        if qty > 0:
            return qty, float(getattr(pos, "avg_price", 0.0)), "FILLED" if attempt == 0 else f"FILLED_AFTER_{attempt + 1}", qty
        if attempt < retries - 1:
            time.sleep(delay_sec)
    return 0, 0.0, "TIMEOUT", last_qty


def execute_signal(
    *, broker, direction: Direction, signal_id: str, quotes: dict[str, float],
    position: Optional[PositionSnapshot], budget_usd: float,
    processed_signal_ids: frozenset[str] = frozenset(),
    strategy_owned_qty: Optional[int] = None,
    reconcile_retries: int = 5, reconcile_delay_sec: float = 0.5,
    market_state: Optional[market_session.USMarketSessionState] = None,
) -> ExecutionOutcome:
    """Idempotent signal_id execution: entry (flat) or direction switch.

    Never places a BUY before a required SELL has been confirmed AND the
    resulting holdings reconcile to 0 (docs §14). Never re-executes a
    signal_id already in ``processed_signal_ids``, and never adds to an
    already-held same-direction position.
    """
    timestamps: dict[str, str] = {"evaluated_at": _now_iso()}
    target_symbol = target_symbol_for_direction(direction)
    market_state = market_state or market_session.get_us_market_state(datetime.now(ET))

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
    if target_symbol in config.FORBIDDEN_SYMBOLS or target_symbol not in config.TRADE_SYMBOLS:
        # docs §3/§4: 잘못된 상승 ETF 코드(TSLT 등)나 TSLA_AUTO 미허용 심볼이
        # 주문 대상으로 전달되면 즉시 차단한다 — broker를 절대 호출하지 않는다.
        return ExecutionOutcome(
            signal_id, direction, target_symbol, SignalState.BLOCKED,
            block_reason=BLOCK_NOT_TRADABLE_DIRECTION, timestamps=timestamps,
        )

    held_symbol = position.symbol if position and position.quantity > 0 else None
    account_qty = int(position.quantity) if position and position.quantity > 0 else 0
    held_qty = int(strategy_owned_qty) if strategy_owned_qty is not None else account_qty
    if held_symbol is not None and (held_qty <= 0 or held_qty > account_qty):
        return ExecutionOutcome(
            signal_id, direction, target_symbol, SignalState.BLOCKED,
            block_reason=STRATEGY_OWNERSHIP_MISMATCH, timestamps=timestamps,
        )

    if held_symbol == target_symbol:
        return ExecutionOutcome(
            signal_id, direction, target_symbol, SignalState.BLOCKED,
            block_reason=BLOCK_ALREADY_HOLDING, timestamps=timestamps,
        )

    outcome = ExecutionOutcome(signal_id, direction, target_symbol, SignalState.DETECTED, timestamps=timestamps)

    if held_symbol is None and not market_state.entry_allowed:
        return ExecutionOutcome(
            signal_id, direction, target_symbol, SignalState.BLOCKED,
            block_reason=market_state.reason_code, timestamps=timestamps,
            order_failure_stage=ORDER_BLOCKED_BY_MARKET_PHASE,
        )

    if held_symbol is not None:
        timestamps["sell_requested_at"] = _now_iso()
        sell_result = broker.sell_market(held_symbol, held_qty, f"{signal_id}:SELL:{held_symbol}")
        outcome.sell_result = sell_result
        if not sell_result.success:
            outcome.final_state = SignalState.FAILED
            outcome.block_reason = FAIL_SELL
            return outcome
        timestamps["sell_confirmed_at"] = _now_iso()

        qty_after = _reconcile_to_zero(broker, held_symbol, retries=reconcile_retries, delay_sec=reconcile_delay_sec)
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
            confirmed_at=timestamps["sell_confirmed_at"], requested_price=sell_result.executed_price,
        )

    if not market_state.entry_allowed:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = market_state.reason_code
        outcome.order_failure_stage = ORDER_BLOCKED_BY_MARKET_PHASE
        return outcome

    quote_price = quotes.get(target_symbol)
    if quote_price is None or quote_price <= 0:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ORDER_DATA_INVALID
        return outcome

    ask_getter = getattr(broker, "get_fresh_ask1", None)
    if ask_getter is None:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ASK_QUOTE_FAILED
        outcome.order_failure_stage = BLOCK_ASK_QUOTE_FAILED
        return outcome
    try:
        ask_quote = dict(ask_getter(target_symbol) or {})
    except Exception as exc:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ASK_QUOTE_FAILED
        outcome.order_failure_stage = BLOCK_ASK_QUOTE_FAILED
        outcome.msg1 = repr(exc)
        return outcome
    ask1 = float(ask_quote.get("ask1") or 0.0)
    outcome.ask1 = ask1 if ask1 > 0 else None
    outcome.bid1 = ask_quote.get("bid1")
    if ask_quote.get("stale") or ask_quote.get("is_stale"):
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ASK_QUOTE_STALE
        outcome.order_failure_stage = BLOCK_ASK_QUOTE_STALE
        outcome.rt_cd = str(ask_quote.get("rt_cd") or "")
        outcome.msg_cd = str(ask_quote.get("msg_cd") or "")
        outcome.msg1 = str(ask_quote.get("msg1") or "stale ask1")
        return outcome
    if not ask_quote.get("ok", False) or ask1 <= 0:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ASK_QUOTE_FAILED
        outcome.order_failure_stage = BLOCK_ASK_QUOTE_FAILED
        outcome.rt_cd = str(ask_quote.get("rt_cd") or "")
        outcome.msg_cd = str(ask_quote.get("msg_cd") or "")
        outcome.msg1 = str(ask_quote.get("msg1") or "ask1 unavailable")
        return outcome
    # docs §14: fresh 매도1호가 기반 일반 지정가 — tick 단위 정규화는 미국 ETF의
    # 실제 최소 호가단위 확인 전까지 센트(0.01) 단위로 보수적으로 반올림한다
    # (KRX get_tick_size와 동등한 것을 임의로 만들지 않는다).
    order_price = round(ask1 + 0.01, 2)
    outcome.order_price = order_price

    sizing_getter = getattr(broker, "get_buy_sizing_quote", None)
    if sizing_getter is None:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_BUYABLE_QUERY_FAILED
        outcome.order_failure_stage = BLOCK_BUYABLE_QUERY_FAILED
        return outcome
    try:
        sizing_quote: BuySizingQuote = sizing_getter(target_symbol, price=order_price)
    except Exception as exc:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_BUYABLE_QUERY_FAILED
        outcome.order_failure_stage = BLOCK_BUYABLE_QUERY_FAILED
        outcome.msg1 = repr(exc)
        return outcome

    usable_usd, budget_qty, requested_qty, expected_notional = compute_limit_buy_quantity(
        ui_budget_usd=budget_usd, available_usd=sizing_quote.available_usd,
        order_price=order_price, available_qty=sizing_quote.available_qty,
    )
    outcome.available_usd = sizing_quote.available_usd
    outcome.usable_usd = usable_usd
    outcome.available_qty = sizing_quote.available_qty
    outcome.budget_qty = budget_qty
    outcome.final_qty = requested_qty
    outcome.quantity = requested_qty
    outcome.expected_notional_usd = expected_notional
    outcome.expected_fee_usd = round(
        expected_notional * OverseasTradeCostEngine().fee_rate("BUY"), 4,
    )
    outcome.rt_cd = sizing_quote.rt_cd
    outcome.msg_cd = sizing_quote.msg_cd
    outcome.msg1 = sizing_quote.msg1
    if sizing_quote.rt_cd not in ("", "0", None):
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_BUYABLE_QUERY_FAILED
        outcome.order_failure_stage = BLOCK_BUYABLE_QUERY_FAILED
        return outcome
    if sizing_quote.available_qty < 1 or requested_qty < 1:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_INSUFFICIENT_QTY
        outcome.order_failure_stage = BLOCK_INSUFFICIENT_QTY
        return outcome
    # docs §13: 수수료 반영한 예상 결제금액이 usable_usd(사용비율 반영 후)를
    # 넘지 않도록 재검증한다.
    if expected_notional > usable_usd * config.ORDER_USAGE_RATIO + 1e-6:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_INSUFFICIENT_QTY
        outcome.order_failure_stage = BLOCK_INSUFFICIENT_QTY
        return outcome

    timestamps["buy_requested_at"] = _now_iso()
    buy_limit = getattr(broker, "buy_limit", None)
    if buy_limit is None:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ORDER_DATA_INVALID
        outcome.order_failure_stage = BLOCK_ORDER_DATA_INVALID
        return outcome
    buy_result = buy_limit(target_symbol, requested_qty, order_price, f"{signal_id}:BUY:{target_symbol}")
    outcome.broker_called = True
    outcome.buy_result = buy_result
    if not buy_result.success:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_BUY
        outcome.order_failure_stage = ORDER_REJECTED
        outcome.filled_qty = 0
        outcome.unfilled_qty = requested_qty
        outcome.fill_poll_result = "NOT_POLLED_ORDER_REJECTED"
        return outcome
    if not buy_result.order_id:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_BUY
        outcome.order_failure_stage = NO_ORDER_ID
        outcome.filled_qty = 0
        outcome.unfilled_qty = requested_qty
        outcome.fill_poll_result = "NOT_POLLED_NO_ORDER_ID"
        return outcome
    timestamps["buy_confirmed_at"] = _now_iso()

    fill_retries = max(1, int(BUY_FILL_POLL_MAX_SEC / BUY_FILL_POLL_INTERVAL_SEC))
    fill_delay = BUY_FILL_POLL_INTERVAL_SEC
    if reconcile_retries < fill_retries or reconcile_delay_sec < BUY_FILL_POLL_INTERVAL_SEC:
        fill_retries = max(1, int(reconcile_retries))
        fill_delay = float(reconcile_delay_sec)
    filled_qty, filled_avg_price, fill_poll_result, balance_qty = _reconcile_buy_fill(
        broker, target_symbol, retries=fill_retries, delay_sec=fill_delay,
    )
    outcome.filled_qty = filled_qty
    outcome.fill_poll_result = fill_poll_result
    outcome.balance_qty = balance_qty
    outcome.unfilled_qty = max(requested_qty - filled_qty, 0)
    timestamps["buy_reconciled_at"] = _now_iso()
    if filled_qty <= 0:
        _cancel_unfilled(broker, outcome, buy_result.order_id, target_symbol)
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_BUY_NOT_CONFIRMED
        outcome.order_failure_stage = CANCEL_FAILED if outcome.cancel_result == CANCEL_FAILED else FILL_TIMEOUT_CANCELLED
        return outcome
    if filled_qty < requested_qty:
        _cancel_unfilled(broker, outcome, buy_result.order_id, target_symbol)
        outcome.order_failure_stage = CANCEL_FAILED if outcome.cancel_result == CANCEL_FAILED else BALANCE_MISMATCH

    outcome.quantity = filled_qty
    outcome.filled_avg_price = filled_avg_price
    _record_leg(
        broker_mode=broker.mode, signal_id=signal_id, symbol=target_symbol, side="BUY",
        qty=filled_qty, price=filled_avg_price or buy_result.executed_price or order_price,
        position_before=0, position_after=filled_qty, exit_reason="", order_result=buy_result,
        entry_price=filled_avg_price or buy_result.executed_price or order_price,
        confirmed_at=timestamps["buy_confirmed_at"], requested_qty=requested_qty, requested_price=order_price,
    )
    outcome.final_state = SignalState.EXECUTED
    return outcome


def execute_exit(
    *, broker, symbol: str, quantity: int, exit_reason: str, entry_price: float,
    strategy_owned_qty: Optional[int] = None,
    reconcile_retries: int = 5, reconcile_delay_sec: float = 0.5,
) -> ExecutionOutcome:
    """Sell-only exit (STOP_LOSS / PROFIT_LOCK / FORCED_LIQUIDATION) — no
    follow-up BUY. Confirms execution then reconciles to 0 before recording."""
    timestamps = {"sell_requested_at": _now_iso()}
    outcome = ExecutionOutcome("", Direction.HOLD, symbol, SignalState.DETECTED, timestamps=timestamps)
    if strategy_owned_qty is not None and int(quantity) != int(strategy_owned_qty):
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = STRATEGY_OWNERSHIP_MISMATCH
        return outcome

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
        confirmed_at=timestamps["sell_confirmed_at"], requested_price=sell_result.executed_price,
    )
    outcome.final_state = SignalState.EXECUTED
    outcome.block_reason = exit_reason
    return outcome
