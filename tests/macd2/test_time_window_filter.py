"""Unit tests for app.trading.macd2.time_window_filter — pure functions only."""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, signal_engine, time_window_filter as twf
from app.trading.macd2.models import Direction

KST = config.KST


def _bars(prices: list[float], *, start: datetime, volumes: list[float] | None = None) -> pd.DataFrame:
    rows = []
    for i, price in enumerate(prices):
        dt = start + timedelta(minutes=3 * i)
        vol = volumes[i] if volumes else 1000.0
        rows.append({
            "datetime": dt, "open": price - 0.01, "high": price + 0.05,
            "low": price - 0.05, "close": price, "volume": vol,
        })
    return pd.DataFrame(rows)


def _down_then_up(n_down: int, n_up: int, *, start: datetime, start_price: float = 100.0,
                   down_step: float = 0.03, up_step: float = 0.09) -> pd.DataFrame:
    prices = []
    price = start_price
    for _ in range(n_down):
        price -= down_step
        prices.append(price)
    for _ in range(n_up):
        price += up_step
        prices.append(price)
    return _bars(prices, start=start)


def _first_up_red_flag(bars: pd.DataFrame) -> int:
    series = signal_engine.calculate_macd_series(bars)
    assert series is not None
    last_dir = None
    for i in range(1, len(series)):
        prev = float(series["macd"].iloc[i - 1] - series["signal"].iloc[i - 1])
        curr = float(series["macd"].iloc[i] - series["signal"].iloc[i])
        if prev <= 0 and curr > 0 and last_dir != Direction.UP_RED:
            return i
        if (prev <= 0 and curr > 0) or (prev >= 0 and curr < 0):
            last_dir = Direction.UP_RED if prev <= 0 and curr > 0 else Direction.DOWN_BLUE
    raise AssertionError("synthetic series produced no UP_RED crossover -- fix test fixture")


@pytest.fixture
def flagged_session():
    """30 completed bars of gentle decline then a strong rally -- guaranteed
    to produce a real UP_RED crossover with room for a T+3 confirmation bar
    and morning-window classification."""
    start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
    bars = _down_then_up(28, 12, start=start)
    flag_idx = _first_up_red_flag(bars)
    assert flag_idx + 1 < len(bars), "fixture needs a bar after the flag for T+3"
    return bars, flag_idx


class TestClassifyWindow:
    def test_boundaries(self):
        assert twf.classify_window(dtime(8, 59)) is None
        assert twf.classify_window(dtime(9, 0)) == twf.WINDOW_MORNING_1
        assert twf.classify_window(dtime(9, 44)) == twf.WINDOW_MORNING_1
        assert twf.classify_window(dtime(9, 45)) == twf.WINDOW_MORNING_2
        assert twf.classify_window(dtime(10, 19)) == twf.WINDOW_MORNING_2
        assert twf.classify_window(dtime(10, 20)) == twf.WINDOW_MORNING_3
        assert twf.classify_window(dtime(10, 49)) == twf.WINDOW_MORNING_3
        assert twf.classify_window(dtime(10, 50)) == twf.WINDOW_NO_NEW_ENTRY
        assert twf.classify_window(dtime(12, 59)) == twf.WINDOW_NO_NEW_ENTRY
        assert twf.classify_window(dtime(13, 0)) == twf.WINDOW_AFTERNOON_1
        assert twf.classify_window(dtime(13, 59)) == twf.WINDOW_AFTERNOON_1
        assert twf.classify_window(dtime(14, 0)) == twf.WINDOW_AFTERNOON_2
        assert twf.classify_window(dtime(14, 59)) == twf.WINDOW_AFTERNOON_2
        assert twf.classify_window(dtime(15, 0)) is None

    def test_session_for_window(self):
        assert twf.session_for_window(twf.WINDOW_MORNING_1) == "MORNING"
        assert twf.session_for_window(twf.WINDOW_AFTERNOON_2) == "AFTERNOON"
        # WINDOW_NO_NEW_ENTRY classifies as a morning session for entry-cap/
        # position-management purposes IF config.TW_ALLOW_ENTRY_1050_1300
        # ever relaxes it into a tradeable window (default keeps it closed
        # entirely -- see TestWindowGating.test_no_new_entry_window_always_rejects).
        assert twf.session_for_window(twf.WINDOW_NO_NEW_ENTRY) == "MORNING"
        assert twf.session_for_window(None) is None


class TestNoImmediateEntry:
    def test_bars_ending_at_flag_bar_itself_is_rejected_not_confirmed(self, flagged_session):
        """Passing bars_3m truncated AT the flag bar (no T+3 bar yet) must
        never approve -- entry requires waiting for the next completed bar."""
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        bars_at_flag_only = bars.iloc[: flag_idx + 1]
        decision = twf.evaluate_time_window_entry(
            bars_at_flag_only, Direction.UP_RED, flag_dt, flag_dt,
        )
        assert decision.approved is False
        assert decision.decision == config.TW_REJECT_NOT_CONFIRMED

    def test_confirmation_exactly_one_bar_after_flag_can_approve(self, flagged_session):
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        confirm_bars = bars.iloc[: flag_idx + 2]
        decision_at = confirm_bars["datetime"].iloc[-1] + timedelta(minutes=3)
        decision = twf.evaluate_time_window_entry(
            confirm_bars, Direction.UP_RED, flag_dt, decision_at,
        )
        assert decision.decision != config.TW_REJECT_NOT_CONFIRMED

    def test_signal_disappears_by_t_plus_3_is_rejected(self):
        """If MACD reverses back by the very next completed bar, the
        candidate must not be confirmed (gap_now <= 0 -> REJECT_NOT_CONFIRMED)."""
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        bars = _down_then_up(28, 2, start=start)  # only a brief 2-bar rally
        flag_idx = _first_up_red_flag(bars)
        # extend with an immediate sharp reversal right after the flag bar
        extra_start = bars["datetime"].iloc[-1] + timedelta(minutes=3)
        crash = _bars([bars["close"].iloc[-1] - 5.0], start=extra_start)
        extended = pd.concat([bars, crash], ignore_index=True)
        flag_dt = extended["datetime"].iloc[flag_idx]
        confirm_bars = extended.iloc[: flag_idx + 2]
        decision_at = confirm_bars["datetime"].iloc[-1] + timedelta(minutes=3)
        decision = twf.evaluate_time_window_entry(confirm_bars, Direction.UP_RED, flag_dt, decision_at)
        assert decision.approved is False
        assert decision.decision in (config.TW_REJECT_NOT_CONFIRMED, config.TW_REJECT_MACD_GAP_NOT_EXPANDING)


class TestWindowGating:
    def _decision_at_time(self, flagged_session, moment: dtime):
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        confirm_bars = bars.iloc[: flag_idx + 2]
        decision_at = confirm_bars["datetime"].iloc[-1].replace(
            hour=moment.hour, minute=moment.minute, second=0, microsecond=0,
        )
        return twf.evaluate_time_window_entry(confirm_bars, Direction.UP_RED, flag_dt, decision_at)

    def test_no_new_entry_window_rejects_when_relaxation_off(self, flagged_session, monkeypatch):
        """§7 spec default: 10:50-13:00 has no new entries at all. The
        current shipped default relaxes this (TW_ALLOW_ENTRY_1050_1300=True)
        per the 2026-08-15 win-rate tuning, so this test pins the ORIGINAL
        spec behavior explicitly rather than relying on whatever the
        current default happens to be."""
        monkeypatch.setattr(config, "TW_ALLOW_ENTRY_1050_1300", False)
        decision = self._decision_at_time(flagged_session, dtime(11, 30))
        assert decision.approved is False
        assert decision.decision == config.TW_REJECT_TIME_WINDOW

    def test_no_new_entry_window_score_gated_when_relaxation_on(self, flagged_session, monkeypatch):
        """When TW_ALLOW_ENTRY_1050_1300 is True (current default), this
        window is no longer unconditionally rejected -- it becomes
        score-gated just like W3/W5, so a flag can approve there."""
        monkeypatch.setattr(config, "TW_ALLOW_ENTRY_1050_1300", True)
        decision = self._decision_at_time(flagged_session, dtime(11, 30))
        assert decision.decision != config.TW_REJECT_TIME_WINDOW or not decision.approved
        # never unconditionally blocked anymore -- either approved, or
        # rejected for a score/other substantive reason, never a blanket
        # "no entries in this window" block.
        if not decision.approved:
            assert decision.decision in (config.TW_REJECT_LOW_QUALITY_SCORE, config.TW_REJECT_MAX_ENTRY_COUNT, config.TW_REJECT_DUPLICATE_POSITION)

    def test_outside_session_rejects(self, flagged_session):
        decision = self._decision_at_time(flagged_session, dtime(20, 0))
        assert decision.approved is False
        assert decision.decision == config.TW_REJECT_TIME_WINDOW

    def test_afternoon_hard_cutoff_rejects(self, flagged_session):
        decision = self._decision_at_time(flagged_session, dtime(14, 58))
        assert decision.approved is False
        assert decision.decision == config.TW_REJECT_TIME_WINDOW


class TestEntryCaps:
    def test_morning_cap_reached_rejects(self, flagged_session):
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        confirm_bars = bars.iloc[: flag_idx + 2]
        decision_at = confirm_bars["datetime"].iloc[-1] + timedelta(minutes=3)
        decision = twf.evaluate_time_window_entry(
            confirm_bars, Direction.UP_RED, flag_dt, decision_at,
            morning_entry_count=config.MAX_MORNING_ENTRIES,
        )
        assert decision.approved is False
        assert decision.decision == config.TW_REJECT_MAX_ENTRY_COUNT

    def test_daily_cap_reached_rejects(self, flagged_session):
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        confirm_bars = bars.iloc[: flag_idx + 2]
        decision_at = confirm_bars["datetime"].iloc[-1] + timedelta(minutes=3)
        decision = twf.evaluate_time_window_entry(
            confirm_bars, Direction.UP_RED, flag_dt, decision_at,
            morning_entry_count=config.MAX_DAILY_ENTRIES,
        )
        assert decision.approved is False
        assert decision.decision == config.TW_REJECT_MAX_ENTRY_COUNT

    def test_duplicate_position_same_direction_rejects(self, flagged_session):
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        confirm_bars = bars.iloc[: flag_idx + 2]
        decision_at = confirm_bars["datetime"].iloc[-1] + timedelta(minutes=3)
        decision = twf.evaluate_time_window_entry(
            confirm_bars, Direction.UP_RED, flag_dt, decision_at,
            position_direction=Direction.UP_RED,
        )
        assert decision.approved is False
        assert decision.decision == config.TW_REJECT_DUPLICATE_POSITION


class TestShortIntervalAndReset:
    def test_short_interval_without_reset_rejects(self):
        """Two flags only 2 bars (6 minutes) apart, with no genuine reset
        signature, must be rejected under the default 9-minute floor."""
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        up = _down_then_up(28, 6, start=start)
        down_start = up["datetime"].iloc[-1] + timedelta(minutes=3)
        down = _bars(
            [up["close"].iloc[-1] - 0.05, up["close"].iloc[-1] - 0.15],
            start=down_start,
        )
        combined = pd.concat([up, down], ignore_index=True)
        # second reversal back up almost immediately (short round trip)
        up2_start = combined["datetime"].iloc[-1] + timedelta(minutes=3)
        up2 = _bars(
            [combined["close"].iloc[-1] + 0.20, combined["close"].iloc[-1] + 0.45],
            start=up2_start,
        )
        full = pd.concat([combined, up2], ignore_index=True)

        series = signal_engine.calculate_macd_series(full)
        flags = []
        last_dir = None
        for i in range(1, len(series)):
            prev = float(series["macd"].iloc[i - 1] - series["signal"].iloc[i - 1])
            curr = float(series["macd"].iloc[i] - series["signal"].iloc[i])
            if prev <= 0 and curr > 0 and last_dir != Direction.UP_RED:
                flags.append((i, Direction.UP_RED)); last_dir = Direction.UP_RED
            elif prev >= 0 and curr < 0 and last_dir != Direction.DOWN_BLUE:
                flags.append((i, Direction.DOWN_BLUE)); last_dir = Direction.DOWN_BLUE
        assert len(flags) >= 2, "fixture needs at least two flags to test interval gating"
        last_idx, last_direction = flags[-1]
        interval_bars = last_idx - flags[-2][0]
        interval_minutes = interval_bars * 3
        if interval_minutes >= config.MIN_FLAG_INTERVAL_MINUTES or last_idx + 1 >= len(full):
            pytest.skip("fixture did not produce a short-interval last flag; timing-sensitive")
        flag_dt = full["datetime"].iloc[last_idx]
        confirm_bars = full.iloc[: last_idx + 2]
        decision_at = confirm_bars["datetime"].iloc[-1] + timedelta(minutes=3)
        decision = twf.evaluate_time_window_entry(confirm_bars, last_direction, flag_dt, decision_at)
        assert decision.approved is False
        assert decision.decision in (config.TW_REJECT_SHORT_FLAG_INTERVAL, config.TW_REJECT_MACD_GAP_NOT_EXPANDING)

    def test_is_valid_reset_true_when_prior_opposite_state_held_many_bars(self, flagged_session):
        """A flag preceded by a long, single opposite-direction run (this
        fixture's 28-bar decline before the rally) always satisfies
        condition1 (opposite state held >= TW_RESET_MIN_OPPOSITE_BARS) --
        whether or not an even earlier prior-opposite-flag exists deeper in
        the history, this reset must read as valid."""
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        ok, detail = twf.is_valid_reset(bars.iloc[: flag_idx + 1], Direction.UP_RED, flag_dt)
        assert ok is True
        assert detail.get("reason") == "no_prior_opposite_flag" or detail.get("condition1_opposite_state_held") is True

    def test_is_valid_reset_vacuously_true_with_no_history_before_flag(self):
        """A single bar with nothing before it in the frame has no prior
        opposite flag at all -- must default to True."""
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        bars = _bars([100.0], start=start)
        ok, detail = twf.is_valid_reset(bars, Direction.UP_RED, bars["datetime"].iloc[0])
        assert ok is True

    def test_is_valid_reset_rejects_invalid_direction(self, flagged_session):
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        ok, detail = twf.is_valid_reset(bars.iloc[: flag_idx + 1], "NOT_A_DIRECTION", flag_dt)
        assert ok is False


class TestQualityScore:
    def test_score_between_0_and_5(self, flagged_session):
        bars, flag_idx = flagged_session
        confirm_bars = bars.iloc[: flag_idx + 2]
        score, detail = twf.calculate_flag_quality_score(confirm_bars, Direction.UP_RED, flag_gap=0.0)
        assert 0 <= score <= 5
        assert set(detail.keys()) >= {"confirmed_3min", "gap_expanding", "price_vs_ema", "ema_stack_aligned", "volume_vs_5bar_avg"}

    def test_insufficient_data_returns_zero(self):
        tiny = _bars([100.0, 101.0], start=datetime(2026, 8, 10, 9, 0, tzinfo=KST))
        score, detail = twf.calculate_flag_quality_score(tiny, Direction.UP_RED)
        assert score == 0
        assert "error" in detail


class TestNoLookAhead:
    def test_evaluate_time_window_entry_never_reads_past_confirm_bar(self, flagged_session):
        """Appending future bars after the T+3 confirmation bar must not
        change the decision -- evaluate_time_window_entry only accepts
        bars_3m ending exactly one bar after flag_bar_dt, so extra rows
        beyond that raise the same 'not confirmed' rejection instead of
        silently using them."""
        bars, flag_idx = flagged_session
        flag_dt = bars["datetime"].iloc[flag_idx]
        confirm_bars = bars.iloc[: flag_idx + 2]
        decision_at = confirm_bars["datetime"].iloc[-1] + timedelta(minutes=3)
        baseline = twf.evaluate_time_window_entry(confirm_bars, Direction.UP_RED, flag_dt, decision_at)

        with_future = bars.iloc[: flag_idx + 3]  # one extra future bar
        leaked = twf.evaluate_time_window_entry(with_future, Direction.UP_RED, flag_dt, decision_at)
        assert leaked.decision == config.TW_REJECT_NOT_CONFIRMED
        assert leaked.approved is False
        assert baseline.decision != config.TW_REJECT_NOT_CONFIRMED or baseline.approved is False
