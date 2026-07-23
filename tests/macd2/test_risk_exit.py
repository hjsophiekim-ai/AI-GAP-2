"""Unit tests for app.trading.macd2.risk_exit — pure decision logic only."""
from __future__ import annotations

from app.trading.macd2 import config
from app.trading.macd2.risk_exit import (
    check_stop_loss,
    evaluate_position_exits,
    update_profit_lock_tracker,
)


def test_check_stop_loss_boundary():
    assert check_stop_loss(-1.5) is True  # exactly at threshold
    assert check_stop_loss(-1.4999) is False
    assert check_stop_loss(-3.0) is True
    assert check_stop_loss(2.0) is False


def test_profit_lock_activates_at_threshold():
    tracker = update_profit_lock_tracker(current_net_return=1.5, peak_net_return=0.0, profit_lock_active=False)
    assert tracker.profit_lock_active is True
    assert tracker.peak_net_return == 1.5
    assert tracker.should_exit is False  # no giveback yet


def test_profit_lock_not_active_below_threshold():
    tracker = update_profit_lock_tracker(current_net_return=1.0, peak_net_return=1.0, profit_lock_active=False)
    assert tracker.profit_lock_active is False
    assert tracker.giveback_pct == 0.0
    assert tracker.should_exit is False


def test_profit_lock_exits_on_giveback_boundary():
    # peak 4.2, current 3.4 -> giveback 0.8pp == threshold -> exit
    tracker = update_profit_lock_tracker(current_net_return=3.4, peak_net_return=4.2, profit_lock_active=True)
    assert tracker.giveback_pct == 0.8
    assert tracker.should_exit is True


def test_profit_lock_no_exit_just_under_giveback_boundary():
    tracker = update_profit_lock_tracker(current_net_return=3.41, peak_net_return=4.2, profit_lock_active=True)
    assert round(tracker.giveback_pct, 2) == 0.79
    assert tracker.should_exit is False


def test_profit_lock_peak_never_decreases():
    tracker = update_profit_lock_tracker(current_net_return=2.0, peak_net_return=5.0, profit_lock_active=True)
    assert tracker.peak_net_return == 5.0


def test_evaluate_position_exits_stop_loss_takes_priority_over_profit_lock():
    # Even if profit-lock-style giveback conditions were met, SL below -1.5% wins.
    decision = evaluate_position_exits(current_net_return=-2.0, peak_net_return=5.0, profit_lock_active=True)
    assert decision.exit_reason == config.EXIT_STOP_LOSS


def test_evaluate_position_exits_profit_lock_when_no_stop_loss():
    decision = evaluate_position_exits(current_net_return=3.4, peak_net_return=4.2, profit_lock_active=True)
    assert decision.exit_reason == config.EXIT_PROFIT_LOCK


def test_evaluate_position_exits_hold_when_neither_triggered():
    decision = evaluate_position_exits(current_net_return=0.5, peak_net_return=0.5, profit_lock_active=False)
    assert decision.exit_reason is None


def test_evaluate_position_exits_example_from_docs():
    """docs §10 worked example: peak +4.2%, current +3.4%, giveback 0.8pp -> exit."""
    decision = evaluate_position_exits(current_net_return=3.4, peak_net_return=4.2, profit_lock_active=True)
    assert decision.peak_net_return == 4.2
    assert decision.current_net_return == 3.4
    assert decision.giveback_pct == 0.8
    assert decision.exit_reason == config.EXIT_PROFIT_LOCK
