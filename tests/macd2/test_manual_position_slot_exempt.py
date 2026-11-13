"""수동거래 슬롯 분리 (2026-11-12).

실사고: 2026-11-12 09:38 사용자가 KIS 에서 0193T0 1주를 수동 매수 -> reconcile 이 RECOVERED_FROM_BROKER
로 입양하면서 CHOP 하루 2슬롯 중 1개를 소비 -> 11:42 BLUE 이후 플래그가 TW2_3SLOT_REJECT_DAILY_SLOT_CAP.

규칙: 봇 BUY 주문 흔적(직전 봇 BUY 가 성공 확정 아님 / 미확정 주문 추적 중) 없이 브로커에서 발견된
포지션은 수동거래 -> 입양·청산관리는 그대로, 3-SLOT/TW 슬롯·하루 진입수는 소비하지 않는다.
봇 BUY 실패/미확정 보고 뒤 발견(= 늦은 체결)은 기존처럼 센다.
"""
from __future__ import annotations

from datetime import timedelta

from app.trading.macd2 import config, state_store, worker
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_p3_retry_postentry import _setup
from tests.macd2.test_worker_time_window import _fresh_state as _tw_fresh_state

KST = config.KST
LONG = config.LONG_SYMBOL


def _broker_with_position(qty=1):
    broker = FakeBroker(cash=10_000_000.0, quotes={LONG: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(LONG, qty, "manual-hts")
    return broker


def _now():
    from datetime import datetime
    return datetime.now(KST)


def test_manual_discovery_does_not_count_daily_total():
    state = _tw_fresh_state()
    result = worker.reconcile_position_state(_broker_with_position(), state, _now(), force=True)
    assert result == worker.RECOVERED_FROM_BROKER
    assert state.position is not None and state.position.symbol == LONG     # 관리는 그대로
    assert state.position_manual_origin is True
    assert state.daily_total_entry_count == 0


def test_discovery_after_failed_bot_buy_still_counts():
    state = _tw_fresh_state()
    state.bot_buy_unconfirmed_symbol = LONG
    result = worker.reconcile_position_state(_broker_with_position(10), state, _now(), force=True)
    assert result == worker.RECOVERED_FROM_BROKER
    assert state.position_manual_origin is False
    assert state.daily_total_entry_count == 1
    assert state.bot_buy_unconfirmed_symbol is None


def test_failed_bot_buy_on_other_symbol_does_not_make_it_bot_origin():
    state = _tw_fresh_state()
    state.bot_buy_unconfirmed_symbol = config.INVERSE_SYMBOL
    worker.reconcile_position_state(_broker_with_position(), state, _now(), force=True)
    assert state.position_manual_origin is True
    assert state.daily_total_entry_count == 0


def test_toggle_off_restores_counting(monkeypatch):
    monkeypatch.setattr(config, "MANUAL_POSITION_SLOT_EXEMPT", False)
    state = _tw_fresh_state()
    worker.reconcile_position_state(_broker_with_position(), state, _now(), force=True)
    assert state.position_manual_origin is False
    assert state.daily_total_entry_count == 1


def _outcome(side, success, symbol=LONG):
    from types import SimpleNamespace
    r = BrokerOrderResult(success=success, order_id="o1", symbol=symbol, side=side,
                          requested_qty=10, executed_qty=10 if success else 0, executed_price=15_000.0,
                          message="", raw={})
    return SimpleNamespace(buy_result=r if side == "BUY" else None, sell_result=r if side == "SELL" else None,
                           order_failure_stage=None, block_reason=None)


def test_broker_order_result_tracks_unconfirmed_bot_buy():
    state = _tw_fresh_state()
    worker._record_broker_order_result(state, _outcome("BUY", False))
    assert state.bot_buy_unconfirmed_symbol == LONG
    worker._record_broker_order_result(state, _outcome("SELL", True))
    assert state.bot_buy_unconfirmed_symbol == LONG          # SELL 은 건드리지 않는다
    worker._record_broker_order_result(state, _outcome("BUY", True))
    assert state.bot_buy_unconfirmed_symbol is None


def test_new_position_epoch_clears_marker_but_exit_cleanup_does_not():
    state = _tw_fresh_state()
    state.bot_buy_unconfirmed_symbol = LONG
    worker._clear_position_scoped_state(state, reason="EXIT")
    assert state.bot_buy_unconfirmed_symbol == LONG
    state.position_manual_origin = True
    worker._begin_position_epoch(state, reason="NEW")
    assert state.bot_buy_unconfirmed_symbol is None
    assert state.position_manual_origin is False


def test_state_roundtrip_keeps_fields():
    state = _tw_fresh_state()
    state.bot_buy_unconfirmed_symbol = LONG
    state.position_manual_origin = True
    back = state_store.deserialize(state_store.serialize(state))
    assert back.bot_buy_unconfirmed_symbol == LONG
    assert back.position_manual_origin is True


# ── run_once: 3-SLOT 입양 경로 (실사고 재현) ─────────────────────────────

def test_run_once_manual_position_adopted_without_consuming_slot(monkeypatch, tmp_path):
    svc, now0, state, _ = _setup(monkeypatch, tmp_path)
    broker = _broker_with_position(1)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(seconds=30))
    assert state.position is not None and state.position.symbol == LONG
    assert state.time_window_position_active is True, "수동 포지션도 청산관리 대상"
    assert state.position_manual_origin is True
    assert int(state.tw2_3slot_slots_used_today or 0) == 0
    assert int(state.tw2_3slot_morning_count or 0) + int(state.tw2_3slot_afternoon_count or 0) == 0
    assert int(state.daily_total_entry_count or 0) == 0


def test_run_once_late_filled_bot_buy_still_consumes_slot(monkeypatch, tmp_path):
    svc, now0, state, _ = _setup(monkeypatch, tmp_path)
    state.bot_buy_unconfirmed_symbol = LONG
    broker = _broker_with_position(10)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(seconds=30))
    assert state.position is not None and state.time_window_position_active is True
    assert state.position_manual_origin is False
    assert int(state.tw2_3slot_slots_used_today or 0) == 1
    assert int(state.daily_total_entry_count or 0) == 1
