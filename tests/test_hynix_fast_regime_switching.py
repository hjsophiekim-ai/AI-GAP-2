from datetime import datetime

import pandas as pd

from app.models.hynix_action_decider import decide_hynix_or_inverse_action
from app.models.hynix_enhanced_score import _live_order_weights
from app.services.hynix_switch_engine import evaluate_pullback_gate
from app.trading.hynix_fast_trend import compute_fast_trend_signal
from app.trading.hynix_switch_position_manager import run_switch_or_entry


def _df(prices):
    return pd.DataFrame(
        {
            "datetime": pd.date_range("2026-07-14 09:00", periods=len(prices), freq="min"),
            "close": prices,
            "volume": [1000 + i * 10 for i in range(len(prices))],
        }
    )


def test_up_down_up_fast_trend_switch_sequence():
    up1 = compute_fast_trend_signal(_df([100, 101, 102, 103, 104, 105, 106]))
    down = compute_fast_trend_signal(_df([106, 105, 104, 103, 102, 101, 100]))
    up2 = compute_fast_trend_signal(_df([100, 101, 102, 103, 104, 105, 106]))

    assert up1["direction"] == "UP"
    assert down["direction"] == "DOWN"
    assert up2["direction"] == "UP"


def test_general_signal_two_confirmations_skip_pullback():
    state = {
        "trend_switch_confirm_tracker": {
            "direction": "HYNIX",
            "same_direction_streak": 1,
            "reversal_streak": 0,
            "reversal_against_symbol": None,
            "_state_date": "20260714",
        },
        "trend_switch_frequency_state": {"round_trips_today": 0, "consecutive_losses": 0, "_state_date": "20260714"},
    }
    gate = evaluate_pullback_gate(
        state,
        "000660",
        "HYNIX_BUY",
        datetime(2026, 7, 14, 10, 0),
        {},
        _df([100, 101, 102, 103, 104, 105, 106]),
        "mock",
    )
    assert gate["proceed"] is True
    assert state["last_trend_switch_plan"]["entry_type"] == "EXPLORATORY"
    assert state["last_trend_switch_plan"]["position_pct"] == 0.20


def test_stale_micron_weight_zero_for_live_orders():
    weights = _live_order_weights(
        {"base_prediction": 0.45, "existing_micron": 0.20, "hynix_technical": 0.25, "intraday_momentum": 0.10},
        {"micron_data_status": "STALE_DATA", "micron_last_update_time": "2026-07-14T08:00:00"},
    )
    assert weights["existing_micron"] == 0.0
    assert weights["hynix_technical"] + weights["intraday_momentum"] >= 0.80


def test_inverse_blocked_during_live_hynix_uptrend():
    result = decide_hynix_or_inverse_action(
        {
            "enhanced_score": 20.0,
            "inverse_pressure_score": 80.0,
            "existing_micron_score": 50.0,
            "hynix_technical_score": 50.0,
            "data_valid": {"base_prediction": True, "hynix_technical": True},
            "fast_live_trend": {
                "above_vwap": True,
                "returns": {"3m": 0.5, "5m": 0.8},
                "ema_slope_pct": 0.1,
            },
        }
    )
    assert result["final_action"] == "HOLD"
    assert any("blocks new INVERSE" in r for r in result["reasons"])


def test_duplicate_order_prevention_same_cycle():
    class Broker:
        def get_buyable_cash(self):
            return 1_000_000

        def buy(self, *args, **kwargs):
            return {"success": True, "bought_quantity": 2, "actual_quantity": 2, "order_id": "b1"}

        def get_positions(self):
            return [{"symbol": "000660", "quantity": 2, "avg_price": 100_000, "market_value": 200_000}]

    now = datetime(2026, 7, 14, 10, 0)
    state = {"mode": "mock", "position": {}, "last_trend_switch_plan": {"desired_symbol": "000660", "proceed": True}}
    first = run_switch_or_entry(state, Broker(), "HYNIX_BUY", 100_000, 5_000, now=now)
    second = run_switch_or_entry(state, Broker(), "HYNIX_BUY", 100_000, 5_000, now=now)

    assert first["acted"] is True
    assert second["acted"] is False
