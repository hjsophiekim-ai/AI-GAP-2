"""부분체결 잔량 재호가 (2026-10-23) — 연구용 모듈 partial_fill_reprice 단독 테스트.

production 경로(order_executor/worker)와 연결되지 않았음을 마지막 테스트가 지킨다.
가짜 브로커는 가짜 시계 위에서 주문별 체결 일정(시각, 누적체결)을 재생한다. 취소는
'요청 + cancel_delay 초 뒤 확정'이라 그 사이 일정에 있는 체결은 실제로 들어온다(취소 중 추가체결).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.trading.macd2 import partial_fill_reprice as pfr
from app.trading.macd2.broker_adapter import BrokerOrderResult

KST = timezone(timedelta(hours=9))
SYM = "0193T0"


class Clock:
    def __init__(self):
        self.t0 = datetime(2026, 10, 23, 12, 33, 12, tzinfo=KST)
        self.t = 0.0

    def now(self):
        return self.t0 + timedelta(seconds=self.t)

    def sleep(self, s):
        self.t += max(0.0, float(s))


class FakeBroker:
    """plans[i] = i번째 주문(0 = 최초 주문)의 일정:
         fills  : [(t_from_order, cumulative_filled), ...]
         cancel : "confirm" | "unconfirmed" | "reject"
         cancel_delay : 취소 요청 후 확정까지 초
    asks = [(t_abs_from, ask1), ...]  (마지막으로 지난 구간 값)"""

    def __init__(self, clock, plans, asks=((0.0, 4950.0),), *, cancel_allowed=True, status_fail=False,
                 best_limit=False):
        self.c, self.plans, self.asks = clock, list(plans), list(asks)
        self.cancel_allowed, self.status_fail = cancel_allowed, status_fail
        self.orders: dict[str, dict] = {}
        self.buy_calls: list[tuple] = []
        self.cancel_calls: list[str] = []
        self.live_at_place: list[int] = []
        if best_limit:
            self.buy_best_limit = self._buy_best_limit

    # 주문 생성 ----------------------------------------------------------
    def _new(self, qty, price, kind):
        n = len(self.orders)
        plan = self.plans[n] if n < len(self.plans) else {}
        oid = f"ORD{n}"
        self.orders[oid] = {"qty": int(qty), "price": float(price), "t": self.c.t, "fills": plan.get("fills", []),
                            "cancel": plan.get("cancel", "confirm"), "cancel_delay": plan.get("cancel_delay", 0.5),
                            "cancel_at": None, "kind": kind}
        return oid

    def place_initial(self, qty, price):
        oid = self._new(qty, price, "INITIAL")
        return BrokerOrderResult(True, oid, SYM, "BUY", qty, 0, price, "OK", raw={})

    def _live_count(self):
        return sum(1 for oid in self.orders if self._view(oid)["remaining_qty"] > 0)

    def buy_limit(self, symbol, qty, price, client_order_id):
        self.live_at_place.append(self._live_count())
        self.buy_calls.append((symbol, int(qty), float(price), client_order_id))
        oid = self._new(qty, price, "LIMIT")
        return BrokerOrderResult(True, oid, symbol, "BUY", qty, 0, price, "OK", raw={})

    def _buy_best_limit(self, symbol, qty, client_order_id):
        self.live_at_place.append(self._live_count())
        self.buy_calls.append((symbol, int(qty), "BEST", client_order_id))
        oid = self._new(qty, self._ask(), "BEST")
        return BrokerOrderResult(True, oid, symbol, "BUY", qty, 0, 0.0, "OK", raw={})

    # 조회 ----------------------------------------------------------------
    def _ask(self):
        a = self.asks[0][1]
        for t, v in self.asks:
            if self.c.t >= t:
                a = v
        return a

    def _view(self, oid):
        o = self.orders[oid]
        horizon = self.c.t
        cancelled = False
        if o["cancel_at"] is not None and o["cancel"] == "confirm" and self.c.t >= o["cancel_at"]:
            horizon, cancelled = o["cancel_at"], True
        filled = 0
        for dt, cum in o["fills"]:
            if o["t"] + dt <= horizon:
                filled = max(filled, min(int(cum), o["qty"]))
        rem = 0 if (cancelled or filled >= o["qty"]) else o["qty"] - filled
        return {"ok": True, "found": True, "order_qty": o["qty"], "filled_qty": filled, "remaining_qty": rem,
                "amount": filled * o["price"], "cancelled": cancelled and filled < o["qty"]}

    def get_order_status(self, order_id, symbol="", trading_date=""):
        if self.status_fail:
            return {"ok": False, "found": False, "error": "boom"}
        return self._view(order_id)

    def get_fresh_ask1(self, symbol):
        return {"ok": True, "symbol": symbol, "ask1": self._ask(), "age_sec": 0.2}

    # 취소 ----------------------------------------------------------------
    def cancel_v3_allowed(self):
        return self.cancel_allowed

    def cancel_order_v3(self, order_id, symbol="", org_no=None):
        self.cancel_calls.append(order_id)
        o = self.orders[order_id]
        if o["cancel"] == "reject":
            return BrokerOrderResult(False, order_id, symbol, "CANCEL", 0, 0, 0.0, "REJECTED", raw={})
        if o["cancel_at"] is None:
            o["cancel_at"] = self.c.t + o["cancel_delay"]
        return BrokerOrderResult(True, order_id, symbol, "CANCEL", 0, 0, 0.0, "OK", raw={})

    def total_filled(self):
        return sum(self._view(oid)["filled_qty"] for oid in self.orders)


FAST = dict(status_retries=2, status_retry_sec=0.2, cancel_confirm_checks=5, cancel_confirm_sec=0.5)


def _run(broker, clock, *, target=1900, price=4950.0, signal_valid=lambda: True, **params):
    first = broker.place_initial(target, price)
    p = pfr.RepriceParams(**{**FAST, **params})
    r = pfr.PartialFillRepricer(broker, symbol=SYM, target_qty=target, order_price=price, first_result=first,
                                client_order_id="SIG", params=p, signal_valid=signal_valid,
                                sleep=clock.sleep, clock=clock.now).run()
    # 모든 시나리오 공통 불변식
    assert all(n == 0 for n in broker.live_at_place), "살아 있는 주문 위에 새 주문을 겹쳐 냈다"
    assert broker.total_filled() <= target
    assert r.state.FILLED_QTY == broker.total_filled() or r.final in (pfr.CANCEL_FAILED, pfr.STATUS_UNAVAILABLE)
    assert len(broker.buy_calls) <= min(p.max_reprice, pfr.HARD_MAX_REPRICE)
    return r


# ── 1. 2026-10-23 12:33 사례: 117 / 1900 ─────────────────────────────────
def test_117_of_1900_reprices_remainder_within_chase():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}, {"fills": [(1.0, 1783)]}], asks=[(0, 4950.0), (2.0, 4955.0)])
    r = _run(b, c)
    assert r.final == pfr.FILLED
    assert r.state.FILLED_QTY == 1900 and r.state.REMAINING_QTY == 0 and r.state.REPRICE_COUNT == 1
    assert b.buy_calls == [(SYM, 1783, 4955.0, "SIG:PF1")]
    assert r.state.ORIGINAL_ORDER_ID == "ORD0"
    assert r.state.avg_price == pytest.approx((117 * 4950 + 1783 * 4955) / 1900)


def test_117_of_1900_price_ran_beyond_chase_keeps_117_and_cancels_rest():
    """실제 10/23 처럼 매도호가가 4,990(+0.81%)으로 달아나면 0.3% 상한(4,960)을 넘어 잔량을 포기한다."""
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}], asks=[(0, 4950.0), (1.0, 4990.0)])
    r = _run(b, c)
    assert r.final == pfr.ABANDONED_CHASE_LIMIT
    assert r.state.FILLED_QTY == 117 and r.state.CANCEL_CONFIRMED is True
    assert b.buy_calls == [] and b.cancel_calls == ["ORD0"]


# ── 2. 전량체결 ───────────────────────────────────────────────────────────
def test_full_fill_no_cancel_no_reorder():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 1900)]}])
    r = _run(b, c)
    assert r.final == pfr.FILLED and r.state.FILLED_QTY == 1900
    assert b.buy_calls == [] and b.cancel_calls == [] and r.state.CANCEL_CONFIRMED is None


# ── 3. 0체결 ──────────────────────────────────────────────────────────────
def test_zero_fill_reprices_full_qty():
    c = Clock()
    b = FakeBroker(c, [{"fills": []}, {"fills": [(0.5, 1900)]}], asks=[(0, 4955.0)])
    r = _run(b, c)
    assert r.final == pfr.FILLED and b.buy_calls == [(SYM, 1900, 4955.0, "SIG:PF1")]


def test_zero_fill_beyond_chase_abandons_with_nothing_held():
    c = Clock()
    b = FakeBroker(c, [{"fills": []}], asks=[(0, 4970.0)])
    r = _run(b, c)
    assert r.final == pfr.ABANDONED_CHASE_LIMIT and r.state.FILLED_QTY == 0 and b.buy_calls == []


# ── 4. CANCEL_FAILED ──────────────────────────────────────────────────────
def test_cancel_unconfirmed_blocks_any_further_buy():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)], "cancel": "unconfirmed"}], asks=[(0, 4955.0)])
    r = _run(b, c)
    assert r.final == pfr.CANCEL_FAILED and r.state.CANCEL_CONFIRMED is False
    assert b.buy_calls == []
    assert r.open_order == {"order_id": "ORD0", "qty": 1900, "price": 4950.0, "filled": 117,
                            "amount": 117 * 4950.0}


def test_cancel_not_allowed_real_account_is_cancel_failed():
    """현재 실계좌는 취소 API 가 막혀 있다(cancel_v2_allowed=False) -> 재호가 0, 추가매수 0."""
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}], asks=[(0, 4955.0)], cancel_allowed=False)
    r = _run(b, c)
    assert r.final == pfr.CANCEL_FAILED and b.buy_calls == [] and b.cancel_calls == []


def test_cancel_rejected_but_already_fully_filled_is_filled():
    """취소 접수 거절이어도 상태를 본다 -- 그 사이 전량체결이면 FILLED."""
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117), (3.1, 1900)], "cancel": "reject"}], asks=[(0, 4955.0)])
    r = _run(b, c)
    assert r.final == pfr.FILLED and b.buy_calls == []


# ── 5. 취소 성공 후 재호가 ────────────────────────────────────────────────
def test_reorder_only_after_cancel_confirmed():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)], "cancel_delay": 1.2}, {"fills": [(0.5, 1783)]}],
                   asks=[(0, 4955.0)])
    r = _run(b, c)
    names = [e[1] for e in r.state.events]
    assert names.index("CANCEL_CONFIRMED") < names.index("REPRICED")
    cancel_t = next(e[0] for e in r.state.events if e[1] == "CANCEL_CONFIRMED")
    reprice_t = next(e[0] for e in r.state.events if e[1] == "REPRICED")
    assert cancel_t <= reprice_t
    assert b.orders["ORD1"]["t"] >= b.orders["ORD0"]["t"] + 3.0 + 1.2      # 첫 확인 3초 + 취소확정 1.2초 이후


# ── 6. 재호가 중 원주문 추가체결 ──────────────────────────────────────────
def test_original_fills_more_during_cancel_reorder_qty_shrinks():
    c = Clock()
    # 3초 확인 때 117, 취소 요청(3.0) ~ 확정(3.5) 사이 3.2초에 600까지 체결
    b = FakeBroker(c, [{"fills": [(0.5, 117), (3.2, 600)]}, {"fills": [(0.5, 1300)]}], asks=[(0, 4955.0)])
    r = _run(b, c)
    assert b.buy_calls[0][1] == 1300
    assert r.final == pfr.FILLED and r.state.FILLED_QTY == 1900


def test_original_fully_fills_during_cancel_no_reorder():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117), (3.2, 1900)]}], asks=[(0, 4955.0)])
    r = _run(b, c)
    assert r.final == pfr.FILLED and b.buy_calls == [] and r.state.FILLED_QTY == 1900


# ── 7·8. 중복 주문 0 / 목표수량 초과 0 (무작위 시나리오) ──────────────────
@pytest.mark.parametrize("seed", range(300))
def test_random_scenarios_never_overlap_or_overfill(seed):
    rng = random.Random(seed)
    target = rng.choice([1, 50, 117, 1900, 2540])
    plans = []
    for _ in range(4):
        fills, cum = [], 0
        for _ in range(rng.randint(0, 3)):
            cum = rng.randint(cum, target)
            fills.append((round(rng.uniform(0.1, 6.0), 2), cum))
        plans.append({"fills": sorted(fills), "cancel": rng.choice(["confirm"] * 4 + ["unconfirmed", "reject"]),
                      "cancel_delay": round(rng.uniform(0.1, 3.0), 2)})
    asks = sorted((round(rng.uniform(0, 12), 1), float(rng.choice([4945, 4950, 4955, 4960, 4965, 4990])))
                  for _ in range(4))
    asks = [(0.0, 4950.0)] + asks
    c = Clock()
    b = FakeBroker(c, plans, asks=asks)
    r = _run(b, c, target=target, accept_wait_sec=rng.choice([0.0, 2.0]))
    assert r.state.FILLED_QTY <= target
    for (_, qty, _, _) in b.buy_calls:
        assert qty >= 1
    if r.final == pfr.CANCEL_FAILED:
        assert r.open_order is not None


# ── 9. 신호 무효화 ────────────────────────────────────────────────────────
def test_signal_invalid_cancels_remainder_no_reorder():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}], asks=[(0, 4955.0)])
    r = _run(b, c, signal_valid=lambda: False)
    assert r.final == pfr.ABANDONED_SIGNAL_INVALID and r.state.FILLED_QTY == 117
    assert b.cancel_calls == ["ORD0"] and b.buy_calls == [] and r.state.CANCEL_CONFIRMED is True


def test_signal_invalidated_while_cancelling_no_reorder():
    c = Clock()
    calls = {"n": 0}

    def valid():
        calls["n"] += 1
        return calls["n"] == 1          # 취소 전에는 유효, 취소 확정 뒤 재확인 때 무효

    b = FakeBroker(c, [{"fills": [(0.5, 117)]}], asks=[(0, 4955.0)])
    r = _run(b, c, signal_valid=valid)
    assert r.final == pfr.ABANDONED_SIGNAL_INVALID and b.buy_calls == []


# ── 10. TP 근처 추격 금지 ─────────────────────────────────────────────────
def test_no_chase_near_take_profit():
    """추격 상한을 1.0% 로 넓혀도, 재호가 가격이 체결평균 대비 +0.5%(TP 1.5 - near 1.0) 이상이면 사지 않는다."""
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}], asks=[(0, 4980.0)])
    r = _run(b, c, max_chase_pct=1.0)
    assert r.final == pfr.ABANDONED_TP_NEAR and b.buy_calls == [] and r.state.FILLED_QTY == 117


def test_below_tp_near_band_still_reprices():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}, {"fills": [(0.5, 1783)]}], asks=[(0, 4970.0)])
    r = _run(b, c, max_chase_pct=1.0)                     # 4970 = +0.40% < +0.5%
    assert r.final == pfr.FILLED and b.buy_calls[0][2] == 4970.0


# ── 그 밖 ────────────────────────────────────────────────────────────────
def test_fill_ratio_at_or_above_threshold_accepts_and_cancels_rest():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 1330)]}], asks=[(0, 4955.0)])      # 1330/1900 = 70%
    r = _run(b, c)
    assert r.final == pfr.PARTIAL_ACCEPTED and r.state.FILLED_QTY == 1330 and b.buy_calls == []


def test_max_two_reprices_then_abandon():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 100)]}, {"fills": [(0.5, 100)]}, {"fills": [(0.5, 100)]}],
                   asks=[(0, 4955.0)])
    r = _run(b, c)
    assert r.final == pfr.ABANDONED_MAX_REPRICE and r.state.REPRICE_COUNT == 2
    assert [q for (_, q, _, _) in b.buy_calls] == [1800, 1700]
    assert r.state.FILLED_QTY == 300 and r.state.CANCEL_CONFIRMED is True


def test_max_reprice_is_hard_capped_at_two():
    c = Clock()
    b = FakeBroker(c, [{"fills": []}] * 10, asks=[(0, 4955.0)])
    r = _run(b, c, max_reprice=50)
    assert r.final == pfr.ABANDONED_MAX_REPRICE and len(b.buy_calls) == 2


def test_status_unavailable_does_nothing():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}], status_fail=True)
    r = _run(b, c)
    assert r.final == pfr.STATUS_UNAVAILABLE and b.buy_calls == [] and b.cancel_calls == []
    assert r.open_order["order_id"] == "ORD0"


def test_price_mode_ask_plus_tick_and_cap():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}, {"fills": [(0.5, 1783)]}], asks=[(0, 4955.0)])
    r = _run(b, c, price_mode=pfr.PRICE_ASK_PLUS_TICK)
    assert b.buy_calls[0][2] == 4960.0 and r.final == pfr.FILLED      # 4955 + 5원, 상한 4960 이내
    c2 = Clock()
    b2 = FakeBroker(c2, [{"fills": [(0.5, 117)]}], asks=[(0, 4960.0)])
    r2 = _run(b2, c2, price_mode=pfr.PRICE_ASK_PLUS_TICK)
    assert r2.final == pfr.ABANDONED_CHASE_LIMIT                      # 4965 > 4960


def test_best_limit_requires_broker_support():
    c = Clock()
    b = FakeBroker(c, [{"fills": [(0.5, 117)]}], asks=[(0, 4955.0)])
    r = _run(b, c, price_mode=pfr.PRICE_BEST_LIMIT)
    assert r.final == pfr.ABANDONED_ORDER_UNSUPPORTED and b.buy_calls == [] and r.state.CANCEL_CONFIRMED is True
    c2 = Clock()
    b2 = FakeBroker(c2, [{"fills": [(0.5, 117)]}, {"fills": [(0.5, 1783)]}], asks=[(0, 4955.0)], best_limit=True)
    r2 = _run(b2, c2, price_mode=pfr.PRICE_BEST_LIMIT)
    assert r2.final == pfr.FILLED and b2.buy_calls[0][2] == "BEST"


def test_chase_cap_is_floored_to_tick():
    c = Clock()
    b = FakeBroker(c, [{"fills": []}])
    first = b.place_initial(1900, 4950.0)
    rp = pfr.PartialFillRepricer(b, symbol=SYM, target_qty=1900, order_price=4950.0, first_result=first,
                                 client_order_id="SIG", sleep=c.sleep, clock=c.now)
    assert rp.chase_cap == 4960.0                                      # 4964.85 -> 5원 단위 내림


def test_not_wired_into_production():
    root = Path(__file__).resolve().parents[2] / "app"
    hits = [p for p in root.rglob("*.py") if p.name != "partial_fill_reprice.py"
            and "partial_fill_reprice" in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == []
