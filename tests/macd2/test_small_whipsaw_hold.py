"""H50 (작은 휩쏘 HOLD) 단위/통합 테스트 — 2026-09-15.

핵심 계약
  1. H50 OFF -> 모듈이 아예 no-op (is_active False, 기존 X2-lite 동작 불변)
  2. HOLD 조건: 추세 순방향 AND range <= 2.35%
  3. 해제: 추세 2봉 연속 반전 | 60분 경과
  4. 상태 저장/복원/일자초기화
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, small_whipsaw_hold as swh, state_store
from app.trading.macd2 import time_window_3slot as tw3
from app.trading.macd2.models import Direction

KST = config.KST


def _bars(closes, *, highs=None, lows=None, start="2026-09-15 09:00"):
    """완성 3분봉 프레임. highs/lows 를 주지 않으면 close 와 같게 둔다."""
    n = len(closes)
    idx = [pd.Timestamp(start, tz=KST) + timedelta(minutes=3 * i) for i in range(n)]
    return pd.DataFrame({
        "datetime": idx,
        "open": closes,
        "high": highs if highs is not None else closes,
        "low": lows if lows is not None else closes,
        "close": closes,
        "volume": [1000.0] * n,
    })


def _uptrend_closes(n=80, base=100000.0, step=60.0):
    """EMA20 > EMA50 이 확실한 완만한 상승."""
    return [base + step * i for i in range(n)]


def _downtrend_closes(n=80, base=100000.0, step=60.0):
    return [base - step * i for i in range(n)]


def _state(*, h50=True):
    st = state_store.default_state()
    st.time_window_x2lite_filter_enabled = False
    st.time_window_3slot_filter_enabled = False
    st.time_window_twf_filter_enabled = False
    st.time_window_h50_filter_enabled = bool(h50)
    return st


# ── 1. 모드 배선 / OFF 시 no-op ────────────────────────────────────────────
def test_h50_default_off_and_x2lite_untouched():
    st = state_store.default_state()
    assert st.time_window_h50_filter_enabled is False
    assert tw3.active_3slot_mode(st) != tw3.MODE_X2LITE_H50_3SLOT
    assert swh.is_active(st) is False


def test_h50_mode_resolves_and_shares_x2lite_parameters():
    st = _state(h50=True)
    assert tw3.active_3slot_mode(st) == tw3.MODE_X2LITE_H50_3SLOT
    assert swh.is_active(st) is True
    # 진입/청산 파라미터가 X2-lite 와 완전히 같아야 한다.
    assert (tw3.exit_overrides(tw3.MODE_X2LITE_H50_3SLOT)
            == tw3.exit_overrides(tw3.MODE_X2LITE_3SLOT))
    assert (tw3.morning_tp2_pct_override(tw3.MODE_X2LITE_H50_3SLOT)
            == tw3.morning_tp2_pct_override(tw3.MODE_X2LITE_3SLOT))
    assert (tw3.requires_chop_teg_gate(tw3.MODE_X2LITE_H50_3SLOT)
            == tw3.requires_chop_teg_gate(tw3.MODE_X2LITE_3SLOT) is True)


def test_w1a_and_etp_active_in_h50_mode():
    from app.trading.macd2 import early_take_profit, position_sizing
    st = _state(h50=True)
    assert position_sizing.is_active(st) is True
    assert early_take_profit.is_enabled(st) is True
    assert early_take_profit.thresholds(st) == (
        float(config.X2LITE_EARLY_TP_TRIGGER_PCT), float(config.X2LITE_EARLY_TP_FLOOR_PCT))


def test_module_is_noop_when_mode_off():
    st = state_store.default_state()          # X2-lite ON, H50 OFF (기본)
    assert swh.is_active(st) is False
    assert swh.is_holding(st) is False


# ── 2. HOLD 조건 ───────────────────────────────────────────────────────────
def _frame_with_range(range_pct: float, *, up=True, n=80):
    """마지막 20봉 high-low 폭이 정확히 range_pct 가 되도록 만든 프레임."""
    closes = _uptrend_closes(n) if up else _downtrend_closes(n)
    highs = list(closes)
    lows = list(closes)
    last_close = closes[-1]
    span = last_close * range_pct / 100.0
    # 최근 20봉 안에서만 폭을 만든다(추세 EMA 는 close 로만 계산되므로 불변).
    highs[-20] = closes[-20] + span / 2.0
    lows[-20] = closes[-20] + span / 2.0 - span
    return _bars(closes, highs=highs, lows=lows)


def test_hold_when_trend_aligned_and_range_narrow():
    bars = _frame_with_range(2.00, up=True)
    d = swh.evaluate_hold(bars, Direction.UP_RED, datetime.now(KST))
    assert d.trend == swh.TREND_UP
    assert d.trend_ok is True and d.range_ok is True
    assert d.should_hold is True
    assert d.reason == "SMALL_WHIPSAW_HOLD"


def test_range_234_holds_and_236_does_not():
    """임계 2.35% 경계 — 2.34% 는 HOLD, 2.36% 는 정상 opposite exit."""
    d_in = swh.evaluate_hold(_frame_with_range(2.34, up=True), Direction.UP_RED)
    d_out = swh.evaluate_hold(_frame_with_range(2.36, up=True), Direction.UP_RED)
    assert d_in.should_hold is True
    assert d_out.should_hold is False
    assert d_out.reason == "RANGE_TOO_WIDE"
    assert d_in.range_pct < config.H50_RANGE_MAX_PCT < d_out.range_pct


def test_no_hold_when_trend_not_aligned():
    """EMA20/EMA50 이 보유방향과 반대면 range 가 아무리 좁아도 HOLD 하지 않는다."""
    bars = _frame_with_range(1.00, up=False)          # 하락추세
    d = swh.evaluate_hold(bars, Direction.UP_RED, datetime.now(KST))   # LONG 보유
    assert d.trend == swh.TREND_DOWN
    assert d.trend_ok is False
    assert d.should_hold is False
    assert d.reason == "TREND_NOT_ALIGNED"


def test_short_side_is_mirrored():
    bars = _frame_with_range(1.00, up=False)
    d = swh.evaluate_hold(bars, Direction.DOWN_BLUE)
    assert d.trend == swh.TREND_DOWN and d.should_hold is True


def test_insufficient_bars_never_holds():
    bars = _bars(_uptrend_closes(10))
    d = swh.evaluate_hold(bars, Direction.UP_RED)
    assert d.should_hold is False and d.insufficient_data is True


# ── 3. 해제 조건 ───────────────────────────────────────────────────────────
def test_release_needs_two_consecutive_trend_break_bars():
    """1봉 반전 -> HOLD 유지 / 2봉 연속 -> 청산."""
    started = datetime(2026, 9, 15, 10, 0, tzinfo=KST)
    now = started + timedelta(minutes=9)
    down = _bars(_downtrend_closes(80))               # 추세가 보유(LONG) 반대
    first = swh.evaluate_release(down, Direction.UP_RED, started, now, trend_break_count=0)
    assert first.trend_break_count == 1
    assert first.should_release is False              # 1봉만으로는 유지
    second = swh.evaluate_release(down, Direction.UP_RED, started, now,
                                  trend_break_count=first.trend_break_count)
    assert second.trend_break_count == 2
    assert second.should_release is True and second.reason == "TREND_BREAK"


def test_trend_break_counter_resets_when_trend_returns():
    started = datetime(2026, 9, 15, 10, 0, tzinfo=KST)
    now = started + timedelta(minutes=9)
    up = _bars(_uptrend_closes(80))
    d = swh.evaluate_release(up, Direction.UP_RED, started, now, trend_break_count=1)
    assert d.trend_break_count == 0 and d.should_release is False


def test_release_at_60min_not_at_59():
    started = datetime(2026, 9, 15, 10, 0, tzinfo=KST)
    up = _bars(_uptrend_closes(80))
    at59 = swh.evaluate_release(up, Direction.UP_RED, started,
                                started + timedelta(minutes=59))
    at60 = swh.evaluate_release(up, Direction.UP_RED, started,
                                started + timedelta(minutes=60))
    assert at59.should_release is False and at59.reason == "HOLDING"
    assert at60.should_release is True and at60.reason == "MAX_HOLD"


# ── 4. state 저장/복원/초기화 ──────────────────────────────────────────────
def test_note_hold_start_and_clear():
    st = _state(h50=True)
    now = datetime(2026, 9, 15, 10, 0, tzinfo=KST)
    d = swh.evaluate_hold(_frame_with_range(2.00, up=True), Direction.UP_RED, now)
    swh.note_hold_start(st, held_direction=Direction.UP_RED, now=now, decision=d)
    assert swh.is_holding(st) is True
    assert st.h50_original_direction == Direction.UP_RED.value
    assert swh.held_direction(st) == Direction.UP_RED
    assert st.h50_hold_started_at == now.isoformat()
    assert st.h50_trend_break_count == 0
    swh.clear(st)
    assert swh.is_holding(st) is False
    assert st.h50_hold_started_at is None and st.h50_original_direction is None


def test_hold_state_survives_serialize_roundtrip():
    st = _state(h50=True)
    now = datetime(2026, 9, 15, 10, 0, tzinfo=KST)
    d = swh.evaluate_hold(_frame_with_range(2.00, up=True), Direction.UP_RED, now)
    swh.note_hold_start(st, held_direction=Direction.UP_RED, now=now, decision=d)
    swh.note_trend_break_count(st, 1)
    back = state_store.deserialize(state_store.serialize(st))
    assert back.time_window_h50_filter_enabled is True
    assert back.h50_hold_active is True
    assert back.h50_original_direction == Direction.UP_RED.value
    assert back.h50_hold_started_at == now.isoformat()
    assert back.h50_trend_break_count == 1


def test_h50_on_forces_x2lite_off_on_restore():
    """3-SLOT 계열 상호배제 — 두 토글이 동시에 켜진 상태로 저장돼도 복원 시 정리."""
    st = _state(h50=True)
    st.time_window_x2lite_filter_enabled = True
    raw = state_store.serialize(st)
    back = state_store.deserialize(raw)
    assert back.time_window_h50_filter_enabled is True
    assert back.time_window_x2lite_filter_enabled is False
    assert tw3.active_3slot_mode(back) == tw3.MODE_X2LITE_H50_3SLOT


def test_day_rollover_clears_hold_but_keeps_toggle():
    st = _state(h50=True)
    now = datetime(2026, 9, 15, 10, 0, tzinfo=KST)
    d = swh.evaluate_hold(_frame_with_range(2.00, up=True), Direction.UP_RED, now)
    swh.note_hold_start(st, held_direction=Direction.UP_RED, now=now, decision=d)
    swh.clear(st)                        # worker 의 일자 rollover 가 하는 것과 동일
    assert st.time_window_h50_filter_enabled is True     # 토글은 유지
    assert st.h50_hold_active is False                   # 보류 상태만 초기화
    assert st.h50_trend_break_count == 0


# ── 5. 설정값 고정 (연구 확정값이 조용히 바뀌지 않게) ──────────────────────
def test_config_values_locked_to_research():
    assert config.H50_RANGE_MAX_PCT == pytest.approx(2.35)
    assert config.H50_RANGE_BARS == 20          # 60분
    assert config.H50_TREND_EMA_FAST == 20
    assert config.H50_TREND_EMA_SLOW == 50
    assert config.H50_TREND_BREAK_BARS == 2
    assert config.H50_MAX_HOLD_MIN == 60
    assert config.H50_3SLOT_FILTER_DEFAULT is False      # 기본 OFF
