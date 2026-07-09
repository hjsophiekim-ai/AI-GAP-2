"""
test_dynamic_exit_engine.py — DynamicExitEngine의 시장유형 분류/TP·SL/Trailing/
Profit Lock/Exit Score 동작 검증(6종: LOW_VOLATILITY/NORMAL/HIGH_VOLATILITY/
TREND_UP/TREND_DOWN/PANIC + SHORT_SQUEEZE 분류 포함).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.trading.dynamic_exit_engine import (
    DynamicExitEngine, LOW_VOLATILITY, NORMAL, HIGH_VOLATILITY, TREND_UP, TREND_DOWN, PANIC, SHORT_SQUEEZE,
)
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL


@pytest.fixture
def engine():
    return DynamicExitEngine()


def _neutral_snapshot(**overrides):
    base = {
        "atr_14_pct": 1.8, "atr_5_pct": 1.8, "rsi_14": 50.0, "macd_histogram": 0.0, "macd_histogram_prev": 0.0,
        "bollinger_width_pct": 4.0, "bollinger_upper": 101000.0, "bollinger_lower": 99000.0,
        "williams_r": -50.0, "stochastic_k": 50.0, "stochastic_d": 50.0,
        "vwap": 100000.0, "vwap_distance_pct": 0.0, "current_price": 100000.0,
        "relative_volume": 1.0, "return_3m_pct": 0.0, "return_5m_pct": 0.0,
        "held_minutes": 5.0, "profit_pct": 0.5,
    }
    base.update(overrides)
    return base


# ── 시장유형 분류 ────────────────────────────────────────────────────────────

def test_classify_normal(engine):
    assert engine.classify_market(_neutral_snapshot()) == NORMAL


def test_classify_low_volatility(engine):
    snap = _neutral_snapshot(atr_14_pct=0.5, bollinger_width_pct=1.0, return_5m_pct=0.05)
    assert engine.classify_market(snap) == LOW_VOLATILITY


def test_classify_high_volatility(engine):
    snap = _neutral_snapshot(atr_14_pct=4.0, bollinger_width_pct=9.0, return_5m_pct=0.9)
    assert engine.classify_market(snap) == HIGH_VOLATILITY


def test_classify_trend_up(engine):
    snap = _neutral_snapshot(macd_histogram=2.0, vwap_distance_pct=1.5, rsi_14=75.0, return_5m_pct=1.2, return_3m_pct=0.4, relative_volume=1.2)
    assert engine.classify_market(snap) == TREND_UP


def test_classify_trend_down(engine):
    snap = _neutral_snapshot(macd_histogram=-2.0, vwap_distance_pct=-1.5, rsi_14=25.0, return_5m_pct=-1.2, return_3m_pct=-0.4, relative_volume=1.2)
    assert engine.classify_market(snap) == TREND_DOWN


def test_classify_panic(engine):
    snap = _neutral_snapshot(return_3m_pct=-2.0, relative_volume=3.0)
    assert engine.classify_market(snap) == PANIC


def test_classify_short_squeeze(engine):
    snap = _neutral_snapshot(return_3m_pct=2.0, relative_volume=3.0, rsi_14=45.0)
    assert engine.classify_market(snap) == SHORT_SQUEEZE


# ── 프로파일 (TP/SL/Trailing) ────────────────────────────────────────────────

@pytest.mark.parametrize("market_type,expected_tp,expected_sl,expected_trailing", [
    (LOW_VOLATILITY, 2.0, 1.0, False),
    (NORMAL, 3.0, 1.5, False),
    (HIGH_VOLATILITY, 4.5, 2.2, False),
    (TREND_UP, 6.0, 2.5, True),
    (TREND_DOWN, 2.5, 1.2, False),
    (PANIC, 2.0, 0.8, False),
])
def test_profile_values_for_hynix_long(engine, market_type, expected_tp, expected_sl, expected_trailing):
    profile = engine.get_profile(market_type, HYNIX_SYMBOL)
    assert profile["tp_pct"] == expected_tp
    assert profile["sl_pct"] == expected_sl
    assert profile["uses_trailing"] == expected_trailing


def test_inverse_position_flips_trend_profiles(engine):
    """인버스 보유 중 TREND_DOWN(하이닉스 하락=인버스에 유리)은 TREND_UP 프로필을 적용해야 한다."""
    profile_for_inverse = engine.get_profile(TREND_DOWN, INVERSE_SYMBOL)
    assert profile_for_inverse["applied_profile"] == TREND_UP
    assert profile_for_inverse["tp_pct"] == 6.0
    assert profile_for_inverse["uses_trailing"] is True

    profile_for_hynix = engine.get_profile(TREND_DOWN, HYNIX_SYMBOL)
    assert profile_for_hynix["applied_profile"] == TREND_DOWN
    assert profile_for_hynix["tp_pct"] == 2.5


# ── Profit Lock ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("peak_profit,expected_floor", [
    (0.5, None), (1.0, 0.0), (1.9, 0.0), (2.0, 1.0), (3.5, 2.0), (5.5, 4.0),
])
def test_profit_lock_floor_ratchet(engine, peak_profit, expected_floor):
    assert engine.compute_profit_lock_floor(peak_profit) == expected_floor


# ── Trailing Stop ────────────────────────────────────────────────────────────

def test_trailing_arms_at_tp_then_triggers_on_pullback(engine):
    profile = {"tp_pct": 6.0, "trailing_pct": 2.0, "uses_trailing": True}
    position = {"trailing_armed": False, "trailing_peak_price": None}

    result = engine.update_trailing(position, profile, current_price=106_000, profit_pct=6.0)
    assert result["triggered"] is False
    assert position["trailing_armed"] is True
    assert position["trailing_peak_price"] == 106_000

    result = engine.update_trailing(position, profile, current_price=110_000, profit_pct=10.0)
    assert result["triggered"] is False
    assert position["trailing_peak_price"] == 110_000

    result = engine.update_trailing(position, profile, current_price=107_600, profit_pct=7.6)  # 110000 대비 -2.18%
    assert result["triggered"] is True


def test_trailing_not_armed_below_tp(engine):
    profile = {"tp_pct": 6.0, "trailing_pct": 2.0, "uses_trailing": True}
    position = {"trailing_armed": False, "trailing_peak_price": None}
    result = engine.update_trailing(position, profile, current_price=103_000, profit_pct=3.0)
    assert result["triggered"] is False
    assert position["trailing_armed"] is False


# ── Time Stop ─────────────────────────────────────────────────────────────────

def test_time_stop_stagnant_20min(engine):
    snap = {"held_minutes": 22.0, "profit_pct": 0.1}
    assert engine.check_time_stop(snap, NORMAL) is not None


def test_time_stop_hard_cap_30min_for_normal(engine):
    snap = {"held_minutes": 31.0, "profit_pct": 2.0}
    assert engine.check_time_stop(snap, NORMAL) is not None


def test_time_stop_allows_60min_for_strong_trend(engine):
    snap = {"held_minutes": 45.0, "profit_pct": 2.0}
    assert engine.check_time_stop(snap, TREND_UP) is None
    snap2 = {"held_minutes": 61.0, "profit_pct": 2.0}
    assert engine.check_time_stop(snap2, TREND_UP) is not None


# ── decide() 통합 ─────────────────────────────────────────────────────────────

def test_decide_triggers_take_profit_in_normal_market():
    engine = DynamicExitEngine()
    position = {
        "symbol": HYNIX_SYMBOL, "entry_price": 100_000.0, "entry_time": datetime(2026, 7, 9, 10, 0).isoformat(),
        "highest_price": 100_000.0, "lowest_price": 100_000.0, "trailing_armed": False,
        "trailing_peak_price": None, "profit_lock_peak_pct": 0.0,
    }
    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=103_100.0, now=datetime(2026, 7, 9, 10, 5))
    assert decision["action"] == "SELL_ALL"
    assert decision["market_type"] == NORMAL
    assert "익절" in decision["reason"]


def test_decide_triggers_stop_loss_in_normal_market():
    engine = DynamicExitEngine()
    position = {
        "symbol": HYNIX_SYMBOL, "entry_price": 100_000.0, "entry_time": datetime(2026, 7, 9, 10, 0).isoformat(),
        "highest_price": 100_000.0, "lowest_price": 100_000.0, "trailing_armed": False,
        "trailing_peak_price": None, "profit_lock_peak_pct": 0.0,
    }
    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=98_400.0, now=datetime(2026, 7, 9, 10, 5))
    assert decision["action"] == "SELL_ALL"
    assert "손절" in decision["reason"]


def test_decide_holds_when_profit_between_thresholds():
    engine = DynamicExitEngine()
    position = {
        "symbol": HYNIX_SYMBOL, "entry_price": 100_000.0, "entry_time": datetime(2026, 7, 9, 10, 0).isoformat(),
        "highest_price": 100_000.0, "lowest_price": 100_000.0, "trailing_armed": False,
        "trailing_peak_price": None, "profit_lock_peak_pct": 0.0,
    }
    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=100_500.0, now=datetime(2026, 7, 9, 10, 5))
    assert decision["action"] == "HOLD"
