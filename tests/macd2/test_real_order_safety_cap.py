"""실계좌 주문금액 한도 x SMART 사이징 충돌 수정 (2026-09-29 09:54 실사고).

사고: 예산 1,000만원, P3(=SMART 강제 ON) 오전 slot1 x1.05 -> 1,739주 @6,005
= 10,442,695원. config.yaml 의 원 단위 고정 한도(건당/종목당/일일 1,000만원)
에 걸려 KIS 호출 전에 FAILED.

수정 후 (사용자 결정 2026-09-29):
  * 원 단위 고정 상한(yaml / env REAL_MAX_* / 코드 기본값)은 주문을 막지 않는다.
  * 기본 base budget 9,000,000원 (UI 입력 시 그 값). 상한이 아니라 배수 전 기준.
  * 주문 = min(base x SMART/slot 배수(전략 일일노출 3.0 room 반영), KIS 매수가능)
  * 사용자가 UI 에서 명시한 한도(1회/일일)만 추가 상한 — 넘으면 FAILED 가 아니라
    cap, 1주도 안 되면 BLOCKED.
  * 브로커 일일 누계는 KST 날짜가 바뀌면 0 (재시작 불필요), 실패 주문은 누계
    제외, 취소된 미체결분은 누계에서 되돌린다.

KIS 는 전부 가짜(_FakeKis) — 네트워크/실원장 쓰기 없음(conftest 격리).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.config import Config
from app.trading import kis_real_broker as krb
from app.trading import real_order_limits
from app.trading.kis_real_broker import KisRealBroker
from app.trading.macd2 import config as macd2_config
from app.trading.macd2 import order_executor, position_sizing, strategy_mode
from app.trading.macd2 import time_window_3slot as t3
from app.trading.macd2.broker_adapter import _BrokerAdapterBase
from app.trading.macd2.models import Direction, RuntimeState, SignalState

PRICE_ASK1 = 5995          # 09:54 ask1 — 주문가 = ask1 + 1틱(10원) = 6,005
ORDER_PRICE = 6005
INVERSE = "0197X0"


class _FakeKis:
    """KisRealBroker 가 쓰는 KIS 표면만 흉내낸다. fill=True 면 즉시 전량 체결."""

    def __init__(self, *, cash=5_000_000_000, fill=True, accept=True):
        self.cash = cash
        self.fill = fill
        self.accept = accept
        self.buy_calls: list[tuple] = []
        self.cancel_calls: list[str] = []
        self.positions: dict[str, int] = {}

    def get_orderbook_quote(self, symbol):
        return {"ok": True, "symbol": symbol, "ask1": float(PRICE_ASK1), "rt_cd": "0", "msg_cd": "", "msg1": ""}

    def get_buyable_cash_raw(self, symbol="", price=0, ord_dvsn="00", **kw):
        qty = int(self.cash // price) if price else 0
        return {"rt_cd": "0", "msg_cd": "", "msg1": "", "nrcvb_buy_amt": float(self.cash),
                "ord_psbl_cash": float(self.cash), "nrcvb_buy_qty": qty, "psbl_qty": qty,
                "psbl_qty_calc_unpr": float(price)}

    def get_buyable_cash(self, symbol="", price=0, **kw):
        return float(self.cash)

    def buy(self, symbol, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, int(quantity), int(price), order_type))
        if not self.accept:
            return {"success": False, "order_id": "", "message": "주문가능금액을 초과했습니다",
                    "raw": {"rt_cd": "1", "msg_cd": "APBK0952", "msg1": "주문가능금액을 초과했습니다"},
                    "http_status": 200, "rt_cd": "1", "msg_cd": "APBK0952", "msg1": "주문가능금액을 초과했습니다"}
        if self.fill:
            self.positions[symbol] = self.positions.get(symbol, 0) + int(quantity)
        return {"success": True, "order_id": f"ODNO{len(self.buy_calls)}", "message": "OK", "raw": {},
                "http_status": 200, "rt_cd": "0", "msg_cd": "", "msg1": ""}

    def cancel_order(self, order_id, symbol=""):
        self.cancel_calls.append(order_id)
        return True

    def get_today_fills(self, symbol=""):
        return {"ok": True, "fills": []}

    def get_balance(self):
        return {"cash": self.cash, "positions": [
            {"symbol": s, "name": s, "quantity": q, "avg_price": float(ORDER_PRICE), "current_price": float(ORDER_PRICE)}
            for s, q in self.positions.items() if q > 0
        ]}


def _legacy_yaml_cfg(amount=10_000_000):
    """09:54 당시 config.yaml 의 고정 한도를 그대로 가진 설정 — 이제 무시돼야 한다."""
    cfg = Config()
    cfg._raw.setdefault("safety", {}).update({
        "max_order_amount": amount, "max_real_order_amount": amount,
        "max_position_amount_per_symbol": amount,
        "max_daily_order_amount": amount, "max_real_daily_budget": amount,
    })
    return cfg


def _broker(kis, cfg=None):
    # __new__: 계좌/확인문구 gate(생성자)는 이 테스트의 관심사가 아니다.
    b = KisRealBroker.__new__(KisRealBroker)
    b._cfg = cfg or _legacy_yaml_cfg()
    b.kis = kis
    b._runtime_real_mode = True
    b._runtime_enable_real_buy = True
    b._runtime_enable_real_sell = True
    b._daily_ordered_amount = 0.0
    return b


class _Adapter(_BrokerAdapterBase):
    mode = "real"

    def __init__(self, broker):
        self._broker = broker


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """사용자 한도 파일은 tmp 로, 고정한도 env 는 '설정돼 있어도 무시'를 보려고 일부러 켠다."""
    monkeypatch.setattr(real_order_limits, "override_path", lambda: tmp_path / "user_limits.json")
    monkeypatch.setenv("REAL_MAX_ORDER_AMOUNT", "10000000")
    monkeypatch.setenv("REAL_MAX_DAILY_ORDER_AMOUNT", "10000000")
    monkeypatch.setenv("REAL_MAX_POSITION_AMOUNT_PER_SYMBOL", "10000000")
    monkeypatch.delenv("AUTO_REDUCE_QUANTITY_ON_SAFETY_LIMIT", raising=False)
    monkeypatch.setattr(order_executor, "BUY_FILL_POLL_MAX_SEC", 0.0)
    monkeypatch.setattr(order_executor, "POST_CANCEL_RECHECK_DELAY_SEC", 0.0)
    monkeypatch.setattr(order_executor, "ASK1_FETCH_RETRY_DELAY_SEC", 0.0)


def _p3_state(budget):
    st = RuntimeState()
    st.budget = budget
    strategy_mode.apply(st, "P3", changed_by="test", now_iso="2026-09-29T09:00:00+09:00")
    return st


def _execute(adapter, budget, sid):
    return order_executor.execute_signal(
        broker=adapter, direction=Direction.DOWN_BLUE, signal_id=sid,
        quotes={INVERSE: float(PRICE_ASK1)}, position=None, budget=budget,
        reconcile_retries=1, reconcile_delay_sec=0.0,
    )


# ── 1. 09:54 앵커 + 고정 상한 제거 ─────────────────────────────────────────

def test_0954_anchor_reaches_kis_with_full_smart_order():
    """당시 설정(yaml/env 고정 1,000만원) 그대로여도 1,739주 전량이 KIS 에 간다."""
    kis = _FakeKis()
    out = _execute(_Adapter(_broker(kis)), 10_000_000 * 1.05, "S0954")
    assert kis.buy_calls == [(INVERSE, 1739, ORDER_PRICE, "limit")]
    assert out.final_state == SignalState.EXECUTED
    assert out.quantity == 1739
    assert out.safety_requested_amount == pytest.approx(10_442_695)
    assert out.safety_capped_qty == 1739
    assert out.safety_limit_type == ""


def test_fixed_krw_ceilings_are_ignored():
    limits = _broker(_FakeKis())._get_order_limits()
    assert limits["per_order"] == float("inf")
    assert limits["per_symbol"] == float("inf")
    assert limits["daily"] == float("inf")
    assert limits["source"] == {"per_order": "none", "daily": "none"}


def test_production_config_yaml_has_no_fixed_order_ceiling():
    safety = Config()._raw.get("safety", {})
    for key in ("max_order_amount", "max_daily_order_amount", "max_position_amount_per_symbol",
                "max_real_order_amount", "max_real_daily_budget"):
        assert key not in safety


@pytest.mark.parametrize("budget, mult, expected_qty", [
    (9_000_000, 1.05, 1565),        # 945만 x0.995 // 6,005
    (9_000_000, 0.25, 372),         # 225만
    (9_000_000, 1.50, 2236),        # 1,350만
    (100_000_000, 1.05, 17398),     # 1억 x1.05
    (100_000_000, 1.50, 24854),     # 1억 x1.5 — 고정 상한 없이 그대로
])
def test_large_budgets_are_not_blocked(budget, mult, expected_qty):
    kis = _FakeKis()
    out = _execute(_Adapter(_broker(kis)), budget * mult, f"S_BIG_{budget}_{mult}")
    assert out.final_state == SignalState.EXECUTED
    assert kis.buy_calls[-1][1] == expected_qty


def test_default_base_budget_is_9m():
    assert macd2_config.DEFAULT_BUDGET == 9_000_000
    assert RuntimeState().budget == 9_000_000


def test_kis_buying_power_still_caps():
    kis = _FakeKis(cash=3_000_000)
    out = _execute(_Adapter(_broker(kis)), 9_000_000 * 1.05, "S_CASH")
    assert kis.buy_calls[-1][1] == int(3_000_000 * 0.995 // ORDER_PRICE)
    assert out.safety_limit_type == ""


# ── 2. 슬롯 1~3 연쇄 (전략층 무변경, 3번째는 남은 전략 예산만큼) ──────────

@pytest.mark.parametrize("budget, slot3_session, expected", [
    (9_000_000, t3.SESSION_AFTERNOON, [1565, 1565, 1342]),     # 3번째 x0.90 = 810만
    (9_000_000, t3.SESSION_MORNING, [1565, 1565, 372]),        # 오전 slot3 x0.25
    (10_000_000, t3.SESSION_AFTERNOON, [1739, 1739, 1491]),    # 09:54 당일 예산
])
def test_three_slot_chain(budget, slot3_session, expected):
    kis = _FakeKis()
    adapter = _Adapter(_broker(kis))
    st = _p3_state(budget)
    sent = []
    for slot, session in ((1, t3.SESSION_MORNING), (2, t3.SESSION_MORNING), (3, slot3_session)):
        d = position_sizing.evaluate(st, entry_chop=False, slot_number=slot, session=session, toxic=False)
        out = _execute(adapter, float(st.budget) * float(d.applied), f"S_SLOT{slot}")
        assert out.final_state == SignalState.EXECUTED, (slot, out.block_reason, out.safety_reason)
        position_sizing.note_entry(st, d)
        sent.append(kis.buy_calls[-1][1])
        kis.positions.clear()                        # 다음 슬롯 전 청산된 것으로
    assert sent == expected


def test_strategy_daily_budget_exhausted_blocks_not_fails():
    """전략 일일노출 3.0 을 다 쓰면 배수 0 -> 수량 0 -> BLOCKED (KIS 미호출)."""
    kis = _FakeKis()
    st = _p3_state(9_000_000)
    st.x2lite_exposure_used_today = 3.0
    d = position_sizing.evaluate(st, entry_chop=False, slot_number=1, session=t3.SESSION_MORNING, toxic=False)
    assert d.applied == 0.0
    out = _execute(_Adapter(_broker(kis)), float(st.budget) * d.applied, "S_EXHAUSTED")
    assert out.final_state == SignalState.BLOCKED
    assert out.block_reason == order_executor.BLOCK_INSUFFICIENT_QTY
    assert kis.buy_calls == []


# ── 3. 사용자 한도(UI) 설정 시에만 cap / BLOCKED ──────────────────────────

def test_user_per_order_limit_caps_instead_of_failing():
    assert real_order_limits.save_user_limits(per_order=10_000_000, daily=None)["ok"]
    kis = _FakeKis()
    out = _execute(_Adapter(_broker(kis)), 10_000_000 * 1.05, "S_USER_CAP")
    assert out.safety_limit_type == "PER_ORDER"
    assert out.safety_requested_qty == 1739
    assert out.safety_capped_qty == 1665                     # 10,000,000 // 6,005
    assert out.safety_capped_amount == pytest.approx(9_998_325)
    assert kis.buy_calls == [(INVERSE, 1665, ORDER_PRICE, "limit")]
    assert out.final_state == SignalState.EXECUTED


def test_user_daily_limit_remaining_caps_quantity():
    assert real_order_limits.save_user_limits(per_order=None, daily=32_000_000)["ok"]
    kis = _FakeKis()
    broker = _broker(kis)
    broker._daily_ordered_amount = 30_000_000
    out = _execute(_Adapter(broker), 9_000_000, "S_DAILY_CAP")
    assert out.safety_limit_type == "DAILY"
    assert kis.buy_calls[-1][1] == 333                        # 2,000,000 // 6,005
    assert out.final_state == SignalState.EXECUTED


def test_user_daily_limit_no_room_blocks_without_kis():
    assert real_order_limits.save_user_limits(per_order=None, daily=32_000_000)["ok"]
    kis = _FakeKis()
    broker = _broker(kis)
    broker._daily_ordered_amount = 32_000_000 - 1_000
    out = _execute(_Adapter(broker), 9_000_000, "S_NO_ROOM")
    assert out.final_state == SignalState.BLOCKED
    assert out.block_reason == order_executor.BLOCK_SAFETY_LIMIT_NO_ROOM
    assert out.safety_capped_qty == 0
    assert kis.buy_calls == []


def test_clearing_user_limits_removes_cap():
    real_order_limits.save_user_limits(per_order=5_000_000, daily=None)
    real_order_limits.clear_user_limits()
    kis = _FakeKis()
    _execute(_Adapter(_broker(kis)), 10_000_000 * 1.05, "S_CLEARED")
    assert kis.buy_calls[-1][1] == 1739


def test_user_limit_validation_and_corrupt_file():
    assert not real_order_limits.save_user_limits(per_order=10_000_000, daily=5_000_000)["ok"]
    assert not real_order_limits.save_user_limits(per_order=0, daily=None)["ok"]
    assert not real_order_limits.save_user_limits(per_order=None, daily=None)["ok"]
    real_order_limits.override_path().write_text("{not json", encoding="utf-8")
    assert real_order_limits.load_user_limits() == {}
    assert _broker(_FakeKis())._get_order_limits()["per_order"] == float("inf")


def test_adapter_without_safety_room_is_unchanged():
    """MOCK/기타 브로커(get_buy_safety_room 없음)는 cap 경로를 타지 않는다."""
    class _Plain:
        mode = "mock"

    class _A(_BrokerAdapterBase):
        mode = "mock"

        def __init__(self):
            self._broker = _Plain()

    assert _A().get_buy_safety_room(INVERSE, ORDER_PRICE) is None


def test_internal_gate_rejection_is_diagnosed(monkeypatch):
    """cap 을 우회해 브로커 gate 가 거절하면(비정상) 사유가 outcome 에 남는다."""
    real_order_limits.save_user_limits(per_order=10_000_000, daily=None)
    kis = _FakeKis()
    broker = _broker(kis)
    monkeypatch.setattr(broker, "get_buy_safety_room", lambda *a, **k: None)
    out = _execute(_Adapter(broker), 10_000_000 * 1.05, "S_GATE")
    assert out.final_state == SignalState.FAILED
    assert out.broker_error_type == "safety_per_order_limit_exceeded"
    assert out.safety_limit_type == "safety_per_order_limit_exceeded"
    assert "10,442,695" in out.safety_reason
    assert kis.buy_calls == []


# ── 4. 브로커 일일 누계: 날짜 리셋 / 실패·취소 제외 ────────────────────────

def test_daily_ordered_amount_resets_on_kst_date_change(monkeypatch):
    today = {"d": date(2026, 9, 29)}
    monkeypatch.setattr(krb, "_kst_today", lambda: today["d"])
    b = _broker(_FakeKis())
    b._daily_ordered_amount = 25_000_000
    assert b._daily_ordered_amount == 25_000_000
    today["d"] = date(2026, 9, 30)
    assert b._daily_ordered_amount == 0.0
    real_order_limits.save_user_limits(per_order=None, daily=32_000_000)
    assert b.get_buy_safety_room(INVERSE, ORDER_PRICE)["daily_remaining"] == pytest.approx(32_000_000)


def test_successful_order_increments_daily_total():
    kis = _FakeKis()
    broker = _broker(kis)
    _execute(_Adapter(broker), 9_000_000, "S_OK")
    assert broker._daily_ordered_amount == pytest.approx(kis.buy_calls[-1][1] * ORDER_PRICE)


def test_failed_order_does_not_increase_daily_total():
    kis = _FakeKis(accept=False)
    broker = _broker(kis)
    out = _execute(_Adapter(broker), 9_000_000, "S_FAILED")
    assert out.final_state == SignalState.FAILED
    assert len(kis.buy_calls) == 1                   # KIS 까지는 갔다
    assert broker._daily_ordered_amount == 0.0
    assert out.broker_error_type == ""               # KIS 거절 — 내부 gate 아님


def test_cancelled_unfilled_limit_order_is_released_from_daily_total():
    kis = _FakeKis(fill=False)
    broker = _broker(kis)
    out = _execute(_Adapter(broker), 9_000_000, "S_UNFILLED")
    assert kis.cancel_calls
    assert out.final_state == SignalState.FAILED
    assert out.block_reason == order_executor.FAIL_BUY_NOT_CONFIRMED
    assert out.daily_ordered_released == pytest.approx(kis.buy_calls[-1][1] * ORDER_PRICE)
    assert broker._daily_ordered_amount == 0.0
