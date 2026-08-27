"""Unit tests for TW2 (2026-08-21 사용자 요청): mutual exclusion with the TEG
filter at the service-setter level (2026-08-27: TW1 retired, TEG filter
took its former slot), the two extra entry vetoes
(time_window_filter.evaluate_tw2_extra_vetoes), and the TP2 threshold
override plumbed through time_window_position_manager. Pure-function /
service-layer tests only — no broker, no network (blocked by conftest.py
anyway)."""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, service as service_module, state_store
from app.trading.macd2 import time_window_filter as twf
from app.trading.macd2 import time_window_position_manager as twpm
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


def _warmup_then_rally(n_flat: int, n_up: int, *, start: datetime, start_price: float = 100.0) -> pd.DataFrame:
    """Enough flat bars to satisfy EMA_SLOW(26)'s minimum, then a clean
    monotonic rally so a real UP_RED crossover exists with room to spare."""
    prices = [start_price] * n_flat
    price = start_price
    for _ in range(n_up):
        price += 0.15
        prices.append(price)
    return _bars(prices, start=start)


class TestServiceTegDependency:
    def test_enabling_teg_also_enables_tw2(self):
        svc = service_module.Macd2Service()
        svc.set_time_window_2_filter_enabled(False, changed_by="test")

        res = svc.set_time_window_teg_filter_enabled(True, changed_by="test")

        assert res["ok"] is True
        assert res["time_window_teg_filter_enabled"] is True
        assert res["time_window_2_filter_enabled"] is True
        state = state_store.load_state()
        assert state.time_window_teg_filter_enabled is True
        assert state.time_window_2_filter_enabled is True

    def test_turning_tw2_off_forces_teg_off(self):
        svc = service_module.Macd2Service()
        svc.set_time_window_2_filter_enabled(True, changed_by="test")
        svc.set_time_window_teg_filter_enabled(True, changed_by="test")

        res = svc.set_time_window_2_filter_enabled(False, changed_by="test")

        assert res["time_window_2_filter_enabled"] is False
        assert res["time_window_teg_filter_enabled"] is False
        state = state_store.load_state()
        assert state.time_window_2_filter_enabled is False
        assert state.time_window_teg_filter_enabled is False

    def test_turning_teg_off_keeps_tw2_on(self):
        svc = service_module.Macd2Service()
        svc.set_time_window_teg_filter_enabled(True, changed_by="test")

        svc.set_time_window_teg_filter_enabled(False, changed_by="test")

        state = state_store.load_state()
        assert state.time_window_teg_filter_enabled is False
        assert state.time_window_2_filter_enabled is True

    def test_state_with_teg_enabled_auto_enables_tw2_if_hand_edited(self, tmp_path, monkeypatch):
        """Defensive migration check: TEG true but TW2 false loads with TW2
        enabled so the TEG sub-filter is not a dead toggle."""
        state = state_store.default_state()
        state.time_window_teg_filter_enabled = True
        state.time_window_2_filter_enabled = False
        state_store.save_state(state)

        reloaded = state_store.load_state()

        assert reloaded.time_window_2_filter_enabled is True
        assert reloaded.time_window_teg_filter_enabled is True


def _last_confirmed_flag(bars: pd.DataFrame) -> tuple[int, Direction]:
    """Locates the LAST confirmed crossover in ``bars`` via this module's
    own _gap_series/_confirmed_flag_indices — never assumes a fixed offset,
    since a fixture's actual crossover bar depends on its exact price path."""
    work = twf._prepare_bars(bars)
    series = twf._gap_series(work)
    assert series is not None, "fixture has too few bars for calculate_macd_series"
    flags = twf._confirmed_flag_indices(series)
    assert flags, "fixture produced no confirmed crossover -- fix the price path"
    return flags[-1]


def _truncate_to_confirmation_bar(bars: pd.DataFrame, flag_idx: int) -> tuple[pd.DataFrame, datetime, datetime]:
    """Mirrors worker._resolve_time_window_candidate's real convention:
    bars_3m truncated so the last row is exactly the T+3 confirmation bar
    (one bar past the flag), decision_at ~3 minutes after that bar's own
    timestamp (real wall-clock time once it has fully closed)."""
    assert flag_idx + 1 < len(bars), "fixture needs a bar after the flag for T+3"
    truncated = bars.iloc[: flag_idx + 2].reset_index(drop=True)
    flag_bar_dt = pd.Timestamp(bars["datetime"].iloc[flag_idx]).to_pydatetime()
    decision_at = pd.Timestamp(truncated["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3)
    return truncated, flag_bar_dt, decision_at


class TestTW2ExtraVetoes:
    def test_vwap_veto_rejects_when_price_far_below_vwap_for_up_red(self):
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        # Rally happens, but a final bar knocked well below the accumulated
        # VWAP -- should veto an UP_RED candidate confirmed on that bar.
        bars = _warmup_then_rally(26, 10, start=start)
        flag_idx, _direction = _last_confirmed_flag(bars)
        bars, flag_bar_dt, decision_at = _truncate_to_confirmation_bar(bars, flag_idx)
        low_row = bars.iloc[-1].copy()
        low_row["close"] = low_row["close"] * 0.97
        low_row["low"] = low_row["close"] - 0.05
        bars.iloc[-1] = low_row
        vetoed, reason = twf.evaluate_tw2_extra_vetoes(bars, Direction.UP_RED, flag_bar_dt, decision_at)
        assert vetoed is True
        assert reason == config.TW2_REJECT_VWAP_VETO

    def test_no_veto_when_price_comfortably_above_vwap_for_up_red(self):
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        bars = _warmup_then_rally(26, 15, start=start)
        flag_idx, _direction = _last_confirmed_flag(bars)
        bars, flag_bar_dt, decision_at = _truncate_to_confirmation_bar(bars, flag_idx)
        vetoed, reason = twf.evaluate_tw2_extra_vetoes(bars, Direction.UP_RED, flag_bar_dt, decision_at)
        assert vetoed is False
        assert reason is None

    def test_recent_cross_veto_rejects_during_whipsaw(self):
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        prices = [100.0] * 26
        # Oscillate hard for many bars right before the decision bar to
        # manufacture >= TW2_RECENT_CROSS_VETO_COUNT confirmed crossovers
        # inside the lookback window, then one final clean leg up so the
        # LAST confirmed flag (the candidate) is itself an UP_RED.
        price = 100.0
        for _ in range(20):
            price += 1.2
            prices.append(price)
            price -= 1.1
            prices.append(price)
        price += 3.0
        prices.append(price)
        price += 0.3
        prices.append(price)  # one extra bar past the flag for T+3
        bars = _bars(prices, start=start)
        flag_idx, direction = _last_confirmed_flag(bars)
        assert direction == Direction.UP_RED
        bars, flag_bar_dt, decision_at = _truncate_to_confirmation_bar(bars, flag_idx)
        recent = twf._count_recent_confirmed_crossovers(
            twf._prepare_bars(bars), decision_at, config.TW2_RECENT_CROSS_LOOKBACK_MINUTES,
            exclude_bar_dt=flag_bar_dt,
        )
        assert recent >= config.TW2_RECENT_CROSS_VETO_COUNT, "fixture must actually manufacture enough crossovers"
        vetoed, reason = twf.evaluate_tw2_extra_vetoes(bars, Direction.UP_RED, flag_bar_dt, decision_at)
        assert vetoed is True
        assert reason == config.TW2_REJECT_RECENT_CROSSES

    def test_recent_cross_count_excludes_the_candidate_itself(self):
        """The crossover being judged is the flag bar itself (exclude_bar_dt)
        -- it must never count itself as one of the 'recent OTHER' crosses,
        even though its own confirmation time falls inside the lookback
        window (it's only 3 minutes before decision_at)."""
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        bars = _warmup_then_rally(26, 3, start=start)
        flag_idx, _direction = _last_confirmed_flag(bars)
        bars, flag_bar_dt, decision_at = _truncate_to_confirmation_bar(bars, flag_idx)
        recent_excluding_self = twf._count_recent_confirmed_crossovers(
            twf._prepare_bars(bars), decision_at, config.TW2_RECENT_CROSS_LOOKBACK_MINUTES,
            exclude_bar_dt=flag_bar_dt,
        )
        recent_including_self = twf._count_recent_confirmed_crossovers(
            twf._prepare_bars(bars), decision_at, config.TW2_RECENT_CROSS_LOOKBACK_MINUTES,
        )
        assert recent_including_self == recent_excluding_self + 1, (
            "fixture's only crossover must be the candidate's own flag bar"
        )
        assert recent_excluding_self == 0

    def test_recent_cross_veto_ignores_premarket_whipsaw_for_first_session_flag(self):
        """2026-08-25 fix (real incident: the 09:03 confirmation of the
        day's first regular-session flag was rejected with
        TW2_REJECT_RECENT_CROSSES). Same oscillation shape as
        test_recent_cross_veto_rejects_during_whipsaw above, just shifted
        earlier so all of the whipsaw bars fall before 09:00 (premarket,
        now part of the same continuous NXT 1m history per the 2026-08-20
        fix) and the clean flag lands exactly at market open (09:00,
        confirmed 09:03). decision_at - 30min = 08:33 still overlaps the
        premarket oscillation in raw bar-time terms, so before the fix
        this still vetoed the day's first real flag on pure premarket
        noise -- exactly mirroring _session_vwap()'s own premarket
        exclusion for the OTHER TW2 veto."""
        n_flat = 26
        n_pairs = 20
        total_bars = n_flat + 2 * n_pairs + 2
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST) - timedelta(minutes=3 * (total_bars - 2))
        prices = [100.0] * n_flat
        price = 100.0
        for _ in range(n_pairs):
            price += 1.2
            prices.append(price)
            price -= 1.1
            prices.append(price)
        price += 3.0
        prices.append(price)  # flag bar -- lands exactly at 09:00
        price += 0.3
        prices.append(price)  # confirmation bar -- lands at 09:03
        bars = _bars(prices, start=start)
        flag_idx, direction = _last_confirmed_flag(bars)
        assert direction == Direction.UP_RED
        flag_bar_dt = pd.Timestamp(bars["datetime"].iloc[flag_idx]).to_pydatetime()
        assert flag_bar_dt.astimezone(KST).time() == dtime(9, 0)
        bars, flag_bar_dt, decision_at = _truncate_to_confirmation_bar(bars, flag_idx)
        assert decision_at.astimezone(KST).time() == dtime(9, 6)
        recent = twf._count_recent_confirmed_crossovers(
            twf._prepare_bars(bars), decision_at, config.TW2_RECENT_CROSS_LOOKBACK_MINUTES,
            exclude_bar_dt=flag_bar_dt,
        )
        assert recent == 0, "premarket crossovers must never count toward the intraday whipsaw veto"
        vetoed, reason = twf.evaluate_tw2_extra_vetoes(bars, Direction.UP_RED, flag_bar_dt, decision_at)
        assert vetoed is False
        assert reason is None

    def test_unknown_direction_never_vetoes(self):
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        bars = _warmup_then_rally(26, 5, start=start)
        flag_idx, _direction = _last_confirmed_flag(bars)
        bars, flag_bar_dt, decision_at = _truncate_to_confirmation_bar(bars, flag_idx)
        vetoed, reason = twf.evaluate_tw2_extra_vetoes(bars, "not_a_direction", flag_bar_dt, decision_at)
        assert vetoed is False
        assert reason is None

    def test_insufficient_bars_never_vetoes(self):
        start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
        bars = _bars([100.0, 100.5], start=start)
        flag_bar_dt = pd.Timestamp(bars["datetime"].iloc[0]).to_pydatetime()
        decision_at = pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3)
        vetoed, reason = twf.evaluate_tw2_extra_vetoes(bars, Direction.UP_RED, flag_bar_dt, decision_at)
        assert vetoed is False
        assert reason is None


class TestTP2Override:
    def test_evaluate_morning_position_default_tp2_unchanged(self):
        decision = twpm.evaluate_morning_position(net_return_pct=5.5, tp1_done=True, peak_net_return=5.5)
        assert decision.exit_reason == config.EXIT_TW_TP2_FULL

    def test_evaluate_morning_position_override_raises_threshold(self):
        # 5.5% would trigger TW1's default 5.0% TP2, but must NOT trigger
        # when tp2_pct_override=6.0 is supplied.
        decision = twpm.evaluate_morning_position(
            net_return_pct=5.5, tp1_done=True, peak_net_return=5.5, tp2_pct_override=6.0,
        )
        assert decision.exit_reason != config.EXIT_TW_TP2_FULL
        decision_at_6 = twpm.evaluate_morning_position(
            net_return_pct=6.1, tp1_done=True, peak_net_return=6.1, tp2_pct_override=6.0,
        )
        assert decision_at_6.exit_reason == config.EXIT_TW_TP2_FULL

    def test_evaluate_take_profit_immediate_override(self):
        decision = twpm.evaluate_take_profit_immediate(
            session="MORNING", net_return_pct=5.5, tp1_done=True, tp2_pct_override=6.0,
        )
        assert decision.exit_reason is None
        decision_full = twpm.evaluate_take_profit_immediate(
            session="MORNING", net_return_pct=6.2, tp1_done=True, tp2_pct_override=6.0,
        )
        assert decision_full.exit_reason == config.EXIT_TW_TP2_FULL

    def test_evaluate_position_dispatcher_passes_override_through_for_morning_only(self):
        morning = twpm.evaluate_position(
            session="MORNING", net_return_pct=5.5, tp1_done=True, peak_net_return=5.5, tp2_pct_override=6.0,
        )
        assert morning.exit_reason != config.EXIT_TW_TP2_FULL

        # AFTERNOON_TP must be completely unaffected by a MORNING TP2 override.
        afternoon_default = twpm.evaluate_afternoon_position(net_return_pct=3.0, peak_net_return=3.0)
        afternoon_with_override = twpm.evaluate_position(
            session="AFTERNOON", net_return_pct=3.0, tp1_done=False, peak_net_return=3.0, tp2_pct_override=6.0,
        )
        assert afternoon_with_override.exit_reason == afternoon_default.exit_reason

    def test_no_override_matches_pre_existing_mu_macd_call_shape(self):
        """MU_MACD (and any other caller) never passes tp2_pct_override --
        must behave exactly as before this feature existed."""
        decision = twpm.evaluate_position(session="MORNING", net_return_pct=5.1, tp1_done=True, peak_net_return=5.1)
        assert decision.exit_reason == config.EXIT_TW_TP2_FULL
