"""X1 주문경로 배선 (2026-09-23 단계 1b).

검증 원칙
  * X1 OFF 면 배선된 함수들이 **완전한 no-op** 이어야 한다(OFF parity).
  * X1 은 기존 청산보다 **뒤에서만** 돈다 — 호출 순서를 소스로 고정한다.
  * flip 이력은 워커가 직접 적재하므로 정의상 LIVE_CONFIRMED 다.
  * 포지션이 닫히면 ARM 은 즉시 reset 된다.
  * stale ARM 은 **주문을 내지 않고** reset 만 한다.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, state_store, worker, x1_context as X
from app.trading.macd2.models import Direction, RuntimeState

KST = config.KST


class _Knobs:
    """monkeypatch.undo() 를 쓰지 않기 위한 holder."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def act(self, *, flip_exit=None, ar1=None):
        if flip_exit is not None:
            self.mp.setattr(config, "X1_ACT_FLIP_EXIT", bool(flip_exit))
        if ar1 is not None:
            self.mp.setattr(config, "X1_ACT_AR1", bool(ar1))


def _state(*, n1=True, c1=True, x1=False) -> RuntimeState:
    s = state_store.default_state()
    s.time_window_n1_filter_enabled = bool(n1)
    s.c1_peak_protection_enabled = bool(c1)
    s.x1_context_enabled = bool(x1)
    return s


def _now(hhmm="10:00"):
    return pd.Timestamp("2026-09-22 " + hhmm, tz=KST).to_pydatetime()


# ── 활성 게이트 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n1,c1,x1,expected", [
    (True, True, True, True),
    (True, True, False, False),
    (True, False, True, False),
    (False, True, True, False),
])
def test_x1_enabled_requires_n1_c1_and_toggle(n1, c1, x1, expected):
    assert worker._x1_enabled(_state(n1=n1, c1=c1, x1=x1)) is expected


def test_default_state_has_x1_off():
    s = state_store.default_state()
    assert s.x1_context_enabled is False
    assert s.x1_shadow_mode_enabled is False
    assert s.x1_flip_exit_armed is False
    assert list(s.x1_flag_history) == []


# ── LIVE 플래그 이력 ───────────────────────────────────────────────────────
def test_flag_history_is_recorded_and_is_live_confirmed():
    s = _state()
    worker._x1_record_live_flag(s, Direction.UP_RED, _now("09:00"))
    worker._x1_record_live_flag(s, Direction.DOWN_BLUE, _now("09:27"))
    assert len(s.x1_flag_history) == 2
    ev = worker._x1_flag_events(s)
    assert [e["source"] for e in ev] == [X.LIVE_CONFIRMED, X.LIVE_CONFIRMED]
    assert [e["direction"] for e in ev] == ["UP_RED", "DOWN_BLUE"]


def test_flag_history_is_day_scoped():
    s = _state()
    worker._x1_record_live_flag(s, Direction.UP_RED, _now("09:00"))
    nxt = pd.Timestamp("2026-09-23 09:00", tz=KST).to_pydatetime()
    worker._x1_record_live_flag(s, Direction.DOWN_BLUE, nxt)
    assert len(s.x1_flag_history) == 1
    assert s.x1_flag_history[0][1] == "DOWN_BLUE"


def test_flag_history_dedupes_same_bar():
    s = _state()
    for _ in range(3):
        worker._x1_record_live_flag(s, Direction.UP_RED, _now("09:00"))
    assert len(s.x1_flag_history) == 1


def test_flag_history_is_capped():
    s = _state()
    base = pd.Timestamp("2026-09-22 09:00", tz=KST)
    for i in range(config.X1_FLAG_HISTORY_MAX + 20):
        worker._x1_record_live_flag(
            s, Direction.UP_RED, (base + timedelta(minutes=3 * i)).to_pydatetime())
    assert len(s.x1_flag_history) == config.X1_FLAG_HISTORY_MAX


def test_flag_history_is_recorded_even_when_x1_off():
    """켜는 순간 이력이 비어 있으면 flip 을 못 세므로 OFF 여도 적재한다."""
    s = _state(x1=False)
    worker._x1_record_live_flag(s, Direction.UP_RED, _now("09:00"))
    assert len(s.x1_flag_history) == 1


# ── OFF parity ─────────────────────────────────────────────────────────────
def test_flip_exit_is_noop_when_x1_off():
    s = _state(x1=False)
    s.x1_flip_exit_armed = True          # 켜져 있었다 해도
    out = worker._advance_x1_flip_exit(
        broker=None, state=s, now=_now(), macd_snap=None, bars_3m=None,
        position=None, result=None)
    assert out is None
    assert s.x1_flip_exit_armed is True  # OFF 면 손대지 않는다(완전 no-op)


def test_flip_exit_is_noop_when_action_flag_off(monkeypatch):
    _Knobs(monkeypatch).act(flip_exit=False)
    s = _state(x1=True)
    out = worker._advance_x1_flip_exit(
        broker=None, state=s, now=_now(), macd_snap=None, bars_3m=None,
        position=None, result=None)
    assert out is None


def test_flip_exit_resets_when_flat():
    s = _state(x1=True)
    s.x1_flip_exit_armed = True
    s.x1_flip_exit_armed_at = _now().isoformat()
    s.x1_flip_exit_owner_epoch = 7
    out = worker._advance_x1_flip_exit(
        broker=None, state=s, now=_now(), macd_snap=None, bars_3m=None,
        position=None, result=None)
    assert out is None
    assert s.x1_flip_exit_armed is False
    assert s.x1_flip_exit_owner_epoch == 0


def test_position_close_resets_arm():
    s = _state(x1=True)
    s.x1_flip_exit_armed = True
    s.x1_flip_exit_armed_at = _now().isoformat()
    s.x1_flip_exit_owner_epoch = 3
    s.x1_last_checked_bar_ts = _now().isoformat()
    worker._x1_reset_position_state(s)
    assert s.x1_flip_exit_armed is False
    assert s.x1_flip_exit_armed_at is None
    assert s.x1_flip_exit_owner_epoch == 0
    assert s.x1_last_checked_bar_ts is None


# ── state 영속 ─────────────────────────────────────────────────────────────
def test_x1_runtime_state_round_trips():
    s = _state(x1=True)
    s.x1_flag_history = [["2026-09-22T09:00:00+09:00", "UP_RED"]]
    s.x1_flip_exit_armed = True
    s.x1_flip_exit_armed_at = "2026-09-22T10:00:00+09:00"
    s.x1_flip_exit_owner_epoch = 5
    s.x1_last_checked_bar_ts = "2026-09-22T10:00:00+09:00"
    s.x1_last_action = "FLIP_EXIT_ARMED"
    back = state_store.deserialize(state_store.serialize(s))
    assert back.x1_flag_history == [["2026-09-22T09:00:00+09:00", "UP_RED"]]
    assert back.x1_flip_exit_armed is True
    assert back.x1_flip_exit_owner_epoch == 5
    assert back.x1_last_action == "FLIP_EXIT_ARMED"


def test_old_state_without_x1_wiring_keys_loads_safely():
    s = _state()
    raw = state_store.serialize(s)
    for k in ("x1_flag_history", "x1_flip_exit_armed", "x1_flip_exit_armed_at",
              "x1_flip_exit_owner_epoch", "x1_last_checked_bar_ts"):
        raw.pop(k, None)
    back = state_store.deserialize(raw)
    assert back.x1_flip_exit_armed is False
    assert list(back.x1_flag_history) == []


# ── 소스 계약: 호출 순서 / 청산 전용 ───────────────────────────────────────
def _worker_src() -> str:
    return io.open("app/trading/macd2/worker.py", encoding="utf-8").read()


def test_x1_flip_exit_runs_after_every_existing_exit():
    """X1 은 H50 / C1 **뒤**에서만 호출돼야 한다 — 기존 청산을 지연시키지 않는다."""
    src = _worker_src()
    i_h50 = src.index("h50_outcome = _advance_h50_hold(")
    i_c1 = src.index("c1_outcome = _advance_c1_peak_protection(")
    i_x1 = src.index("x1_outcome = _advance_x1_flip_exit(")
    assert i_h50 < i_c1 < i_x1


def test_x1_flip_exit_is_exit_only():
    """청산 전용 — 진입 주문 경로를 부르지 않는다."""
    src = _worker_src()
    start = src.index("def _advance_x1_flip_exit(")
    end = src.index("def _advance_c1_peak_protection(", start)
    body = src[start:end]
    assert "_execute_reversal_exit_only_for_filtered_entry(" in body
    for forbidden in ("_execute_entry", "place_buy", "BUY"):
        assert forbidden not in body


def test_ar1_hook_is_scoped_to_same_direction_reject_only():
    src = _worker_src()
    start = src.index("# ── X1-3 AR1 (2026-09-23)")
    body = src[start:start + 2000]
    assert "TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND" in body
    assert "config.X1_ACT_AR1" in body
    assert "_x1_enabled(state)" in body


def test_x1_reset_is_wired_into_position_close():
    src = _worker_src()
    assert "_x1_reset_position_state(state)     # X1-2 FLIP EXIT" in src


def test_worker_records_live_flag_on_confirmed_flag():
    src = _worker_src()
    assert "_x1_record_live_flag(state, direction, flag_bar_dt)" in src
