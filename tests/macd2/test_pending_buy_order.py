"""미확정 매수주문(pending_buy_order) 추적 — 2026-10-16 09:51 실사고 회귀 테스트.

실사고: 지정가 매수 60초 미체결 → 취소 실패(국내 취소 API 미지원) → BLOCKED:BUY_CANCEL_NOT_CONFIRMED 로 끝난 것
처리 → 09:54 KIS 에서 실제 체결 → UI "0주" 를 보고 사용자가 수동 추가매수.

필수 시나리오:
  1) 취소 미확인 후 2분 뒤 체결 (부분 → 전량) → 원래 신호로 장부 반영, LATE_FILL_CONFIRMED
  2) 취소 미확인 후 실제 취소 → CANCEL_CONFIRMED, 포지션/체결원장 없음
  3) 재시작(state 저장·복원) 후 pending 추적 계속
  4) pending 중 수동매수 차단 (같은 종목·반대 종목 모두)
  5) pending 중 같은 방향 자동주문 차단
  6) 반대방향 신호 → 주문 안 냄 + 취소 시도, 취소 확정 후엔 정상 진입
  + pending 없으면 기존 동작(추적 호출 0) / 지연 계측 trace
"""
from __future__ import annotations

import types
from datetime import timedelta

import pytest

from app.trading.macd2 import config, ledger, order_executor, pending_order, state_store, worker
from app.trading.macd2 import service as service_module
from app.trading.macd2.models import Direction, QuoteSnapshot, SignalState
from app.models import Position
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_early_take_profit_worker import _market
from tests.macd2.test_tw2_3slot_worker_regression import _fresh_3slot_state
from tests.macd2.test_20261002_egw00215_reversal import _snap

LONG, INV = config.LONG_SYMBOL, config.INVERSE_SYMBOL


class LiveOrderBroker(FakeBroker):
    """KIS 처럼 '접수됐지만 미체결' 주문이 살아 있고, 취소는 실패하며, 주문번호로 상태를 조회할 수 있는 가짜."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.live: dict[str, dict] = {}
        self.cancel_works = False
        self.status_calls: list[str] = []
        self.status_error = False

    def buy_limit(self, symbol, qty, price, client_order_id):
        res = super().buy_limit(symbol, qty, price, client_order_id)
        if res.success:
            self.live[res.order_id] = {"symbol": symbol, "qty": qty, "price": float(price),
                                       "filled": int(res.executed_qty), "amount": float(price) * int(res.executed_qty),
                                       "cancelled": False}
        return res

    def cancel_order(self, order_id, symbol=""):
        self.cancel_calls.append((str(order_id), str(symbol)))
        o = self.live.get(str(order_id))
        if self.cancel_works and o and o["filled"] < o["qty"]:
            o["cancelled"] = True
            from app.trading.macd2.broker_adapter import BrokerOrderResult
            return BrokerOrderResult(True, str(order_id), symbol, "CANCEL", 0, 0, 0.0, "OK", raw={})
        from app.trading.macd2.broker_adapter import BrokerOrderResult
        return BrokerOrderResult(False, str(order_id), symbol, "CANCEL", 0, 0, 0.0, "CANCEL_NOT_SUPPORTED", raw={})

    def fill_later(self, order_id, qty, price=None):
        o = self.live[order_id]
        px = float(price if price is not None else o["price"])
        o["filled"] += qty
        o["amount"] += px * qty
        sym = o["symbol"]
        ex = self._positions.get(sym)
        if ex:
            tot = ex.quantity + qty
            self._positions[sym] = Position(symbol=sym, name=sym, quantity=tot,
                                            avg_price=(ex.avg_price * ex.quantity + px * qty) / tot, current_price=px)
        else:
            self._positions[sym] = Position(symbol=sym, name=sym, quantity=qty, avg_price=px, current_price=px)

    def get_order_status(self, order_id, symbol="", trading_date=""):
        self.status_calls.append(order_id)
        if self.status_error:
            return {"ok": False, "found": False, "error": "EGW_FAKE"}
        o = self.live.get(order_id)
        if o is None:
            return {"ok": True, "found": False}
        rem = 0 if o["cancelled"] else o["qty"] - o["filled"]
        return {"ok": True, "found": True, "order_id": order_id, "symbol": o["symbol"], "order_qty": o["qty"],
                "filled_qty": o["filled"], "remaining_qty": rem, "cancelled": o["cancelled"],
                "cancelled_qty": (o["qty"] - o["filled"]) if o["cancelled"] else 0, "rejected_qty": 0,
                "avg_price": (o["amount"] / o["filled"]) if o["filled"] else 0.0, "amount": o["amount"],
                "order_price": o["price"]}


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 2)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)
    monkeypatch.setattr(order_executor, "POST_CANCEL_RECHECK_DELAY_SEC", 0.0)


def _flat_world():
    svc, now = _market(inverse_price=10_000.0, long_price=5_450.0)
    state = _fresh_3slot_state()
    state.position = None
    broker = LiveOrderBroker(cash=10_000_000.0, quotes={LONG: 5_450.0, INV: 10_000.0})
    broker.next_buy_fill_qty = 0                      # 접수만, 60초(테스트 2회) 동안 미체결
    return svc, now, state, broker


def _enter(svc, now, state, broker, direction=Direction.UP_RED, sid="20261016_094500_UP_RED:TW2_3SLOT_CONFIRM"):
    res = worker.TickResult()
    out = worker._execute_or_wait(broker=broker, market_data=svc, state=state, now=now, macd_snap=_snap(now),
                                  direction=direction, signal_id=sid, signal_type="INITIAL", position=state.position,
                                  result=res, signal_detected_at=now)
    return out, res


def _signal_rows():
    return ledger.load_signal_ledger() if hasattr(ledger, "load_signal_ledger") else []


def _register_live_order(svc, now, state, broker):
    out, res = _enter(svc, now, state, broker)
    assert out.final_state == SignalState.BLOCKED
    assert out.block_reason == pending_order.PENDING_ORDER_UNCONFIRMED
    assert out.order_failure_stage == order_executor.BUY_CANCEL_NOT_CONFIRMED
    p = state.pending_buy_order
    assert p and p["symbol"] == LONG and p["direction"] == "UP_RED" and p["requested_qty"] > 0
    assert p["order_id"] in broker.live and state.position is None
    assert res.signal_dispatch_trace["pending_order"]["order_id"] == p["order_id"]
    return p


# ── 1) 2분 뒤 늦은 체결 (부분 → 전량) ────────────────────────────────────────
def test_late_fill_two_minutes_later_is_booked_to_original_signal():
    svc, now, state, broker = _flat_world()
    p = _register_live_order(svc, now, state, broker)
    oid, qty, sid = p["order_id"], p["requested_qty"], p["signal_id"]

    assert worker.track_pending_order(broker, state, now + timedelta(seconds=30), force=True)["action"] == "OPEN"
    broker.fill_later(oid, 500, 5_460.0)                                   # 1분 뒤 부분체결
    r = worker.track_pending_order(broker, state, now + timedelta(minutes=1), force=True)
    assert r["action"] == "FILL_RECORDED" and r["new_qty"] == 500
    assert state.position.symbol == LONG and state.position.quantity == 500
    assert pending_order.is_active(state), "잔량이 남아 있으면 아직 확정이 아니다"

    broker.fill_later(oid, qty - 500, 5_460.0)                             # 2분 뒤 나머지 체결
    r = worker.track_pending_order(broker, state, now + timedelta(minutes=2), force=True)
    assert r["action"] == pending_order.LATE_FILL_CONFIRMED
    assert state.pending_buy_order is None
    assert state.position.quantity == qty and state.position.avg_price == pytest.approx(5_460.0)
    assert state.last_pending_order_result["result"] == pending_order.LATE_FILL_CONFIRMED

    buys = [x for x in ledger.load_execution_ledger() if x["side"] == "BUY"]
    assert sum(int(x["executed_qty"]) for x in buys) == qty
    assert {x["signal_id"] for x in buys} == {sid}, "늦은 체결은 원래 자동신호의 주문으로 기록"
    assert {x["order_id"] for x in buys} == {oid, f"{oid}#2"}, "부분체결 2건 모두 기록(2번째는 #2)"
    # reconcile 이 출처 모를 보유로 다시 흡수하지 않는다
    rc = worker.reconcile_position_state(broker, state, now + timedelta(minutes=2, seconds=5), force=True)
    assert rc != worker.RECOVERED_FROM_BROKER
    assert not any(str(x.get("source")) == "RECONCILE_BACKFILL" for x in ledger.load_execution_ledger())


# ── 2) 실제 취소 확정 ─────────────────────────────────────────────────────────
def test_cancel_confirmed_later_ends_pending_without_position():
    svc, now, state, broker = _flat_world()
    p = _register_live_order(svc, now, state, broker)
    broker.live[p["order_id"]]["cancelled"] = True                         # KIS 에서 취소 확정(또는 장마감 소멸)
    r = worker.track_pending_order(broker, state, now + timedelta(minutes=3), force=True)
    assert r["action"] == pending_order.CANCEL_CONFIRMED
    assert state.pending_buy_order is None and state.position is None
    assert not [x for x in ledger.load_execution_ledger() if x["side"] == "BUY"]


def test_query_error_never_confirms_anything():
    svc, now, state, broker = _flat_world()
    _register_live_order(svc, now, state, broker)
    broker.status_error = True
    r = worker.track_pending_order(broker, state, now + timedelta(minutes=1), force=True)
    assert r["action"] == "UNKNOWN" and r["state"] == "UNKNOWN" and pending_order.is_active(state)


# ── 3) 재시작 후 복원 ─────────────────────────────────────────────────────────
def test_pending_survives_restart_and_tracking_resumes():
    svc, now, state, broker = _flat_world()
    p = _register_live_order(svc, now, state, broker)
    state_store.save_state(state)
    restored = state_store.load_state()                                    # 프로세스 재시작
    assert restored.pending_buy_order == p
    broker.fill_later(p["order_id"], p["requested_qty"])
    r = worker.track_pending_order(broker, restored, now + timedelta(minutes=5), force=True)
    assert r["action"] == pending_order.LATE_FILL_CONFIRMED
    assert restored.position.quantity == p["requested_qty"]
    state_store.save_state(restored)
    assert state_store.load_state().pending_buy_order is None


def test_legacy_state_without_field_loads_as_no_pending():
    raw = state_store.serialize(state_store.default_state())
    raw.pop("pending_buy_order", None)
    raw.pop("last_pending_order_result", None)
    assert state_store.deserialize(raw).pending_buy_order is None


# ── 4) pending 중 수동매수 차단 ───────────────────────────────────────────────
def test_manual_buy_blocked_for_both_symbols_while_pending():
    svc, now, state, broker = _flat_world()
    p = _register_live_order(svc, now, state, broker)
    state.auto_trade_on = True
    state_store.save_state(state)
    s = service_module.Macd2Service()
    s._worker = types.SimpleNamespace(is_alive=lambda: True)
    s._broker = broker
    s._market_data = types.SimpleNamespace(get_quote=lambda sym: QuoteSnapshot(
        symbol=sym, price=5_450.0, fetched_at=now, age_sec=0.1, source="fake"))
    n_orders = len(broker.orders)
    for d, want in (("UP_RED", pending_order.PENDING_ORDER_UNCONFIRMED),
                    ("DOWN_BLUE", pending_order.PENDING_ORDER_UNCONFIRMED_OPPOSITE)):
        res = s.manual_entry(d)
        assert res["ok"] is False and res["block_reason"] == want
        assert res["message"] == pending_order.UI_WARNING and res["pending_order_id"] == p["order_id"]
    assert len(broker.orders) == n_orders, "수동 주문이 브로커로 나가면 안 된다"
    # 확정되면 자동 해제
    broker.live[p["order_id"]]["cancelled"] = True
    st2 = state_store.load_state()
    worker.track_pending_order(broker, st2, now + timedelta(minutes=1), force=True)
    state_store.save_state(st2)
    assert pending_order.block_reason_for(state_store.load_state(), "UP_RED") is None


# ── 5) pending 중 같은 방향 자동주문 차단 ─────────────────────────────────────
def test_same_direction_auto_order_blocked_while_pending():
    svc, now, state, broker = _flat_world()
    _register_live_order(svc, now, state, broker)
    n_orders = len(broker.orders)
    out, res = _enter(svc, now + timedelta(minutes=3), state, broker, sid="20261016_095400_UP_RED:TW2_3SLOT_CONFIRM")
    assert out.final_state == SignalState.BLOCKED and out.block_reason == pending_order.PENDING_ORDER_UNCONFIRMED
    assert not out.timestamps.get("buy_requested_at")
    assert len(broker.orders) == n_orders and res.skipped == pending_order.PENDING_ORDER_UNCONFIRMED


# ── 6) 반대방향 신호 ─────────────────────────────────────────────────────────
def test_opposite_signal_blocks_buy_tries_cancel_then_enters_after_confirm():
    svc, now, state, broker = _flat_world()
    p = _register_live_order(svc, now, state, broker)
    n_orders = len(broker.orders)
    broker.cancel_works = True                                              # (향후 취소 API 가 생긴 경우)
    out, _ = _enter(svc, now + timedelta(minutes=2), state, broker, direction=Direction.DOWN_BLUE,
                    sid="20261016_095700_DOWN_BLUE:TW2_3SLOT_CONFIRM")
    assert out.block_reason == pending_order.PENDING_ORDER_UNCONFIRMED_OPPOSITE
    assert len(broker.orders) == n_orders, "살아 있는 레버리지 매수가 있는데 인버스를 사면 양방향 동시보유"
    assert (p["order_id"], LONG) in broker.cancel_calls and state.pending_buy_order["cancel_attempts"] >= 2
    r = worker.track_pending_order(broker, state, now + timedelta(minutes=2, seconds=10), force=True)
    assert r["action"] == pending_order.CANCEL_CONFIRMED and state.position is None
    broker.next_buy_fill_qty = None                                         # 이제 정상 체결되는 브로커
    out2, _ = _enter(svc, now + timedelta(minutes=3), state, broker, direction=Direction.DOWN_BLUE,
                     sid="20261016_100000_DOWN_BLUE:TW2_3SLOT_CONFIRM")
    assert out2.final_state == SignalState.EXECUTED and out2.target_symbol == INV   # 위치 반영은 호출부(_apply_switch_outcome)
    assert broker.get_position(INV).quantity > 0 and broker.get_position(LONG) is None


def test_opposite_signal_when_cancel_unsupported_keeps_tracking():
    svc, now, state, broker = _flat_world()
    p = _register_live_order(svc, now, state, broker)
    out, _ = _enter(svc, now + timedelta(minutes=2), state, broker, direction=Direction.DOWN_BLUE,
                    sid="20261016_095700_DOWN_BLUE:TW2_3SLOT_CONFIRM")
    assert out.block_reason == pending_order.PENDING_ORDER_UNCONFIRMED_OPPOSITE
    assert "CANCEL_NOT_SUPPORTED" in state.pending_buy_order["last_cancel_result"]
    broker.fill_later(p["order_id"], p["requested_qty"])                    # 그 사이 레버리지가 체결돼도
    r = worker.track_pending_order(broker, state, now + timedelta(minutes=3), force=True)
    assert r["action"] == pending_order.LATE_FILL_CONFIRMED and state.position.symbol == LONG


# ── 기존 동작 보존 / 계측 ─────────────────────────────────────────────────────
def test_no_pending_means_no_tracking_calls_and_normal_execution():
    svc, now, state, broker = _flat_world()
    broker.next_buy_fill_qty = None
    assert worker.track_pending_order(broker, state, now) is None and broker.status_calls == []
    out, res = _enter(svc, now, state, broker)
    assert out.final_state == SignalState.EXECUTED and state.pending_buy_order is None
    assert broker.status_calls == [] and "pending_order" not in res.signal_dispatch_trace
    lat = res.signal_dispatch_trace["latency"]
    for k in ("position_reconcile", "quote_wait", "ask1_fetch", "buy_sizing_query", "buy_send_to_ack",
              "signal_to_buy_sent"):
        assert lat[k] is not None and lat[k] >= 0, k


def test_executor_still_reports_buy_cancel_not_confirmed_unchanged():
    """order_executor 판정 자체는 바뀌지 않는다(재분류는 worker 의 register 에서만)."""
    broker = LiveOrderBroker(cash=10_000_000.0, quotes={LONG: 5_450.0})
    broker.next_buy_fill_qty = 0
    out = order_executor.execute_signal(broker=broker, direction=Direction.UP_RED, signal_id="x",
                                        quotes={LONG: 5_450.0}, position=None, budget=10_000_000.0,
                                        reconcile_retries=1, reconcile_delay_sec=0.0)
    assert out.final_state == SignalState.BLOCKED and out.block_reason == order_executor.BUY_CANCEL_NOT_CONFIRMED
    for k in ("ask_fetch_started_at", "ask_fetch_done_at", "sizing_started_at", "sizing_done_at",
              "buy_requested_at", "buy_confirmed_at"):
        assert out.timestamps.get(k), k
