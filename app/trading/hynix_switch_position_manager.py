"""
hynix_switch_position_manager.py — SK Hynix/inverse switching, TP/SL, end-of-day forced liquidation.

Calls buy()/sell() directly on the broker made by
`app.trading.broker_factory.create_broker(mode)` (OrderManager 경유 금지 —
the inverse symbol '0197X0' has isdigit()==False and gets blocked by the
ETF/ETN filter). Every position follows same-day-entry/same-day-exit rules.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.utils.time_utils import kst_now
from app.trading.hynix_symbols import (
    SIGNAL_SYMBOL,
    SIGNAL_NAME,
    LONG_SYMBOL,
    LONG_NAME,
    SHORT_SYMBOL,
    SHORT_NAME,
)
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed, should_liquidate_now
from app.trading.hynix_position_common import (
    get_hynix_auto_position, is_buy_cooldown_active, POSITION_CONFLICT, MIN_SECONDS_BETWEEN_BUYS,
)
from app.trading.etf_entry_confirmation import (
    confirm_etf_entry, classify_etf_direction_confirmation,
    resolve_window_directions, has_any_slope_data,
    ETF_CONFIRM_UP, ETF_CONFIRM_DOWN, ALIGNED_PULLBACK, ETF_CONFIRMATION_PENDING,
)

ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = ROOT / "config" / "hynix_enhanced_weights.json"

_DEFAULT_RISK = {
    "take_profit_1_pct": 1.2, "take_profit_1_ratio": 0.5,
    "take_profit_2_pct": 2.0, "take_profit_2_ratio": 1.0,
    "stop_loss_1_pct": -1.0, "stop_loss_1_ratio": 0.5,
    "stop_loss_2_pct": -1.5, "stop_loss_2_ratio": 1.0,
    "daily_loss_limit_pct": -2.0,
}
# 요구사항6(2026-07-15) — 레버리지 ETF 위험 반영: 일반(눌림목 확인) 진입은 최대 30%.
# "3회 확인 후 최대 50%"로 확대되는 계단식 사이징은 이 값이 아니라
# hynix_trend_switch_accelerator.py의 exploratory_position_pct(30%)/
# confirmed_position_pct_min·max(50%)가 담당한다(plan_entry의 same_direction_streak
# 기반 진입 전용 경로) — 이 sizing 섹션은 그 경로를 타지 않는 일반 진입에만 쓰인다.
_DEFAULT_SIZING = {"normal_trade_cash_pct": 0.30, "forced_trade_cash_pct": 0.08}
POSITION_SYNC_PENDING = "POSITION_SYNC_PENDING"
SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH = "ENHANCED_REGIME_SWITCH"

# 요구사항(2026-07-16) — Entry Approved=YES인데 Order Sent=NO일 때, blocking_reason에
# 진입 승인 문구가 그대로 남는 대신 "왜 주문이 전송되지 않았는지" 정확히 한 가지
# 사유를 남긴다. hynix_switch_engine._build_blocking_reason()이 이 코드를 사람이
# 읽을 문장으로 매핑한다.
ORDER_FAILURE_ORDER_QTY_ZERO = "ORDER_QTY_ZERO"
ORDER_FAILURE_BUYABLE_CASH_ZERO = "BUYABLE_CASH_ZERO"
ORDER_FAILURE_PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
ORDER_FAILURE_COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
ORDER_FAILURE_ORDER_IN_FLIGHT = "ORDER_IN_FLIGHT"
ORDER_FAILURE_BROKER_REJECTED = "BROKER_REJECTED"
ORDER_FAILURE_EXECUTION_EXCEPTION = "EXECUTION_EXCEPTION"
ORDER_FAILURE_MIN_ORDER_NOTIONAL = "MIN_ORDER_NOTIONAL"
MIN_ORDER_NOTIONAL_KRW = 100_000.0
WEIGHTED_ORDER_CONTROLLER_SOURCE = "WEIGHTED_ORDER_CONTROLLER"
_POSITION_SYNC_RETRY_ATTEMPTS = 3
_POSITION_SYNC_RETRY_DELAY_SECONDS = 2
_POSITION_STATE_LOCK = threading.RLock()

# 요구사항(2026-07-15) — SK하이닉스(000660)는 시세·추세·신호 계산에만 쓴다. 실제
# 매매는 상승 신호일 때 KODEX SK하이닉스단일종목레버리지(0193T0), 하락 신호일 때
# SOL SK하이닉스선물단일종목인버스2X(0197X0)를 매수한다. 000660 직접 매수·매도는
# 완전히 금지된다 — _buy_new/_execute_sell의 하드 가드가 이를 강제한다.
HYNIX_SYMBOL = SIGNAL_SYMBOL
HYNIX_NAME = SIGNAL_NAME
INVERSE_SYMBOL = SHORT_SYMBOL
INVERSE_NAME = SHORT_NAME

_ACTION_TO_SYMBOL = {
    "HYNIX_STRONG_BUY": LONG_SYMBOL, "HYNIX_BUY": LONG_SYMBOL,
    "INVERSE_STRONG_BUY": INVERSE_SYMBOL, "INVERSE_BUY": INVERSE_SYMBOL,
}
_SYMBOL_NAME = {LONG_SYMBOL: LONG_NAME, INVERSE_SYMBOL: INVERSE_NAME, HYNIX_SYMBOL: HYNIX_NAME}


def _load_section(name: str, defaults: dict) -> dict:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            return {**defaults, **(data.get(name) or {})}
    except Exception as exc:
        logger.debug("[SwitchPositionManager] failed to load %s config, using defaults: %s", name, exc)
    return dict(defaults)


def _cycle_bucket(now: datetime) -> str:
    return now.strftime("%Y%m%d%H") + f"{(now.minute // 3) * 3:02d}"


def _current_price(symbol: str, hynix_price: Optional[float], inverse_price: Optional[float]) -> Optional[float]:
    """`hynix_price`/`inverse_price`는 실행(주문·손익) 기준 가격 슬롯의 기존 이름을
    그대로 쓴다 — 실제로 이 자리에 들어오는 값은 000660이 아니라 실거래 종목(상승:
    LONG_SYMBOL/0193T0, 하락: INVERSE_SYMBOL/0197X0)의 현재가다(호출부인
    hynix_switch_engine.py에서 그렇게 채워 넘긴다). SIGNAL_SYMBOL(000660)은 이
    함수가 알 필요가 없다 — 거래 대상이 아니기 때문이다."""
    if symbol == LONG_SYMBOL:
        return hynix_price
    if symbol == INVERSE_SYMBOL:
        return inverse_price
    return None


def _assert_not_signal_symbol(symbol: str, action: str) -> None:
    """요구사항8 — SIGNAL_SYMBOL(000660) 직접 매수·매도는 완전히 금지한다. 호출부
    로직에 실수가 있어 000660이 실제 주문 경로로 흘러 들어와도, 여기서 즉시 막아
    (테스트가 이 예외로 실패해) 절대 브로커에 도달하지 않게 한다."""
    if symbol == SIGNAL_SYMBOL:
        raise ValueError(
            f"{action}: SIGNAL_SYMBOL({SIGNAL_SYMBOL}) direct order is forbidden — "
            f"trade LONG_SYMBOL({LONG_SYMBOL}) or INVERSE_SYMBOL({INVERSE_SYMBOL}) instead"
        )


def evaluate_tp_sl(position: dict, current_price: Optional[float], hard_sl_pct: Optional[float] = None) -> Optional[dict]:
    """Evaluate TP/SL stage. Returns {"ratio":.., "reason":.., "tag":..} when triggered, else None.

    hard_sl_pct(음수, confirmed adaptive regime 기준 effective_sl_pct)가 주어지면
    legacy 고정폭 tier(_DEFAULT_RISK)보다 항상 먼저 확인한다 — Dynamic Exit
    watcher 스레드가 죽어 이 legacy 경로가 fallback으로 동작할 때도, 손절 계산의
    단일 입력(단일 effective_sl_pct)에서 벗어나지 않게 한다."""
    risk = _load_section("risk", _DEFAULT_RISK)
    if not position or not position.get("symbol") or (position.get("quantity") or 0) <= 0:
        return None
    entry = position.get("entry_price")
    if not entry or entry <= 0 or current_price is None:
        return None
    pct = (current_price / entry - 1.0) * 100

    if hard_sl_pct is not None and pct <= hard_sl_pct:
        return {"ratio": 1.0, "reason": f"hard stop loss ({pct:.2f}% <= {hard_sl_pct:.2f}%, confirmed regime)", "tag": "hard_sl"}

    if pct >= risk["take_profit_2_pct"]:
        return {"ratio": 1.0, "reason": f"take profit full (+{pct:.2f}%>=+{risk['take_profit_2_pct']}%)", "tag": "tp2"}
    if pct >= risk["take_profit_1_pct"] and not position.get("partial_tp1_done"):
        return {"ratio": risk["take_profit_1_ratio"], "reason": f"take profit 50% (+{pct:.2f}%>=+{risk['take_profit_1_pct']}%)", "tag": "tp1"}
    if pct <= risk["stop_loss_2_pct"]:
        return {"ratio": 1.0, "reason": f"stop loss full ({pct:.2f}% <= {risk['stop_loss_2_pct']}%)", "tag": "sl2"}
    if pct <= risk["stop_loss_1_pct"] and not position.get("partial_sl1_done"):
        return {"ratio": risk["stop_loss_1_ratio"], "reason": f"stop loss partial ({pct:.2f}% <= {risk['stop_loss_1_pct']}%)", "tag": "sl1"}
    return None


def _positive_float(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _state_buyable_cash_fallback(state: Optional[dict]) -> tuple[float, Optional[str]]:
    if not isinstance(state, dict):
        return 0.0, None
    snapshot = state.get("last_account_equity_snapshot") or {}
    daily_calc = state.get("daily_return_calculation") or {}
    calc_snapshot = daily_calc.get("account_snapshot") or {}
    candidates: list[tuple[str, object]] = [
        ("state.cash", state.get("cash")),
        ("last_account_equity_snapshot.cash", snapshot.get("cash")),
        ("daily_return_calculation.account_snapshot.cash", calc_snapshot.get("cash")),
    ]
    if state.get("mode") == "mock":
        candidates.extend([
            ("mock_budget_krw", state.get("mock_budget_krw")),
            ("daily_pnl_baseline_equity", state.get("daily_pnl_baseline_equity")),
        ])
    for source, raw in candidates:
        cash = _positive_float(raw)
        if cash is not None:
            return cash, source
    return 0.0, None


def _query_buyable_cash(
    broker, *, symbol: Optional[str] = None, current_price: Optional[float] = None, state: Optional[dict] = None,
) -> tuple[float, str]:
    """요구사항(2026-07-16) — 매수가능금액 0원이 "조회 실패"인지 "실제 0원"인지
    구분한다. broker.get_buyable_cash_status()가 있으면(KisMockBroker/KisRealBroker)
    그 결과를 최우선으로 신뢰한다: 정상 응답의 실제 0원은 fallback으로 덮어쓰지
    않고 그대로 0을 반환해 매수를 막고(정상 상태), API 실패/필드누락일 때만 기존
    폴백 체인(stock_buyable → buyable_cash → state 캐시)으로 넘어간다."""
    errors: list[str] = []

    if hasattr(broker, "get_buyable_cash_status"):
        status = None
        try:
            status = broker.get_buyable_cash_status(symbol=symbol or "005930", price=int(current_price or 0))
        except Exception as exc:
            errors.append(f"buyable_cash_status: {exc}")
        if isinstance(state, dict):
            state["buyable_cash_diagnostic"] = {
                "status": (status or {}).get("status", "EXCEPTION"),
                "ok": (status or {}).get("ok", False),
                "value": (status or {}).get("value"),
                "source": (status or {}).get("source"),
                "rt_cd": (status or {}).get("rt_cd"), "msg_cd": (status or {}).get("msg_cd"),
                "msg1": (status or {}).get("msg1"), "error": (status or {}).get("error") or (errors[-1] if errors else None),
                "checked_at": kst_now().isoformat(),
            }
        if status is not None and status.get("ok"):
            value = float(status.get("value") or 0.0)
            if value > 0:
                return value, f"broker_buyable_cash_status:{status.get('source')}"
            # 정상 응답의 실제 0원 — "조회 실패로 인한 0"과 달리 fallback으로
            # 대체하지 않는다(실제로 살 수 있는 돈이 없는 정상 상태이기 때문).
            logger.info(
                "[SwitchPositionManager] buyable cash query OK, actual balance is 0 (source=%s)",
                status.get("source"),
            )
            return 0.0, f"broker_buyable_cash_status_zero:{status.get('source')}"
        if status is not None:
            errors.append(f"buyable_cash_status: {status.get('status')} {status.get('error')}")

    if symbol and hasattr(broker, "get_stock_buyable_amount"):
        try:
            cash = _positive_float(broker.get_stock_buyable_amount(symbol, int(current_price or 0)))
            if cash is not None:
                return cash, "broker_stock_buyable"
        except Exception as exc:
            errors.append(f"stock_buyable: {exc}")
    try:
        cash = _positive_float(broker.get_buyable_cash())
        if cash is not None:
            return cash, "broker_buyable_cash"
    except Exception as exc:
        errors.append(f"buyable_cash: {exc}")
    fallback_cash, fallback_source = _state_buyable_cash_fallback(state)
    if fallback_cash > 0:
        logger.warning(
            "[SwitchPositionManager] buyable cash query unavailable/zero; using %s=%s for sizing. errors=%s",
            fallback_source, fallback_cash, "; ".join(errors) or "zero response",
        )
        return fallback_cash, f"state_fallback:{fallback_source}"
    if errors:
        logger.warning("[SwitchPositionManager] buyable cash query failed: %s", "; ".join(errors))
    return 0.0, "unavailable"


def _sizing_cash_amount(
    broker, forced: bool, target_position_pct: Optional[float] = None, *,
    symbol: Optional[str] = None, current_price: Optional[float] = None, state: Optional[dict] = None,
) -> tuple[float, float]:
    """Return (sized cash amount, total buyable cash) for a new entry. (0.0, 0.0) if the query fails."""
    sizing = _load_section("sizing", _DEFAULT_SIZING)
    cash, cash_source = _query_buyable_cash(broker, symbol=symbol, current_price=current_price, state=state)
    if cash <= 0:
        return 0.0, 0.0
    if target_position_pct is not None:
        pct = float(target_position_pct)
        if pct > 1.0:
            pct = pct / 100.0
        pct = max(0.0, min(1.0, pct))
    else:
        pct = sizing["forced_trade_cash_pct"] if forced else sizing["normal_trade_cash_pct"]
    if isinstance(state, dict):
        state["last_buyable_cash_source"] = cash_source
        state["last_buyable_cash_used"] = cash
    return max(0.0, cash * float(pct)), cash


def _record_order(
    orders: list, order_result, action: str, symbol: str, quantity: int, price: float, reason: str,
    before_qty: Optional[int] = None, expected_remaining_qty: Optional[int] = None,
    mode: str = "mock", signal_source: str = SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH, entry_price: Optional[float] = None,
    broker=None, fusion_metadata: Optional[dict] = None, confirmed_executed_qty: Optional[int] = None,
) -> Optional[dict]:
    """confirmed_executed_qty: the ACTUAL filled quantity, confirmed by re-querying the
    broker after the order (see _confirm_remaining_quantity_from_broker) — never the
    requested `quantity`. rt_cd=0 ("success") or the presence of an order number only
    means the order was ACCEPTED, not that it filled (limit orders can sit unfilled).
    Recording the requested quantity as executed_qty on acceptance alone falsely marks
    unfilled/pending orders as filled in the ledger (2026-07-15 user report: CSV showed
    1480 bought/648 sold while the broker/UI showed no holding). When the caller hasn't
    confirmed a fill (confirmed_executed_qty is None), this records executed_qty=0 —
    acceptance and fill are recorded as separate facts.

    fusion_metadata: dict passed only from the Adaptive Fusion path —
    {active_probability, prediction_v2_probability, cycle_probability, fused_probability,
     prediction_v2_weight, dominant_model, model_agreement, expected_value, target_position_pct}.
    Other paths (ENHANCED_REGIME_SWITCH/DYNAMIC_EXIT etc.) pass None, leaving those columns
    blank (those probability fields were never computed by that strategy — a blank value is
    correct, unlike the trade-cost fields below).

    Return value: when SELL succeeds and entry_price is known, {"gross_pnl", "net_pnl",
    "total_cost"} — the trade-cost breakdown for this fill. The caller MUST use this value
    to update state's realized PnL — recomputing (current_price-entry_price)*qty (Gross)
    separately would drift from the ledger's net_pnl (2026-07-13 user report: the UI labeled
    a field "net PnL" while actually accumulating Gross). Otherwise (BUY/failure/no entry_price)
    returns None."""
    result = order_result.to_dict() if hasattr(order_result, "to_dict") else dict(order_result)
    success = bool(result.get("success"))
    # confirmed_executed_qty=None means "not confirmed yet" (caller had no position_manager
    # to verify with) — historically this fell back to trusting `quantity` on success, which
    # is exactly the bug being fixed. None now means 0 (unconfirmed), not "assume full fill".
    executed_qty = int(confirmed_executed_qty) if confirmed_executed_qty is not None else 0
    orders.append({
        "timestamp": kst_now().isoformat(), "action": action, "symbol": symbol,
        "name": _SYMBOL_NAME.get(symbol, symbol), "quantity": quantity, "price": price,
        "amount": (quantity or 0) * (price or 0), "reason": reason,
        "success": result.get("success"), "message": result.get("message"), "order_id": result.get("order_id"),
        "before_qty": before_qty, "executed_qty": executed_qty,
        "expected_remaining_qty": expected_remaining_qty,
        # 요구사항1 — KIS 응답의 rt_cd/msg_cd/msg1을 UI/로그에서 바로 진단할 수 있도록
        # 주문 로그에도 그대로 남긴다(그동안은 success/message만 남아 실패 원인을
        # 알 수 없었다).
        "rt_cd": result.get("rt_cd"), "msg_cd": result.get("msg_cd"), "msg1": result.get("msg1"),
    })

    # ── Single trade-ledger record (the UI's today's-trade-count/realized-PnL/last buy·sell
    # price MUST be computed from this ledger — individual CSV/state fields are not
    # aggregated separately) ──
    try:
        from app.services.hynix_execution_ledger import record_execution, record_confirmed_fill
        from app.trading.trading_cost_engine import TradeCostEngine

        # Trade-cost fields must always be filled with a real number on every fill
        # (BUY/SELL, on success) — never left NaN/blank (2026-07-13 user verification issue).
        # BUY records only the buy fee (+estimated buy-side slippage) actually incurred at
        # that moment (gross/net_pnl stay 0 since the position isn't closed yet); SELL knows
        # entry_price, so it computes the full round-trip cost and finalizes
        # realized_pnl (=net_pnl).
        cost_engine = TradeCostEngine()
        gross_pnl = buy_fee = sell_fee = transaction_tax = slippage_cost = net_pnl = 0.0
        realized_pnl = None
        fees_total = tax_total = 0.0

        # 요구사항2 — 비용/손익도 확정된 체결수량(executed_qty) 기준으로 계산한다.
        # 접수(success)만으로 quantity(요청수량) 기준 비용을 매겼다면 미체결/부분체결
        # 주문에서 실제보다 큰 수수료·손익이 기록된다.
        if success and action == "BUY" and executed_qty > 0:
            buy_cost = cost_engine.compute_trade_cost(symbol, "BUY", price, executed_qty)
            buy_fee = buy_cost["fee"]
            slippage_cost = cost_engine._slippage_rate("limit") * price * executed_qty
            net_pnl = -(buy_fee + slippage_cost)
            fees_total = buy_fee
        elif success and action == "SELL" and entry_price and executed_qty > 0:
            cost = cost_engine.compute_net_pnl(symbol, entry_price=entry_price, exit_price=price, quantity=executed_qty)
            gross_pnl, buy_fee, sell_fee = cost["gross_pnl"], cost["buy_fee"], cost["sell_fee"]
            transaction_tax, slippage_cost, net_pnl = cost["transaction_tax"], cost["slippage"], cost["net_pnl"]
            fees_total = buy_fee + sell_fee
            tax_total = transaction_tax
            realized_pnl = net_pnl

        after_qty = None
        if before_qty is not None:
            after_qty = expected_remaining_qty if action == "SELL" else before_qty + executed_qty

        cash_before = cash_after = None
        if broker is not None:
            try:
                cash_after = float(broker.get_buyable_cash())
            except Exception:
                cash_after = None

        is_test_order = "E2E forced" in (reason or "")
        fm = fusion_metadata or {}

        if success and executed_qty > 0 and not is_test_order:
            # 요구사항(2026-07-16) — 실제 확정 체결(executed_qty>0)은 단일 기록 지점
            # record_confirmed_fill()을 거친다(idempotent — 재확인 사이클에서 같은
            # 체결이 다시 넘어와도 중복 기록되지 않음). 쓰기 실패는 예외로 삼키지
            # 않고 orders[-1]에 남겨 호출부가 LEDGER_WRITE_FAILED 경고를 세울 수
            # 있게 한다(체결 자체는 취소하지 않는다).
            outcome = record_confirmed_fill(
                action=action, symbol=symbol, executed_qty=executed_qty,
                executed_price=price if executed_qty > 0 else None,
                mode=mode, before_qty=before_qty or 0, after_qty=after_qty if after_qty is not None else (before_qty or 0),
                order_id=result.get("order_id") or "", requested_qty=quantity, requested_price=price,
                signal_source=signal_source, realized_pnl=realized_pnl, fees=fees_total, tax=tax_total,
                cash_before=cash_before, cash_after=cash_after,
                gross_pnl=gross_pnl, buy_fee=buy_fee, sell_fee=sell_fee,
                transaction_tax=transaction_tax, slippage_cost=slippage_cost, net_pnl=net_pnl,
                active_probability=fm.get("active_probability"), prediction_v2_probability=fm.get("prediction_v2_probability"),
                cycle_probability=fm.get("cycle_probability"), fused_probability=fm.get("fused_probability"),
                prediction_v2_weight=fm.get("prediction_v2_weight"), dominant_model=fm.get("dominant_model"),
                model_agreement=fm.get("model_agreement"), expected_value=fm.get("expected_value"),
                target_position_pct=fm.get("target_position_pct"),
            )
            if outcome.get("error"):
                orders[-1]["ledger_write_failed"] = True
                orders[-1]["ledger_error"] = outcome["error"]
                logger.error(
                    "[SwitchPositionManager] LEDGER_WRITE_FAILED symbol=%s action=%s qty=%s: %s",
                    symbol, action, executed_qty, outcome["error"],
                )
        elif action == "BUY" and executed_qty <= 0:
            orders[-1]["ledger_skipped"] = True
            orders[-1]["ledger_skip_reason"] = "BUY fill not broker-confirmed"
        else:
            # 실패한 시도/미확정 체결(executed_qty=0)/E2E 테스트 주문은 dedup 대상이
            # 아니므로(같은 초에 반복 재시도돼도 각각 남아야 진단에 유용) 기존
            # 방식대로 매 시도를 그대로 기록한다.
            record_execution(
                action=action, symbol=symbol, requested_qty=quantity, executed_qty=executed_qty,
                requested_price=price, executed_price=price if executed_qty > 0 else None, success=success,
                mode=mode, strategy_name="hynix_switch", signal_source=signal_source,
                before_qty=before_qty, after_qty=after_qty, cash_before=cash_before, cash_after=cash_after,
                realized_pnl=realized_pnl, fees=fees_total, tax=tax_total,
                order_id=result.get("order_id") or "", is_test_order=is_test_order,
                gross_pnl=gross_pnl, buy_fee=buy_fee, sell_fee=sell_fee,
                transaction_tax=transaction_tax, slippage_cost=slippage_cost, net_pnl=net_pnl,
                active_probability=fm.get("active_probability"), prediction_v2_probability=fm.get("prediction_v2_probability"),
                cycle_probability=fm.get("cycle_probability"), fused_probability=fm.get("fused_probability"),
                prediction_v2_weight=fm.get("prediction_v2_weight"), dominant_model=fm.get("dominant_model"),
                model_agreement=fm.get("model_agreement"), expected_value=fm.get("expected_value"),
                target_position_pct=fm.get("target_position_pct"),
            )
        if action == "SELL" and realized_pnl is not None:
            return {"gross_pnl": gross_pnl, "net_pnl": net_pnl, "total_cost": round(fees_total + tax_total + slippage_cost, 2)}
        return None
    except Exception as exc:
        logger.error("[SwitchPositionManager] execution ledger record failed (harmless but reduces ledger trust): %s", exc)
        return None


def _record_skipped(orders: list, action: str, symbol: str, price: Optional[float], reason: str, message: str) -> None:
    """Log even orders that never made it to the broker (avoid silent gaps in the order log)."""
    orders.append({
        "timestamp": kst_now().isoformat(), "action": action, "symbol": symbol,
        "name": _SYMBOL_NAME.get(symbol, symbol), "quantity": 0, "price": price or 0,
        "amount": 0, "reason": f"{reason} — skipped: {message}",
        "success": False, "message": message, "order_id": "",
    })


def _position_qty(position, symbol: str) -> int:
    if isinstance(position, dict):
        pos_symbol = position.get("symbol")
        qty = position.get("quantity", position.get("hldg_qty", 0))
    else:
        pos_symbol = getattr(position, "symbol", None)
        qty = getattr(position, "quantity", getattr(position, "hldg_qty", 0))
    if pos_symbol != symbol:
        return 0
    try:
        return int(float(qty or 0))
    except Exception:
        return 0


def _position_avg_price(position):
    if isinstance(position, dict):
        return position.get("avg_price") or position.get("pchs_avg_pric") or position.get("entry_price")
    return getattr(position, "avg_price", getattr(position, "pchs_avg_pric", None))


def _confirm_remaining_quantity_from_broker(
    broker, symbol: str, position_manager=None, attempts: int = _POSITION_SYNC_RETRY_ATTEMPTS,
    delay_seconds: int = _POSITION_SYNC_RETRY_DELAY_SECONDS, retry_while_qty_equals: Optional[int] = None,
) -> dict:
    """Confirm symbol quantity from broker/KIS after an order.

    A failed balance read is never treated as zero. The caller must keep the
    previous local position and block new entries until a later sync succeeds.

    retry_while_qty_equals: 요구사항3 — 브로커 조회 자체는 성공(예외 없음)했지만
    아직 이번 주문 전(before_qty)과 수량이 똑같다면(=아직 반영 안 됨), 그것만으로
    "확정"하지 않고 2초 간격으로 재조회한다. 예외 발생 시 재시도 로직과는 별개의
    경로다 — 여기서는 "조회는 성공했는데 체결이 아직 안 보임"을 다룬다.
    """
    last_error = None
    attempts = max(1, int(attempts or 1))
    for idx in range(attempts):
        try:
            positions = broker.get_positions()
            qty = 0
            avg_price = None
            matched = None
            for pos in positions or []:
                q = _position_qty(pos, symbol)
                if q > 0:
                    qty = q
                    matched = pos
                    avg_price = _position_avg_price(pos)
                    break
            if position_manager is not None:
                try:
                    position_manager.sync(force=True)
                    pm_position = position_manager.current_position or {}
                    pm_qty = _position_qty(pm_position, symbol)
                    if pm_qty > 0:
                        # Treat a still-visible position-manager balance as real until
                        # it disappears. This prevents an opposite buy after an accepted
                        # sell when the broker/ledger state has not fully converged.
                        qty = max(qty, pm_qty)
                        matched = pm_position
                        avg_price = _position_avg_price(pm_position) or avg_price
                except Exception:
                    pass
            if (
                retry_while_qty_equals is not None and qty == retry_while_qty_equals
                and idx < attempts - 1 and delay_seconds > 0
            ):
                time.sleep(delay_seconds)
                continue
            return {
                "ok": True, "quantity": qty, "avg_price": avg_price, "position": matched,
                "attempts": idx + 1, "status": "SYNCED",
            }
        except Exception as exc:
            last_error = str(exc)
            if position_manager is not None:
                # broker.get_positions()가 아예 없거나(테스트 스텁 등) 실패해도,
                # position_manager가 이미 재조회한 결과를 갖고 있다면 그것으로
                # 확정한다 — 브로커 직접 조회 실패가 곧 "미확인"을 뜻하지는 않는다.
                try:
                    position_manager.sync(force=True)
                    pm_position = position_manager.current_position or {}
                    if pm_position.get("symbol") == symbol:
                        return {
                            "ok": True, "quantity": int(pm_position.get("quantity") or 0),
                            "avg_price": pm_position.get("avg_price"), "position": pm_position,
                            "attempts": idx + 1, "status": "SYNCED",
                        }
                    if not pm_position.get("symbol"):
                        return {
                            "ok": True, "quantity": 0, "avg_price": None, "position": None,
                            "attempts": idx + 1, "status": "SYNCED",
                        }
                except Exception:
                    pass
            if idx < attempts - 1 and delay_seconds > 0:
                time.sleep(delay_seconds)
    return {
        "ok": False, "quantity": None, "avg_price": None, "position": None,
        "attempts": attempts, "status": POSITION_SYNC_PENDING, "error": last_error,
    }


def _resync_position_from_broker(state: dict, broker, position_manager=None) -> dict:
    """Force a fresh broker balance read and refresh state["position"] from it.

    Used before any "already holding X" decision so a stale/pending local flag
    is never trusted over the broker's actual current holdings (requirement:
    POSITION_SYNC_PENDING must not be treated as a confirmed position).
    """
    if position_manager is None:
        try:
            from app.trading.broker_factory import create_broker  # noqa: F401 - broker already provided by caller
        except Exception:
            pass
        return {"ok": False, "error": "no position_manager available for resync"}
    try:
        position_manager.sync(force=True)
        apply_position_manager_to_state(state, position_manager)
        return {"ok": state.get("position_sync_status") == "SYNCED", "status": state.get("position_sync_status")}
    except Exception as exc:
        state["position_sync_status"] = POSITION_SYNC_PENDING
        state["position_sync_error"] = str(exc)
        state["position_sync_block_new_orders"] = True
        return {"ok": False, "error": str(exc)}


def _sell_all_or_ratio(
    broker, position: dict, current_price: float, ratio: float, reason: str, orders: list,
    mode: str = "mock", exit_reason_type: Optional[str] = None, signal_source: str = SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH,
    fusion_metadata: Optional[dict] = None, position_manager=None,
) -> dict:
    """Sell the full position or a ratio of it.

    When exit_reason_type is given, this runs through the Exit Order Coordinator
    lock — if another sell for the same (mode, symbol, exit_reason_type) is in
    progress or filled within the last 30 seconds, this sell is skipped (prevents
    legacy TP/SL, forced liquidation, switching, and Dynamic Exit AI from all
    selling the same position at once).

    When position_manager is given, the order's accepted(success) response alone
    isn't trusted — the broker is re-queried so remaining_quantity reflects the
    actual remaining shares (covers unfilled/partial fills).
    """
    symbol = position["symbol"]
    entry_price = position.get("entry_price")
    total_qty = int(position.get("quantity") or 0)
    sell_qty = max(1, int(total_qty * ratio)) if ratio < 1.0 else total_qty
    sell_qty = min(sell_qty, total_qty)
    expected_remaining = 0 if ratio >= 1.0 else max(0, total_qty - sell_qty)
    if sell_qty <= 0:
        _record_skipped(orders, "SELL_SKIPPED", symbol, current_price, reason, "sell quantity is 0")
        return {"success": False, "message": "sell quantity is 0"}

    return _execute_sell(
        broker, symbol, sell_qty, current_price, reason, orders, total_qty, expected_remaining,
        mode=mode, signal_source=signal_source, entry_price=entry_price, fusion_metadata=fusion_metadata,
        position_manager=position_manager, exit_reason_type=exit_reason_type, ratio=ratio,
    )


def _execute_sell(
    broker, symbol: str, sell_qty: int, current_price: float, reason: str, orders: list,
    before_qty: int, expected_remaining: int,
    mode: str = "mock", signal_source: str = SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH, entry_price: Optional[float] = None,
    fusion_metadata: Optional[dict] = None, position_manager=None, exit_reason_type: Optional[str] = None,
    ratio: float = 1.0,
) -> dict:
    _assert_not_signal_symbol(symbol, "SELL")
    from app.trading import exit_order_coordinator as order_coord

    meta = fusion_metadata or {}
    episode_id = meta.get("episode_id") or meta.get("signal_id") or "NO_EPISODE"
    exit_event_id = meta.get("exit_event_id") or f"{exit_reason_type or reason or 'SELL'}:{time.monotonic_ns()}"
    severity = meta.get("severity") or ("HARD_STOP" if exit_reason_type == "stop_loss" else ("STRONG" if ratio >= 1.0 else "WEAK"))
    account = order_coord.infer_account_id(broker, mode)

    with order_coord.coordinated_order(
        mode=mode, account=account, symbol=symbol, side="SELL",
        episode_id=episode_id, exit_event_id=exit_event_id, target_qty=sell_qty,
        source=signal_source, severity=severity, reason=reason,
        detected_at=meta.get("detected_at"),
    ) as coordinated:
        if coordinated.blocked:
            _record_skipped(orders, "SELL_SKIPPED", symbol, current_price, reason, coordinated.block_reason)
            return {
                "success": False, "message": coordinated.block_reason, "blocked_by_coordinator": True,
                "idempotency_key": coordinated.idempotency_key,
            }
        broker_held_qty = order_coord.get_broker_held_quantity(broker, symbol)
        if broker_held_qty is None and position_manager is not None:
            try:
                position_manager.sync(force=True)
                pm_position = position_manager.current_position or {}
                if pm_position.get("symbol") == symbol:
                    broker_held_qty = int(pm_position.get("quantity") or 0)
                elif not pm_position.get("symbol"):
                    broker_held_qty = 0
            except Exception:
                broker_held_qty = None
        if broker_held_qty is None:
            message = "broker held quantity query failed before sell"
            coordinated.mark(order_coord.ORDER_FAILED, error=message)
            _record_skipped(orders, "SELL_SKIPPED", symbol, current_price, reason, message)
            return {"success": False, "message": message, "position_sync_status": POSITION_SYNC_PENDING}
        if int(broker_held_qty or 0) <= 0 and position_manager is None and mode != "real" and int(before_qty or 0) > 0:
            broker_held_qty = int(before_qty or 0)
        actual_before_qty = int(broker_held_qty)
        actual_sell_qty = min(int(sell_qty or 0), actual_before_qty)
        actual_expected_remaining = max(0, actual_before_qty - actual_sell_qty)
        if actual_sell_qty <= 0:
            message = "broker held quantity is 0 before sell"
            coordinated.mark(order_coord.ORDER_CANCELLED, broker_held_qty=actual_before_qty, sent_qty=0)
            _record_skipped(orders, "SELL_SKIPPED", symbol, current_price, reason, message)
            return {
                "success": False, "message": message, "sold_quantity": 0,
                "remaining_quantity": actual_before_qty, "idempotency_key": coordinated.idempotency_key,
            }
        with _POSITION_STATE_LOCK:
            order = broker.sell(symbol, _SYMBOL_NAME.get(symbol, symbol), actual_sell_qty, current_price)
        order_dict = order.to_dict() if hasattr(order, "to_dict") else dict(order)
        confirmed = None
        confirmed_sell_qty = None
        actual_remaining_qty = None
        if order_dict.get("success"):
            # 요구사항2 — 접수 성공만으로 매도 체결수량을 원장에 기록하지 않는다.
            # 브로커 재조회로 실제 잔량을 확정한 뒤, before_qty와의 차이만큼만 "체결된
            # 매도수량"으로 인정한다.
            confirmed = _confirm_remaining_quantity_from_broker(
                broker, symbol, position_manager=position_manager, retry_while_qty_equals=actual_before_qty,
            )
            if confirmed.get("ok"):
                actual_remaining_qty = int(confirmed.get("quantity") or 0)
                confirmed_sell_qty = max(0, int(actual_before_qty or 0) - actual_remaining_qty)
        cost_breakdown = _record_order(
            orders, order, "SELL", symbol, actual_sell_qty, current_price, reason, before_qty=actual_before_qty,
            expected_remaining_qty=actual_expected_remaining, mode=mode, signal_source=signal_source,
            entry_price=entry_price, broker=broker, fusion_metadata=fusion_metadata,
            confirmed_executed_qty=confirmed_sell_qty,
        )
        if order_dict.get("success") and confirmed is not None and confirmed.get("ok"):
            coordinated.mark(
                order_coord.ORDER_FILLED, broker_held_qty=actual_before_qty,
                sent_qty=actual_sell_qty, filled_qty=confirmed_sell_qty,
                remaining_quantity=actual_remaining_qty, broker_order_id=order_dict.get("order_id"),
            )
        elif order_dict.get("success"):
            coordinated.mark(
                order_coord.ORDER_ACCEPTED, broker_held_qty=actual_before_qty,
                sent_qty=actual_sell_qty, broker_order_id=order_dict.get("order_id"),
            )
        else:
            coordinated.mark(
                order_coord.ORDER_FAILED, broker_held_qty=actual_before_qty,
                sent_qty=actual_sell_qty, broker_error=order_dict.get("message"),
            )
    result = order_dict
    result["idempotency_key"] = coordinated.idempotency_key
    result["sold_quantity"] = confirmed_sell_qty if confirmed_sell_qty is not None else 0
    result["remaining_quantity"] = actual_remaining_qty
    result["expected_remaining_qty"] = actual_expected_remaining
    result["broker_held_qty_before_order"] = actual_before_qty
    result["sent_quantity"] = actual_sell_qty
    result["fill_confirmed"] = None
    if result.get("success"):
        result["position_sync"] = confirmed
        if confirmed is not None and confirmed.get("ok"):
            result["fill_confirmed"] = True
            result["position_sync_status"] = "SYNCED"
            if actual_remaining_qty > actual_expected_remaining:
                result["partial_fill_detected"] = True
                logger.warning(
                    "[SwitchPositionManager] sell partially filled or broker still holds shares: %s expected_remaining=%s actual_remaining=%s",
                    symbol, actual_expected_remaining, actual_remaining_qty,
                )
        else:
            logger.warning(
                "[SwitchPositionManager] sell succeeded but position sync failed; keeping local position pending: %s",
                confirmed.get("error") if confirmed else "no confirmation attempted",
            )
            result["fill_confirmed"] = False
            result["position_sync_status"] = POSITION_SYNC_PENDING

    # The caller MUST update state's realized PnL with net_pnl below — recomputing a
    # separate Gross formula would make the UI's displayed value drift from the ledger.
    result["gross_pnl"] = cost_breakdown.get("gross_pnl") if cost_breakdown else None
    result["net_pnl"] = cost_breakdown.get("net_pnl") if cost_breakdown else None
    return result


def _resolve_realized_pnl(sell_result: dict, current_price: float, entry_price: float, sold_qty: float) -> tuple[float, float]:
    """Extract (Net realized PnL, Gross realized PnL) from a sell result.

    Always prefer the net_pnl/gross_pnl that _execute_sell() computed alongside the
    ledger record — if the caller instead recomputes (current_price-entry_price)*qty
    (Gross) and accumulates that into state, "today's realized PnL (net)" ends up
    actually accumulating Gross, drifting from the ledger's net_realized_pnl
    (2026-07-13 user report). If net_pnl is missing (the ledger record itself failed),
    fall back to Gross but fill both fields with the same value to stay internally
    consistent."""
    gross_pnl = sell_result.get("gross_pnl")
    net_pnl = sell_result.get("net_pnl")
    if net_pnl is not None:
        return float(net_pnl), float(gross_pnl if gross_pnl is not None else net_pnl)
    fallback = (current_price - entry_price) * sold_qty
    return fallback, fallback


def _buy_new(
    broker, symbol: str, current_price: float, cash_amount: float, reason: str, orders: list,
    mode: str = "mock", signal_source: str = SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH, before_qty: int = 0,
    fusion_metadata: Optional[dict] = None, position_manager=None,
) -> dict:
    # 요구사항(2026-07-16 사용자 리포트) — "invalid price/amount"는 가격 문제와
    # 현금(매수가능금액 산정 0원) 문제를 구분하지 못해, BUY 신호가 떴는데 왜 실제
    # 주문이 안 나갔는지 화면에서 알 수 없었다. 두 원인을 분리해 로그/blocking_reason
    # 에서 바로 진단 가능하게 한다. requested_qty/order_price/sized_cash는 실패해도
    # 항상 반환해 UI가 "계산식과 입력값"을 그대로 보여줄 수 있게 한다(요구사항4).
    _diag_base = {"requested_symbol": symbol, "order_price": current_price, "sized_cash": cash_amount}
    if not current_price or current_price <= 0:
        _record_skipped(orders, "BUY_SKIPPED", symbol, current_price, reason, "no valid current price")
        return {
            "success": False, "message": "no valid current price",
            "failure_code": ORDER_FAILURE_PRICE_UNAVAILABLE, "requested_qty": 0, **_diag_base,
        }
    if cash_amount <= 0:
        _record_skipped(orders, "BUY_SKIPPED", symbol, current_price, reason, "sized cash amount is 0 (buyable cash query returned 0/unavailable)")
        return {
            "success": False, "message": "sized cash amount is 0 (buyable cash query returned 0/unavailable)",
            "failure_code": ORDER_FAILURE_BUYABLE_CASH_ZERO, "requested_qty": 0, **_diag_base,
        }
    quantity = int(cash_amount // current_price)
    if quantity < 1:
        _record_skipped(orders, "BUY_SKIPPED", symbol, current_price, reason, "buy amount insufficient for 1 share")
        return {
            "success": False, "message": "buy amount insufficient for 1 share",
            "failure_code": ORDER_FAILURE_ORDER_QTY_ZERO, "requested_qty": quantity, **_diag_base,
        }
    if quantity * current_price < MIN_ORDER_NOTIONAL_KRW:
        _record_skipped(orders, "BUY_SKIPPED", symbol, current_price, reason, "order notional below minimum")
        return {
            "success": False, "message": "order notional below minimum",
            "failure_code": ORDER_FAILURE_MIN_ORDER_NOTIONAL, "requested_qty": quantity, **_diag_base,
        }
    _assert_not_signal_symbol(symbol, "BUY")

    # 요구사항(2026-07-21) — 매도(_execute_sell)만 공용 OrderCoordinator를 거치고
    # 매수(_buy_new)는 broker.buy()를 직접 호출해, Fast Worker와 메인 사이클이
    # 동시에 매수를 시도하면 중복주문/직렬화 보장이 매도 경로와 비대칭이었다.
    # 매도와 동일하게 idempotency key 기반 직렬화·중복차단을 적용한다.
    from app.trading import exit_order_coordinator as order_coord

    meta = fusion_metadata or {}
    episode_id = meta.get("episode_id") or meta.get("signal_id") or "NO_EPISODE"
    entry_event_id = meta.get("entry_event_id") or meta.get("signal_id") or f"BUY:{time.monotonic_ns()}"
    account = order_coord.infer_account_id(broker, mode)

    with order_coord.coordinated_order(
        mode=mode, account=account, symbol=symbol, side="BUY",
        episode_id=episode_id, exit_event_id=entry_event_id, target_qty=quantity,
        source=signal_source, severity=meta.get("severity"), reason=reason,
        detected_at=meta.get("detected_at"),
    ) as coordinated:
        if coordinated.blocked:
            _record_skipped(orders, "BUY_SKIPPED", symbol, current_price, reason, coordinated.block_reason)
            return {
                "success": False, "message": coordinated.block_reason, "blocked_by_coordinator": True,
                "idempotency_key": coordinated.idempotency_key, "requested_qty": quantity, **_diag_base,
            }
        with _POSITION_STATE_LOCK:
            try:
                order = broker.buy(symbol, _SYMBOL_NAME.get(symbol, symbol), quantity, current_price)
            except Exception as exc:
                logger.error("[SwitchPositionManager] broker.buy() 예외: %s", exc)
                coordinated.mark(order_coord.ORDER_FAILED, error=str(exc))
                return {
                    "success": False, "message": f"broker.buy() exception: {exc}",
                    "failure_code": ORDER_FAILURE_EXECUTION_EXCEPTION, "broker_error": str(exc),
                    "requested_qty": quantity, "idempotency_key": coordinated.idempotency_key, **_diag_base,
                }
            order_dict = order.to_dict() if hasattr(order, "to_dict") else dict(order)
            if not order_dict.get("success"):
                order_dict["failure_code"] = ORDER_FAILURE_BROKER_REJECTED
                order_dict["broker_error"] = ", ".join(
                    f"{k}={order_dict.get(k)}" for k in ("rt_cd", "msg_cd", "msg1") if order_dict.get(k) not in (None, "")
                ) or order_dict.get("message") or "broker rejected order"
            order_dict.update({"requested_qty": quantity, **_diag_base})
            confirmed = None
            confirmed_qty = None
            # 요구사항2 — rt_cd=0(success) 또는 주문번호만으로 원장에 체결수량을 기록하지
            # 않는다. position_manager가 주어지면(실거래 경로는 항상 준다) 브로커를
            # 재조회해 실제 체결량을 확정한 뒤에만 원장에 넘긴다. position_manager가 없는
            # 호출부(간단한 단위테스트 등, 체결 재확인 자체가 불가능)만 과거처럼 접수
            # 성공을 곧 체결로 간주한다 — 실거래 경로는 이 분기를 타지 않는다.
            if order_dict.get("success") and hasattr(broker, "get_positions"):
                confirmed = _confirm_remaining_quantity_from_broker(
                    broker, symbol, position_manager=position_manager, retry_while_qty_equals=before_qty,
                )
                if confirmed.get("ok"):
                    confirmed_qty = max(0, int(confirmed.get("quantity") or 0) - int(before_qty or 0))
            elif order_dict.get("success"):
                confirmed_qty = None
            _record_order(
                orders, order, "BUY", symbol, quantity, current_price, reason,
                before_qty=before_qty, mode=mode, signal_source=signal_source, broker=broker,
                fusion_metadata=fusion_metadata, confirmed_executed_qty=confirmed_qty,
            )
            if order_dict.get("success") and confirmed is not None and confirmed.get("ok"):
                coordinated.mark(
                    order_coord.ORDER_FILLED, sent_qty=quantity, filled_qty=confirmed_qty,
                    broker_order_id=order_dict.get("order_id"),
                )
            elif order_dict.get("success"):
                coordinated.mark(
                    order_coord.ORDER_ACCEPTED, sent_qty=quantity, broker_order_id=order_dict.get("order_id"),
                )
            else:
                coordinated.mark(
                    order_coord.ORDER_FAILED, sent_qty=quantity, broker_error=order_dict.get("message"),
                )
    result = order_dict
    result["idempotency_key"] = coordinated.idempotency_key
    result["bought_quantity"] = confirmed_qty if confirmed_qty is not None else 0
    result["filled_quantity"] = confirmed_qty
    result["actual_quantity"] = int(confirmed.get("quantity") or 0) if confirmed and confirmed.get("ok") else None
    result["fill_confirmed"] = None
    if result.get("success"):
        if confirmed is None:
            result["fill_confirmed"] = None
            result["position_sync_status"] = None
        elif not confirmed.get("ok"):
            result["fill_confirmed"] = False
            result["position_sync_status"] = POSITION_SYNC_PENDING
            result["message"] = (result.get("message") or "order accepted") + " / broker balance confirmation failed"
            logger.warning(
                "[SwitchPositionManager] buy succeeded but position sync failed; keeping local position pending: %s",
                confirmed.get("error") if confirmed else "no confirmation attempted",
            )
        elif confirmed_qty is not None and confirmed_qty <= 0:
            result["fill_confirmed"] = False
            result["position_sync_status"] = POSITION_SYNC_PENDING
            result["message"] = (result.get("message") or "order accepted") + " / buy fill not visible in broker balance"
        else:
            result["fill_confirmed"] = True
            result["position_sync_status"] = "SYNCED"
    return result


def _mark_position_sync_pending(state: dict, position: Optional[dict], error: Optional[str], message: str) -> None:
    kept = dict(position or state.get("position") or _empty_position())
    kept["position_sync_status"] = POSITION_SYNC_PENDING
    kept["position_sync_error"] = error
    kept["position_sync_pending_since"] = kst_now().isoformat()
    state["position"] = kept
    state["position_sync_status"] = POSITION_SYNC_PENDING
    state["position_sync_error"] = error
    state["position_sync_block_new_orders"] = True
    state["critical_alert"] = message


def _clear_stale_buy_state_when_flat(state: dict) -> None:
    if state.get("last_action") == "BUY":
        state["last_action"] = None
        state["last_buy_price"] = None
        state["last_order_signature"] = None
        state["last_order_cycle_bucket"] = None
    state["last_big_trend_result"] = None
    state["big_trend_state"] = {}
    state["stop_loss_snapshot"] = None
    state["last_stop_loss_signature"] = None
    state["pending_manual_stop_loss_alert"] = None


def _apply_buy_result_to_state_position(
    state: dict, symbol: str, current_price: float, buy_result: dict, *,
    now: datetime, previous_position: Optional[dict] = None,
    entry_type: Optional[str] = None, stop_loss_pct: Optional[float] = None,
) -> bool:
    status = buy_result.get("position_sync_status")
    if status == POSITION_SYNC_PENDING:
        _mark_position_sync_pending(
            state, previous_position, (buy_result.get("position_sync") or {}).get("error"),
            "POSITION_SYNC_PENDING - broker balance confirmation failed after buy; new orders blocked",
        )
        return False

    if status == "SYNCED":
        qty = int(buy_result.get("actual_quantity") or 0)
        if qty <= 0:
            _mark_position_sync_pending(
                state, previous_position, (buy_result.get("position_sync") or {}).get("error"),
                "POSITION_SYNC_PENDING - buy order accepted but filled quantity is not visible in broker balance",
            )
            return False
    else:
        qty = int(buy_result.get("bought_quantity") or 0)

    if qty <= 0:
        return False

    avg_price = ((buy_result.get("position_sync") or {}).get("avg_price")) or current_price
    state["position"] = {
        **_empty_position(),
        "symbol": symbol, "name": _SYMBOL_NAME.get(symbol, symbol),
        "quantity": qty, "avg_price": avg_price, "entry_price": avg_price,
        "entry_time": now.isoformat(),
        "entry_type": entry_type or "NORMAL",
        "stop_loss_pct": stop_loss_pct,
        "position_sync_status": "SYNCED",
    }
    state["position_sync_status"] = "SYNCED"
    state["position_sync_block_new_orders"] = False
    state["position_sync_error"] = None
    return True


def sync_position_from_broker(state: dict, broker) -> dict:
    """[Legacy compat] Queries the broker directly and syncs state. New code should use
    `HynixPositionManager.sync()` + `apply_position_manager_to_state()` instead.
    """
    from app.trading.hynix_position_common import HynixPositionManager

    pm = HynixPositionManager(broker, mode=state.get("mode", "mock"))
    pm.sync(force=True)
    return apply_position_manager_to_state(state, pm)


def _reconcile_ledger_with_kis(state: dict, position_manager, pos_info: dict, previous_position: dict, symbols: set) -> None:
    """symbols 각각에 대해 KIS 실보유수량과 원장 순수량을 비교하고 필요하면
    backfill한다(요구사항 2026-07-16). UI 표시를 위해 결과를 state["ledger_reconciliation"]
    에 남긴다 — 이 함수 자체는 예외를 밖으로 던지지 않는다(포지션 동기화 자체를
    막으면 안 됨)."""
    from app.services.hynix_execution_ledger import compute_ledger_net_quantity, reconcile_symbol_with_kis

    mode = getattr(position_manager, "mode", None) or state.get("mode", "mock")
    broker = getattr(position_manager, "broker", None)
    now = kst_now()
    reconciliations: dict = {}
    for symbol in symbols:
        is_current_broker_symbol = pos_info.get("symbol") == symbol
        broker_qty = pos_info.get("quantity") if is_current_broker_symbol else 0
        avg_price = pos_info.get("avg_price") if is_current_broker_symbol else None
        if avg_price is None and previous_position.get("symbol") == symbol:
            avg_price = previous_position.get("avg_price") or previous_position.get("entry_price")
        try:
            broker_qty_int = int(broker_qty or 0)
            ledger_qty = compute_ledger_net_quantity(symbol, mode, now.strftime("%Y%m%d"))
            if broker_qty_int <= 0 and ledger_qty > 0:
                result = {
                    "symbol": symbol,
                    "kis_quantity": broker_qty_int,
                    "ledger_quantity": int(ledger_qty),
                    "mismatch": True,
                    "mismatch_code": "LEDGER_BROKER_MISMATCH",
                    "backfilled": [],
                    "error": None,
                    "requires_fill_query": True,
                }
            else:
                result = reconcile_symbol_with_kis(
                    symbol, mode, broker_qty=broker_qty_int, avg_price=avg_price, broker=broker, now=now,
                )
        except Exception as exc:
            logger.error("[SwitchPositionManager] 원장-KIS 재조정 실패(%s): %s", symbol, exc)
            result = {"symbol": symbol, "error": str(exc), "mismatch": None, "backfilled": []}
        reconciliations[symbol] = result
        if result.get("mismatch"):
            code = result.get("mismatch_code") or "LEDGER_POSITION_MISMATCH"
            logger.warning(
                "[SwitchPositionManager] %s symbol=%s kis_qty=%s ledger_qty=%s backfilled=%d",
                code, symbol, result.get("kis_quantity"), result.get("ledger_quantity"), len(result.get("backfilled") or []),
            )
    state["ledger_reconciliation"] = {"checked_at": now.isoformat(timespec="seconds"), "results": reconciliations}


def apply_position_manager_to_state(state: dict, position_manager) -> dict:
    """Reflect HynixPositionManager.sync() result (broker's actual holdings) into state (cache).

    The broker is always the source of truth — when they agree, only quantity/avg_price and
    entry_time are refreshed; the fields we manage on our side are preserved. When they
    disagree, the position is fully re-initialized from the broker.
    """
    if getattr(position_manager, "last_sync_ok", True) is False:
        existing = state.get("position") or {}
        local_flat = not existing.get("symbol") or (existing.get("quantity") or 0) <= 0
        last_ok_at = state.get("position_sync_last_ok_at")
        recent_flat_ok = False
        if local_flat and last_ok_at:
            try:
                age = (kst_now() - datetime.fromisoformat(str(last_ok_at))).total_seconds()
                last_pos = state.get("position_sync_last_position") or {}
                recent_flat_ok = age <= 90 and (
                    not last_pos.get("symbol") or (last_pos.get("quantity") or 0) <= 0
                )
            except Exception:
                recent_flat_ok = False
        sync_error = getattr(position_manager, "last_sync_error", None)
        if recent_flat_ok:
            state["position_sync_status"] = "SYNCED_RECENT_CACHE"
            state["position_sync_error"] = sync_error
            state["position_sync_block_new_orders"] = False
            state["position_sync_warning"] = (
                "broker position sync temporarily failed; using recent flat broker sync"
            )
            # 최근(90초 이내) 정상 동기화된 flat 포지션이 있으므로 실질적으로 위험한
            # 상황이 아니다 — 이전에 남아있던 POSITION_SYNC_PENDING류 stale 배너를 지운다.
            if state.get("critical_alert") and "POSITION_SYNC_PENDING" in str(state.get("critical_alert")):
                state["critical_alert"] = None
            return state
        state["position_sync_status"] = POSITION_SYNC_PENDING
        state["position_sync_error"] = sync_error
        state["position_sync_block_new_orders"] = True
        # 요구사항 — 원인을 명확히 표시: 브로커 예외(rt_cd/msg_cd/msg1이 포함된 문자열)를
        # 그대로 이어붙인다. 이전에는 고정 문구만 남아 "왜" 실패했는지 화면에서 전혀 알 수
        # 없었다(2026-07-16 사용자 리포트).
        state["critical_alert"] = (
            "POSITION_SYNC_PENDING - broker position sync failed; keeping previous local position"
            + (f" — cause: {sync_error}" if sync_error else "")
        )
        return state

    pos_info = position_manager.current_position
    state["position_sync_status"] = "SYNCED"
    state["position_sync_block_new_orders"] = False
    state["position_sync_error"] = None
    state["position_sync_warning"] = None
    state["position_sync_last_ok_at"] = kst_now().isoformat()
    state["position_sync_last_position"] = dict(pos_info or {})
    state["position_conflict"] = bool(pos_info.get("conflict"))
    if state["position_conflict"]:
        state["critical_alert"] = position_manager.conflict_error
        logger.error("[SwitchPositionManager] %s", position_manager.conflict_error)
        return state

    # 브로커 동기화가 정상 회복됐다 — 이전에 남아있던 POSITION_SYNC_PENDING류 stale
    # critical_alert를 지운다. 그렇지 않으면 과거 1회성 실패로 세팅된 "🔴 CRITICAL"
    # 배너가 이후 몇 번이고 정상 동기화에 성공해도 화면에 영구히 남는다(2026-07-16
    # 사용자 리포트 — position_sync_status/error는 이미 정상인데 배너만 계속 표시됨).
    if state.get("critical_alert") and "POSITION_SYNC_PENDING" in str(state.get("critical_alert")):
        state["critical_alert"] = None

    broker_symbol = pos_info.get("symbol")
    existing = state.get("position") or {}
    state_symbol = existing.get("symbol")

    # 요구사항(2026-07-16) — 매 사이클 KIS 실보유수량과 원장 순수량을 비교하고,
    # state.position을 실제로 갱신하기 전에 먼저 원장을 맞춘다. 그렇지 않으면
    # "브로커에는 129주 실보유가 확인되는데 원장 매수/매도/총체결은 0건"인 상황이
    # 다음 사이클로도 계속 넘어간다. 대상 심볼: 브로커가 지금 보고하는 심볼 +
    # (다른 심볼을 들고 있다가 방금 청산되어 사라졌다면) 그 이전 심볼도 함께 맞춘다.
    reconcile_targets = set()
    if broker_symbol:
        reconcile_targets.add(broker_symbol)
    if state_symbol and state_symbol != broker_symbol:
        reconcile_targets.add(state_symbol)
    if reconcile_targets:
        _reconcile_ledger_with_kis(state, position_manager, pos_info, existing, reconcile_targets)

    if broker_symbol == state_symbol:
        if broker_symbol is not None:
            existing["quantity"] = pos_info.get("quantity")
            existing["avg_price"] = pos_info.get("avg_price")
            # 요구사항(2026-07-16 실측) — 브로커가 보고하는 평단(avg_price)이 이
            # 포지션의 손익 계산 기준 진실이다(추가매수/부분체결/수수료 반영 등으로
            # 로컬에서 계산한 값과 어긋날 수 있음). entry_price를 여기서 함께
            # 갱신하지 않으면 evaluate_tp_sl()/dynamic_exit_engine.decide()가 계속
            # 예전(보통 더 낮은) entry_price로 손익률을 계산해 실제로는 손절선을
            # 넘은 손실도 문턱을 못 넘은 것처럼 보고한다.
            if pos_info.get("avg_price"):
                existing["entry_price"] = pos_info.get("avg_price")
            state["position"] = existing
        else:
            _clear_stale_buy_state_when_flat(state)
    else:
        logger.warning(
            "[SwitchPositionManager] state/broker position mismatch; syncing to broker. state=%s broker=%s",
            state_symbol, broker_symbol,
        )
        if broker_symbol is None:
            state["position"] = _empty_position()
            _clear_stale_buy_state_when_flat(state)
        else:
            state["position"] = {
                **_empty_position(),
                "symbol": broker_symbol, "name": _SYMBOL_NAME.get(broker_symbol, broker_symbol),
                "quantity": pos_info.get("quantity"), "avg_price": pos_info.get("avg_price"),
                "entry_price": pos_info.get("avg_price"),
                "entry_time": kst_now().isoformat(),
            }

    # Trade count: when the broker itself tracks it (e.g. DryRunBroker), that value always
    # takes priority over log aggregation.
    if hasattr(position_manager.broker, "get_executed_order_count"):
        state["daily_trade_count"] = position_manager.trade_count
    return state


def run_liquidation_if_needed(
    now: datetime, state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    position_manager=None,
) -> dict:
    """After 15:15, force-liquidate the full held position (regardless of profit/loss, ahead of TP/SL).

    If there's no held position, there's nothing to liquidate, so liquidation_done=True is set
    immediately (past bug: with no held position, this function was never called at all, so the
    UI kept showing "forced liquidation not done" even though there was nothing to liquidate — if
    the stop-loss mode isn't AUTO (ALERT_ONLY/BATCH_MANUAL), no auto-sell happens and only an
    alert is logged; in that case liquidation_done stays False while a position remains). On
    failure, retries once; if the retry also fails, records a critical_alert (position kept as
    is). Every attempt is logged to data/logs/forced_liquidation_log.csv.
    """
    from app.trading.hynix_stop_loss_control import (
        apply_stop_loss_mode_gate, log_forced_liquidation_event, verify_order_confirmed,
    )

    orders: list = []
    position = state.get("position") or {}
    symbol = position.get("symbol")
    mode = state.get("mode", "mock")

    if not should_liquidate_now(now):
        return {"liquidated": False, "orders": orders}

    if position_manager is not None:
        try:
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            position = state.get("position") or {}
            symbol = position.get("symbol")
        except Exception as exc:
            state["liquidation_done"] = False
            state["position_sync_status"] = POSITION_SYNC_PENDING
            state["position_sync_block_new_orders"] = True
            state["critical_alert"] = f"POSITION_SYNC_PENDING - 15:15 liquidation balance check failed: {exc}"
            return {"liquidated": False, "orders": orders, "position_sync_pending": True}

    if not symbol or (position.get("quantity") or 0) <= 0:
        state["liquidation_done"] = True
        return {"liquidated": False, "orders": orders, "already_empty": True}

    current_price = _current_price(symbol, hynix_price, inverse_price)
    quantity = position.get("quantity")
    entry_price = position.get("entry_price")

    gate = apply_stop_loss_mode_gate(state, mode, symbol, position_manager, "FORCED_LIQUIDATION", current_price, now)
    if gate["blocked"]:
        state["liquidation_done"] = False
        log_forced_liquidation_event({
            "mode": mode, "symbol": symbol, "quantity": quantity, "entry_price": entry_price,
            "current_price": current_price, "liquidation_attempted": False, "order_sent": False,
            "order_confirmed": False, "result": "BLOCKED_MANUAL_MODE", "reason": gate["reason"],
        })
        return {"liquidated": False, "orders": orders, "blocked_by_mode": True}

    if not current_price:
        state["liquidation_done"] = False
        state["critical_alert"] = f"[{now.isoformat()}] forced liquidation time reached but no current price - keeping position"
        logger.error(state["critical_alert"])
        log_forced_liquidation_event({
            "mode": mode, "symbol": symbol, "quantity": quantity, "entry_price": entry_price,
            "current_price": None, "liquidation_attempted": True, "order_sent": False,
            "order_confirmed": False, "result": "NO_PRICE", "reason": "current price query failed",
        })
        return {"liquidated": False, "orders": orders}

    for attempt in (1, 2):
        result = _sell_all_or_ratio(
            broker, position, current_price, 1.0, "15:15 end-of-day forced liquidation", orders,
            mode=mode, exit_reason_type="liquidation", signal_source="FORCED_LIQUIDATION",
            position_manager=position_manager,
        )
        if result.get("success"):
            net_realized, gross_realized = _resolve_realized_pnl(
                result, current_price, position.get("entry_price", current_price),
                result.get("sold_quantity", position.get("quantity", 0)),
            )
            state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + net_realized
            state["gross_realized_pnl_today_krw"] = state.get("gross_realized_pnl_today_krw", 0.0) + gross_realized
            state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
            _apply_sell_result_to_state_position(state, position, result)
            state["last_sell_price"] = current_price
            state["last_trade_time"] = now.isoformat()
            state["last_action"] = "SELL"
            state["last_order_id"] = result.get("order_id")
            state["critical_alert"] = None

            order_confirmed = True
            if position_manager is not None:
                order_confirmed = verify_order_confirmed(position_manager, symbol, expect_cleared=True)
            state["liquidation_done"] = bool(order_confirmed and (state.get("position") or {}).get("symbol") is None)

            log_forced_liquidation_event({
                "mode": mode, "symbol": symbol, "quantity": quantity, "entry_price": entry_price,
                "current_price": current_price, "liquidation_attempted": True, "order_sent": True,
                "order_confirmed": order_confirmed,
                "result": "SUCCESS" if order_confirmed else "UNCONFIRMED",
                "reason": "15:15 end-of-day forced liquidation" + ("" if order_confirmed else " - fill unconfirmed, needs verification"),
            })
            return {"liquidated": True, "orders": orders, "attempts": attempt, "order_confirmed": order_confirmed}
        logger.warning("[SwitchPositionManager] forced liquidation attempt %s failed: %s", attempt, result.get("message"))

    state["liquidation_done"] = False
    failure_message = orders[-1].get("message") if orders else "no message"
    state["critical_alert"] = f"[{now.isoformat()}] forced liquidation failed on both retries: {failure_message}"
    logger.error(state["critical_alert"])
    log_forced_liquidation_event({
        "mode": mode, "symbol": symbol, "quantity": quantity, "entry_price": entry_price,
        "current_price": current_price, "liquidation_attempted": True, "order_sent": False,
        "order_confirmed": False, "result": "FAILED", "reason": f"both retries failed: {failure_message}",
    })
    return {"liquidated": False, "orders": orders}


def _empty_position() -> dict:
    return {
        "symbol": None, "name": None, "quantity": 0, "avg_price": None, "entry_price": None,
        "entry_time": None, "partial_tp1_done": False, "partial_sl1_done": False,
        "highest_price": None, "lowest_price": None,
        "trailing_armed": False, "trailing_peak_price": None,
        "profit_lock_peak_pct": 0.0,
    }


def _apply_sell_result_to_state_position(state: dict, position: dict, sell_result: dict, *, mark_partial: Optional[str] = None) -> None:
    remaining = sell_result.get("remaining_quantity")
    status = sell_result.get("position_sync_status")
    if status == POSITION_SYNC_PENDING or remaining is None:
        _mark_position_sync_pending(
            state, position, (sell_result.get("position_sync") or {}).get("error"),
            "POSITION_SYNC_PENDING - broker balance confirmation failed after sell; new orders blocked",
        )
        return

    remaining = int(remaining or 0)
    state["position_sync_status"] = "SYNCED"
    state["position_sync_block_new_orders"] = False
    if remaining <= 0:
        state["position"] = _empty_position()
        _clear_stale_buy_state_when_flat(state)
        return

    position["quantity"] = remaining
    avg_price = (sell_result.get("position_sync") or {}).get("avg_price")
    if avg_price:
        position["avg_price"] = avg_price
        # entry_price도 함께 맞춘다 — evaluate_tp_sl()/dynamic_exit_engine이 손익률
        # 계산에 entry_price를 쓰므로, 브로커가 보고한 평단과 어긋나면 안 된다.
        position["entry_price"] = avg_price
    if mark_partial == "tp1":
        position["partial_tp1_done"] = True
    elif mark_partial == "sl1":
        position["partial_sl1_done"] = True
    position["position_sync_status"] = "SYNCED"
    state["position"] = position


def run_tp_sl_if_needed(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    position_manager=None, now: Optional[datetime] = None,
) -> dict:
    """Evaluate and execute TP/SL for the held position (runs after forced-liquidation checks,
    before switch-decision entry).

    If the Dynamic Exit AI watcher thread is running, legacy TP/SL is skipped entirely — two
    systems using different thresholds (legacy -0.8% vs Dynamic Exit AI's dynamic -1.2%) would
    otherwise judge and sell the same position at the same time, risking duplicate sells. This
    function's own watcher-status check is only a real fallback for when the watcher thread died
    unnoticed.

    SL triggers must pass the stop-loss-mode (AUTO/ALERT_ONLY/BATCH_MANUAL) gate before an actual
    sell fires; TP triggers run regardless of the stop-loss mode setting.
    """
    now = now or kst_now()
    orders: list = []
    position = state.get("position") or {}
    symbol = position.get("symbol")
    if not symbol or (position.get("quantity") or 0) <= 0:
        return {"triggered": False, "orders": orders}

    try:
        from app.trading.dynamic_exit_watcher import is_watcher_running

        if is_watcher_running():
            return {"triggered": False, "orders": orders, "skipped_reason": "Dynamic Exit AI watcher is active; legacy TP/SL is a fallback and was skipped"}
    except Exception as exc:
        logger.debug("[SwitchPositionManager] watcher status check failed, continuing with legacy TP/SL: %s", exc)

    mode = state.get("mode", "mock")
    current_price = _current_price(symbol, hynix_price, inverse_price)
    from app.trading.adaptive_market_regime import effective_sl_pct_for_position

    confirmed_regime = (state.get("adaptive_regime") or {}).get("confirmed_regime")
    hard_sl_pct = effective_sl_pct_for_position(confirmed_regime, symbol)
    trigger = evaluate_tp_sl(position, current_price, hard_sl_pct=hard_sl_pct)
    if not trigger:
        return {"triggered": False, "orders": orders}

    if trigger["tag"].startswith("sl"):
        from app.trading.hynix_stop_loss_control import apply_stop_loss_mode_gate

        gate = apply_stop_loss_mode_gate(state, mode, symbol, position_manager, "LEGACY_TP_SL", current_price, now)
        if gate["blocked"]:
            return {"triggered": True, "executed": False, "blocked_by_mode": True, "orders": orders}

    exit_reason_type = "stop_loss" if trigger["tag"].startswith("sl") else "take_profit"
    result = _sell_all_or_ratio(
        broker, position, current_price, trigger["ratio"], trigger["reason"], orders,
        mode=mode, exit_reason_type=exit_reason_type, position_manager=position_manager,
    )
    if not result.get("success"):
        return {"triggered": True, "executed": False, "orders": orders, "blocked_by_coordinator": result.get("blocked_by_coordinator", False)}

    sold_qty = result.get("sold_quantity", 0)
    net_realized, gross_realized = _resolve_realized_pnl(
        result, current_price, position.get("entry_price", current_price), sold_qty,
    )
    state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + net_realized
    state["gross_realized_pnl_today_krw"] = state.get("gross_realized_pnl_today_krw", 0.0) + gross_realized
    state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
    state["last_sell_price"] = current_price
    state["last_trade_time"] = kst_now().isoformat()
    state["last_action"] = "SELL"
    state["last_order_id"] = result.get("order_id")

    _apply_sell_result_to_state_position(
        state, position, result,
        mark_partial=trigger["tag"] if trigger["tag"] in ("tp1", "sl1") else None,
    )

    return {"triggered": True, "executed": True, "orders": orders}


def _held_symbol_confirmed(state: dict, broker, desired_symbol: str, position_manager=None) -> tuple[Optional[str], dict]:
    """Resolve the currently-held symbol, refusing to trust a stale/pending local flag.

    If position_sync_status is POSITION_SYNC_PENDING (or otherwise not confirmed SYNCED),
    a fresh broker resync is forced before any "already holding X" decision is made — the
    local `state["position"]` dict is never treated as ground truth on its own while a sync
    is pending. If the resync still can't confirm, or if it reveals a different holding
    than local state expected (UI/engine mismatch), new orders are blocked and the caller
    is told to wait for the next sync instead of silently trusting the old flag.
    """
    position = state.get("position") or {}
    sync_status = state.get("position_sync_status")
    if sync_status == POSITION_SYNC_PENDING:
        local_symbol = position.get("symbol")
        resync = _resync_position_from_broker(state, broker, position_manager=position_manager)
        position = state.get("position") or {}
        if not resync.get("ok"):
            return None, {
                "blocked": True,
                "message": "POSITION_SYNC_PENDING - broker holdings unconfirmed; new orders blocked until resync succeeds",
            }
        broker_symbol = position.get("symbol")
        if local_symbol != broker_symbol:
            return None, {
                "blocked": True,
                "message": (
                    f"position mismatch detected during resync (local={local_symbol!r} vs broker={broker_symbol!r}); "
                    "orders blocked, state resynced to broker"
                ),
            }
    return position.get("symbol"), {"blocked": False}


def run_switch_or_entry(
    state: dict, broker, final_action: str, hynix_price: Optional[float], inverse_price: Optional[float],
    now: Optional[datetime] = None, forced: bool = False, reason: str = "", position_manager=None,
    target_position_pct: Optional[float] = None, entry_type: Optional[str] = None,
    stop_loss_pct: Optional[float] = None, signal_source: str = SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH,
) -> dict:
    """Execute switching or a new entry. After 14:50, only sells (no buying a new symbol) fire.

    signal_source(요구사항 2026-07-20) — 이 함수 내부의 모든 buy/sell 호출(top-up,
    switch-sell, fresh-entry)에 그대로 전달되어 원장에 기록된다. 이 파라미터가
    없던 예전에는 호출부가 무엇을 넘기든 내부에서 _buy_new/_sell_all_or_ratio의
    기본값(SIGNAL_SOURCE_ENHANCED_REGIME_SWITCH)으로 고정 기록돼, Early Trend
    Detector가 만든 주문도 원장에는 전부 ENHANCED_REGIME_SWITCH로만 남았다."""
    now = now or kst_now()
    orders: list = []
    desired_symbol = _ACTION_TO_SYMBOL.get(final_action)
    trend_plan = state.get("last_trend_switch_plan") or {}
    if trend_plan.get("desired_symbol") == desired_symbol and trend_plan.get("proceed"):
        if target_position_pct is None:
            target_position_pct = trend_plan.get("position_pct")
        if entry_type is None:
            entry_type = trend_plan.get("entry_type")
        if stop_loss_pct is None:
            stop_loss_pct = trend_plan.get("stop_loss_pct")
    if desired_symbol is None:
        return {"acted": False, "orders": orders, "message": "HOLD - no new entry/switch", "stage": "entry"}

    weighted_controller_entry = (
        entry_type in ("WEIGHTED_RANGE_ENTRY", "WEIGHTED_ORDER_CONTROLLER", "WEIGHTED_ORDER_CONTROLLER_SCALE_IN")
        or signal_source in ("WEIGHTED_RANGE_ENTRY", WEIGHTED_ORDER_CONTROLLER_SOURCE)
    )
    if state.get("weighted_entry_controller_only") and not weighted_controller_entry:
        return {
            "acted": False,
            "orders": orders,
            "message": "weighted order controller owns new entries; legacy source blocked",
            "stage": "entry",
            "failure_code": "LEGACY_ENTRY_SOURCE_BLOCKED",
            "requested_symbol": desired_symbol,
        }

    bucket = _cycle_bucket(now)
    signature = f"{final_action}:{desired_symbol}"
    if state.get("last_order_cycle_bucket") == bucket and state.get("last_order_signature") == signature:
        return {"acted": False, "orders": orders, "message": "same signal within same 3-minute cycle; duplicate order blocked", "stage": "entry"}

    # 요구사항(2026-07-16) — Entry Approved=YES까지 도달했더라도 이미 처리 중인
    # 주문이 있으면(원인 불명 재시도/중복 사이클 등) 새 주문을 또 보내지 않는다.
    # order_sent 실패사유를 정확히 ORDER_IN_FLIGHT로 남긴다.
    if state.get("order_in_flight") or state.get("pending_order"):
        return {
            "acted": False, "orders": orders, "message": "order already in flight for this position",
            "stage": "order_sent", "failure_code": ORDER_FAILURE_ORDER_IN_FLIGHT,
            "pending_order": True, "requested_symbol": desired_symbol,
        }

    # Buy cooldown must be determined from the previous trade record before "already holding
    # this cycle" overwrites that value.
    buy_cooldown_active = is_buy_cooldown_active(state.get("last_trade_time"), state.get("last_action"), now)

    held_symbol, sync_check = _held_symbol_confirmed(state, broker, desired_symbol, position_manager=position_manager)
    if sync_check.get("blocked"):
        return {"acted": False, "orders": orders, "message": sync_check["message"], "stage": "state_sync"}
    position = state.get("position") or {}

    if held_symbol == desired_symbol:
        current_price = _current_price(desired_symbol, hynix_price, inverse_price)
        # 요구사항(Early Trend Detector 통합) — 여기서는 trend_plan.get("entry_type")
        # (state["last_trend_switch_plan"], 레거시 accelerator 전용 캐시)가 아니라
        # 이 함수 상단에서 이미 trend_plan 폴백까지 반영된 로컬 entry_type을 확인한다
        # — 호출부가 entry_type을 명시적으로 넘기면(예: Early Trend Detector의 단계
        # 진행/확대 top-up) 그 값 그대로 게이트를 통과해야 하며, entry_type이 None이라
        # trend_plan에서 채워진 경우도 값이 동일해 기존 동작과 차이가 없다.
        # "EARLY_PROBE"는 문자열로만 참조한다(hynix_switch_position_manager.py가
        # early_trend_detector.py를 임포트하지 않도록 결합도를 낮춘다).
        if target_position_pct and current_price and entry_type in (
            "NORMAL", "CONFIRMED", "EARLY_PROBE", "WEIGHTED_ORDER_CONTROLLER_SCALE_IN"
        ):
            full_cash, cash_source = _query_buyable_cash(
                broker, symbol=desired_symbol, current_price=current_price, state=state,
            )
            state["last_buyable_cash_source"] = cash_source
            state["last_buyable_cash_used"] = full_cash
            if full_cash <= 0:
                return {
                    "acted": False, "orders": orders, "message": "buyable cash unavailable", "stage": "entry",
                    "failure_code": ORDER_FAILURE_BUYABLE_CASH_ZERO, "buyable_cash": full_cash,
                    "requested_symbol": desired_symbol,
                }
            pct = float(target_position_pct)
            if pct > 1.0:
                pct = pct / 100.0
            held_value = float(position.get("quantity") or 0) * float(current_price)
            target_value = (full_cash + held_value) * max(0.0, min(1.0, pct))
            add_cash = max(0.0, target_value - held_value)
            if add_cash >= current_price:
                buy_result = _buy_new(
                    broker, desired_symbol, current_price, add_cash,
                    f"TrendSwitchAccel target-weight increase {pct * 100:.0f}%", orders,
                    mode=state.get("mode", "mock"), before_qty=int(position.get("quantity") or 0),
                    position_manager=position_manager, signal_source=signal_source,
                )
                if buy_result.get("success"):
                    if buy_result.get("position_sync_status") == POSITION_SYNC_PENDING:
                        _mark_position_sync_pending(
                            state, position, (buy_result.get("position_sync") or {}).get("error"),
                            "POSITION_SYNC_PENDING - broker balance confirmation failed after buy; new orders blocked",
                        )
                        return {
                            "acted": True, "orders": orders, "message": buy_result.get("message", "buy fill unconfirmed"),
                            "stage": "state_sync", "requested_symbol": desired_symbol,
                            "requested_qty": buy_result.get("requested_qty"), "order_price": buy_result.get("order_price"),
                            "sized_cash": buy_result.get("sized_cash"), "buyable_cash": full_cash,
                        }
                    add_qty = int(buy_result.get("bought_quantity", 0))
                    old_qty = int(position.get("quantity") or 0)
                    new_qty = old_qty + add_qty
                    old_avg = float(position.get("avg_price") or position.get("entry_price") or current_price)
                    new_avg = ((old_avg * old_qty) + (current_price * add_qty)) / new_qty if new_qty else current_price
                    position["quantity"] = new_qty
                    position["avg_price"] = new_avg
                    # 요구사항(2026-07-16 실측) — entry_price도 함께 갱신해야 한다.
                    # evaluate_tp_sl()/dynamic_exit_engine.decide()는 손익률을
                    # avg_price가 아니라 entry_price 기준으로 계산하는데, 목표비중
                    # 증액(top-up) 시 entry_price를 그대로 두면 최초 진입가(보통 더
                    # 낮은 가격)로 손익을 계산하게 되어 실제 평단 대비 손실이 커도
                    # 손절 문턱을 넘지 못한 것처럼 보인다(2026-07-16 사용자 리포트:
                    # 실제 -3.48% 손실인데 자동손절이 전혀 발동하지 않음).
                    position["entry_price"] = new_avg
                    position["entry_type"] = entry_type or position.get("entry_type") or "NORMAL"
                    position["stop_loss_pct"] = stop_loss_pct if stop_loss_pct is not None else position.get("stop_loss_pct")
                    state["position"] = position
                    state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
                    state["last_buy_price"] = current_price
                    state["last_trade_time"] = now.isoformat()
                    state["last_action"] = "BUY"
                    state["last_order_id"] = buy_result.get("order_id")
                    state["last_order_cycle_bucket"] = bucket
                    state["last_order_signature"] = signature
                    return {
                        "acted": True, "orders": orders, "message": buy_result.get("message", "target-weight increase"),
                        "stage": "order_sent", "requested_symbol": desired_symbol,
                        "requested_qty": buy_result.get("requested_qty"), "order_price": buy_result.get("order_price"),
                        "sized_cash": buy_result.get("sized_cash"), "buyable_cash": full_cash,
                    }
                # 요구사항(2026-07-16 사용자 리포트) — 여기서 그냥 아래 "이미 보유 중"
                # 문구로 흘러가면 실제 매수 시도가 실패한 진짜 이유(예: "sized cash
                # amount is 0", KIS rt_cd/msg_cd 거부 사유)가 통째로 사라지고, 마치
                # "이미 보유해서 매수할 필요 없음"인 것처럼 보인다 — 이건 시도했다가
                # 실패한 것이지 "필요 없어서 안 한 것"이 아니다.
                return {
                    "acted": True, "orders": orders,
                    "message": buy_result.get("message") or "target-weight increase buy failed",
                    "stage": "order_sent", "requested_symbol": desired_symbol,
                    "requested_qty": buy_result.get("requested_qty"), "order_price": buy_result.get("order_price"),
                    "sized_cash": buy_result.get("sized_cash"), "buyable_cash": full_cash,
                    "failure_code": buy_result.get("failure_code"), "broker_error": buy_result.get("broker_error"),
                }
            # add_cash가 1주 가격보다 작아 애초에 매수를 시도하지 않은 경우도 "이미
            # 보유 중 — 중복 매수 방지"로 뭉뚱그리면 원인(목표비중 증액분이 1주 미만)을
            # 알 수 없다.
            return {
                "acted": False, "orders": orders,
                "message": (
                    f"target-weight increase skipped: add_cash {add_cash:,.0f} < "
                    f"1-share price {current_price:,.0f} (already close to target weight)"
                ),
                "stage": "entry",
            }
        label = "인버스" if desired_symbol == INVERSE_SYMBOL else "하이닉스"
        return {"acted": False, "orders": orders, "message": f"이미 {label} 보유 중 — 중복 매수 방지", "stage": "entry"}

    entry_allowed = is_new_entry_allowed(now)

    if held_symbol:
        current_price = _current_price(held_symbol, hynix_price, inverse_price)
        if not current_price:
            return {"acted": False, "orders": orders, "message": "no current price for held symbol; switch skipped", "stage": "entry"}
        sell_result = _sell_all_or_ratio(
            broker, position, current_price, 1.0, f"switch sell ({reason})", orders,
            mode=state.get("mode", "mock"), exit_reason_type="switch", position_manager=position_manager,
            signal_source=signal_source,
        )
        if not sell_result.get("success"):
            return {"acted": False, "orders": orders, "message": f"switch sell failed: {sell_result.get('message')}", "stage": "order_sent"}
        sold_qty = sell_result.get("sold_quantity", position.get("quantity", 0))
        net_realized, gross_realized = _resolve_realized_pnl(
            sell_result, current_price, position.get("entry_price", current_price), sold_qty,
        )
        state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + net_realized
        state["gross_realized_pnl_today_krw"] = state.get("gross_realized_pnl_today_krw", 0.0) + gross_realized
        state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
        try:
            from app.trading.hynix_trend_switch_accelerator import register_round_trip_closed

            state["trend_switch_frequency_state"] = register_round_trip_closed(
                state.get("trend_switch_frequency_state"), bool(net_realized < 0), now,
            )
        except Exception as exc:
            logger.debug("[SwitchPositionManager] trend frequency sell update skipped: %s", exc)
        state["last_sell_price"] = current_price
        state["last_trade_time"] = now.isoformat()
        state["last_action"] = "SELL"
        state["last_order_id"] = sell_result.get("order_id")

        # Only enter the opposite position once the existing position's sell is actually
        # confirmed filled. An accepted-order (rt_cd=0) response alone isn't enough — if
        # position_manager's resync shows remaining_quantity isn't 0, treat it as
        # unfilled/partial and defer the opposite buy to a later cycle.
        remaining_after_sell = sell_result.get("remaining_quantity")
        sell_confirmed = sell_result.get("position_sync_status") == "SYNCED" and int(remaining_after_sell or 0) <= 0
        if not sell_confirmed:
            _apply_sell_result_to_state_position(state, position, sell_result)
            state["last_order_cycle_bucket"] = bucket
            state["last_order_signature"] = signature
            return {
                "acted": True,
                "orders": orders,
                "message": "switch sell not confirmed; opposite buy deferred until broker balance sync",
                "stage": "state_sync",
            }
        _apply_sell_result_to_state_position(state, position, sell_result)

        if not entry_allowed:
            state["last_order_cycle_bucket"] = bucket
            state["last_order_signature"] = signature
            return {"acted": True, "orders": orders, "message": "after 14:50 - sell only, no new symbol entry", "stage": "entry"}

    if not entry_allowed:
        return {
            "acted": bool(orders), "orders": orders, "message": "new entry window closed", "stage": "entry",
            "requested_symbol": desired_symbol,
        }

    if buy_cooldown_active:
        cooldown_remaining = None
        try:
            last_dt = datetime.fromisoformat(str(state.get("last_trade_time")))
            cooldown_remaining = max(0, int(MIN_SECONDS_BETWEEN_BUYS - (now - last_dt).total_seconds()))
        except Exception:
            cooldown_remaining = None
        return {
            "acted": bool(orders), "orders": orders, "message": "buy cooldown active", "stage": "entry",
            "failure_code": ORDER_FAILURE_COOLDOWN_ACTIVE, "cooldown_remaining": cooldown_remaining,
            "requested_symbol": desired_symbol,
        }

    current_price = _current_price(desired_symbol, hynix_price, inverse_price)
    if not current_price:
        return {
            "acted": bool(orders), "orders": orders, "message": "no current price for target symbol; buy skipped",
            "stage": "entry", "failure_code": ORDER_FAILURE_PRICE_UNAVAILABLE, "requested_symbol": desired_symbol,
        }

    # 요구사항(2026-07-20) — 방향판단(000660/Adaptive Regime)과 주문실행 데이터
    # (0193T0/0197X0 실제 ETF)를 분리한다. 000660 신호만으로 ETF 주문을 내보내지
    # 않는다 — 실제 매수 직전 그 ETF 자신의 1분봉으로 재확인하고, 데이터가
    # 없거나 오래됐으면 ETF_DATA_INSUFFICIENT로 fail-closed 차단한다. 모든
    # 신규진입 경로(ENHANCED_REGIME_SWITCH/Early Trend Detector/Active Strategy/
    # Fast Watcher)가 이 run_switch_or_entry() 하나를 거치므로, 여기 한 곳에서만
    # 확인해도 전체에 동일 적용된다.
    try:
        _weighted_controller_entry = (
            entry_type in ("WEIGHTED_RANGE_ENTRY", "WEIGHTED_ORDER_CONTROLLER", "WEIGHTED_ORDER_CONTROLLER_SCALE_IN")
            or signal_source in ("WEIGHTED_RANGE_ENTRY", WEIGHTED_ORDER_CONTROLLER_SOURCE)
        )
        if _weighted_controller_entry:
            state["last_etf_entry_confirmation"] = {
                "approved": True,
                "state": "WEIGHTED_CONTROLLER_APPROVED",
                "reason": "ETF/live/profitability gates already evaluated by evaluate_range_weighted_entry",
            }
            raise StopIteration
        _etf_direction = "DOWN" if desired_symbol == INVERSE_SYMBOL else "UP"
        _other_symbol = LONG_SYMBOL if desired_symbol == INVERSE_SYMBOL else INVERSE_SYMBOL

        # 요구사항(2026-07-21 실측 버그 수정) — 아래 confirm_etf_entry()는 1분봉
        # 종가 단 1개로 방향을 근사해, 000660이 30분 넘게 상승 중이어도 그 1분봉
        # 하나가 잠깐 눌리면 즉시 ETF_DIRECTION_MISMATCH로 신규진입 전체를
        # 막았다(Early Trend Detector 5초 피드가 이미 만들어 둔 live_slopes가
        # 있으면 그 5/10/20/30초 다중 구간+VWAP+swing 기준으로 재확인하고,
        # ALIGNED_PULLBACK(일시 눌림)까지 허용한다). live_slopes가 아직 없는
        # 구성(Early Trend Detector 비활성 등)에서는 기존 1분봉 기준
        # confirm_etf_entry()로 안전하게 폴백한다 — 이 폴백 경로 자체는 그대로 둔다.
        _etd_state = state.get("early_trend_detector") or {}
        _live_slopes = _etd_state.get("live_slopes") or {}
        _dt_symbols = (_etd_state.get("data_time_status") or {}).get("symbols") or {}
        _has_live_slope_data = has_any_slope_data(_live_slopes.get(desired_symbol))

        if _has_live_slope_data:
            from app.trading.etf_entry_confirmation import compute_etf_breakouts
            from app.data_sources.hynix_long_collector import _load_long_minute_cache
            from app.data_sources.hynix_inverse_collector import _load_inverse_minute_cache

            _confirm_df = _load_long_minute_cache() if desired_symbol == LONG_SYMBOL else _load_inverse_minute_cache()
            _confirm_breakouts = compute_etf_breakouts(_confirm_df, current_price, _etf_direction)
            _swing_broken = None
            if _confirm_df is not None:
                if _etf_direction == "UP" and _confirm_breakouts.get("recent_low"):
                    _swing_broken = current_price < _confirm_breakouts["recent_low"]
                elif _etf_direction == "DOWN" and _confirm_breakouts.get("recent_high"):
                    _swing_broken = current_price > _confirm_breakouts["recent_high"]
            _etf_confirmation = classify_etf_direction_confirmation(
                direction=_etf_direction,
                signal_direction=(_live_slopes.get(SIGNAL_SYMBOL) or {}).get("direction"),
                confirm_window_directions=resolve_window_directions(_live_slopes.get(desired_symbol)),
                oppose_window_directions=resolve_window_directions(_live_slopes.get(_other_symbol)),
                confirm_above_vwap=_confirm_breakouts.get("vwap_breakout"),
                confirm_swing_broken_against=_swing_broken,
                structural_direction=(state.get("last_primary_trend") or {}).get("primary_trend"),
                data_ages_seconds=(
                    {
                        "signal": (_dt_symbols.get(SIGNAL_SYMBOL) or {}).get("age_seconds"),
                        "confirm": (_dt_symbols.get(desired_symbol) or {}).get("age_seconds"),
                        "oppose": (_dt_symbols.get(_other_symbol) or {}).get("age_seconds"),
                    }
                    if _dt_symbols else None
                ),
            )
            state["last_etf_entry_confirmation"] = _etf_confirmation
            if _etf_confirmation["state"] not in (ETF_CONFIRM_UP, ETF_CONFIRM_DOWN, ALIGNED_PULLBACK, ETF_CONFIRMATION_PENDING):
                return {
                    "acted": bool(orders), "orders": orders,
                    "message": f"ETF entry confirmation failed: {_etf_confirmation['reason']}",
                    "stage": "entry", "failure_code": _etf_confirmation["state"], "requested_symbol": desired_symbol,
                    "etf_confirmation": _etf_confirmation,
                }
        else:
            _etf_confirmation = confirm_etf_entry(
                symbol=desired_symbol, underlying_direction=_etf_direction, current_price=current_price,
                mode=state.get("mode", "mock"),
            )
            state["last_etf_entry_confirmation"] = _etf_confirmation
            if not _etf_confirmation["approved"]:
                return {
                    "acted": bool(orders), "orders": orders,
                    "message": f"ETF entry confirmation failed: {_etf_confirmation['reason']}",
                    "stage": "entry", "failure_code": _etf_confirmation["block_code"], "requested_symbol": desired_symbol,
                    "etf_confirmation": _etf_confirmation,
                }
    except StopIteration:
        pass
    except Exception as exc:
        logger.error("[SwitchPositionManager] ETF 진입 확인 실패(안전을 위해 이번 진입은 보류): %s", exc)
        return {
            "acted": bool(orders), "orders": orders, "message": f"ETF entry confirmation error: {exc}",
            "stage": "entry", "failure_code": "ETF_DATA_INSUFFICIENT", "requested_symbol": desired_symbol,
        }

    sized_cash, full_cash = _sizing_cash_amount(
        broker, forced, target_position_pct=target_position_pct,
        symbol=desired_symbol, current_price=current_price, state=state,
    )
    buy_reason = f"new entry/switch buy ({reason})"
    if int(sized_cash // current_price) < 1 and full_cash >= current_price:
        cash_amount = current_price  # sized amount rounds to <1 share but buyable cash covers 1 - guarantee at least 1 share
        buy_reason += " [sized amount insufficient for 1 share - bumped to minimum 1 share]"
    else:
        cash_amount = sized_cash

    buy_result = _buy_new(
        broker, desired_symbol, current_price, cash_amount, buy_reason, orders,
        mode=state.get("mode", "mock"), position_manager=position_manager, signal_source=signal_source,
    )
    if buy_result.get("success"):
        if buy_result.get("position_sync_status") == POSITION_SYNC_PENDING:
            _mark_position_sync_pending(
                state, state.get("position"), (buy_result.get("position_sync") or {}).get("error"),
                "POSITION_SYNC_PENDING - broker balance confirmation failed after buy; new orders blocked",
            )
            state["last_order_cycle_bucket"] = bucket
            state["last_order_signature"] = signature
            return {
                "acted": True, "orders": orders, "message": buy_result.get("message", "buy fill unconfirmed"),
                "stage": "state_sync", "requested_symbol": desired_symbol, "requested_qty": buy_result.get("requested_qty"),
                "order_price": buy_result.get("order_price"), "sized_cash": buy_result.get("sized_cash"),
                "buyable_cash": full_cash,
            }
        qty = buy_result.get("actual_quantity") or buy_result.get("bought_quantity", 0)
        avg_price = ((buy_result.get("position_sync") or {}).get("avg_price")) or current_price
        state["position"] = {
            **_empty_position(),
            "symbol": desired_symbol, "name": _SYMBOL_NAME.get(desired_symbol, desired_symbol),
            "quantity": qty, "avg_price": avg_price, "entry_price": avg_price,
            "entry_time": now.isoformat(),
            "entry_type": entry_type or ("FORCED" if forced else "NORMAL"),
            "stop_loss_pct": stop_loss_pct,
        }
        state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
        try:
            from app.trading.hynix_trend_switch_accelerator import register_frequency_entry, signal_direction

            state["trend_switch_frequency_state"] = register_frequency_entry(
                state.get("trend_switch_frequency_state"), signal_direction(final_action), now,
            )
        except Exception as exc:
            logger.debug("[SwitchPositionManager] trend frequency buy update skipped: %s", exc)
        state["last_buy_price"] = current_price
        state["last_trade_time"] = now.isoformat()
        state["last_action"] = "BUY"
        state["last_order_id"] = buy_result.get("order_id")

    if any(bool(o.get("success")) for o in orders if isinstance(o, dict)):
        state["last_order_cycle_bucket"] = bucket
        state["last_order_signature"] = signature
    return {
        "acted": True, "orders": orders, "message": buy_result.get("message", ""), "stage": "order_sent",
        "requested_symbol": desired_symbol, "requested_qty": buy_result.get("requested_qty"),
        "order_price": buy_result.get("order_price"), "sized_cash": buy_result.get("sized_cash"),
        "buyable_cash": full_cash, "failure_code": buy_result.get("failure_code"),
        "broker_error": buy_result.get("broker_error"),
    }


def run_reversal_switch_if_needed(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    now: Optional[datetime] = None, position_manager=None,
    hard_stop_triggered: bool = False, regime_downgraded_to_range: bool = False,
    snapshot: Optional[dict] = None,
    previous_regime: Optional[str] = None, current_regime: Optional[str] = None,
    allow_final_actions: bool = True,
) -> dict:
    """장중 다중 추세전환 상태머신(2026-07-16)의 pending_action을 실제 주문으로
    실행한다. run_switch_or_entry()(신규진입/즉시스위칭)와는 별개의 경로다 —
    STRONG_TREND 보유 중 반전 신호가 쌓일 때 선제적으로 축소·청산하고, 브로커가
    실제 잔량 0을 확인해준 뒤에만 반대 ETF 탐색진입을 허용한다(요구사항
    "잔량 0 확인 전 반대 ETF 주문 금지"). Fast Watcher와 메인 3분 사이클이 모두
    이 함수를 통해서만 반전 스위칭을 실행한다 — Fast Watcher는 이 함수를 호출하는
    것 외에 단독으로 반대 ETF 전액 주문을 넣지 않는다(요구사항7).

    주문이 실패/생략되면(executed_qty<=0) 상태머신의 단계·확인횟수를 이번 호출
    직전 값으로 되돌린다 — evaluate_reversal_switch()는 pending_action을 결정할
    때 단계를 먼저 전진시키므로, 그대로 저장하면 주문 실패 시 그 조치가 영영
    재시도되지 않는다(다음 틱에 같은 단계에서 다시 판단하게 하기 위한 안전장치).
    """
    from app.trading.adaptive_market_regime import (
        evaluate_reversal_switch, mark_reversal_stage_executed, default_reversal_switch_state,
        REVERSAL_ACTION_REDUCE_EXISTING, REVERSAL_ACTION_FULL_EXIT,
        REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE, REVERSAL_ACTION_EXPAND_OPPOSITE,
        REVERSAL_ACTION_REDUCE_TO_CORE,
    )

    now = now or kst_now()
    orders: list = []
    snapshot = snapshot or {}
    rs_state = state.get("reversal_switch") or default_reversal_switch_state()
    pre_rs_state = dict(rs_state)

    position = state.get("position") or {}
    symbol = position.get("symbol")
    qty = int(position.get("quantity") or 0)
    if symbol == LONG_SYMBOL and qty > 0:
        held_direction = "UP"
    elif symbol == INVERSE_SYMBOL and qty > 0:
        held_direction = "DOWN"
    else:
        held_direction = None

    broker_confirmed_flat = False
    kis_flat_check = None
    if rs_state.get("stage") == "FULLY_EXITED":
        if held_direction is None:
            # 요구사항 "잔량 0 확인 전 반대 ETF 주문 금지" — 로컬 캐시가 아니라
            # 브로커 재조회로 실제 청산 여부를 확인한 뒤에만 다음 단계로 넘어간다.
            if position_manager is not None:
                try:
                    position_manager.sync(force=True)
                    apply_position_manager_to_state(state, position_manager)
                    position = state.get("position") or {}
                except Exception as exc:
                    logger.error("[SwitchPositionManager] reversal switch 잔량 재확인 실패: %s", exc)
            remaining_symbol = position.get("symbol")
            remaining_qty = int(position.get("quantity") or 0)
            broker_confirmed_flat = not remaining_symbol or remaining_qty <= 0
            kis_flat_check = {
                "checked_at": now.isoformat(timespec="seconds"),
                "confirmed_flat": broker_confirmed_flat,
                "remaining_symbol": remaining_symbol,
                "remaining_qty": remaining_qty,
            }
        # 청산 완료 대기 중에는(브로커 확인 전이든 후든) 원래 감시하던 방향을 그대로
        # 유지한다 — 지금 실제 보유가 없어(held_direction=None) 감시가 리셋되면 안 된다.
        held_direction = rs_state.get("direction") or held_direction

    result = evaluate_reversal_switch(
        rs_state, held_direction=held_direction, snapshot=snapshot, now=now,
        hard_stop_triggered=hard_stop_triggered, broker_confirmed_flat=broker_confirmed_flat,
        regime_downgraded_to_range=regime_downgraded_to_range,
    )
    pending_action = result.get("pending_action")
    executed_qty = 0
    execution_message = None

    final_only_actions = {
        REVERSAL_ACTION_FULL_EXIT, REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE,
        REVERSAL_ACTION_EXPAND_OPPOSITE,
    }
    if pending_action and not allow_final_actions and pending_action.get("action") in final_only_actions:
        final_rs_state = {**pre_rs_state, "last_updated_at": now.isoformat(timespec="seconds")}
        final_rs_state["opposite_entry_wait_reason"] = "waiting_for_main_adaptive_regime_cycle"
        state["reversal_switch"] = {
            k: v for k, v in final_rs_state.items() if k not in ("pending_action", "votes", "reasons")
        }
        return {
            "pending_action": pending_action, "executed_qty": 0, "orders": orders,
            "message": "Fast Watcher limited to alert/first reduction; final action deferred",
            "reversal_switch": state["reversal_switch"], "votes": result.get("votes"),
            "reasons": result.get("reasons"),
        }

    if pending_action:
        action = pending_action["action"]
        try:
            if action in (REVERSAL_ACTION_REDUCE_EXISTING, REVERSAL_ACTION_FULL_EXIT, REVERSAL_ACTION_REDUCE_TO_CORE):
                sell_position = state.get("position") or {}
                if sell_position.get("symbol") and int(sell_position.get("quantity") or 0) > 0:
                    current_price = _current_price(sell_position["symbol"], hynix_price, inverse_price)
                    if current_price:
                        if action == REVERSAL_ACTION_REDUCE_TO_CORE:
                            sell_ratio = max(0.0, min(1.0, 1.0 - float(pending_action.get("target_ratio", 0.25))))
                        else:
                            sell_ratio = float(pending_action.get("ratio", 1.0))
                        sell_result = _sell_all_or_ratio(
                            broker, sell_position, current_price, sell_ratio,
                            pending_action.get("reason", "reversal switch"), orders,
                            mode=state.get("mode", "mock"), exit_reason_type=f"reversal_switch_{action.lower()}",
                            position_manager=position_manager,
                        )
                        if sell_result.get("success"):
                            sold_qty = sell_result.get("sold_quantity") or 0
                            net_realized, gross_realized = _resolve_realized_pnl(
                                sell_result, current_price, sell_position.get("entry_price", current_price), sold_qty,
                            )
                            state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + net_realized
                            state["gross_realized_pnl_today_krw"] = state.get("gross_realized_pnl_today_krw", 0.0) + gross_realized
                            state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
                            _apply_sell_result_to_state_position(state, sell_position, sell_result)
                            executed_qty = sold_qty
                            execution_message = sell_result.get("message")
                        else:
                            execution_message = sell_result.get("message")
                    else:
                        execution_message = "no current price for held symbol; reversal action skipped"
                else:
                    execution_message = "no position held; reversal action skipped"
            elif action in (REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE, REVERSAL_ACTION_EXPAND_OPPOSITE):
                opposite_symbol = INVERSE_SYMBOL if result.get("direction") == "UP" else LONG_SYMBOL
                current_price = _current_price(opposite_symbol, hynix_price, inverse_price)
                existing_position = state.get("position") or {}
                before_qty = int(existing_position.get("quantity") or 0) if existing_position.get("symbol") == opposite_symbol else 0
                if current_price:
                    target_ratio = pending_action.get("ratio", pending_action.get("target_ratio"))
                    if action == REVERSAL_ACTION_EXPAND_OPPOSITE and before_qty > 0:
                        # 이미 탐색진입된 포지션을 목표비중까지 "증액"한다 — 부족분만
                        # 추가 매수한다(run_switch_or_entry의 target-weight increase와 동일 원칙).
                        full_cash, cash_source = _query_buyable_cash(
                            broker, symbol=opposite_symbol, current_price=current_price, state=state,
                        )
                        pct = float(target_ratio)
                        if pct > 1.0:
                            pct = pct / 100.0
                        held_value = before_qty * current_price
                        target_value = (full_cash + held_value) * max(0.0, min(1.0, pct))
                        add_cash = max(0.0, target_value - held_value)
                        cash_amount = add_cash if add_cash >= current_price else 0.0
                    else:
                        cash_amount, _full_cash = _sizing_cash_amount(
                            broker, False, target_position_pct=target_ratio,
                            symbol=opposite_symbol, current_price=current_price, state=state,
                        )
                    if cash_amount >= current_price:
                        buy_result = _buy_new(
                            broker, opposite_symbol, current_price, cash_amount,
                            pending_action.get("reason", "reversal switch entry"), orders,
                            mode=state.get("mode", "mock"), before_qty=before_qty, position_manager=position_manager,
                        )
                        if buy_result.get("success"):
                            applied = _apply_buy_result_to_state_position(
                                state, opposite_symbol, current_price, buy_result, now=now,
                                previous_position=existing_position if before_qty else None,
                                entry_type=(
                                    "REVERSAL_EXPLORATORY" if action == REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE
                                    else "REVERSAL_EXPANDED"
                                ),
                            )
                            if applied:
                                bought = int(buy_result.get("actual_quantity") or buy_result.get("bought_quantity") or 0)
                                executed_qty = max(0, bought - before_qty) if action == REVERSAL_ACTION_EXPAND_OPPOSITE else bought
                                execution_message = buy_result.get("message")
                        else:
                            execution_message = buy_result.get("message")
                    else:
                        execution_message = "sized cash amount below 1-share price; reversal entry skipped"
                else:
                    execution_message = "no current price for opposite symbol; reversal entry skipped"
        except Exception as exc:
            logger.error("[SwitchPositionManager] reversal switch 실행 실패(action=%s): %s", pending_action.get("action"), exc)
            execution_message = f"reversal switch execution failed: {exc}"

    if pending_action and executed_qty > 0:
        final_rs_state = mark_reversal_stage_executed(
            result, action=pending_action["action"], executed_qty=executed_qty, now=now,
            previous_regime=previous_regime, current_regime=current_regime,
            reasons=result.get("reasons"), kis_balance_check=kis_flat_check,
        )
    elif pending_action:
        # 주문이 실패/생략됐다 — 단계를 전진시키지 않고 이번 호출 직전 상태를 그대로
        # 유지해 다음 틱에 같은 단계에서 재시도되게 한다.
        final_rs_state = {**pre_rs_state, "last_updated_at": now.isoformat(timespec="seconds")}
    else:
        final_rs_state = result

    state["reversal_switch"] = {
        k: v for k, v in final_rs_state.items() if k not in ("pending_action", "votes", "reasons")
    }
    if kis_flat_check is not None:
        state["reversal_switch"]["last_kis_flat_check"] = kis_flat_check
    return {
        "pending_action": pending_action, "executed_qty": executed_qty, "orders": orders,
        "message": execution_message, "reversal_switch": state["reversal_switch"],
        "votes": result.get("votes"), "reasons": result.get("reasons"),
    }
