"""MU CONFIRM 진입 손절 1봉 유예 (2026-11-05, 사용자 결정) — worker 경로 계약.

  · 진입 MU=ENTRY_CONFIRM 포지션만, 첫 TIME_WINDOW_STOP_LOSS 판정 봉에서 팔지 않고 다음 완성 3분봉 1개 대기
  · 다음 봉도 손절선 아래 -> 청산 / 회복 -> 보유, 포지션당 1회
  · 대기 중 매 틱 hard stop(-2.5%) -> 즉시 청산
  · 비CONFIRM·토글 OFF·이미 사용 -> 기존 손절 그대로
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import config, mu_assist, strategy_mode, worker
from app.trading.macd2.worker import _net_return_pct, run_once
from tests.macd2.test_early_take_profit_worker import _broker, _market
from tests.macd2.test_fixed_exit_15 import ENTRY, _isolate_shadow_ledger, _sells, _state
from tests.macd2.test_tw2_3slot_worker_regression import _patch_common

STOP = f"{config.EXIT_TW_STOP_LOSS}:{config.INVERSE_SYMBOL}"
DEFER = f"CONFIRM_STOP_DEFER:{config.INVERSE_SYMBOL}"
HARD = f"CONFIRM_STOP_DEFER_HARD_STOP:{config.INVERSE_SYMBOL}"


def _px(net_pct):
    p = ENTRY
    while p > ENTRY * 0.9:
        if _net_return_pct(config.INVERSE_SYMBOL, ENTRY, p, 10) <= net_pct:
            return p
        p -= 1.0
    raise AssertionError


def _go(monkeypatch, tmp_path, *, price, bar_close=None, mu=mu_assist.ENTRY_CONFIRM, defer="", no_bar=False):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now = _market(inverse_price=price)
    st = _state(now=now, mode=strategy_mode.MODE_N1, on=False, price=price, bar_close=bar_close)
    st.time_window_entry_mu_status = mu
    st.confirm_stop_defer_state = defer
    if no_bar:                                   # 이번 틱엔 새 완성봉 없음(같은 봉 진행 중)
        cur, _ = worker.forming_bar_window(now)
        st.stop_loss_bar_ts = cur.isoformat()
    broker = _broker(price)
    _patch_common(monkeypatch)
    n = len(broker.orders)
    res = run_once(broker=broker, state=st, market_data=svc, now=now)
    return st, broker.orders[n:], res


def test_confirm_entry_first_stop_is_deferred_one_bar(monkeypatch, tmp_path):
    p = _px(-1.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p)
    assert DEFER in res.actions and STOP not in res.actions and _sells(orders) == []
    assert st.confirm_stop_defer_state == "PENDING" and st.confirm_stop_defer_at


@pytest.mark.parametrize("mu", [mu_assist.ENTRY_WAIT, mu_assist.ENTRY_BAD, "", None])
def test_non_confirm_entry_stops_as_before(monkeypatch, tmp_path, mu):
    p = _px(-1.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p, mu=mu)
    assert STOP in res.actions and len(_sells(orders)) == 1 and DEFER not in res.actions


def test_toggle_off_stops_as_before(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MU_CONFIRM_STOP_DEFER_ENABLED", False)
    p = _px(-1.6)
    _st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p)
    assert STOP in res.actions and len(_sells(orders)) == 1


def test_next_bar_still_below_stop_exits(monkeypatch, tmp_path):
    p = _px(-1.7)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p, defer="PENDING")
    assert STOP in res.actions and len(_sells(orders)) == 1 and DEFER not in res.actions


def test_next_bar_recovered_holds_and_no_second_deferral(monkeypatch, tmp_path):
    p = _px(-0.4)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p, defer="PENDING")
    assert _sells(orders) == [] and STOP not in res.actions and st.confirm_stop_defer_state == "USED"
    q = _px(-1.6)                                                     # 같은 포지션의 두 번째 손절은 바로 판다
    st2, orders2, res2 = _go(monkeypatch, tmp_path / "2", price=q, bar_close=q, defer="USED")
    assert STOP in res2.actions and len(_sells(orders2)) == 1 and DEFER not in res2.actions


def test_hard_stop_on_tick_while_pending(monkeypatch, tmp_path):
    p = _px(-2.6)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, defer="PENDING", no_bar=True)
    assert HARD in res.actions and len(_sells(orders)) == 1


def test_pending_tick_above_hard_stop_waits(monkeypatch, tmp_path):
    p = _px(-2.0)
    st, orders, res = _go(monkeypatch, tmp_path, price=p, defer="PENDING", no_bar=True)
    assert _sells(orders) == [] and st.confirm_stop_defer_state == "PENDING"


def test_bar_already_beyond_hard_stop_is_not_deferred(monkeypatch, tmp_path):
    p = _px(-2.6)
    _st, orders, res = _go(monkeypatch, tmp_path, price=p, bar_close=p)
    assert STOP in res.actions and len(_sells(orders)) == 1 and DEFER not in res.actions


def test_position_scoped_reset_and_defaults():
    from app.trading.macd2.models import RuntimeState
    s = RuntimeState()
    assert s.time_window_entry_mu_status is None and s.confirm_stop_defer_state == ""
    s.time_window_entry_mu_status, s.confirm_stop_defer_state, s.confirm_stop_defer_at = "ENTRY_CONFIRM", "USED", "x"
    worker._clear_position_scoped_state(s, reason="TEST")
    assert s.time_window_entry_mu_status is None and s.confirm_stop_defer_state == "" and s.confirm_stop_defer_at is None
    assert config.MU_CONFIRM_STOP_DEFER_ENABLED is True and config.MU_CONFIRM_STOP_DEFER_HARD_PCT == pytest.approx(-2.5)
    assert config.AFTERNOON_STOP_LOSS == pytest.approx(-0.015)


def test_state_roundtrip_keeps_fields():
    from app.trading.macd2 import state_store
    s = state_store.default_state()
    s.time_window_entry_mu_status, s.confirm_stop_defer_state = "ENTRY_CONFIRM", "PENDING"
    r = state_store.deserialize(state_store.serialize(s))
    assert r.time_window_entry_mu_status == "ENTRY_CONFIRM" and r.confirm_stop_defer_state == "PENDING"
    raw = state_store.serialize(state_store.default_state())
    for k in ("time_window_entry_mu_status", "confirm_stop_defer_state", "confirm_stop_defer_at"):
        raw.pop(k, None)
    old = state_store.deserialize(raw)                                # 예전 상태파일 호환
    assert old.time_window_entry_mu_status is None and old.confirm_stop_defer_state == ""
