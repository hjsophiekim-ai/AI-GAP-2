"""주문실행 v2 (2026-10-16) — 최신 호가 / 주문번호 체결확인 / MOCK 전용 취소 / 안전 repricing / 계측 로그.

전부 기본 OFF. 여기 테스트는 config 를 monkeypatch 로 켜서 검증한다. OFF 일 때 기존과 같다는 것은
test_v2_off_* 와 기존 tests/macd2 전체(test_order_executor 등)가 지킨다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, ledger, order_execution_v2 as v2, order_executor, pending_order, state_store, worker
from app.trading.macd2.broker_adapter import BrokerOrderResult, MockBrokerAdapter, RealBrokerAdapter
from app.trading.macd2.models import Direction, SignalState
from tests.macd2.test_pending_buy_order import LiveOrderBroker, _enter, _flat_world

LONG = config.LONG_SYMBOL


class V2Broker(LiveOrderBroker):
    """+ 나이가 있는 호가 시퀀스, MOCK 전용 취소(cancel_order_v2), 주문별 즉시체결 수량 큐."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.mode = "mock"
        self.asks: list[tuple[float, float]] = []          # (ask1, age_sec) 순서대로 소비, 비면 마지막 값 반복
        self.fill_queue: list[int] = []                     # 주문마다 즉시 체결 수량
        self.v2_allowed = True
        self.cancel_mode = "confirm"                        # confirm / unconfirmed / fill_during
        self.v2_cancel_calls: list[tuple] = []

    def get_fresh_ask1(self, symbol):
        if self.asks:
            a, age = self.asks.pop(0) if len(self.asks) > 1 else self.asks[0]
        else:
            a, age = self._quotes.get(symbol, 0.0), 0.1
        return {"ok": a > 0, "symbol": symbol, "ask1": a, "age_sec": age}

    def buy_limit(self, symbol, qty, price, client_order_id):
        if self.fill_queue:
            self.next_buy_fill_qty = self.fill_queue.pop(0)
        return super().buy_limit(symbol, qty, price, client_order_id)

    def cancel_v2_allowed(self):
        return self.v2_allowed

    def cancel_order_v2(self, order_id, symbol="", org_no=None):
        self.v2_cancel_calls.append((order_id, symbol, org_no))
        o = self.live.get(order_id)
        if o and self.cancel_mode == "confirm":
            o["cancelled"] = True
        elif o and self.cancel_mode == "fill_during":
            self.fill_later(order_id, o["qty"] - o["filled"])
        return BrokerOrderResult(True, order_id, symbol, "CANCEL", 0, 0, 0.0, "OK", raw={})


@pytest.fixture
def v2on(monkeypatch):
    for k, val in {"EXEC_V2_ENABLED": True, "EXEC_QUOTE_MAX_AGE_SEC": 2.0, "EXEC_QUOTE_FETCH_ATTEMPTS": 3,
                   "EXEC_QUOTE_RETRY_DELAY_SEC": 0.0, "EXEC_FILL_CHECK_INTERVAL_SEC": 0.0,
                   "EXEC_FILL_WAIT_MAX_SEC": 0.0, "EXEC_CANCEL_CONFIRM_CHECKS": 2,
                   "EXEC_CANCEL_CONFIRM_INTERVAL_SEC": 0.0, "EXEC_REPRICE_ENABLED": False, "EXEC_REPRICE_MAX": 1,
                   "EXEC_REPRICE_MAX_SLIPPAGE_PCT": 0.0}.items():
        monkeypatch.setattr(config, k, val)
    monkeypatch.setattr(order_executor, "POST_CANCEL_RECHECK_DELAY_SEC", 0.0)


def _broker(**kw):
    b = V2Broker(cash=10_000_000.0, quotes={LONG: 5_450.0})
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _run(broker, sid="SIG-V2"):
    return order_executor.execute_signal(broker=broker, direction=Direction.UP_RED, signal_id=sid,
                                         quotes={LONG: 5_450.0}, position=None, budget=10_000_000.0,
                                         reconcile_retries=1, reconcile_delay_sec=0.0)


def _buys():
    return [r for r in ledger.load_execution_ledger() if r["side"] == "BUY"]


# ── OFF = 기존 동작 ───────────────────────────────────────────────────────────
def test_v2_off_uses_legacy_paths_only():
    b = _broker(fill_queue=[0])
    out = _run(b)
    assert out.final_state == SignalState.BLOCKED and out.block_reason == order_executor.BUY_CANCEL_NOT_CONFIRMED
    assert b.v2_cancel_calls == [] and b.cancel_calls, "OFF 면 v2 취소를 부르지 않고 기존 cancel_order 만"
    assert b.status_calls == [], "OFF 면 주문번호 상태조회로 체결확인하지 않는다(기존 잔고 기반)"
    assert out.reprice_count == 0 and out.exec_log.get("cancel_requested_at")


# ── A. 최신 호가 ─────────────────────────────────────────────────────────────
def test_stale_quote_is_refetched_and_fresh_one_used(v2on):
    b = _broker(asks=[(5_440.0, 3.5), (5_470.0, 0.4)], fill_queue=[None])
    out = _run(b)
    assert out.final_state == SignalState.EXECUTED
    assert b.orders[-1].executed_price == 5_480.0, "늙은 5,440 이 아니라 새 5,470 + 1틱"
    assert out.exec_log["quote_attempts"] == 2 and out.exec_log["quote_ages_ms"] == [3500, 400]
    assert out.exec_log["quote_age_ms"] == 400


def test_all_quotes_stale_means_no_order_with_reason(v2on):
    b = _broker(asks=[(5_440.0, 2.5)])
    out = _run(b)
    assert out.final_state == SignalState.BLOCKED and out.block_reason == v2.BLOCK_QUOTE_TOO_OLD
    assert not b.orders and "주문하지 않음" in out.sizing_msg1
    assert out.exec_log["quote_ages_ms"] == [2500, 2500, 2500]


def test_quote_age_from_exchange_time_and_rtt():
    t0 = datetime(2026, 10, 16, 9, 51, 20, 0, tzinfo=v2.KST)
    t1 = t0 + timedelta(milliseconds=150)
    assert v2.quote_age_ms({"exch_time_hms": "095117"}, t0, t1) == 3150
    assert v2.quote_age_ms({}, t0, t1) == 150


# ── B. 주문번호 기반 체결확인 ─────────────────────────────────────────────────
class _Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 16, 9, 51, 23, tzinfo=v2.KST)

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += timedelta(seconds=sec)


def test_poll_interval_and_wait_are_separate_settings(monkeypatch):
    monkeypatch.setattr(config, "EXEC_FILL_CHECK_INTERVAL_SEC", 3.0)
    monkeypatch.setattr(config, "EXEC_FILL_WAIT_MAX_SEC", 9.0)
    b = _broker(fill_queue=[0])
    r = b.buy_limit(LONG, 100, 5_460.0, "x")
    c = _Clock()
    res = v2.poll_fill(b, r.order_id, LONG, 100, log := {}, sleep=c.sleep, clock=c.now)
    assert res["state"] == "OPEN" and res["checks"] == 4 and log["fill_wait_sec"] == 9.0   # 0,3,6,9초


def test_poll_detects_fill_at_third_check_and_records_first_fill(monkeypatch):
    monkeypatch.setattr(config, "EXEC_FILL_CHECK_INTERVAL_SEC", 3.0)
    monkeypatch.setattr(config, "EXEC_FILL_WAIT_MAX_SEC", 60.0)
    b = _broker(fill_queue=[0])
    r = b.buy_limit(LONG, 100, 5_460.0, "x")
    c = _Clock()
    calls = {"n": 0}
    orig = b.get_order_status

    def status(oid, symbol="", trading_date=""):
        calls["n"] += 1
        if calls["n"] == 3:
            b.fill_later(oid, 100)
        return orig(oid, symbol, trading_date)

    b.get_order_status = status
    res = v2.poll_fill(b, r.order_id, LONG, 100, log := {}, sleep=c.sleep, clock=c.now)
    assert res["state"] == "FILLED" and res["checks"] == 3 and log["first_fill_at"].endswith("09:51:29.000+09:00")


@pytest.mark.parametrize("st, want", [
    ({"found": True, "filled_qty": 10, "remaining_qty": 0}, "FILLED"),
    ({"found": True, "filled_qty": 4, "remaining_qty": 6}, "PARTIAL"),
    ({"found": True, "filled_qty": 0, "remaining_qty": 10}, "OPEN"),
    ({"found": True, "filled_qty": 3, "remaining_qty": 0, "cancelled": True}, "CANCELLED"),
])
def test_classify_states(st, want):
    assert v2.classify(st, 10) == want


# ── D. 취소 ──────────────────────────────────────────────────────────────────
def test_partial_fill_then_cancel_confirmed_books_partial(v2on):
    b = _broker(fill_queue=[700])
    out = _run(b)
    assert out.final_state == SignalState.EXECUTED and out.quantity == 700
    assert out.order_failure_stage == "PARTIAL_FILL_CANCEL_CONFIRMED" and out.open_order is None
    assert b.v2_cancel_calls and out.exec_log["cancel_confirmed_at"]
    assert [int(r["executed_qty"]) for r in _buys()] == [700]


def test_zero_fill_cancel_confirmed_without_reprice_fails_cleanly(v2on):
    b = _broker(fill_queue=[0])
    out = _run(b)
    assert out.final_state == SignalState.FAILED and out.block_reason == order_executor.FAIL_BUY_NOT_CONFIRMED
    assert len(b.orders) == 1 and not _buys() and out.exec_log["cancel_confirmed_at"]
    assert ledger.try_claim_signal_dispatch("SIG-V2", "BUY"), "취소 확정이면 claim 을 푼다(기존과 같은 규칙)"


def test_cancel_unconfirmed_never_reorders_and_goes_to_pending(v2on, monkeypatch):
    monkeypatch.setattr(config, "EXEC_REPRICE_ENABLED", True)
    monkeypatch.setattr(config, "EXEC_REPRICE_MAX_SLIPPAGE_PCT", 5.0)
    b = _broker(fill_queue=[0], cancel_mode="unconfirmed")
    out = _run(b)
    assert out.final_state == SignalState.BLOCKED and out.block_reason == order_executor.BUY_CANCEL_NOT_CONFIRMED
    assert len(b.orders) == 1 and out.reprice_count == 0, "취소 미확인이면 절대 재주문 금지"
    again = _run(b)                                                     # 같은 신호 다시 와도
    assert again.block_reason == order_executor.BLOCK_DUPLICATE_SIGNAL and len(b.orders) == 1


def test_partial_fill_with_cancel_unconfirmed_tracks_remainder(v2on, monkeypatch):
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)
    svc, now, state, _ = _flat_world()
    b = _broker(fill_queue=[500], cancel_mode="unconfirmed")
    out, res = _enter(svc, now, state, b)
    assert out.final_state == SignalState.EXECUTED and out.quantity == 500
    p = state.pending_buy_order
    assert p and p["recorded_qty"] == 500 and p["order_id"] == out.buy_result.order_id
    state.position = worker.PositionSnapshot(symbol=LONG, quantity=500, avg_price=5_460.0, entry_at=now)  # 호출부 반영
    state_store.save_state(state)
    restored = state_store.load_state()                                 # 재시작 후 pending 복원
    assert restored.pending_buy_order["recorded_qty"] == 500
    b.fill_later(p["order_id"], p["requested_qty"] - 500)
    r = worker.track_pending_order(b, restored, now + timedelta(minutes=2), force=True)
    assert r["action"] == pending_order.LATE_FILL_CONFIRMED and restored.position.quantity == p["requested_qty"]
    assert sorted(x["order_id"] for x in _buys()) == sorted([p["order_id"], f"{p['order_id']}#2"])


# ── E. repricing ─────────────────────────────────────────────────────────────
def test_reprice_after_confirmed_cancel_within_slippage(v2on, monkeypatch):
    monkeypatch.setattr(config, "EXEC_REPRICE_ENABLED", True)
    monkeypatch.setattr(config, "EXEC_REPRICE_MAX_SLIPPAGE_PCT", 1.0)
    b = _broker(asks=[(5_450.0, 0.2), (5_480.0, 0.3)], fill_queue=[0, None])
    out = _run(b)
    assert out.final_state == SignalState.EXECUTED and out.reprice_count == 1
    assert [o.executed_price for o in b.orders] == [5_460.0, 5_490.0]        # 5,490 <= 5,450 x 1.01 = 5,504.5
    assert [r["order_id"] for r in _buys()] == [b.orders[-1].order_id]
    assert out.exec_log["r1_quote_age_ms"] == 300


def test_reprice_aborts_when_price_ran_away(v2on, monkeypatch):
    monkeypatch.setattr(config, "EXEC_REPRICE_ENABLED", True)
    monkeypatch.setattr(config, "EXEC_REPRICE_MAX_SLIPPAGE_PCT", 0.5)
    b = _broker(asks=[(5_450.0, 0.2), (5_500.0, 0.2)], fill_queue=[0])
    out = _run(b)
    assert out.final_state == SignalState.FAILED and out.block_reason == v2.REPRICE_ABORTED_SLIPPAGE
    assert len(b.orders) == 1 and "SLIPPAGE" in out.exec_log["reprice_abort_reason"]


def test_reprice_count_capped(v2on, monkeypatch):
    monkeypatch.setattr(config, "EXEC_REPRICE_ENABLED", True)
    monkeypatch.setattr(config, "EXEC_REPRICE_MAX", 2)
    monkeypatch.setattr(config, "EXEC_REPRICE_MAX_SLIPPAGE_PCT", 5.0)
    b = _broker(fill_queue=[0, 0, 0, 0])
    out = _run(b)
    assert len(b.orders) == 3 and out.reprice_count == 2 and out.final_state == SignalState.FAILED


def test_no_reprice_when_slippage_cap_unset(v2on, monkeypatch):
    monkeypatch.setattr(config, "EXEC_REPRICE_ENABLED", True)          # 상한 0 = 미설정
    b = _broker(fill_queue=[0, None])
    out = _run(b)
    assert len(b.orders) == 1 and out.block_reason == order_executor.FAIL_BUY_NOT_CONFIRMED


# ── 취소 API 차단 (실계좌 금지) ──────────────────────────────────────────────
@pytest.mark.parametrize("cls, mode, enabled, verify, want", [
    (MockBrokerAdapter, "mock", True, True, True),
    (MockBrokerAdapter, "mock", True, False, False),
    (MockBrokerAdapter, "mock", False, True, False),
    (RealBrokerAdapter, "real", True, True, False),
])
def test_cancel_v2_gate(monkeypatch, cls, mode, enabled, verify, want):
    monkeypatch.setattr(config, "EXEC_CANCEL_ENABLED", enabled)
    monkeypatch.setattr(config, "EXEC_CANCEL_MOCK_VERIFY", verify)
    a = cls.__new__(cls)
    a.mode = mode
    called = []
    a._broker = type("B", (), {"kis": type("K", (), {"cancel_order_rvsecncl": lambda *x: called.append(x) or
                                                       {"success": True}})()})()
    a.order_org_no["0000013052"] = "06010"
    assert a.cancel_v2_allowed() is want
    r = a.cancel_order_v2("0000013052", LONG)
    assert r.success is want and (called != []) is want
    if not want:
        assert r.message == "CANCEL_V2_DISABLED"


def test_kis_client_refuses_real_cancel_without_http():
    from app.trading.kis_client import KISClient
    k = KISClient.__new__(KISClient)
    k.mode = "real"
    k._post = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("실전 취소 HTTP 금지"))
    assert k.cancel_order_rvsecncl("0000013052", "06010", LONG)["message"] == "REAL_CANCEL_DISABLED"


def test_legacy_adapter_cancel_does_not_pick_up_new_kis_method():
    """기존 adapter.cancel_order 는 kis.cancel_order 만 찾는다 -- 새 메서드 이름을 다르게 둔 이유."""
    a = MockBrokerAdapter.__new__(MockBrokerAdapter)
    a.mode = "mock"
    a._broker = type("B", (), {"kis": type("K", (), {"cancel_order_rvsecncl": lambda *x: {"success": True}})()})()
    assert a.cancel_order("1", LONG).message == "CANCEL_NOT_SUPPORTED"


# ── C. 계측 로그 ─────────────────────────────────────────────────────────────
def test_exec_log_has_all_requested_fields(v2on, monkeypatch):
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)
    svc, now, state, _ = _flat_world()
    b = _broker(fill_queue=[300])
    out, res = _enter(svc, now, state, b)
    rec = res.signal_dispatch_trace["exec_log"]
    assert set(worker.EXEC_LOG_FIELDS) <= set(rec)
    for k in ("signal_approved_at", "balance_start", "balance_end", "buying_power_start", "buying_power_end",
              "quote_requested_at", "quote_received_at", "order_sent_at", "broker_accepted_at", "first_fill_at",
              "cancel_requested_at", "cancel_confirmed_at"):
        assert rec[k], k
    assert rec["quote_age_ms"] is not None and rec["reprice_count"] == 0
