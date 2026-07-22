"""Regression: structural live direction + drawdown gates vs Enhanced bias."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

import app.services.hynix_switch_engine as engine
from app.trading import early_trend_live_feed as feed


def _declining_df(n: int = 45) -> pd.DataFrame:
    rows = []
    t0 = datetime(2026, 7, 22, 10, 0, 0)
    price = 100.0
    for i in range(20):
        price += 0.4
        rows.append({
            "datetime": t0 + timedelta(minutes=i),
            "open": price - 0.1, "high": price + 0.2, "low": price - 0.2,
            "close": price, "volume": 1000,
        })
    for i in range(20, n):
        price -= 0.25
        rows.append({
            "datetime": t0 + timedelta(minutes=i),
            "open": price + 0.1, "high": price + 0.15, "low": price - 0.2,
            "close": price, "volume": 1200,
        })
    return pd.DataFrame(rows)


def test_structural_down_blocks_leverage_despite_high_enhanced_score():
    df = _declining_df()
    now = datetime(2026, 7, 22, 10, 44, 0)
    structural = feed.compute_structural_live_direction(
        df,
        etf_window_directions={5: "DOWN", 10: "DOWN", 20: "UP"},
        now=now,
    )
    assert structural["direction"] == "DOWN"
    merged = feed.merge_live_trade_direction({"direction": "UP"}, structural)
    assert merged["direction"] == "DOWN"

    gates = feed.compute_session_drawdown_gates(df, now=now)
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 92.0, "inverse_pressure_score": 20.0, "final_action": "HYNIX_STRONG_BUY"},
        direction="UP",
        live_direction=merged["direction"],
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        signal_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=1.0,
        expected_move_pct=1.0,
        cost_pct=0.1,
        expected_mfe_pct=1.0,
        expected_mae_pct=0.3,
        drawdown_gates=gates,
    )
    # Live DOWN vs desired UP → conflict, or drawdown forbid.
    assert result["action"] == "HOLD"
    assert result["reason_code"] in (
        "LIVE_DIRECTION_CONFLICT",
        "DRAWDOWN_FORBID_HYNIX_BUY",
        "ETF_5S_10S_BOTH_OPPOSITE",
    )


def test_drawdown_forbid_hynix_buy_hard_block():
    result = engine.evaluate_range_weighted_entry(
        decision={"enhanced_score": 80.0, "inverse_pressure_score": 40.0, "final_action": "HYNIX_BUY"},
        direction="UP",
        live_direction="UP",
        confirm_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        signal_window_directions={5: "UP", 10: "UP", 20: "UP", 30: "UP"},
        oppose_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"},
        confirm_above_vwap=True,
        data_age_seconds=1.0,
        expected_move_pct=1.0,
        cost_pct=0.1,
        expected_mfe_pct=1.0,
        expected_mae_pct=0.3,
        drawdown_gates={"forbid_hynix_buy": True, "forbid_inverse_buy": False},
    )
    assert result["action"] == "HOLD"
    assert result["reason_code"] == "DRAWDOWN_FORBID_HYNIX_BUY"


def test_one_two_bar_rebound_does_not_clear_structural_down():
    df = _declining_df(n=42)
    # Append 2 rebound bars
    last = df.iloc[-1]
    t = last["datetime"]
    extra = []
    price = float(last["close"])
    for i, bounce in enumerate((0.3, 0.2)):
        price = price + bounce
        extra.append({
            "datetime": t + timedelta(minutes=i + 1),
            "open": price - bounce, "high": price + 0.05, "low": price - 0.15,
            "close": price, "volume": 1000,
        })
    df2 = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
    structural = feed.compute_structural_live_direction(
        df2,
        etf_window_directions={5: "DOWN", 10: "DOWN", 20: "DOWN"},
        now=df2.iloc[-1]["datetime"],
    )
    # Still DOWN or at least not UP from 2 rebound bars alone
    assert structural["direction"] != "UP"
