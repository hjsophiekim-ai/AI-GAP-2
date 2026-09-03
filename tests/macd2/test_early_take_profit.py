"""조기익절 필터 (app/trading/macd2/early_take_profit.py) 순수함수 단위테스트.

worker 경로 회귀테스트는 tests/macd2/test_early_take_profit_worker.py 에 있다.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, early_take_profit as etp
from app.trading.macd2.models import Direction, RuntimeState

KST = config.KST


# ── 토글/자동비활성 게이트 ────────────────────────────────────────────────
def _state(**kw) -> RuntimeState:
    s = RuntimeState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_is_enabled_requires_both_its_own_toggle_and_tw2_3slot():
    assert etp.is_enabled(_state()) is False
    assert etp.is_enabled(_state(early_tp_filter_enabled=True)) is False, (
        "TW2 3-SLOT이 꺼져 있으면 자기 토글만 켜져도 비활성이어야 한다"
    )
    assert etp.is_enabled(_state(time_window_3slot_filter_enabled=True)) is False
    assert etp.is_enabled(
        _state(early_tp_filter_enabled=True, time_window_3slot_filter_enabled=True)
    ) is True


def test_tw2_3slot_off_auto_deactivates_even_with_a_live_position():
    s = _state(
        early_tp_filter_enabled=True, time_window_3slot_filter_enabled=False,
        time_window_position_active=True, time_window_active_mode="TW2_3SLOT",
    )
    assert etp.is_active(s) is False


@pytest.mark.parametrize("mode", ["TW2", "TEGv2", None, "TW1"])
def test_is_active_only_for_a_tw2_3slot_managed_position(mode):
    s = _state(
        early_tp_filter_enabled=True, time_window_3slot_filter_enabled=True,
        time_window_position_active=True, time_window_active_mode=mode,
    )
    assert etp.is_active(s) is False, (
        f"TW2 3-SLOT이 관리하는 포지션이 아니면(mode={mode!r}) 적용되면 안 된다"
    )
    s.time_window_active_mode = "TW2_3SLOT"
    assert etp.is_active(s) is True


def test_is_active_false_when_no_time_window_position_is_held():
    s = _state(
        early_tp_filter_enabled=True, time_window_3slot_filter_enabled=True,
        time_window_position_active=False, time_window_active_mode="TW2_3SLOT",
    )
    assert etp.is_active(s) is False


# ── evaluate(): arming / floor / 대상 판별 ────────────────────────────────
def test_non_entry_chop_position_is_never_touched():
    d = etp.evaluate(entry_chop=False, peak_net_return_pct=99.0, net_return_pct=-99.0)
    assert d.armed is False and d.exit_reason is None
    assert d.label == etp.LABEL_NOT_ENTRY_CHOP


def test_not_armed_below_trigger_mfe():
    d = etp.evaluate(
        entry_chop=True,
        peak_net_return_pct=config.EARLY_TP_TRIGGER_PCT - 0.01,
        net_return_pct=-1.0,
    )
    assert d.armed is False and d.exit_reason is None
    assert d.label == etp.LABEL_NOT_ARMED


def test_arms_exactly_at_trigger_and_holds_above_floor():
    d = etp.evaluate(
        entry_chop=True,
        peak_net_return_pct=config.EARLY_TP_TRIGGER_PCT,
        net_return_pct=config.EARLY_TP_FLOOR_PCT + 0.01,
    )
    assert d.armed is True and d.exit_reason is None
    assert d.label == etp.LABEL_ARMED_HOLD


def test_fires_at_or_below_floor_and_always_sells_the_whole_remainder():
    for net in (config.EARLY_TP_FLOOR_PCT, config.EARLY_TP_FLOOR_PCT - 0.5, -2.0):
        d = etp.evaluate(
            entry_chop=True, peak_net_return_pct=config.EARLY_TP_TRIGGER_PCT + 1.0,
            net_return_pct=net,
        )
        assert d.exit_reason == config.EXIT_EARLY_TAKE_PROFIT
        assert d.sell_fraction == 1.0, "이 필터는 부분매도를 만들지 않는다"
        assert d.label == etp.LABEL_FIRED


def test_exit_reason_label_is_distinct_from_the_unrelated_legacy_profit_lock():
    assert config.EXIT_EARLY_TAKE_PROFIT != config.EXIT_PROFIT_LOCK
    assert config.EXIT_EARLY_TAKE_PROFIT not in {
        config.EXIT_TW_STOP_LOSS, config.EXIT_TW_TP1_PARTIAL, config.EXIT_TW_TP2_FULL,
        config.EXIT_TW_AFTER_TP1_STOP, config.EXIT_TW_TRAILING_STOP,
        config.EXIT_TW_AFTERNOON_TP, config.EXIT_TW_BREAKEVEN_STOP,
        config.EXIT_TW_PROFIT_LOCK_STOP,
    }


def test_frozen_train_selected_thresholds_are_the_validated_ones():
    """2026-09-03 TRAIN에서 확정하고 OOS에서 재조정하지 않은 값들 —
    바뀌면 60일 검증 결과가 그대로 적용되지 않으므로 고정한다."""
    assert config.EARLY_TP_TRIGGER_PCT == 1.5
    assert config.EARLY_TP_FLOOR_PCT == 0.8
    assert config.EARLY_TP_LOOKBACK_MINUTES == 30
    assert config.EARLY_TP_SCORE_MIN == 3
    assert config.EARLY_TP_RECENT_CROSS_MIN == 1
    assert config.EARLY_TP_VWAP_FLIP_MIN == 3
    assert config.EARLY_TP_MIN_BARS == 4
    assert config.EARLY_TP_FILTER_DEFAULT is False, "기본 OFF여야 한다"


# ── evaluate_entry_chop(): 데이터 부족/경계 ───────────────────────────────
def _bars_3m(start: datetime, closes: list[float], *, volume: float = 1000.0) -> pd.DataFrame:
    rows = [
        {
            "datetime": start + timedelta(minutes=3 * i),
            "open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": volume,
        }
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


_DAY = datetime(2026, 3, 10, 9, 0, tzinfo=KST)


def test_entry_chop_insufficient_when_frame_is_too_short():
    bars = _bars_3m(_DAY, [100.0] * 5)
    d = etp.evaluate_entry_chop(bars, Direction.UP_RED, _DAY + timedelta(minutes=15))
    assert d.insufficient_data is True and d.is_chop is False


def test_entry_chop_insufficient_before_0915_because_the_30min_window_cannot_form():
    """09:15 이전 진입은 30분 창에 정규장 봉이 EARLY_TP_MIN_BARS개도 없어
    구조적으로 판정 불가 -- TREND(=필터 미적용)로 떨어져야 한다."""
    closes = [100.0 + 0.1 * i for i in range(60)]
    bars = _bars_3m(_DAY - timedelta(days=1, minutes=0), closes)  # 전일 프레임
    # 마지막 봉을 오늘 09:06으로 만들기 위해 오늘 봉 3개만 덧붙인다
    today = _bars_3m(_DAY, [106.0, 106.2, 106.4])
    frame = pd.concat([bars, today], ignore_index=True)
    d = etp.evaluate_entry_chop(frame, Direction.UP_RED, _DAY + timedelta(minutes=9))
    assert d.insufficient_data is True, (
        "당일 정규장 봉이 3개뿐이면(09:06 확정) 판정 불가여야 한다"
    )
    assert d.is_chop is False


def test_entry_chop_invalid_direction_is_insufficient_not_chop():
    closes = [100.0 + 0.1 * i for i in range(60)]
    d = etp.evaluate_entry_chop(_bars_3m(_DAY, closes), "NOT_A_DIRECTION", _DAY + timedelta(hours=3))
    assert d.insufficient_data is True and d.is_chop is False


def test_entry_chop_pure_function_never_mutates_the_input_frame():
    closes = [100.0 + math.sin(i / 3.0) for i in range(60)]
    bars = _bars_3m(_DAY, closes)
    before = bars.copy(deep=True)
    etp.evaluate_entry_chop(bars, Direction.UP_RED, _DAY + timedelta(minutes=180))
    pd.testing.assert_frame_equal(bars, before)


def test_clean_one_way_trend_is_not_classified_chop():
    """단조 상승 + 거래량 일정 -> spread 확장/EMA20 상승/VWAP 한쪽 -> CHOP 아님."""
    closes = [100.0 + 0.5 * i for i in range(80)]
    bars = _bars_3m(_DAY, closes)
    last = pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime()
    d = etp.evaluate_entry_chop(bars, Direction.UP_RED, last + timedelta(minutes=3))
    assert d.insufficient_data is False
    assert d.is_chop is False, f"명확한 추세가 CHOP으로 분류됐다: {d.conditions} {d.metrics}"
    assert d.conditions[etp.CHOP_COND_SPREAD_NOT_EXPANDING] is False
    assert d.conditions[etp.CHOP_COND_EMA20_SLOPE_NOT_ALIGNED] is False


def test_oscillating_range_is_classified_chop():
    """VWAP 위아래를 반복하는 좁은 박스 -> 4개 조건 중 3개 이상 충족."""
    closes = [100.0 + 1.2 * math.sin(2 * math.pi * i / 4.0) for i in range(80)]
    bars = _bars_3m(_DAY, closes)
    last = pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime()
    d = etp.evaluate_entry_chop(bars, Direction.UP_RED, last + timedelta(minutes=3))
    assert d.insufficient_data is False
    assert d.score >= config.EARLY_TP_SCORE_MIN, (
        f"진동 구간이 CHOP으로 분류되지 않았다: score={d.score} {d.conditions} {d.metrics}"
    )
    assert d.is_chop is True
    assert d.conditions[etp.CHOP_COND_VWAP_REPEAT] is True


def test_entry_chop_reports_all_four_conditions_and_the_required_threshold():
    closes = [100.0 + math.sin(i / 2.0) for i in range(80)]
    bars = _bars_3m(_DAY, closes)
    last = pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime()
    d = etp.evaluate_entry_chop(bars, Direction.DOWN_BLUE, last + timedelta(minutes=3))
    assert set(d.conditions) == set(etp.ALL_CHOP_CONDITIONS)
    assert d.required == config.EARLY_TP_SCORE_MIN
    assert d.score == sum(1 for c in etp.ALL_CHOP_CONDITIONS if d.conditions[c])
    for key in ("recent_confirmed_crosses", "ema_spread_signed_change",
                "ema20_signed_change", "vwap_flip_count", "lookback_bars_used"):
        assert key in d.metrics


def test_direction_flips_the_signed_conditions():
    """같은 봉을 UP_RED / DOWN_BLUE로 판정하면 부호 기반 두 조건이 반대여야 한다
    (순변화가 정확히 0인 경우는 양쪽 모두 True이므로 그때만 예외)."""
    closes = [100.0 + 0.5 * i for i in range(80)]
    bars = _bars_3m(_DAY, closes)
    last = pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime()
    up = etp.evaluate_entry_chop(bars, Direction.UP_RED, last + timedelta(minutes=3))
    dn = etp.evaluate_entry_chop(bars, Direction.DOWN_BLUE, last + timedelta(minutes=3))
    assert up.metrics["ema20_signed_change"] == pytest.approx(-dn.metrics["ema20_signed_change"])
    assert up.conditions[etp.CHOP_COND_EMA20_SLOPE_NOT_ALIGNED] is False
    assert dn.conditions[etp.CHOP_COND_EMA20_SLOPE_NOT_ALIGNED] is True
