"""MACD2 broker adapter — thin wrapper over the existing, shared broker layer.

Reuses app.trading.broker_factory.create_broker / KisMockBroker /
KisRealBroker / BrokerBase directly — these are generic trading
infrastructure shared by Enhanced and MACD v1, not MACD-v1 domain code (see
the 2026-07-23 code-reuse audit). This module does NOT import anything from
§9/§14 — REAL confirm/gate lives entirely inside KisRealBroker.__init__;
MockBrokerAdapter never touches it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.models import Position
from app.trading.broker_base import BrokerBase


@dataclass(frozen=True)
class BrokerOrderResult:
    success: bool
    order_id: str
    symbol: str
    side: str  # "BUY" / "SELL"
    requested_qty: int
    executed_qty: int
    executed_price: float
    message: str
    raw: dict[str, Any] = field(default_factory=dict)
    # 2026-09-29: 브로커 내부 gate 가 KIS 호출 전에 거절한 경우의 사유 코드
    # (예: "safety_daily_limit_exceeded"). KIS 가 거절했거나 성공이면 "".
    error_type: str = ""


@dataclass(frozen=True)
class BuySizingQuote:
    symbol: str
    order_type: str
    ord_dvsn: str
    orderable_cash: float
    nrcvb_buy_amt: float
    nrcvb_buy_qty: int
    psbl_qty_calc_unpr: float
    psbl_qty: int
    rt_cd: str
    msg_cd: str
    msg1: str
    ask1: float = 0.0
    order_price: float = 0.0
    usable_cash: float = 0.0
    limit_buyable_qty: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def _to_order_result(raw_result: Any, symbol: str, side: str, requested_qty: int) -> BrokerOrderResult:
    success = bool(getattr(raw_result, "success", False))
    raw = dict(getattr(raw_result, "raw", {}) or {})
    for key in ("rt_cd", "msg_cd", "msg1", "http_status"):
        value = getattr(raw_result, key, None)
        if value not in (None, "") and key not in raw:
            raw[key] = value
    return BrokerOrderResult(
        success=success,
        order_id=str(getattr(raw_result, "order_id", "") or ""),
        symbol=symbol,
        side=side,
        requested_qty=requested_qty,
        executed_qty=requested_qty if success else 0,
        executed_price=float(getattr(raw_result, "price", 0.0) or 0.0),
        message=str(getattr(raw_result, "message", "") or ""),
        raw=raw,
        error_type=str(getattr(raw_result, "error_type", "") or ""),
    )


class _BrokerAdapterBase:
    """Shared implementation for Mock/Real adapters (docs/MACD2_LOGIC.md §9 interface)."""

    mode: str
    _broker: BrokerBase

    @property
    def order_org_no(self) -> dict:
        """주문번호 -> KRX_FWDG_ORD_ORGNO (정정취소용, 2026-10-16). 프로세스 메모리에만."""
        d = self.__dict__.get("_order_org_no")
        if d is None:
            d = self.__dict__["_order_org_no"] = {}
        return d

    def get_cash(self) -> float:
        return float(self._broker.get_balance())

    def get_orderable_cash(self, symbol: str) -> float:
        # Query KIS's per-symbol 매수가능금액 (inquire-psbl-order) for THIS
        # target symbol/market-order — never the account-level
        # get_orderable_cash()/get_buyable_cash() convenience methods, which
        # silently query a hardcoded placeholder symbol ("005930") unrelated
        # to what MACD2 is actually about to trade. That mismatch let the
        # sizing figure diverge from what KIS actually accepts for the real
        # target ETN, producing a "주문가능금액 부족" rejection even though
        # compute_order_quantity's own min(cash, budget) math was correct
        # given the wrong cash figure it was fed (2026-07-27 incident).
        stock_getter = getattr(self._broker, "get_stock_buyable_amount", None)
        if stock_getter is not None:
            return float(stock_getter(symbol=symbol, price=0))
        getter = getattr(self._broker, "get_orderable_cash", None) or self._broker.get_buyable_cash
        return float(getter())

    def get_cash_diagnostics(self) -> dict[str, Any]:
        """진단 전용: 계좌 현금 필드 원값 스냅샷(KIS 잔고조회 output2). 주문 판단에 쓰지 않는다.

        2026-10-06: APBK3052(레버리지 기본예탁금 부족) 거절 순간에만 호출해, KIS 가 말한
        '현재 기본예탁금'이 어느 필드와 같은지 확정하는 근거로 남긴다. 실패해도 예외 없음."""
        kis = getattr(self._broker, "kis", None)
        getter = getattr(kis, "get_balance", None)
        if getter is None:
            return {"error": "get_balance unavailable"}
        try:
            # 2026-10-21: 진단 전용이므로 공유 TTL 캐시/동시호출 합치기를 쓴다
            # (주문 판단 경로는 여전히 직접 조회 -- app/trading/balance_cache.py).
            from app.trading import balance_cache

            bal = dict(balance_cache.get_balance_shared(kis, max_age_sec=5.0, fetch=getter) or {})
        except Exception as exc:
            return {"error": str(exc)}
        out: dict[str, Any] = {"balance_cash_fields": dict(bal.get("cash_fields") or {})}
        for key in ("cash", "orderable_cash", "error", "msg_cd", "as_of"):
            if bal.get(key) not in (None, ""):
                out[key] = bal.get(key)
        return out

    def get_buy_safety_room(self, symbol: str, price: float) -> Optional[dict[str, Any]]:
        """사용자 주문금액 한도 안에서 남은 금액 — 브로커가 지원할 때만(REAL).
        None 이면 cap 하지 않는다(MOCK/기타 브로커: 예전과 byte 단위 동일)."""
        getter = getattr(self._broker, "get_buy_safety_room", None)
        if getter is None:
            return None
        return dict(getter(symbol, price) or {})

    def release_daily_ordered_amount(self, amount: float) -> Optional[float]:
        """취소된 미체결 BUY 금액을 브로커 일일 누계에서 되돌린다(지원 시)."""
        releaser = getattr(self._broker, "release_daily_ordered_amount", None)
        if releaser is None:
            return None
        return releaser(amount)

    def get_buy_sizing_quote(self, symbol: str, *, price: float, order_type: str = "market") -> BuySizingQuote:
        ord_dvsn = "01" if order_type == "market" else ("11" if order_type == "ioc_limit" else "00")
        raw_getter = getattr(getattr(self._broker, "kis", None), "get_buyable_cash_raw", None)
        if raw_getter is not None:
            raw = dict(raw_getter(symbol=symbol, price=0 if order_type == "market" else int(price), ord_dvsn=ord_dvsn) or {})
            output = dict(raw.get("output") or {})
            orderable_cash = max(float(raw.get("nrcvb_buy_amt") or 0.0), float(raw.get("ord_psbl_cash") or 0.0))
            nrcvb_buy_qty = int(raw.get("nrcvb_buy_qty") or output.get("nrcvb_buy_qty") or 0)
            return BuySizingQuote(
                symbol=symbol,
                order_type=order_type,
                ord_dvsn=ord_dvsn,
                orderable_cash=orderable_cash,
                nrcvb_buy_amt=float(raw.get("nrcvb_buy_amt") or 0.0),
                nrcvb_buy_qty=nrcvb_buy_qty,
                psbl_qty_calc_unpr=float(raw.get("psbl_qty_calc_unpr") or output.get("psbl_qty_calc_unpr") or 0.0),
                psbl_qty=int(raw.get("psbl_qty") or 0),
                rt_cd=str(raw.get("rt_cd") or ""),
                msg_cd=str(raw.get("msg_cd") or ""),
                msg1=str(raw.get("msg1") or raw.get("error") or ""),
                order_price=float(price or 0.0),
                limit_buyable_qty=nrcvb_buy_qty if order_type in ("ioc_limit", "limit") else 0,
                raw=raw,
            )
        cash = self.get_orderable_cash(symbol)
        qty = int(cash // price) if price > 0 else 0
        return BuySizingQuote(
            symbol=symbol,
            order_type=order_type,
            ord_dvsn=ord_dvsn,
            orderable_cash=cash,
            nrcvb_buy_amt=cash,
            nrcvb_buy_qty=qty,
            psbl_qty_calc_unpr=price,
            psbl_qty=qty,
            rt_cd="",
            msg_cd="",
            msg1="",
            order_price=float(price or 0.0),
            limit_buyable_qty=qty if order_type in ("ioc_limit", "limit") else 0,
            raw={},
        )

    def get_fresh_ask1(self, symbol: str) -> dict[str, Any]:
        getter = getattr(getattr(self._broker, "kis", None), "get_orderbook_quote", None)
        if getter is not None:
            q = dict(getter(symbol) or {})
            # 2026-10-16(주문실행 v2 계측): 호가 접수시각(거래소, HHMMSS). 새 키만 추가 -- 기존 키는 그대로.
            raw = q.get("raw") or {}
            out = raw.get("output1") or raw.get("output") or {}
            if isinstance(out, list):
                out = out[0] if out else {}
            if isinstance(out, dict) and out.get("aspr_acpt_hour"):
                q["exch_time_hms"] = str(out.get("aspr_acpt_hour"))
            return q
        price = self.get_quote(symbol)
        return {
            "ok": bool(price and price > 0),
            "symbol": symbol,
            "ask1": float(price or 0.0),
            "rt_cd": "",
            "msg_cd": "",
            "msg1": "" if price else "ask1 unavailable",
            "raw": {},
        }

    def get_quote(self, symbol: str) -> Optional[float]:
        price = self._broker.get_current_price(symbol)
        return float(price) if price is not None else None

    def get_positions(self) -> list[Position]:
        return list(self._broker.get_positions())

    def refresh_kis_session(self) -> bool:
        """내부 브로커의 KISClient 를 새 인스턴스(새 requests.Session)로 교체한다
        (2026-10-21, position_feed 의 연결/세션 오류 복구 전용).

        같은 mode 로 ``create_kis_client`` 를 다시 부르므로 계좌/인증은 같고,
        토큰은 mode 단위 파일 캐시를 먼저 읽어 재발급하지 않는다. 교체는 참조
        한 번 바꾸기라, 다른 스레드에서 이미 진행 중인 호출은 옛 객체로 끝난다.
        ``kis`` 속성이 없는 브로커(테스트 더블/dry-run)면 아무것도 하지 않는다."""
        old = getattr(self._broker, "kis", None)
        if old is None:
            return False
        from app.trading.kis_client import create_kis_client

        fresh = create_kis_client(getattr(old, "mode", None) or self.mode)
        if fresh is None:
            return False
        self._broker.kis = fresh
        return True

    def get_position(self, symbol: str) -> Optional[Position]:
        for pos in self.get_positions():
            if pos.symbol == symbol:
                return pos
        return None

    def get_order_status(self, order_id: str, symbol: str = "", trading_date: str = "") -> dict[str, Any]:
        """주문번호 1건 상태(주문/체결/잔량/취소) — 조회 전용 (2026-10-16, pending_order 추적)."""
        getter = getattr(self._broker, "get_order_status", None) or getattr(
            getattr(self._broker, "kis", None), "get_order_status", None
        )
        if getter is None:
            return {"ok": False, "found": False, "error": "get_order_status unavailable"}
        try:
            return dict(getter(order_id, symbol=symbol, trading_date=trading_date) or {})
        except Exception as exc:
            return {"ok": False, "found": False, "error": str(exc)}

    def get_today_fills(self, symbol: str = "") -> dict[str, Any]:
        getter = getattr(self._broker, "get_today_fills", None) or getattr(
            getattr(self._broker, "kis", None), "get_today_fills", None
        )
        if getter is None:
            return {"ok": False, "fills": [], "error": "get_today_fills unavailable"}
        try:
            return dict(getter(symbol=symbol) or {})
        except TypeError:
            return dict(getter(symbol) or {})
        except Exception as exc:
            return {"ok": False, "fills": [], "error": str(exc)}

    def _call_broker_without_direct_ledger(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        old_value = getattr(self._broker, "_suppress_direct_execution_ledger", False)
        setattr(self._broker, "_suppress_direct_execution_ledger", True)
        try:
            return getattr(self._broker, method_name)(*args, **kwargs)
        finally:
            setattr(self._broker, "_suppress_direct_execution_ledger", old_value)

    def buy_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id  # not accepted by the underlying broker layer; kept for interface parity
        result = self._call_broker_without_direct_ledger(
            "buy", symbol, symbol, int(qty), 0, order_type="market",
        )
        return _to_order_result(result, symbol, "BUY", int(qty))

    def buy_ioc_limit(self, symbol: str, qty: int, price: float, client_order_id: str) -> BrokerOrderResult:
        """Legacy IOC path — kept for adapter compatibility; MACD2 Worker BUY
        must not call this (see buy_limit / order_executor.execute_signal)."""
        del client_order_id
        result = self._call_broker_without_direct_ledger(
            "buy", symbol, symbol, int(qty), int(price), order_type="ioc_limit",
        )
        return _to_order_result(result, symbol, "BUY", int(qty))

    def buy_limit(self, symbol: str, qty: int, price: float, client_order_id: str) -> BrokerOrderResult:
        """Regular limit BUY (ORD_DVSN=00). Preferred path for new MACD2 entries."""
        del client_order_id
        result = self._call_broker_without_direct_ledger(
            "buy", symbol, symbol, int(qty), int(price), order_type="limit",
        )
        out = _to_order_result(result, symbol, "BUY", int(qty))
        # 2026-10-16: 취소에 필요한 주문조직번호(KRX_FWDG_ORD_ORGNO)를 주문번호별로 기억한다(관측만, 동작 무관).
        org = (out.raw or {}).get("KRX_FWDG_ORD_ORGNO")
        if out.success and out.order_id and org:
            self.order_org_no[str(out.order_id)] = str(org)
        return out

    # ── 주문실행 v2: 국내 정정취소 (2026-10-16, MOCK 전용) ──────────────────────
    def cancel_v2_allowed(self) -> bool:
        """설정 두 개(EXEC_CANCEL_ENABLED, EXEC_CANCEL_MOCK_VERIFY)가 모두 켜져 있고 mode=="mock" 일 때만 True.
        실계좌(real)는 어떤 설정으로도 True 가 되지 않는다."""
        from app.trading.macd2 import config as _cfg
        return bool(getattr(_cfg, "EXEC_CANCEL_ENABLED", False) and getattr(_cfg, "EXEC_CANCEL_MOCK_VERIFY", False)
                    and getattr(self, "mode", "") == "mock")

    def cancel_order_v2(self, order_id: str, symbol: str = "", org_no: Optional[str] = None) -> BrokerOrderResult:
        """취소 **접수** 요청. 성공해도 취소 확정이 아니다(확정은 get_order_status 로 따로 본다)."""
        if not self.cancel_v2_allowed():
            return BrokerOrderResult(False, str(order_id or ""), symbol, "CANCEL", 0, 0, 0.0,
                                     "CANCEL_V2_DISABLED", raw={"mode": getattr(self, "mode", "")})
        fn = getattr(getattr(self._broker, "kis", None), "cancel_order_rvsecncl", None)
        org = org_no or self.order_org_no.get(str(order_id or ""))
        if fn is None or not org:
            return BrokerOrderResult(False, str(order_id or ""), symbol, "CANCEL", 0, 0, 0.0,
                                     "CANCEL_V2_UNAVAILABLE" if fn is None else "CANCEL_V2_NO_ORG_NO", raw={})
        try:
            r = dict(fn(str(order_id), str(org), symbol) or {})
        except Exception as exc:  # noqa: BLE001
            return BrokerOrderResult(False, str(order_id or ""), symbol, "CANCEL", 0, 0, 0.0,
                                     f"CANCEL_ERROR:{exc}", raw={"msg1": str(exc)})
        return BrokerOrderResult(bool(r.get("success")), str(order_id or ""), symbol, "CANCEL", 0, 0, 0.0,
                                 str(r.get("message") or ("OK" if r.get("success") else "CANCEL_FAILED")),
                                 raw={k: r.get(k) for k in ("order_id", "rt_cd", "msg_cd", "msg1")})

    # ── 부분체결 재호가용 취소 v3 (2026-10-23 -- order_executor 의 잔량 처리만 쓴다. 청산 경로는 쓰지 않는다) ──
    def cancel_v3_allowed(self) -> bool:
        """모의/실전 브로커면 True. 비상차단 PARTIAL_FILL_REPRICE_KILL 이 켜져 있으면 False."""
        from app.trading.macd2 import config as _cfg
        if bool(getattr(_cfg, "PARTIAL_FILL_REPRICE_KILL", False)):
            return False
        return getattr(self, "mode", "") in ("mock", "real")

    @property
    def cancel_v3_sent(self) -> dict:
        """주문번호 -> 첫 취소 요청 결과. 같은 주문에 두 번째 취소 HTTP 를 보내지 않기 위한 기록(프로세스 메모리)."""
        d = self.__dict__.get("_cancel_v3_sent")
        if d is None:
            d = self.__dict__["_cancel_v3_sent"] = {}
        return d

    def cancel_order_v3(self, order_id: str, symbol: str = "", org_no: Optional[str] = None) -> BrokerOrderResult:
        """잔량 전부 취소 **요청**(주문번호당 최대 1회). success=True 는 '접수'일 뿐 확정이 아니다 --
        확정은 get_order_status 의 잔량 0 으로 호출자가 따로 본다. raw["status"] 에 KIS 결과 상태
        (ACCEPTED/REJECTED/UNKNOWN/...)를 담는다. 두 번째 요청은 HTTP 없이 DUPLICATE_SUPPRESSED 로 돌려준다."""
        oid = str(order_id or "")
        if not self.cancel_v3_allowed():
            return BrokerOrderResult(False, oid, symbol, "CANCEL", 0, 0, 0.0, "CANCEL_V3_DISABLED",
                                     raw={"status": "DISABLED", "mode": getattr(self, "mode", "")})
        prev = self.cancel_v3_sent.get(oid)
        if prev is not None:
            return BrokerOrderResult(False, oid, symbol, "CANCEL", 0, 0, 0.0, "DUPLICATE_SUPPRESSED",
                                     raw={**prev, "duplicate": True})
        fn = getattr(getattr(self._broker, "kis", None), "cancel_order_v3", None)
        org = org_no or self.order_org_no.get(oid)
        if fn is None or not org:
            return BrokerOrderResult(False, oid, symbol, "CANCEL", 0, 0, 0.0,
                                     "CANCEL_V3_UNAVAILABLE" if fn is None else "CANCEL_V3_NO_ORG_NO",
                                     raw={"status": "UNAVAILABLE" if fn is None else "NO_ORG_NO"})
        # 기록을 HTTP 전에 남긴다 -- 예외로 끝나도 재전송되지 않게.
        self.cancel_v3_sent[oid] = {"status": "IN_FLIGHT"}
        try:
            r = dict(fn(oid, str(org), symbol, allow_real=(getattr(self, "mode", "") == "real")) or {})
        except Exception as exc:  # noqa: BLE001
            r = {"status": "UNKNOWN", "success": False, "message": f"CANCEL_V3_ERROR:{exc}"}
        rec = {k: r.get(k) for k in ("status", "cancel_order_id", "rt_cd", "msg_cd", "msg1", "tr_id")}
        self.cancel_v3_sent[oid] = rec
        return BrokerOrderResult(bool(r.get("success")), oid, symbol, "CANCEL", 0, 0, 0.0,
                                 str(r.get("message") or r.get("status") or ""), raw=rec)

    def cancel_order(self, order_id: str, symbol: str = "") -> BrokerOrderResult:
        """Best-effort cancel of unfilled remainder. Returns success=False if
        underlying broker has no cancel API (still records the attempt)."""
        cancel_fn = getattr(self._broker, "cancel_order", None) or getattr(
            getattr(self._broker, "kis", None), "cancel_order", None
        )
        if cancel_fn is None:
            return BrokerOrderResult(
                False, str(order_id or ""), symbol, "CANCEL", 0, 0, 0.0,
                "CANCEL_NOT_SUPPORTED", raw={"msg1": "cancel_order unavailable"},
            )
        try:
            raw_result = cancel_fn(order_id) if cancel_fn.__code__.co_argcount <= 2 else cancel_fn(order_id, symbol)
        except TypeError:
            try:
                raw_result = cancel_fn(order_id, symbol)
            except Exception as exc:
                return BrokerOrderResult(
                    False, str(order_id or ""), symbol, "CANCEL", 0, 0, 0.0,
                    f"CANCEL_ERROR:{exc}", raw={"msg1": str(exc)},
                )
        except Exception as exc:
            return BrokerOrderResult(
                False, str(order_id or ""), symbol, "CANCEL", 0, 0, 0.0,
                f"CANCEL_ERROR:{exc}", raw={"msg1": str(exc)},
            )
        if isinstance(raw_result, BrokerOrderResult):
            return raw_result
        success = bool(getattr(raw_result, "success", raw_result is True))
        return BrokerOrderResult(
            success, str(order_id or ""), symbol, "CANCEL", 0, 0, 0.0,
            "OK" if success else "CANCEL_FAILED",
            raw=dict(getattr(raw_result, "raw", {}) or {}) if raw_result is not True else {},
        )

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        result = self._call_broker_without_direct_ledger(
            "sell", symbol, symbol, int(qty), 0, order_type="market",
        )
        return _to_order_result(result, symbol, "SELL", int(qty))

    def wait_for_execution(self, order_id: str, timeout: float = 10.0) -> BrokerOrderResult:
        """buy_market/sell_market on this broker layer already confirm synchronously
        (a single HTTP round-trip) — there is no separate async order-status
        endpoint wired here. This method exists for interface completeness (a
        future real-async KIS path) and simply reports that no pending order
        tracking is needed at this layer.
        """
        del timeout
        raise NotImplementedError(
            "wait_for_execution: this broker layer confirms synchronously inside "
            "buy_market/sell_market — call reconcile_position(symbol) to verify "
            f"the resulting holdings instead (order_id={order_id!r})."
        )

    def reconcile_position(self, symbol: str) -> int:
        """Actual held quantity for ``symbol``, re-queried fresh from the broker
        (no cache). Retry/backoff for "did the sell actually clear to 0 yet"
        belongs to order_executor.py, not this thin adapter.
        """
        pos = self.get_position(symbol)
        return int(pos.quantity) if pos else 0

    def is_market_open(self) -> bool:
        # The shared broker layer has no explicit market-clock API; MACD2's own
        # session-time gating (docs §10/§11, 09:00-15:30 KST) is evaluated by
        # worker.py against wall-clock KST, not by asking the broker.
        return True


class MockBrokerAdapter(_BrokerAdapterBase):
    mode = "mock"

    def __init__(self, broker: Optional[BrokerBase] = None) -> None:
        if broker is None:
            from app.trading.broker_factory import create_broker

            broker = create_broker(mode="mock")
        self._broker = broker


class RealBrokerAdapter(_BrokerAdapterBase):
    """REAL-mode adapter. All REAL safety gates run inside broker_factory.create_broker
    -> KisRealBroker.__init__ at construction time (docs §14) — this adapter
    never duplicates or bypasses them.
    """

    mode = "real"

    def __init__(
        self,
        *,
        confirm_text: str,
        runtime_real_mode: bool = False,
        runtime_enable_real_buy: bool = False,
        runtime_enable_real_sell: bool = False,
        broker: Optional[BrokerBase] = None,
    ) -> None:
        if broker is None:
            from app.trading.broker_factory import create_broker

            broker = create_broker(
                mode="real",
                confirm_text=confirm_text,
                runtime_real_mode=runtime_real_mode,
                runtime_enable_real_buy=runtime_enable_real_buy,
                runtime_enable_real_sell=runtime_enable_real_sell,
            )
        self._broker = broker


def create_macd2_broker(mode: str, **kwargs: Any) -> _BrokerAdapterBase:
    """docs §9 factory: mode in {"mock", "real"}. REAL kwargs (confirm_text, ...)
    are required only for mode="real"; MOCK never receives or checks them.
    """
    if mode == "mock":
        return MockBrokerAdapter()
    if mode == "real":
        return RealBrokerAdapter(**kwargs)
    raise ValueError(f"create_macd2_broker: unknown mode {mode!r}")
