"""Unit tests for app.trading.macd2.state_store — isolated to tmp_path via conftest.py."""
from __future__ import annotations

from datetime import datetime

from app.trading.macd2 import config, state_store
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeStatus


def test_default_state_is_stopped_and_mock():
    state = state_store.default_state()
    assert state.ui_mode == RuntimeStatus.STOPPED
    assert state.auto_trade_on is False
    assert state.mode == "mock"
    assert state.budget == config.DEFAULT_BUDGET


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
    assert loaded.updated_at is not None


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


def test_does_not_reference_macd_v1_paths():
    assert "macd_hynix" not in str(state_store.STATE_PATH)
    assert state_store.STATE_PATH.name == "macd2_runtime.json"
