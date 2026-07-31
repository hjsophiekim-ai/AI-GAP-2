"""Unit tests for app.trading.tsla_auto.state_store — isolated to tmp_path via conftest.py."""
from __future__ import annotations

from datetime import datetime

from app.trading.tsla_auto import config, state_store
from app.trading.tsla_auto.models import Direction, PositionSnapshot, RuntimeState


def test_default_state_uses_tsla_auto_identity():
    state = state_store.default_state()
    assert state.strategy_id == config.STRATEGY_ID
    assert state.strategy_name == config.STRATEGY_NAME
    assert state.budget_usd == config.DEFAULT_BUDGET_USD


def test_save_and_load_round_trip_preserves_fields():
    state = state_store.default_state()
    state.auto_trade_on = True
    state.mode = "MOCK"
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=30.5, entry_at=datetime.now(config.ET))
    state.last_signal_direction = Direction.UP_RED
    state.processed_signal_ids = ["sid-1", "sid-2"]
    state.market_regime = "NORMAL"
    state.stop_loss_cooldown_direction = Direction.DOWN_BLUE
    state.stop_loss_reentry_override_used_today = True
    state.account_holding_qty = 10
    state.strategy_owned_qty = 10
    state.strategy_average_price = 30.5
    state.strategy_order_ids = ["order-1"]

    state_store.save_state(state)
    loaded = state_store.load_state()

    assert loaded.auto_trade_on is True
    assert loaded.mode == "MOCK"
    assert loaded.position.symbol == config.LONG_SYMBOL
    assert loaded.position.quantity == 10
    assert loaded.last_signal_direction == Direction.UP_RED
    assert loaded.processed_signal_ids == ["sid-1", "sid-2"]
    assert loaded.market_regime == "NORMAL"
    assert loaded.stop_loss_cooldown_direction == Direction.DOWN_BLUE
    assert loaded.stop_loss_reentry_override_used_today is True
    assert loaded.account_holding_qty == 10
    assert loaded.strategy_owned_qty == 10
    assert loaded.strategy_average_price == 30.5
    assert loaded.strategy_order_ids == ["order-1"]


def test_load_state_recovers_from_corrupted_json():
    state_store.ensure_paths()
    state_store.STATE_PATH.write_text("not valid json {{{", encoding="utf-8")
    loaded = state_store.load_state()
    assert isinstance(loaded, RuntimeState)
    assert loaded.strategy_id == config.STRATEGY_ID


def test_load_state_missing_file_returns_default():
    loaded = state_store.load_state()
    assert isinstance(loaded, RuntimeState)
    assert loaded.position is None


def test_state_path_never_touches_macd2():
    assert "macd2" not in str(state_store.STATE_PATH).lower()
    assert state_store.STATE_PATH.name == "tsla_auto_runtime.json"
