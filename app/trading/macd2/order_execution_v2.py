"""주문 실행 v2 — 승인된 매수의 '실행부'만 (2026-10-16, feature/macd2-order-execution-v2). 기본 OFF.

order_executor.execute_signal 이 config.EXEC_V2_ENABLED 일 때만 이 모듈을 쓴다. 진입 판정과는 무관하다.

A. fetch_fresh_ask   : 주문 직전 매도1호가 조회, 나이(quote_age_ms) 기록. EXEC_QUOTE_MAX_AGE_SEC 초과면 다시 조회,
                       EXEC_QUOTE_FETCH_ATTEMPTS 안에 못 얻으면 None(주문하지 않음, 사유 기록).
B. poll_fill         : 주문번호 상태조회(get_order_status)로 EXEC_FILL_CHECK_INTERVAL_SEC 마다, 최대 EXEC_FILL_WAIT_MAX_SEC.
                       FILLED / PARTIAL / OPEN / CANCELLED 를 구분한다(잔고수량 추정 아님).
D. cancel_and_confirm: broker.cancel_order_v2(MOCK 전용) → 상태조회로 잔량 0 + 취소 확인까지. 확인 못 하면 '미확인'.
E. run_buy           : 주문 → 체결확인 → (미체결 잔량) 취소 → 취소 **확정** + 이번 주문 0주일 때만 최신 호가로 재주문
                       (최대 EXEC_REPRICE_MAX, 최초 매도1호가 대비 EXEC_REPRICE_MAX_SLIPPAGE_PCT 초과면 추격 포기).
                       취소 미확인이면 절대 재주문하지 않는다 → 결과 CANCEL_UNCONFIRMED (pending_order 추적으로 넘어감).
모든 시각은 exec_log 에 남긴다(order_executor 가 outcome.exec_log 로 노출, worker 가 [MACD2][EXEC_LOG] 로그).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.logger import logger
from app.trading.macd2 import config

KST = timezone(timedelta(hours=9))

# 결과 코드
FILLED = "FILLED"
PARTIAL_CANCEL_CONFIRMED = "PARTIAL_CANCEL_CONFIRMED"
NO_FILL_CANCEL_CONFIRMED = "NO_FILL_CANCEL_CONFIRMED"
CANCEL_UNCONFIRMED = "CANCEL_UNCONFIRMED"
CANCEL_UNAVAILABLE = "CANCEL_UNAVAILABLE"          # 취소 기능 OFF/미지원 → 기존(legacy) 취소 경로로 넘김
STATUS_UNAVAILABLE = "STATUS_UNAVAILABLE"          # 주문번호 조회 불가 → 기존(legacy) 잔고 기반 확인으로 넘김
REPRICE_ABORTED_SLIPPAGE = "REPRICE_ABORTED_SLIPPAGE"
REPRICE_QUOTE_UNAVAILABLE = "REPRICE_QUOTE_UNAVAILABLE"
REPRICE_ORDER_REJECTED = "REPRICE_ORDER_REJECTED"
BLOCK_QUOTE_TOO_OLD = "EXEC_QUOTE_TOO_OLD"


def _now() -> datetime:
    return datetime.now(KST)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def quote_age_ms(q: dict, requested: datetime, received: datetime) -> int:
    """호가 나이: 브로커가 age_sec 를 주면 그것, 아니면 max(왕복시간, 받은 시각 - 거래소 호가접수시각)."""
    if q.get("age_sec") is not None:
        try:
            return int(float(q["age_sec"]) * 1000)
        except (TypeError, ValueError):
            pass
    rtt = int((received - requested).total_seconds() * 1000)
    hms = str(q.get("exch_time_hms") or "")
    if len(hms) >= 6 and hms[:6].isdigit():
        ex = received.replace(hour=int(hms[:2]), minute=int(hms[2:4]), second=int(hms[4:6]), microsecond=0)
        if ex > received + timedelta(seconds=5):           # 자정 근처 등 비정상 — 왕복시간만 쓴다
            return rtt
        return max(rtt, int((received - ex).total_seconds() * 1000))
    return rtt


def fetch_fresh_ask(broker, symbol: str, log: dict, *, prefix: str = "", sleep: Callable = time.sleep,
                    clock: Callable[[], datetime] = _now) -> tuple[Optional[dict], str]:
    """(ask_quote, reason). ask_quote 가 None 이면 주문하지 말 것. reason: OK / EXEC_QUOTE_TOO_OLD / ASK_QUOTE_FAILED."""
    getter = getattr(broker, "get_fresh_ask1", None)
    if getter is None:
        return None, "ASK_QUOTE_FAILED"
    max_age_ms = int(float(config.EXEC_QUOTE_MAX_AGE_SEC) * 1000)
    attempts = max(1, int(config.EXEC_QUOTE_FETCH_ATTEMPTS))
    reason = "ASK_QUOTE_FAILED"
    ages = []
    for i in range(attempts):
        t0 = clock()
        try:
            q = dict(getter(symbol) or {})
        except Exception as exc:  # noqa: BLE001
            q = {"ok": False, "msg1": repr(exc)[:120]}
        t1 = clock()
        age = quote_age_ms(q, t0, t1)
        ages.append(age)
        log[f"{prefix}quote_requested_at"] = _iso(t0)
        log[f"{prefix}quote_received_at"] = _iso(t1)
        log[f"{prefix}quote_age_ms"] = age
        log[f"{prefix}quote_attempts"] = i + 1
        ok = bool(q.get("ok")) and float(q.get("ask1") or 0) > 0 and not (q.get("stale") or q.get("is_stale"))
        if ok and age <= max_age_ms:
            log[f"{prefix}quote_ages_ms"] = ages
            return q, "OK"
        reason = BLOCK_QUOTE_TOO_OLD if ok else "ASK_QUOTE_FAILED"
        if i < attempts - 1:
            sleep(float(config.EXEC_QUOTE_RETRY_DELAY_SEC))
    log[f"{prefix}quote_ages_ms"] = ages
    log[f"{prefix}quote_block_reason"] = reason
    return None, reason


def _status(broker, order_id: str, symbol: str) -> dict:
    fn = getattr(broker, "get_order_status", None)
    if fn is None:
        return {"ok": False, "error": "get_order_status unavailable"}
    try:
        return dict(fn(order_id, symbol=symbol) or {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:200]}


def classify(st: dict, requested_qty: int) -> str:
    filled = int(st.get("filled_qty") or 0)
    rem = int(st.get("remaining_qty") or 0)
    if filled >= requested_qty > 0:
        return "FILLED"
    if st.get("cancelled") or (rem == 0 and filled < requested_qty and st.get("found")):
        return "CANCELLED"
    return "PARTIAL" if filled > 0 else "OPEN"


def poll_fill(broker, order_id: str, symbol: str, requested_qty: int, log: dict, *, prefix: str = "",
              sleep: Callable = time.sleep, clock: Callable[[], datetime] = _now) -> dict:
    """주문번호 기준 체결확인. 반환 {"state", "filled_qty", "amount", "avg_price", "checks", "status_ok"}."""
    interval = max(0.0, float(config.EXEC_FILL_CHECK_INTERVAL_SEC))
    wait_max = max(0.0, float(config.EXEC_FILL_WAIT_MAX_SEC))
    start = clock()
    checks, any_ok, last = 0, False, {}
    while True:
        st = _status(broker, order_id, symbol)
        checks += 1
        if st.get("ok") and st.get("found"):
            any_ok, last = True, st
            if int(st.get("filled_qty") or 0) > 0 and not log.get(f"{prefix}first_fill_at"):
                log[f"{prefix}first_fill_at"] = _iso(clock())
            state = classify(st, requested_qty)
            if state in ("FILLED", "CANCELLED"):
                break
        if (clock() - start).total_seconds() >= wait_max:
            break
        sleep(interval)
    filled = int(last.get("filled_qty") or 0)
    amount = float(last.get("amount") or 0.0)
    log[f"{prefix}fill_checks"] = checks
    log[f"{prefix}fill_wait_sec"] = round((clock() - start).total_seconds(), 3)
    return {"state": classify(last, requested_qty) if any_ok else STATUS_UNAVAILABLE, "filled_qty": filled,
            "amount": amount, "avg_price": (amount / filled) if filled else float(last.get("avg_price") or 0.0),
            "checks": checks, "status_ok": any_ok, "remaining_qty": int(last.get("remaining_qty") or 0)}


def cancel_and_confirm(broker, order_id: str, symbol: str, requested_qty: int, log: dict, *, prefix: str = "",
                       sleep: Callable = time.sleep, clock: Callable[[], datetime] = _now) -> dict:
    """반환 {"confirmed": bool, "filled_qty", "amount", "cancel_result"}. 확정 = 상태조회에서 잔량 0."""
    log[f"{prefix}cancel_requested_at"] = _iso(clock())
    res = broker.cancel_order_v2(order_id, symbol)
    log[f"{prefix}cancel_result"] = "OK" if getattr(res, "success", False) else str(getattr(res, "message", ""))
    last = {}
    for i in range(max(1, int(config.EXEC_CANCEL_CONFIRM_CHECKS))):
        st = _status(broker, order_id, symbol)
        if st.get("ok") and st.get("found"):
            last = st
            filled = int(st.get("filled_qty") or 0)
            if filled > 0 and not log.get(f"{prefix}first_fill_at"):
                log[f"{prefix}first_fill_at"] = _iso(clock())
            if int(st.get("remaining_qty") or 0) <= 0:       # 잔량 0 = 취소(또는 그 사이 전량체결) 확정
                log[f"{prefix}cancel_confirmed_at"] = _iso(clock())
                return {"confirmed": True, "filled_qty": filled, "amount": float(st.get("amount") or 0.0),
                        "cancel_result": log[f"{prefix}cancel_result"]}
        if i < int(config.EXEC_CANCEL_CONFIRM_CHECKS) - 1:
            sleep(float(config.EXEC_CANCEL_CONFIRM_INTERVAL_SEC))
    return {"confirmed": False, "filled_qty": int(last.get("filled_qty") or 0),
            "amount": float(last.get("amount") or 0.0), "cancel_result": log[f"{prefix}cancel_result"]}


def run_buy(broker, *, symbol: str, qty: int, order_price: float, reference_ask: float, usable_cash: float,
            client_order_id: str, first_result, log: dict, tick_fn: Callable[[float], float],
            sleep: Callable = time.sleep, clock: Callable[[], datetime] = _now) -> dict:
    """이미 접수된 첫 주문(first_result)부터 시작한다. 반환 dict:
      final       : FILLED / PARTIAL_CANCEL_CONFIRMED / NO_FILL_CANCEL_CONFIRMED / CANCEL_UNCONFIRMED /
                    CANCEL_UNAVAILABLE / STATUS_UNAVAILABLE / REPRICE_ABORTED_SLIPPAGE / REPRICE_QUOTE_UNAVAILABLE /
                    REPRICE_ORDER_REJECTED
      filled_qty, avg_price, amount : 체결이 있는 **마지막 주문** 기준(재주문은 0주 체결+취소확정 뒤에만 하므로
                                      체결이 있는 주문은 최대 1개)
      buy_result  : 그 주문의 BrokerOrderResult (체결원장 order_id)
      requested_qty, order_price : 그 주문의 값
      open_order  : 취소 미확인으로 살아 있을 수 있는 주문 {"order_id","qty","price","filled","amount"} 또는 None
    """
    cur, cur_qty, cur_price = first_result, int(qty), float(order_price)
    reprice_count = 0
    log["reprice_count"] = 0
    log.setdefault("orders", []).append({"order_id": cur.order_id, "qty": cur_qty, "price": cur_price})
    while True:
        p = f"o{reprice_count}_" if reprice_count else ""
        fill = poll_fill(broker, cur.order_id, symbol, cur_qty, log, prefix=p, sleep=sleep, clock=clock)
        if fill["state"] == STATUS_UNAVAILABLE:
            return {"final": STATUS_UNAVAILABLE, "buy_result": cur, "requested_qty": cur_qty, "order_price": cur_price,
                    "filled_qty": 0, "avg_price": 0.0, "amount": 0.0, "open_order": None, "reprice_count": reprice_count}
        if fill["state"] == "FILLED":
            return {"final": FILLED, "buy_result": cur, "requested_qty": cur_qty, "order_price": cur_price,
                    "filled_qty": fill["filled_qty"], "avg_price": fill["avg_price"], "amount": fill["amount"],
                    "open_order": None, "reprice_count": reprice_count}
        if fill["state"] == "CANCELLED":                     # 이미 취소(거부·소멸)로 끝남
            conf = {"confirmed": True, "filled_qty": fill["filled_qty"], "amount": fill["amount"]}
        elif not (getattr(broker, "cancel_v2_allowed", None) and broker.cancel_v2_allowed()):
            return {"final": CANCEL_UNAVAILABLE, "buy_result": cur, "requested_qty": cur_qty, "order_price": cur_price,
                    "filled_qty": fill["filled_qty"], "avg_price": fill["avg_price"], "amount": fill["amount"],
                    "open_order": None, "reprice_count": reprice_count}
        else:
            conf = cancel_and_confirm(broker, cur.order_id, symbol, cur_qty, log, prefix=p, sleep=sleep, clock=clock)
        filled, amount = int(conf["filled_qty"]), float(conf["amount"])
        avg = (amount / filled) if filled else 0.0
        if not conf["confirmed"]:
            # 절대 재주문하지 않는다. 남은 주문은 pending_order 추적이 맡는다.
            return {"final": CANCEL_UNCONFIRMED, "buy_result": cur, "requested_qty": cur_qty, "order_price": cur_price,
                    "filled_qty": filled, "avg_price": avg, "amount": amount, "reprice_count": reprice_count,
                    "open_order": {"order_id": cur.order_id, "qty": cur_qty, "price": cur_price,
                                   "filled": filled, "amount": amount}}
        if filled > 0:
            return {"final": FILLED if filled >= cur_qty else PARTIAL_CANCEL_CONFIRMED, "buy_result": cur,
                    "requested_qty": cur_qty, "order_price": cur_price, "filled_qty": filled, "avg_price": avg,
                    "amount": amount, "open_order": None, "reprice_count": reprice_count}
        # 0주 + 취소 확정 → repricing 여부
        slip = float(config.EXEC_REPRICE_MAX_SLIPPAGE_PCT or 0.0)
        base = {"buy_result": cur, "requested_qty": cur_qty, "order_price": cur_price, "filled_qty": 0,
                "avg_price": 0.0, "amount": 0.0, "open_order": None, "reprice_count": reprice_count}
        if not config.EXEC_REPRICE_ENABLED or slip <= 0 or reprice_count >= int(config.EXEC_REPRICE_MAX):
            return {"final": NO_FILL_CANCEL_CONFIRMED, **base}
        q, why = fetch_fresh_ask(broker, symbol, log, prefix=f"r{reprice_count + 1}_", sleep=sleep, clock=clock)
        if q is None:
            log["reprice_abort_reason"] = why
            return {"final": REPRICE_QUOTE_UNAVAILABLE, **base}
        new_price = float(int(float(q["ask1"]) + tick_fn(float(q["ask1"]))))
        cap = float(reference_ask) * (1.0 + slip / 100.0)
        log[f"r{reprice_count + 1}_price"] = new_price
        log[f"r{reprice_count + 1}_cap"] = round(cap, 2)
        if new_price > cap:
            log["reprice_abort_reason"] = f"SLIPPAGE {new_price} > cap {cap:.2f} (ref ask {reference_ask}, {slip}%)"
            return {"final": REPRICE_ABORTED_SLIPPAGE, **base}
        new_qty = min(int(qty), int(float(usable_cash) // new_price)) if new_price > 0 else 0
        if new_qty < 1:
            log["reprice_abort_reason"] = "NO_QTY_AT_NEW_PRICE"
            return {"final": REPRICE_ABORTED_SLIPPAGE, **base}
        log[f"r{reprice_count + 1}_order_sent_at"] = _iso(clock())
        nxt = broker.buy_limit(symbol, new_qty, new_price, f"{client_order_id}:R{reprice_count + 1}")
        log[f"r{reprice_count + 1}_broker_accepted_at"] = _iso(clock())
        if not getattr(nxt, "success", False) or not getattr(nxt, "order_id", ""):
            log["reprice_abort_reason"] = f"REORDER_REJECTED:{getattr(nxt, 'message', '')}"
            return {"final": REPRICE_ORDER_REJECTED, **base}
        reprice_count += 1
        log["reprice_count"] = reprice_count
        log["orders"].append({"order_id": nxt.order_id, "qty": new_qty, "price": new_price})
        logger.warning("[MACD2][EXEC_V2] 재주문 #%s %s %s주 @%s (이전 %s 0주·취소확정, 상한 %.2f)",
                       reprice_count, symbol, new_qty, new_price, cur.order_id, cap)
        cur, cur_qty, cur_price = nxt, new_qty, new_price
