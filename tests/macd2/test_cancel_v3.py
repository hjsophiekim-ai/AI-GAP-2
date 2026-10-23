"""취소 v3 (2026-10-23) — kis_client.cancel_order_v3 / broker_adapter.cancel_order_v3 + 부분체결 재호가 end-to-end.

실제 KISClient.cancel_order_v3 와 실제 RealBrokerAdapter/MockBrokerAdapter 메서드를 쓰고, HTTP(_post)와
주문상태만 가짜 거래소(Exchange)로 바꾼다. 가짜 거래소는 가짜 시계 위에서 주문별 체결 일정과 취소 응답
방식(접수/거절/timeout 등)을 재생한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from app.trading import kis_client as kc
from app.trading.macd2 import config, partial_fill_reprice as pfr
from app.trading.macd2.broker_adapter import MockBrokerAdapter, RealBrokerAdapter

KST = timezone(timedelta(hours=9))
SYM = "0193T0"


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    monkeypatch.setattr(kc, "_RATE_LIMIT_RETRY_DELAY_SECONDS", {"mock": 0.0, "real": 0.0})


def _flags(monkeypatch, kill=False):
    monkeypatch.setattr(config, "PARTIAL_FILL_REPRICE_KILL", kill)


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._d is None:
            raise ValueError("no body")
        return self._d


def _kis(mode, post):
    k = kc.KISClient.__new__(kc.KISClient)
    k.mode, k.base_url, k.account_no, k.product_code = mode, "https://x", "12345678", "01"
    k.get_hashkey = lambda body: "HASH"
    k._auth_headers = lambda tr_id: {"tr_id": tr_id}
    k._post = post
    return k


OK = {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "주문 전송 완료", "output": {"ODNO": "0000099001",
                                                                          "KRX_FWDG_ORD_ORGNO": "06010"}}


# ═══ A. KISClient.cancel_order_v3 ════════════════════════════════════════════
def test_real_without_allow_real_sends_no_http():
    k = _kis("real", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("HTTP 금지")))
    r = k.cancel_order_v3("0000027653", "06010", SYM)
    assert r["status"] == kc.CANCEL_V3_BLOCKED_REAL and r["success"] is False


@pytest.mark.parametrize("mode,tr", [("real", "TTTC0013U"), ("mock", "VTTC0013U")])
def test_tr_and_body_by_mode(mode, tr):
    sent = []
    k = _kis(mode, lambda url, **kw: sent.append((url, kw)) or Resp(OK))
    r = k.cancel_order_v3("0000027653", "06010", SYM, allow_real=True)
    assert r["status"] == kc.CANCEL_V3_ACCEPTED and r["success"] and r["cancel_order_id"] == "0000099001"
    assert r["tr_id"] == tr and len(sent) == 1
    url, kw = sent[0]
    assert url.endswith("/uapi/domestic-stock/v1/trading/order-rvsecncl")
    assert kw["headers"] == {"tr_id": tr, "hashkey": "HASH"}
    assert kw["json"] == {"CANO": "12345678", "ACNT_PRDT_CD": "01", "KRX_FWDG_ORD_ORGNO": "06010",
                          "ORGN_ODNO": "0000027653", "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",
                          "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX"}


def test_rejected():
    k = _kis("mock", lambda *a, **kw: Resp({"rt_cd": "1", "msg_cd": "APBK0920", "msg1": "정정취소 가능수량 없음"}))
    r = k.cancel_order_v3("1", "06010", SYM)
    assert r["status"] == kc.CANCEL_V3_REJECTED and r["success"] is False and r["msg_cd"] == "APBK0920"


def test_timeout_is_unknown_and_not_resent():
    calls = []

    def post(*a, **kw):
        calls.append(1)
        raise requests.Timeout("read timed out")

    r = _kis("real", post).cancel_order_v3("1", "06010", SYM, allow_real=True)
    assert r["status"] == kc.CANCEL_V3_UNKNOWN and r["success"] is False and len(calls) == 1


def test_rate_limit_gateway_reject_is_retried_then_accepted():
    seq = [Resp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과"}, 500), Resp(OK)]
    calls = []
    r = _kis("mock", lambda *a, **kw: calls.append(1) or seq.pop(0)).cancel_order_v3("1", "06010", SYM)
    assert r["status"] == kc.CANCEL_V3_ACCEPTED and len(calls) == 2


@pytest.mark.parametrize("resp", [Resp({"rt_cd": "0", "msg1": "ok", "output": {}}), Resp(None, 502)])
def test_ambiguous_responses_are_unknown(resp):
    r = _kis("mock", lambda *a, **kw: resp).cancel_order_v3("1", "06010", SYM)
    assert r["status"] == kc.CANCEL_V3_UNKNOWN


@pytest.mark.parametrize("oid,org,ex", [("", "06010", "KRX"), ("A123", "06010", "KRX"), ("1", "", "KRX"),
                                        ("1", "06010", "XXX")])
def test_invalid_input_sends_no_http(oid, org, ex):
    k = _kis("mock", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("HTTP 금지")))
    assert k.cancel_order_v3(oid, org, SYM, exchange=ex)["status"] == kc.CANCEL_V3_INVALID_INPUT


def test_legacy_cancel_unchanged_still_refuses_real():
    k = _kis("real", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("HTTP 금지")))
    assert k.cancel_order_rvsecncl("1", "06010", SYM)["message"] == "REAL_CANCEL_DISABLED"


def test_get_order_status_ignores_other_order_numbers():
    """주문번호 불일치: 조회 응답에 다른 주문만 있으면 found=False (다른 주문의 체결을 내 것으로 세지 않는다)."""
    k = kc.KISClient.__new__(kc.KISClient)
    k.mode, k.base_url, k.account_no, k.product_code = "real", "https://x", "1", "01"
    rows = [{"odno": "0000027654", "pdno": SYM, "ord_qty": "1900", "tot_ccld_qty": "1900", "rmn_qty": "0"}]
    k._request_with_token_retry = lambda *a, **kw: Resp({"rt_cd": "0", "output1": rows})
    st = k.get_order_status("0000027653", symbol=SYM)
    assert st["ok"] is True and st["found"] is False and st["filled_qty"] == 0


# ═══ B. adapter.cancel_order_v3 ══════════════════════════════════════════════
def _adapter(cls, kis):
    a = cls.__new__(cls)
    a._broker = SimpleNamespace(kis=kis)
    return a


@pytest.mark.parametrize("cls,kill,want", [
    (MockBrokerAdapter, False, True), (MockBrokerAdapter, True, False),
    (RealBrokerAdapter, False, True), (RealBrokerAdapter, True, False)])
def test_cancel_v3_allowed_matrix(monkeypatch, cls, kill, want):
    _flags(monkeypatch, kill)
    calls = []
    a = _adapter(cls, _kis(cls.mode, lambda *x, **kw: calls.append(1) or Resp(OK)))
    a.order_org_no["0000027653"] = "06010"
    assert a.cancel_v3_allowed() is want
    r = a.cancel_order_v3("0000027653", SYM)
    assert r.success is want and (len(calls) == 1) is want


def test_adapter_duplicate_cancel_sends_one_http(monkeypatch):
    _flags(monkeypatch)
    calls = []
    a = _adapter(RealBrokerAdapter, _kis("real", lambda *x, **kw: calls.append(1) or Resp(OK)))
    a.order_org_no["0000027653"] = "06010"
    r1 = a.cancel_order_v3("0000027653", SYM)
    r2 = a.cancel_order_v3("0000027653", SYM)
    assert r1.success and not r2.success and r2.message == "DUPLICATE_SUPPRESSED" and len(calls) == 1


def test_adapter_duplicate_after_timeout_also_suppressed(monkeypatch):
    _flags(monkeypatch)
    calls = []

    def post(*a, **kw):
        calls.append(1)
        raise requests.ConnectionError("reset")

    a = _adapter(RealBrokerAdapter, _kis("real", post))
    a.order_org_no["1"] = "06010"
    assert a.cancel_order_v3("1", SYM).raw["status"] == kc.CANCEL_V3_UNKNOWN
    assert a.cancel_order_v3("1", SYM).message == "DUPLICATE_SUPPRESSED" and len(calls) == 1


def test_adapter_missing_org_no_sends_nothing(monkeypatch):
    _flags(monkeypatch)
    a = _adapter(RealBrokerAdapter, _kis("real", lambda *x, **kw: (_ for _ in ()).throw(AssertionError())))
    r = a.cancel_order_v3("0000027653", SYM)
    assert not r.success and r.message == "CANCEL_V3_NO_ORG_NO"
    assert "0000027653" not in a.cancel_v3_sent        # 보낸 적 없으니 기록도 없다


def test_legacy_adapter_cancel_paths_untouched(monkeypatch):
    """새 플래그를 켜도 기존 cancel_order / cancel_order_v2 동작은 같다."""
    _flags(monkeypatch)
    monkeypatch.setattr(config, "EXEC_CANCEL_ENABLED", False)
    a = _adapter(RealBrokerAdapter, _kis("real", lambda *x, **kw: (_ for _ in ()).throw(AssertionError())))
    assert a.cancel_order("1", SYM).message == "CANCEL_NOT_SUPPORTED"
    assert a.cancel_order_v2("1", SYM).message == "CANCEL_V2_DISABLED"


# ═══ C. end-to-end: 재호가 루프 + 실제 adapter + 실제 cancel_order_v3 + 가짜 거래소 ═════════
class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return datetime(2026, 10, 23, 12, 33, 12, tzinfo=KST) + timedelta(seconds=self.t)

    def sleep(self, s):
        self.t += max(0.0, float(s))


class Exchange:
    """plans[i]: i번째 주문. fills=[(dt, 누적)], cancel=accept|reject|reject_notfound|timeout_accepted|
    timeout_lost|no_odno, delay=취소 접수 후 확정까지 초."""

    def __init__(self, clock, plans, ask=4955.0):
        self.c, self.plans, self.ask = clock, list(plans), ask
        self.orders: dict[str, dict] = {}
        self.cancel_posts: dict[str, int] = {}
        self.buys: list[tuple] = []
        self.live_at_buy: list[int] = []

    def _view(self, oid):
        o = self.orders[oid]
        horizon, cancelled = self.c.t, False
        if o["cancel_at"] is not None and self.c.t >= o["cancel_at"]:
            horizon, cancelled = o["cancel_at"], True
        filled = 0
        for dt, cum in o["fills"]:
            if o["t"] + dt <= horizon:
                filled = max(filled, min(cum, o["qty"]))
        rem = 0 if (cancelled or filled >= o["qty"]) else o["qty"] - filled
        return filled, rem, cancelled and filled < o["qty"]

    # 브로커 계층(_broker)
    def buy(self, name, symbol, qty, price, order_type="limit"):
        self.live_at_buy.append(sum(1 for k in self.orders if self._view(k)[1] > 0))
        n = len(self.orders)
        oid = f"{27653 + n:010d}"
        p = self.plans[n] if n < len(self.plans) else {}
        self.orders[oid] = {"qty": int(qty), "price": float(price), "t": self.c.t, "fills": p.get("fills", []),
                            "cancel": p.get("cancel", "accept"), "delay": p.get("delay", 0.5), "cancel_at": None}
        self.buys.append((symbol, int(qty), float(price)))
        return SimpleNamespace(success=True, order_id=oid, message="OK", price=float(price),
                               raw={"ODNO": oid, "KRX_FWDG_ORD_ORGNO": "06010"})

    def get_order_status(self, order_id, symbol="", trading_date=""):
        if order_id not in self.orders:
            return {"ok": True, "found": False}
        filled, rem, cancelled = self._view(order_id)
        o = self.orders[order_id]
        return {"ok": True, "found": True, "order_qty": o["qty"], "filled_qty": filled, "remaining_qty": rem,
                "amount": filled * o["price"], "cancelled": cancelled}

    # HTTP 계층(KISClient._post)
    def post(self, url, json=None, headers=None, timeout=None):
        oid = json["ORGN_ODNO"]
        self.cancel_posts[oid] = self.cancel_posts.get(oid, 0) + 1
        o = self.orders.get(oid)
        mode = o["cancel"] if o else "reject_notfound"
        if mode in ("accept", "timeout_accepted", "no_odno") and o and self._view(oid)[1] > 0:
            o["cancel_at"] = self.c.t + o["delay"]
        if mode == "timeout_accepted" or mode == "timeout_lost":
            raise requests.Timeout("read timed out")
        if mode == "reject_notfound":
            return Resp({"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "원주문번호가 존재하지 않습니다"})
        if mode == "reject" or (o and self._view(oid)[1] == 0):
            return Resp({"rt_cd": "1", "msg_cd": "APBK0920", "msg1": "정정취소 가능수량이 없습니다"})
        if mode == "no_odno":
            return Resp({"rt_cd": "0", "msg1": "ok", "output": {}})
        return Resp({**OK, "output": {"ODNO": f"9{oid[1:]}", "KRX_FWDG_ORD_ORGNO": "06010"}})


def _world(monkeypatch, plans, ask=4955.0):
    _flags(monkeypatch)
    c = Clock()
    ex = Exchange(c, plans, ask)
    k = _kis("real", ex.post)
    k.get_orderbook_quote = lambda symbol: {"ok": True, "symbol": symbol, "ask1": ex.ask, "age_sec": 0.2}
    a = RealBrokerAdapter.__new__(RealBrokerAdapter)
    a._broker = SimpleNamespace(kis=k, buy=ex.buy, get_order_status=ex.get_order_status)
    return c, ex, a


def _e2e(monkeypatch, plans, *, target=1900, ask=4955.0, **params):
    c, ex, a = _world(monkeypatch, plans, ask)
    first = a.buy_limit(SYM, target, 4950.0, "SIG")                  # 최초 주문(기존 경로가 낸 것)
    p = pfr.RepriceParams(status_retries=2, status_retry_sec=0.2, cancel_confirm_checks=5,
                          cancel_confirm_sec=0.5, **params)
    r = pfr.PartialFillRepricer(a, symbol=SYM, target_qty=target, order_price=4950.0, first_result=first,
                                client_order_id="SIG", params=p, sleep=c.sleep, clock=c.now).run()
    total = sum(ex._view(k)[0] for k in ex.orders)
    # 공통 불변식
    assert all(n == 0 for n in ex.live_at_buy[1:]), "살아 있는 주문 위에 새 매수"
    assert max(ex.cancel_posts.values(), default=0) <= 1, "같은 주문에 취소 HTTP 2회"
    assert total <= target, "목표수량 초과"
    return r, ex, a, total


def test_e2e_partial_then_cancel_success_then_reprice(monkeypatch):
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 117)]}, {"fills": [(1.0, 1783)]}])
    assert r.final == pfr.FILLED and total == 1900
    assert ex.buys[1] == (SYM, 1783, 4955.0)
    assert a.cancel_v3_sent["0000027653"]["status"] == kc.CANCEL_V3_ACCEPTED
    assert a.cancel_v3_sent["0000027653"]["tr_id"] == "TTTC0013U"


def test_e2e_fills_during_cancel(monkeypatch):
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 117), (3.2, 600)], "delay": 0.5},
                                         {"fills": [(1.0, 1300)]}])
    assert ex.buys[1][1] == 1300 and r.final == pfr.FILLED and total == 1900


def test_e2e_cancel_rejected_while_open_is_cancel_failed(monkeypatch):
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 117)], "cancel": "reject"}])
    assert r.final == pfr.CANCEL_FAILED and len(ex.buys) == 1 and total == 117
    assert r.open_order["order_id"] == "0000027653"


def test_e2e_already_fully_filled_before_cancel(monkeypatch):
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 117), (3.0, 1900)]}])
    assert r.final == pfr.FILLED and len(ex.buys) == 1 and total == 1900


def test_e2e_cancel_rejected_order_not_found_is_cancel_failed(monkeypatch):
    """주문번호 불일치(취소 응답 '원주문번호 없음')인데 상태조회상 잔량이 있으면 추가매수 금지."""
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 117)], "cancel": "reject_notfound"}])
    assert r.final == pfr.CANCEL_FAILED and len(ex.buys) == 1


def test_e2e_status_cannot_find_order_does_nothing(monkeypatch):
    """주문번호 불일치(상태조회에서 그 주문을 못 찾음) -> 체결수량 모름 -> 취소도 재주문도 안 한다."""
    c, ex, a = _world(monkeypatch, [{"fills": [(0.5, 117)]}])
    a.buy_limit(SYM, 1900, 4950.0, "SIG")
    wrong = SimpleNamespace(order_id="0000099999")
    r = pfr.PartialFillRepricer(a, symbol=SYM, target_qty=1900, order_price=4950.0, first_result=wrong,
                                client_order_id="SIG",
                                params=pfr.RepriceParams(status_retries=2, status_retry_sec=0.1),
                                sleep=c.sleep, clock=c.now).run()
    assert r.final == pfr.STATUS_UNAVAILABLE and ex.cancel_posts == {} and len(ex.buys) == 1


def test_e2e_timeout_but_cancel_actually_processed(monkeypatch):
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 117)], "cancel": "timeout_accepted"},
                                         {"fills": [(1.0, 1783)]}])
    assert a.cancel_v3_sent["0000027653"]["status"] == kc.CANCEL_V3_UNKNOWN
    assert r.final == pfr.FILLED and ex.cancel_posts == {"0000027653": 1}


def test_e2e_timeout_and_cancel_lost_is_cancel_failed(monkeypatch):
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 117)], "cancel": "timeout_lost"}])
    assert r.final == pfr.CANCEL_FAILED and len(ex.buys) == 1 and ex.cancel_posts == {"0000027653": 1}


def test_e2e_accepted_without_odno_still_confirmed_by_status(monkeypatch):
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 117)], "cancel": "no_odno"}, {"fills": [(1.0, 1783)]}])
    assert r.final == pfr.FILLED


def test_e2e_two_reprices_each_order_cancelled_once(monkeypatch):
    r, ex, a, total = _e2e(monkeypatch, [{"fills": [(0.5, 100)]}, {"fills": [(0.5, 100)]}, {"fills": [(0.5, 100)]}])
    assert r.final == pfr.ABANDONED_MAX_REPRICE and len(ex.buys) == 3 and total == 300
    assert sorted(ex.cancel_posts.values()) == [1, 1, 1]


def test_e2e_kill_switch_means_no_cancel_no_reprice(monkeypatch):
    c, ex, a = _world(monkeypatch, [{"fills": [(0.5, 117)]}])
    _flags(monkeypatch, kill=True)
    first = a.buy_limit(SYM, 1900, 4950.0, "SIG")
    r = pfr.PartialFillRepricer(a, symbol=SYM, target_qty=1900, order_price=4950.0, first_result=first,
                                client_order_id="SIG", sleep=c.sleep, clock=c.now).run()
    assert r.final == pfr.CANCEL_FAILED and ex.cancel_posts == {} and len(ex.buys) == 1
