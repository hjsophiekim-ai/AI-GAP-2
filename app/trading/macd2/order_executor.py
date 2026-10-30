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

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.logger import logger
from app.trading.kis_realized import kis_reported_leg_cost
from app.trading.macd2 import config, ledger
from app.trading.macd2 import order_execution_v2 as exec_v2
from app.trading.macd2 import partial_fill_reprice as pf_reprice
from app.trading.macd2 import sell_fill_telemetry
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
# 2026-10-06: 단일종목 레버리지 매수인데 기본예탁금(3천만원) 미달 -- 사전점검으로
# 주문을 보내지 않았거나(BLOCKED) KIS 가 APBK3052 로 거절했다(FAILED). 같은 사유로 남긴다.
BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT = "LEVERAGE_DEPOSIT_INSUFFICIENT"
LEVERAGE_DEPOSIT_SRC_PRECHECK = "PRECHECK"
LEVERAGE_DEPOSIT_SRC_KIS_REJECT = "KIS_REJECT"
# 2026-09-29: 사용자 주문금액 한도(UI) 안에 1주도 못 넣을 때. FAILED 가 아니라
# KIS 호출 전 BLOCKED 다 — 한도 초과는 거절이 아니라 cap 대상이다.
BLOCK_SAFETY_LIMIT_NO_ROOM = "SAFETY_LIMIT_NO_ROOM"
SAFETY_CAP_PER_ORDER = "PER_ORDER"
SAFETY_CAP_DAILY = "DAILY"
FAIL_SELL = "SELL_FAILED"
FAIL_SELL_NOT_CONFIRMED = "SELL_NOT_CONFIRMED_QTY_NONZERO"
# 2026-10-02 실사고: 매도는 접수됐는데 체결 확인용 잔고조회가 전부 실패(KIS 초당한도
# EGW00215 등)해 보유수량을 모른다. '안 팔렸다'가 아니라 '모른다'이므로
# FAIL_SELL_NOT_CONFIRMED 와 구분한다 -- 반대 BUY 는 내지 않고(보유가 확인되지 않은
# 채 사면 양쪽을 동시에 들 수 있다) worker 가 다음 tick 에 잔고를 다시 맞춘 뒤
# 같은 신호를 재시도한다.
FAIL_SELL_RECONCILE_QUERY_ERROR = "SELL_RECONCILE_QUERY_ERROR"
#: 보유수량을 조회하지 못했을 때 reconcile 함수들이 돌려주는 값(정상 수량은 >= 0).
RECONCILE_QTY_UNKNOWN = -1
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
    # 2026-09-01: set to the swept quantity when execute_exit/execute_
    # partial_exit's own reconcile left a small residual that
    # _attempt_residual_cleanup then cleared in one extra market sell.
    residual_cleanup_qty: int = 0
    # 2026-09-29 safety 진단 — 사용자 주문금액 한도 cap 전/후 수량·금액과 사유.
    # safety_requested_* = 전략 사이징 결과(cap 전), safety_capped_* = 실제로
    # KIS 에 보낸 값(cap 후, cap 이 없으면 requested 와 같다). REAL 이 아닌
    # 브로커(사용자 한도 조회 미지원)는 전부 None/"" 로 남는다.
    safety_requested_qty: Optional[int] = None
    safety_requested_amount: Optional[float] = None
    safety_capped_qty: Optional[int] = None
    safety_capped_amount: Optional[float] = None
    safety_limit_type: str = ""
    safety_reason: str = ""
    broker_error_type: str = ""
    daily_ordered_released: Optional[float] = None
    # 2026-10-06 레버리지 기본예탁금 진단. source: PRECHECK / KIS_REJECT.
    # current = 판단에 쓴 금액(사전점검: ord_psbl_cash / KIS 거절: 메시지의 '현재').
    # ord_psbl_cash = 같은 순간 우리가 본 순수 현금 주문가능금액(기준 확정용 비교값).
    leverage_deposit_source: str = ""
    leverage_deposit_current: Optional[float] = None
    leverage_deposit_required: Optional[float] = None
    leverage_deposit_ord_psbl_cash: Optional[float] = None
    # APBK3052 순간의 현금 필드 원값(JSON): 매수가능 조회 output + 잔고조회 output2.
    leverage_deposit_cash_fields: str = ""
    # 2026-10-16 주문실행 계측/v2: 단계별 시각(exec_log), 재주문 횟수, 취소 미확인으로 살아 있을 수 있는 주문.
    exec_log: dict = field(default_factory=dict)
    reprice_count: int = 0
    open_order: Optional[dict] = None


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
        TradeCostEngine). This is the STRATEGY rate, deliberately -- sizing
        is a pre-trade decision, not a record of a finished trade, and the
        strategy rate is the conservative (larger) one, so it over-reserves
        cash rather than letting an order bounce. The ledger's realized cost
        uses TradeCostEngine.realized_leg_fee instead; the two are separate
        on purpose (see trading_cost_engine.py's module header).
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


def apply_safety_cap(
    *, requested_qty: int, order_price: float, room: Optional[dict[str, Any]],
) -> tuple[int, float, str, str]:
    """사용자 주문금액 한도 안으로 수량을 줄인다 (2026-09-29).

    ``room`` 은 broker.get_buy_safety_room() 결과 — ``per_order``(1회 한도)와
    ``daily_remaining``(오늘 남은 한도). 한도가 없으면 inf 다. 전략 사이징과
    KIS 매수가능 상한은 호출 전에 이미 ``requested_qty`` 에 반영돼 있다.

    반환 (qty, amount, limit_type, reason). 한도 안이면 limit_type 은 "".
    qty*price 가 한도 이하가 되는 최대 정수 수량이라 브로커 gate(초과 시 거절)
    를 통과한다. 0 이면 호출부가 BLOCKED 처리한다.
    """
    amount = round(float(order_price) * int(requested_qty), 2)
    if not room or order_price <= 0:
        return int(requested_qty), amount, "", ""
    caps = []
    for limit_type, key in ((SAFETY_CAP_PER_ORDER, "per_order"), (SAFETY_CAP_DAILY, "daily_remaining")):
        value = room.get(key)
        if value is not None:
            caps.append((limit_type, float(value)))
    if not caps:
        return int(requested_qty), amount, "", ""
    limit_type, cap = min(caps, key=lambda kv: kv[1])
    if amount <= cap:
        return int(requested_qty), amount, "", ""
    qty = max(int(cap // float(order_price)), 0)
    while qty > 0 and float(order_price) * qty > cap:
        qty -= 1
    capped_amount = round(float(order_price) * qty, 2)
    reason = (
        f"{limit_type} 사용자한도 {cap:,.0f}원 < 요청 {amount:,.0f}원 "
        f"-> {int(requested_qty)}주를 {qty}주({capped_amount:,.0f}원)로 축소"
    )
    return qty, capped_amount, limit_type, reason


def _release_cancelled_reservation(broker, outcome: ExecutionOutcome, unfilled_qty: int, order_price: float) -> None:
    """취소가 확인된 미체결 BUY 수량만큼 브로커 일일 누계를 되돌린다.
    (접수 시점에 요청금액 전액이 누계에 들어가므로, 안 되돌리면 체결 안 된
    주문이 그날 사용자 일일한도를 계속 잡아먹는다.)"""
    releaser = getattr(broker, "release_daily_ordered_amount", None)
    if releaser is None or unfilled_qty <= 0 or order_price <= 0:
        return
    amount = round(float(order_price) * int(unfilled_qty), 2)
    try:
        releaser(amount)
    except Exception:
        return
    outcome.daily_ordered_released = amount


def _now_iso() -> str:
    return datetime.now(KST).isoformat()


def _record_leg(
    *, broker_mode: str, signal_id: str, symbol: str, side: str, qty: int,
    price: float, position_before: int, position_after: int, exit_reason: str,
    order_result: BrokerOrderResult, entry_price: float, confirmed_at: str,
    requested_qty: Optional[int] = None, ledger_module: Any = None,
    source: Optional[str] = None, sell_telemetry: Optional[dict] = None,
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
    silently writing into macd2's.

    ``sell_telemetry`` (2026-10-30, SELL 실제 체결 계측 -- 기록 전용) -- SELL 레그의
    price_source / sell_*_at / fallback_quote_price. SELL 이면 주문번호를
    sell_fill_telemetry 대기열에 넣어 worker 루프가 나중에 실제 평균체결가로 갱신한다.
    가격·손익 계산 자체는 이 값과 무관하게 기존과 같다."""
    lm = ledger_module if ledger_module is not None else ledger
    cost_engine = TradeCostEngine()
    # 매수/매도 모두 같은 우선순위로 실현비용을 구한다: KIS 가 그 레그의 실제
    # 제비용을 알려주면 그 값, 아니면 realized_fee_rate (TradeCostEngine.
    # realized_leg_fee). 전략 판정용 요율(compute_trade_cost)은 원장 비용에
    # 쓰지 않는다 — 2026-09-18 수정 전까지 매수 레그만 그 요율에 남아 있었고,
    # 그래서 매도를 고친 뒤에도 원장 수수료가 계속 15원으로 찍혔다
    # (98,155 x 0.00015 = 14.72 -> 15원, KIS 실제 4원).
    kis_leg_cost = kis_reported_leg_cost(getattr(order_result, "raw", None))
    if side == "SELL":
        # 2026-09-17: 실제 체결가로 계산하는 실현손익에는 슬리피지를 빼지 않는다.
        # 슬리피지는 "주문가 대비 체결가가 얼마나 밀릴까"를 예측하는 값이라
        # 이미 체결된 가격에 다시 적용하면 이중 차감이다. 그날 인버스 17주
        # 실거래에서 이 이중 차감이 59.45원, 4배 과다 수수료가 23.9원이었고
        # 둘이 합쳐 원장 1,756원 vs KIS 1,717원 괴리를 만들었다.
        cost = cost_engine.compute_realized_pnl(
            symbol, buy_amount=float(entry_price) * qty, sell_amount=float(price) * qty,
            sell_fee_override=kis_leg_cost,
        )
        gross_pnl, fee, slippage, net_pnl = cost["gross_pnl"], cost["sell_fee"], 0.0, cost["net_pnl"]
    else:
        fee = cost_engine.realized_leg_fee(float(price) * qty, fee_override=kis_leg_cost)
        gross_pnl, slippage, net_pnl = 0.0, 0.0, 0.0

    lm.append_execution({
        "order_id": order_result.order_id, "signal_id": signal_id, "timestamp": confirmed_at,
        "mode": broker_mode, "symbol": symbol, "side": side,
        "requested_qty": requested_qty if requested_qty is not None else qty,
        "executed_qty": qty,
        "requested_price": price, "executed_price": price,
        "position_before": position_before, "position_after": position_after,
        "gross_pnl": gross_pnl, "fee": fee, "slippage": slippage, "net_pnl": net_pnl,
        "exit_reason": exit_reason, "broker_response": str(order_result.raw),
        "source": source or "",
        **(_sell_telemetry_columns(sell_telemetry, order_result, net_pnl, lm) if side == "SELL" else {}),
    })
    if side == "SELL" and _sell_lookup_wanted(sell_telemetry, order_result, lm):
        sell_fill_telemetry.register(
            order_id=order_result.order_id, symbol=symbol, qty=qty, entry_price=entry_price,
            raw=getattr(order_result, "raw", None), mode=broker_mode,
        )


def _sell_telemetry_columns(sell_telemetry: Optional[dict], order_result, net_pnl: float, lm) -> dict:
    """SELL 레그 계측 컬럼(기록 전용). 주문조회 대기열에 들어가는 행만 PENDING 이다."""
    t = dict(sell_telemetry or {})
    queued = _sell_lookup_wanted(sell_telemetry, order_result, lm)
    return {
        "price_source": t.get("price_source") or sell_fill_telemetry.PRICE_SOURCE_FALLBACK_QUOTE,
        "fallback_quote_price": t.get("fallback_quote_price", ""),
        "fallback_net_pnl": net_pnl,
        "sell_requested_at": t.get("sell_requested_at", ""),
        "sell_confirmed_at": t.get("sell_confirmed_at", ""),
        "sell_reconciled_at": t.get("sell_reconciled_at", ""),
        "kis_ord_tmd": sell_fill_telemetry.ord_tmd_of(getattr(order_result, "raw", None)),
        "fill_lookup_status": t.get("fill_lookup_status") or (sell_fill_telemetry.LOOKUP_PENDING if queued else ""),
    }


def _sell_lookup_wanted(sell_telemetry: Optional[dict], order_result, lm) -> bool:
    """실제 체결 조회 대기열 대상: macd2 원장 + 주문번호 있음 + 아직 실제 체결가가 아님."""
    source = str((sell_telemetry or {}).get("price_source") or "")
    return (lm is ledger and bool(order_result.order_id)
            and source != sell_fill_telemetry.PRICE_SOURCE_ACTUAL_FILL)


def _sell_price_with_source(sell_result, broker, symbol: str, last_resort: float) -> tuple[float, dict]:
    """기존 SELL 가격 우선순위 그대로(주문응답 체결가 > 매도 직후 현재가 > last_resort)
    + 어느 값이 쓰였는지(price_source)를 함께 돌려준다. 가격 값은 2026-10-30 이전과 동일."""
    if sell_result.executed_price:
        return sell_result.executed_price, {"price_source": sell_fill_telemetry.PRICE_SOURCE_ORDER_RESPONSE}
    quote = _fallback_sell_price(broker, symbol)
    if quote:
        return quote, {"price_source": sell_fill_telemetry.PRICE_SOURCE_FALLBACK_QUOTE, "fallback_quote_price": quote}
    return last_resort, {"price_source": sell_fill_telemetry.PRICE_SOURCE_FALLBACK_ENTRY}


def _sell_timestamps(timestamps: dict) -> dict:
    return {k: timestamps.get(k, "") for k in ("sell_requested_at", "sell_confirmed_at", "sell_reconciled_at")}


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


def _query_position_qty(broker, symbol: str) -> int:
    """보유수량 1회 조회. 조회 자체가 실패하면 예외 대신 ``RECONCILE_QTY_UNKNOWN``.

    2026-10-02 실사고: 실계좌 반전 매도 직후 이 조회가 KIS 초당한도(EGW00215)로
    RuntimeError 를 던졌고, 그 예외가 execute_signal 밖으로 튀어 **이미 나간 매도**의
    원장 기록과 반대 BUY 를 통째로 날렸다(T+3 후보는 RESOLVE_ERROR 로 소멸).
    주문이 나간 뒤의 확인 단계는 절대 예외로 끝나면 안 된다 -- '모른다'를 값으로
    돌려주고, 호출자가 재시도/보수적 처리를 고른다."""
    try:
        return int(broker.reconcile_position(symbol))
    except Exception as exc:
        logger.warning("[MACD2] 보유수량 확인 조회 실패 %s -- 재시도 대상: %s", symbol, exc)
        return RECONCILE_QTY_UNKNOWN


def _reconcile_to_zero(broker, symbol: str, *, retries: int, delay_sec: float) -> int:
    qty_after = RECONCILE_QTY_UNKNOWN
    for attempt in range(max(1, retries)):
        qty = _query_position_qty(broker, symbol)
        if qty == 0:
            return 0
        if qty != RECONCILE_QTY_UNKNOWN or qty_after == RECONCILE_QTY_UNKNOWN:
            # 마지막으로 '확인된' 수량을 유지한다 -- 조회 실패가 앞선 확인값을 덮지 않게.
            qty_after = qty
        if attempt < retries - 1:
            time.sleep(delay_sec)
    return qty_after


def _attempt_residual_cleanup(
    *, broker, symbol: str, residual_qty: int, target_qty: int, entry_price: float,
) -> Optional[dict]:
    """One-shot cleanup for a small leftover after an exit's own reconcile
    (2026-09-01 real incident: a 809-share leverage TP exit left 1 share
    held). Only ever called when ``0 < residual_qty <= config.
    RESIDUAL_CLEANUP_MAX_QTY`` — a larger leftover is still a genuine
    failure, unchanged. Immediately market-sells exactly ``residual_qty``
    and re-reconciles to ``target_qty`` (0 for a full exit, the partial
    exit's own remaining_qty otherwise). Never retried beyond this single
    attempt: if the sell itself fails, or the post-sell reconcile still
    doesn't land on ``target_qty``, this returns None and the caller's
    existing FAIL_SELL_NOT_CONFIRMED handling applies exactly as before —
    this function never turns a failure into a silently-accepted success.

    Does NOT record anything to the ledger itself (unlike a normal exit leg)
    — the caller records the MAIN leg first, then this residual leg second
    via its own separate ``_record_leg`` call (source=config.
    RESIDUAL_CLEANUP_SOURCE, exit_reason suffixed), so the raw ledger's row
    order matches the true real-world chronology (main sell, then the
    follow-up sweep moments later) — the raw ledger is append-only and never
    retroactively edited. The UI trade-history aggregation (app/ui/pages/
    11_MACD_자동매매2.py) merges this row into the same display group as the
    exit leg it follows, exactly like a RECONCILE_BACKFILL/BROKER_DIRECT
    continuation row, so it is never shown or counted as its own separate
    round-trip trade."""
    requested_at = _now_iso()
    client_order_id = f"RESIDUAL_CLEANUP:{symbol}:{requested_at}"
    sell_result = broker.sell_market(symbol, residual_qty, client_order_id)
    if not sell_result.success:
        return None
    confirmed_at = _now_iso()
    qty_after = _reconcile_to_target(
        broker, symbol, target_qty,
        retries=config.RESIDUAL_CLEANUP_RECONCILE_RETRIES,
        delay_sec=config.RESIDUAL_CLEANUP_RECONCILE_DELAY_SEC,
    )
    if qty_after != target_qty:
        return None
    price, telemetry = _sell_price_with_source(sell_result, broker, symbol, entry_price)
    telemetry.update(sell_requested_at=requested_at, sell_confirmed_at=confirmed_at, sell_reconciled_at=_now_iso())
    return {
        "qty": residual_qty, "price": price, "qty_after": qty_after,
        "sell_result": sell_result, "confirmed_at": confirmed_at, "telemetry": telemetry,
    }


def _cancel_unfilled(broker, outcome: ExecutionOutcome, order_id: str, symbol: str) -> None:
    cancel_fn = getattr(broker, "cancel_order", None)
    outcome.cancel_called = True
    outcome.exec_log.setdefault("cancel_requested_at", _now_iso())
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


def _leverage_deposit_applies(broker, symbol: Optional[str]) -> bool:
    """레버리지 기본예탁금 점검 대상인가 -- REAL 브로커의 레버리지 종목 매수만."""
    return bool(
        getattr(config, "LEVERAGE_DEPOSIT_PRECHECK_ENABLED", True)
        and str(getattr(broker, "mode", "")) == "real"
        and symbol in tuple(getattr(config, "LEVERAGE_DEPOSIT_SYMBOLS", ()) or ())
    )


def _quote_ord_psbl_cash(sizing_quote) -> Optional[float]:
    """매수가능 조회 응답의 순수 현금 주문가능금액. 없거나 읽을 수 없으면 None."""
    raw = dict(getattr(sizing_quote, "raw", None) or {})
    value = raw.get("ord_psbl_cash")
    if value in (None, ""):
        value = dict(raw.get("output") or {}).get("ord_psbl_cash")
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _leverage_deposit_cash_fields(broker, sizing_quote) -> str:
    """APBK3052 순간의 현금 필드 원값 -- 매수가능 조회 응답(이미 받아 둔 것) +
    잔고조회 현금 필드(이 거절에서만 1회 조회). 진단 기록 전용, 실패해도 예외 없음."""
    raw = dict(getattr(sizing_quote, "raw", None) or {})
    psbl = {k: v for k, v in dict(raw.get("output") or {}).items() if isinstance(v, (str, int, float))}
    for key in ("ord_psbl_cash", "nrcvb_buy_amt", "nrcvb_buy_qty", "psbl_qty", "psbl_qty_calc_unpr"):
        if raw.get(key) not in (None, ""):
            psbl.setdefault(key, raw.get(key))
    snap: dict[str, Any] = {"psbl_order": psbl}
    getter = getattr(broker, "get_cash_diagnostics", None)
    if getter is not None:
        try:
            snap["balance"] = dict(getter() or {})
        except Exception as exc:
            snap["balance"] = {"error": str(exc)}
    try:
        return json.dumps(snap, ensure_ascii=False, default=str)[:4000]
    except Exception:
        return ""


def _parse_kis_current_amount(text: str) -> Optional[float]:
    """KIS 메시지 '(현재:28983650원)' 에서 금액을 꺼낸다."""
    m = re.search(r"현재\s*[:：]\s*([0-9,]+)", str(text or ""))
    return float(m.group(1).replace(",", "")) if m else None


def _finish_v2_buy(outcome: ExecutionOutcome, res: dict, *, broker, signal_id: str, target_symbol: str,
                   timestamps: dict, release_claim, ledger_module) -> ExecutionOutcome:
    """주문실행 v2 결과를 outcome 으로 옮긴다(2026-10-16). 체결이 있으면 그 주문으로 BUY 레그 1개를 기록."""
    buy_result = res["buy_result"]
    outcome.buy_result = buy_result
    requested_qty = int(res["requested_qty"])
    outcome.order_price = float(res["order_price"])
    outcome.final_qty = requested_qty
    filled = int(res["filled_qty"] or 0)
    outcome.filled_qty = filled
    outcome.unfilled_qty = max(requested_qty - filled, 0)
    outcome.balance_qty = filled
    outcome.fill_poll_result = f"V2_{res['final']}"
    outcome.cancel_called = bool(outcome.exec_log.get("cancel_requested_at") or
                                 any(k.endswith("cancel_requested_at") for k in outcome.exec_log))
    outcome.cancel_result = ("OK" if res["final"] in (exec_v2.NO_FILL_CANCEL_CONFIRMED, exec_v2.PARTIAL_CANCEL_CONFIRMED,
                                                      exec_v2.REPRICE_ABORTED_SLIPPAGE, exec_v2.REPRICE_QUOTE_UNAVAILABLE,
                                                      exec_v2.REPRICE_ORDER_REJECTED)
                             else (CANCEL_FAILED if res["final"] == exec_v2.CANCEL_UNCONFIRMED else ""))
    timestamps["buy_reconciled_at"] = _now_iso()
    if filled <= 0:
        if res["final"] == exec_v2.CANCEL_UNCONFIRMED:
            # 절대 재주문하지 않았다. 살아 있을 수 있는 주문은 pending_order 추적으로(worker 가 등록).
            outcome.final_state = SignalState.BLOCKED
            outcome.block_reason = BUY_CANCEL_NOT_CONFIRMED
            outcome.order_failure_stage = BUY_CANCEL_NOT_CONFIRMED
            return outcome
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = (FAIL_BUY_NOT_CONFIRMED if res["final"] == exec_v2.NO_FILL_CANCEL_CONFIRMED
                                else res["final"])
        outcome.order_failure_stage = res["final"]
        for o in outcome.exec_log.get("orders", []):          # 취소 확정된 주문들의 일일 주문금액 예약 해제
            _release_cancelled_reservation(broker, outcome, int(o["qty"]), float(o["price"]))
        if release_claim is not None:
            release_claim(signal_id, "BUY")
        return outcome
    avg = float(res["avg_price"] or outcome.order_price)
    if res["final"] == exec_v2.CANCEL_UNCONFIRMED and res.get("open_order"):
        outcome.open_order = dict(res["open_order"])          # 일부 체결 + 잔량 취소 미확인 -> pending 추적
        outcome.order_failure_stage = CANCEL_FAILED
    elif filled < requested_qty:
        outcome.order_failure_stage = "PARTIAL_FILL_CANCEL_CONFIRMED"
    outcome.quantity = filled
    outcome.filled_avg_price = avg
    _record_leg(
        broker_mode=broker.mode, signal_id=signal_id, symbol=target_symbol, side="BUY",
        qty=filled, price=avg, position_before=0, position_after=filled, exit_reason="", order_result=buy_result,
        entry_price=avg, confirmed_at=timestamps["buy_confirmed_at"], requested_qty=requested_qty,
        ledger_module=ledger_module,
    )
    outcome.final_state = SignalState.EXECUTED
    return outcome


#: 부분체결 재호가 실행 시 쓰는 sleep / 파라미터 덮어쓰기 -- 테스트 전용 주입점(운영에서는 바꾸지 않는다).
PARTIAL_FILL_SLEEP = time.sleep
PARTIAL_FILL_PARAM_OVERRIDES: dict = {}

PF_ROUTE_ACTIVE = "ACTIVE"                  # 주문번호 조회 + 취소 확정 + 재호가
PF_ROUTE_SHADOW = "SHADOW_ONLY"             # 주문번호 조회 불가(모의) -- 취소 요청 + '재호가했다면' 기록만
PF_ROUTE_SAFE_FALLBACK = "SAFE_FALLBACK"    # 실계좌 당일 자동차단 / 상태조회 불가 -- 기존 경로
#: 재주문을 허용하는 연속매매 시간(KST). 마감 동시호가(15:20~)·장전에는 재호가하지 않는다. (adapter.is_market_open 은 항상 True)
PF_SESSION_START, PF_SESSION_END = "09:00", "15:20"
_pf_now = lambda: datetime.now(KST)  # noqa: E731 -- 테스트 주입점


def _pf_signal_valid(broker, symbol: str, held_at_handoff: int):
    """재주문 직전 확인: 연속매매 시간이고, 넘겨받을 때 보유하던 수량이 줄지 않았어야(그 사이 매도·청산 없음) 유효."""
    def _valid() -> bool:
        try:
            if not (PF_SESSION_START <= _pf_now().strftime("%H:%M") < PF_SESSION_END):
                return False
            fn = getattr(broker, "is_market_open", None)
            if fn is not None and not fn():
                return False
            pos = broker.get_position(symbol)
            return int(getattr(pos, "quantity", 0) or 0) >= int(held_at_handoff)
        except Exception:
            return False
    return _valid


def _elapsed_since(iso: Optional[str]) -> float:
    try:
        return max(0.0, (datetime.now(KST) - datetime.fromisoformat(str(iso))).total_seconds())
    except Exception:
        return 0.0


def _handle_partial_fill(outcome: ExecutionOutcome, *, broker, buy_result, signal_id: str, target_symbol: str,
                         requested_qty: int, order_price: float, balance_qty: int, balance_avg: float,
                         timestamps: dict, release_claim, ledger_module) -> Optional[ExecutionOutcome]:
    """기존 잔고 확인 뒤 잔량이 남았을 때(0 <= 체결 < 주문)만 부른다. None 이면 기존 경로를 그대로 이어 간다.

    ACTIVE       : 주문번호 조회 -> 잔량 취소 v3 -> 취소 확정 -> 잔량 재호가(partial_fill_reprice). 결과로 끝낸다.
    SHADOW_ONLY  : 주문번호 조회가 안 됨(KIS 모의) -> 잔량 취소 요청 1회 + '재호가했다면' 기록, 재주문 0. 기존 경로로.
    SAFE_FALLBACK: 실계좌 당일 차단 중 / 실계좌 상태조회 불가 -> 기존 경로로(차단 중이면 취소 v3 도 보내지 않는다)."""
    if bool(getattr(config, "PARTIAL_FILL_REPRICE_KILL", False)):
        return None
    allowed = getattr(broker, "cancel_v3_allowed", None)
    if allowed is None or getattr(broker, "get_order_status", None) is None:
        return None
    try:
        if not allowed():
            return None
    except Exception:
        return None
    mode = str(getattr(broker, "mode", "") or "")
    day = datetime.now(KST).strftime("%Y%m%d")
    tripped = pf_reprice.breaker_reason(day) if mode == "real" else None
    if tripped:
        outcome.exec_log["partial_fill_reprice"] = {"route": PF_ROUTE_SAFE_FALLBACK, "reason": tripped}
        return None
    params = pf_reprice.RepriceParams(**{"tp_pct": float(config.FIXED_EXIT_TP_PCT), **PARTIAL_FILL_PARAM_OVERRIDES})
    max_notional = (float(outcome.usable_cash or 0.0) * 0.995) or None
    rp = pf_reprice.PartialFillRepricer(
        broker, symbol=target_symbol, target_qty=int(requested_qty), order_price=float(order_price),
        first_result=buy_result, client_order_id=f"{signal_id}:BUY:{target_symbol}", params=params,
        sleep=PARTIAL_FILL_SLEEP, signal_valid=_pf_signal_valid(broker, target_symbol, int(balance_qty)),
        tick_fn=get_tick_size, max_notional=max_notional,
        initial_sleep=max(0.0, params.first_check_sec - _elapsed_since(timestamps.get("buy_requested_at"))),
    )
    try:
        r = rp.run()
    except Exception as exc:  # noqa: BLE001 -- 불변식 위반 등: 더 사지 않고, 살아 있을 수 있는 주문은 미확정으로 넘긴다
        logger.error("[MACD2][PARTIAL_FILL] 재호가 루프 예외 -- 추가주문 중단: %r", exc)
        if mode == "real":
            pf_reprice.trip_breaker(day, f"EXCEPTION:{type(exc).__name__}")
        live = rp.st.live_orders()
        r = pf_reprice.RepriceResult(
            final=pf_reprice.CANCEL_FAILED, state=rp.st,
            open_order=({k: live[-1][k] for k in ("order_id", "qty", "price", "filled", "amount")} if live else None))
    st = r.state
    if (r.final == pf_reprice.STATUS_UNAVAILABLE and len(st.orders) == 1
            and not any(e[1] == "CANCEL_REQUESTED" for e in st.events)):
        # 첫 주문의 상태조회부터 안 됨 -> 취소 확정을 볼 수 없으니 재주문 금지. 잔량 취소 요청만 1회 보내고 기존 경로로.
        route = PF_ROUTE_SHADOW if mode == "mock" else PF_ROUTE_SAFE_FALLBACK
        if mode == "real":
            pf_reprice.trip_breaker(day, "STATUS_UNAVAILABLE")
        try:
            cr = broker.cancel_order_v3(str(buy_result.order_id), target_symbol)
            cancel_req = {"accepted": bool(getattr(cr, "success", False)), "message": str(getattr(cr, "message", "")),
                          "status": (getattr(cr, "raw", None) or {}).get("status")}
        except Exception as exc:  # noqa: BLE001
            cancel_req = {"accepted": False, "message": f"CANCEL_V3_ERROR:{exc}"}
        shadow = pf_reprice.shadow_plan(
            broker, symbol=target_symbol, target_qty=int(requested_qty), filled_qty=int(balance_qty),
            filled_amount=float(balance_qty) * float(balance_avg or order_price), order_price=float(order_price),
            params=params, tick_fn=get_tick_size, max_notional=max_notional)
        outcome.exec_log["partial_fill_reprice"] = {"route": route, "final": r.final, "cancel_request": cancel_req,
                                                    "shadow": shadow, "events": [list(e) for e in st.events]}
        logger.warning("[MACD2][PARTIAL_FILL] %s %s 체결 %s/%s -- 주문번호 조회 불가, 재주문 없음. 취소요청 %s, "
                       "재호가했다면 %s주 @%s (%s)", route, target_symbol, balance_qty, requested_qty,
                       cancel_req.get("accepted"), shadow.get("qty"), shadow.get("price"), shadow.get("reason"))
        return None
    if mode == "real" and r.final in (pf_reprice.CANCEL_FAILED, pf_reprice.STATUS_UNAVAILABLE):
        pf_reprice.trip_breaker(day, r.final)
    outcome.reprice_count = int(st.REPRICE_COUNT)
    outcome.exec_log["partial_fill_reprice"] = {
        "route": PF_ROUTE_ACTIVE, "final": r.final, "target_qty": st.TARGET_QTY, "filled_qty": st.FILLED_QTY,
        "remaining_qty": st.REMAINING_QTY, "original_order_id": st.ORIGINAL_ORDER_ID,
        "cancel_confirmed": st.CANCEL_CONFIRMED, "reprice_count": st.REPRICE_COUNT,
        "balance_qty_at_handoff": int(balance_qty),
        "orders": [dict(o) for o in st.orders], "events": [list(e) for e in st.events],
    }
    # 취소가 확정된(terminal) 주문의 미체결분만 일일 주문누계에서 되돌린다. 살아 있을 수 있는 주문은 그대로.
    for o in st.orders:
        if o["terminal"] and int(o["filled"]) < int(o["qty"]):
            _release_cancelled_reservation(broker, outcome, int(o["qty"]) - int(o["filled"]), float(o["price"]))
    if r.final == pf_reprice.FILLED:
        final = exec_v2.FILLED
    elif r.final in (pf_reprice.CANCEL_FAILED, pf_reprice.STATUS_UNAVAILABLE):
        final = exec_v2.CANCEL_UNCONFIRMED
    else:
        final = exec_v2.PARTIAL_CANCEL_CONFIRMED if st.FILLED_QTY > 0 else exec_v2.NO_FILL_CANCEL_CONFIRMED
    outcome.exec_log["orders"] = []      # 위에서 이미 되돌렸다 -- _finish_v2_buy 가 다시 되돌리지 않게
    res = {
        "final": final, "buy_result": buy_result, "requested_qty": int(requested_qty),
        "order_price": float(order_price), "filled_qty": int(st.FILLED_QTY), "avg_price": float(st.avg_price),
        "amount": float(st.FILLED_AMOUNT), "reprice_count": int(st.REPRICE_COUNT), "open_order": None,
    }
    if r.open_order is not None:
        # pending 추적은 이 주문 하나만 본다 -- 다른 주문으로 이미 산 수량(held_offset)을 함께 넘긴다.
        res["open_order"] = {**r.open_order, "held_offset": int(st.FILLED_QTY) - int(r.open_order["filled"])}
    out = _finish_v2_buy(outcome, res, broker=broker, signal_id=signal_id, target_symbol=target_symbol,
                         timestamps=timestamps, release_claim=release_claim, ledger_module=ledger_module)
    if out.filled_qty == 0 and res["open_order"] is not None:
        out.open_order = dict(res["open_order"])      # 0주 + 취소 미확인: 살아 있을 수 있는 주문은 재호가 주문일 수 있다
    if r.final not in (pf_reprice.FILLED, pf_reprice.CANCEL_FAILED, pf_reprice.STATUS_UNAVAILABLE):
        out.exec_log["partial_fill_reason"] = r.final
    logger.warning("[MACD2][PARTIAL_FILL] ACTIVE %s 결과 %s 체결 %s/%s 재호가 %s회", target_symbol, r.final,
                   st.FILLED_QTY, requested_qty, st.REPRICE_COUNT)
    return out


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
    timestamps["ask_fetch_started_at"] = _now_iso()          # 2026-10-16 지연 계측(관측 전용)
    if config.EXEC_STALE_QUOTE_CHECK_ENABLED or config.EXEC_V2_ENABLED:
        # A: 데이터 나이 상한(EXEC_QUOTE_MAX_AGE_SEC) 안의 최신 호가만 쓴다. 못 얻으면 주문하지 않는다.
        _q, _why = exec_v2.fetch_fresh_ask(broker, target_symbol, outcome.exec_log)
        timestamps["ask_fetch_done_at"] = _now_iso()
        if _q is None:
            outcome.final_state = SignalState.BLOCKED
            outcome.block_reason = (_why if _why == exec_v2.BLOCK_QUOTE_TOO_OLD
                                    else BLOCK_ASK_QUOTE_STALE if _why == "BROKER_STALE" else BLOCK_ASK_QUOTE_FAILED)
            outcome.order_failure_stage = outcome.block_reason
            outcome.sizing_msg1 = (f"최신 호가 확보 실패({_why}) -- 주문하지 않음, "
                                   f"나이 {outcome.exec_log.get('quote_ages_ms')}ms, 상한 "
                                   f"{int(float(config.EXEC_QUOTE_MAX_AGE_SEC) * 1000)}ms")
            return outcome
        ask_quote = _q
    else:
        for retry_i in range(ASK1_FETCH_RETRIES):
            ask_quote = dict(ask_getter(target_symbol) or {})
            if ask_quote.get("stale") or ask_quote.get("is_stale"):
                break  # a real, distinct signal -- never retried here
            if ask_quote.get("ok", False) and float(ask_quote.get("ask1") or 0.0) > 0:
                break  # succeeded
            if retry_i < ASK1_FETCH_RETRIES - 1:
                time.sleep(ASK1_FETCH_RETRY_DELAY_SEC)
        timestamps["ask_fetch_done_at"] = _now_iso()
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
            # 조회 실패로 '모른다'와 확인된 '안 팔렸다'를 구분한다(2026-10-02).
            # 어느 쪽이든 반대 BUY 는 내지 않는다 -- 매도 체결이 확인되지 않았다.
            outcome.block_reason = (FAIL_SELL_RECONCILE_QUERY_ERROR
                                    if qty_after == RECONCILE_QTY_UNKNOWN
                                    else FAIL_SELL_NOT_CONFIRMED)
            if release_claim is not None:
                release_claim(signal_id, "SELL")
            return outcome

        sell_price, sell_telemetry = _sell_price_with_source(
            sell_result, broker, held_symbol, position.avg_price if position else 0.0,
        )
        _record_leg(
            broker_mode=broker.mode, signal_id=signal_id, symbol=held_symbol, side="SELL",
            qty=held_qty, price=sell_price,
            position_before=held_qty, position_after=0, exit_reason=config.EXIT_OPPOSITE_SIGNAL,
            order_result=sell_result, entry_price=position.avg_price if position else 0.0,
            confirmed_at=timestamps["sell_confirmed_at"], ledger_module=ledger_module,
            sell_telemetry={**sell_telemetry, **_sell_timestamps(timestamps)},
        )

    timestamps["sizing_started_at"] = _now_iso()
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
    timestamps["sizing_done_at"] = _now_iso()
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

    # 2026-09-29: 사용자 주문금액 한도(UI 에서 명시적으로 설정한 경우만) 안으로
    # KIS 호출 전에 수량을 줄인다. 넘는다고 FAILED 시키지 않는다(09:54 실사고).
    # 한도 조회를 지원하지 않는 브로커(MOCK 등)는 room=None 이라 무변경.
    room_getter = getattr(broker, "get_buy_safety_room", None)
    room = None
    if room_getter is not None:
        timestamps["safety_room_started_at"] = _now_iso()
        try:
            room = room_getter(target_symbol, order_price)
        except Exception as exc:  # 조회 실패 시 cap 없이 진행 — 브로커 gate 가 최종 방어
            outcome.safety_reason = f"SAFETY_ROOM_QUERY_FAILED:{exc}"
            room = None
        timestamps["safety_room_done_at"] = _now_iso()
    if room is not None:
        capped_qty, capped_amount, limit_type, cap_reason = apply_safety_cap(
            requested_qty=requested_qty, order_price=order_price, room=room,
        )
        outcome.safety_requested_qty = requested_qty
        outcome.safety_requested_amount = expected_amount
        outcome.safety_capped_qty = capped_qty
        outcome.safety_capped_amount = capped_amount
        outcome.safety_limit_type = limit_type
        outcome.safety_reason = cap_reason
        if capped_qty < 1:
            outcome.final_state = SignalState.BLOCKED
            outcome.block_reason = BLOCK_SAFETY_LIMIT_NO_ROOM
            outcome.order_failure_stage = BLOCK_SAFETY_LIMIT_NO_ROOM
            return outcome
        if capped_qty < requested_qty:
            requested_qty = capped_qty
            expected_amount = capped_amount
            outcome.quantity = requested_qty
            outcome.final_qty = requested_qty
            outcome.expected_amount = expected_amount

    # 2026-10-06: 단일종목 레버리지 기본예탁금 사전점검 (REAL + 레버리지 매수만).
    # 이미 받아 둔 매수가능 조회 응답을 쓰므로 KIS 호출이 늘지 않는다. 값이 없으면
    # 점검하지 않는다(모르면 막지 않는다 -- KIS 가 최종 판정, 거절 시 아래에서 기록).
    if _leverage_deposit_applies(broker, target_symbol):
        _lev_ord_psbl = _quote_ord_psbl_cash(sizing_quote)
        _lev_min = float(config.LEVERAGE_DEPOSIT_MIN_KRW)
        if _lev_ord_psbl is not None and _lev_ord_psbl < _lev_min:
            outcome.final_state = SignalState.BLOCKED
            outcome.block_reason = BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
            outcome.order_failure_stage = BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
            outcome.leverage_deposit_source = LEVERAGE_DEPOSIT_SRC_PRECHECK
            outcome.leverage_deposit_current = _lev_ord_psbl
            outcome.leverage_deposit_required = _lev_min
            outcome.leverage_deposit_ord_psbl_cash = _lev_ord_psbl
            outcome.sizing_msg1 = (f"레버리지 기본예탁금 부족 -- 주문하지 않음 "
                                   f"(현재 {_lev_ord_psbl:,.0f}원 < 필요 {_lev_min:,.0f}원)")
            logger.warning("[MACD2] %s BUY 차단 -- 레버리지 기본예탁금 부족 (ord_psbl_cash=%s < %s)",
                           target_symbol, f"{_lev_ord_psbl:,.0f}", f"{_lev_min:,.0f}")
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
    reference_ask = ask1
    if config.EXEC_STALE_QUOTE_CHECK_ENABLED or config.EXEC_V2_ENABLED:
        _recv = outcome.exec_log.get("quote_received_at")
        _age_now = (datetime.fromisoformat(_now_iso()) - datetime.fromisoformat(_recv)).total_seconds() if _recv else 1e9
        if _age_now > float(config.EXEC_QUOTE_MAX_AGE_SEC):
            # 매도 레그·사이징 동안 호가가 늙었다 -- 주문 직전에 다시 받는다(가격이 올랐으면 수량만 줄인다).
            _q2, _why2 = exec_v2.fetch_fresh_ask(broker, target_symbol, outcome.exec_log, prefix="pre_")
            if _q2 is None:
                outcome.final_state = SignalState.BLOCKED
                outcome.block_reason = _why2 if _why2 == exec_v2.BLOCK_QUOTE_TOO_OLD else BLOCK_ASK_QUOTE_FAILED
                outcome.order_failure_stage = outcome.block_reason
                outcome.sizing_msg1 = f"주문 직전 최신 호가 확보 실패({_why2}) -- 주문하지 않음"
                if release_claim is not None:
                    release_claim(signal_id, "BUY")
                return outcome
            _new_price = float(int(float(_q2["ask1"]) + get_tick_size(float(_q2["ask1"]))))
            if _new_price != order_price:
                outcome.exec_log["pre_order_price_changed"] = f"{order_price}->{_new_price}"
                order_price = _new_price
                outcome.order_price = order_price
                if usable_cash and order_price > 0:
                    requested_qty = min(requested_qty, int(float(usable_cash) // order_price))
                    outcome.quantity = outcome.final_qty = requested_qty
                if requested_qty < 1:
                    outcome.final_state = SignalState.BLOCKED
                    outcome.block_reason = BLOCK_INSUFFICIENT_QTY
                    outcome.order_failure_stage = BLOCK_INSUFFICIENT_QTY
                    if release_claim is not None:
                        release_claim(signal_id, "BUY")
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
        _buy_raw = dict(buy_result.raw or {})
        if str(_buy_raw.get("msg_cd") or "") == str(config.KIS_MSG_LEVERAGE_DEPOSIT):
            # 2026-10-06: KIS 가 레버리지 기본예탁금 부족으로 거절 -- 같은 사유로 남기고,
            # KIS 의 '현재' 금액과 우리가 본 ord_psbl_cash 를 나란히 기록한다(기준 확정용).
            outcome.block_reason = BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
            outcome.leverage_deposit_source = LEVERAGE_DEPOSIT_SRC_KIS_REJECT
            outcome.leverage_deposit_current = _parse_kis_current_amount(
                str(_buy_raw.get("msg1") or buy_result.message or ""))
            outcome.leverage_deposit_required = float(config.LEVERAGE_DEPOSIT_MIN_KRW)
            outcome.leverage_deposit_ord_psbl_cash = _quote_ord_psbl_cash(sizing_quote)
            outcome.leverage_deposit_cash_fields = _leverage_deposit_cash_fields(broker, sizing_quote)
            logger.warning("[MACD2] %s BUY KIS 거절 APBK3052 -- 기본예탁금 현재 %s / 우리 ord_psbl_cash %s "
                           "/ 주문가능 %s / 현금필드 %s",
                           target_symbol, outcome.leverage_deposit_current,
                           outcome.leverage_deposit_ord_psbl_cash, outcome.orderable_cash_at_sizing,
                           outcome.leverage_deposit_cash_fields)
        outcome.broker_error_type = str(getattr(buy_result, "error_type", "") or "")
        if outcome.broker_error_type.startswith("safety"):
            # 브로커 내부 gate 가 KIS 호출 전에 거절 — 원장에 사유를 남긴다.
            outcome.safety_limit_type = outcome.broker_error_type
            outcome.safety_reason = " | ".join(
                line.strip() for line in str(buy_result.message or "").splitlines() if line.strip()
            )
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

    v2res = None
    if config.EXEC_V2_ENABLED:
        v2res = exec_v2.run_buy(
            broker, symbol=target_symbol, qty=requested_qty, order_price=order_price, reference_ask=reference_ask,
            usable_cash=float(outcome.usable_cash or 0.0), client_order_id=f"{signal_id}:BUY:{target_symbol}",
            first_result=buy_result, log=outcome.exec_log, tick_fn=get_tick_size,
        )
        outcome.reprice_count = int(v2res.get("reprice_count") or 0)
        if v2res["final"] not in (exec_v2.STATUS_UNAVAILABLE, exec_v2.CANCEL_UNAVAILABLE):
            return _finish_v2_buy(outcome, v2res, broker=broker, signal_id=signal_id, target_symbol=target_symbol,
                                  timestamps=timestamps, release_claim=release_claim, ledger_module=ledger_module)

    if v2res is not None and v2res["final"] == exec_v2.CANCEL_UNAVAILABLE:
        # 주문번호 기준으로 이미 기다렸다 -- 같은 시간을 잔고 기준으로 또 기다리지 않는다. 취소는 기존 경로.
        filled_qty, filled_avg_price = int(v2res["filled_qty"]), float(v2res["avg_price"] or 0.0)
        fill_poll_result = f"ORDER_STATUS_{'PARTIAL' if filled_qty else 'OPEN'}"
        balance_qty = filled_qty
    else:
        # Order acceptance != fill. Poll up to 10s @ 1s; tests may pass shorter reconcile_*.
        fill_retries = max(1, int(BUY_FILL_POLL_MAX_SEC / BUY_FILL_POLL_INTERVAL_SEC))
        fill_delay = BUY_FILL_POLL_INTERVAL_SEC
        if reconcile_retries < fill_retries or reconcile_delay_sec < BUY_FILL_POLL_INTERVAL_SEC:
            fill_retries = max(1, int(reconcile_retries))
            fill_delay = float(reconcile_delay_sec)
        filled_qty, filled_avg_price, fill_poll_result, balance_qty = _reconcile_buy_fill(
            broker, target_symbol, retries=fill_retries, delay_sec=fill_delay,
        )
    if filled_qty > 0 and not outcome.exec_log.get("first_fill_at"):
        outcome.exec_log["first_fill_at"] = _now_iso()
    if (filled_qty < requested_qty and v2res is None and config.EXEC_ORDER_STATUS_ADVISORY
            and getattr(broker, "get_order_status", None) is not None):
        # 보조정보(advisory)만: 판정은 위 잔고 기반 결과 그대로. 모의계좌는 0건을 돌려줄 수 있다(2026-10-16 실측).
        try:
            _adv = dict(broker.get_order_status(buy_result.order_id, symbol=target_symbol) or {})
        except Exception as _exc:  # noqa: BLE001
            _adv = {"ok": False, "error": repr(_exc)[:120]}
        outcome.exec_log["status_advisory"] = {k: _adv.get(k) for k in (
            "ok", "found", "order_qty", "filled_qty", "remaining_qty", "cancelled", "error")}
    outcome.filled_qty = filled_qty
    outcome.fill_poll_result = fill_poll_result
    outcome.balance_qty = balance_qty
    outcome.unfilled_qty = max(requested_qty - filled_qty, 0)
    timestamps["buy_reconciled_at"] = _now_iso()
    if filled_qty < requested_qty and v2res is None and lm is ledger:
        # 부분체결 잔량 재호가 (2026-10-23, 기본 기능). 전량체결이면 여기 오지 않는다. MACD2 원장 경로 전용.
        pf_out = _handle_partial_fill(
            outcome, broker=broker, buy_result=buy_result, signal_id=signal_id, target_symbol=target_symbol,
            requested_qty=requested_qty, order_price=order_price, balance_qty=filled_qty,
            balance_avg=filled_avg_price, timestamps=timestamps, release_claim=release_claim,
            ledger_module=ledger_module,
        )
        if pf_out is not None:
            return pf_out
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
        if outcome.cancel_result == "OK":
            _release_cancelled_reservation(broker, outcome, requested_qty - max(filled_qty, 0), order_price)
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

    residual = None
    if 0 < qty_after <= config.RESIDUAL_CLEANUP_MAX_QTY:
        # 2026-09-01 real incident: a full exit's own sell can leave a tiny
        # broker-side residual (e.g. 1 of 809 shares) instead of reconciling
        # cleanly to 0. Never changes WHEN/WHY this exit fired -- only clears
        # a small leftover AFTER the decision's own sell already happened.
        residual = _attempt_residual_cleanup(
            broker=broker, symbol=symbol, residual_qty=qty_after, target_qty=0, entry_price=entry_price,
        )
        if residual is not None:
            qty_after = residual["qty_after"]
            outcome.sell_qty_after = qty_after

    if qty_after != 0:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = (FAIL_SELL_RECONCILE_QUERY_ERROR
                                if qty_after == RECONCILE_QTY_UNKNOWN
                                else FAIL_SELL_NOT_CONFIRMED)
        return outcome

    sell_price, sell_telemetry = _sell_price_with_source(sell_result, broker, symbol, entry_price)
    _record_leg(
        broker_mode=broker.mode, signal_id="", symbol=symbol, side="SELL", qty=quantity,
        price=sell_price,
        position_before=quantity + (residual["qty"] if residual else 0),
        position_after=(residual["qty"] if residual else 0),
        exit_reason=exit_reason, order_result=sell_result, entry_price=entry_price,
        confirmed_at=timestamps["sell_confirmed_at"], ledger_module=ledger_module,
        sell_telemetry={**sell_telemetry, **_sell_timestamps(timestamps)},
    )
    if residual is not None:
        _record_leg(
            broker_mode=broker.mode, signal_id="", symbol=symbol, side="SELL", qty=residual["qty"],
            price=residual["price"], position_before=residual["qty"], position_after=0,
            exit_reason=f"{exit_reason}_RESIDUAL_CLEANUP", order_result=residual["sell_result"],
            entry_price=entry_price, confirmed_at=residual["confirmed_at"], ledger_module=ledger_module,
            source=config.RESIDUAL_CLEANUP_SOURCE, sell_telemetry=residual["telemetry"],
        )
        outcome.residual_cleanup_qty = residual["qty"]
    outcome.final_state = SignalState.EXECUTED
    outcome.block_reason = exit_reason
    return outcome


def _reconcile_to_target(broker, symbol: str, target_qty: int, *, retries: int, delay_sec: float) -> int:
    qty_after = RECONCILE_QTY_UNKNOWN
    for attempt in range(max(1, retries)):
        qty = _query_position_qty(broker, symbol)
        if qty == target_qty:
            return qty
        if qty != RECONCILE_QTY_UNKNOWN or qty_after == RECONCILE_QTY_UNKNOWN:
            qty_after = qty
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

    residual = None
    excess = qty_after - remaining_qty
    if 0 < excess <= config.RESIDUAL_CLEANUP_MAX_QTY:
        # Same 2026-09-01 fix as execute_exit, for a partial exit's own
        # target (remaining_qty, not necessarily 0): the broker held
        # `excess` MORE than the intended remaining_qty after this sell --
        # clear exactly that tiny overshoot in one extra market sell.
        residual = _attempt_residual_cleanup(
            broker=broker, symbol=symbol, residual_qty=excess, target_qty=remaining_qty, entry_price=entry_price,
        )
        if residual is not None:
            qty_after = residual["qty_after"]
            outcome.sell_qty_after = qty_after

    if qty_after != remaining_qty:
        outcome.final_state = SignalState.FAILED
        outcome.block_reason = (FAIL_SELL_RECONCILE_QUERY_ERROR
                                if qty_after == RECONCILE_QTY_UNKNOWN
                                else FAIL_SELL_NOT_CONFIRMED)
        return outcome

    sell_price, sell_telemetry = _sell_price_with_source(sell_result, broker, symbol, entry_price)
    _record_leg(
        broker_mode=broker.mode, signal_id="", symbol=symbol, side="SELL", qty=sell_qty,
        price=sell_price,
        position_before=sell_qty + remaining_qty + (residual["qty"] if residual else 0),
        position_after=remaining_qty + (residual["qty"] if residual else 0),
        exit_reason=exit_reason, order_result=sell_result, entry_price=entry_price,
        confirmed_at=timestamps["sell_confirmed_at"], ledger_module=ledger_module,
        sell_telemetry={**sell_telemetry, **_sell_timestamps(timestamps)},
    )
    if residual is not None:
        _record_leg(
            broker_mode=broker.mode, signal_id="", symbol=symbol, side="SELL", qty=residual["qty"],
            price=residual["price"], position_before=residual["qty"] + remaining_qty, position_after=remaining_qty,
            exit_reason=f"{exit_reason}_RESIDUAL_CLEANUP", order_result=residual["sell_result"],
            entry_price=entry_price, confirmed_at=residual["confirmed_at"], ledger_module=ledger_module,
            source=config.RESIDUAL_CLEANUP_SOURCE, sell_telemetry=residual["telemetry"],
        )
        outcome.residual_cleanup_qty = residual["qty"]
    outcome.final_state = SignalState.EXECUTED
    outcome.block_reason = exit_reason
    outcome.quantity = sell_qty
    return outcome
