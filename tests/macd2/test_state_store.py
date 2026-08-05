"""Unit tests for app.trading.macd2.state_store — isolated to tmp_path via conftest.py."""
from __future__ import annotations

import json
from datetime import datetime

from app.trading.macd2 import config, state_store
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeStatus


def test_default_state_is_stopped_and_mock():
    state = state_store.default_state()
    assert state.ui_mode == RuntimeStatus.STOPPED
    assert state.auto_trade_on is False
    assert state.mode == "mock"
    assert state.budget == config.DEFAULT_BUDGET
    # 2026-08-05 (사용자 요청 — 모든 필터 기본값 OFF)
    assert state.major_filter_enabled is False
    assert state.major_filter_version == config.MAJOR_FILTER_VERSION


def test_default_state_all_filters_default_off():
    """2026-08-05 (사용자 요청): 강한 플래그/추세전환장/퀵Profit/Profit Lock 모두 기본값 OFF."""
    state = state_store.default_state()
    assert state.major_filter_enabled is False
    assert state.sideways_filter_enabled is False
    assert state.quick_profit_enabled is False
    assert state.profit_lock_enabled is False


def test_load_state_creates_no_file_until_saved(tmp_path):
    assert not state_store.STATE_PATH.exists()
    state = state_store.load_state()
    assert state.ui_mode == RuntimeStatus.STOPPED
    # load_state on a missing file must NOT create one as a side effect.
    assert not state_store.STATE_PATH.exists()


def test_save_then_load_roundtrip():
    state = state_store.default_state()
    state.auto_trade_on = True
    state.ui_mode = RuntimeStatus.RUNNING
    state.mode = "mock"
    state.budget = 5_000_000.0
    state.last_signal_direction = Direction.UP_RED
    state.last_signal_bar_ts = "2026-07-23T10:27:00+09:00"
    state.processed_signal_ids = ["20260723_102700_UP_RED"]
    state.position = PositionSnapshot(
        symbol="0193T0", quantity=10, avg_price=15000.0,
        entry_at=datetime(2026, 7, 23, 10, 27, tzinfo=config.KST),
    )
    state.peak_net_return = 2.5
    state.profit_lock_active = True
    state.possible_toggle_reset_at = "2026-07-23T10:20:00+09:00"
    state.profit_lock_enabled = False
    state.profit_lock_symbol = "0197X0"
    state.profit_lock_entry_bar_ts = "2026-07-23T10:24:00+09:00"
    state.profit_lock_last_bar_ts = "2026-07-23T10:33:00+09:00"
    state.profit_lock_bars_since_entry = 3
    state.profit_lock_gap_history = [10.0, 6.0, 3.0]
    state.profit_lock_peak_return_pct = 2.1
    state.profit_lock_current_support_gap = 3.0
    state.profit_lock_max_support_gap = 10.0
    state.profit_lock_gap_ratio = 0.3
    state.profit_lock_contraction_count = 2
    state.profit_lock_drawdown_pct = 0.4

    state_store.save_state(state)
    assert state_store.STATE_PATH.exists()

    loaded = state_store.load_state()
    assert loaded.auto_trade_on is True
    assert loaded.ui_mode == RuntimeStatus.RUNNING
    assert loaded.budget == 5_000_000.0
    assert loaded.last_signal_direction == Direction.UP_RED
    assert loaded.processed_signal_ids == ["20260723_102700_UP_RED"]
    assert loaded.position is not None
    assert loaded.position.symbol == "0193T0"
    assert loaded.position.quantity == 10
    assert loaded.position.entry_at == datetime(2026, 7, 23, 10, 27, tzinfo=config.KST)
    assert loaded.peak_net_return == 2.5
    assert loaded.profit_lock_active is True
    assert loaded.possible_toggle_reset_at == "2026-07-23T10:20:00+09:00"
    assert loaded.updated_at is not None
    # docs §10 2026-08-05 Profit Lock — MACD Convergence Early Exit: full
    # restart-persistence roundtrip for every profit_lock_* field.
    assert loaded.profit_lock_enabled is False
    assert loaded.profit_lock_symbol == "0197X0"
    assert loaded.profit_lock_entry_bar_ts == "2026-07-23T10:24:00+09:00"
    assert loaded.profit_lock_last_bar_ts == "2026-07-23T10:33:00+09:00"
    assert loaded.profit_lock_bars_since_entry == 3
    assert loaded.profit_lock_gap_history == [10.0, 6.0, 3.0]
    assert loaded.profit_lock_peak_return_pct == 2.1
    assert loaded.profit_lock_current_support_gap == 3.0
    assert loaded.profit_lock_max_support_gap == 10.0
    assert loaded.profit_lock_gap_ratio == 0.3
    assert loaded.profit_lock_contraction_count == 2
    assert loaded.profit_lock_drawdown_pct == 0.4


def test_profit_lock_enabled_omitted_from_disk_defaults_to_config_default():
    """A state file written before this feature existed (no profit_lock_enabled
    key at all) must fall back to config.PROFIT_LOCK_DEFAULT_ENABLED (2026-08-05:
    OFF), including for pre-existing persisted state."""
    state = state_store.default_state()
    state_store.save_state(state)
    raw = json.loads(state_store.STATE_PATH.read_text(encoding="utf-8"))
    del raw["profit_lock_enabled"]
    state_store.STATE_PATH.write_text(json.dumps(raw), encoding="utf-8")

    loaded = state_store.load_state()
    assert loaded.profit_lock_enabled is False


def test_save_is_atomic_no_tmp_file_left_behind():
    state_store.save_state(state_store.default_state())
    leftovers = list(state_store.STATE_DIR_PATH.glob("*.tmp.*"))
    assert leftovers == []


def test_load_state_recovers_from_corrupted_json():
    state_store.ensure_paths()
    state_store.STATE_PATH.write_text("{not valid json", encoding="utf-8")
    loaded = state_store.load_state()
    assert loaded.ui_mode == RuntimeStatus.STOPPED
    assert loaded.auto_trade_on is False


def test_load_state_discards_unexpected_keys():
    state_store.ensure_paths()
    state_store.STATE_PATH.write_text(
        '{"schema_version": 1, "ui_mode": "STOPPED", "mode": "mock", '
        '"legacy_v1_only_field": "should be dropped", "auto_trade_on": false}',
        encoding="utf-8",
    )
    loaded = state_store.load_state()
    serialized = state_store.serialize(loaded)
    assert "legacy_v1_only_field" not in serialized


def test_load_state_resets_major_filter_to_default_when_saved_version_is_old():
    """A saved major_filter_version mismatching the current code's version
    resets major_filter_enabled to config.MAJOR_FILTER_DEFAULT (2026-08-05:
    False) regardless of the stored ON/OFF value -- version-migration safety,
    independent of what that default currently is."""
    state_store.ensure_paths()
    state_store.STATE_PATH.write_text(
        '{"schema_version": 1, "ui_mode": "STOPPED", "mode": "mock", '
        '"auto_trade_on": false, "major_filter_enabled": true, '
        '"major_filter_version": "MAJOR_FILTER_HYBRID_V5"}',
        encoding="utf-8",
    )

    loaded = state_store.load_state()

    assert loaded.major_filter_enabled is False
    assert loaded.major_filter_version == config.MAJOR_FILTER_VERSION


def test_state_path_is_macd2_owned():
    assert "macd_hynix" not in str(state_store.STATE_PATH)
    assert state_store.STATE_PATH.name == "macd2_runtime.json"
