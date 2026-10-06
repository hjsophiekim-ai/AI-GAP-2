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


# ── F. 10/02 그대로: 매도 직후부터 잔고 API 전체가 실패 -> 재시도에서 매도 중복 금지 ──
def _strict_1002_setup(monkeypatch):
    """11:51:04 실사고 재현. 반전 매도 주문이 체결된 **직후부터** KIS 잔고 API 가
    EGW00215 로 실패한다(체결 확인 reconcile_position 도, 잔고 대조 get_positions 도 --
    둘 다 같은 inquire-balance 다). 매도 전 잔고 대조는 정상이었다(주문이 나갔으므로)."""
    svc, now = _market(inverse_price=5_500.0, long_price=11_000.0)
    state = _held_3slot_state(now=now, entry_price=5_555.0, qty=1802, early_tp_on=False,
                              entry_chop=False, peak=0.0)
    broker = _broker(5_500.0, entry_price=5_555.0, qty=1802)
    broker.set_quote(config.LONG_SYMBOL, 11_000.0)
    kis_down = {"on": False}
    real_sell, real_get_positions, real_reconcile = (
        broker.sell_market, broker.get_positions, broker.reconcile_position)

    def sell_then_kis_rate_limited(symbol, qty, cid):
        res = real_sell(symbol, qty, cid)
        kis_down["on"] = True                     # 11:51:04 매도 체결 직후부터 잔고 API 실패
        return res

    def get_positions():
        if kis_down["on"]:
            raise RuntimeError(EGW00215_ERR)
        return real_get_positions()

    def reconcile_position(symbol):
        if kis_down["on"]:
            raise RuntimeError(EGW00215_ERR)
        return real_reconcile(symbol)

    monkeypatch.setattr(broker, "sell_market", sell_then_kis_rate_limited)
    monkeypatch.setattr(broker, "get_positions", get_positions)
    monkeypatch.setattr(broker, "reconcile_position", reconcile_position)
    return svc, now, state, broker, kis_down


def _orders(broker, symbol, side):
    return [o for o in broker.orders if o.symbol == symbol and o.side == side]


def test_1002_retry_does_not_resell_the_inverse_and_only_buys_the_leverage(monkeypatch):
    svc, now, state, broker, kis_down = _strict_1002_setup(monkeypatch)
    sid = "20261002_114500_UP_RED:TW2_3SLOT_CONFIRM"
    stale_inverse = state.position                 # 1차 tick 이 시작될 때의 보유 스냅샷

    # ── 1차 (11:51:04): 인버스 매도 체결 -> 확인 조회 실패 -> 레버리지 BUY 보류, 신호 pending
    out1 = worker._execute_or_wait(
        broker=broker, market_data=svc, state=state, now=now, macd_snap=_snap(now),
        direction=Direction.UP_RED, signal_id=sid, signal_type="REVERSAL",
        position=stale_inverse, result=worker.TickResult(), signal_detected_at=now)
    assert out1.block_reason == order_executor.FAIL_SELL_RECONCILE_QUERY_ERROR
    assert len(_orders(broker, config.INVERSE_SYMBOL, "SELL")) == 1
    assert _orders(broker, config.LONG_SYMBOL, "BUY") == []
    assert state.pending_signal and state.pending_signal["signal_id"] == sid
    assert sid not in state.processed_signal_ids

    # ── 2차 (다음 tick): 잔고 API 회복. 호출부가 **옛 보유 스냅샷(인버스 1802)** 을
    #    넘기는 최악의 경우에도 인버스를 다시 팔면 안 된다.
    kis_down["on"] = False
    out2 = worker._retry_pending_signal(
        broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=20),
        macd_snap=_snap(now), pending_dir=Direction.UP_RED, position=stale_inverse,
        result=worker.TickResult(), bars_3m=None, default_signal_type="REVERSAL")

    assert out2 is not None and out2.final_state == SignalState.EXECUTED
    inv_sells = _orders(broker, config.INVERSE_SYMBOL, "SELL")
    assert len(inv_sells) == 1, f"인버스 매도가 중복으로 나갔다: {inv_sells!r}"
    long_buys = _orders(broker, config.LONG_SYMBOL, "BUY")
    assert len(long_buys) == 1 and long_buys[0].success, "누락된 레버리지 매수만 1건 나가야 한다"
    # 계좌: 인버스 0, 레버리지만 보유
    held = {p.symbol: p.quantity for p in broker.get_positions()}
    assert held.get(config.INVERSE_SYMBOL, 0) == 0 and held.get(config.LONG_SYMBOL, 0) > 0
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    # 원장: 인버스 매도 1건(중복 기록 없음) + 레버리지 매수 1건
    rows = ledger.load_execution_ledger()
    inv_sell_rows = [r for r in rows if r["symbol"] == config.INVERSE_SYMBOL and r["side"] == "SELL"]
    long_buy_rows = [r for r in rows if r["symbol"] == config.LONG_SYMBOL and r["side"] == "BUY"]
    assert len(inv_sell_rows) == 1, f"인버스 매도 원장 기록 {len(inv_sell_rows)}건"
    assert int(float(inv_sell_rows[0]["executed_qty"])) == 1802
    assert len(long_buy_rows) == 1
    assert sid in state.processed_signal_ids and state.pending_signal is None


def test_1002_third_attempt_is_a_no_op_after_completion(monkeypatch):
    """완료 뒤 같은 신호가 다시 들어와도(중복 tick) 아무 주문도 나가지 않는다."""
    svc, now, state, broker, kis_down = _strict_1002_setup(monkeypatch)
    sid = "20261002_114500_UP_RED:TW2_3SLOT_CONFIRM"
    stale = state.position
    worker._execute_or_wait(broker=broker, market_data=svc, state=state, now=now, macd_snap=_snap(now),
                            direction=Direction.UP_RED, signal_id=sid, signal_type="REVERSAL",
                            position=stale, result=worker.TickResult(), signal_detected_at=now)
    kis_down["on"] = False
    worker._retry_pending_signal(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=20),
                                 macd_snap=_snap(now), pending_dir=Direction.UP_RED, position=stale,
                                 result=worker.TickResult(), bars_3m=None, default_signal_type="REVERSAL")
    n_before = len(broker.orders)
    worker._execute_or_wait(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=40),
                            macd_snap=_snap(now), direction=Direction.UP_RED, signal_id=sid,
                            signal_type="REVERSAL", position=state.position, result=worker.TickResult(),
                            signal_detected_at=now)
    assert len(broker.orders) == n_before, "완료된 신호가 다시 주문을 냈다"


def test_1002_if_the_sell_did_not_actually_fill_the_retry_sells_then_buys(monkeypatch):
    """반대 경우: 매도 접수는 성공했지만 실제로는 체결되지 않았다(브로커에 인버스가 그대로).
    재시도는 잔고를 다시 맞춰 '아직 보유'를 확인하고 매도부터 다시 낸 뒤 매수한다 --
    양쪽을 동시에 들지 않는다."""
    svc, now, state, broker, kis_down = _strict_1002_setup(monkeypatch)
    sid = "20261002_114500_UP_RED:TW2_3SLOT_CONFIRM"
    from tests.macd2.fake_broker import BrokerOrderResult
    first = {"done": False}
    inner_sell = FakeBroker.sell_market.__get__(broker)

    def sell(symbol, qty, cid):
        if not first["done"]:
            first["done"] = True
            kis_down["on"] = True
            res = BrokerOrderResult(True, broker._next_order_id(), symbol, "SELL", qty, 0, 0.0, "ACCEPTED")
            broker.orders.append(res)                 # 접수만 되고 체결 안 됨
            return res
        return inner_sell(symbol, qty, cid)

    monkeypatch.setattr(broker, "sell_market", sell)
    stale = state.position
    worker._execute_or_wait(broker=broker, market_data=svc, state=state, now=now, macd_snap=_snap(now),
                            direction=Direction.UP_RED, signal_id=sid, signal_type="REVERSAL",
                            position=stale, result=worker.TickResult(), signal_detected_at=now)
    kis_down["on"] = False
    out2 = worker._retry_pending_signal(broker=broker, market_data=svc, state=state,
                                        now=now + timedelta(seconds=20), macd_snap=_snap(now),
                                        pending_dir=Direction.UP_RED, position=stale,
                                        result=worker.TickResult(), bars_3m=None, default_signal_type="REVERSAL")
    assert out2 is not None and out2.final_state == SignalState.EXECUTED
    assert len(_orders(broker, config.INVERSE_SYMBOL, "SELL")) == 2    # 미체결분을 다시 판다
    held = {p.symbol: p.quantity for p in broker.get_positions()}
    assert held.get(config.INVERSE_SYMBOL, 0) == 0 and held.get(config.LONG_SYMBOL, 0) > 0
