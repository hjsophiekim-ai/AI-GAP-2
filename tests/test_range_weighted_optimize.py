"""Tests for range_weighted_optimize module."""
from __future__ import annotations

import pandas as pd

from app.trading.range_weighted_optimize import (
    RangeWeightedConfig,
    aggregate_metrics,
    classify_intraday_regime,
    compute_objective,
    constraints_satisfied,
    daily_loss_limit_reached,
    daily_loss_limit_reached_from_pct,
    evidence_thresholds_for_regime,
    min_net_edge_for_regime,
)


def test_classify_strong_trend_day():
    df = pd.DataFrame({
        "open": [100.0] * 30,
        "high": [103.0] * 30,
        "low": [99.0] * 30,
        "close": [i * 0.1 + 100 for i in range(30)],
    })
    assert classify_intraday_regime(df) == "STRONG_TREND"


def test_classify_ambiguous_day():
    df = pd.DataFrame({
        "open": [100.0] * 30,
        "high": [100.5] * 30,
        "low": [99.5] * 30,
        "close": [100.0 + (i % 2) * 0.05 for i in range(30)],
    })
    assert classify_intraday_regime(df) == "AMBIGUOUS"


def test_daily_loss_limit():
    cfg = RangeWeightedConfig(daily_loss_limit_pct=-0.8)
    assert daily_loss_limit_reached(-80_000, 10_000_000, cfg)
    assert not daily_loss_limit_reached(-50_000, 10_000_000, cfg)
    assert daily_loss_limit_reached_from_pct(-0.85, cfg)
    assert not daily_loss_limit_reached_from_pct(-0.5, cfg)


def test_regime_thresholds():
    cfg = RangeWeightedConfig()
    amb = evidence_thresholds_for_regime("AMBIGUOUS", cfg)
    trend = evidence_thresholds_for_regime("STRONG_TREND", cfg)
    assert amb["weak"] > trend["weak"]
    assert min_net_edge_for_regime("STRONG_TREND", cfg) < min_net_edge_for_regime("AMBIGUOUS", cfg)


def test_objective_penalties():
    good = {
        "total_return_pct": 5.0,
        "max_drawdown_pct": -1.0,
        "median_cost_over_gross": 0.15,
        "wrong_direction_rate": 0.05,
        "duplicate_episode_entries": 0,
        "trend_day_miss_3pct": 0,
        "daily_loss_breaches": 0,
        "median_pf": 1.6,
        "avg_daily_return_pct": 0.2,
    }
    bad = {
        **good,
        "wrong_direction_rate": 0.20,
        "median_cost_over_gross": 0.40,
        "duplicate_episode_entries": 2,
        "daily_loss_breaches": 1,
    }
    assert compute_objective(good) > compute_objective(bad)
    assert constraints_satisfied(good)
    assert not constraints_satisfied(bad)


def test_aggregate_metrics():
    days = [
        {
            "net_pnl_krw": 100_000,
            "gross_profit_krw": 150_000,
            "total_cost_krw": 20_000,
            "entries": 2,
            "wrong_direction_entries": 0,
            "duplicate_episode_entries": 0,
            "profit_factor": 1.5,
            "cost_over_gross": 0.13,
            "return_pct": 1.0,
            "max_intraday_dd_pct": -0.5,
            "regime": "STRONG_TREND",
            "daily_loss_breached": False,
        },
        {
            "net_pnl_krw": 50_000,
            "gross_profit_krw": 80_000,
            "total_cost_krw": 10_000,
            "entries": 1,
            "wrong_direction_entries": 0,
            "duplicate_episode_entries": 0,
            "profit_factor": 1.4,
            "cost_over_gross": 0.12,
            "return_pct": 0.5,
            "max_intraday_dd_pct": -0.3,
            "regime": "NORMAL",
            "daily_loss_breached": False,
        },
    ]
    agg = aggregate_metrics(days, initial_cash=10_000_000)
    assert agg["days"] == 2
    assert agg["duplicate_episode_entries"] == 0
    assert agg["avg_daily_return_pct"] > 0
