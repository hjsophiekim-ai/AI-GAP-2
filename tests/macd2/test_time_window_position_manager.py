"""Unit tests for app.trading.macd2.time_window_position_manager — pure
functions only. Test values are derived RELATIVE to the module's own
threshold constants (twpm.MORNING_TP1_PCT etc., sourced from config.py) so
these tests stay valid regardless of which profile config.py currently
ships (spec-default or a later tuned profile) — only the ORDERING/ladder
LOGIC is under test here, not any specific numeric threshold value.
"""
from __future__ import annotations

from app.trading.macd2 import config, time_window_position_manager as twpm

_EPS = 0.05  # small offset well inside/outside any realistic threshold gap


class TestMorningLadder:
    def test_hold_below_tp1_and_above_stop(self):
        below_tp1 = twpm.MORNING_TP1_PCT / 2.0
        d = twpm.evaluate_morning_position(net_return_pct=below_tp1, tp1_done=False, peak_net_return=0.0)
        assert d.exit_reason is None
        assert d.tp1_done is False

    def test_stop_loss_before_tp1(self):
        d = twpm.evaluate_morning_position(net_return_pct=twpm.MORNING_STOP_LOSS_PCT - _EPS, tp1_done=False, peak_net_return=0.0)
        assert d.exit_reason == config.EXIT_TW_STOP_LOSS
        assert d.sell_fraction == 1.0
        assert d.tp1_done is False

    def test_stop_loss_exact_threshold_fires(self):
        d = twpm.evaluate_morning_position(net_return_pct=twpm.MORNING_STOP_LOSS_PCT, tp1_done=False, peak_net_return=0.0)
        assert d.exit_reason == config.EXIT_TW_STOP_LOSS

    def test_tp1_partial_sell_at_threshold(self):
        d = twpm.evaluate_morning_position(net_return_pct=twpm.MORNING_TP1_PCT, tp1_done=False, peak_net_return=0.0)
        assert d.exit_reason == config.EXIT_TW_TP1_PARTIAL
        assert d.sell_fraction == config.MORNING_TP1_SELL_RATIO
        assert d.tp1_done is True

    def test_after_tp1_default_stop_holds_and_exits_around_its_threshold(self):
        just_above = twpm.MORNING_AFTER_TP1_STOP_PCT + _EPS
        just_below = twpm.MORNING_AFTER_TP1_STOP_PCT - _EPS
        peak_below_trailing_trigger = twpm.MORNING_TP1_PCT  # not yet enough to raise the trailing tier
        d_hold = twpm.evaluate_morning_position(net_return_pct=just_above, tp1_done=True, peak_net_return=peak_below_trailing_trigger)
        assert d_hold.exit_reason is None
        d_exit = twpm.evaluate_morning_position(net_return_pct=just_below, tp1_done=True, peak_net_return=peak_below_trailing_trigger)
        assert d_exit.exit_reason == config.EXIT_TW_AFTER_TP1_STOP

    def test_trailing_stop_raises_after_trailing_trigger_peak(self, monkeypatch):
        # Ladder-tier logic test, independent of whatever profile config.py
        # currently ships -- if the active profile's TP2 sits BELOW the
        # trailing tier (e.g. a tuned "tight TP" profile), that tier is a
        # legitimately dead/unreachable branch in real trading, so this
        # test locally widens TP2 just far enough to isolate and verify the
        # trailing-stop COMPARISON logic on its own terms.
        monkeypatch.setattr(twpm, "MORNING_TP2_PCT", twpm.MORNING_TRAILING_TRIGGER_PCT + 10.0)
        peak = twpm.MORNING_TRAILING_TRIGGER_PCT + _EPS
        just_above_trailing_stop = twpm.MORNING_TRAILING_STOP_PCT + _EPS
        just_below_trailing_stop = twpm.MORNING_TRAILING_STOP_PCT - _EPS
        d_hold = twpm.evaluate_morning_position(net_return_pct=just_above_trailing_stop, tp1_done=True, peak_net_return=peak)
        assert d_hold.exit_reason is None
        d_exit = twpm.evaluate_morning_position(net_return_pct=just_below_trailing_stop, tp1_done=True, peak_net_return=peak)
        assert d_exit.exit_reason == config.EXIT_TW_TRAILING_STOP

    def test_tp2_full_exit_at_threshold(self):
        d = twpm.evaluate_morning_position(net_return_pct=twpm.MORNING_TP2_PCT, tp1_done=True, peak_net_return=twpm.MORNING_TP2_PCT)
        assert d.exit_reason == config.EXIT_TW_TP2_FULL
        assert d.sell_fraction == 1.0

    def test_tp2_direct_if_tp1_never_triggered(self):
        beyond_tp2 = twpm.MORNING_TP2_PCT + _EPS
        d = twpm.evaluate_morning_position(net_return_pct=beyond_tp2, tp1_done=False, peak_net_return=0.0)
        assert d.exit_reason == config.EXIT_TW_TP2_FULL
        assert d.tp1_done is True

    def test_peak_tracker_monotonic(self):
        d = twpm.evaluate_morning_position(net_return_pct=0.1, tp1_done=False, peak_net_return=2.0)
        assert d.peak_net_return == 2.0  # never decreases below prior peak


class TestAfternoonLadder:
    def test_hold_between_stop_and_tp(self):
        mid = (twpm.AFTERNOON_STOP_LOSS_PCT + twpm.AFTERNOON_TP_PCT) / 2.0
        d = twpm.evaluate_afternoon_position(net_return_pct=mid, peak_net_return=max(mid, 0.0))
        assert d.exit_reason is None

    def test_base_stop_loss(self):
        d = twpm.evaluate_afternoon_position(net_return_pct=twpm.AFTERNOON_STOP_LOSS_PCT, peak_net_return=twpm.AFTERNOON_STOP_LOSS_PCT)
        assert d.exit_reason == config.EXIT_TW_STOP_LOSS

    def test_breakeven_stop_after_trigger_peak(self):
        peak = twpm.AFTERNOON_BREAKEVEN_TRIGGER_PCT + _EPS
        just_above = twpm.AFTERNOON_BREAKEVEN_STOP_PCT + _EPS
        just_below = twpm.AFTERNOON_BREAKEVEN_STOP_PCT - _EPS
        d_hold = twpm.evaluate_afternoon_position(net_return_pct=just_above, peak_net_return=peak)
        assert d_hold.exit_reason is None
        d_exit = twpm.evaluate_afternoon_position(net_return_pct=just_below, peak_net_return=peak)
        assert d_exit.exit_reason == config.EXIT_TW_BREAKEVEN_STOP

    def test_profit_lock_stop_after_trigger_peak(self, monkeypatch):
        # See test_trailing_stop_raises_after_trailing_trigger_peak's
        # docstring -- isolates this tier's comparison logic from whichever
        # profile's TP happens to be active (a tuned "tight TP" profile can
        # legitimately put TP below this tier, making it unreachable in
        # real trading without that meaning the comparison logic is wrong).
        monkeypatch.setattr(twpm, "AFTERNOON_TP_PCT", twpm.AFTERNOON_PROFIT_LOCK_TRIGGER_PCT + 10.0)
        peak = twpm.AFTERNOON_PROFIT_LOCK_TRIGGER_PCT + _EPS
        just_above = twpm.AFTERNOON_PROFIT_LOCK_STOP_PCT + _EPS
        just_below = twpm.AFTERNOON_PROFIT_LOCK_STOP_PCT - _EPS
        d_hold = twpm.evaluate_afternoon_position(net_return_pct=just_above, peak_net_return=peak)
        assert d_hold.exit_reason is None
        d_exit = twpm.evaluate_afternoon_position(net_return_pct=just_below, peak_net_return=peak)
        assert d_exit.exit_reason == config.EXIT_TW_PROFIT_LOCK_STOP

    def test_full_tp_at_threshold(self):
        d = twpm.evaluate_afternoon_position(net_return_pct=twpm.AFTERNOON_TP_PCT, peak_net_return=twpm.AFTERNOON_TP_PCT)
        assert d.exit_reason == config.EXIT_TW_AFTERNOON_TP
        assert d.sell_fraction == 1.0

    def test_afternoon_never_partial_sells(self):
        candidates = [
            twpm.AFTERNOON_STOP_LOSS_PCT - _EPS, twpm.AFTERNOON_STOP_LOSS_PCT,
            0.0, twpm.AFTERNOON_BREAKEVEN_TRIGGER_PCT + _EPS,
            twpm.AFTERNOON_PROFIT_LOCK_TRIGGER_PCT + _EPS, twpm.AFTERNOON_TP_PCT + _EPS,
        ]
        for net in candidates:
            d = twpm.evaluate_afternoon_position(net_return_pct=net, peak_net_return=max(net, 0.0))
            assert d.sell_fraction in (0.0, 1.0)


class TestSessionDispatch:
    def test_dispatches_morning(self):
        d = twpm.evaluate_position(session="MORNING", net_return_pct=twpm.MORNING_TP1_PCT, tp1_done=False, peak_net_return=0.0)
        assert d.exit_reason == config.EXIT_TW_TP1_PARTIAL

    def test_dispatches_afternoon(self):
        d = twpm.evaluate_position(session="AFTERNOON", net_return_pct=twpm.AFTERNOON_TP_PCT, tp1_done=False, peak_net_return=0.0)
        assert d.exit_reason == config.EXIT_TW_AFTERNOON_TP

    def test_unknown_session_defaults_to_morning(self):
        d = twpm.evaluate_position(session="UNKNOWN", net_return_pct=twpm.MORNING_TP1_PCT, tp1_done=False, peak_net_return=0.0)
        assert d.exit_reason == config.EXIT_TW_TP1_PARTIAL
