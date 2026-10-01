"""2026-10-01 hotfix 보강 — run_once 앞단 잔고조회 실패(POSITION_DATA_ERROR) tick 에서도
보유가 확실한 포지션의 청산 보호(강제청산/손절/익절)가 돌아야 한다.

수정 전: 앞단 reconcile 이 POSITION_DATA_ERROR 면 _advance_held_position_risk_management
자체가 호출되지 않아, 손절선을 깨거나 익절선에 닿아도 SELL 이 0 건이었다(재현 확인).
수정 후: 판정 로직은 그대로, 주문만 _SafeExitGuardBroker 로 보호(전량 SELL 1회, BUY 금지,
체결 확인 전 가짜 청산 기록 금지).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import config, ledger, worker
from app.trading.macd2.models import PositionSnapshot
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import _fresh_3slot_state, tw2_3slot_market_data  # noqa: F401

INV, LONG = config.INVERSE_SYMBOL, config.LONG_SYMBOL


def _setup(svc_now, monkeypatch):
    svc, now0 = svc_now
    st = _fresh_3slot_state()
    st.position = PositionSnapshot(symbol=INV, quantity=10, avg_price=10_000.0, entry_at=now0 - timedelta(minutes=40))
    st.time_window_position_active = True
    st.time_window_active_mode = "TW2_3SLOT"
    st.time_window_entry_session = "MORNING"
    b = FakeBroker(cash=10_000_000.0, quotes={LONG: 15_000.0, INV: 10_000.0})
    b.buy_market(INV, 10, "seed")
    b._positions[INV].avg_price = 10_000.0
    b.orders.clear()
    px = {"v": 10_000.0}
    monkeypatch.setattr(worker, "_fresh_quote_prices",
                        lambda md, syms: {config.WATCH_SYMBOL: 1.0, LONG: 15_000.0, INV: px["v"]})
    return svc, now0, st, b, px


def _down(b):
    real = b.get_positions
    b.get_positions = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("KIS 잔고조회 실패 (simulated)"))
    return real


def _sells(b):
    return [o for o in b.orders if o.side == "SELL" and o.success]


def _ticks(svc, st, b, now0, n):
    out = []
    for k in range(1, n + 1):
        r = run_once(broker=b, market_data=svc, state=st, now=now0 + timedelta(minutes=3 * k, seconds=1))
        out.append(r)
    return out


@pytest.mark.parametrize("price,reason", [(9_000.0, config.EXIT_TW_STOP_LOSS), (13_000.0, config.EXIT_TW_TP2_FULL)])
def test_exit_fires_on_same_tick_as_healthy_path(tw2_3slot_market_data, monkeypatch, price, reason):
    # 정상 경로 기준 (어느 tick 에 어떤 사유로 청산되는가)
    svc, now0, st, b, px = _setup(tw2_3slot_market_data, monkeypatch)
    run_once(broker=b, market_data=svc, state=st, now=now0)
    px["v"] = price
    healthy = _ticks(svc, st, b, now0, 3)
    h_tick = next(i for i, r in enumerate(healthy) if any(a.startswith(reason) for a in r.actions))
    assert len(_sells(b)) == 1

    # 같은 시나리오, 정상 tick 1 회 뒤 잔고조회가 계속 실패
    sell_rows_before = len([r for r in ledger._load_rows(ledger.EXECUTION_LEDGER_PATH, limit=0) if r.get("side") == "SELL"])
    svc, now0, st, b, px = _setup(tw2_3slot_market_data, monkeypatch)
    run_once(broker=b, market_data=svc, state=st, now=now0)            # 마지막 정상 잔고 스냅샷
    _down(b)
    px["v"] = price
    err = _ticks(svc, st, b, now0, 3)
    assert all(r.skipped == worker.POSITION_DATA_ERROR for r in err)
    e_tick = next(i for i, r in enumerate(err) if any(a.startswith("PROTECTIVE_EXIT_SUBMITTED") for a in r.actions))
    assert e_tick == h_tick, "잔고조회 실패여도 같은 tick 에 같은 판정으로 청산 보호가 돈다"
    assert st.safe_exit["exit_reason"] == reason
    assert len(_sells(b)) == 1, "보호 매도 정확히 1회 (이후 tick 은 체결 확인 대기)"
    assert any("PROTECTIVE_EXIT_WAITING_CONFIRMATION" in r.actions for r in err[e_tick + 1:])
    assert not [o for o in b.orders if o.side == "BUY"]
    assert st.position is not None, "체결 확인 전에는 포지션/원장을 바꾸지 않는다"
    assert len([r for r in ledger._load_rows(ledger.EXECUTION_LEDGER_PATH, limit=0)
                if r.get("side") == "SELL"]) == sell_rows_before, "보호 매도는 체결 확인 전 원장에 기록하지 않는다"


def test_protective_exit_is_confirmed_on_recovery_with_real_order_and_reason(tw2_3slot_market_data, monkeypatch):
    svc, now0, st, b, px = _setup(tw2_3slot_market_data, monkeypatch)
    run_once(broker=b, market_data=svc, state=st, now=now0)
    real = _down(b)
    px["v"] = 9_000.0
    _ticks(svc, st, b, now0, 3)
    sell = _sells(b)[0]
    b.get_positions = real                                              # 잔고조회 정상화
    r = run_once(broker=b, market_data=svc, state=st, now=now0 + timedelta(minutes=12, seconds=1))
    assert r.skipped == worker.RECOVERED_TO_FLAT
    assert st.position is None and st.safe_exit["status"] == "CONFIRMED"
    rows = [x for x in ledger._load_rows(ledger.EXECUTION_LEDGER_PATH, limit=0) if x.get("side") == "SELL"]
    assert len(rows) == 1
    assert rows[0]["order_id"] == sell.order_id
    assert rows[0]["exit_reason"] == config.EXIT_TW_STOP_LOSS
    assert rows[0]["source"] == worker.SAFE_EXIT_SOURCE
    assert len(_sells(b)) == 1


def test_no_protective_exit_when_holding_is_not_certain(tw2_3slot_market_data, monkeypatch):
    svc, now0, st, b, px = _setup(tw2_3slot_market_data, monkeypatch)
    _down(b)                                                             # 정상 잔고 스냅샷이 한 번도 없음
    px["v"] = 9_000.0
    err = _ticks(svc, st, b, now0, 3)
    assert _sells(b) == []
    assert st.safe_exit is None
    assert "NO_LOCAL_POSITION" not in str(st.position_reconcile_diag)
    assert "LAST_BROKER_SNAPSHOT_MISMATCH" in str(st.position_reconcile_diag.get("protective_exit_skipped"))
    assert all(r.skipped == worker.POSITION_DATA_ERROR for r in err)


def test_guard_blocks_partial_sell_second_sell_and_any_buy(tw2_3slot_market_data, monkeypatch):
    svc, now0, st, b, px = _setup(tw2_3slot_market_data, monkeypatch)
    g = worker._SafeExitGuardBroker(b, st, now0)
    assert g.buy_market(LONG, 5, "x").success is False
    assert g.buy_limit(LONG, 5, 15_000.0, "x").success is False
    assert g.sell_market(INV, 4, "EXIT:TIME_WINDOW_TP1_PARTIAL:x").success is False   # 부분매도 차단
    ok = g.sell_market(INV, 10, "EXIT:TIME_WINDOW_STOP_LOSS:0197X0:t")
    assert ok.success and st.safe_exit["status"] == "SUBMITTED"
    assert g.sell_market(INV, 10, "EXIT:TIME_WINDOW_STOP_LOSS:0197X0:t2").success is False  # 두 번째 차단
    with pytest.raises(worker._SafeExitUnconfirmed):
        g.reconcile_position(INV)
    assert len(_sells(b)) == 1


def test_healthy_exit_path_is_unchanged(tw2_3slot_market_data, monkeypatch):
    svc, now0, st, b, px = _setup(tw2_3slot_market_data, monkeypatch)
    run_once(broker=b, market_data=svc, state=st, now=now0)
    px["v"] = 13_000.0
    r = _ticks(svc, st, b, now0, 1)[0]
    assert any(a.startswith(config.EXIT_TW_TP2_FULL) for a in r.actions)
    assert st.position is None and st.safe_exit is None
    rows = [x for x in ledger._load_rows(ledger.EXECUTION_LEDGER_PATH, limit=0) if x.get("side") == "SELL"]
    assert len(rows) == 1 and rows[0].get("source", "") != worker.SAFE_EXIT_SOURCE
