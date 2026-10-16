"""미확정 매수주문(pending_buy_order) 추적 — 2026-10-16 실사고 대응.

배경 (2026-10-16 09:51, 모의투자):
  지정가 매수가 60초 체결폴링 동안 0주 → 취소 시도 → 취소 실패 → BLOCKED:BUY_CANCEL_NOT_CONFIRMED 로 "끝난 것"
  처리됐다. 국내 주문취소 API 가 아직 없어(broker_adapter.cancel_order -> CANCEL_NOT_SUPPORTED) 그 주문은 KIS 에
  살아 있었고, 09:54 에 체결됐다. UI 는 "0주" 로 보여 사용자가 수동으로 또 샀다.

이 모듈이 하는 일 (pending 이 없으면 아무 것도 하지 않는다 = 기존 동작 그대로):
  · register(): 실행 결과가 BUY_CANCEL_NOT_CONFIRMED 이면 주문번호/종목/수량/가격/신호시각을
    state.pending_buy_order 에 저장(state_store 로 영구 저장 → 재시작해도 복원)하고, 신호원장 결과를
    PENDING_ORDER_UNCONFIRMED 로 남긴다(failure_stage 는 BUY_CANCEL_NOT_CONFIRMED 그대로).
  · check(): 매 tick(최소 CHECK_INTERVAL_SEC 간격) 주문번호로 상태를 조회(broker.get_order_status, 조회 전용).
      - 체결이 늘었으면 그 수량을 **원래 신호 signal_id 로** 체결원장 BUY 레그로 기록하고 state.position 에 반영.
      - 잔량 0(전량체결/취소/거부/장마감 소멸)이 확인되면 LATE_FILL_CONFIRMED 또는 CANCEL_CONFIRMED 로 종료하고
        신호원장에 결과 행(signal_type=PENDING_ORDER_RESOLUTION)을 남긴다.
      - 조회 실패면 아무 것도 확정하지 않고 다음에 다시 본다(모르면 확정하지 않는다).
  · block_reason_for(): pending 이 살아 있는 동안 신규 매수(자동·수동, 같은 방향·반대 방향 모두)를 막는 사유.
  · 반대방향 신호가 오면 cancel_order 를 한 번 시도한다(현재 국내 API 미지원이라 기록만 남는다).

종료 판정은 "잔량 0" 하나뿐이다. 체결수량은 KIS 주문조회의 누적 체결수량(tot_ccld_qty)이 truth 다.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any, Callable, Optional

from app.logger import logger
from app.trading.macd2 import config, ledger
from app.trading.macd2.broker_adapter import BrokerOrderResult

PENDING_ORDER_UNCONFIRMED = "PENDING_ORDER_UNCONFIRMED"
PENDING_ORDER_UNCONFIRMED_OPPOSITE = "PENDING_ORDER_UNCONFIRMED_OPPOSITE"
LATE_FILL_CONFIRMED = "LATE_FILL_CONFIRMED"
CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
MANUAL_RELEASED = "PENDING_MANUAL_RELEASED"
RESOLUTION_SIGNAL_TYPE = "PENDING_ORDER_RESOLUTION"
SOURCE_BUY_CANCEL_NOT_CONFIRMED = "BUY_CANCEL_NOT_CONFIRMED"
CHECK_INTERVAL_SEC = 5.0
#: 주문실행 v2 취소가 허용된 경우(MOCK 전용)에만: 추적 중 살아 있는 주문에 취소를 다시 넣는 최소 간격.
CANCEL_RETRY_SEC = 30.0
UI_WARNING = "주문 상태 미확정 — 기존 주문이 살아 있을 수 있음. 수동매수 금지"


def get(state) -> Optional[dict]:
    p = getattr(state, "pending_buy_order", None)
    return p if isinstance(p, dict) and p.get("order_id") else None


def is_active(state) -> bool:
    return get(state) is not None


def block_reason_for(state, direction_value: Optional[str] = None) -> Optional[str]:
    """pending 이 있으면 신규 매수 금지 사유. 같은 방향/모름 → PENDING_ORDER_UNCONFIRMED, 반대 → _OPPOSITE."""
    p = get(state)
    if p is None:
        return None
    if direction_value and p.get("direction") and direction_value != p["direction"]:
        return PENDING_ORDER_UNCONFIRMED_OPPOSITE
    return PENDING_ORDER_UNCONFIRMED


def _direction_value(direction) -> str:
    return getattr(direction, "value", None) or str(direction or "")


def register(state, outcome, *, direction, signal_id: str, signal_type: str, now: datetime,
             signal_detected_at: Optional[datetime] = None, source: str = "AUTO") -> bool:
    """outcome 이 BUY_CANCEL_NOT_CONFIRMED 면 pending 으로 저장하고 True. 아니면 아무 것도 안 하고 False.

    2026-10-16 주문실행 v2: 일부 체결(EXECUTED) + 잔량 취소 미확인(outcome.open_order)도 같은 추적으로 넘긴다
    — 이미 체결·기록된 수량은 recorded_qty 로 시작하므로 다시 세지 않는다."""
    open_order = getattr(outcome, "open_order", None)
    if getattr(outcome, "block_reason", None) != SOURCE_BUY_CANCEL_NOT_CONFIRMED and not open_order:
        return False
    buy = getattr(outcome, "buy_result", None)
    order_id = str((open_order or {}).get("order_id") or getattr(buy, "order_id", "") or "")
    if not order_id:
        return False
    ts = dict(getattr(outcome, "timestamps", {}) or {})
    state.pending_buy_order = {
        "order_id": order_id,
        "symbol": outcome.target_symbol,
        "direction": _direction_value(direction),
        "signal_id": signal_id,
        "signal_type": signal_type,
        "source": source,
        "requested_qty": int(getattr(outcome, "final_qty", None) or getattr(outcome, "quantity", 0) or 0),
        "order_price": float(getattr(outcome, "order_price", None) or 0.0),
        "signal_detected_at": (signal_detected_at or now).isoformat(),
        "ordered_at": ts.get("buy_requested_at") or now.isoformat(),
        "registered_at": now.isoformat(),
        "trading_date": now.strftime("%Y%m%d"),
        "recorded_qty": int((open_order or {}).get("filled") or 0),
        "recorded_amount": float((open_order or {}).get("amount") or 0.0),
        "legs_recorded": 1 if (open_order or {}).get("filled") else 0,
        "org_no": str(((getattr(buy, "raw", None) or {}).get("KRX_FWDG_ORD_ORGNO")) or ""),
        "checks": 0, "last_checked_at": None, "last_status": "REGISTERED",
        "cancel_attempts": int(bool(getattr(outcome, "cancel_called", False))),
        "last_cancel_result": str(getattr(outcome, "cancel_result", "") or ""),
    }
    if not open_order:
        outcome.block_reason = PENDING_ORDER_UNCONFIRMED  # 신호원장: BLOCKED:PENDING_ORDER_UNCONFIRMED
    logger.warning("[MACD2][PENDING_ORDER] 등록 %s %s %s주 @%s order_id=%s (취소 미확인 — 체결/취소 확정까지 추적, "
                   "신규 매수 금지)", signal_id, outcome.target_symbol, state.pending_buy_order["requested_qty"],
                   state.pending_buy_order["order_price"], order_id)
    return True


def try_cancel(broker, state, now: datetime, why: str) -> str:
    """살아 있을 수 있는 주문 취소 시도(반대 신호 등). 결과 문자열만 기록한다."""
    p = get(state)
    if p is None:
        return ""
    v2_ok = bool(getattr(broker, "cancel_v2_allowed", None) and broker.cancel_v2_allowed())
    fn = getattr(broker, "cancel_order_v2" if v2_ok else "cancel_order", None)
    res = "CANCEL_UNAVAILABLE"
    if fn is not None:
        try:
            r = fn(p["order_id"], p["symbol"], p.get("org_no") or None) if v2_ok else fn(p["order_id"], p["symbol"])
            res = "OK" if getattr(r, "success", False) else f"FAILED:{getattr(r, 'message', '')}"
        except Exception as exc:  # noqa: BLE001 — 취소 시도는 실패해도 추적은 계속
            res = f"FAILED:{exc!r}"[:120]
    p["cancel_attempts"] = int(p.get("cancel_attempts", 0)) + 1
    p["last_cancel_result"] = f"{now.isoformat()} {why} {res}"
    p["last_cancel_at"] = now.isoformat()
    logger.warning("[MACD2][PENDING_ORDER] 취소 시도(%s) order_id=%s -> %s", why, p["order_id"], res)
    return res


def _held_qty(state, symbol) -> int:
    pos = getattr(state, "position", None)
    return int(pos.quantity) if (pos is not None and pos.symbol == symbol and pos.quantity > 0) else 0


def check(broker, state, now: datetime, *, apply_new_position: Callable[[Any, int, float], None],
          force: bool = False, ledger_extra: Optional[dict] = None) -> Optional[dict]:
    """pending 1건 조회·반영. 반환: 이번 호출에서 일어난 일 dict 또는 None(할 일 없음/조회 생략).

    apply_new_position(state, qty, avg_price): runtime 이 flat 일 때 첫 체결을 포지션으로 세우는 콜백
    (worker._apply_switch_outcome 을 감싼 것). 이미 같은 종목을 들고 있으면 수량·평단만 늘린다.
    """
    p = get(state)
    if p is None:
        return None
    last = p.get("last_checked_at")
    if not force and last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < CHECK_INTERVAL_SEC:
                return None
        except ValueError:
            pass
    p["checks"] = int(p.get("checks", 0)) + 1
    p["last_checked_at"] = now.isoformat()
    getter = getattr(broker, "get_order_status", None)
    st = {}
    if getter is not None:
        try:
            st = dict(getter(p["order_id"], symbol=p["symbol"], trading_date=p.get("trading_date", "")) or {})
        except Exception as exc:  # noqa: BLE001
            st = {"ok": False, "error": repr(exc)[:200]}
    prev_day = p.get("trading_date", now.strftime("%Y%m%d")) < now.strftime("%Y%m%d")
    if not st.get("ok"):
        p["last_status"] = f"QUERY_ERROR:{st.get('error') or 'get_order_status unavailable'}"[:200]
        return {"action": "QUERY_ERROR", "order_id": p["order_id"]}
    if not st.get("found"):
        p["last_status"] = "NOT_FOUND"
        # 당일 주문조회에 아직 안 잡힐 수 있다 — 같은 날엔 확정하지 않는다. 전일 주문이 그 날짜 조회에도 없으면
        # 체결 기록이 없는 소멸 주문으로 본다(혹시 잔고가 있으면 reconcile 이 발견해 기록한다).
        if prev_day:
            return _resolve(state, now, CANCEL_CONFIRMED, "PREV_DAY_ORDER_NOT_FOUND", ledger_extra)
        return {"action": "NOT_FOUND", "order_id": p["order_id"]}

    filled = int(st.get("filled_qty") or 0)
    amount = float(st.get("amount") or 0.0)
    remaining = int(st.get("remaining_qty") or 0)
    p["last_status"] = (f"ord={st.get('order_qty')} filled={filled} remaining={remaining} "
                        f"cancelled={st.get('cancelled')} rejected={st.get('rejected_qty')}")
    out = {"action": "OPEN", "order_id": p["order_id"], "filled_qty": filled, "remaining_qty": remaining}

    base = max(int(p.get("recorded_qty", 0)), _held_qty(state, p["symbol"]))
    new_qty = filled - base
    if new_qty > 0:
        delta_amt = amount - float(p.get("recorded_amount", 0.0))
        price = (delta_amt / new_qty) if delta_amt > 0 else float(st.get("avg_price") or p.get("order_price") or 0.0)
        price = round(price, 4)
        held_before = _held_qty(state, p["symbol"])
        # 순서: 포지션 → 누적수량 → 원장. 원장 기록이 실패해도 다음 조회에서 같은 체결을 두 번 세지 않는다.
        if held_before == 0 and getattr(state, "position", None) is None:
            apply_new_position(state, new_qty, price)
        elif held_before > 0:
            pos = state.position                                   # PositionSnapshot 은 frozen
            tot = pos.quantity + new_qty
            state.position = dataclasses.replace(
                pos, quantity=tot, avg_price=(pos.avg_price * pos.quantity + price * new_qty) / tot)
        else:
            logger.error("[MACD2][PENDING_ORDER] 늦은 체결 %s주 — runtime 이 다른 종목(%s) 보유 중, 포지션 반영은 "
                         "reconcile 에 맡김", new_qty, state.position.symbol)
        p["recorded_qty"] = base + new_qty
        p["recorded_amount"] = amount
        try:
            _record_late_fill_leg(broker, p, new_qty, price, base, now)
        except Exception as exc:  # noqa: BLE001
            logger.error("[MACD2][PENDING_ORDER] 늦은 체결 원장 기록 실패(포지션은 반영됨): %s", exc)
        out.update(action="FILL_RECORDED", new_qty=new_qty, price=price)
        logger.warning("[MACD2][PENDING_ORDER] 늦은 체결 기록 %s %s주 @%s (누적 %s / 주문 %s) signal=%s",
                       p["symbol"], new_qty, price, p["recorded_qty"], p["requested_qty"], p["signal_id"])

    if remaining > 0 and not prev_day and getattr(broker, "cancel_v2_allowed", None) and broker.cancel_v2_allowed():
        # v2 취소가 허용된 경우(MOCK 전용)만: 살아 있는 잔량에 취소를 다시 넣는다(최소 CANCEL_RETRY_SEC 간격).
        last_c = p.get("last_cancel_at")
        try:
            due = (not last_c) or (now - datetime.fromisoformat(last_c)).total_seconds() >= CANCEL_RETRY_SEC
        except ValueError:
            due = True
        if due:
            out["cancel"] = try_cancel(broker, state, now, "TRACK_RETRY")

    if remaining <= 0 or prev_day:
        # 잔량 0 = 전량체결/취소확인/거부. 전일 주문은 장마감에 남은 잔량이 소멸한다.
        final = LATE_FILL_CONFIRMED if int(p.get("recorded_qty", 0)) > 0 else CANCEL_CONFIRMED
        why = "PREV_DAY_EXPIRED" if (prev_day and remaining > 0) else ("FILLED" if filled >= int(st.get("order_qty") or 0) > 0
                                                                       else "REMAINING_ZERO")
        res = _resolve(state, now, final, why, ledger_extra)
        res["fill"] = out.get("new_qty")
        return res
    return out


def _record_late_fill_leg(broker, p: dict, qty: int, price: float, base: int, now: datetime) -> None:
    """체결원장 BUY 레그. 체결원장은 order_id 로 중복을 거르므로 같은 주문의 2번째 이후 부분체결은
    `<주문번호>#2`, `#3` ... 으로 남긴다(첫 레그는 실제 KIS 주문번호 그대로)."""
    from app.trading.macd2 import order_executor            # 지연 import (순환 방지)
    p["legs_recorded"] = int(p.get("legs_recorded", 0)) + 1
    leg_order_id = p["order_id"] if p["legs_recorded"] == 1 else f"{p['order_id']}#{p['legs_recorded']}"
    order_executor._record_leg(
        broker_mode=getattr(broker, "mode", "") or "", signal_id=p["signal_id"], symbol=p["symbol"], side="BUY",
        qty=qty, price=price, position_before=base, position_after=base + qty, exit_reason="",
        order_result=BrokerOrderResult(True, leg_order_id, p["symbol"], "BUY", int(p["requested_qty"]), qty, price,
                                       "LATE_FILL_CONFIRMED", raw={"kis_order_id": p["order_id"]}),
        entry_price=price, confirmed_at=now.isoformat(), requested_qty=int(p["requested_qty"]),
    )


def _resolve(state, now: datetime, final: str, why: str, ledger_extra: Optional[dict]) -> dict:
    p = dict(get(state) or {})
    row = {
        "trading_date": now.strftime("%Y%m%d"), "completed_bar_at": "",
        "signal_id": f"{p.get('signal_id')}:{final}", "signal_type": RESOLUTION_SIGNAL_TYPE,
        "direction": p.get("direction", ""), "macd": "", "signal": "", "hist_last3": "",
        "detected_at": now.isoformat(), "order_requested_at": p.get("ordered_at", ""),
        "order_result": final,
        "block_reason": (f"order_id={p.get('order_id')}_filled={p.get('recorded_qty', 0)}_requested="
                         f"{p.get('requested_qty')}_price={p.get('order_price')}_why={why}"),
        "final_result": f"{final}:{why}",
        "broker_order_id": p.get("order_id", ""), "filled_qty": p.get("recorded_qty", 0),
        "requested_qty": p.get("requested_qty", ""), "order_price": p.get("order_price", ""),
        "failure_stage": SOURCE_BUY_CANCEL_NOT_CONFIRMED,
        "strategy_name": config.STRATEGY_NAME, "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        **(ledger_extra or {}),
    }
    try:
        ledger.append_signal(row)
    except Exception as exc:  # noqa: BLE001 — 원장 실패로 추적 종료를 막지 않는다
        logger.error("[MACD2][PENDING_ORDER] 결과 원장 기록 실패: %s", exc)
    state.pending_buy_order = None
    state.last_pending_order_result = {"result": final, "why": why, "resolved_at": now.isoformat(), **{
        k: p.get(k) for k in ("order_id", "symbol", "signal_id", "requested_qty", "recorded_qty", "order_price")}}
    logger.warning("[MACD2][PENDING_ORDER] 종료 %s (%s) order_id=%s 체결 %s/%s signal=%s", final, why,
                   p.get("order_id"), p.get("recorded_qty", 0), p.get("requested_qty"), p.get("signal_id"))
    return {"action": final, "why": why, "order_id": p.get("order_id"), "filled_qty": p.get("recorded_qty", 0)}


def manual_release(state, now: datetime, who: str = "ui") -> Optional[dict]:
    """사용자가 KIS 에서 직접 확인한 뒤 해제(비상용). 원장에 PENDING_MANUAL_RELEASED 로 남긴다."""
    if get(state) is None:
        return None
    return _resolve(state, now, MANUAL_RELEASED, f"BY_{who.upper()}", None)
