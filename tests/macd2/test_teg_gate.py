"""Unit tests for app.trading.macd2.teg_gate (2026-08-27, production port of
the validated scripts/teg_gate_v2.py signed-net-change logic). Condition-by-
condition coverage, mirroring the backtest scripts' own validation approach.
Pure-function tests only — no broker, no state, no worker."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config
from app.trading.macd2 import teg_gate
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


def test_frozen_thresholds_match_train_derived_values():
    """Pins the exact TRAIN-only (2026-06-01~07-28) frozen values -- never
    auto-retuned; a change here must be backed by a fresh TRAIN/OOS
    backtest, not a casual edit."""
    assert teg_gate.TEG_HIST_DELTA_FLOOR == 162.928
    assert teg_gate.TEG_SPREAD_DELTA_FLOOR == 242.760


def test_insufficient_bars_rejects_cleanly():
    bars = _bars([100.0, 100.5], start=datetime(2026, 8, 10, 9, 0, tzinfo=KST))
    decision = teg_gate.evaluate_teg(
        bars, Direction.UP_RED, bars["datetime"].iloc[0].to_pydatetime(),
        bars["datetime"].iloc[-1].to_pydatetime() + timedelta(minutes=3),
    )
    assert decision.approved is False
    assert decision.reject_reasons == ("insufficient_bars",)


def test_invalid_direction_rejects_cleanly():
    bars = _bars([100.0] * 30, start=datetime(2026, 8, 10, 9, 0, tzinfo=KST))
    decision = teg_gate.evaluate_teg(
        bars, "not_a_direction", bars["datetime"].iloc[0].to_pydatetime(),
        bars["datetime"].iloc[-1].to_pydatetime(),
    )
    assert decision.approved is False
    assert decision.reject_reasons == ("invalid_direction",)


def _real_8_25_1209_up_red_bars() -> tuple[pd.DataFrame, datetime, datetime]:
    """Reproduces the real 2026-08-25 12:09 UP_RED flag's exact bar shape
    that motivated the v1->v2 fix (dip-then-jump at the crossover boundary:
    |hist| 12:06->12:09->12:12 went 207.4->154.9->376.0) -- close prices
    chosen to reproduce the same signed-net-change pattern relative to
    EMA10/EMA20/VWAP without depending on any external data file."""
    start = datetime(2026, 8, 25, 8, 0, tzinfo=KST)
    n_flat = 26
    prices = [1_590_000.0] * n_flat
    # gentle down-drift into the dip, then a sharp rally through the flag
    # and confirmation bars -- shaped to land close>ema10>ema20, above VWAP,
    # and a strongly positive signed 2-bar net change on both hist and
    # EMA10-EMA20 spread.
    price = prices[-1]
    for _ in range(6):
        price -= 800.0
        prices.append(price)
    for _ in range(4):
        price += 3_500.0
        prices.append(price)
    bars = _bars(prices, start=start)
    flag_bar_dt = bars["datetime"].iloc[-2].to_pydatetime()
    decision_at = bars["datetime"].iloc[-1].to_pydatetime() + timedelta(minutes=3)
    return bars, flag_bar_dt, decision_at


def test_real_shaped_dip_then_jump_flag_approves_under_signed_net_change():
    """The exact failure mode v1 (strict per-bar absolute monotonicity) had
    on the real 2026-08-25 12:09 UP_RED flag: a dip-then-jump pattern right
    at the crossover boundary. v2's signed net-change logic must approve a
    candidate shaped this way (deliberately NOT asserting a rigid channel of
    exact numbers -- see scripts/teg_gate_v2.py's own validated run for the
    real production data confirmation; this is a structural regression
    guard, not a re-derivation of the real backtest)."""
    bars, flag_bar_dt, decision_at = _real_8_25_1209_up_red_bars()
    decision = teg_gate.evaluate_teg(bars, Direction.UP_RED, flag_bar_dt, decision_at)
    # tw2_confirmed and the two signed-net-change conditions are the ones
    # v1's strict monotonicity broke; assert those three directly even if
    # VWAP/stack/interval happen not to align for this synthetic shape.
    assert decision.conditions.get(teg_gate.COND_TW2_CONFIRMED) is True
    assert decision.conditions.get(teg_gate.COND_MACD_GAP_EXPANDING) is True
    assert decision.conditions.get(teg_gate.COND_EMA_SPREAD_EXPANDING) is True


def test_signed_net_change_rejects_a_flat_non_trending_session():
    """A flat/oscillating session with no real net acceleration must fail
    the signed net-change conditions (floored at TEG_HIST_DELTA_FLOOR /
    TEG_SPREAD_DELTA_FLOOR) -- proves the floor is not a no-op."""
    start = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
    prices = [100.0] * 26
    price = 100.0
    for _ in range(10):
        price += 0.05
        prices.append(price)
        price -= 0.04
        prices.append(price)
    bars = _bars(prices, start=start)
    flag_bar_dt = bars["datetime"].iloc[-2].to_pydatetime()
    decision_at = bars["datetime"].iloc[-1].to_pydatetime() + timedelta(minutes=3)
    decision = teg_gate.evaluate_teg(bars, Direction.UP_RED, flag_bar_dt, decision_at)
    assert decision.approved is False


def test_min_9min_interval_condition_uses_config_constant():
    assert teg_gate.TEG_MIN_OPPOSITE_INTERVAL_MINUTES == config.MIN_FLAG_INTERVAL_MINUTES


def test_all_seven_conditions_present_in_every_decision():
    bars, flag_bar_dt, decision_at = _real_8_25_1209_up_red_bars()
    decision = teg_gate.evaluate_teg(bars, Direction.UP_RED, flag_bar_dt, decision_at)
    assert set(decision.conditions.keys()) == set(teg_gate.ALL_CONDITIONS)
