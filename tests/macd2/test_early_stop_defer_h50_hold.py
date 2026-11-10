"""EARLY_STOP_DEFER H50 보류 예외 (2026-11-09).

진입 후 30분 이내 첫 일반 손절은 기존대로 1봉 유예한다. 단, 손절 판정 시점에 **이미** 반대 HY 플래그가
확정돼 H50 이 청산만 보류 중(h50_hold_active, 이 포지션 소유)이면 유예하지 않고 정상 손절한다
(NOT_DEFERRED / reason=H50_HOLD). 같은 틱에 H50 이 새로 설정되는 경우는 worker 실행순서(리스크 관리 →
T+3 판정)상 포함되지 않는다. -2.5% hard stop 등 다른 청산은 그대로.

사례: 2026-11-09 12:33 RED(0193T0 4,330원, 12:39 진입) → 12:57 BLUE 확정, 13:03 H50 보류 →
13:09:03 경과 29.97분 −1.59% 에서 유예 → 13:11:58 hard stop −2.51%(체결 4,210, −261,195원).
새 규칙이면 13:09 정상 손절이었다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, early_stop_defer
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from tests.macd2.test_early_stop_defer import DEFER, HARD, STOP, _events, _mk, _pending, _px, _tick
from tests.macd2.test_fixed_exit_15 import _sells


def _hold(st, *, held=Direction.DOWN_BLUE, owner_epoch=None, started_min_after_entry=5.0):
    """반대 플래그로 H50 이 이미 보류 중인 상태 (worker 의 note_hold_start + 소유권 각인과 같은 필드)."""
    st.h50_hold_active = True
    st.h50_original_direction = held.value
    st.h50_hold_started_at = (st.position.entry_at + timedelta(minutes=started_min_after_entry)).isoformat()
    st.position_epoch = int(st.position_epoch or 0) or 7
    st.h50_owner_epoch = st.position_epoch if owner_epoch is None else owner_epoch


# ── worker 경로 ────────────────────────────────────────────────────────────
def test_h50_hold_active_first_stop_exits_without_defer(monkeypatch, tmp_path):
    p = _px(-1.6)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p, minutes=10.0)
    _hold(st)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert STOP in res.actions and DEFER not in res.actions and len(_sells(orders)) == 1
    assert not st.early_stop_defer_active
    rows = early_stop_defer.load_rows()
    assert [r["event"] for r in rows] == [early_stop_defer.EVENT_NOT_DEFERRED]
    assert rows[0]["reason"] == early_stop_defer.SKIP_H50_HOLD == "H50_HOLD"
    assert rows[0]["deferred"] == "False"


def test_case_20261109_1309_afternoon_29_97min_is_normal_stop(monkeypatch, tmp_path):
    """11/09 13:09:03 재현: 오후 진입, 경과 29.97분, 판정봉 −1.59%, H50 이 13:03 부터 보류 중 → 정상 손절."""
    p = _px(-1.59)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p, minutes=29.97, session="AFTERNOON")
    _hold(st, started_min_after_entry=24.0)                    # 12:39 진입 → 13:03 H50 보류
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert STOP in res.actions and DEFER not in res.actions and HARD not in res.actions
    assert len(_sells(orders)) == 1
    row = early_stop_defer.load_rows()[0]
    assert (row["event"], row["reason"]) == (early_stop_defer.EVENT_NOT_DEFERRED, "H50_HOLD")
    assert float(row["elapsed_min"]) == pytest.approx(29.97)


def test_same_case_without_h50_is_still_deferred(monkeypatch, tmp_path):
    """대조: 같은 조건에서 H50 보류가 없으면 기존 30분 1봉 유예 그대로(실제 11/09 동작)."""
    p = _px(-1.59)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p, minutes=29.97, session="AFTERNOON")
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert DEFER in res.actions and _sells(orders) == [] and st.early_stop_defer_active
    assert _events() == [early_stop_defer.EVENT_DEFER]


def test_stale_h50_from_previous_position_does_not_block_defer(monkeypatch, tmp_path):
    p = _px(-1.6)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p)
    _hold(st, owner_epoch=3)                                    # position_epoch=7 과 다른 소유자
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert DEFER in res.actions and _sells(orders) == []


def test_h50_hold_for_other_direction_does_not_block_defer(monkeypatch, tmp_path):
    """H50 이 기록한 보유 방향이 현재 포지션(인버스=DOWN_BLUE)과 다르면 기존 유예."""
    p = _px(-1.6)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p)
    _hold(st, held=Direction.UP_RED)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert DEFER in res.actions and _sells(orders) == []


def test_hard_stop_while_waiting_unchanged_even_if_h50_starts_later(monkeypatch, tmp_path):
    """이미 유예 대기 중에 H50 이 생겨도 hard stop 경로는 그대로(이번 변경은 유예 '시작' 판정만)."""
    p = _px(-2.6)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=None, no_bar=True)
    _pending(st, monkeypatch, svc, now)
    _hold(st)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert HARD in res.actions and len(_sells(orders)) == 1
    assert _events() == [early_stop_defer.EVENT_HARD_STOP]


def test_after_30min_reason_stays_late_with_h50(monkeypatch, tmp_path):
    p = _px(-1.6)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p, minutes=30.5)
    _hold(st)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert STOP in res.actions and early_stop_defer.load_rows()[0]["reason"] == early_stop_defer.SKIP_LATE


def test_bar_beyond_hard_stop_reason_unchanged_with_h50(monkeypatch, tmp_path):
    p = _px(-2.6)
    svc, now, st = _mk(tmp_path, monkeypatch, price=p, bar_close=p)
    _hold(st)
    orders, res = _tick(monkeypatch, svc, now, st, p)
    assert early_stop_defer.load_rows()[0]["reason"] == early_stop_defer.SKIP_ALREADY_HARD


# ── 순수 판정 (11/09 실제 포지션 = 레버리지 0193T0, H50 보유방향 UP_RED) ───────────────
def _unit_state(*, sym, held, minutes=29.97, owner=1, epoch=1, active=True):
    now = datetime(2026, 11, 9, 13, 9, 3, tzinfo=config.KST)
    s = RuntimeState()
    pos = PositionSnapshot(symbol=sym, quantity=2171, avg_price=4330.0, entry_at=now - timedelta(minutes=minutes))
    s.position = pos
    s.h50_hold_active = active
    s.h50_original_direction = held.value
    s.h50_owner_epoch, s.position_epoch = owner, epoch
    return s, pos, now


def test_unit_long_position_case_20261109():
    s, pos, now = _unit_state(sym=config.LONG_SYMBOL, held=Direction.UP_RED)
    assert early_stop_defer.skip_reason(s, pos, now, -1.5905) == early_stop_defer.SKIP_H50_HOLD


@pytest.mark.parametrize("kw", [dict(active=False), dict(held=Direction.DOWN_BLUE), dict(owner=1, epoch=2)])
def test_unit_no_exception_cases(kw):
    base = dict(sym=config.LONG_SYMBOL, held=Direction.UP_RED)
    base.update(kw)
    s, pos, now = _unit_state(**base)
    assert early_stop_defer.skip_reason(s, pos, now, -1.5905) is None


def test_unit_unknown_epoch_still_applies():
    """소유권 키가 0(모름)이면 worker stale 판정과 같이 stale 로 몰지 않는다."""
    s, pos, now = _unit_state(sym=config.LONG_SYMBOL, held=Direction.UP_RED, owner=0, epoch=5)
    assert early_stop_defer.skip_reason(s, pos, now, -1.5905) == early_stop_defer.SKIP_H50_HOLD


def test_unit_toggle_off_wins(monkeypatch):
    s, pos, now = _unit_state(sym=config.LONG_SYMBOL, held=Direction.UP_RED)
    monkeypatch.setattr(config, "EARLY_STOP_DEFER_ENABLED", False)
    assert early_stop_defer.skip_reason(s, pos, now, -1.6) == early_stop_defer.SKIP_DISABLED
