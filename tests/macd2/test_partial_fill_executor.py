"""부분체결 잔량 재호가 -- order_executor 기본 기능 (2026-10-23, 사용자 토글 없음, 비상차단 env 만).

전량체결: 재호가 경로를 아예 타지 않는다(기존 결과 그대로).
잔량 있음 + 주문번호 조회 가능(실계좌) : 조회 -> 잔량 취소 v3 -> 취소 확정 -> 재호가 (ACTIVE)
잔량 있음 + 주문번호 조회 불가(모의)   : 잔량 취소 요청 1회 + '재호가했다면' 기록, 재주문 0 (SHADOW_ONLY)
실계좌에서 상태조회 불가·취소 미확정  : 그날은 기존 경로만 (SAFE_FALLBACK)

불변식(모든 테스트 공통): CANCEL_CONFIRMED 전 추가주문 0 / 살아 있는 매수주문 최대 1개 / 같은 주문 중복취소 0 /
목표수량 초과 0 / 재호가 최대 2회.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from app.trading.macd2 import config, ledger, order_executor, partial_fill_reprice as pfr, pending_order
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import Direction, PositionSnapshot, SignalState
from tests.macd2.test_order_execution_v2 import V2Broker

LONG = config.LONG_SYMBOL
KST = timezone(timedelta(hours=9))
KEYS = ("final_state", "block_reason", "quantity", "filled_qty", "unfilled_qty", "order_failure_stage",
        "cancel_result", "fill_poll_result", "filled_avg_price", "reprice_count")


class PFBroker(V2Broker):
    """V2Broker + 취소 v3(주문당 1회 기록) + 첫 주문 뒤 바뀌는 매도호가 + 살아 있는 주문 수 감시."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.v3_calls: list[str] = []
        self.ask_after_first = 5_470.0
        self.live_at_buy: list[int] = []
        self.market_open = True
        self.cancel_behavior = "confirm"     # confirm / accept_but_alive / raise / fill_during
        self.fill_during_qty = 0
        self.fill_after_first_poll = 0       # 잔고 첫 조회 뒤(상태조회 전) 원주문에 추가 체결

        self._polls = 0

    def is_market_open(self):
        return self.market_open

    def get_fresh_ask1(self, symbol):
        a = 5_450.0 if not self.live else self.ask_after_first
        return {"ok": True, "symbol": symbol, "ask1": a, "age_sec": 0.1}

    def get_position(self, symbol):
        pos = super().get_position(symbol)
        self._polls += 1
        if self._polls == 1 and self.fill_after_first_poll and self.live:
            o = list(self.live.values())[0]
            self.fill_later(list(self.live)[0], o["qty"] - o["filled"])
            self.fill_after_first_poll = 0
        return pos

    def buy_limit(self, symbol, qty, price, client_order_id):
        self.live_at_buy.append(sum(1 for o in self.live.values() if not o["cancelled"] and o["filled"] < o["qty"]))
        return super().buy_limit(symbol, qty, price, client_order_id)

    def cancel_v3_allowed(self):
        return True

    def cancel_order_v3(self, order_id, symbol="", org_no=None):
        self.v3_calls.append(order_id)
        if self.cancel_behavior == "raise":
            raise TimeoutError("read timeout")
        o = self.live.get(order_id)
        if o and o["filled"] < o["qty"]:
            if self.cancel_behavior == "fill_during" and self.fill_during_qty:
                self.fill_later(order_id, self.fill_during_qty)
                self.fill_during_qty = 0
            if self.cancel_behavior != "accept_but_alive":
                o["cancelled"] = True
        return BrokerOrderResult(True, order_id, symbol, "CANCEL", 0, 0, 0.0, "OK", raw={"status": "ACCEPTED"})


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(order_executor, "PARTIAL_FILL_SLEEP", lambda s: None)
    monkeypatch.setattr(order_executor, "POST_CANCEL_RECHECK_DELAY_SEC", 0.0)
    monkeypatch.setattr(order_executor, "_pf_now", lambda: datetime(2026, 10, 23, 12, 33, 10, tzinfo=KST))
    pfr.reset_breaker()
    yield
    pfr.reset_breaker()


def _broker(fills, cls=PFBroker, **kw):
    b = cls(cash=10_000_000.0, quotes={LONG: 5_450.0})
    b.fill_queue = list(fills)
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _run(b, sid="SIG-PF", ledger_module=None):
    return order_executor.execute_signal(broker=b, direction=Direction.UP_RED, signal_id=sid,
                                         quotes={LONG: 5_450.0}, position=None, budget=10_000_000.0,
                                         reconcile_retries=1, reconcile_delay_sec=0.0, ledger_module=ledger_module)


def _common(b, out):
    assert all(n == 0 for n in b.live_at_buy[1:]), "살아 있는 매수주문 위에 새 주문"
    assert len(set(b.v3_calls)) == len(b.v3_calls), "같은 주문 중복 취소"
    total = sum(o["filled"] for o in b.live.values())
    assert total <= (out.final_qty or total), "목표수량 초과"
    assert len(b.live) <= 3, "재호가 2회 초과"


def _no_repricer(monkeypatch):
    monkeypatch.setattr(pfr, "PartialFillRepricer",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("재호가 경로를 타면 안 됨")))


def _buys(sid=None):
    return [r for r in ledger.load_execution_ledger() if r["side"] == "BUY" and (sid is None or sid in r["signal_id"])]


# ── 1. 전량체결: 기존 결과 변화 0 ─────────────────────────────────────────────
def test_full_fill_never_enters_partial_path_and_matches_legacy(monkeypatch):
    _no_repricer(monkeypatch)
    a = _broker([])                                         # 전량 즉시 체결
    out_a = _run(a, sid="SIG-A")
    b = _broker([], cls=V2Broker)                          # 취소 v3 가 아예 없는 기존 브로커
    out_b = _run(b, sid="SIG-B")
    assert {k: getattr(out_a, k) for k in KEYS} == {k: getattr(out_b, k) for k in KEYS}
    assert a.v3_calls == [] and a.status_calls == [] and "partial_fill_reprice" not in out_a.exec_log
    la = _buys("SIG-A")                     # (두 가짜 브로커가 같은 주문번호를 써서 원장은 A 만 본다 -- 원장 dedup)
    assert len(la) == 1 and int(float(la[0]["executed_qty"])) == out_a.quantity == out_b.quantity


def test_kill_switch_restores_legacy_path_exactly(monkeypatch):
    monkeypatch.setattr(config, "PARTIAL_FILL_REPRICE_KILL", True)
    _no_repricer(monkeypatch)
    a = _broker([117]); out_a = _run(a, sid="SIG-A")
    b = _broker([117], cls=V2Broker); out_b = _run(b, sid="SIG-B")
    assert {k: getattr(out_a, k) for k in KEYS} == {k: getattr(out_b, k) for k in KEYS}
    assert a.v3_calls == [] and len(a.live) == 1


def test_other_ledger_module_mu_macd_not_applied(monkeypatch):
    _no_repricer(monkeypatch)
    other = types.SimpleNamespace(record_execution=getattr(ledger, "record_execution", None))
    b = _broker([117])
    try:
        _run(b, ledger_module=other)
    except AssertionError:
        raise
    except Exception:
        pass                       # 가짜 원장 모듈의 기록 실패는 이 테스트의 관심사가 아니다
    assert b.v3_calls == []


# ── 2. 실계좌 ACTIVE: 부분체결 -> 취소확정 -> 재호가 ────────────────────────────
def test_partial_cancel_confirmed_then_reprice_one_aggregate_leg():
    b = _broker([117, 10_000], mode="real")
    out = _run(b)
    _common(b, out)
    first, second = list(b.live.values())
    assert b.v3_calls == [list(b.live)[0]], "원주문 잔량 취소 1회"
    assert out.final_state == SignalState.EXECUTED and out.reprice_count == 1
    assert second["price"] == 5_470.0
    cap = float(out.usable_cash) * 0.995
    assert second["qty"] == min(out.final_qty - 117, int((cap - 117 * first["price"]) // 5_470.0)), "현금 한도로 수량 재산정"
    assert out.quantity == 117 + second["qty"] <= out.final_qty
    assert out.filled_avg_price == pytest.approx((117 * first["price"] + second["qty"] * 5_470.0) / out.quantity)
    rec = out.exec_log["partial_fill_reprice"]
    assert rec["route"] == order_executor.PF_ROUTE_ACTIVE and rec["cancel_confirmed"] is True
    assert len(_buys()) == 1, "새 주문을 독립 포지션으로 쌓지 않는다 -- 신호당 BUY 레그 1개"


def test_fills_during_cancel_reprice_only_true_remainder():
    b = _broker([117, 10_000], mode="real", cancel_behavior="fill_during", fill_during_qty=500)
    out = _run(b)
    _common(b, out)
    first, second = list(b.live.values())
    assert first["filled"] == 617 and second["qty"] <= out.final_qty - 617
    assert out.quantity == 617 + second["filled"] <= out.final_qty


def test_cancel_failed_means_zero_additional_buy_and_pending_handoff():
    b = _broker([117], mode="real", cancel_behavior="accept_but_alive")
    out = _run(b)
    _common(b, out)
    assert len(b.live) == 1 and out.quantity == 117, "CANCEL_FAILED 면 추가매수 0"
    assert out.order_failure_stage == order_executor.CANCEL_FAILED and out.open_order["order_id"] == list(b.live)[0]
    assert pfr.breaker_reason(datetime.now(KST).strftime("%Y%m%d")) == pfr.CANCEL_FAILED


def test_cancel_request_network_timeout_no_additional_buy():
    b = _broker([117], mode="real", cancel_behavior="raise")
    out = _run(b)
    _common(b, out)
    assert len(b.live) == 1 and out.quantity == 117 and b.v3_calls == [list(b.live)[0]], "timeout 뒤 재전송 0"
    assert out.order_failure_stage == order_executor.CANCEL_FAILED


def test_partial_on_balance_but_fully_filled_by_status_check():
    """잔고 첫 조회는 117주였지만 주문번호 조회 시점엔 전량체결 -> 취소·재호가 없이 전량으로 기록."""
    b = _broker([117], mode="real")
    b.fill_after_first_poll = 1                   # 첫 잔고조회 직후 원주문 잔량 전부 체결
    out = _run(b)
    _common(b, out)
    assert b.v3_calls == [] and len(b.live) == 1
    assert out.final_state == SignalState.EXECUTED and out.quantity == out.final_qty


def test_high_fill_ratio_cancels_remainder_without_chasing():
    out0 = _run(_broker([], mode="real"), sid="SIG-SIZE")    # 목표수량 알아내기
    b = _broker([int(out0.final_qty * 0.8)], mode="real")
    out = _run(b)
    _common(b, out)
    assert len(b.live) == 1 and b.v3_calls == [list(b.live)[0]]
    assert out.exec_log["partial_fill_reprice"]["final"] == pfr.PARTIAL_ACCEPTED


def test_chase_limit_cancels_remainder_and_stops():
    b = _broker([117], mode="real", ask_after_first=5_600.0)       # 0.3% 상한(5,475) 초과
    out = _run(b)
    _common(b, out)
    assert len(b.live) == 1 and out.quantity == 117
    assert out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_CHASE_LIMIT


def test_zero_fill_beyond_chase_releases_reservation():
    b = _broker([0], mode="real", ask_after_first=5_600.0)
    released = []
    b.release_daily_ordered_amount = lambda amt: released.append(amt)
    out = _run(b)
    _common(b, out)
    assert out.final_state == SignalState.FAILED and len(b.live) == 1
    assert released and released[0] == pytest.approx(out.final_qty * out.order_price)


def test_tp_near_blocks_chase(monkeypatch):
    monkeypatch.setattr(order_executor, "PARTIAL_FILL_PARAM_OVERRIDES", {"max_chase_pct": 2.0})
    b = _broker([117], mode="real", ask_after_first=5_500.0)          # 체결가 대비 +0.73% >= +0.5%
    out = _run(b)
    assert len(b.live) == 1 and out.quantity == 117
    assert out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_TP_NEAR


def test_position_changed_meanwhile_blocks_reorder():
    """넘겨받은 뒤 보유수량이 줄었으면(그 사이 매도/청산) 재주문 금지, 잔량만 취소."""
    b = _broker([117], mode="real")
    orig = b.get_fresh_ask1

    def ask_and_sell(symbol):
        b._positions.pop(LONG, None)                                  # 보유분이 사라짐
        return orig(symbol)
    b.get_fresh_ask1 = ask_and_sell
    out = _run(b)
    assert len(b.live) == 1 and out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_SIGNAL_INVALID


def test_market_closed_blocks_reorder():
    b = _broker([117], mode="real", market_open=False)
    out = _run(b)
    assert len(b.live) == 1 and out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_SIGNAL_INVALID


@pytest.mark.parametrize("hhmm", [(8, 59), (15, 20), (15, 29)])
def test_outside_continuous_session_blocks_reorder(monkeypatch, hhmm):
    """실제 adapter.is_market_open 은 항상 True -- 시간은 executor 가 직접 본다(장전·마감 동시호가엔 재호가 금지)."""
    monkeypatch.setattr(order_executor, "_pf_now", lambda: datetime(2026, 10, 23, *hhmm, tzinfo=KST))
    b = _broker([117], mode="real")
    out = _run(b)
    assert len(b.live) == 1 and out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_SIGNAL_INVALID


def test_max_two_reprices():
    b = _broker([100, 100, 100], mode="real")
    out = _run(b)
    _common(b, out)
    assert len(b.live) == 3 and out.reprice_count == 2 and out.quantity == 300
    assert out.exec_log["partial_fill_reprice"]["final"] == pfr.ABANDONED_MAX_REPRICE


def test_cash_cap_never_exceeded():
    b = _broker([117, 10_000], mode="real")
    out = _run(b)
    spent = sum(o["filled"] * o["price"] for o in b.live.values())
    assert spent <= float(out.usable_cash) * 0.995 + 1e-6


# ── 3. 주문조회 실패 ──────────────────────────────────────────────────────────
def test_mock_status_unavailable_is_shadow_only_no_reorder(monkeypatch):
    """KIS 모의(주문별 행 0건): 잔량 취소 요청 1회 + '재호가했다면' 기록, 재주문 0. 판정은 기존 경로 결과."""
    a = _broker([117], mode="mock", status_error=True)
    out_a = _run(a, sid="SIG-A")
    b = _broker([117], cls=V2Broker, status_error=True)
    out_b = _run(b, sid="SIG-B")
    assert len(a.live) == 1 and a.v3_calls == [list(a.live)[0]]
    rec = out_a.exec_log["partial_fill_reprice"]
    assert rec["route"] == order_executor.PF_ROUTE_SHADOW and rec["cancel_request"]["accepted"] is True
    sh = rec["shadow"]
    assert sh["would_reprice"] is True and sh["price"] == 5_470.0 and 0 < sh["qty"] <= out_a.final_qty - 117
    assert {k: getattr(out_a, k) for k in KEYS} == {k: getattr(out_b, k) for k in KEYS}, "판정은 기존 경로와 같다"


def test_mock_status_not_found_rows_is_shadow_only():
    """모의 실측 그대로: ok=True 이지만 found=False(주문별 행 0건)."""
    b = _broker([117], mode="mock")
    b.get_order_status = lambda oid, symbol="", trading_date="": {"ok": True, "found": False}
    out = _run(b)
    assert len(b.live) == 1 and out.exec_log["partial_fill_reprice"]["route"] == order_executor.PF_ROUTE_SHADOW


def test_real_status_unavailable_trips_day_breaker_then_legacy():
    b = _broker([117], mode="real", status_error=True)
    out = _run(b, sid="SIG-1")
    day = datetime.now(KST).strftime("%Y%m%d")
    assert len(b.live) == 1 and out.exec_log["partial_fill_reprice"]["route"] == order_executor.PF_ROUTE_SAFE_FALLBACK
    assert pfr.breaker_reason(day) == "STATUS_UNAVAILABLE"
    b2 = _broker([117], mode="real")                                   # 같은 날 다음 주문: 조회·취소 v3 없이 기존 경로
    out2 = _run(b2, sid="SIG-2")
    assert b2.v3_calls == [] and b2.status_calls.count(list(b2.live)[0]) <= 1
    assert out2.exec_log["partial_fill_reprice"]["route"] == order_executor.PF_ROUTE_SAFE_FALLBACK


def test_mock_status_restored_uses_active_path_automatically():
    """모의 주문조회가 나중에 정상화되면(행이 나오면) 실계좌와 같은 경로로 자동 전환."""
    b = _broker([117, 10_000], mode="mock")
    out = _run(b)
    _common(b, out)
    assert out.exec_log["partial_fill_reprice"]["route"] == order_executor.PF_ROUTE_ACTIVE and len(b.live) == 2


# ── 4. 취소 미확인 -> pending 추적 (재호가 주문) ──────────────────────────────────
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


def test_reprice_order_cancel_unconfirmed_pending_counts_only_that_order():
    b = _broker([117, 0], cls=OneCancelBroker, mode="real")
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


def test_reprice_order_cancel_unconfirmed_balance_fallback_uses_offset():
    b = _broker([117, 0], cls=OneCancelBroker, mode="real")
    out = _run(b)
    second = list(b.live)[1]
    now = datetime(2026, 10, 23, 12, 34, tzinfo=KST)
    state = _pf_world(b, out, now)
    b.status_error = True                                              # 이후 주문조회 불가 -> 잔고로 판단
    b.fill_later(second, 50)
    r = pending_order.check(b, state, now + timedelta(minutes=1), apply_new_position=_apply, force=True)
    assert r["new_qty"] == 50 and state.position.quantity == 167 and state.pending_buy_order["recorded_qty"] == 50


def test_zero_fill_then_reprice_cancel_unconfirmed_tracks_reprice_order():
    b = _broker([0, 0], cls=OneCancelBroker, mode="real")
    out = _run(b)
    _common(b, out)
    first, second = list(b.live)
    assert out.final_state == SignalState.BLOCKED and out.block_reason == order_executor.BUY_CANCEL_NOT_CONFIRMED
    assert out.open_order["order_id"] == second
    state = _pf_world(b, out, datetime(2026, 10, 23, 12, 34, tzinfo=KST))
    assert state.pending_buy_order["order_id"] == second, "살아 있는 건 재호가 주문 -- 원주문을 추적하면 안 됨"
    assert out.block_reason == pending_order.PENDING_ORDER_UNCONFIRMED


def test_invariant_exception_stops_buying_and_hands_off(monkeypatch):
    """재호가 루프 안에서 예외가 나도 더 사지 않고, 살아 있을 수 있는 주문은 미확정으로 넘긴다."""
    b = _broker([117], mode="real")
    monkeypatch.setattr(pfr.PartialFillRepricer, "_fresh_ask",
                        lambda self: (_ for _ in ()).throw(pfr.InvariantViolation("boom")))
    out = _run(b)
    assert len(b.live) == 1 and out.quantity == 117 and out.open_order is not None
    assert pfr.breaker_reason(datetime.now(KST).strftime("%Y%m%d")).startswith("EXCEPTION")
