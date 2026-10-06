"""2026-10-02 실사고 회귀: 11:45 RED -> 11:48 T+3 반전이 잔고조회 초당한도로 사라짐.

원장: ``RESOLVE_ERROR / RuntimeError: KIS 실계좌 잔고 조회 실패: HTTP 500
msg_cd=EGW00215: 원장에서 허용 가능한 초당 거래건수를 초과하였습니다.``

원인 두 겹:
  1. kis_client 의 초당한도 재시도 대상이 EGW00201 하나뿐이라 EGW00215 는 재시도
     없이 즉시 실패했다.
  2. 반전(execute_signal) 은 보유 ETF 를 **먼저 매도**한 뒤 체결 확인용 잔고조회
     (reconcile_position)를 하는데, 그 조회의 예외가 그대로 밖으로 튀어 이미 나간
     매도의 원장 기록·반대 BUY·T+3 후보를 통째로 날렸다(RESOLVE_ERROR -> processed).

잠그는 것:
  A. EGW00215 도 초당한도로 보고 재시도한다(조회·주문 공통).
  B. 매도 후 확인 조회가 일시적으로 실패해도 같은 tick 안에서 회복하면 반전이 끝난다.
  C. 확인 조회가 끝내 실패해도 예외가 나가지 않고, 반대 BUY 는 내지 않는다.
  D. worker 는 그 신호를 버리지 않고 pending 으로 남겨, 다음 시도에서 잔고를 다시
     맞춘 뒤(매도 레그 기록) 반대 BUY 를 낸다.
  E. 3-SLOT 진입 문맥이 재시도에도 실린다(재시도 체결이 최초 체결과 같은 후처리).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading import kis_client
from app.trading.macd2 import config, ledger, order_executor, worker
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot, SignalState
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_early_take_profit_worker import _broker, _held_3slot_state, _market

EGW00215_ERR = ("KIS 실계좌 잔고 조회 실패: HTTP 500 msg_cd=EGW00215: "
                "원장에서 허용 가능한 초당 거래건수를 초과하였습니다.")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(order_executor, "POST_CANCEL_RECHECK_DELAY_SEC", 0.0)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)
    monkeypatch.setattr(worker, "POSITION_DATA_ERROR_RETRY_DELAY_SEC", 0.0)
    monkeypatch.setattr(kis_client.time, "sleep", lambda s: None)


# ── A. kis_client ─────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = status < 400
        self._body = body

    def json(self):
        return self._body


def _client(responses):
    c = kis_client.KISClient.__new__(kis_client.KISClient)
    c.mode = "real"
    c.base_url = "https://example.invalid"
    c.account_no = "12345678"
    c.product_code = "01"
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return responses[min(calls["n"], len(responses)) - 1]

    c._get = fake_get
    c._post = fake_get
    c._auth_headers = lambda tr_id: {}
    c._is_token_expired_response = lambda data: False
    return c, calls


def test_egw00215_is_treated_as_rate_limited():
    assert kis_client._is_rate_limited_response({"msg_cd": "EGW00215"})
    assert kis_client._is_rate_limited_response({"msg_cd": "EGW00201"})
    assert not kis_client._is_rate_limited_response({"msg_cd": "APBK0013"})


def test_balance_query_retries_through_egw00215():
    limited = _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00215",
                          "msg1": "원장에서 허용 가능한 초당 거래건수를 초과하였습니다."})
    ok = _Resp(200, {"rt_cd": "0", "msg_cd": "KIOK0000", "output1": [],
                     "output2": [{"dnca_tot_amt": "29000000", "ord_psbl_cash": "29000000"}]})
    c, calls = _client([limited, ok])
    out = c.get_balance()
    assert calls["n"] == 2, "EGW00215 한 번이면 재시도해서 성공해야 한다"
    assert "error" not in out


# ── B/C. order_executor ──────────────────────────────────────────────────
def _reversal(broker, position, **kw):
    return order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="20261002_114500_UP_RED:TW2_3SLOT_CONFIRM",
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
        position=position, budget=10_000_000.0, reconcile_retries=2, reconcile_delay_sec=0.0, **kw)


def _held_inverse_broker():
    b = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    b.buy_market(config.INVERSE_SYMBOL, 20, "seed")
    return b, PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=20, avg_price=10_000.0)


def test_transient_query_failure_after_sell_still_completes_reversal(monkeypatch):
    broker, pos = _held_inverse_broker()
    real = broker.reconcile_position
    n = {"i": 0}

    def flaky(symbol):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError(EGW00215_ERR)
        return real(symbol)

    monkeypatch.setattr(broker, "reconcile_position", flaky)
    out = _reversal(broker, pos)
    assert out.final_state == SignalState.EXECUTED
    sides = [(r["symbol"], r["side"]) for r in ledger.load_execution_ledger()]
    assert (config.INVERSE_SYMBOL, "SELL") in sides and (config.LONG_SYMBOL, "BUY") in sides


def test_persistent_query_failure_after_sell_never_raises_and_never_buys(monkeypatch):
    broker, pos = _held_inverse_broker()
    monkeypatch.setattr(broker, "reconcile_position",
                        lambda symbol: (_ for _ in ()).throw(RuntimeError(EGW00215_ERR)))
    out = _reversal(broker, pos)                       # 예외가 나가면 이 줄에서 실패한다
    assert out.final_state == SignalState.FAILED
    assert out.block_reason == order_executor.FAIL_SELL_RECONCILE_QUERY_ERROR
    assert out.sell_result is not None and out.sell_result.success
    assert out.sell_qty_after == order_executor.RECONCILE_QTY_UNKNOWN
    assert not out.timestamps.get("buy_requested_at"), "체결 미확인 상태에서 반대 BUY 금지"
    assert not any(o.side == "BUY" and o.symbol == config.LONG_SYMBOL for o in broker.orders)


def test_exit_with_persistent_query_failure_does_not_raise(monkeypatch):
    broker, pos = _held_inverse_broker()
    monkeypatch.setattr(broker, "reconcile_position",
                        lambda symbol: (_ for _ in ()).throw(RuntimeError(EGW00215_ERR)))
    out = order_executor.execute_exit(
        broker=broker, symbol=config.INVERSE_SYMBOL, quantity=20, exit_reason=config.EXIT_OPPOSITE_SIGNAL,
        entry_price=10_000.0, reconcile_retries=2, reconcile_delay_sec=0.0)
    assert out.final_state == SignalState.FAILED
    assert out.block_reason == order_executor.FAIL_SELL_RECONCILE_QUERY_ERROR


def test_confirmed_qty_is_not_overwritten_by_a_later_query_failure(monkeypatch):
    """확인된 '안 팔렸다(20)' 뒤에 조회 실패가 와도 '모른다'로 덮지 않는다."""
    broker, pos = _held_inverse_broker()
    seq = iter([20, RuntimeError(EGW00215_ERR)] * 10)

    def mixed(symbol):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(broker, "reconcile_position", mixed)
    assert order_executor._reconcile_to_zero(broker, config.INVERSE_SYMBOL, retries=2, delay_sec=0.0) == 20


# ── D/E. worker ──────────────────────────────────────────────────────────
def _snap(now):
    return MacdSnapshot(bar_dt=now - timedelta(minutes=3), macd=1.0, signal=0.0, hist=1.0,
                        hist_last3=(-0.5, 0.5, 1.0), completed_3m_count=100, previous_diff=-0.5,
                        current_diff=1.0, relation="ABOVE")


def test_worker_keeps_the_reversal_pending_and_completes_it_on_retry(monkeypatch):
    svc, now = _market(inverse_price=10_000.0, long_price=15_000.0)
    state = _held_3slot_state(now=now, entry_price=10_000.0, qty=10, early_tp_on=False,
                              entry_chop=False, peak=0.0)
    broker = _broker(10_000.0, entry_price=10_000.0, qty=10)
    sid = "20261002_114500_UP_RED:TW2_3SLOT_CONFIRM"
    real = broker.reconcile_position
    failing = {"on": True}

    def reconcile(symbol):
        if failing["on"]:
            raise RuntimeError(EGW00215_ERR)
        return real(symbol)

    monkeypatch.setattr(broker, "reconcile_position", reconcile)

    # 1차: 매도는 나가지만 확인 조회가 전부 실패 -> 예외 없음, pending 유지, processed 아님
    out1 = worker._execute_or_wait(
        broker=broker, market_data=svc, state=state, now=now, macd_snap=_snap(now),
        direction=Direction.UP_RED, signal_id=sid, signal_type="REVERSAL",
        position=state.position, result=worker.TickResult(), signal_detected_at=now)
    assert out1 is not None and out1.block_reason == order_executor.FAIL_SELL_RECONCILE_QUERY_ERROR
    assert state.pending_signal and state.pending_signal["signal_id"] == sid
    assert sid not in state.processed_signal_ids, "반전 신호를 소비하면 10/02 와 똑같이 사라진다"
    assert broker.reconcile_position.__name__ == "reconcile"
    assert not any(o.side == "BUY" and o.symbol == config.LONG_SYMBOL for o in broker.orders)

    # 2차: 조회 회복 -> 잔고 재대조(인버스 매도 레그 기록) -> 레버리지 BUY
    failing["on"] = False
    out2 = worker._retry_pending_signal(
        broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=20),
        macd_snap=_snap(now), pending_dir=Direction.UP_RED, position=state.position,
        result=worker.TickResult(), bars_3m=None, default_signal_type="REVERSAL")
    assert out2 is not None and out2.final_state == SignalState.EXECUTED
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert sid in state.processed_signal_ids
    rows = ledger.load_execution_ledger()
    assert any(r["symbol"] == config.INVERSE_SYMBOL and r["side"] == "SELL" for r in rows), \
        "이미 나간 인버스 매도가 원장에 남아야 한다"
    assert any(r["symbol"] == config.LONG_SYMBOL and r["side"] == "BUY" for r in rows)


def test_retry_keeps_the_3slot_context_even_when_the_retry_fails_again(monkeypatch):
    state = _held_3slot_state(now=_market()[1], early_tp_on=False, entry_chop=False, peak=0.0)
    sid = "S:TW2_3SLOT_CONFIRM"
    ctx = {"session": "MORNING", "sizing": None, "presized_chop": None,
           "signal_detected_at": "2026-10-02T11:48:00+09:00", "flag_bar_dt": None}
    state.pending_signal = {"signal_id": sid, "direction": "UP_RED", "signal_type": "REVERSAL",
                            "detected_at": "2026-10-02T11:48:00+09:00", "tw2_3slot_ctx": dict(ctx)}

    def fake_eow(**kw):
        state.pending_signal = {"signal_id": sid, "direction": "UP_RED", "signal_type": "REVERSAL",
                                "detected_at": "2026-10-02T11:48:00+09:00"}
        return order_executor.ExecutionOutcome(
            sid, Direction.UP_RED, config.LONG_SYMBOL, SignalState.FAILED,
            block_reason=order_executor.FAIL_SELL_RECONCILE_QUERY_ERROR)

    monkeypatch.setattr(worker, "_execute_or_wait", fake_eow)
    monkeypatch.setattr(worker, "_record_signal_ledger", lambda *a, **k: None)
    worker._retry_pending_signal(
        broker=None, market_data=None, state=state, now=_market()[1], macd_snap=None,
        pending_dir=Direction.UP_RED, position=state.position, result=worker.TickResult(),
        bars_3m=None, default_signal_type="REVERSAL")
    assert state.pending_signal.get("tw2_3slot_ctx") == ctx
