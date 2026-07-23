"""A vs C replay parity when opening probe does not fire.

Contract: with `open_probe_fired=False`, IMMEDIATE_50_THEN_CONFIRM (C) must match
NEW_TURN_ONLY (A) on signals, round-trips, and net PnL. See
`data/state/macd_opening_abc_parity_audit.md` for the 2026-07-22 audit.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

from scripts.compare_macd_opening_abc_20d import (
    STRATEGIES,
    replay_day,
    summarize,
)
from scripts.compare_macd_vs_williams_early_20d import build_day_universe


ROOT = Path(__file__).resolve().parent.parent
COMPARE_JSON = ROOT / "data" / "state" / "macd_opening_abc_20d_compare.json"


def _trade_keys(trades: list) -> list[tuple]:
    return [(t.day, t.signal_time, t.direction, t.symbol) for t in trades]


def _replay_pair(day: str, dates: list[str], day_data: dict) -> tuple:
    ds_a = replay_day(STRATEGIES[0], day, day_data, dates)
    ds_c = replay_day(STRATEGIES[2], day, day_data, dates)
    return ds_a, ds_c


@pytest.fixture(scope="module")
def day_universe():
    dates, _, day_data = build_day_universe(20, refetch_naver=False)
    return dates, day_data


def test_opening_abc_parity_on_non_903_signal_day(day_universe):
    """Days without 09:03 new_signal must already match (rolling 20d sample).

    Date is chosen from the current universe (not hardcoded) so the test does not
    fail when the oldest day rolls off the 20-session window.
    """
    dates, day_data = day_universe
    day = None
    ds_a = ds_c = None
    for candidate in dates:
        a, c = _replay_pair(candidate, dates, day_data)
        if c.open_probe_fired is False:
            day, ds_a, ds_c = candidate, a, c
            break
    assert day is not None, f"no non-probe day in universe {dates}"
    assert ds_c.open_probe_fired is False
    assert _trade_keys(ds_a.trades) == _trade_keys(ds_c.trades)
    assert round(sum(t.net_pnl for t in ds_a.trades), 2) == round(
        sum(t.net_pnl for t in ds_c.trades), 2
    )


def test_opening_abc_parity_when_probe_off_jul13(day_universe):
    """Jul 13: probe off but 09:03 new_signal — C must not diverge from A."""
    dates, day_data = day_universe
    day = "2026-07-13"
    ds_a, ds_c = _replay_pair(day, dates, day_data)

    assert ds_c.open_probe_fired is False
    assert ds_c.open_probe_success is False
    assert _trade_keys(ds_a.trades) == _trade_keys(ds_c.trades), (
        "C skips 09:03 new_signal even when probe never fired"
    )
    assert len(ds_a.trades) == len(ds_c.trades)
    assert round(sum(t.net_pnl for t in ds_a.trades), 2) == round(
        sum(t.net_pnl for t in ds_c.trades), 2
    )


def test_opening_abc_parity_aggregate_when_probe_attempts_zero(day_universe):
    """Full 20d: if C records zero probe attempts, aggregate RT/Net must match A."""
    dates, day_data = day_universe
    trades_a, trades_c = [], []
    stats_a, stats_c = [], []
    probe_fired_days = []

    for day in dates:
        ds_a, ds_c = _replay_pair(day, dates, day_data)
        trades_a.extend(ds_a.trades)
        trades_c.extend(ds_c.trades)
        stats_a.append(ds_a)
        stats_c.append(ds_c)
        if ds_c.open_probe_fired:
            probe_fired_days.append(day)

    assert probe_fired_days == []
    sum_a = summarize(trades_a, stats_a)
    sum_c = summarize(trades_c, stats_c)

    assert sum_c["open_probe_attempts"] == 0
    assert sum_a["round_trips"] == sum_c["round_trips"]
    assert sum_a["net"] == sum_c["net"]


@pytest.mark.skipif(not COMPARE_JSON.exists(), reason="compare JSON not generated")
def test_opening_abc_parity_compare_artifact_matches_when_probe_zero():
    """After fix: 20d compare artifact must show A≡C when probe attempts = 0."""
    import json

    data = json.loads(COMPARE_JSON.read_text(encoding="utf-8"))

    def key(t):
        return (t["day"], t["signal_time"], t["direction"], t["symbol"])

    a_keys = {key(t) for t in data["A"]["trades"]}
    c_keys = {key(t) for t in data["C"]["trades"]}

    assert data["C"]["open_probe_attempts"] == 0
    assert sorted(a_keys) == sorted(c_keys)
    assert data["A"]["round_trips"] == data["C"]["round_trips"]
    assert data["A"]["net"] == data["C"]["net"]
    assert data["adoption"]["verdict"] == "INSUFFICIENT_SAMPLE"
