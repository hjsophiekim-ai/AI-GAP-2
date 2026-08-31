"""MACD2 order executor — idempotent per-signal_id execution (docs §8/§9/§11).

Combines budget/cash-based quantity sizing, the sell-then-confirm-then-
reconcile-then-buy direction-switch sequence, and duplicate-order
prevention. Reuses TradeCostEngine (generic shared trading infra, not
MACD-v1 domain code — see the 2026-07-23 code-reuse audit) for fee/net-PnL
calculation only.

Writes a confirmed leg to the execution ledger only after both KIS execution
success AND position reconciliation succeed (docs §17) — never before.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.trading.macd2 import config, ledger
from app.trading.macd2.broker_adapter import BrokerOrderResult, BuySizingQuote
from app.trading.macd2.models import Direction, PositionSnapshot, SignalState
from app.trading.trading_cost_engine import TradeCostEngine
from app.utils.stock_utils import get_tick_size

KST = config.KST

BLOCK_DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL_BLOCKED"
BLOCK_ALREADY_HOLDING = "ALREADY_HOLDING_SAME_DIRECTION"
BLOCK_ORDER_DATA_INVALID = "ORDER_DATA_INVALID"
BLOCK_INSUFFICIENT_QTY = "INSUFFICIENT_QTY"
BLOCK_KIS_BUYABLE_QUERY_FAILED = "BUYABLE_QUERY_FAILED"
BLOCK_ASK_QUOTE_FAILED = "ASK_QUOTE_FAILED"
BLOCK_ASK_QUOTE_STALE = "ASK_QUOTE_STALE"
BLOCK_NRCVB_BUY_QTY_ZERO = "INSUFFICIENT_QTY"
BLOCK_NOT_TRADABLE_DIRECTION = "NOT_A_TRADABLE_DIRECTION"
FAIL_SELL = "SELL_FAILED"
FAIL_SELL_NOT_CONFIRMED = "SELL_NOT_CONFIRMED_QTY_NONZERO"
FAIL_BUY = "BUY_FAILED"
FAIL_BUY_NOT_CONFIRMED = "BUY_NOT_CONFIRMED_QTY_ZERO"
ORDER_REJECTED = "ORDER_REJECTED"
NO_ORDER_ID = "NO_ORDER_ID"
FILL_TIMEOUT = "FILL_TIMEOUT_CANCELLED"
FILL_TIMEOUT_CANCELLED = FILL_TIMEOUT
CANCEL_FAILED = "CANCEL_FAILED"
BALANCE_MISMATCH = "BALANCE_MISMATCH"
BUY_CANCEL_NOT_CONFIRMED = "BUY_CANCEL_NOT_CONFIRMED"
BUY_FILL_POLL_MAX_SEC = 10.0
BUY_FILL_POLL_INTERVAL_SEC = 1.0
# 2026-08-11 fix (real incident: a BUY filled ~10s after the fill-poll
# timeout, right around a cancel attempt, on a day KIS was already
# returning repeated 500s for this exact symbol) -- the 2026-08-10
# "never drop a real fill" recheck (see execute_signal/execute_exit below)
# originally used a single INSTANT recheck (retries=1, delay_sec=0.0)
# right after cancel/timeout. That is too thin against real KIS latency:
# a fill landing even one second late still fell through as unrecorded.
# Give the post-cancel/post-timeout recheck the same few-second window the
# primary poll gets, instead of a single instant snapshot.
POST_CANCEL_RECHECK_RETRIES = 3
POST_CANCEL_RECHECK_DELAY_SEC = 1.0
# 2026-08-11 fix (real incident: a confirmed REVERSAL never placed either
# leg -- no exit of the held position, no new entry -- because the
# get_fresh_ask1() call backing it was tried exactly once; a transient KIS
# failure permanently blocked that signal_id with no retry ANYWHERE
# downstream (unlike a stale-quote block, this one is never revisited by a
# pending-signal retry on a later tick)). Retry a genuine fetch failure the
# same few-second window every other KIS-backed retry in this file already
# gets -- a legitimately STALE ask1 (ask_quote.get("stale")) is a distinct,
# real signal and is NOT retried here.
ASK1_FETCH_RETRIES = 3
ASK1_FETCH_RETRY_DELAY_SEC = 1.0


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
    # Sizing diagnostics (docs 2026-07-27 주문가능금액 fix) — the actual
    # per-symbol orderable cash and quote price used to compute ``quantity``,
    # so the caller can display/verify requested_qty * sizing_price never
    # exceeds orderable_cash_at_sizing.
    orderable_cash_at_sizing: Optional[float] = None
    nrcvb_buy_amt: Optional[float] = None
    nrcvb_buy_qty: Optional[int] = None
    psbl_qty_calc_unpr: Optional[float] = None
    ask1: Optional[float] = None
    order_price: Optional[float] = None
    order_type: Optional[str] = None
    usable_cash: Optional[float] = None
    limit_buyable_qty: Optional[int] = None
    budget_qty: Optional[int] = None
    final_qty: Optional[int] = None
    sizing_rt_cd: Optional[str] = None
    sizing_msg_cd: Optional[str] = None
    sizing_msg1: Optional[str] = None
    sizing_price: Optional[float] = None
    expected_amount: Optional[float] = None
    broker_called: bool = False
    order_failure_stage: Optional[str] = None
    filled_qty: Optional[int] = None
    fill_poll_result: Optional[str] = None
    balance_qty: Optional[int] = None
    ord_dvsn: Optional[str] = None
    ask1_age: Optional[float] = None
    cancel_called: bool = False
    cancel_result: Optional[str] = None
    unfilled_qty: Optional[int] = None


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


def compute_final_order_quantity(
    *,
    budget: float,
    price: float,
    nrcvb_buy_qty: int,
    symbol: str = config.LONG_SYMBOL,
    safety_margin_pct: Optional[float] = None,
) -> tuple[int, int]:
    """Final BUY qty for live KIS: min(UI budget qty, KIS no-margin buyable qty),
    then subtract the same fee/tick safety margin from that quantity.

    KIS ``nrcvb_buy_qty`` is already the account/symbol/order-type answer from
    inquire-psbl-order, so we never recompute the final order only from cash.
    """
    if price <= 0:
        return 0, 0
    budget_qty = max(int(float(budget) // float(price)), 0)
    base_qty = min(budget_qty, max(int(nrcvb_buy_qty or 0), 0))
    margin_pct = (
        safety_margin_pct if safety_margin_pct is not None
        else compute_order_safety_margin_pct(price, symbol)
    )
    final_qty = max(int(base_qty * (1 - margin_pct / 100.0)), 0)
    return budget_qty, final_qty


def compute_limit_buy_quantity(
    *,
    ui_budget: float,
    orderable_cash: float,
    order_price: float,
    limit_buyable_qty: int,
) -> tuple[float, int, int, float]:
    if order_price <= 0:
        return 0.0, 0, 0, 0.0
    usable_cash = min(float(ui_budget or 0.0), float(orderable_cash or 0.0))
    max_notional = usable_cash * 0.995
    budget_qty = max(int(max_notional // float(order_price)), 0)
    final_qty = min(budget_qty, max(int(limit_buyable_qty or 0), 0))
    expected_amount = round(float(order_price) * final_qty, 2)
    if final_qty > 0 and expected_amount > max_notional:
        final_qty = max(final_qty - 1, 0)
        expected_amount = round(float(order_price) * final_qty, 2)
    return usable_cash, budget_qty, final_qty, expected_amount


# Backward-compatible alias (legacy name from IOC era).
compute_ioc_limit_buy_quantity = compute_limit_buy_quantity


def _now_iso() -> str:
    return datetime.now(KST).isoformat()


def _record_leg(
    *, broker_mode: str, signal_id: str, symbol: str, side: str, qty: int,
    price: float, position_before: int, position_after: int, exit_reason: str,
    order_result: BrokerOrderResult, entry_price: float, confirmed_at: str,
    requested_qty: Optional[int] = None, ledger_module: Any = None,
) -> None:
    """``qty`` is the REAL (reconciled) quantity that changed hands — used for
    fee/PnL math and the ledger's own ``executed_qty`` column (never the
    order response's own ``executed_qty``, which the broker layer cannot
    distinguish from a requested-and-accepted qty on a partial fill).
    ``requested_qty`` (defaults to ``qty`` — true for every SELL/exit leg,
    which already reconciles to the exact held quantity) records the
    originally-requested BUY size separately so a partial fill stays visible
    in the ledger.

    ``ledger_module`` (2026-08-13) — defaults to this module's own
    app.trading.macd2.ledger import, so every existing macd2 caller is
    byte-for-byte unaffected. A caller outside macd2 (e.g. mu_macd's worker,
    which reuses this generic executor) passes its OWN ledger module here so
    its executions land in its own execution ledger file instead of
    silently writing into macd2's."""
    lm = ledger_module if ledger_module is not None else ledger
    cost_engine = TradeCostEngine()
    if side == "SELL":
        cost = cost_engine.compute_net_pnl(
            symbol, entry_price, price, qty, buy_order_type="market", sell_order_type="market",
        )
        gross_pnl, fee, slippage, net_pnl = cost["gross_pnl"], cost["sell_fee"], cost["slippage"], cost["net_pnl"]
    else:
        cost = cost_engine.compute_trade_cost(symbol, "BUY", price, qty, order_type="market")
        gross_pnl, fee, slippage, net_pnl = 0.0, cost["fee"], 0.0, 0.0

    lm.append_execution({
        "order_id": order_result.order_id, "signal_id": signal_id, "timestamp": confirmed_at,
        "mode": broker_mode, "symbol": symbol, "side": side,
        "requested_qty": requested_qty if requested_qty is not None else qty,
        "executed_qty": qty,
        "requested_price": price, "executed_price": price,
        "position_before": position_before, "position_after": position_after,
        "gross_pnl": gross_pnl, "fee": fee, "slippage": slippage, "net_pnl": net_pnl,
        "exit_reason": exit_reason, "broker_response": str(order_result.raw),
    })


def _fallback_sell_price(broker, symbol: str) -> Optional[float]:
    """A market SELL's own order-submission response never carries a real
    fill price (KIS's order-cash endpoint only acknowledges the order; the
    actual 체결가 needs a separate fill/balance query, and our SELL requests
    always pass price=0 for a market order) — so ``BrokerOrderResult.
    executed_price`` is always 0/falsy for every SELL, which previously made
    ``_record_leg`` silently fall back to ``entry_price`` and record every
    single exit as if the price never moved (2026-08-04 real-money incident:
    a real ~-30만원 loss showed as -8,775원 in the ledger). Best-effort: ask
    the broker for a fresh quote right after the sell clears — far closer to
    the true fill than reusing the entry price, without needing a new KIS
    fill-history endpoint. Never raises — a quote failure here must not turn
    a successful, already-confirmed sell into a recording error."""
    try:
        price = broker.get_quote(symbol)
    except Exception:
        return None
    return float(price) if price and price > 0 else None


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
    """Real (qty, avg_price) actually held for ``symbol`` after a BUY order
    reported ``success=True`` — never trust order acceptance as fill success
    (docs: 주문 접수 성공 != 체결 성공). Re-queried fresh from the broker on
    every attempt (no cache), so a partial fill naturally reports a quantity
    below what was requested. Still 0 after all retries means the order was
    accepted but nothing actually landed in the account.
    """
    last_qty = 0
    for attempt in range(max(1, retries)):
        try:
            pos = broker.get_position(symbol)
        except Exception as exc:
            return 0, 0.0, f"ERROR:{exc}", last_qty
        qty = int(pos.quantity) if pos else 0
        last_qty = qty
        if qty > 0:
            return qty, float(pos.avg_price), "FILLED" if attempt == 0 else f"FILLED_AFTER_{attempt + 1}", qty
        if attempt < retries - 1:
            time.sleep(delay_sec)
    return 0, 0.0, "TIMEOUT", last_qty


def _reconcile_buy_fill_from_today_fills(broker, symbol: str, order_id: str) -> tuple[int, float, str]:
    fills_getter = getattr(broker, "get_today_fills", None)
    if fills_getter is None or not order_id:
        return 0, 0.0, "TODAY_FILLS_UNAVAILABLE"
    try:
        raw = fills_getter(symbol)
    except TypeError:
        raw = fills_getter()
    except Exception as exc:
        return 0, 0.0, f"TODAY_FILLS_ERROR:{exc}"
    if not isinstance(raw, dict) or not raw.get("ok"):
        return 0, 0.0, f"TODAY_FILLS_ERROR:{raw.get('error') if isinstance(raw, dict) else 'invalid_response'}"
    matching = [
        fill for fill in (raw.get("fills") or [])
        if str(fill.get("order_id") or "") == str(order_id)
        and str(fill.get("symbol") or "") == str(symbol)
        and str(fill.get("side") or "").upper() == "BUY"
    ]
    qty = sum(int(fill.get("quantity") or 0) for fill in matching)
    if qty <= 0:
        return 0, 0.0, "TODAY_FILLS_NO_MATCH"
    amount = sum(float(fill.get("price") or 0.0) * int(fill.get("quantity") or 0) for fill in matching)
    avg_price = amount / qty if qty > 0 else 0.0
    return qty, avg_price, "FILLED_FROM_TODAY_FILLS"


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
    ledger_module: Any = None,
) -> ExecutionOutcome:
    """Idempotent signal_id execution: entry (flat) or direction switch.

    Never places a BUY before a required SELL has been confirmed AND the
    resulting holdings reconcile to 0 (docs §8). Never re-executes a
    signal_id already in ``processed_signal_ids`` (docs §6/§11), and never
    adds to an already-held same-direction position (docs §8).
    """
    timestamps: dict[str, str] = {"evaluated_at": _now_iso()}
    target_symbol = target_symbol_for_direction(direction)
    lm = ledger_module if ledger_module is not None else ledger
    # Persistent-disk idempotency, in addition to the in-memory
    # processed_signal_ids check right below (docs 2026-08-26 incident: two
    # independent processes/RuntimeStates each had their OWN in-memory
    # processed_signal_ids, so that in-memory check alone never caught a
    # second one dispatching the same signal_id). getattr(...) so a
    # ledger_module without these functions (e.g. mu_macd's own ledger,
    # which reuses this executor but has no such incident to guard against
    # yet) is a byte-for-byte no-op, never an AttributeError.
    signal_leg_check = getattr(lm, "signal_id_has_leg", None)
    claim_dispatch = getattr(lm, "try_claim_signal_dispatch", None)
    release_claim = getattr(lm, "release_signal_dispatch_claim", None)

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
    ask_quote: dict[str, Any] = {}
    for retry_i in range(ASK1_FETCH_RETRIES):
        ask_quote = dict(ask_getter(target_symbol) or {})
        if ask_quote.get("stale") or ask_quote.get("is_stale"):
            break  # a real, distinct signal -- never retried here
        if ask_quote.get("ok", False) and float(ask_quote.get("ask1") or 0.0) > 0:
            break  # succeeded
        if retry_i < ASK1_FETCH_RETRIES - 1:
            time.sleep(ASK1_FETCH_RETRY_DELAY_SEC)
    ask1 = float(ask_quote.get("ask1") or 0.0)
    outcome.ask1 = ask1 if ask1 > 0 else None
    outcome.ask1_age = float(ask_quote["age_sec"]) if ask_quote.get("age_sec") is not None else None
    outcome.order_type = "limit"
    outcome.ord_dvsn = "00"
    if ask_quote.get("stale") or ask_quote.get("is_stale"):
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ASK_QUOTE_STALE
        outcome.order_failure_stage = BLOCK_ASK_QUOTE_STALE
        outcome.sizing_rt_cd = str(ask_quote.get("rt_cd") or "")
        outcome.sizing_msg_cd = str(ask_quote.get("msg_cd") or "")
        outcome.sizing_msg1 = str(ask_quote.get("msg1") or "stale ask1")
        return outcome
    if not ask_quote.get("ok", False) or ask1 <= 0:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ASK_QUOTE_FAILED
        outcome.order_failure_stage = BLOCK_ASK_QUOTE_FAILED
        outcome.sizing_rt_cd = str(ask_quote.get("rt_cd") or "")
        outcome.sizing_msg_cd = str(ask_quote.get("msg_cd") or "")
        outcome.sizing_msg1 = str(ask_quote.get("msg1") or "ask1 unavailable")
        return outcome
    order_price = float(int(ask1 + get_tick_size(ask1)))
    outcome.order_price = order_price

    if held_symbol is not None:
        if signal_leg_check is not None and signal_leg_check(signal_id, "SELL"):
            outcome.final_state = SignalState.BLOCKED
            outcome.block_reason = BLOCK_DUPLICATE_SIGNAL
            return outcome
        if claim_dispatch is not None and not claim_dispatch(signal_id, "SELL"):
            outcome.final_state = SignalState.BLOCKED
            outcome.block_reason = BLOCK_DUPLICATE_SIGNAL
            return outcome
        timestamps["sell_requested_at"] = _now_iso()
        sell_result = broker.sell_market(held_symbol, held_qty, f"{signal_id}:SELL:{held_symbol}")
        outcome.sell_result = sell_result
        if not sell_result.success:
            outcome.final_state = SignalState.FAILED
            outcome.block_reason = FAIL_SELL
            if release_claim is not None:
                release_claim(signal_id, "SELL")
            return outcome
        timestamps["sell_confirmed_at"] = _now_iso()

        qty_after = _reconcile_to_zero(
            broker, held_symbol, retries=reconcile_retries, delay_sec=reconcile_delay_sec,
        )
        outcome.sell_qty_after = qty_after
        if qty_after != 0:
            # 2026-08-13 fix (real incident: a MU_MACD reversal's SELL
            # cleared at the broker, but real KIS settlement latency meant
            # the primary reconcile window gave up before the position
            # actually hit zero -- the whole reversal aborted with NO
            # execution-ledger row for a sell that really happened, and the
            # follow-up BUY into the opposite ETF was never even attempted).
            # execute_exit already got this exact "give real settlement one
            # more window" recheck on 2026-08-10 (see
            # test_sell_settles_to_zero_on_recheck_after_slow_reconcile) --
            # this reversal-SELL leg never did. Same recheck now, here too.
            qty_after = _reconcile_to_zero(
                broker, held_symbol, retries=POST_CANCEL_RECHECK_RETRIES, delay_sec=POST_CANCEL_RECHECK_DELAY_SEC,
            )
            outcome.sell_qty_after = qty_after
        timestamps["sell_reconciled_at"] = _now_iso()
        if qty_after != 0:
            outcome.final_state = SignalState.FAILED
            outcome.block_reason = FAIL_SELL_NOT_CONFIRMED
            if release_claim is not None:
                release_claim(signal_id, "SELL")
            return outcome

        _record_leg(
            broker_mode=broker.mode, signal_id=signal_id, symbol=held_symbol, side="SELL",
            qty=held_qty,
            price=(
                sell_result.executed_price
                or _fallback_sell_price(broker, held_symbol)
                or (position.avg_price if position else 0.0)
            ),
            position_before=held_qty, position_after=0, exit_reason=config.EXIT_OPPOSITE_SIGNAL,
            order_result=sell_result, entry_price=position.avg_price if position else 0.0,
            confirmed_at=timestamps["sell_confirmed_at"], ledger_module=ledger_module,
        )

    sizing_getter = getattr(broker, "get_buy_sizing_quote", None)
    if sizing_getter is not None:
        sizing_quote = sizing_getter(target_symbol, price=order_price, order_type="limit")
    else:
        cash = broker.get_orderable_cash(target_symbol)
        fallback_qty = int(cash // order_price) if order_price > 0 else 0
        sizing_quote = BuySizingQuote(
            symbol=target_symbol, order_type="limit", ord_dvsn="00",
            orderable_cash=cash, nrcvb_buy_amt=cash, nrcvb_buy_qty=fallback_qty,
            psbl_qty_calc_unpr=order_price, psbl_qty=fallback_qty,
            rt_cd="", msg_cd="", msg1="", order_price=order_price,
            limit_buyable_qty=fallback_qty, raw={},
        )
    limit_buyable_qty = sizing_quote.limit_buyable_qty or sizing_quote.nrcvb_buy_qty
    usable_cash, budget_qty, requested_qty, expected_amount = compute_limit_buy_quantity(
        ui_budget=budget,
        orderable_cash=sizing_quote.orderable_cash,
        order_price=order_price,
        limit_buyable_qty=limit_buyable_qty,
    )
    outcome.quantity = requested_qty
    outcome.orderable_cash_at_sizing = sizing_quote.orderable_cash
    outcome.nrcvb_buy_amt = sizing_quote.nrcvb_buy_amt
    outcome.nrcvb_buy_qty = sizing_quote.nrcvb_buy_qty
    outcome.psbl_qty_calc_unpr = sizing_quote.psbl_qty_calc_unpr
    outcome.usable_cash = usable_cash
    outcome.limit_buyable_qty = limit_buyable_qty
    outcome.budget_qty = budget_qty
    outcome.final_qty = requested_qty
    outcome.sizing_rt_cd = sizing_quote.rt_cd
    outcome.sizing_msg_cd = sizing_quote.msg_cd
    outcome.sizing_msg1 = sizing_quote.msg1
    outcome.sizing_price = order_price
    outcome.expected_amount = expected_amount
    outcome.ord_dvsn = str(sizing_quote.ord_dvsn or "00")
    if sizing_quote.rt_cd not in ("", "0", None):
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_KIS_BUYABLE_QUERY_FAILED
        outcome.order_failure_stage = BLOCK_KIS_BUYABLE_QUERY_FAILED
        return outcome
    if limit_buyable_qty < 1:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_INSUFFICIENT_QTY
        outcome.order_failure_stage = BLOCK_INSUFFICIENT_QTY
        return outcome
    if requested_qty < 1:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_INSUFFICIENT_QTY
        outcome.order_failure_stage = BLOCK_INSUFFICIENT_QTY
        return outcome

    if signal_leg_check is not None and signal_leg_check(signal_id, "BUY"):
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_DUPLICATE_SIGNAL
        outcome.order_failure_stage = BLOCK_DUPLICATE_SIGNAL
        return outcome
    if claim_dispatch is not None and not claim_dispatch(signal_id, "BUY"):
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_DUPLICATE_SIGNAL
        outcome.order_failure_stage = BLOCK_DUPLICATE_SIGNAL
        return outcome
    timestamps["buy_requested_at"] = _now_iso()
    buy_limit = getattr(broker, "buy_limit", None)
    if buy_limit is None:
        outcome.final_state = SignalState.BLOCKED
        outcome.block_reason = BLOCK_ORDER_DATA_INVALID
        outcome.order_failure_stage = BLOCK_ORDER_DATA_INVALID
        if release_claim is not None:
            release_claim(signal_id, "BUY")
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
        if release_claim is not None:
            release_claim(signal_id, "BUY")
        outcome.balance_qty = None
        return outcome
    if not buy_result.order_id:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_BUY
        outcome.order_failure_stage = NO_ORDER_ID
        outcome.filled_qty = 0
        outcome.unfilled_qty = requested_qty
        outcome.fill_poll_result = "NOT_POLLED_NO_ORDER_ID"
        outcome.balance_qty = None
        return outcome
    timestamps["buy_confirmed_at"] = _now_iso()

    # Order acceptance != fill. Poll up to 10s @ 1s; tests may pass shorter reconcile_*.
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
    if filled_qty < requested_qty:
        _cancel_unfilled(broker, outcome, buy_result.order_id, target_symbol)
        # 2026-08-10/11 fix (real incident: every real fill must land in
        # the ledger) -- a "cancel succeeded" response never guarantees the
        # order actually stopped; it can still fill at the broker right
        # after (or even during) cancellation, and that fill can take a few
        # seconds to become visible under real KIS latency. Re-verify
        # against the broker over the same short window the primary poll
        # gets (not a single instant snapshot) before trusting the
        # pre-cancel snapshot -- otherwise a real fill silently vanishes:
        # no ledger row now, and no way to write an accurate one later
        # (reconcile_position_state can only adopt the mystery qty going
        # forward, with no BUY record at all, discovered only once/if it
        # gets stopped out).
        recheck_qty, recheck_avg_price, recheck_poll_result, recheck_balance = _reconcile_buy_fill(
            broker, target_symbol, retries=POST_CANCEL_RECHECK_RETRIES, delay_sec=POST_CANCEL_RECHECK_DELAY_SEC,
        )
        if recheck_qty != filled_qty:
            filled_qty, filled_avg_price = recheck_qty, recheck_avg_price
            outcome.filled_qty = filled_qty
            outcome.fill_poll_result = recheck_poll_result
            outcome.balance_qty = recheck_balance
            outcome.unfilled_qty = max(requested_qty - filled_qty, 0)
        if filled_qty <= 0:
            fill_qty_from_orders, fill_avg_from_orders, fill_status_from_orders = _reconcile_buy_fill_from_today_fills(
                broker, target_symbol, buy_result.order_id,
            )
            if fill_qty_from_orders > 0:
                filled_qty, filled_avg_price = fill_qty_from_orders, fill_avg_from_orders
                outcome.filled_qty = filled_qty
                outcome.fill_poll_result = fill_status_from_orders
                outcome.balance_qty = filled_qty
                outcome.unfilled_qty = max(requested_qty - filled_qty, 0)
        if filled_qty <= 0:
            cancel_confirmed = outcome.cancel_result == "OK"
            outcome.final_state = SignalState.FAILED if cancel_confirmed else SignalState.BLOCKED
            outcome.block_reason = FAIL_BUY_NOT_CONFIRMED if cancel_confirmed else BUY_CANCEL_NOT_CONFIRMED
            outcome.order_failure_stage = FILL_TIMEOUT_CANCELLED if cancel_confirmed else BUY_CANCEL_NOT_CONFIRMED
            if cancel_confirmed and release_claim is not None:
                release_claim(signal_id, "BUY")
            return outcome
        # Fell through: the order filled (fully or partially) despite the
        # cancel attempt -- record the TRUE quantity below instead of the
        # FAILED path above.
        if filled_qty < requested_qty:
            outcome.order_failure_stage = (
                CANCEL_FAILED if outcome.cancel_result == CANCEL_FAILED else BALANCE_MISMATCH
            )

    outcome.quantity = filled_qty
    outcome.filled_avg_price = filled_avg_price
    _record_leg(
        broker_mode=broker.mode, signal_id=signal_id, symbol=target_symbol, side="BUY",
        qty=filled_qty, price=filled_avg_price or buy_result.executed_price or order_price,
        position_before=0, position_after=filled_qty,
        exit_reason="", order_result=buy_result,
        entry_price=filled_avg_price or buy_result.executed_price or order_price,
        confirmed_at=timestamps["buy_confirmed_at"], requested_qty=requested_qty,
        ledger_module=ledger_module,
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
    ledger_module: Any = None,
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
    if qty_after != 0:
        # 2026-08-10/11 fix (same "every real fill must land in the
        # ledger" principle as the BUY path): a confirmed sell that hasn't
        # reconciled to zero yet may just be a slow broker-side settlement
        # (real KIS latency/500s observed directly on this exact symbol),
        # not a genuine failure. Give it the same few-second recheck window
        # as the BUY path (not a single instant snapshot) before giving up
        # -- if it settles to zero within that window, this is a completed
        # exit (this matters most for FORCED_LIQUIDATION at 15:00: without
        # this, a real fill lands but the ledger/state.position never
        # reflects it, so nothing catches the still-held position) and
        # must be recorded exactly like any other.
        qty_after = _reconcile_to_zero(broker, symbol, retries=POST_CANCEL_RECHECK_RETRIES, delay_sec=POST_CANCEL_RECHECK_DELAY_SEC)
        outcome.sell_qty_after = qty_after
    timestamps["sell_reconciled_at"] = _now_iso()
    if qty_after != 0:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_SELL_NOT_CONFIRMED
        return outcome

    _record_leg(
        broker_mode=broker.mode, signal_id="", symbol=symbol, side="SELL", qty=quantity,
        price=sell_result.executed_price or _fallback_sell_price(broker, symbol) or entry_price,
        position_before=quantity, position_after=0,
        exit_reason=exit_reason, order_result=sell_result, entry_price=entry_price,
        confirmed_at=timestamps["sell_confirmed_at"], ledger_module=ledger_module,
    )
    outcome.final_state = SignalState.EXECUTED
    outcome.block_reason = exit_reason
    return outcome


def _reconcile_to_target(broker, symbol: str, target_qty: int, *, retries: int, delay_sec: float) -> int:
    qty_after = -1
    for attempt in range(max(1, retries)):
        qty_after = broker.reconcile_position(symbol)
        if qty_after == target_qty:
            return qty_after
        if attempt < retries - 1:
            time.sleep(delay_sec)
    return qty_after


def execute_partial_exit(
    *,
    broker,
    symbol: str,
    sell_qty: int,
    remaining_qty: int,
    exit_reason: str,
    entry_price: float,
    reconcile_retries: int = 5,
    reconcile_delay_sec: float = 0.5,
    ledger_module: Any = None,
) -> ExecutionOutcome:
    """Sell-only PARTIAL exit — the time-window filter's TP1 ladder (docs
    §11) is the only caller. Sells exactly ``sell_qty`` and reconciles the
    holding down to ``remaining_qty`` (NOT necessarily 0), unlike
    ``execute_exit`` (always a full liquidation reconciled to exactly 0).

    Purely additive: does not modify ``execute_exit`` or any other exit
    path, so every existing full-exit caller (STOP_LOSS/PROFIT_LOCK/
    OPPOSITE_SIGNAL/FORCED_LIQUIDATION/QUICK_PROFIT) is byte-for-byte
    unaffected by this function's existence.
    """
    timestamps = {"sell_requested_at": _now_iso()}
    outcome = ExecutionOutcome("", Direction.HOLD, symbol, SignalState.DETECTED, timestamps=timestamps)
    if sell_qty < 1:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = BLOCK_ORDER_DATA_INVALID
        return outcome

    client_order_id = f"PARTIAL_EXIT:{exit_reason}:{symbol}:{timestamps['sell_requested_at']}"
    sell_result = broker.sell_market(symbol, sell_qty, client_order_id)
    outcome.sell_result = sell_result
    if not sell_result.success:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_SELL
        return outcome
    timestamps["sell_confirmed_at"] = _now_iso()

    qty_after = _reconcile_to_target(
        broker, symbol, remaining_qty, retries=reconcile_retries, delay_sec=reconcile_delay_sec,
    )
    outcome.sell_qty_after = qty_after
    timestamps["sell_reconciled_at"] = _now_iso()
    if qty_after != remaining_qty:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = FAIL_SELL_NOT_CONFIRMED
        return outcome

    _record_leg(
        broker_mode=broker.mode, signal_id="", symbol=symbol, side="SELL", qty=sell_qty,
        price=sell_result.executed_price or _fallback_sell_price(broker, symbol) or entry_price,
        position_before=sell_qty + remaining_qty, position_after=remaining_qty,
        exit_reason=exit_reason, order_result=sell_result, entry_price=entry_price,
        confirmed_at=timestamps["sell_confirmed_at"], ledger_module=ledger_module,
    )
    outcome.final_state = SignalState.EXECUTED
    outcome.block_reason = exit_reason
    outcome.quantity = sell_qty
    return outcome
