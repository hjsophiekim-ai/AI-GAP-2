"""SELL 실제 체결 계측 — 기록 전용 (2026-10-30).

배경: 시장가 SELL 의 주문 응답(order-cash)에는 체결가가 없다. 그래서 원장 SELL 행의
price 는 매도 확인 + reconcile 직후 받은 현재가(_fallback_sell_price)였고, 실제 체결가와
다를 수 있었다. 손절 연구(P70R 등)가 이 가격을 실제 체결로 오인하지 않도록 구분한다.

이 모듈이 하는 일:
  · SELL 레그가 원장에 기록될 때(order_executor._record_leg) 주문번호를 대기열에 넣는다.
    그 순간의 행은 기존과 똑같은 fallback 가격으로 기록되고 price_source=FALLBACK_QUOTE 다.
  · worker 루프가 run_once 를 마친 뒤 process_pending() 을 부른다. 기록 후
    FIRST_LOOKUP_DELAY_SEC 가 지난 주문만, tick 당 최대 1건, KIS 주문번호 조회
    (broker.get_order_status → inquire-daily-ccld) 로 실제 누적 체결수량·평균체결가를 읽는다.
  · 원장 체결수량과 같으면 executed_price/손익을 실제 평균체결가로 다시 쓰고
    price_source=ACTUAL_FILL. 조회 실패/0행(KIS 모의는 주문별 행이 항상 0)/수량 불일치면
    가격은 그대로 두고 fill_lookup_status 에 이유만 남긴다.

절대 하지 않는 것: 주문을 내거나 취소하지 않는다. 매도 흐름(반전 BUY 포함)을 기다리게 하지
않는다(조회는 run_once 밖). 어떤 예외도 밖으로 던지지 않는다. 청산·진입 판단은 원장 손익을
읽지 않으므로 이 갱신이 판단을 바꾸지 않는다.

한계(정직하게 기록): inquire-daily-ccld 는 주문 1건을 누적 1행으로 준다 → 분할체결 건별
가격/시각은 REST 로 얻을 수 없다. fill_granularity=ORDER_AGGREGATE 로 표시하고, 행이 여러 개
돌아오면 그대로 fill_details 에 남긴다. 건별 체결은 체결통보 WebSocket(H0STCNI0)이 필요하다.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Optional

from app.logger import logger
from app.trading.kis_realized import kis_reported_leg_cost
from app.trading.macd2 import config, ledger
from app.trading.trading_cost_engine import TradeCostEngine

PRICE_SOURCE_ACTUAL_FILL = "ACTUAL_FILL"
PRICE_SOURCE_FALLBACK_QUOTE = "FALLBACK_QUOTE"
PRICE_SOURCE_ORDER_RESPONSE = "ORDER_RESPONSE"
PRICE_SOURCE_FALLBACK_ENTRY = "FALLBACK_ENTRY_PRICE"

LOOKUP_PENDING = "PENDING"
LOOKUP_UNAVAILABLE = "LOOKUP_UNAVAILABLE"        # 브로커에 주문조회가 없음 (테스트/리플레이 fake)
GRANULARITY_ORDER_AGGREGATE = "ORDER_AGGREGATE"

FIRST_LOOKUP_DELAY_SEC = 3.0
RETRY_INTERVAL_SEC = 10.0
MAX_ATTEMPTS = 6
MAX_QUEUE = 50

_LOCK = threading.Lock()
_PENDING: list[dict[str, Any]] = []


def now_iso() -> str:
    return datetime.now(config.KST).isoformat()


def ord_tmd_of(raw: Any) -> str:
    """주문 응답의 ORD_TMD(HHMMSS, KST) — 실제 KIS 주문 접수시각. 없으면 ""."""
    if not isinstance(raw, dict):
        return ""
    value = str(raw.get("ORD_TMD") or raw.get("ord_tmd") or (raw.get("output") or {}).get("ORD_TMD") or "").strip()
    return value if len(value) == 6 and value.isdigit() else ""


def sell_leg_pnl(symbol: str, *, entry_price: float, qty: int, sell_amount: float, raw: Any) -> dict[str, float]:
    """order_executor._record_leg 의 SELL 손익 계산과 같은 식(실현손익, 슬리피지 0)."""
    cost = TradeCostEngine().compute_realized_pnl(
        symbol, buy_amount=float(entry_price) * qty, sell_amount=float(sell_amount),
        sell_fee_override=kis_reported_leg_cost(raw),
    )
    return {"gross_pnl": cost["gross_pnl"], "fee": cost["sell_fee"], "net_pnl": cost["net_pnl"]}


def register(*, order_id: str, symbol: str, qty: int, entry_price: float, raw: Any, mode: str) -> bool:
    """SELL 레그 기록 직후 실제 체결 조회 대기열에 넣는다. 넣었으면 True."""
    if not order_id or qty <= 0:
        return False
    item = {
        "order_id": str(order_id), "symbol": str(symbol), "qty": int(qty),
        "entry_price": float(entry_price or 0.0), "raw": dict(raw) if isinstance(raw, dict) else {},
        "mode": str(mode or ""), "registered_mono": time.monotonic(), "attempts": 0, "next_mono": 0.0,
        "trading_date": datetime.now(config.KST).strftime("%Y%m%d"),
    }
    item["next_mono"] = item["registered_mono"] + FIRST_LOOKUP_DELAY_SEC
    with _LOCK:
        if any(p["order_id"] == item["order_id"] for p in _PENDING):
            return False
        _PENDING.append(item)
        del _PENDING[:-MAX_QUEUE]
    return True


def pending_snapshot() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(p) for p in _PENDING]


def clear_pending() -> None:
    with _LOCK:
        _PENDING.clear()


def _hhmmss_to_iso(day: str, hhmmss: str) -> str:
    try:
        return datetime.strptime(day + hhmmss, "%Y%m%d%H%M%S").replace(tzinfo=config.KST).isoformat()
    except (TypeError, ValueError):
        return ""


def build_fill_fields(item: dict[str, Any], status: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """주문조회 결과 -> 원장 갱신 필드. (fields, done). done=False 면 다시 조회할 가치가 있다."""
    lookup_at = now_iso()
    if not status.get("ok"):
        return {"fill_lookup_status": f"LOOKUP_ERROR:{str(status.get('error') or '')[:120]}",
                "fill_lookup_at": lookup_at}, False
    if not status.get("found"):
        return {"fill_lookup_status": "ORDER_NOT_FOUND", "fill_lookup_at": lookup_at}, False
    filled = int(status.get("filled_qty") or 0)
    avg = float(status.get("avg_price") or 0.0)
    amount = float(status.get("amount") or 0.0)
    rows = list(status.get("fill_rows") or [])
    day = item.get("trading_date") or ""
    times = [str(t) for r in rows for k, t in (r.get("times") or {}).items() if k.lower() != "ord_tmd"]
    ord_tmd = str(status.get("ord_tmd") or "")
    fields: dict[str, Any] = {
        "fill_total_qty": filled, "fill_avg_price": avg if avg > 0 else "", "fill_amount": amount if amount > 0 else "",
        "fill_granularity": GRANULARITY_ORDER_AGGREGATE,
        "fill_details": json.dumps(rows, ensure_ascii=False)[:2000],
        "fill_lookup_at": lookup_at,
    }
    if ord_tmd:
        fields["kis_ord_tmd"] = ord_tmd
    # 체결 시각: REST 는 건별 시각을 주지 않는다. KIS 가 ord_tmd 외의 *tmd(예: 통보시각)를
    # 주면 그 최소/최대를 남기고, 없으면 비운다(추정하지 않는다).
    valid = sorted(t for t in times if len(t) == 6 and t.isdigit() and t != "000000")
    if valid:
        fields["fill_first_at"] = _hhmmss_to_iso(day, valid[0])
        fields["fill_last_at"] = _hhmmss_to_iso(day, valid[-1])
    if filled <= 0 or avg <= 0:
        fields["fill_lookup_status"] = "NO_FILL_YET"
        return fields, False
    if int(status.get("remaining_qty") or 0) > 0 and filled < int(item["qty"]):
        fields["fill_lookup_status"] = f"PARTIAL_OPEN:{filled}/{item['qty']}"
        return fields, False
    if filled != int(item["qty"]):
        # 원장 수량과 다르면 가격을 바꾸지 않는다(어느 쪽이 맞는지 모른다).
        fields["fill_lookup_status"] = f"QTY_MISMATCH:kis={filled},ledger={item['qty']}"
        return fields, True
    sell_amount = amount if amount > 0 else avg * filled
    fields.update(sell_leg_pnl(item["symbol"], entry_price=item["entry_price"], qty=filled,
                               sell_amount=sell_amount, raw=item.get("raw")))
    fields["executed_price"] = round(sell_amount / filled, 4)
    fields["price_source"] = PRICE_SOURCE_ACTUAL_FILL
    fields["fill_lookup_status"] = "OK"
    return fields, True


def process_pending(broker, *, max_lookups: int = 1, ledger_module: Any = None) -> list[str]:
    """대기 중인 SELL 주문 중 조회 시각이 된 것을 최대 ``max_lookups`` 건 조회해 원장을 갱신한다.

    worker 루프에서 run_once 뒤에 부른다. 예외를 던지지 않는다. 처리한 order_id 목록을 돌려준다."""
    lm = ledger_module if ledger_module is not None else ledger
    patcher = getattr(lm, "record_sell_fill_fields", None)
    getter = getattr(broker, "get_order_status", None)
    now_mono = time.monotonic()
    with _LOCK:
        due = [p for p in _PENDING if p["next_mono"] <= now_mono][:max(0, int(max_lookups))]
    handled: list[str] = []
    for item in due:
        try:
            if getter is None or patcher is None:
                fields, done = {"fill_lookup_status": LOOKUP_UNAVAILABLE, "fill_lookup_at": now_iso()}, True
            else:
                try:
                    status = dict(getter(item["order_id"], symbol=item["symbol"], trading_date=item["trading_date"]) or {})
                except Exception as exc:
                    status = {"ok": False, "error": str(exc)}
                fields, done = build_fill_fields(item, status)
            item["attempts"] += 1
            if not done and item["attempts"] >= MAX_ATTEMPTS:
                fields["fill_lookup_status"] = f"GAVE_UP:{fields.get('fill_lookup_status')}"
                done = True
            if patcher is not None:
                patcher(item["order_id"], fields)
            handled.append(item["order_id"])
            with _LOCK:
                if done:
                    _PENDING[:] = [p for p in _PENDING if p["order_id"] != item["order_id"]]
                else:
                    item["next_mono"] = time.monotonic() + RETRY_INTERVAL_SEC
        except Exception:
            logger.exception("[MACD2][SELL_FILL] 실제 체결 조회 처리 실패 %s -- 매매에는 영향 없음", item.get("order_id"))
            with _LOCK:
                _PENDING[:] = [p for p in _PENDING if p["order_id"] != item.get("order_id")]
    return handled
