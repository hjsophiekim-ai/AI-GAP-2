"""SELL 실제 체결 계측 (2026-10-30) — 기록 전용.

시장가 SELL 의 주문응답엔 체결가가 없어서 원장 price 는 매도 직후 현재가(fallback)였다.
이 테스트들은 (1) 기록 순간의 가격·손익은 예전과 똑같고 price_source 로 출처가 구분되는지,
(2) worker 루프의 지연 조회가 실제 평균체결가로 바꾸는지, (3) 조회 실패/모의 0행/수량 불일치는
가격을 건드리지 않고 매도 흐름도 막지 않는지를 본다.
"""
from __future__ import annotations

from typing import Optional

import pytest

from app.models import Position
from app.trading.macd2 import config, ledger, order_executor, sell_fill_telemetry as sft
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import Direction, PositionSnapshot, SignalState
from tests.macd2.fake_broker import FakeBroker

SYM = config.LONG_SYMBOL


class KisLikeBroker(FakeBroker):
    """KIS 시장가 매도처럼 주문응답 체결가 0 + ORD_TMD 만 주는 브로커. 주문조회 결과는 테스트가 정한다."""

    def __init__(self, *, status: Optional[dict] = None, raise_on_status: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.status = status
        self.raise_on_status = raise_on_status
        self.status_calls: list[tuple] = []

    def sell_market(self, symbol, qty, client_order_id):
        res = super().sell_market(symbol, qty, client_order_id)
        return BrokerOrderResult(res.success, res.order_id, res.symbol, "SELL", res.requested_qty,
                                 res.executed_qty, 0.0, res.message, raw={"ORD_TMD": "091502", "ODNO": res.order_id})

    def get_order_status(self, order_id, symbol="", trading_date=""):
        self.status_calls.append((order_id, symbol, trading_date))
        if self.raise_on_status:
            raise RuntimeError("EGW00215 초당 거래건수 초과")
        return dict(self.status or {})


def _seed(broker, qty=1300, avg=14_000.0):
    broker._positions[SYM] = Position(symbol=SYM, name=SYM, quantity=qty, avg_price=avg, current_price=avg)


def _sell_row():
    rows = [r for r in ledger.load_execution_ledger() if r["side"] == "SELL"]
    assert len(rows) == 1
    return rows[0]


def _found(qty, avg, amount=None, **extra):
    return {"ok": True, "found": True, "filled_qty": qty, "remaining_qty": 0, "avg_price": avg,
            "amount": amount if amount is not None else avg * qty, "ord_tmd": "091502",
            "fill_rows": [{"qty": qty, "avg_price": avg, "amount": amount or avg * qty,
                           "times": {"ord_tmd": "091502", "infm_tmd": "091503"}}], **extra}


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(sft, "FIRST_LOOKUP_DELAY_SEC", 0.0)
    monkeypatch.setattr(sft, "RETRY_INTERVAL_SEC", 0.0)


def _exit(broker):
    return order_executor.execute_exit(broker=broker, symbol=SYM, quantity=1300,
                                       exit_reason="TIME_WINDOW_STOP_LOSS", entry_price=14_000.0)


def test_record_time_row_keeps_legacy_price_and_marks_fallback_quote():
    broker = KisLikeBroker(quotes={SYM: 13_820.0}, status=_found(1300, 13_800.0))
    _seed(broker)
    out = _exit(broker)
    assert out.final_state == SignalState.EXECUTED
    row = _sell_row()
    # 가격·손익은 예전 식(주문응답 0 -> 현재가) 그대로
    assert float(row["executed_price"]) == 13_820.0
    assert row["price_source"] == sft.PRICE_SOURCE_FALLBACK_QUOTE
    assert float(row["fallback_quote_price"]) == 13_820.0
    assert float(row["fallback_net_pnl"]) == float(row["net_pnl"])
    assert row["kis_ord_tmd"] == "091502"
    assert row["sell_requested_at"] and row["sell_confirmed_at"] and row["sell_reconciled_at"]
    assert row["sell_requested_at"] <= row["sell_confirmed_at"] <= row["sell_reconciled_at"]
    assert row["fill_lookup_status"] == sft.LOOKUP_PENDING
    # 매도 흐름 안에서는 주문조회를 하지 않는다(반전 BUY 등을 기다리게 하지 않음)
    assert broker.status_calls == []
    assert [p["order_id"] for p in sft.pending_snapshot()] == [row["order_id"]]


def test_deferred_lookup_rewrites_price_with_actual_average_fill(no_delay):
    # 분할체결 2건(800주 13,790 + 500주 13,815) 의 누적: 금액이 truth
    amount = 800 * 13_790.0 + 500 * 13_815.0
    broker = KisLikeBroker(quotes={SYM: 13_820.0}, status=_found(1300, 13_799.62, amount=amount))
    _seed(broker)
    _exit(broker)
    before = _sell_row()
    handled = sft.process_pending(broker)
    assert handled == [before["order_id"]]
    row = _sell_row()
    assert row["price_source"] == sft.PRICE_SOURCE_ACTUAL_FILL
    assert float(row["executed_price"]) == pytest.approx(amount / 1300, abs=1e-3)
    assert float(row["fill_total_qty"]) == 1300 and float(row["fill_amount"]) == amount
    assert row["fill_granularity"] == sft.GRANULARITY_ORDER_AGGREGATE
    assert row["fill_first_at"].endswith("09:15:03+09:00") and row["fill_last_at"] == row["fill_first_at"]
    assert row["fill_lookup_status"] == "OK"
    # fallback 값은 보존 -> 실제 vs fallback 비교 가능
    assert float(row["fallback_quote_price"]) == 13_820.0
    assert float(row["fallback_net_pnl"]) == float(before["net_pnl"])
    assert float(row["net_pnl"]) < float(before["net_pnl"])  # 실제 체결이 fallback 보다 낮았다
    # 다른 컬럼은 그대로
    for col in ("order_id", "timestamp", "executed_qty", "exit_reason", "position_before", "position_after"):
        assert row[col] == before[col]
    assert sft.pending_snapshot() == []
    # 멱등: 한 번 더 패치해도 바뀌지 않는다
    assert ledger.record_sell_fill_fields(row["order_id"], {"executed_price": 1.0}) is True
    assert _sell_row()["executed_price"] == row["executed_price"]


def test_lookup_error_never_raises_keeps_fallback_and_gives_up(no_delay):
    broker = KisLikeBroker(quotes={SYM: 13_820.0}, raise_on_status=True)
    _seed(broker)
    out = _exit(broker)
    assert out.final_state == SignalState.EXECUTED
    for _ in range(sft.MAX_ATTEMPTS - 1):
        sft.process_pending(broker)
        row = _sell_row()
        assert row["price_source"] == sft.PRICE_SOURCE_FALLBACK_QUOTE
        assert row["fill_lookup_status"].startswith("LOOKUP_ERROR:")
        assert len(sft.pending_snapshot()) == 1
    sft.process_pending(broker)
    row = _sell_row()
    assert row["fill_lookup_status"].startswith("GAVE_UP:LOOKUP_ERROR")
    assert float(row["executed_price"]) == 13_820.0
    assert sft.pending_snapshot() == []


def test_mock_account_zero_rows_stays_fallback(no_delay):
    """KIS 모의(VTTC8001R)는 주문별 행이 항상 0 -> ORDER_NOT_FOUND, 가격 불변."""
    broker = KisLikeBroker(quotes={SYM: 13_820.0}, status={"ok": True, "found": False})
    _seed(broker)
    _exit(broker)
    sft.process_pending(broker)
    row = _sell_row()
    assert row["price_source"] == sft.PRICE_SOURCE_FALLBACK_QUOTE
    assert row["fill_lookup_status"] == "ORDER_NOT_FOUND"
    assert float(row["executed_price"]) == 13_820.0


def test_qty_mismatch_does_not_touch_price(no_delay):
    broker = KisLikeBroker(quotes={SYM: 13_820.0}, status=_found(1200, 13_700.0))
    _seed(broker)
    _exit(broker)
    sft.process_pending(broker)
    row = _sell_row()
    assert row["price_source"] == sft.PRICE_SOURCE_FALLBACK_QUOTE
    assert row["fill_lookup_status"] == "QTY_MISMATCH:kis=1200,ledger=1300"
    assert float(row["executed_price"]) == 13_820.0
    assert float(row["fill_avg_price"]) == 13_700.0   # 관측값은 남긴다
    assert sft.pending_snapshot() == []


def test_not_due_yet_is_not_queried():
    broker = KisLikeBroker(quotes={SYM: 13_820.0}, status=_found(1300, 13_800.0))
    _seed(broker)
    _exit(broker)
    assert sft.process_pending(broker) == []        # FIRST_LOOKUP_DELAY_SEC 전
    assert broker.status_calls == []


def test_order_response_price_and_entry_fallback_sources():
    # FakeBroker 는 주문응답에 체결가를 준다 -> ORDER_RESPONSE, 가격 그대로(리플레이 parity)
    fb = FakeBroker(quotes={SYM: 15_000.0})
    _seed(fb, qty=10)
    order_executor.execute_exit(broker=fb, symbol=SYM, quantity=10, exit_reason="X", entry_price=14_000.0)
    row = _sell_row()
    assert row["price_source"] == sft.PRICE_SOURCE_ORDER_RESPONSE and float(row["executed_price"]) == 15_000.0
    # 주문응답 0 + 현재가 없음 -> 진입가(기존 last resort) + FALLBACK_ENTRY_PRICE
    price, tele = order_executor._sell_price_with_source(
        BrokerOrderResult(True, "1", SYM, "SELL", 1, 1, 0.0, "OK"), FakeBroker(quotes={}), SYM, 14_000.0)
    assert price == 14_000.0 and tele["price_source"] == sft.PRICE_SOURCE_FALLBACK_ENTRY


def test_reversal_sell_leg_carries_telemetry():
    broker = KisLikeBroker(quotes={SYM: 13_820.0, config.INVERSE_SYMBOL: 5_000.0})
    _seed(broker, qty=100)
    order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="SIG-REV-1",
        quotes={SYM: 13_820.0, config.INVERSE_SYMBOL: 5_000.0},
        position=PositionSnapshot(symbol=SYM, quantity=100, avg_price=14_000.0),
        budget=1_000_000.0,
    )
    row = _sell_row()
    assert row["signal_id"] == "SIG-REV-1"
    assert row["price_source"] == sft.PRICE_SOURCE_FALLBACK_QUOTE
    assert row["kis_ord_tmd"] == "091502" and row["sell_reconciled_at"]
    assert broker.status_calls == []


def test_buy_rows_have_no_sell_telemetry():
    fb = FakeBroker(quotes={SYM: 15_000.0})
    order_executor._record_leg(
        broker_mode="mock", signal_id="S", symbol=SYM, side="BUY", qty=1, price=15_000.0,
        position_before=0, position_after=1, exit_reason="",
        order_result=BrokerOrderResult(True, "B1", SYM, "BUY", 1, 1, 15_000.0, "OK"),
        entry_price=15_000.0, confirmed_at="2026-10-30T09:00:00+09:00",
    )
    row = ledger.load_execution_ledger()[0]
    assert all(row[c] == "" for c in ledger.SELL_FILL_LEDGER_COLUMNS)
    assert sft.pending_snapshot() == []
    del fb


def test_kis_client_get_order_status_exposes_ord_tmd_and_fill_rows(monkeypatch):
    from app.trading import kis_client as kc

    class _Resp:
        ok = True
        status_code = 200

        def json(self):
            return {"rt_cd": "0", "output1": [{
                "odno": "0000012345", "pdno": "0193T0", "ord_qty": "1300", "tot_ccld_qty": "1300",
                "rmn_qty": "0", "avg_prvs": "13799.62", "tot_ccld_amt": "17939500", "ord_unpr": "0",
                "ord_tmd": "091502", "infm_tmd": "091503", "cncl_yn": "N"}]}

    client = kc.KISClient.__new__(kc.KISClient)
    client.mode, client.base_url, client.account_no, client.product_code = "real", "https://x", "1", "01"
    monkeypatch.setattr(client, "_request_with_token_retry", lambda *a, **k: _Resp(), raising=False)
    st = client.get_order_status("12345", symbol="0193T0", trading_date="20261029")
    assert st["found"] and st["filled_qty"] == 1300 and st["ord_tmd"] == "091502"
    assert st["fill_rows"][0]["times"] == {"ord_tmd": "091502", "infm_tmd": "091503"}
    assert st["fill_rows"][0]["amount"] == 17_939_500.0
