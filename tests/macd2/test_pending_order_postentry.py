"""2026-10-27 — 미확정 매수주문(pending_buy_order)의 늦은 체결이 3-SLOT 후처리를 건너뛰던 버그.

취소 미확인(BUY_CANCEL_NOT_CONFIRMED)으로 끝난 3-SLOT 진입은 나중에 주문조회/잔고로
체결이 확인되면 ``worker._pending_apply_new_position`` 이 포지션을 세운다. 그 경로는
``_apply_switch_outcome`` 만 탔고 ``_finalize_tw2_3slot_entry`` 를 건너뛰어

  * P3 note_entry_regime (B3 ownership) / position_epoch
  * 3-SLOT 슬롯·오전오후 카운트 (하루 cap 의 입력)
  * W1a/x2lite 일일 노출 note_entry

이 전부 빠졌다 -- 2026-09-29 재시도 체결 사고(test_p3_retry_postentry.py)와 같은 유형이다.
모의계좌는 주문번호 조회가 0행이라 잔고 fallback 경로가 실제로 쓰인다. 실계좌도 같은 함수다 --
부분체결 재호가(v3 취소)가 당일 차단/상태조회 불가/KILL 로 기존 경로에 떨어지면 국내 취소가
CANCEL_NOT_SUPPORTED 라 똑같이 BUY_CANCEL_NOT_CONFIRMED 가 되고, 늦은 체결은 주문조회로 확인된다.
그래서 아래 시나리오는 MOCK / REAL 두 계좌 모드로 전부 돈다.

전부 실제 ``worker.run_once()`` 진입 경로를 탄다(fake broker, conftest 격리).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import (
    chop_regime, config, order_executor, p3_stack, pending_order, state_store, time_window_3slot, worker,
)
from app.trading.macd2.models import Direction
from app.trading.macd2.worker import run_once
from tests.macd2.test_p3_retry_postentry import _setup
from tests.macd2.test_pending_buy_order import LiveOrderBroker
from tests.macd2.test_tw2_3slot_worker_regression import _PRIOR_DAY, _prime_3slot_pending

LONG = config.LONG_SYMBOL


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 2)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)
    monkeypatch.setattr(order_executor, "POST_CANCEL_RECHECK_DELAY_SEC", 0.0)


ACCOUNTS = pytest.mark.parametrize("account", ["mock", "real"])


def _unconfirmed_entry(monkeypatch, tmp_path, *, status_error=False, account="mock"):
    """P3 + CHOP regime 에서 3-SLOT T+3 승인 -> 지정가 미체결 + 취소 실패 -> pending_buy_order."""
    svc, now0, state, _ = _setup(monkeypatch, tmp_path, long_price=15_000.0)
    state.mode = account
    broker = LiveOrderBroker(cash=10_000_000.0, quotes={LONG: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.mode = account
    broker.next_buy_fill_qty = 0
    broker.status_error = status_error
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    p = state.pending_buy_order
    assert p, "취소 미확인 진입은 pending_buy_order 로 넘어가야 한다"
    assert state.position is None
    assert state.tw2_3slot_slots_used_today == 0
    return svc, now0, state, broker, p


def _assert_full_post_entry(state, signal_id, epoch_before):
    assert state.position is not None and state.position.symbol == LONG
    assert state.p3_entry_regime == chop_regime.REGIME_CHOP
    assert state.p3_position_active is True
    assert p3_stack.position_mode(state) == p3_stack.MODE_B3
    assert p3_stack.governs_position(state) is True
    assert state.tw2_3slot_slots_used_today == 1
    assert state.tw2_3slot_morning_count + state.tw2_3slot_afternoon_count == 1
    assert state.x2lite_entry_seq_today == 1
    assert state.x2lite_exposure_used_today > 0
    assert int(state.position_epoch) == epoch_before + 1
    assert state.time_window_position_active is True
    assert state.time_window_active_mode == time_window_3slot.MODE_N1_3SLOT
    assert state.tw2_3slot_post_entry_signal_id == signal_id


@ACCOUNTS
def test_daily_cap_counts_the_late_fill(monkeypatch, tmp_path, account):
    """늦은 체결이 슬롯을 세지 않으면 하루 진입 한도를 넘는 추가 진입이 가능해진다."""
    svc, now0, state, broker, p = _unconfirmed_entry(monkeypatch, tmp_path, account=account)
    state.tw2_3slot_slots_used_today = worker._tw2_3slot_daily_cap(state) - 1
    broker.fill_later(p["order_id"], p["requested_qty"])
    worker.track_pending_order(broker, state, now0 + timedelta(minutes=2), force=True)
    assert state.tw2_3slot_slots_used_today == worker._tw2_3slot_daily_cap(state)


@ACCOUNTS
def test_pending_order_carries_3slot_context(monkeypatch, tmp_path, account):
    _, _, _, _, p = _unconfirmed_entry(monkeypatch, tmp_path, account=account)
    ctx = p.get("tw2_3slot_ctx")
    assert isinstance(ctx, dict) and ctx.get("session") and ctx.get("signal_detected_at")
    assert ctx.get("sizing"), "실제 주문에 쓴 사이징을 실어야 노출 누계가 맞는다"


@ACCOUNTS
def test_late_fill_by_order_status_gets_full_post_entry(monkeypatch, tmp_path, account):
    svc, now0, state, broker, p = _unconfirmed_entry(monkeypatch, tmp_path, account=account)
    epoch_before = int(state.position_epoch or 0)
    broker.fill_later(p["order_id"], p["requested_qty"])
    r = worker.track_pending_order(broker, state, now0 + timedelta(minutes=2), force=True)
    assert r["action"] == pending_order.LATE_FILL_CONFIRMED
    _assert_full_post_entry(state, p["signal_id"], epoch_before)


@ACCOUNTS
def test_late_fill_by_balance_fallback_gets_full_post_entry(monkeypatch, tmp_path, account):
    """모의계좌 실측: 주문번호 조회 0행 -> 잔고 증가로 체결 확인 (오늘 운영 경로)."""
    svc, now0, state, broker, p = _unconfirmed_entry(monkeypatch, tmp_path, status_error=True, account=account)
    epoch_before = int(state.position_epoch or 0)
    broker.fill_later(p["order_id"], p["requested_qty"])
    r = worker.track_pending_order(broker, state, now0 + timedelta(minutes=2), force=True)
    assert r["action"] == pending_order.LATE_FILL_CONFIRMED
    _assert_full_post_entry(state, p["signal_id"], epoch_before)


@ACCOUNTS
def test_partial_late_fills_apply_post_entry_exactly_once(monkeypatch, tmp_path, account):
    svc, now0, state, broker, p = _unconfirmed_entry(monkeypatch, tmp_path, account=account)
    epoch_before = int(state.position_epoch or 0)
    first = max(1, p["requested_qty"] // 3)
    broker.fill_later(p["order_id"], first)
    worker.track_pending_order(broker, state, now0 + timedelta(minutes=1), force=True)
    exposure_after_first = state.x2lite_exposure_used_today
    broker.fill_later(p["order_id"], p["requested_qty"] - first)
    worker.track_pending_order(broker, state, now0 + timedelta(minutes=2), force=True)
    assert state.position.quantity == p["requested_qty"]
    _assert_full_post_entry(state, p["signal_id"], epoch_before)   # 슬롯/epoch 가 두 번 오르지 않는다
    assert state.x2lite_exposure_used_today == pytest.approx(exposure_after_first)


@ACCOUNTS
def test_context_and_ownership_survive_restart(monkeypatch, tmp_path, account):
    svc, now0, state, broker, p = _unconfirmed_entry(monkeypatch, tmp_path, account=account)
    state_store.save_state(state)
    state = state_store.load_state()
    assert isinstance((state.pending_buy_order or {}).get("tw2_3slot_ctx"), dict)
    broker.fill_later(p["order_id"], p["requested_qty"])
    worker.track_pending_order(broker, state, now0 + timedelta(minutes=2), force=True)
    assert p3_stack.position_mode(state) == p3_stack.MODE_B3
    assert state.tw2_3slot_slots_used_today == 1


@ACCOUNTS
def test_pending_without_context_is_unchanged(monkeypatch, tmp_path, account):
    """문맥 없는 pending(수동·예약매수 등)은 예전처럼 포지션만 세운다."""
    svc, now0, state, broker, p = _unconfirmed_entry(monkeypatch, tmp_path, account=account)
    state.pending_buy_order.pop("tw2_3slot_ctx")
    broker.fill_later(p["order_id"], p["requested_qty"])
    worker.track_pending_order(broker, state, now0 + timedelta(minutes=2), force=True)
    assert state.position is not None and state.position.quantity == p["requested_qty"]
    assert state.tw2_3slot_slots_used_today == 0
    assert p3_stack.position_mode(state) == p3_stack.MODE_BASE
