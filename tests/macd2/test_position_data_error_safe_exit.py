"""2026-10-01 실사고 회귀 — POSITION_DATA_ERROR 때문에 보유 인버스의 청산/반대전환이 막힌 문제.

실사고: 인버스 1,754주 @5,940 보유 -> 11:06 RED -> 11:12 T+3 승인 -> 주문 직전 잔고조회 실패
-> 신호원장 WAITING / POSITION_DATA_ERROR -> pending(30초) 만료 -> 11:24 손절 @5,830 (-193,691원).

원칙: 잔고가 불확실하면 신규 BUY 는 막되, 보유가 확실한 기존 포지션의 EXIT 는 막지 않는다.
전부 실제 ``worker.run_once()`` 경로(fake broker, conftest 격리)를 탄다.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.models import Direction, PositionSnapshot
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (  # noqa: F401  (fixture)
    _PRIOR_DAY, _approved, _fresh_3slot_state, _patch_common, _prime_3slot_pending, _quality,
    tw2_3slot_market_data,
)

INV, LONG = config.INVERSE_SYMBOL, config.LONG_SYMBOL
QTY, AVG = 1754, 10_000.0         # 오늘 보유 수량 (가격은 테스트 시장 데이터 기준 10,000)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(worker, "POSITION_DATA_ERROR_RETRY_DELAY_SEC", 0.0)


def _held_inverse(svc_now, monkeypatch):
    svc, now0 = svc_now
    state = _fresh_3slot_state()
    state.tw2_3slot_slots_used_today = 1
    state.tw2_3slot_morning_count = 1
    state.position = PositionSnapshot(symbol=INV, quantity=QTY, avg_price=AVG, entry_at=now0 - timedelta(minutes=40))
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=30_000_000.0, quotes={LONG: 15_000.0, INV: 10_000.0})
    broker.buy_market(INV, QTY, "seed-order")
    broker._positions[INV].avg_price = AVG
    broker.orders.clear()
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True, 5))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    return svc, now0, state, broker


def _fail_forced_reconciles(monkeypatch, n: int, *, wipe_snapshot: bool = False):
    """주문 직전 강제 reconcile(force=True) 처음 n 회만 잔고조회를 실패시킨다(run_once 앞단 정상)."""
    real = worker.reconcile_position_state
    left = {"n": int(n)}

    def wrapped(broker_, state_, now_, *, force=False):
        if force and left["n"] > 0:
            left["n"] -= 1
            if wipe_snapshot:
                state_.last_good_broker_positions = {}
            real_get = broker_.get_positions

            def boom(*a, **k):
                raise RuntimeError("KIS 실계좌 잔고 조회 실패: simulated (2026-10-01 11:12)")

            broker_.get_positions = boom
            try:
                return real(broker_, state_, now_, force=force)
            finally:
                broker_.get_positions = real_get
        return real(broker_, state_, now_, force=force)

    monkeypatch.setattr(worker, "reconcile_position_state", wrapped)
    return left


def _sells(b):
    return [o for o in b.orders if o.side == "SELL" and o.success]


def _buys(b):
    return [o for o in b.orders if o.side == "BUY" and o.success]


def _exec_rows(side="SELL"):
    return [r for r in ledger._load_rows(ledger.EXECUTION_LEDGER_PATH, limit=0) if r.get("side") == side]


def _signal_row(prefix):
    rows = [r for r in ledger._load_rows(ledger.SIGNAL_LEDGER_PATH, limit=0)
            if str(r.get("signal_id") or "").startswith(prefix) and "TW2_3SLOT_CONFIRM" in str(r.get("signal_id"))]
    return rows[-1] if rows else None


# ── CASE A: 첫 조회 실패, 재조회 성공 -> 기존 production 전환 그대로 ─────────
def test_case_a_first_query_fails_retry_succeeds_normal_switch(tw2_3slot_market_data, monkeypatch):
    svc, now0, state, broker = _held_inverse(tw2_3slot_market_data, monkeypatch)
    _fail_forced_reconciles(monkeypatch, n=1)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert any(a.startswith("TW2_3SLOT_SWITCH") for a in r.actions), r.actions
    assert len(_sells(broker)) == 1 and len(_buys(broker)) == 1
    assert state.position is not None and state.position.symbol == LONG
    assert state.safe_exit is None
    trace = r.signal_dispatch_trace
    assert trace["position_data_error_retries"] == [worker.MATCH_POSITION]
    assert trace["position_reconcile_result"].startswith("POSITION_DATA_ERROR|retry=")


# ── CASE B: 3회 모두 실패 + 보유 확실 -> SAFE EXIT SELL 1회, BUY 0회 ────────
def test_case_b_all_retries_fail_safe_exit_sells_once_and_blocks_buy(tw2_3slot_market_data, monkeypatch):
    svc, now0, state, broker = _held_inverse(tw2_3slot_market_data, monkeypatch)
    _fail_forced_reconciles(monkeypatch, n=1 + worker.POSITION_DATA_ERROR_RETRY_MAX)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert len(_sells(broker)) == 1, "기존 인버스 SELL 정확히 1회"
    assert _sells(broker)[0].symbol == INV and _sells(broker)[0].requested_qty == QTY
    assert len(_buys(broker)) == 0, "잔고 미확인 상태에서 레버리지 BUY 금지"
    assert state.position is None
    assert state.safe_exit["status"] == "CONFIRMED" and state.safe_exit["qty"] == QTY
    rows = _exec_rows("SELL")
    assert len(rows) == 1 and rows[0]["source"] == worker.SAFE_EXIT_SOURCE
    assert rows[0]["order_id"] == _sells(broker)[0].order_id      # 실제 주문번호 (추정 행 아님)
    sig = _signal_row("")
    assert sig is not None and sig["order_result"] != "WAITING"
    assert worker.SAFE_EXIT_BUY_BLOCKED in sig["final_result"]
    assert "retry=" in sig["position_reconcile"]
    # 다음 tick 들 (조회가 계속 실패하든 아니든): 추가 SELL/BUY 없음
    _fail_forced_reconciles(monkeypatch, n=10)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(seconds=5))
    assert len(_sells(broker)) == 1 and len(_buys(broker)) == 0


# ── CASE C: 잔고도 로컬 근거도 불확실 -> SELL 도 BUY 도 없음, CRITICAL 기록 ──
def test_case_c_uncertain_local_state_does_nothing_and_records_critical(tw2_3slot_market_data, monkeypatch):
    svc, now0, state, broker = _held_inverse(tw2_3slot_market_data, monkeypatch)
    _fail_forced_reconciles(monkeypatch, n=50, wipe_snapshot=True)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _sells(broker) == [] and _buys(broker) == []
    assert state.position is not None and state.position.symbol == INV      # 그대로 보유
    assert state.order_block_reason == worker.CRITICAL_POSITION_DATA_ERROR
    assert "LAST_BROKER_SNAPSHOT_MISMATCH" in r.signal_dispatch_trace["failure_stage"]
    assert state.safe_exit is None


# ── CASE D: SAFE EXIT 직후(체결 미확인) 재시작 -> 중복 SELL 0, 정상 reconcile ─
def test_case_d_restart_after_unconfirmed_safe_exit_is_idempotent(tw2_3slot_market_data, monkeypatch):
    svc, now0, state, broker = _held_inverse(tw2_3slot_market_data, monkeypatch)
    _fail_forced_reconciles(monkeypatch, n=1 + worker.POSITION_DATA_ERROR_RETRY_MAX)
    real_rp = broker.reconcile_position
    broker.reconcile_position = lambda s: (_ for _ in ()).throw(RuntimeError("still down"))
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert len(_sells(broker)) == 1 and state.safe_exit["status"] == "SUBMITTED"
    assert state.position is not None, "체결 확인 전에는 가짜 청산 기록을 남기지 않는다"
    assert _exec_rows("SELL") == []
    # 재시작: 디스크 state 를 다시 읽는다 (SAFE EXIT 는 제출 직후 저장됨)
    restarted = state_store.deserialize(state_store.serialize(state_store.load_state()))
    assert restarted.safe_exit and restarted.safe_exit["status"] == "SUBMITTED"
    broker.reconcile_position = real_rp          # 브로커 정상화 (위 강제 실패 횟수는 이미 소진)
    res = worker.reconcile_position_state(broker, restarted, now0 + timedelta(seconds=20), force=True)
    assert res == worker.RECOVERED_TO_FLAT
    assert restarted.position is None and restarted.safe_exit["status"] == "CONFIRMED"
    rows = _exec_rows("SELL")
    assert len(rows) == 1 and rows[0]["order_id"] == _sells(broker)[0].order_id
    # 같은 신호가 다시 와도 SELL 은 더 나가지 않는다 (claim + leg 기록)
    sid = rows[0]["signal_id"]
    assert ledger.signal_id_has_leg(sid, "SELL")
    assert len(_sells(broker)) == 1


# ── CASE E: 부분체결 -> 잔량만 관리, 반대 BUY 금지 ─────────────────────────
def test_case_e_partial_fill_keeps_managing_remainder_and_blocks_buy(tw2_3slot_market_data, monkeypatch):
    svc, now0, state, broker = _held_inverse(tw2_3slot_market_data, monkeypatch)
    _fail_forced_reconciles(monkeypatch, n=1 + worker.POSITION_DATA_ERROR_RETRY_MAX)
    real_rp = broker.reconcile_position
    broker.reconcile_position = lambda s: (_ for _ in ()).throw(RuntimeError("still down"))
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert state.safe_exit["status"] == "SUBMITTED"
    broker.buy_market(INV, 400, "partial-remainder")          # 400주는 체결 안 된 것으로 남김
    broker.orders.pop()
    broker.reconcile_position = real_rp
    res = worker.reconcile_position_state(broker, state, now0 + timedelta(seconds=10), force=True)
    assert res == worker.RECOVERED_QTY_MISMATCH
    assert state.safe_exit["status"] == "PARTIAL"
    assert state.position is not None and state.position.symbol == INV and state.position.quantity == 400
    assert state.pending_signal is None
    rows = _exec_rows("SELL")
    assert len(rows) == 1 and int(float(rows[0]["executed_qty"])) == QTY - 400
    assert len(_buys(broker)) == 0


# ── 체결 반영 지연: 잔고가 그대로면 대기 동안 tick 을 막아 중복 매도 방지 ────
def test_unconfirmed_safe_exit_blocks_ticks_while_settling(tw2_3slot_market_data, monkeypatch):
    svc, now0, state, broker = _held_inverse(tw2_3slot_market_data, monkeypatch)
    _fail_forced_reconciles(monkeypatch, n=1 + worker.POSITION_DATA_ERROR_RETRY_MAX)
    broker.reconcile_position = lambda s: (_ for _ in ()).throw(RuntimeError("still down"))
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    broker.buy_market(INV, QTY, "not-yet-reflected")           # 브로커 잔고가 아직 그대로 보임
    broker.orders.pop()
    assert worker.reconcile_position_state(broker, state, now0 + timedelta(seconds=10), force=True) == worker.POSITION_DATA_ERROR
    assert state.safe_exit["status"] == "SUBMITTED"
    later = now0 + timedelta(seconds=worker.SAFE_EXIT_SETTLE_SEC + 5)
    assert worker.reconcile_position_state(broker, state, later, force=True) == worker.MATCH_POSITION
    assert state.safe_exit["status"] == "NOT_FILLED"


# ── CASE F: 정상 조회 -> 기존 production 과 동일 ───────────────────────────
def test_case_f_healthy_broker_path_unchanged(tw2_3slot_market_data, monkeypatch):
    svc, now0, state, broker = _held_inverse(tw2_3slot_market_data, monkeypatch)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert any(a.startswith("TW2_3SLOT_SWITCH") for a in r.actions), r.actions
    assert len(_sells(broker)) == 1 and len(_buys(broker)) == 1
    trace = r.signal_dispatch_trace
    assert trace["position_reconcile_result"] == worker.MATCH_POSITION          # 문자열도 기존 그대로
    assert "position_data_error_retries" not in trace
    assert state.safe_exit is None
