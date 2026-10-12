"""CHOP 모드 생애주기 — 전환 / 재시작 복원 / daily reset (2026-10-11).

``test_chop_defense.py`` 가 규칙 자체를 잠그고, 여기서는 **모드가 운영 중에
오가도 상태가 깨지지 않는지**를 잠근다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.trading.macd2 import chop_defense, config, state_store, strategy_mode
from app.trading.macd2.models import Direction

KST = config.KST
MODES = (strategy_mode.MODE_N1, strategy_mode.MODE_P3, strategy_mode.MODE_CHOP)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")
    return state_store


def _flag(st, hhmm, direction=Direction.UP_RED, day=8):
    chop_defense.note_morning_flag(
        st, dt.datetime(2026, 10, day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=KST), direction)


# ── 전환 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", MODES)
def test_every_mode_sets_a_coherent_flag_set(store, mode):
    st = store.default_state()
    strategy_mode.apply(st, mode)
    # 진입 tier 는 세 모드가 같다
    assert st.time_window_n1_filter_enabled is True
    assert st.c1_peak_protection_enabled is True
    assert st.smart_sizing_enabled is True
    # 청산 레이어
    assert bool(st.p3_enabled) is (mode in (strategy_mode.MODE_P3, strategy_mode.MODE_CHOP))
    # 왕복장 방어
    assert bool(st.chop_defense_enabled) is (mode == strategy_mode.MODE_CHOP)
    assert strategy_mode.current(st) == mode
    assert strategy_mode.infer_from_legacy(st)[0] == mode


def test_switching_between_all_modes_round_trips(store):
    st = store.default_state()
    for mode in (strategy_mode.MODE_N1, strategy_mode.MODE_CHOP, strategy_mode.MODE_P3,
                 strategy_mode.MODE_CHOP, strategy_mode.MODE_N1):
        strategy_mode.apply(st, mode)
        assert strategy_mode.current(st) == mode
        assert bool(st.chop_defense_enabled) is (mode == strategy_mode.MODE_CHOP)


def test_leaving_chop_clears_the_days_state(store):
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    _flag(st, "0924")
    st.chop_mc_on_at = "1012"
    st.chop_stop_key = "x"
    strategy_mode.apply(st, strategy_mode.MODE_P3)
    assert st.chop_mc_flags == []
    assert st.chop_mc_on_at is None
    assert st.chop_defense_enabled is False


# ── 재시작 복원 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", MODES)
def test_mode_and_chop_state_survive_restart(store, mode):
    st = store.default_state()
    strategy_mode.apply(st, mode)
    st.mode = "mock"
    if mode == strategy_mode.MODE_CHOP:
        _flag(st, "0924", Direction.UP_RED)
        _flag(st, "0936", Direction.DOWN_BLUE)
        st.chop_mc_on_at = "1012"
    store.save_state(st)

    back = store.load_state()
    assert strategy_mode.current(back) == mode, "재시작으로 모드가 바뀌면 안 된다"
    assert bool(back.chop_defense_enabled) is (mode == strategy_mode.MODE_CHOP)
    if mode == strategy_mode.MODE_CHOP:
        # no-trade 구간이 재시작으로 풀리면 안 된다
        assert back.chop_mc_on_at == "1012"
        assert [f[1] for f in back.chop_mc_flags] == ["UP_RED", "DOWN_BLUE"]
        assert chop_defense.daily_cap(back) == int(config.CHOP_DAILY_CAP)


def test_restart_does_not_silently_downgrade_chop_to_p3(store):
    """회귀 방지 — infer_from_legacy 가 CHOP 을 모르면 restore 가 모드를 지웠다."""
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    st.mode = "mock"
    store.save_state(st)
    back = store.load_state()
    assert strategy_mode.current(back) != strategy_mode.MODE_P3
    assert strategy_mode.current(back) == strategy_mode.MODE_CHOP


# ── daily reset ─────────────────────────────────────────────────────────
def test_new_day_resets_the_morning_state(store):
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    _flag(st, "0924", Direction.UP_RED, day=8)
    _flag(st, "0936", Direction.DOWN_BLUE, day=8)
    st.chop_mc_on_at = "1012"
    st.chop_mc_off_at = "1048"
    assert st.chop_mc_day == "20261008"

    # 다음 날 첫 플래그가 들어오면 전날 상태는 사라진다
    _flag(st, "0915", Direction.UP_RED, day=9)
    assert st.chop_mc_day == "20261009"
    assert [f[0] for f in st.chop_mc_flags] == ["0915"]
    assert st.chop_mc_on_at is None
    assert st.chop_mc_off_at is None


def test_reset_daily_clears_bar_cache_too(store):
    st = store.default_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    st.chop_bars_at = "202610081010"
    st.chop_box = [1.0, 2.0, 3.0]
    st.chop_last_close = 1.5
    chop_defense.reset_daily(st)
    assert st.chop_bars_at is None
    assert chop_defense.cached_box(st) is None
    assert st.chop_last_close is None
