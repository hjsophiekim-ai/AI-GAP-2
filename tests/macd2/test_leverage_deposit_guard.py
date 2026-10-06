"""단일종목 레버리지 기본예탁금 사전점검 (2026-10-06) — 계약 고정.

배경: 2026-10-01 09:18 KIS 가 레버리지 매수를 APBK3052 로 거절
("단일종목 레버리지 주문시 기본예탁금(3천만원)이 필요합니다.(현재:28983650원)").

  * REAL + 레버리지 매수에서 매수가능 조회의 ord_psbl_cash < 3천만원이면 주문을 보내지
    않고 LEVERAGE_DEPOSIT_INSUFFICIENT 로 남긴다(BLOCKED).
  * 인버스 / MOCK / ord_psbl_cash 를 모를 때는 막지 않는다(모르면 막지 않는다).
  * KIS 가 APBK3052 로 거절하면 같은 사유 + KIS '현재' 금액 + 우리 ord_psbl_cash 를 남긴다.
  * 반전에서 매수가 막혀도 보유 포지션 매도는 그대로 나간다.
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from app.trading.macd2 import config, ledger, order_executor, worker
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot, SignalState
from tests.macd2.fake_broker import FakeBroker

LONG, INV = config.LONG_SYMBOL, config.INVERSE_SYMBOL
KIS_MSG = "단일종목 레버리지 주문시 기본예탁금(3천만원)이 필요합니다.(현재:28983650원)"


class _Broker(FakeBroker):
    """매수가능 조회 응답에 ord_psbl_cash 를 싣는 FakeBroker. mode 는 테스트가 정한다."""

    def __init__(self, *, mode="real", ord_psbl_cash=None, kis_reject=False, **kw):
        super().__init__(cash=50_000_000.0, quotes={LONG: 11_000.0, INV: 5_500.0}, **kw)
        self.mode = mode
        self._ord_psbl = ord_psbl_cash
        self._kis_reject = kis_reject

    def get_buy_sizing_quote(self, symbol, *, price, order_type="market"):
        q = super().get_buy_sizing_quote(symbol, price=price, order_type=order_type)
        raw = dict(q.raw)
        if self._ord_psbl is not None:
            raw["ord_psbl_cash"] = str(int(self._ord_psbl))
        return dataclasses.replace(q, raw=raw)

    def buy_limit(self, symbol, qty, price, client_order_id):
        if self._kis_reject and symbol == LONG:
            r = BrokerOrderResult(False, self._next_order_id(), symbol, "BUY", qty, 0, 0.0, KIS_MSG,
                                  raw={"rt_cd": "7", "msg_cd": "APBK3052", "msg1": KIS_MSG})
            self.orders.append(r)
            return r
        return super().buy_limit(symbol, qty, price, client_order_id)


def _buy(broker, direction=Direction.UP_RED, position=None):
    return order_executor.execute_signal(
        broker=broker, direction=direction, signal_id="SIG", quotes={LONG: 11_000.0, INV: 5_500.0},
        position=position, budget=10_000_000.0, reconcile_retries=1, reconcile_delay_sec=0.0)


def _buys(broker, sym):
    return [o for o in broker.orders if o.side == "BUY" and o.symbol == sym]


def test_real_leverage_buy_below_30m_is_not_sent():
    b = _Broker(ord_psbl_cash=29_600_000)
    out = _buy(b)
    assert out.final_state == SignalState.BLOCKED
    assert out.block_reason == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert out.leverage_deposit_source == order_executor.LEVERAGE_DEPOSIT_SRC_PRECHECK
    assert out.leverage_deposit_current == 29_600_000 and out.leverage_deposit_required == 30_000_000
    assert _buys(b, LONG) == [], "주문을 보내면 안 된다"
    assert "기본예탁금 부족" in out.sizing_msg1


@pytest.mark.parametrize("cash", [30_000_000, 45_000_000])
def test_real_leverage_buy_at_or_above_30m_goes_through(cash):
    b = _Broker(ord_psbl_cash=cash)
    out = _buy(b)
    assert out.final_state == SignalState.EXECUTED and len(_buys(b, LONG)) == 1
    assert out.leverage_deposit_source == ""


def test_inverse_is_never_checked():
    b = _Broker(ord_psbl_cash=1_000_000)
    out = _buy(b, direction=Direction.DOWN_BLUE)
    assert out.final_state == SignalState.EXECUTED and len(_buys(b, INV)) == 1


def test_mock_is_never_checked():
    b = _Broker(mode="mock", ord_psbl_cash=1_000_000)
    assert _buy(b).final_state == SignalState.EXECUTED


def test_unknown_ord_psbl_cash_is_not_blocked():
    b = _Broker(ord_psbl_cash=None)
    assert _buy(b).final_state == SignalState.EXECUTED


def test_precheck_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "LEVERAGE_DEPOSIT_PRECHECK_ENABLED", False)
    b = _Broker(ord_psbl_cash=1_000_000)
    assert _buy(b).final_state == SignalState.EXECUTED


def test_kis_apbk3052_rejection_is_recorded_with_both_amounts():
    """사전점검을 통과했는데(ord_psbl_cash 4천만) KIS 가 거절 -- 기준 확정용으로 둘 다 남긴다."""
    b = _Broker(ord_psbl_cash=40_000_000, kis_reject=True)
    out = _buy(b)
    assert out.final_state == SignalState.FAILED
    assert out.block_reason == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert out.leverage_deposit_source == order_executor.LEVERAGE_DEPOSIT_SRC_KIS_REJECT
    assert out.leverage_deposit_current == 28_983_650
    assert out.leverage_deposit_ord_psbl_cash == 40_000_000


def test_reversal_still_sells_the_held_inverse_when_leverage_buy_is_blocked():
    b = _Broker(ord_psbl_cash=29_000_000)
    b.buy_market(INV, 1_000, "seed")
    pos = PositionSnapshot(symbol=INV, quantity=1_000, avg_price=5_500.0)
    out = _buy(b, position=pos)
    assert out.block_reason == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert any(o.side == "SELL" and o.symbol == INV and o.success for o in b.orders)
    assert out.sell_qty_after == 0 and _buys(b, LONG) == []


def test_signal_ledger_row_carries_the_reason_and_amounts(monkeypatch):
    """worker 경로: 신호원장에 사유와 금액이 남는다(원인이 화면에서 바로 보인다)."""
    from tests.macd2.test_early_take_profit_worker import _market
    from tests.macd2.test_tw2_3slot_worker_regression import _fresh_3slot_state
    svc, now = _market(inverse_price=5_500.0, long_price=11_000.0)
    state = _fresh_3slot_state()
    state.mode = "real"
    b = _Broker(ord_psbl_cash=29_000_000)
    snap = MacdSnapshot(bar_dt=now - timedelta(minutes=3), macd=1.0, signal=0.0, hist=1.0,
                        hist_last3=(-0.5, 0.5, 1.0), completed_3m_count=100, previous_diff=-0.5,
                        current_diff=1.0, relation="ABOVE")
    res = worker.TickResult()
    out = worker._execute_or_wait(broker=b, market_data=svc, state=state, now=now, macd_snap=snap,
                                  direction=Direction.UP_RED, signal_id="LEV:TW2_3SLOT_CONFIRM",
                                  signal_type="INITIAL", position=None, result=res, signal_detected_at=now)
    assert out.block_reason == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert res.signal_dispatch_trace["leverage_deposit_source"] == "PRECHECK"
    assert res.signal_dispatch_trace["leverage_deposit_current"] == 29_000_000
    worker._record_signal_ledger(state, snap, Direction.UP_RED, "INITIAL", "LEV:TW2_3SLOT_CONFIRM",
                                 now, out, res.signal_dispatch_trace)
    rows = [r for r in ledger.load_signal_ledger(limit=0) if r.get("signal_id") == "LEV:TW2_3SLOT_CONFIRM"]
    assert rows and rows[-1]["block_reason"] == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert rows[-1]["leverage_deposit_source"] == "PRECHECK"
    assert float(rows[-1]["leverage_deposit_current"]) == 29_000_000
