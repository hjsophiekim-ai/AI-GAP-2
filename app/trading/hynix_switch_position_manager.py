"""
hynix_switch_position_manager.py — 하이닉스⇄인버스 스위칭, TP/SL, 당일 강제청산.

`app.trading.broker_factory.create_broker(mode)`가 만든 브로커의 buy()/sell()을
직접 호출한다(OrderManager 경유 금지 — 인버스 종목코드 '0197X0'은 isdigit()==False라
ETF/ETN 필터에 걸려 차단됨). 모든 포지션은 당일 진입·당일 청산 원칙을 따른다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL, HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed, should_liquidate_now
from app.trading.hynix_position_common import (
    get_hynix_auto_position, is_buy_cooldown_active, POSITION_CONFLICT,
)

ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = ROOT / "config" / "hynix_enhanced_weights.json"

_DEFAULT_RISK = {
    "take_profit_1_pct": 1.2, "take_profit_1_ratio": 0.5,
    "take_profit_2_pct": 2.0, "take_profit_2_ratio": 1.0,
    "stop_loss_1_pct": -0.8, "stop_loss_1_ratio": 0.5,
    "stop_loss_2_pct": -1.5, "stop_loss_2_ratio": 1.0,
    "daily_loss_limit_pct": -2.5,
}
_DEFAULT_SIZING = {"normal_trade_cash_pct": 0.20, "forced_trade_cash_pct": 0.08}

_ACTION_TO_SYMBOL = {
    "HYNIX_STRONG_BUY": HYNIX_SYMBOL, "HYNIX_BUY": HYNIX_SYMBOL,
    "INVERSE_STRONG_BUY": INVERSE_SYMBOL, "INVERSE_BUY": INVERSE_SYMBOL,
}
_SYMBOL_NAME = {HYNIX_SYMBOL: HYNIX_NAME, INVERSE_SYMBOL: INVERSE_NAME}


def _load_section(name: str, defaults: dict) -> dict:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            return {**defaults, **(data.get(name) or {})}
    except Exception as exc:
        logger.debug("[SwitchPositionManager] %s 설정 로드 실패, 기본값 사용: %s", name, exc)
    return dict(defaults)


def _cycle_bucket(now: datetime) -> str:
    return now.strftime("%Y%m%d%H") + f"{(now.minute // 3) * 3:02d}"


def _current_price(symbol: str, hynix_price: Optional[float], inverse_price: Optional[float]) -> Optional[float]:
    if symbol == HYNIX_SYMBOL:
        return hynix_price
    if symbol == INVERSE_SYMBOL:
        return inverse_price
    return None


def evaluate_tp_sl(position: dict, current_price: Optional[float]) -> Optional[dict]:
    """TP/SL 단계 판정. 트리거되면 {"ratio":.., "reason":.., "tag":..} 반환, 아니면 None."""
    risk = _load_section("risk", _DEFAULT_RISK)
    if not position or not position.get("symbol") or (position.get("quantity") or 0) <= 0:
        return None
    entry = position.get("entry_price")
    if not entry or entry <= 0 or current_price is None:
        return None
    pct = (current_price / entry - 1.0) * 100

    if pct >= risk["take_profit_2_pct"]:
        return {"ratio": 1.0, "reason": f"익절 전량(+{pct:.2f}%≥+{risk['take_profit_2_pct']}%)", "tag": "tp2"}
    if pct >= risk["take_profit_1_pct"] and not position.get("partial_tp1_done"):
        return {"ratio": risk["take_profit_1_ratio"], "reason": f"익절 50%(+{pct:.2f}%≥+{risk['take_profit_1_pct']}%)", "tag": "tp1"}
    if pct <= risk["stop_loss_2_pct"]:
        return {"ratio": 1.0, "reason": f"손절 전량({pct:.2f}%≤{risk['stop_loss_2_pct']}%)", "tag": "sl2"}
    if pct <= risk["stop_loss_1_pct"] and not position.get("partial_sl1_done"):
        return {"ratio": risk["stop_loss_1_ratio"], "reason": f"손절 50%({pct:.2f}%≤{risk['stop_loss_1_pct']}%)", "tag": "sl1"}
    return None


def _sizing_cash_amount(broker, forced: bool) -> tuple[float, float]:
    """반환: (사이징 적용된 매수금액, 실제 매수가능금액 전체). 조회 실패 시 (0.0, 0.0)."""
    sizing = _load_section("sizing", _DEFAULT_SIZING)
    try:
        cash = float(broker.get_buyable_cash())
    except Exception as exc:
        logger.warning("[SwitchPositionManager] 매수가능금액 조회 실패: %s", exc)
        return 0.0, 0.0
    pct = sizing["forced_trade_cash_pct"] if forced else sizing["normal_trade_cash_pct"]
    return max(0.0, cash * float(pct)), cash


def _record_order(
    orders: list, order_result, action: str, symbol: str, quantity: int, price: float, reason: str,
    before_qty: Optional[int] = None, expected_remaining_qty: Optional[int] = None,
) -> None:
    result = order_result.to_dict() if hasattr(order_result, "to_dict") else dict(order_result)
    orders.append({
        "timestamp": datetime.now().isoformat(), "action": action, "symbol": symbol,
        "name": _SYMBOL_NAME.get(symbol, symbol), "quantity": quantity, "price": price,
        "amount": (quantity or 0) * (price or 0), "reason": reason,
        "success": result.get("success"), "message": result.get("message"), "order_id": result.get("order_id"),
        "before_qty": before_qty, "executed_qty": quantity if result.get("success") else 0,
        "expected_remaining_qty": expected_remaining_qty,
    })


def _record_skipped(orders: list, action: str, symbol: str, price: Optional[float], reason: str, message: str) -> None:
    """주문을 브로커에 제출하지도 못한 채 스킵된 경우도 로그에 남긴다(조용한 누락 방지)."""
    orders.append({
        "timestamp": datetime.now().isoformat(), "action": action, "symbol": symbol,
        "name": _SYMBOL_NAME.get(symbol, symbol), "quantity": 0, "price": price or 0,
        "amount": 0, "reason": f"{reason} — 스킵: {message}",
        "success": False, "message": message, "order_id": "",
    })


def _sell_all_or_ratio(
    broker, position: dict, current_price: float, ratio: float, reason: str, orders: list,
    mode: str = "mock", exit_reason_type: Optional[str] = None,
) -> dict:
    """포지션 전량 또는 비율만큼 매도.

    exit_reason_type이 주어지면 Exit Order Coordinator 락을 통해 실행한다 — 같은
    (mode, symbol, exit_reason_type)에 대해 동시 진행 중이거나 최근 30초 이내
    체결된 매도가 있으면 이번 매도는 스킵된다(레거시 TP/SL, 강제청산, 스위칭,
    Dynamic Exit AI가 동시에 같은 포지션을 파는 것을 방지).
    """
    from app.trading.exit_order_coordinator import try_acquire_exit_lock

    symbol = position["symbol"]
    total_qty = int(position.get("quantity") or 0)
    sell_qty = max(1, int(total_qty * ratio)) if ratio < 1.0 else total_qty
    sell_qty = min(sell_qty, total_qty)
    expected_remaining = 0 if ratio >= 1.0 else max(0, total_qty - sell_qty)
    if sell_qty <= 0:
        _record_skipped(orders, "SELL_SKIPPED", symbol, current_price, reason, "매도 수량 0")
        return {"success": False, "message": "매도 수량 0"}

    if exit_reason_type is None:
        return _execute_sell(broker, symbol, sell_qty, current_price, reason, orders, total_qty, expected_remaining)

    with try_acquire_exit_lock(mode, symbol, exit_reason_type) as lock:
        if not lock:
            message = "Exit Order Coordinator: 동시 매도 차단(다른 곳에서 진행 중이거나 최근 30초 이내 체결됨)"
            _record_skipped(orders, "SELL_SKIPPED", symbol, current_price, reason, message)
            return {"success": False, "message": message, "blocked_by_coordinator": True}
        result = _execute_sell(broker, symbol, sell_qty, current_price, reason, orders, total_qty, expected_remaining)
        if result.get("success"):
            lock.mark_executed()
        return result


def _execute_sell(
    broker, symbol: str, sell_qty: int, current_price: float, reason: str, orders: list,
    before_qty: int, expected_remaining: int,
) -> dict:
    order = broker.sell(symbol, _SYMBOL_NAME.get(symbol, symbol), sell_qty, current_price)
    _record_order(orders, order, "SELL", symbol, sell_qty, current_price, reason, before_qty=before_qty, expected_remaining_qty=expected_remaining)
    result = order.to_dict() if hasattr(order, "to_dict") else dict(order)
    result["sold_quantity"] = sell_qty
    result["remaining_quantity"] = before_qty - sell_qty
    result["expected_remaining_qty"] = expected_remaining
    return result


def _buy_new(broker, symbol: str, current_price: float, cash_amount: float, reason: str, orders: list) -> dict:
    if not current_price or current_price <= 0 or cash_amount <= 0:
        _record_skipped(orders, "BUY_SKIPPED", symbol, current_price, reason, "가격/금액 유효하지 않음")
        return {"success": False, "message": "가격/금액 유효하지 않음"}
    quantity = int(cash_amount // current_price)
    if quantity < 1:
        _record_skipped(orders, "BUY_SKIPPED", symbol, current_price, reason, "매수금액으로 1주도 매수 불가")
        return {"success": False, "message": "매수금액으로 1주도 매수 불가"}
    order = broker.buy(symbol, _SYMBOL_NAME.get(symbol, symbol), quantity, current_price)
    _record_order(orders, order, "BUY", symbol, quantity, current_price, reason)
    result = order.to_dict() if hasattr(order, "to_dict") else dict(order)
    result["bought_quantity"] = quantity
    return result


def sync_position_from_broker(state: dict, broker) -> dict:
    """[하위호환용] 브로커를 직접 조회해 state를 동기화한다. 신규 코드는 엔진에서
    `HynixPositionManager.sync()` 후 `apply_position_manager_to_state()`를 사용할 것.
    """
    from app.trading.hynix_position_common import HynixPositionManager

    pm = HynixPositionManager(broker, mode=state.get("mode", "mock"))
    pm.sync(force=True)
    return apply_position_manager_to_state(state, pm)


def apply_position_manager_to_state(state: dict, position_manager) -> dict:
    """HynixPositionManager.sync() 결과(브로커 조회값)를 state(캐시)에 반영한다.

    브로커가 항상 우선한다 — 심볼이 같으면 수량/평단만 갱신하고 entry_time 등
    우리 쪽에서만 관리하는 필드는 보존하며, 심볼이 다르면 완전히 새로 시작한다.
    """
    pos_info = position_manager.current_position
    state["position_conflict"] = bool(pos_info.get("conflict"))
    if state["position_conflict"]:
        state["critical_alert"] = position_manager.conflict_error
        logger.error("[SwitchPositionManager] %s", position_manager.conflict_error)
        return state

    broker_symbol = pos_info.get("symbol")
    existing = state.get("position") or {}
    state_symbol = existing.get("symbol")

    if broker_symbol == state_symbol:
        if broker_symbol is not None:
            existing["quantity"] = pos_info.get("quantity")
            existing["avg_price"] = pos_info.get("avg_price")
            state["position"] = existing
    else:
        logger.warning(
            "[SwitchPositionManager] state 포지션(%s)과 실제 브로커 포지션(%s) 불일치 — 브로커 기준으로 동기화",
            state_symbol, broker_symbol,
        )
        if broker_symbol is None:
            state["position"] = _empty_position()
        else:
            state["position"] = {
                **_empty_position(),
                "symbol": broker_symbol, "name": _SYMBOL_NAME.get(broker_symbol, broker_symbol),
                "quantity": pos_info.get("quantity"), "avg_price": pos_info.get("avg_price"),
                "entry_price": pos_info.get("avg_price"),
                "entry_time": datetime.now().isoformat(),
            }

    # 거래횟수는 브로커가 자체 카운터를 지원하면(예: DryRunBroker) 그 값을 항상 우선한다(로그 집계 아님).
    if hasattr(position_manager.broker, "get_executed_order_count"):
        state["daily_trade_count"] = position_manager.trade_count
    return state


def run_liquidation_if_needed(
    now: datetime, state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    position_manager=None,
) -> dict:
    """15:15 도달 시 보유 포지션 전량 강제청산(수익/손실 무관, TP/SL보다 우선).

    보유 포지션이 없으면 청산 대상이 없으므로 즉시 liquidation_done=True로 완료 처리한다
    (기존 버그: 무보유 상태에서 이 함수가 조용히 아무 것도 하지 않아 화면에 "강제청산 완료: 아니오"가
    영구히 표시되는 문제가 있었다). 손절모드가 AUTO가 아니면(ALERT_ONLY/BATCH_MANUAL) 자동매도하지
    않고 알림만 남긴다 — 이 경우 포지션이 남아있는 한 liquidation_done은 False로 유지된다.
    실패 시 1회 재시도, 재시도도 실패하면 critical_alert 기록(포지션은 유지). 모든 시도는
    data/logs/forced_liquidation_log.csv에 기록된다.
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
        state["critical_alert"] = f"[{now.isoformat()}] 강제청산 시각 도달했으나 현재가 없음 — 포지션 유지"
        logger.error(state["critical_alert"])
        log_forced_liquidation_event({
            "mode": mode, "symbol": symbol, "quantity": quantity, "entry_price": entry_price,
            "current_price": None, "liquidation_attempted": True, "order_sent": False,
            "order_confirmed": False, "result": "NO_PRICE", "reason": "현재가 조회 실패",
        })
        return {"liquidated": False, "orders": orders}

    for attempt in (1, 2):
        result = _sell_all_or_ratio(
            broker, position, current_price, 1.0, "15:15 당일 강제청산", orders,
            mode=mode, exit_reason_type="liquidation",
        )
        if result.get("success"):
            realized = (current_price - position.get("entry_price", current_price)) * result.get("sold_quantity", position.get("quantity", 0))
            state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + realized
            state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
            state["position"] = _empty_position()
            state["last_sell_price"] = current_price
            state["last_trade_time"] = now.isoformat()
            state["last_action"] = "SELL"
            state["last_order_id"] = result.get("order_id")
            state["critical_alert"] = None

            order_confirmed = True
            if position_manager is not None:
                order_confirmed = verify_order_confirmed(position_manager, symbol, expect_cleared=True)
            state["liquidation_done"] = bool(order_confirmed)

            log_forced_liquidation_event({
                "mode": mode, "symbol": symbol, "quantity": quantity, "entry_price": entry_price,
                "current_price": current_price, "liquidation_attempted": True, "order_sent": True,
                "order_confirmed": order_confirmed,
                "result": "SUCCESS" if order_confirmed else "UNCONFIRMED",
                "reason": "15:15 당일 강제청산" + ("" if order_confirmed else " — 체결 미확인, 재확인 필요"),
            })
            return {"liquidated": True, "orders": orders, "attempts": attempt, "order_confirmed": order_confirmed}
        logger.warning("[SwitchPositionManager] 강제청산 시도 %s회 실패: %s", attempt, result.get("message"))

    state["liquidation_done"] = False
    failure_message = orders[-1].get("message") if orders else "알수없음"
    state["critical_alert"] = f"[{now.isoformat()}] 강제청산 2회(1회+재시도) 모두 실패: {failure_message}"
    logger.error(state["critical_alert"])
    log_forced_liquidation_event({
        "mode": mode, "symbol": symbol, "quantity": quantity, "entry_price": entry_price,
        "current_price": current_price, "liquidation_attempted": True, "order_sent": False,
        "order_confirmed": False, "result": "FAILED", "reason": f"2회 시도 모두 실패: {failure_message}",
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


def run_tp_sl_if_needed(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    position_manager=None, now: Optional[datetime] = None,
) -> dict:
    """보유 포지션의 TP/SL 판정 및 실행(강제청산 판정 이후, 스위칭 판정 이전에 호출).

    Dynamic Exit AI 감시 스레드가 살아있으면 이 레거시 TP/SL은 완전히 스킵한다 —
    두 시스템이 서로 다른 임계값(예: 레거시 -0.8% vs Dynamic Exit AI 프로필 -1.2%)으로
    동시에 같은 포지션을 판단·매도하면 화면 표시와 실제 체결이 어긋나고 중복 매도
    위험도 생긴다. 이 함수는 감시 스레드가 죽어있을 때만 동작하는 진짜 fallback이다.

    손절(SL) 트리거는 손절모드(AUTO/ALERT_ONLY/BATCH_MANUAL) 게이트를 통과해야만 실제
    매도가 나간다. 익절(TP) 트리거는 손절 방식 설정과 무관하게 그대로 실행한다.
    """
    now = now or datetime.now()
    orders: list = []
    position = state.get("position") or {}
    symbol = position.get("symbol")
    if not symbol or (position.get("quantity") or 0) <= 0:
        return {"triggered": False, "orders": orders}

    try:
        from app.trading.dynamic_exit_watcher import is_watcher_running

        if is_watcher_running():
            return {"triggered": False, "orders": orders, "skipped_reason": "Dynamic Exit AI 감시 스레드가 활성 상태 — 레거시 TP/SL은 fallback이므로 스킵"}
    except Exception as exc:
        logger.debug("[SwitchPositionManager] watcher 상태 확인 실패, 레거시 TP/SL 계속 진행: %s", exc)

    mode = state.get("mode", "mock")
    current_price = _current_price(symbol, hynix_price, inverse_price)
    trigger = evaluate_tp_sl(position, current_price)
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
        mode=mode, exit_reason_type=exit_reason_type,
    )
    if not result.get("success"):
        return {"triggered": True, "executed": False, "orders": orders, "blocked_by_coordinator": result.get("blocked_by_coordinator", False)}

    sold_qty = result.get("sold_quantity", 0)
    realized = (current_price - position.get("entry_price", current_price)) * sold_qty
    state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + realized
    state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
    state["last_sell_price"] = current_price
    state["last_trade_time"] = datetime.now().isoformat()
    state["last_action"] = "SELL"
    state["last_order_id"] = result.get("order_id")

    if trigger["tag"] in ("tp2", "sl2") or result.get("remaining_quantity", 0) <= 0:
        state["position"] = _empty_position()
    else:
        position["quantity"] = result.get("remaining_quantity", position["quantity"])
        if trigger["tag"] == "tp1":
            position["partial_tp1_done"] = True
        elif trigger["tag"] == "sl1":
            position["partial_sl1_done"] = True
        state["position"] = position

    return {"triggered": True, "executed": True, "orders": orders}


def run_switch_or_entry(
    state: dict, broker, final_action: str, hynix_price: Optional[float], inverse_price: Optional[float],
    now: Optional[datetime] = None, forced: bool = False, reason: str = "",
) -> dict:
    """스위칭 또는 신규 진입 실행. 14:50 이후에는 반대 종목 재매수 없이 매도만."""
    now = now or datetime.now()
    orders: list = []
    desired_symbol = _ACTION_TO_SYMBOL.get(final_action)
    if desired_symbol is None:
        return {"acted": False, "orders": orders, "message": "HOLD — 신규 진입/스위칭 없음"}

    bucket = _cycle_bucket(now)
    signature = f"{final_action}:{desired_symbol}"
    if state.get("last_order_cycle_bucket") == bucket and state.get("last_order_signature") == signature:
        return {"acted": False, "orders": orders, "message": "동일 3분 주기 내 동일 신호 — 중복 주문 방지"}

    # 매수 쿨다운은 "이번 사이클에서 있을 매도"가 값을 갱신하기 전, 이전 사이클 기록으로 판정해야 한다.
    buy_cooldown_active = is_buy_cooldown_active(state.get("last_trade_time"), state.get("last_action"), now)

    position = state.get("position") or {}
    held_symbol = position.get("symbol")

    if held_symbol == desired_symbol:
        label = "인버스" if desired_symbol == INVERSE_SYMBOL else "하이닉스"
        return {"acted": False, "orders": orders, "message": f"이미 {label} 보유 중 — 중복 매수 방지"}

    entry_allowed = is_new_entry_allowed(now)

    if held_symbol:
        current_price = _current_price(held_symbol, hynix_price, inverse_price)
        if not current_price:
            return {"acted": False, "orders": orders, "message": "보유종목 현재가 없음 — 스위칭 skip"}
        sell_result = _sell_all_or_ratio(
            broker, position, current_price, 1.0, f"스위칭 매도({reason})", orders,
            mode=state.get("mode", "mock"), exit_reason_type="switch",
        )
        if not sell_result.get("success"):
            return {"acted": False, "orders": orders, "message": f"스위칭 매도 실패: {sell_result.get('message')}"}
        sold_qty = sell_result.get("sold_quantity", position.get("quantity", 0))
        realized = (current_price - position.get("entry_price", current_price)) * sold_qty
        state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + realized
        state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
        state["last_sell_price"] = current_price
        state["last_trade_time"] = now.isoformat()
        state["last_action"] = "SELL"
        state["last_order_id"] = sell_result.get("order_id")
        state["position"] = _empty_position()

        if not entry_allowed:
            state["last_order_cycle_bucket"] = bucket
            state["last_order_signature"] = signature
            return {"acted": True, "orders": orders, "message": "14:50 이후 — 매도만 실행, 반대 종목 재매수 없음"}

    if not entry_allowed:
        return {"acted": bool(orders), "orders": orders, "message": "신규 진입 불가 시간대"}

    if buy_cooldown_active:
        return {"acted": bool(orders), "orders": orders, "message": "마지막 매수 후 180초 이내 — 신규 매수 쿨다운"}

    current_price = _current_price(desired_symbol, hynix_price, inverse_price)
    if not current_price:
        return {"acted": bool(orders), "orders": orders, "message": "목표 종목 현재가 없음 — 매수 skip"}

    sized_cash, full_cash = _sizing_cash_amount(broker, forced)
    buy_reason = f"신규진입/스위칭 매수({reason})"
    if int(sized_cash // current_price) < 1 and full_cash >= current_price:
        cash_amount = current_price  # 사이징 금액으로 1주 미달이나 실제 매수가능금액은 충분 → 최소 1주 보장
        buy_reason += " [사이징 금액 부족 — 매수가능금액 내 최소 1주로 상향]"
    else:
        cash_amount = sized_cash

    buy_result = _buy_new(broker, desired_symbol, current_price, cash_amount, buy_reason, orders)
    if buy_result.get("success"):
        qty = buy_result.get("bought_quantity", 0)
        state["position"] = {
            **_empty_position(),
            "symbol": desired_symbol, "name": _SYMBOL_NAME.get(desired_symbol, desired_symbol),
            "quantity": qty, "avg_price": current_price, "entry_price": current_price,
            "entry_time": now.isoformat(),
        }
        state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
        state["last_buy_price"] = current_price
        state["last_trade_time"] = now.isoformat()
        state["last_action"] = "BUY"
        state["last_order_id"] = buy_result.get("order_id")

    state["last_order_cycle_bucket"] = bucket
    state["last_order_signature"] = signature
    return {"acted": True, "orders": orders, "message": buy_result.get("message", "")}
