"""E 하루 최대 2회 토글 (2026-10-06) — 계약 고정.

  * 기본 OFF. 한도가 2 가 되는 것은 **E 모드 + 토글 ON** 일 때뿐이다.
  * N1 / P3 에서는 토글이 켜져 있어도 기존 한도(3) 그대로다.
  * 토글은 하루 한도 하나만 바꾼다 -- resolve_slot 의 다른 판정은 그대로다.
  * 재시작(직렬화) 후 유지, 예전 state 에는 OFF, 모드 전환으로 지워지지 않는다.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.trading.macd2 import config, e_strategy, state_store, strategy_mode, time_window_3slot, worker
from app.trading.macd2 import service as service_module
from app.trading.macd2.models import Direction

KST = config.KST
MORNING = datetime(2026, 10, 6, 10, 0, tzinfo=KST)
AFTERNOON = datetime(2026, 10, 6, 13, 30, tzinfo=KST)


def _state(mode, cap2=False):
    s = state_store.default_state()
    strategy_mode.apply(s, mode)
    s.e_daily_cap2_enabled = cap2
    return s


def test_default_is_off_and_cap_is_unchanged():
    s = state_store.default_state()
    assert s.e_daily_cap2_enabled is False
    for m in strategy_mode.ALL_MODES:
        assert e_strategy.daily_entry_cap(_state(m)) == config.TW2_3SLOT_DAILY_CAP == 3


@pytest.mark.parametrize("mode,expected", [
    (strategy_mode.MODE_E, 2), (strategy_mode.MODE_P3, 3), (strategy_mode.MODE_N1, 3)])
def test_cap_is_two_only_for_e_with_toggle(mode, expected):
    assert e_strategy.daily_entry_cap(_state(mode, cap2=True)) == expected


@pytest.mark.parametrize("now", [MORNING, AFTERNOON])
def test_resolve_slot_default_is_byte_identical_and_cap2_blocks_only_the_third(now):
    kw = dict(now=now, morning_count=1, afternoon_count=1, direction=Direction.UP_RED,
              is_flat=True, last_afternoon_direction=None)
    for used in (0, 1, 2, 3):
        assert time_window_3slot.resolve_slot(slots_used_today=used, **kw) == \
            time_window_3slot.resolve_slot(slots_used_today=used, daily_cap=None, **kw)
        assert time_window_3slot.resolve_slot(slots_used_today=used, **kw) == \
            time_window_3slot.resolve_slot(slots_used_today=used, daily_cap=3, **kw)
    # cap=2: 1·2번째는 cap=3 과 완전히 같고, 3번째(used=2)만 슬롯 한도로 막힌다.
    for used in (0, 1):
        assert time_window_3slot.resolve_slot(slots_used_today=used, daily_cap=2, **kw) == \
            time_window_3slot.resolve_slot(slots_used_today=used, **kw)
    third = time_window_3slot.resolve_slot(slots_used_today=2, daily_cap=2, **kw)
    assert not third.slot_allowed and third.reject_reason == time_window_3slot.REJECT_SLOT_CAP


def test_toggle_survives_restart_and_legacy_state_is_off():
    s = _state(strategy_mode.MODE_E, cap2=True)
    raw = state_store.serialize(s)
    r = state_store.deserialize(dict(raw))
    strategy_mode.restore(r)
    assert r.e_daily_cap2_enabled is True and e_strategy.daily_entry_cap(r) == 2
    raw.pop("e_daily_cap2_enabled")
    raw.pop("e_daily_cap2_changed_at", None)
    legacy = state_store.deserialize(raw)
    assert legacy.e_daily_cap2_enabled is False


def test_mode_switch_keeps_the_toggle_but_only_e_uses_it():
    s = _state(strategy_mode.MODE_E, cap2=True)
    strategy_mode.apply(s, strategy_mode.MODE_P3)
    assert s.e_daily_cap2_enabled is True and e_strategy.daily_entry_cap(s) == 3
    strategy_mode.apply(s, strategy_mode.MODE_E)
    assert e_strategy.daily_entry_cap(s) == 2


def test_day_rollover_does_not_clear_the_toggle():
    s = _state(strategy_mode.MODE_E, cap2=True)
    worker._apply_day_rollover(s, datetime(2026, 10, 7, 8, 0, tzinfo=KST))
    assert s.e_daily_cap2_enabled is True


def test_e_breakout_hard_safety_respects_cap2():
    s = _state(strategy_mode.MODE_E, cap2=True)
    s.auto_trade_on, s.stopped = True, False
    s.tw2_3slot_slots_used_today = 2
    assert worker._e_fire_hard_safety(s, MORNING, {"signal_id": "A"}) == "TW2_3SLOT_DAILY_CAP_REACHED"
    s.e_daily_cap2_enabled = False
    assert worker._e_fire_hard_safety(s, MORNING, {"signal_id": "A"}) is None


def test_pending_3slot_retry_is_dropped_at_cap2(monkeypatch):
    s = _state(strategy_mode.MODE_E, cap2=True)
    s.tw2_3slot_slots_used_today = 2
    s.pending_signal = {"signal_id": "X", "direction": "UP_RED", "signal_type": "INITIAL",
                        "detected_at": MORNING.isoformat(), "tw2_3slot_ctx": {"session": "MORNING"}}
    monkeypatch.setattr(worker, "_execute_or_wait",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("주문 시도 금지")))
    out = worker._retry_pending_signal(broker=None, market_data=None, state=s, now=MORNING, macd_snap=None,
                                       pending_dir=Direction.UP_RED, position=None,
                                       result=worker.TickResult(), bars_3m=None, default_signal_type="INITIAL")
    assert out is None and s.pending_signal is None
    assert s.order_block_reason == "TW2_3SLOT_DAILY_CAP_REACHED"


def test_service_setter_persists_and_reports_effective_cap():
    svc = service_module.Macd2Service.__new__(service_module.Macd2Service)
    st = state_store.load_state()
    strategy_mode.apply(st, strategy_mode.MODE_E)
    state_store.save_state(st)
    out = svc.set_e_daily_cap2_enabled(True)
    assert out["ok"] and out["e_daily_cap2_enabled"] and out["effective_daily_cap"] == 2
    assert state_store.load_state().e_daily_cap2_enabled is True
    out = svc.set_e_daily_cap2_enabled(False)
    assert out["effective_daily_cap"] == 3 and state_store.load_state().e_daily_cap2_enabled is False


# ── UI: 체크박스는 E 에서만 보이고, 누르면 토글이 저장된다 ─────────────────
CAP2_LABEL = "E 하루 최대 2회"


def _ui_with_mode(mode):
    from tests.macd2.test_ui_strategy_mode import _run
    st = state_store.load_state()
    strategy_mode.apply(st, mode)
    state_store.save_state(st)
    return _run()


@pytest.mark.parametrize("mode", [strategy_mode.MODE_N1, strategy_mode.MODE_P3])
def test_ui_checkbox_hidden_outside_e(mode):
    at = _ui_with_mode(mode)
    assert CAP2_LABEL not in [c.label for c in at.checkbox]


def test_ui_checkbox_in_e_toggles_the_cap():
    at = _ui_with_mode(strategy_mode.MODE_E)
    box = [c for c in at.checkbox if c.label == CAP2_LABEL]
    assert len(box) == 1 and box[0].value is False
    box[0].check().run()
    assert state_store.load_state().e_daily_cap2_enabled is True
    assert any("E 하루 신규진입 한도: **2회**" in str(c.value) for c in at.caption)
