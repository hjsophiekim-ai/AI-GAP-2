"""UI "현재 보유물량 전량매도" 비상경로 안전성 — 2026-09-21 실사고 후속.

사고 당시 이 버튼이 동작하지 않아 사용자가 KIS 앱에서 직접 팔았고, 그
시스템 밖 청산이 stale H50 사고의 출발점이 됐다. 원인은 `manual_exit` 이
`state.position` 만 보고 **브로커에 한 번도 묻지 않은** 것이다.

비상 매도 경로가 지켜야 하는 계약:
  - 브로커 실제 보유분이 권위다 (로컬 상태가 틀려도 팔 수 있어야 한다)
  - 브로커가 flat 이면 주문을 내지 않는다
  - worker 스레드가 죽어 있어도 팔 수 있어야 한다
  - auto_trade_on 과 무관하게 팔 수 있어야 한다
  - 성공하면 포지션 수명 상태를 전부 끝낸다 (다음 진입에 이월 0)
  - 실패/부분실패면 상태를 함부로 비우지 않는다
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.trading.macd2 import (
    config,
    order_executor,
    peak_protection,
    service as sv,
    small_whipsaw_hold,
    state_store,
    worker as wk,
)
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import (
    Direction, PositionSnapshot, SignalState,
)

KST = config.KST
SYM = config.LONG_SYMBOL


class _P:
    def __init__(self, symbol, quantity, avg_price):
        self.symbol = symbol
        self.quantity = quantity
        self.avg_price = avg_price


class _Broker:
    """보유분을 들고 있고, 매도 호출을 전부 기록한다."""

    def __init__(self, positions=(), *, raise_on_positions=False):
        self._positions = [_P(*p) for p in positions]
        self.raise_on_positions = raise_on_positions
        self.sell_calls = []

    def get_positions(self):
        if self.raise_on_positions:
            raise RuntimeError("KIS positions inquiry failed")
        return list(self._positions)


def _svc(broker):
    s = sv.Macd2Service()
    s._broker = broker
    s._worker = None                  # 워커 없음이 기본 — 비상경로는 이것과 무관해야 한다
    return s


def _state(*, position=None, auto_trade_on=True, epoch=1):
    st = state_store.default_state()
    st.auto_trade_on = auto_trade_on
    st.budget = 10_000_000.0
    st.mode = "mock"
    st.time_window_n1_filter_enabled = True
    st.c1_peak_protection_enabled = True
    st.position = position
    st.position_epoch = epoch
    if position is not None:
        st.time_window_position_active = True
        st.time_window_active_mode = "N1_3SLOT"
    state_store.save_state(st)
    return st


def _stub_exit(monkeypatch, *, executed=True, qty_after=0, block_reason=None):
    """order_executor.execute_exit 를 가로채 매도 호출을 기록한다."""
    seen = {"calls": []}

    def _fake(**kw):
        seen["calls"].append(kw)
        res = BrokerOrderResult(
            success=executed, order_id="TEST", symbol=kw["symbol"], side="SELL",
            requested_qty=kw["quantity"],
            executed_qty=(kw["quantity"] - qty_after if executed else 0),
            executed_price=12065.0, message="", raw={},
        )
        return order_executor.ExecutionOutcome(
            signal_id="MANUAL", direction=Direction.UP_RED,
            target_symbol=kw["symbol"],
            final_state=(SignalState.EXECUTED if executed else SignalState.FAILED),
            block_reason=block_reason,
            sell_result=res, sell_qty_after=qty_after, quantity=kw["quantity"],
        )

    monkeypatch.setattr(order_executor, "execute_exit", _fake)
    monkeypatch.setattr(sv.order_executor, "execute_exit", _fake)
    return seen


# ════════════════════════════════════════════════════════════════════════════
# 1. local/broker desync — 로컬이 flat 이어도 브로커 실제 수량 전부 매도
# ════════════════════════════════════════════════════════════════════════════
def test_1_sells_broker_quantity_when_local_state_is_flat(monkeypatch):
    seen = _stub_exit(monkeypatch)
    _state(position=None)                       # 로컬은 아무것도 없다고 알고 있음
    svc = _svc(_Broker([(SYM, 749, 11938.0)]))  # 브로커엔 749주가 있다

    res = svc.manual_exit()

    assert res["ok"] is True, res
    assert len(seen["calls"]) == 1, "매도 주문이 정확히 한 번 나가야 한다"
    assert seen["calls"][0]["symbol"] == SYM
    assert seen["calls"][0]["quantity"] == 749, "브로커 실제 수량 전부"
    assert res["quantity"] == 749


def test_1b_local_qty_is_stale_and_lower_broker_wins(monkeypatch):
    """로컬이 100주라고 알고 있어도 브로커의 749주를 판다."""
    seen = _stub_exit(monkeypatch)
    _state(position=PositionSnapshot(symbol=SYM, quantity=100, avg_price=11000.0,
                                     entry_at=datetime.now(KST)))
    svc = _svc(_Broker([(SYM, 749, 11938.0)]))

    res = svc.manual_exit()
    assert seen["calls"][0]["quantity"] == 749
    # 진입가는 로컬 값을 유지한다(손익 계산 정확도)
    assert seen["calls"][0]["entry_price"] == 11000.0


# ════════════════════════════════════════════════════════════════════════════
# 2. broker flat — 매도 주문 절대 금지
# ════════════════════════════════════════════════════════════════════════════
def test_2_broker_flat_places_no_sell_order(monkeypatch):
    seen = _stub_exit(monkeypatch)
    _state(position=None)
    svc = _svc(_Broker([]))

    res = svc.manual_exit()
    assert res["ok"] is False
    assert res["message"] == "NO_POSITION_TO_SELL"
    assert seen["calls"] == [], "브로커가 flat 이면 주문이 나가면 안 된다"


def test_2b_broker_flat_but_local_stale_still_uses_local_only_as_fallback(monkeypatch):
    """브로커 조회가 **성공**했고 flat 이면 로컬 잔재로 팔지 않는다."""
    seen = _stub_exit(monkeypatch)
    _state(position=PositionSnapshot(symbol=SYM, quantity=749, avg_price=11938.0,
                                     entry_at=datetime.now(KST)))
    svc = _svc(_Broker([]))                       # 브로커: 보유 0

    res = svc.manual_exit()
    assert seen["calls"] == [], "브로커가 0 이라고 했으면 허수 매도를 내지 않는다"
    assert res["ok"] is False and res["message"] == "NO_POSITION_TO_SELL"


def test_2c_broker_query_failure_falls_back_to_local(monkeypatch):
    """브로커 '조회 실패' 는 'flat' 과 다르다 — 로컬이 알고 있으면 판다."""
    seen = _stub_exit(monkeypatch)
    _state(position=PositionSnapshot(symbol=SYM, quantity=749, avg_price=11938.0,
                                     entry_at=datetime.now(KST)))
    svc = _svc(_Broker([], raise_on_positions=True))

    res = svc.manual_exit()
    assert res["ok"] is True
    assert seen["calls"][0]["quantity"] == 749


def test_2d_broker_query_failure_and_local_flat_reports_error(monkeypatch):
    seen = _stub_exit(monkeypatch)
    _state(position=None)
    svc = _svc(_Broker([], raise_on_positions=True))

    res = svc.manual_exit()
    assert res["ok"] is False
    assert "POSITIONS_FETCH_FAILED" in res["message"]
    assert seen["calls"] == []


# ════════════════════════════════════════════════════════════════════════════
# 3. worker 가 None / 재시작 중이어도 비상매도 가능
# ════════════════════════════════════════════════════════════════════════════
def test_3_worker_none_does_not_block_emergency_sell(monkeypatch):
    seen = _stub_exit(monkeypatch)
    _state(position=None)
    svc = _svc(_Broker([(SYM, 500, 11938.0)]))
    assert svc._worker is None

    res = svc.manual_exit()
    assert res["ok"] is True, "워커가 없어도 팔 수 있어야 한다"
    assert seen["calls"][0]["quantity"] == 500


def test_3b_dead_worker_does_not_block(monkeypatch):
    seen = _stub_exit(monkeypatch)
    _state(position=None)
    svc = _svc(_Broker([(SYM, 500, 11938.0)]))
    svc._worker = type("W", (), {"is_alive": lambda self: False})()

    res = svc.manual_exit()
    assert res["ok"] is True, "죽은 워커가 비상매도를 막으면 안 된다"


def test_3c_no_worker_gate_left_in_source():
    import inspect
    src = inspect.getsource(sv.Macd2Service.manual_exit)
    assert "WORKER_NOT_RUNNING" not in src
    assert "is_alive()" not in src


# ════════════════════════════════════════════════════════════════════════════
# 4. auto_trade_on=False 여도 매도 가능
# ════════════════════════════════════════════════════════════════════════════
def test_4_auto_trade_off_does_not_block_emergency_sell(monkeypatch):
    seen = _stub_exit(monkeypatch)
    _state(position=None, auto_trade_on=False)
    svc = _svc(_Broker([(SYM, 300, 11938.0)]))

    res = svc.manual_exit()
    assert res["ok"] is True, "자동매매 OFF 여도 보유분은 팔 수 있어야 한다"
    assert seen["calls"][0]["quantity"] == 300


def test_4b_no_auto_trade_gate_left_in_source():
    import inspect
    src = inspect.getsource(sv.Macd2Service.manual_exit)
    assert "AUTO_TRADE_OFF" not in src


# ════════════════════════════════════════════════════════════════════════════
# 5. 성공 후 상태 정리 + 다음 진입에 이월 0
# ════════════════════════════════════════════════════════════════════════════
def test_5_success_clears_every_position_scoped_state(monkeypatch):
    seen = _stub_exit(monkeypatch, executed=True, qty_after=0)
    st = _state(position=PositionSnapshot(symbol=SYM, quantity=749, avg_price=11938.0,
                                          entry_at=datetime.now(KST)))
    # 포지션 수명 상태를 잔뜩 채워 둔다
    small_whipsaw_hold.note_hold_start(
        st, held_direction=Direction.UP_RED, now=datetime.now(KST),
        decision=small_whipsaw_hold.HoldDecision(True, "UP", 0.9, True, True, False, "HOLD"))
    st.h50_owner_epoch = st.position_epoch
    peak_protection.note_peak(st, 5.5)
    st.c1_armed = True
    st.c1_owner_epoch = st.position_epoch
    st.whipsaw_watch_active = True
    st.n1_last_eval_bar_ts = datetime.now(KST).isoformat()
    st.time_window_tp1_done = True
    state_store.save_state(st)

    svc = _svc(_Broker([(SYM, 749, 11938.0)]))
    res = svc.manual_exit()
    assert res["ok"] is True

    after = state_store.load_state()
    assert after.position is None, "로컬 포지션 정리"
    assert small_whipsaw_hold.is_holding(after) is False, "H50 clear"
    assert after.h50_hold_started_at is None and after.h50_owner_epoch == 0
    assert after.c1_armed is False and after.c1_peak_net_return == 0.0
    assert after.c1_owner_epoch == 0, "C1 clear"
    assert after.whipsaw_watch_active is False, "whipsaw-watch clear"
    assert after.n1_last_eval_bar_ts is None, "N1 clear"
    assert after.time_window_position_active is False
    assert after.time_window_tp1_done is False


def test_5b_next_entry_inherits_nothing(monkeypatch):
    """매도 직후 신규 진입을 열어도 이전 상태가 한 줄도 넘어오지 않는다."""
    _stub_exit(monkeypatch, executed=True, qty_after=0)
    st = _state(position=PositionSnapshot(symbol=SYM, quantity=749, avg_price=11938.0,
                                          entry_at=datetime.now(KST)))
    small_whipsaw_hold.note_hold_start(
        st, held_direction=Direction.UP_RED, now=datetime.now(KST),
        decision=small_whipsaw_hold.HoldDecision(True, "UP", 0.9, True, True, False, "HOLD"))
    st.h50_owner_epoch = st.position_epoch
    state_store.save_state(st)
    _svc(_Broker([(SYM, 749, 11938.0)])).manual_exit()

    after = state_store.load_state()
    before_epoch = after.position_epoch
    wk._begin_position_epoch(after, reason="NEW_ENTRY")
    after.position = PositionSnapshot(symbol=SYM, quantity=740, avg_price=12085.0,
                                      entry_at=datetime.now(KST))
    assert after.position_epoch == before_epoch + 1
    assert small_whipsaw_hold.is_holding(after) is False
    assert after.h50_owner_epoch == 0 and after.c1_owner_epoch == 0


# ════════════════════════════════════════════════════════════════════════════
# 6. 주문 실패 / 부분 실패
# ════════════════════════════════════════════════════════════════════════════
def test_6_failed_sell_reports_failure_and_keeps_position(monkeypatch):
    seen = _stub_exit(monkeypatch, executed=False, qty_after=749,
                      block_reason="BROKER_REJECTED")
    _state(position=PositionSnapshot(symbol=SYM, quantity=749, avg_price=11938.0,
                                     entry_at=datetime.now(KST)))
    svc = _svc(_Broker([(SYM, 749, 11938.0)]))

    res = svc.manual_exit()
    assert res["ok"] is False
    assert res["block_reason"] == "BROKER_REJECTED"
    after = state_store.load_state()
    assert after.position is not None, "실패했는데 포지션을 지우면 안 된다"
    assert after.position.quantity == 749
    assert after.order_block_reason == "BROKER_REJECTED"


def test_6b_partial_fill_is_not_treated_as_a_full_exit(monkeypatch):
    """잔량이 남은 부분 체결은 '전량 청산' 으로 처리되지 않는다.

    `order_executor.execute_exit` 는 보유가 0 으로 정리된 것을 확인한 뒤에만
    EXECUTED 를 돌려준다(docstring: "reconciles the holding to 0 before
    recording it"). 따라서 잔량이 남은 체결은 EXECUTED 가 아니고,
    `_apply_exit_outcome` 의 전량청산 정리 경로를 타지 않아야 한다."""
    _stub_exit(monkeypatch, executed=False, qty_after=200,
               block_reason="PARTIAL_FILL_REMAINING")
    st = _state(position=PositionSnapshot(symbol=SYM, quantity=749, avg_price=11938.0,
                                          entry_at=datetime.now(KST)))
    small_whipsaw_hold.note_hold_start(
        st, held_direction=Direction.UP_RED, now=datetime.now(KST),
        decision=small_whipsaw_hold.HoldDecision(True, "UP", 0.9, True, True, False, "HOLD"))
    st.h50_owner_epoch = st.position_epoch
    state_store.save_state(st)
    svc = _svc(_Broker([(SYM, 749, 11938.0)]))

    res = svc.manual_exit()
    assert res["ok"] is False
    after = state_store.load_state()
    assert after.position is not None, "부분체결로 포지션을 잃어버리면 안 된다"
    assert after.position.quantity == 749
    assert small_whipsaw_hold.is_holding(after) is True,         "전량청산이 아니면 position-scoped 상태를 끝내지 않는다"
    assert after.order_block_reason == "PARTIAL_FILL_REMAINING"


# ════════════════════════════════════════════════════════════════════════════
# 정상 보유 상태 (로컬/브로커 일치) — 기존 동작 보존
# ════════════════════════════════════════════════════════════════════════════
def test_normal_in_sync_holding_still_sells_everything(monkeypatch):
    seen = _stub_exit(monkeypatch)
    _state(position=PositionSnapshot(symbol=SYM, quantity=749, avg_price=11938.0,
                                     entry_at=datetime.now(KST)))
    svc = _svc(_Broker([(SYM, 749, 11938.0)]))

    res = svc.manual_exit()
    assert res["ok"] is True
    assert seen["calls"][0]["quantity"] == 749
    assert seen["calls"][0]["exit_reason"] == config.EXIT_MANUAL_LIQUIDATION


def test_broker_not_started_is_reported(monkeypatch):
    _stub_exit(monkeypatch)
    _state(position=None)
    svc = sv.Macd2Service()
    svc._broker = None
    assert svc.manual_exit()["message"] == "NOT_STARTED"
