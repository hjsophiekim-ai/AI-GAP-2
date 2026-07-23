"""Formula parity between MACD2's independent implementation and MACD v1.

MACD2's signal_engine.py / risk_exit.py do NOT import from
app.trading.macd_hynix_strategy (see docs/MACD2_LOGIC.md and the 2026-07-23
design decision: MACD2 must be fully independent of MACD v1). v1 is imported
here, in this test file only, purely as a reference oracle to prove MACD2's
independently-written formulas produce numerically identical results.

IMPORTANT — scope of this parity check: the real, historically-verified
7/21 and 7/22 signed-B timelines live in data/cache/replay_*.csv, which were
deleted in the 2026-07-23 incident and are still pending Google Drive/OneDrive
recovery at the time this file was written. That real-timeline parity check
is BLOCKED_DATA_MISSING and is not performed here — do not read this file's
passing tests as satisfying docs §19's "7/21·7/22 timeline diff=0"
requirement. What IS verified here, using synthetic (not historical) 1-minute
bars: the 3m resample boundary, the EMA12/26/9 (adjust=False) MACD/Signal/
Histogram math, the signed-B pattern classification, and the stop-loss /
Profit Lock decisions are bit-for-bit identical between MACD2 and v1 across
a full synthetic session. Once the replay CSVs are recovered, a follow-up
test should feed them through both implementations the same way.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

# Reference-only import — v1 is never imported by MACD2 production code.
from app.trading import macd_hynix_strategy as v1
from app.trading.macd2 import config
from app.trading.macd2.models import Direction
from app.trading.macd2.risk_exit import (
    check_stop_loss,
    evaluate_position_exits,
    update_profit_lock_tracker,
)
from app.trading.macd2.signal_engine import calculate_macd, evaluate_signed_b, resample_completed_3m

KST = config.KST


def _synthetic_1m_closes(n_minutes: int) -> list[float]:
    """Deterministic sine-wave price path — not historical data.

    A pure linear ramp makes the MACD histogram converge to a near-constant
    (delta -> 0), which never satisfies signed-B's "two consecutive positive/
    negative deltas" condition. A sine wave's concave/convex phases give
    genuine sustained multi-bar histogram deltas in both directions, long
    enough (>=90 completed 3m bars) to exercise warm-up plus both a UP_RED-
    style run and a DOWN_BLUE-style reversal.
    """
    amplitude = 20.0
    period = n_minutes  # one full cycle across the whole synthetic session
    closes = [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]
    return closes


def _build_1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        dt = start + timedelta(minutes=i)
        rows.append({
            "datetime": dt, "open": close, "high": close + 0.1,
            "low": close - 0.1, "close": close, "volume": 100,
        })
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def synthetic_session():
    start = datetime(2026, 1, 5, 9, 0, tzinfo=KST)  # arbitrary weekday, not a real trading day
    closes = _synthetic_1m_closes(300)  # 300 x 1m -> 100 x 3m, matches docs warm-up minimums
    now = start + timedelta(minutes=len(closes) + 5)
    df_1m = _build_1m_frame(start, closes)
    return start, now, df_1m


def test_3m_resample_matches_v1_bar_for_bar(synthetic_session):
    _start, now, df_1m = synthetic_session

    v1_bars = v1.resample_completed_3m(df_1m, now=now)
    macd2_bars = resample_completed_3m(df_1m, now=now)

    assert len(v1_bars) == len(macd2_bars)
    assert len(v1_bars) >= 90  # sanity: this synthetic session should yield ~100 completed 3m bars
    pd.testing.assert_series_equal(
        v1_bars["datetime"].reset_index(drop=True),
        macd2_bars["datetime"].reset_index(drop=True),
        check_names=False,
    )
    for col in ("open", "high", "low", "close"):
        pd.testing.assert_series_equal(
            v1_bars[col].reset_index(drop=True),
            macd2_bars[col].reset_index(drop=True),
            check_names=False,
        )


def test_macd_signal_hist_match_v1_at_every_bar(synthetic_session):
    _start, now, df_1m = synthetic_session

    v1_bars = v1.resample_completed_3m(df_1m, now=now)
    macd2_bars = resample_completed_3m(df_1m, now=now)

    v1_closes = pd.to_numeric(v1_bars["close"], errors="coerce").dropna()
    v1_comps = v1.macd_components(v1_closes)
    assert v1_comps["hist"] is not None

    mismatches = []
    for end in range(config.EMA_SLOW, len(macd2_bars) + 1):
        window = macd2_bars.iloc[:end]
        snap = calculate_macd(window)
        assert snap is not None
        idx = end - 1
        v1_macd = round(float(v1_comps["macd"].iloc[idx]), 6)
        v1_signal = round(float(v1_comps["signal"].iloc[idx]), 6)
        v1_hist = round(float(v1_comps["hist"].iloc[idx]), 6)
        if (snap.macd, snap.signal, snap.hist) != (v1_macd, v1_signal, v1_hist):
            mismatches.append((idx, snap.macd, v1_macd, snap.signal, v1_signal, snap.hist, v1_hist))

    assert mismatches == [], f"MACD2 vs v1 diverged at {len(mismatches)} bar(s): {mismatches[:5]}"


def test_signed_b_pattern_matches_v1_at_every_bar(synthetic_session):
    _start, now, df_1m = synthetic_session
    macd2_bars = resample_completed_3m(df_1m, now=now)

    mismatches = []
    for end in range(config.EMA_SLOW, len(macd2_bars) + 1):
        window = macd2_bars.iloc[:end]
        snap = calculate_macd(window)
        assert snap is not None
        h2, h1, h0 = snap.hist_last3

        v1_pattern_raw = v1.signed_hist_two_turn_pattern(h0, h1, h2)
        # previous_direction=None disables MACD2's repeat-signal suppression,
        # isolating the raw pattern math for a fair bar-by-bar comparison.
        macd2_pattern = evaluate_signed_b(snap, previous_direction=None)

        expected = Direction(v1_pattern_raw)
        if macd2_pattern != expected:
            mismatches.append((end - 1, v1_pattern_raw, macd2_pattern.value))

    assert mismatches == [], f"signed-B pattern diverged at {len(mismatches)} bar(s): {mismatches[:5]}"
    # Sanity: the synthetic uptrend/downtrend must have actually produced both flags.
    directions_seen = set()
    for end in range(config.EMA_SLOW, len(macd2_bars) + 1):
        snap = calculate_macd(macd2_bars.iloc[:end])
        directions_seen.add(evaluate_signed_b(snap, previous_direction=None))
    assert Direction.UP_RED in directions_seen
    assert Direction.DOWN_BLUE in directions_seen


@pytest.mark.parametrize(
    "current,peak,active",
    [
        (-2.0, 0.0, False),
        (-1.5, 0.0, False),
        (0.5, 0.5, False),
        (1.5, 1.5, False),
        (1.5, 1.5, True),
        (3.4, 4.2, True),
        (3.41, 4.2, True),
        (5.0, 5.0, True),
    ],
)
def test_risk_exit_matches_v1_across_boundary_scenarios(current, peak, active):
    # Compare on the raw pct directly (both v1.update_profit_lock_tracker and
    # MACD2's version take current/peak_net_return as plain floats). v1's
    # price-based evaluate_position_exits()/check_tp_sl() are deliberately
    # NOT used here — they re-derive pct from entry/current price via
    # TradeCostEngine, which would round-trip through fees and no longer
    # equal the exact `current` value under test (an apples-to-oranges
    # comparison, not a real parity check of the decision formula itself).
    v1_tracker = v1.update_profit_lock_tracker(
        current_net_return=current, peak_net_return=peak, profit_lock_active=active,
    )
    v1_sl_hit = current <= v1.SL_NET_PCT
    v1_reason = v1.EXIT_SL if v1_sl_hit else v1_tracker["exit_reason"]

    macd2_tracker = update_profit_lock_tracker(
        current_net_return=current, peak_net_return=peak, profit_lock_active=active,
    )
    macd2_sl = check_stop_loss(current)
    macd2_exits = evaluate_position_exits(
        current_net_return=current, peak_net_return=peak, profit_lock_active=active,
    )

    assert macd2_tracker.peak_net_return == v1_tracker["peak_net_return"]
    assert macd2_tracker.giveback_pct == v1_tracker["giveback_pct"]
    assert macd2_tracker.profit_lock_active == v1_tracker["profit_lock_active"]
    assert macd2_sl == v1_sl_hit == (current <= config.STOP_LOSS_NET_PCT)

    # v1 exit_reason strings differ from MACD2's (docs §17 ledger vocabulary), so
    # compare by exit *category*, not by the literal string.
    if v1_reason == v1.EXIT_SL:
        assert macd2_exits.exit_reason == config.EXIT_STOP_LOSS
    elif v1_reason == v1.EXIT_PROFIT_LOCK:
        assert macd2_exits.exit_reason == config.EXIT_PROFIT_LOCK
    else:
        assert macd2_exits.exit_reason is None
