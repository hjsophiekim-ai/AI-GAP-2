"""Tests for simplified A/C/D/E strategy architecture."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.macd_williams_episode import (
    confirm_episode_direction,
    enhanced_may_set_direction,
    resample_completed_3m,
)
from app.trading.strategy_architecture import (
    EPISODE_GATE_MODE_LIVE,
    EPISODE_GATE_MODE_SHADOW,
    chase_hard_block,
    entry_timing_ok,
    episode_gate_blocks_entry,
    get_episode_gate_mode,
    price_action_may_place_live_order,
    price_action_shadow_payload,
)


def _synthetic_1m(n: int = 90, trend: float = 1.0) -> pd.DataFrame:
    start = datetime(2026, 7, 21, 9, 0)
    rows = []
    price = 100.0
    for i in range(n):
        price += trend * 0.05
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": price - 0.02,
            "high": price + 0.1,
            "low": price - 0.1,
            "close": price,
            "volume": 1000,
        })
    return pd.DataFrame(rows)


def test_price_action_never_live_order():
    assert price_action_may_place_live_order() is False
    shadow = price_action_shadow_payload(direction="UP", factors={"a": True}, factor_count=1)
    assert shadow["live_order_forbidden"] is True
    assert shadow["mode"] == "SHADOW"


def test_entry_timing_and_chase():
    ok, reason = entry_timing_ok(3.0)
    assert ok is False and reason == "TIMING_TOO_EARLY"
    ok, _ = entry_timing_ok(8.0)
    assert ok is True
    assert chase_hard_block(0.7) is True
    assert chase_hard_block(0.3) is False


def test_episode_gate_shadow_does_not_block():
    confirm = {"confirmed": False}
    assert episode_gate_blocks_entry(EPISODE_GATE_MODE_SHADOW, confirm) is False
    assert episode_gate_blocks_entry(EPISODE_GATE_MODE_LIVE, confirm) is True
    assert episode_gate_blocks_entry(EPISODE_GATE_MODE_LIVE, {"confirmed": True}) is False


def test_default_gate_is_shadow():
    assert get_episode_gate_mode({}) == EPISODE_GATE_MODE_SHADOW
    assert get_episode_gate_mode({"macd_williams_episode_gate_mode": "LIVE"}) == EPISODE_GATE_MODE_LIVE


def test_completed_3m_excludes_incomplete_bar():
    df = _synthetic_1m(20)
    now = datetime(2026, 7, 21, 9, 10, 30)  # mid unfinished 3m bar starting 09:09
    bars = resample_completed_3m(df, now=now)
    assert not bars.empty
    # last completed bar must end at or before 09:09
    last_start = bars["datetime"].iloc[-1]
    assert last_start + timedelta(minutes=3) <= now.replace(second=0, microsecond=0)


def test_enhanced_blocked_when_episode_opposite():
    confirm = {
        "confirmed": False,
        "indicator_direction": "DOWN",
        "blocks_enhanced_override": True,
    }
    assert enhanced_may_set_direction(confirm, enhanced_leader="UP", live_direction=None) is False
    assert enhanced_may_set_direction(confirm, enhanced_leader="DOWN", live_direction=None) is True


def test_confirm_episode_direction_returns_no_broker_flag():
    df = _synthetic_1m(100, trend=1.0)
    now = datetime(2026, 7, 21, 10, 30)
    result = confirm_episode_direction(df, proposed_direction="UP", now=now)
    assert result["broker_order_allowed"] is False
    assert "reason" in result
