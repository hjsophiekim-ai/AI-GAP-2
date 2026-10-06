"""단일종목 레버리지 기본예탁금 (2026-10-06) — 계약 고정.

배경: 2026-10-01 09:18 KIS 가 레버리지 매수를 APBK3052 로 거절
("단일종목 레버리지 주문시 기본예탁금(3천만원)이 필요합니다.(현재:28983650원)").

  * APBK3052 거절은 **항상** LEVERAGE_DEPOSIT_INSUFFICIENT 로 남고, KIS 가 말한 현재
    기본예탁금 + 같은 시점 ord_psbl_cash + 주문가능금액 + 현금 필드 원값을 원장에 남긴다.
  * 사전차단(ord_psbl_cash < 3천만원이면 주문 안 보냄)은 feature flag, **기본 OFF**.
    ord_psbl_cash 가 KIS 기준과 같은지 실계좌로 확인된 뒤 켠다(사용자 결정).
  * 켜더라도 REAL + 레버리지 매수만, 값을 모르면 막지 않는다. 반전 매도는 그대로.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import timedelta

import pytest

from app.trading.macd2 import config, ledger, order_executor, worker
from app.trading.macd2.broker_adapter import BrokerOrderResult, RealBrokerAdapter
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot, SignalState
from tests.macd2.fake_broker import FakeBroker

LONG, INV = config.LONG_SYMBOL, config.INVERSE_SYMBOL
KIS_MSG = "단일종목 레버리지 주문시 기본예탁금(3천만원)이 필요합니다.(현재:28983650원)"
BAL_FIELDS = {"dnca_tot_amt": "39000000", "nxdy_excc_amt": "31000000", "prvs_rcdl_excc_amt": "28983650"}


class _Broker(FakeBroker):
    """매수가능 조회 응답에 ord_psbl_cash 를 싣는 FakeBroker. mode 는 테스트가 정한다."""

    def __init__(self, *, mode="real", ord_psbl_cash=None, kis_reject=False, **kw):
        super().__init__(cash=50_000_000.0, quotes={LONG: 11_000.0, INV: 5_500.0}, **kw)
        self.mode = mode
        self._ord_psbl = ord_psbl_cash
        self._kis_reject = kis_reject
        self.diag_calls = 0

    def get_buy_sizing_quote(self, symbol, *, price, order_type="market"):
        q = super().get_buy_sizing_quote(symbol, price=price, order_type=order_type)
        raw = dict(q.raw)
        if self._ord_psbl is not None:
            raw["ord_psbl_cash"] = str(int(self._ord_psbl))
            raw["output"] = {"ord_psbl_cash": str(int(self._ord_psbl)), "nrcvb_buy_amt": "59700000",
                             "max_buy_amt": "59700000", "ruse_psbl_amt": "30716350"}
        return dataclasses.replace(q, raw=raw)

    def get_cash_diagnostics(self):
        self.diag_calls += 1
        return {"balance_cash_fields": dict(BAL_FIELDS), "cash": 39_000_000.0}

    def buy_limit(self, symbol, qty, price, client_order_id):
        if self._kis_reject and symbol == LONG:
            r = BrokerOrderResult(False, self._next_order_id(), symbol, "BUY", qty, 0, 0.0, KIS_MSG,
                                  raw={"rt_cd": "7", "msg_cd": "APBK3052", "msg1": KIS_MSG})
            self.orders.append(r)
            return r
        return super().buy_limit(symbol, qty, price, client_order_id)


@pytest.fixture
def precheck_on(monkeypatch):
    monkeypatch.setattr(config, "LEVERAGE_DEPOSIT_PRECHECK_ENABLED", True)


def _buy(broker, direction=Direction.UP_RED, position=None):
    return order_executor.execute_signal(
        broker=broker, direction=direction, signal_id="SIG", quotes={LONG: 11_000.0, INV: 5_500.0},
        position=position, budget=10_000_000.0, reconcile_retries=1, reconcile_delay_sec=0.0)


def _buys(broker, sym):
    return [o for o in broker.orders if o.side == "BUY" and o.symbol == sym]


# ── 기본값: 사전차단 OFF ───────────────────────────────────────────────────
def test_precheck_is_off_by_default():
    assert config.LEVERAGE_DEPOSIT_PRECHECK_ENABLED is False


def test_default_off_does_not_block_a_real_leverage_buy_below_30m():
    b = _Broker(ord_psbl_cash=29_600_000)
    out = _buy(b)
    assert out.final_state == SignalState.EXECUTED and len(_buys(b, LONG)) == 1
    assert out.leverage_deposit_source == "" and b.diag_calls == 0


# ── APBK3052 기록: 플래그와 무관하게 항상 ─────────────────────────────────
@pytest.mark.parametrize("flag", [False, True])
def test_kis_apbk3052_is_recorded_with_all_cash_fields(monkeypatch, flag):
    monkeypatch.setattr(config, "LEVERAGE_DEPOSIT_PRECHECK_ENABLED", flag)
    b = _Broker(ord_psbl_cash=40_000_000, kis_reject=True)
    out = _buy(b)
    assert out.final_state == SignalState.FAILED
    assert out.block_reason == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert out.leverage_deposit_source == order_executor.LEVERAGE_DEPOSIT_SRC_KIS_REJECT
    assert out.leverage_deposit_current == 28_983_650          # KIS 가 말한 현재 기본예탁금
    assert out.leverage_deposit_ord_psbl_cash == 40_000_000     # 같은 시점 우리 값
    assert out.orderable_cash_at_sizing is not None              # 주문가능금액(기존 컬럼)
    snap = json.loads(out.leverage_deposit_cash_fields)
    assert snap["psbl_order"]["ord_psbl_cash"] == "40000000"
    assert snap["psbl_order"]["nrcvb_buy_amt"] == "59700000"
    assert snap["balance"]["balance_cash_fields"] == BAL_FIELDS
    assert b.diag_calls == 1, "잔고 진단 조회는 거절 때 1회만"


def test_ordinary_buy_failure_is_not_relabelled():
    b = _Broker(ord_psbl_cash=40_000_000)
    b.fail_next_buy = True
    out = _buy(b)
    assert out.block_reason == order_executor.FAIL_BUY and out.leverage_deposit_source == ""
    assert b.diag_calls == 0


def test_cash_diagnostics_failure_never_raises():
    class _Boom(_Broker):
        def get_cash_diagnostics(self):
            raise RuntimeError("EGW00215")
    out = _buy(_Boom(ord_psbl_cash=40_000_000, kis_reject=True))
    assert out.block_reason == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert "EGW00215" in json.loads(out.leverage_deposit_cash_fields)["balance"]["error"]


def test_adapter_cash_diagnostics_reads_balance_cash_fields():
    class _Kis:
        def get_balance(self):
            return {"cash": 39e6, "orderable_cash": 59.7e6, "positions": [], "cash_fields": dict(BAL_FIELDS)}

    class _Real:
        kis = _Kis()
    a = RealBrokerAdapter.__new__(RealBrokerAdapter)
    a._broker = _Real()
    d = a.get_cash_diagnostics()
    assert d["balance_cash_fields"] == BAL_FIELDS and d["cash"] == 39e6

    class _KisErr:
        def get_balance(self):
            raise RuntimeError("down")

    class _RealErr:
        kis = _KisErr()
    a._broker = _RealErr()
    assert "down" in a.get_cash_diagnostics()["error"]


def test_kis_balance_returns_cash_field_values():
    from app.trading import kis_client

    class _Resp:
        status_code, ok = 200, True

        def json(self):
            return {"rt_cd": "0", "msg_cd": "KIOK0000", "output1": [],
                    "output2": [{"dnca_tot_amt": "39000000", "prvs_rcdl_excc_amt": "28983650",
                                 "nxdy_excc_amt": "31000000"}]}
    c = kis_client.KISClient.__new__(kis_client.KISClient)
    c.mode, c.base_url, c.account_no, c.product_code = "real", "https://x.invalid", "12345678", "01"
    c._get = c._post = lambda url, **kw: _Resp()
    c._auth_headers = lambda tr_id: {}
    c._is_token_expired_response = lambda data: False
    out = c.get_balance()
    assert out["cash_fields"]["prvs_rcdl_excc_amt"] == "28983650"
    assert out["cash_fields"]["dnca_tot_amt"] == "39000000"


# ── 사전차단: 플래그 ON 일 때만 ───────────────────────────────────────────
def test_precheck_on_blocks_real_leverage_below_30m(precheck_on):
    b = _Broker(ord_psbl_cash=29_600_000)
    out = _buy(b)
    assert out.final_state == SignalState.BLOCKED
    assert out.block_reason == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert out.leverage_deposit_source == order_executor.LEVERAGE_DEPOSIT_SRC_PRECHECK
    assert out.leverage_deposit_current == 29_600_000 and out.leverage_deposit_required == 30_000_000
    assert _buys(b, LONG) == []


@pytest.mark.parametrize("cash", [30_000_000, 45_000_000])
def test_precheck_on_allows_at_or_above_30m(precheck_on, cash):
    b = _Broker(ord_psbl_cash=cash)
    assert _buy(b).final_state == SignalState.EXECUTED and len(_buys(b, LONG)) == 1


@pytest.mark.parametrize("kw,direction", [
    (dict(ord_psbl_cash=1_000_000), Direction.DOWN_BLUE),          # 인버스
    (dict(mode="mock", ord_psbl_cash=1_000_000), Direction.UP_RED),  # MOCK
    (dict(ord_psbl_cash=None), Direction.UP_RED),                  # 값 모름
])
def test_precheck_on_never_blocks_outside_scope(precheck_on, kw, direction):
    assert _buy(_Broker(**kw), direction=direction).final_state == SignalState.EXECUTED


def test_precheck_on_reversal_still_sells_the_held_inverse(precheck_on):
    b = _Broker(ord_psbl_cash=29_000_000)
    b.buy_market(INV, 1_000, "seed")
    out = _buy(b, position=PositionSnapshot(symbol=INV, quantity=1_000, avg_price=5_500.0))
    assert out.block_reason == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert any(o.side == "SELL" and o.symbol == INV and o.success for o in b.orders)
    assert out.sell_qty_after == 0 and _buys(b, LONG) == []


# ── worker → 신호원장: 사유와 금액·현금 필드가 화면에 남는다 ─────────────────
def test_signal_ledger_row_carries_apbk3052_reason_amounts_and_cash_fields():
    from tests.macd2.test_early_take_profit_worker import _market
    from tests.macd2.test_tw2_3slot_worker_regression import _fresh_3slot_state
    svc, now = _market(inverse_price=5_500.0, long_price=11_000.0)
    state = _fresh_3slot_state()
    state.mode = "real"
    b = _Broker(ord_psbl_cash=40_000_000, kis_reject=True)
    snap = MacdSnapshot(bar_dt=now - timedelta(minutes=3), macd=1.0, signal=0.0, hist=1.0,
                        hist_last3=(-0.5, 0.5, 1.0), completed_3m_count=100, previous_diff=-0.5,
                        current_diff=1.0, relation="ABOVE")
    res = worker.TickResult()
    sid = "LEV:TW2_3SLOT_CONFIRM"
    out = worker._execute_or_wait(broker=b, market_data=svc, state=state, now=now, macd_snap=snap,
                                  direction=Direction.UP_RED, signal_id=sid, signal_type="INITIAL",
                                  position=None, result=res, signal_detected_at=now)
    worker._record_signal_ledger(state, snap, Direction.UP_RED, "INITIAL", sid, now, out,
                                 res.signal_dispatch_trace)
    row = [r for r in ledger.load_signal_ledger(limit=0) if r.get("signal_id") == sid][-1]
    assert row["block_reason"] == order_executor.BLOCK_LEVERAGE_DEPOSIT_INSUFFICIENT
    assert row["leverage_deposit_source"] == "KIS_REJECT"
    assert float(row["leverage_deposit_current"]) == 28_983_650
    assert float(row["leverage_deposit_ord_psbl_cash"]) == 40_000_000
    assert "prvs_rcdl_excc_amt" in row["leverage_deposit_cash_fields"]
