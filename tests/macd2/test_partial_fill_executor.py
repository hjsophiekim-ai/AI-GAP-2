"""부분체결 재호가 -- order_executor 연결 (2026-10-23, MACD2_PARTIAL_FILL_REPRICE_ENABLED 기본 OFF).

OFF: 재호가 모듈이 한 번도 만들어지지 않고 기존 경로 그대로(전체 tests/macd2 회귀가 parity 를 지킨다).
ON : CANCEL_CONFIRMED 전 추가주문 0 / CANCEL_FAILED 면 추가매수 0 / 살아 있는 매수주문 최대 1개 /
     목표수량 초과 0 / TP 근처 추격 금지 / 신호 무효화 시 잔량 취소 후 종료 / 재호가 최대 2회.
"""
from __future__ import annotations

import pytest

from app.trading.macd2 import config, ledger, order_executor, partial_fill_reprice as pfr
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import Direction, SignalState
from tests.macd2.test_order_execution_v2 import V2Broker

LONG = config.LONG_SYMBOL


class PFBroker(V2Broker):
    """V2Broker + 취소 v3(주문당 1회 기록) + 첫 주문 뒤 바뀌는 매도호가 + 살아 있는 주문 수 감시."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.v3_allowed = True
        self.v3_calls: list[str] = []
        self.ask_after_first = 5_470.0
        self.live_at_buy: list[int] = []
        self.market_open = True

    def is_market_open(self):
        return self.market_open

    def get_fresh_ask1(self, symbol):
        a = 5_450.0 if not self.live else self.ask_after_first
        return {"ok": True, "symbol": symbol, "ask1": a, "age_sec": 0.1}

    def buy_limit(self, symbol, qty, price, client_order_id):
        self.live_at_buy.append(sum(1 for o in self.live.values() if not o["cancelled"] and o["filled"] < o["qty"]))
        return super().buy_limit(symbol, qty, price, client_order_id)

    def cancel_v3_allowed(self):
        return self.v3_allowed

    def cancel_order_v3(self, order_id, symbol="", org_no=None):
        self.v3_calls.append(order_id)
        o = self.live.get(order_id)
        if o and o["filled"] < o["qty"]:
            o["cancelled"] = True
        return BrokerOrderResult(True, order_id, symbol, "CANCEL", 0, 0, 0.0, "OK", raw={"status": "ACCEPTED"})


@pytest.fixture
def pf_on(monkeypatch):
    monkeypatch.setattr(config, "PARTIAL_FILL_REPRICE_ENABLED", True)
    monkeypatch.setattr(config, "EXEC_V2_ENABLED", False)
    monkeypatch.setattr(order_executor, "PARTIAL_FILL_SLEEP", lambda s: None)
    monkeypatch.setattr(order_executor, "POST_CANCEL_RECHECK_DELAY_SEC", 0.0)


def _broker(fills, **kw):
    b = PFBroker(cash=10_000_000.0, quotes={LONG: 5_450.0})
    b.fill_queue = list(fills)
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _run(b, sid="SIG-PF", ledger_module=None, signal_valid=None):
    return order_executor.execute_signal(broker=b, direction=Direction.UP_RED, signal_id=sid,
                                         quotes={LONG: 5_450.0}, position=None, budget=10_000_000.0,
                                         reconcile_retries=1, reconcile_delay_sec=0.0,
                                         ledger_module=ledger_module, signal_valid=signal_valid)


def _buys(b):
    return [c for c in b.buy_calls] if hasattr(b, "buy_calls") else []


def _common(b, out):
    assert all(n == 0 for n in b.live_at_buy[1:]), "살아 있는 매수주문 위에 새 주문"
    assert len(set(b.v3_calls)) == len(b.v3_calls), "같은 주문 중복 취소"
    total = sum(o["filled"] for o in b.live.values())
    assert total <= out.final_qty, "목표수량 초과"
    assert len(b.live) <= 3, "재호가 2회 초과"


# ── OFF parity ───────────────────────────────────────────────────────────────
def test_off_never_builds_repricer(monkeypatch):
    monkeypatch.setattr(order_executor, "PARTIAL_FILL_SLEEP", lambda s: None)
    monkeypatch.setattr(pfr, "PartialFillRepricer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("OFF 인데 호출")))
    assert config.PARTIAL_FILL_REPRICE_ENABLED is False
    b = _broker([117])
    out = _run(b)
    assert b.v3_calls == [] and len(b.live) == 1
    assert "partial_fill_reprice" not in out.exec_log


def test_off_outcome_identical_with_and_without_v3_capable_broker(monkeypatch):
    """OFF 에서는 브로커가 취소 v3 를 지원하든 말든 결과가 같다(기존 경로만 탄다)."""
    monkeypatch.setattr(order_executor, "POST_CANCEL_RECHECK_DELAY_SEC", 0.0)
    a = _broker([117]); out_a = _run(a, sid="SIG-A")
    b = _broker([117]); b.v3_allowed = False; out_b = _run(b, sid="SIG-B")
    keys = ("final_state", "block_reason", "quantity", "filled_qty", "unfilled_qty", "order_failure_stage",
            "cancel_result", "fill_poll_result")
    assert {k: getattr(out_a, k) for k in keys} == {k: getattr(out_b, k) for k in keys}


# ── ON ───────────────────────────────────────────────────────────────────────
def test_on_partial_117_reprices_and_records_one_aggregate_leg(pf_on):
    b = _broker([117, 10_000])
    out = _run(b)
    _common(b, out)
    tgt = out.final_qty
    first, second = list(b.live.values())
    assert out.final_state == SignalState.EXECUTED and out.reprice_count == 1 and len(b.live) == 2
    assert second["price"] == 5_470.0
    # 더 비싼 재호가 가격 -> 현금 상한(usable_cash x 0.995) 안에서 잔량보다 조금 적게 산다
    cap = float(out.usable_cash) * 0.995
    assert second["qty"] == min(tgt - 117, int((cap - 117 * first["price"]) // 5_470.0))
    assert out.quantity == 117 + second["qty"] <= tgt
    assert out.filled_avg_price == pytest.approx((117 * first["price"] + second["qty"] * 5_470.0) / out.quantity)
    rec = out.exec_log["partial_fill_reprice"]
    assert rec["final"] in (pfr.FILLED, pfr.PARTIAL_ACCEPTED) and rec["cancel_confirmed"] is True


def test_on_cancel_not_allowed_means_no_additional_buy(pf_on):
    b = _broker([117], v3_allowed=False)
    out = _run(b)
    _common(b, out)
    assert len(b.live) == 1 and out.quantity == 117
    assert out.order_failure_stage == order_executor.CANCEL_FAILED and out.open_order is not None


def test_on_zero_fill_beyond_chase_releases_reservation(pf_on):
    b = _broker([0], ask_after_first=5_600.0)
    released = []
    b.release_daily_ordered_amount = lambda amt: released.append(amt)
    out = _run(b)
    _common(b, out)
    assert out.final_state == SignalState.FAILED and len(b.live) == 1
    assert released and released[0] == pytest.approx(out.final_qty * out.order_price)


def test_on_tp_near_blocks_chase(pf_on, monkeypatch):
    monkeypatch.setattr(order_executor, "PARTIAL_FILL_PARAM_OVERRIDES", {"max_chase_pct": 2.0})
    b = _broker([117], ask_after_first=5_500.0)                 # 5460 대비 +0.73% >= +0.5%
    out = _run(b)
    assert len(b.live) == 1 and out.quantity == 117
    assert out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_TP_NEAR


def test_on_signal_invalid_cancels_and_stops(pf_on):
    b = _broker([117])
    out = _run(b, signal_valid=lambda: False)
    assert len(b.live) == 1 and b.v3_calls == [list(b.live)[0]] and out.quantity == 117
    assert out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_SIGNAL_INVALID


def test_on_market_closed_default_validity_stops(pf_on):
    b = _broker([117], market_open=False)
    out = _run(b)
    assert len(b.live) == 1 and out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_SIGNAL_INVALID


def test_on_max_two_reprices(pf_on):
    b = _broker([100, 100, 100])
    out = _run(b)
    _common(b, out)
    assert len(b.live) == 3 and out.reprice_count == 2 and out.quantity == 300
    assert out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_MAX_REPRICE


def test_on_cash_cap_shrinks_reprice_qty(pf_on):
    b = _broker([117, 10_000])
    out = _run(b)
    spent = sum(o["filled"] * o["price"] for o in b.live.values())
    assert spent <= float(out.usable_cash) * 0.995 + 1e-6


def test_on_does_not_apply_to_other_ledger_modules(pf_on, monkeypatch):
    """MU_MACD 처럼 다른 원장 모듈로 executor 를 쓰면 ON 이어도 재호가하지 않는다."""
    import types
    other = types.SimpleNamespace(record_execution=getattr(ledger, "record_execution", None))
    monkeypatch.setattr(pfr, "PartialFillRepricer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("호출 금지")))
    b = _broker([1_000_000])
    try:
        _run(b, ledger_module=other)
    except AssertionError:
        raise
    except Exception:
        pass                       # 가짜 원장 모듈의 기록 실패는 이 테스트의 관심사가 아니다
    assert b.v3_calls == []


# ── 취소 미확인 / 상태조회 불가 처리 (2026-10-23 검토 보강) ───────────────────────
from datetime import datetime, timedelta, timezone
import types

KST = timezone(timedelta(hours=9))

from app.trading.macd2 import pending_order
from app.trading.macd2.models import PositionSnapshot


class OneCancelBroker(PFBroker):
    """첫 취소만 확정되고 그 뒤 취소는 접수만 되고 잔량이 살아 있다(재호가 주문 취소 실패)."""

    def cancel_order_v3(self, order_id, symbol="", org_no=None):
        if self.v3_calls:
            self.v3_calls.append(order_id)
            return BrokerOrderResult(True, order_id, symbol, "CANCEL", 0, 0, 0.0, "OK", raw={"status": "ACCEPTED"})
        return super().cancel_order_v3(order_id, symbol, org_no)


def _pf_world(b, out, now):
    state = types.SimpleNamespace(position=None, pending_buy_order=None, last_pending_order_result=None)
    if out.filled_qty:
        state.position = PositionSnapshot(symbol=LONG, quantity=out.filled_qty, avg_price=out.filled_avg_price,
                                          entry_at=now)
    assert pending_order.register(state, out, direction=Direction.UP_RED, signal_id="SIG-PF",
                                  signal_type="T", now=now)
    return state


def _apply(state, qty, px):
    state.position = PositionSnapshot(symbol=LONG, quantity=qty, avg_price=px, entry_at=datetime.now(KST))


def test_on_reprice_order_cancel_unconfirmed_pending_counts_only_that_order(pf_on):
    """117주 보유 + 재호가 주문 취소 미확인 -> pending 은 재호가 주문만 추적하고, 늦은 체결을 빠짐없이 센다."""
    b = OneCancelBroker(cash=10_000_000.0, quotes={LONG: 5_450.0})
    b.fill_queue = [117, 0]
    out = _run(b)
    _common(b, out)
    first, second = list(b.live)
    assert out.final_state == SignalState.EXECUTED and out.quantity == 117
    assert out.open_order["order_id"] == second and out.open_order["held_offset"] == 117
    now = datetime(2026, 10, 23, 12, 34, tzinfo=KST)
    state = _pf_world(b, out, now)
    p = state.pending_buy_order
    assert p["order_id"] == second and p["requested_qty"] == b.live[second]["qty"] and p["recorded_qty"] == 0
    b.fill_later(second, b.live[second]["qty"])
    r = pending_order.check(b, state, now + timedelta(minutes=1), apply_new_position=_apply, force=True)
    assert r["action"] == pending_order.LATE_FILL_CONFIRMED
    assert state.position.quantity == 117 + b.live[second]["qty"], "다른 주문 보유분 때문에 늦은 체결을 덜 세면 안 됨"


def test_on_reprice_order_cancel_unconfirmed_balance_fallback_uses_offset(pf_on):
    b = OneCancelBroker(cash=10_000_000.0, quotes={LONG: 5_450.0})
    b.fill_queue = [117, 0]
    out = _run(b)
    second = list(b.live)[1]
    now = datetime(2026, 10, 23, 12, 34, tzinfo=KST)
    state = _pf_world(b, out, now)
    b.status_error = True                                              # 이후 주문조회 불가 -> 잔고로 판단
    b.fill_later(second, 50)
    r = pending_order.check(b, state, now + timedelta(minutes=1), apply_new_position=_apply, force=True)
    assert r["new_qty"] == 50 and state.position.quantity == 167 and state.pending_buy_order["recorded_qty"] == 50


def test_on_zero_fill_then_reprice_cancel_unconfirmed_tracks_reprice_order(pf_on):
    b = OneCancelBroker(cash=10_000_000.0, quotes={LONG: 5_450.0})
    b.fill_queue = [0, 0]
    out = _run(b)
    _common(b, out)
    first, second = list(b.live)
    assert out.final_state == SignalState.BLOCKED and out.block_reason == order_executor.BUY_CANCEL_NOT_CONFIRMED
    assert out.open_order["order_id"] == second
    state = _pf_world(b, out, datetime(2026, 10, 23, 12, 34, tzinfo=KST))
    assert state.pending_buy_order["order_id"] == second, "살아 있는 건 재호가 주문 -- 원주문을 추적하면 안 됨"
    assert out.block_reason == pending_order.PENDING_ORDER_UNCONFIRMED


def test_on_status_unavailable_at_first_check_falls_back_to_legacy_path(pf_on, monkeypatch):
    """첫 주문 상태조회부터 안 되면(모의계좌 실측) 재호가하지 않고 기존 잔고 확인 경로와 같은 결과."""
    on = _broker([117], status_error=True)
    out_on = _run(on, sid="SIG-ON")
    monkeypatch.setattr(config, "PARTIAL_FILL_REPRICE_ENABLED", False)
    off = _broker([117], status_error=True)
    out_off = _run(off, sid="SIG-OFF")
    keys = ("final_state", "block_reason", "quantity", "filled_qty", "unfilled_qty", "order_failure_stage",
            "cancel_result", "fill_poll_result")
    assert {k: getattr(out_on, k) for k in keys} == {k: getattr(out_off, k) for k in keys}
    assert on.v3_calls == [] and len(on.live) == 1
    assert out_on.exec_log["partial_fill_reprice"]["fallback"] == "LEGACY"
