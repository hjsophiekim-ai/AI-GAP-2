"""
test_dynamic_exit_engine.py — DynamicExitEngine이 ADAPTIVE_MARKET_REGIME 공용
엔진(app.trading.adaptive_market_regime)에 위임한 이후의 동작을 검증한다.

장세 분류 자체(갭/VWAP/추세/PANIC 동적임계값/2회 연속 확인 등)는
tests/test_adaptive_market_regime.py에서 이미 검증하므로, 여기서는
  - classify_market()이 공용 엔진에 정확히 위임되는지(스모크)
  - get_profile()이 공용 리스크 프로필을 기존 필드명(tp_pct/sl_pct/trailing_pct/
    uses_trailing)으로 정확히 매핑하는지, 인버스 보유 시 방향이 뒤집히는지
  - Profit Lock/Trailing Stop/Time Stop(프로필 기반 max_hold_minutes)
  - decide()의 2단계(tp1 부분익절 → tp2 전량익절) TP 메커닉과 손절/시간손절
만 다룬다.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.trading.dynamic_exit_engine import DynamicExitEngine
from app.trading.adaptive_market_regime import (
    RANGE, STRONG_UP, STRONG_DOWN, HIGH_VOLATILITY, PANIC, DATA_INSUFFICIENT, VOLATILE_RANGE,
    REVERSAL_CANDIDATE_UP, REVERSAL_CANDIDATE_DOWN,
)
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL


@pytest.fixture
def engine():
    return DynamicExitEngine()


def _base_position(**overrides) -> dict:
    position = {
        "symbol": HYNIX_SYMBOL, "entry_price": 100_000.0,
        "entry_time": datetime(2026, 7, 9, 10, 0).isoformat(),
        "highest_price": 100_000.0, "lowest_price": 100_000.0,
        "trailing_armed": False, "trailing_peak_price": None, "profit_lock_peak_pct": 0.0,
    }
    position.update(overrides)
    return position


def _force_regime(engine: DynamicExitEngine, regime: str) -> None:
    engine.classify_regime = lambda *a, **kw: {"regime": regime, "confidence": 80.0, "reasons": ["forced for test"]}


# ── classify_market() 위임 ───────────────────────────────────────────────────

def test_classify_market_delegates_to_adaptive_regime_insufficient_data(engine):
    assert engine.classify_market(None, None) == DATA_INSUFFICIENT


# ── get_profile() — 공용 리스크 프로필 매핑 ──────────────────────────────────

@pytest.mark.parametrize("regime,expected_tp,expected_sl,expected_trailing", [
    (RANGE, 2.0, 0.8, False),
    (STRONG_UP, 2.0, 1.5, True),
    (STRONG_DOWN, 2.0, 1.5, True),
    (HIGH_VOLATILITY, 3.5, 1.0, False),
    (PANIC, 2.0, 0.7, False),
])
def test_profile_values_for_hynix_long(engine, regime, expected_tp, expected_sl, expected_trailing):
    profile = engine.get_profile(regime, HYNIX_SYMBOL)
    assert profile["tp_pct"] == expected_tp
    assert profile["sl_pct"] == expected_sl
    assert profile["uses_trailing"] == expected_trailing
    assert profile["applied_profile"] == regime


def test_inverse_position_flips_strong_trend_profiles(engine):
    """인버스 보유 중 STRONG_DOWN(하이닉스 하락=인버스에 유리)은 STRONG_UP 프로필을 적용한다."""
    profile_for_inverse = engine.get_profile(STRONG_DOWN, INVERSE_SYMBOL)
    assert profile_for_inverse["applied_profile"] == STRONG_UP

    profile_for_hynix = engine.get_profile(STRONG_DOWN, HYNIX_SYMBOL)
    assert profile_for_hynix["applied_profile"] == STRONG_DOWN


def test_range_and_panic_do_not_flip_for_inverse(engine):
    """방향성이 없는(RANGE) 또는 방향 대칭이 아닌(PANIC) 장세는 인버스 보유중에도 그대로 적용된다."""
    assert engine.get_profile(RANGE, INVERSE_SYMBOL)["applied_profile"] == RANGE
    assert engine.get_profile(PANIC, INVERSE_SYMBOL)["applied_profile"] == PANIC


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


# ── Time Stop — 장세별 프로필의 max_hold_minutes 기준 ────────────────────────

def test_time_stop_stagnant_20min(engine):
    snap = {"held_minutes": 22.0, "profit_pct": 0.1}
    profile = {"max_hold_minutes": None}  # 정체 판단은 하드캡과 무관하게 항상 적용
    assert engine.check_time_stop(snap, profile) is not None


def test_time_stop_hard_cap_from_profile(engine):
    """RANGE(max_hold_minutes=20)처럼 프로필이 명시한 하드캡을 그대로 사용한다."""
    snap = {"held_minutes": 21.0, "profit_pct": 2.0}
    assert engine.check_time_stop(snap, {"max_hold_minutes": 20}) is not None
    snap_ok = {"held_minutes": 19.0, "profit_pct": 2.0}
    assert engine.check_time_stop(snap_ok, {"max_hold_minutes": 20}) is None


def test_time_stop_falls_back_to_trend_max_minutes_when_profile_has_no_cap(engine):
    """STRONG_UP/STRONG_DOWN(max_hold_minutes=None)은 추세를 끝까지 태우되,
    무기한 보유를 막기 위한 안전망(trend_max_minutes, 기본 60분)이 적용된다."""
    snap = {"held_minutes": 45.0, "profit_pct": 2.0}
    assert engine.check_time_stop(snap, {"max_hold_minutes": None}) is None
    snap2 = {"held_minutes": 61.0, "profit_pct": 2.0}
    assert engine.check_time_stop(snap2, {"max_hold_minutes": None}) is not None


# ── decide() 통합 — RANGE(2단계 tp1/tp2) ─────────────────────────────────────

def test_decide_range_partial_take_profit_then_full_on_tp2():
    """요구사항 — RANGE에서 +2% 전량익절(tp2), 그 전 +1.5%에서는 부분익절(tp1)."""
    engine = DynamicExitEngine()
    _force_regime(engine, RANGE)
    position = _base_position()

    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=101_600.0, now=datetime(2026, 7, 9, 10, 5))
    assert decision["action"] == "SELL_PARTIAL"
    assert decision["ratio"] == pytest.approx(0.5)
    assert position["partial_tp1_done"] is True

    decision2 = engine.decide(position, df_daily=None, df_1min=None, current_price=102_100.0, now=datetime(2026, 7, 9, 10, 8))
    assert decision2["action"] == "SELL_ALL"
    assert decision2["ratio"] == pytest.approx(1.0)
    assert "익절" in decision2["reason"]


def test_decide_range_stop_loss_at_minus_0_8_pct():
    """요구사항 — RANGE에서 -0.8% 손절."""
    engine = DynamicExitEngine()
    _force_regime(engine, RANGE)
    position = _base_position()

    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=99_150.0, now=datetime(2026, 7, 9, 10, 5))
    assert decision["action"] == "SELL_ALL"
    assert "손절" in decision["reason"]


def test_decide_holds_when_profit_between_thresholds():
    engine = DynamicExitEngine()
    _force_regime(engine, RANGE)
    position = _base_position()
    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=100_500.0, now=datetime(2026, 7, 9, 10, 5))
    assert decision["action"] == "HOLD"


# ── decide() 통합 — STRONG_UP(tp1 부분익절 후 나머지는 트레일링) ─────────────

def test_decide_strong_up_partial_tp1_then_trails_remainder():
    """요구사항(2026-07-16, 큰 추세 수익 극대화판) — STRONG_TREND: +2%에서
    20~30%만 부분익절, 나머지 70~80%는 ATR trailing(즉시 전량청산 아님)."""
    engine = DynamicExitEngine()
    _force_regime(engine, STRONG_UP)
    position = _base_position()

    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=102_100.0, now=datetime(2026, 7, 9, 10, 5))
    assert decision["action"] == "SELL_PARTIAL"
    assert 0.20 <= decision["ratio"] <= 0.30
    assert position["partial_tp1_done"] is True
    assert position["trailing_armed"] is True  # tp1 도달과 동시에 트레일링 무장


def test_decide_regime_change_swaps_profile_between_calls():
    """요구사항 — 장세가 바뀌면 다음 결정부터 즉시 새 프로필(SL 등)이 적용된다.

    -0.75% 손실은 RANGE(sl=-0.8%)에서는 아직 손절 전이지만, PANIC(sl=-0.7%)으로
    장세가 바뀌면 같은 손실률로도 즉시 손절된다."""
    engine = DynamicExitEngine()
    position = _base_position()

    _force_regime(engine, RANGE)
    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=99_250.0, now=datetime(2026, 7, 9, 10, 3))
    assert decision["action"] == "HOLD"

    _force_regime(engine, PANIC)
    decision2 = engine.decide(position, df_daily=None, df_1min=None, current_price=99_250.0, now=datetime(2026, 7, 9, 10, 4))
    assert decision2["action"] == "SELL_ALL"
    assert "손절" in decision2["reason"]


# ── VOLATILE_RANGE 초단기 실행 보호(2026-07-16) ──────────────────────────────

def test_decide_volatile_range_tp1_50pct_then_tp2_full():
    """요구사항 — VOLATILE_RANGE에서 +0.8% 50% 매도, +1.3%에서 전량매도."""
    engine = DynamicExitEngine()
    _force_regime(engine, VOLATILE_RANGE)
    position = _base_position()

    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=100_800.0, now=datetime(2026, 7, 9, 10, 2))
    assert decision["action"] == "SELL_PARTIAL"
    assert decision["ratio"] == pytest.approx(0.5)

    decision2 = engine.decide(position, df_daily=None, df_1min=None, current_price=101_300.0, now=datetime(2026, 7, 9, 10, 4))
    assert decision2["action"] == "SELL_ALL"
    assert decision2["ratio"] == pytest.approx(1.0)


def test_decide_volatile_range_stop_loss_at_minus_0_6_pct():
    engine = DynamicExitEngine()
    _force_regime(engine, VOLATILE_RANGE)
    position = _base_position()

    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=99_400.0, now=datetime(2026, 7, 9, 10, 2))
    assert decision["action"] == "SELL_ALL"
    assert "손절" in decision["reason"]


def test_decide_volatile_range_time_stop_at_8_minutes():
    engine = DynamicExitEngine()
    _force_regime(engine, VOLATILE_RANGE)
    position = _base_position()

    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=100_000.0, now=datetime(2026, 7, 9, 10, 9))
    assert decision["action"] == "SELL_ALL"
    assert "시간손절" in decision["reason"]


def test_decide_opposite_signal_streak_reduces_then_exits():
    """요구사항 — 반대 강신호 1회면 50% 축소, 2회면 전량청산(TP/SL 도달 전이어도)."""
    engine = DynamicExitEngine()
    _force_regime(engine, VOLATILE_RANGE)
    position = _base_position()

    decision = engine.decide(
        position, df_daily=None, df_1min=None, current_price=100_100.0, now=datetime(2026, 7, 9, 10, 1),
        opposite_signal_streak=1,
    )
    assert decision["action"] == "SELL_PARTIAL"
    assert decision["ratio"] == pytest.approx(0.5)
    assert "반대 강신호" in decision["reason"]

    decision2 = engine.decide(
        position, df_daily=None, df_1min=None, current_price=100_100.0, now=datetime(2026, 7, 9, 10, 2),
        opposite_signal_streak=2,
    )
    assert decision2["action"] == "SELL_ALL"
    assert decision2["ratio"] == 1.0


def test_decide_opposite_signal_streak_zero_is_noop():
    engine = DynamicExitEngine()
    _force_regime(engine, VOLATILE_RANGE)
    position = _base_position()

    decision = engine.decide(
        position, df_daily=None, df_1min=None, current_price=100_100.0, now=datetime(2026, 7, 9, 10, 1),
        opposite_signal_streak=0,
    )
    assert decision["action"] == "HOLD"


# ── 큰 추세 수익 극대화(2026-07-16) — 추세 반전 확정 시 전량청산 ────────────

def test_decide_exits_long_position_when_confirmed_regime_reverses_to_strong_down():
    """요구사항 — VWAP 이탈+15분반전+스윙붕괴 2회 확인(=confirmed_regime이
    STRONG_DOWN으로 확정)되면 TP/SL 도달 여부와 무관하게 롱 포지션을 즉시
    전량청산한다."""
    engine = DynamicExitEngine()
    _force_regime(engine, STRONG_UP)  # raw 재분류는 여전히 STRONG_UP(작은 반대신호일 뿐)일 수도 있음
    position = _base_position()  # HYNIX_SYMBOL(LONG) 보유 중, entry_price=100,000

    decision = engine.decide(
        position, df_daily=None, df_1min=None, current_price=100_500.0, now=datetime(2026, 7, 9, 10, 2),
        confirmed_regime=STRONG_DOWN,
    )
    assert decision["action"] == "SELL_ALL"
    assert decision["ratio"] == 1.0
    assert "추세 반전 확정" in decision["reason"]


def test_decide_does_not_exit_on_small_opposite_signal_without_confirmed_regime():
    """요구사항 — 작은 1·3·5분 반대 신호만으로는(confirmed_regime을 넘기지 않으면)
    청산하지 않는다."""
    engine = DynamicExitEngine()
    _force_regime(engine, STRONG_UP)
    position = _base_position()

    decision = engine.decide(
        position, df_daily=None, df_1min=None, current_price=100_500.0, now=datetime(2026, 7, 9, 10, 2),
    )
    assert decision["action"] == "HOLD"


def test_decide_reversal_exit_does_not_fire_when_regime_still_aligned_with_position():
    engine = DynamicExitEngine()
    _force_regime(engine, STRONG_UP)
    position = _base_position()

    decision = engine.decide(
        position, df_daily=None, df_1min=None, current_price=100_500.0, now=datetime(2026, 7, 9, 10, 2),
        confirmed_regime=STRONG_UP,
    )
    assert decision["action"] != "SELL_ALL"


def test_decide_strong_trend_to_range_applies_short_exit_criteria_immediately():
    """요구사항5 — STRONG_TREND에서 RANGE/VOLATILE_RANGE로 전환되면 즉시 짧은
    TP/SL 프로필로 바뀐다(-0.8%는 RANGE 기준 손절이지 STRONG_UP의 -1.5% 기준으로는
    아직 안전권이었을 손실률)."""
    engine = DynamicExitEngine()
    position = _base_position()

    _force_regime(engine, STRONG_UP)
    decision = engine.decide(position, df_daily=None, df_1min=None, current_price=99_150.0, now=datetime(2026, 7, 9, 10, 3))
    assert decision["action"] == "HOLD"  # STRONG_UP sl=-1.5%, -0.85%는 아직 안전

    _force_regime(engine, RANGE)
    decision2 = engine.decide(position, df_daily=None, df_1min=None, current_price=99_150.0, now=datetime(2026, 7, 9, 10, 4))
    assert decision2["action"] == "SELL_ALL"  # RANGE sl=-0.8%로 즉시 전환되어 같은 손실률로 손절
    assert "손절" in decision2["reason"]


def test_decide_exits_inverse_position_when_confirmed_regime_reverses_to_strong_up():
    from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL

    engine = DynamicExitEngine()
    _force_regime(engine, STRONG_DOWN)
    position = _base_position(symbol=INVERSE_SYMBOL, entry_price=5_000.0)

    decision = engine.decide(
        position, df_daily=None, df_1min=None, current_price=5_010.0, now=datetime(2026, 7, 9, 10, 2),
        confirmed_regime=STRONG_UP,
    )
    assert decision["action"] == "SELL_ALL"
    assert decision["ratio"] == 1.0
