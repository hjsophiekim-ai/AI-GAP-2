"""부분체결 잔량 재호가 (2026-10-23, feature/macd2-partial-fill-reprice) — **연구용, production 미연결**.

배경: 2026-10-23 12:33 레버리지 지정가 4,950 매수가 117주만 체결되고 가격이 4,990 으로 달아났다.
기존 경로(order_executor)는 잔고 첫 조회에서 117주가 보이자 그대로 확정했고, 실계좌 취소가 막혀 있어
잔량은 CANCEL_FAILED 로 살아 남았다. 이 모듈은 그 잔량을 **안전하게** 재호가하는 실행 루프다.

이 모듈은 order_executor / worker 어디에서도 import 하지 않는다. 진입 판정·청산과 무관하다.

원칙 (사용자 지시 2026-10-23):
  - 새 주문을 겹쳐 넣지 않는다. 살아 있는 주문은 언제나 최대 1개다.
  - 재호가 전에 기존 주문의 체결/잔량을 주문번호로 먼저 조회한다.
  - 기존 잔량의 취소가 **확정**(상태조회에서 잔량 0)된 뒤에만 새 주문을 낸다.
  - 취소가 확정되지 않으면(CANCEL_FAILED) 더는 사지 않는다.
  - 새 주문 수량 = 목표수량 - 지금까지 확정된 총 체결수량. 목표수량을 넘을 수 없다.

동작:
  1. 최초 지정가 주문(이미 접수된 first_result 부터 시작)
  2. first_check_sec 뒤 주문번호로 체결상태 확인
  3. 체결률 < fill_threshold 이고 잔량이 있으면: 신호 유효 / 최신 매도호가 / 추격폭 / TP 근접 확인
  4. 통과하면 잔량 취소 -> 확정 -> 최신 호가로 잔량 재주문 (최대 max_reprice 회)
  5. 하나라도 탈락하면 잔량 취소 후 포기(이미 체결된 수량은 그대로 보유)
  체결률 >= fill_threshold 면 재호가하지 않고 잔량만 취소한다(accept_wait_sec 동안은 자연 체결을 기다린다).

재호가 가격 후보(price_mode):
  ASK            최신 매도1호가 그대로(지정가). 가격 상한이 정확하다. 호가가 그 사이 올라가면 또 미체결될 수 있다.
  ASK_PLUS_TICK  매도1호가 + 1틱(지정가). 체결 확률↑, 1틱 더 비싸다. 상한 검사는 이 가격으로 한다.
  BEST_LIMIT     최유리지정가(KIS ORD_DVSN 03). 접수 순간의 매도1호가로 체결되므로 **주문 시점 가격을 모른다**.
                 상한 검사는 직전 매도1호가로만 할 수 있어 상한이 보장되지 않는다. broker.buy_best_limit 가
                 있어야 하며(현재 broker_adapter 에는 없다) 없으면 재호가하지 않는다.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

KST = timezone(timedelta(hours=9))

HARD_MAX_REPRICE = 2

PRICE_ASK = "ASK"
PRICE_ASK_PLUS_TICK = "ASK_PLUS_TICK"
PRICE_BEST_LIMIT = "BEST_LIMIT"

# 최종 결과
FILLED = "FILLED"                                   # 목표수량 전부 체결
PARTIAL_ACCEPTED = "PARTIAL_ACCEPTED"               # 체결률 >= threshold, 잔량 취소 확정
ABANDONED_CHASE_LIMIT = "ABANDONED_CHASE_LIMIT"     # 추격폭 초과 -> 잔량 취소 확정 후 포기
ABANDONED_TP_NEAR = "ABANDONED_TP_NEAR"             # TP 근처 -> 추격 금지, 잔량 취소 확정
ABANDONED_SIGNAL_INVALID = "ABANDONED_SIGNAL_INVALID"
ABANDONED_MAX_REPRICE = "ABANDONED_MAX_REPRICE"     # 재호가 한도 소진 후 남은 잔량 취소 확정
ABANDONED_QUOTE_UNAVAILABLE = "ABANDONED_QUOTE_UNAVAILABLE"
ABANDONED_ORDER_REJECTED = "ABANDONED_ORDER_REJECTED"
ABANDONED_ORDER_UNSUPPORTED = "ABANDONED_ORDER_UNSUPPORTED"
ABANDONED_NO_CASH = "ABANDONED_NO_CASH"             # 재호가 가격에서 현금 상한 안에 1주도 못 삼
CANCEL_FAILED = "CANCEL_FAILED"                     # 취소 미확정 -> 추가 매수 금지, 살아 있을 수 있는 주문 보고
STATUS_UNAVAILABLE = "STATUS_UNAVAILABLE"           # 주문번호 조회 불가 -> 체결수량을 모르므로 아무것도 하지 않음


@dataclass(frozen=True)
class RepriceParams:
    first_check_sec: float = 3.0         # 주문 후 첫 체결 확인까지
    fill_threshold: float = 0.70         # 이 체결률 미만일 때만 재호가
    max_reprice: int = 2                 # 재호가 최대 횟수
    max_chase_pct: float = 0.30          # 최초 주문가 대비 최대 추격폭(%)
    price_mode: str = PRICE_ASK
    accept_wait_sec: float = 0.0         # 체결률 >= threshold 일 때 잔량 자연체결 대기(초), 그 뒤 취소
    tp_pct: Optional[float] = 1.5        # 고정 익절폭(%). None 이면 TP 근접 검사 안 함
    tp_near_pct: float = 1.0             # 재호가 가격이 '기준가 x (1 + (tp_pct - tp_near_pct)%)' 이상이면 추격 금지
    status_retries: int = 3              # 상태조회 실패 시 재시도 횟수
    status_retry_sec: float = 0.5
    cancel_confirm_checks: int = 5
    cancel_confirm_sec: float = 1.0
    max_quote_age_sec: float = 2.0


@dataclass
class RepriceState:
    TARGET_QTY: int
    ORIGINAL_ORDER_ID: str
    FILLED_QTY: int = 0                  # 모든 주문의 확정 체결수량 합
    REMAINING_QTY: int = 0               # TARGET_QTY - FILLED_QTY
    CANCEL_CONFIRMED: Optional[bool] = None   # 마지막 취소 요청의 확정 여부 (None = 취소 요청 없음)
    REPRICE_COUNT: int = 0
    FILLED_AMOUNT: float = 0.0
    orders: list = field(default_factory=list)   # {"order_id","qty","price","kind","filled","amount","terminal"}
    events: list = field(default_factory=list)   # (iso시각, 이벤트, 상세)

    @property
    def avg_price(self) -> float:
        return self.FILLED_AMOUNT / self.FILLED_QTY if self.FILLED_QTY else 0.0

    def live_orders(self) -> list:
        return [o for o in self.orders if not o["terminal"]]


@dataclass
class RepriceResult:
    final: str
    state: RepriceState
    open_order: Optional[dict] = None    # CANCEL_FAILED / STATUS_UNAVAILABLE 일 때 살아 있을 수 있는 주문

    def to_dict(self) -> dict:
        d = asdict(self.state)
        d["avg_price"] = self.state.avg_price
        return {"final": self.final, "state": d, "open_order": self.open_order}


class InvariantViolation(RuntimeError):
    """겹친 주문 / 목표수량 초과 시도. 발생하면 버그다 -- 주문을 내기 전에 막는다."""


def _now() -> datetime:
    return datetime.now(KST)


def _floor_to_tick(price: float, tick_fn: Callable[[float], float]) -> float:
    t = float(tick_fn(price))
    return math.floor(price / t + 1e-9) * t


class PartialFillRepricer:
    """한 매수 신호의 실행 루프. broker 는 order_execution_v2 와 같은 인터페이스를 쓴다:
      get_order_status(order_id, symbol=) -> {"ok","found","filled_qty","remaining_qty","amount","cancelled"}
      cancel_v3_allowed() / cancel_order_v3(order_id, symbol) -> BrokerOrderResult(success, ...)  (취소 v3, 주문당 1회)
      get_fresh_ask1(symbol) -> {"ok","ask1","age_sec"}
      buy_limit(symbol, qty, price, client_order_id) -> BrokerOrderResult(success, order_id, ...)
      buy_best_limit(symbol, qty, client_order_id)   (선택, BEST_LIMIT 모드 전용)
    """

    def __init__(self, broker, *, symbol: str, target_qty: int, order_price: float, first_result,
                 client_order_id: str, params: RepriceParams = RepriceParams(),
                 signal_valid: Callable[[], bool] = lambda: True,
                 tick_fn: Optional[Callable[[float], float]] = None,
                 max_notional: Optional[float] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], datetime] = _now):
        if tick_fn is None:
            from app.utils.stock_utils import get_tick_size
            tick_fn = get_tick_size
        # 재호가 횟수는 어떤 설정으로도 2 를 넘지 않는다(무한 재주문 방지 하드 상한).
        params = replace(params, max_reprice=max(0, min(HARD_MAX_REPRICE, int(params.max_reprice))))
        self.broker, self.symbol, self.p = broker, symbol, params
        self.order_price = float(order_price)
        # 총 매수금액 상한(원). 재호가는 더 비싼 가격이라 잔량 x 새 가격이 남은 현금을 넘지 않게 수량을 줄인다.
        self.max_notional = float(max_notional) if max_notional else None
        self.client_order_id = client_order_id
        self.signal_valid, self.tick_fn, self.sleep, self.clock = signal_valid, tick_fn, sleep, clock
        self.st = RepriceState(TARGET_QTY=int(target_qty), ORIGINAL_ORDER_ID=str(first_result.order_id),
                               REMAINING_QTY=int(target_qty))
        self.chase_cap = _floor_to_tick(self.order_price * (1.0 + params.max_chase_pct / 100.0), tick_fn)
        self._add_order(first_result.order_id, int(target_qty), self.order_price, "INITIAL")

    # ── 기록 ────────────────────────────────────────────────────────────
    def _ev(self, name: str, **detail) -> None:
        self.st.events.append((self.clock().isoformat(timespec="milliseconds"), name, detail))

    def _add_order(self, order_id, qty: int, price: float, kind: str) -> None:
        self.st.orders.append({"order_id": str(order_id), "qty": int(qty), "price": float(price), "kind": kind,
                               "filled": 0, "amount": 0.0, "terminal": False})

    def _recompute(self) -> None:
        self.st.FILLED_QTY = sum(int(o["filled"]) for o in self.st.orders)
        self.st.FILLED_AMOUNT = sum(float(o["amount"]) for o in self.st.orders)
        self.st.REMAINING_QTY = max(self.st.TARGET_QTY - self.st.FILLED_QTY, 0)
        if self.st.FILLED_QTY > self.st.TARGET_QTY:
            raise InvariantViolation(f"체결 {self.st.FILLED_QTY} > 목표 {self.st.TARGET_QTY}")

    # ── 조회 ────────────────────────────────────────────────────────────
    def _status(self, order: dict) -> Optional[dict]:
        """주문번호 조회. 성공하면 주문 dict 의 체결수량/금액을 갱신(감소는 무시)하고 상태를 돌려준다."""
        for i in range(max(1, self.p.status_retries)):
            try:
                st = dict(self.broker.get_order_status(order["order_id"], symbol=self.symbol) or {})
            except Exception as exc:  # noqa: BLE001
                st = {"ok": False, "error": repr(exc)[:120]}
            if st.get("ok") and st.get("found"):
                filled = min(int(st.get("filled_qty") or 0), int(order["qty"]))
                if filled >= int(order["filled"]):
                    order["filled"] = filled
                    amt = st.get("amount")
                    order["amount"] = float(amt) if amt not in (None, "") else filled * order["price"]
                rem = int(st.get("remaining_qty") if st.get("remaining_qty") is not None
                          else order["qty"] - order["filled"])
                if order["filled"] >= order["qty"] or rem <= 0 or st.get("cancelled"):
                    order["terminal"] = True
                self._recompute()
                return st
            if i < self.p.status_retries - 1:
                self.sleep(self.p.status_retry_sec)
        self._ev("STATUS_FAILED", order_id=order["order_id"])
        return None

    def _fresh_ask(self) -> Optional[float]:
        try:
            q = dict(self.broker.get_fresh_ask1(self.symbol) or {})
        except Exception as exc:  # noqa: BLE001
            self._ev("QUOTE_ERROR", error=repr(exc)[:120])
            return None
        if q.get("stale") or q.get("is_stale") or not q.get("ok") or float(q.get("ask1") or 0) <= 0:
            self._ev("QUOTE_UNAVAILABLE", quote={k: q.get(k) for k in ("ok", "ask1", "age_sec", "stale")})
            return None
        age = q.get("age_sec")
        if age is not None and float(age) > self.p.max_quote_age_sec:
            self._ev("QUOTE_TOO_OLD", age_sec=age)
            return None
        return float(q["ask1"])

    # ── 취소 ────────────────────────────────────────────────────────────
    def _cancel_and_confirm(self, order: dict) -> bool:
        """잔량 취소 요청 -> 상태조회로 잔량 0 확인. 확정이면 True. 그 사이 체결분은 order 에 반영된다."""
        if order["terminal"]:
            self.st.CANCEL_CONFIRMED = True
            return True
        allowed = getattr(self.broker, "cancel_v3_allowed", None)
        if not (allowed and allowed()):
            self._ev("CANCEL_UNAVAILABLE", order_id=order["order_id"])
            self.st.CANCEL_CONFIRMED = False
            return False
        try:
            res = self.broker.cancel_order_v3(order["order_id"], self.symbol)
            accepted = bool(getattr(res, "success", False))
            msg = str(getattr(res, "message", ""))
        except Exception as exc:  # noqa: BLE001
            accepted, msg = False, f"CANCEL_ERROR:{exc}"
        self._ev("CANCEL_REQUESTED", order_id=order["order_id"], accepted=accepted, message=msg)
        # 접수 거절이어도 상태는 확인한다 -- 그 사이 전량체결돼 '취소할 잔량 없음'으로 거절됐을 수 있다.
        for i in range(max(1, self.p.cancel_confirm_checks)):
            st = self._status(order)
            if st is not None and order["terminal"]:
                self.st.CANCEL_CONFIRMED = True
                self._ev("CANCEL_CONFIRMED", order_id=order["order_id"], filled=order["filled"])
                return True
            if i < self.p.cancel_confirm_checks - 1:
                self.sleep(self.p.cancel_confirm_sec)
        self.st.CANCEL_CONFIRMED = False
        self._ev("CANCEL_NOT_CONFIRMED", order_id=order["order_id"], filled=order["filled"])
        return False

    # ── 재호가 판단 ─────────────────────────────────────────────────────
    def _reprice_price(self, ask: float) -> float:
        if self.p.price_mode == PRICE_ASK_PLUS_TICK:
            return ask + float(self.tick_fn(ask))
        return ask                       # ASK, BEST_LIMIT(추정가 = 직전 매도1호가)

    def _veto(self, price: float) -> Optional[str]:
        """재호가 금지 사유. None 이면 통과."""
        if self.p.price_mode == PRICE_BEST_LIMIT and getattr(self.broker, "buy_best_limit", None) is None:
            return ABANDONED_ORDER_UNSUPPORTED
        if price > self.chase_cap:
            return ABANDONED_CHASE_LIMIT
        if self.p.tp_pct is not None:
            base = self.st.avg_price or self.order_price
            near = base * (1.0 + (self.p.tp_pct - self.p.tp_near_pct) / 100.0)
            if price >= near:
                return ABANDONED_TP_NEAR
        return None

    def _place(self, qty: int, price: float):
        if self.st.live_orders():
            raise InvariantViolation(f"살아 있는 주문이 있는데 새 주문 시도: {self.st.live_orders()}")
        if qty < 1 or self.st.FILLED_QTY + qty > self.st.TARGET_QTY:
            raise InvariantViolation(f"수량 {qty} + 체결 {self.st.FILLED_QTY} > 목표 {self.st.TARGET_QTY}")
        coid = f"{self.client_order_id}:PF{self.st.REPRICE_COUNT + 1}"
        if self.p.price_mode == PRICE_BEST_LIMIT:
            fn = getattr(self.broker, "buy_best_limit", None)
            if fn is None:
                return None, ABANDONED_ORDER_UNSUPPORTED
            res = fn(self.symbol, qty, coid)
        else:
            res = self.broker.buy_limit(self.symbol, qty, price, coid)
        if not getattr(res, "success", False) or not getattr(res, "order_id", ""):
            self._ev("REORDER_REJECTED", message=str(getattr(res, "message", "")))
            return None, ABANDONED_ORDER_REJECTED
        return res, None

    # ── 메인 루프 ───────────────────────────────────────────────────────
    def _finish(self, final: str, order: Optional[dict] = None) -> RepriceResult:
        self._recompute()
        if self.st.FILLED_QTY >= self.st.TARGET_QTY and final not in (CANCEL_FAILED, STATUS_UNAVAILABLE):
            final = FILLED
        open_order = None
        if final in (CANCEL_FAILED, STATUS_UNAVAILABLE) and order is not None:
            open_order = {k: order[k] for k in ("order_id", "qty", "price", "filled", "amount")}
        self._ev("FINAL", final=final, filled=self.st.FILLED_QTY, target=self.st.TARGET_QTY)
        return RepriceResult(final=final, state=self.st, open_order=open_order)

    def _abandon(self, reason: str, order: dict) -> RepriceResult:
        if not self._cancel_and_confirm(order):
            return self._finish(CANCEL_FAILED, order)
        return self._finish(reason)

    def run(self) -> RepriceResult:
        order = self.st.orders[0]
        while True:
            self.sleep(self.p.first_check_sec)
            if self._status(order) is None:
                return self._finish(STATUS_UNAVAILABLE, order)
            if self.st.REMAINING_QTY <= 0:
                return self._finish(FILLED)
            ratio = self.st.FILLED_QTY / self.st.TARGET_QTY
            self._ev("CHECK", order_id=order["order_id"], filled=self.st.FILLED_QTY, ratio=round(ratio, 4))

            if order["terminal"]:
                # 이미 취소/소멸된 주문(거래소 거부 등) -- 잔량은 재호가 대상이지만 '취소 확정' 상태와 같다.
                self.st.CANCEL_CONFIRMED = True
            if ratio >= self.p.fill_threshold:
                waited = 0.0
                while not order["terminal"] and waited < self.p.accept_wait_sec:
                    self.sleep(min(1.0, self.p.accept_wait_sec - waited))
                    waited += min(1.0, self.p.accept_wait_sec - waited)
                    if self._status(order) is None:
                        return self._finish(STATUS_UNAVAILABLE, order)
                if self.st.REMAINING_QTY <= 0:
                    return self._finish(FILLED)
                return self._abandon(PARTIAL_ACCEPTED, order)

            if self.st.REPRICE_COUNT >= self.p.max_reprice:
                return self._abandon(ABANDONED_MAX_REPRICE, order)
            if not self.signal_valid():
                self._ev("SIGNAL_INVALID")
                return self._abandon(ABANDONED_SIGNAL_INVALID, order)
            ask = self._fresh_ask()
            if ask is None:
                return self._abandon(ABANDONED_QUOTE_UNAVAILABLE, order)
            veto = self._veto(self._reprice_price(ask))
            if veto:
                self._ev("REPRICE_VETO", reason=veto, ask=ask, cap=self.chase_cap)
                return self._abandon(veto, order)

            # 기존 잔량 취소 확정 -> 그 사이 체결분 반영 -> 남은 수량만 재주문
            if not self._cancel_and_confirm(order):
                return self._finish(CANCEL_FAILED, order)
            if self.st.REMAINING_QTY <= 0:
                return self._finish(FILLED)
            # 취소 확인에 시간이 걸렸으므로 호가와 조건을 다시 본다.
            if not self.signal_valid():
                self._ev("SIGNAL_INVALID")
                return self._finish(ABANDONED_SIGNAL_INVALID)
            ask = self._fresh_ask()
            if ask is None:
                return self._finish(ABANDONED_QUOTE_UNAVAILABLE)
            price = self._reprice_price(ask)
            veto = self._veto(price)
            if veto:
                self._ev("REPRICE_VETO", reason=veto, ask=ask, cap=self.chase_cap)
                return self._finish(veto)
            qty = self.st.REMAINING_QTY
            if self.max_notional is not None:
                qty = min(qty, int(max(0.0, self.max_notional - self.st.FILLED_AMOUNT) // price))
                if qty < 1:
                    self._ev("NO_CASH", price=price, filled_amount=self.st.FILLED_AMOUNT)
                    return self._finish(ABANDONED_NO_CASH)
            res, why = self._place(qty, price)
            if res is None:
                return self._finish(why)
            self.st.REPRICE_COUNT += 1
            self._add_order(res.order_id, qty, price, f"REPRICE{self.st.REPRICE_COUNT}")
            self._ev("REPRICED", n=self.st.REPRICE_COUNT, order_id=res.order_id, qty=qty, price=price,
                     cap=self.chase_cap)
            self.st.CANCEL_CONFIRMED = None
            order = self.st.orders[-1]


def run_partial_fill_reprice(broker, **kwargs: Any) -> RepriceResult:
    """편의 함수: PartialFillRepricer(broker, **kwargs).run()."""
    return PartialFillRepricer(broker, **kwargs).run()
