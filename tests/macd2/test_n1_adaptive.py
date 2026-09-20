"""N1 — 사양/모드/adaptive 래더/state 계약 테스트 (2026-09-20).

연구사양 근거: data/validation/macd2/n1_production_20260920/N1_SPEC.md
worker 경로는 tests/macd2/test_n1_worker.py 가 따로 고정한다.

고정하는 계약
-------------
A. N1 OFF parity — X2-lite / H50 / TW2 3-SLOT / TW TEG 3-SLOT 의 모든 모드
   분기 반환값이 N1 도입 전과 **한 값도** 다르지 않다.
B. 사양값     — N1_SPEC.md 3-1/3-2 표의 숫자가 코드에서 그대로 나온다.
C. adaptive   — 상위추세 여부로 TP1/TP1비중/TP2 가 전환되고, 판정식이
                H50 과 같은 EMA 상수를 쓰며 새 지표식이 없다.
D. 방향대칭   — UP_RED / DOWN_BLUE 완전 대칭.
E. state      — 기본 OFF / 하위호환 / 일자변경 / 재시작 복원.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import (
    config, early_take_profit as etp, n1_adaptive as na, position_sizing as ps,
    small_whipsaw_hold as swh, state_store, time_window_3slot as tw3,
    time_window_position_manager as twpm,
)
from app.trading.macd2.models import Direction, RuntimeState

KST = config.KST


def _state(**kw) -> RuntimeState:
    s = RuntimeState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _bars(closes, start=None):
    """3분봉 프레임. major_flag_filter._prepare_bars 가 요구하는 컬럼 전부."""
    start = start or datetime(2026, 9, 18, 9, 0, tzinfo=KST)
    return pd.DataFrame({
        "datetime": [start + timedelta(minutes=3 * i) for i in range(len(closes))],
        "open": closes, "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes], "close": closes,
        "volume": [1000.0] * len(closes),
    })


# ── A. N1 OFF parity — 기존 모드 분기 반환값 불변 ─────────────────────────
def test_existing_modes_exit_overrides_unchanged():
    assert tw3.exit_overrides(tw3.MODE_X2LITE_3SLOT) == {
        "stop_loss_pct_override": -1.3, "after_tp1_stop_pct_override": 2.0,
        "afternoon_tp_pct_override": 3.0, "tp1_sell_ratio_override": 0.2,
        "trailing_stop_pct_override": pytest.approx(2.8),
    }
    assert tw3.exit_overrides(tw3.MODE_X2LITE_H50_3SLOT) == tw3.exit_overrides(tw3.MODE_X2LITE_3SLOT)
    for mode in (tw3.MODE_TW2_3SLOT, tw3.MODE_TWF_3SLOT, None, "MU_MACD"):
        ov = tw3.exit_overrides(mode)
        if mode == tw3.MODE_TWF_3SLOT:
            continue
        assert ov["tp1_sell_ratio_override"] is None, mode
        assert ov["trailing_stop_pct_override"] is None, mode


def test_existing_modes_tp2_and_gates_unchanged():
    assert tw3.morning_tp2_pct_override(tw3.MODE_X2LITE_3SLOT) == pytest.approx(5.0)
    assert tw3.morning_tp2_pct_override(tw3.MODE_X2LITE_H50_3SLOT) == pytest.approx(5.0)
    assert tw3.morning_tp2_pct_override(tw3.MODE_TW2_3SLOT) == pytest.approx(6.0)
    assert tw3.morning_tp2_pct_override(None) is None
    assert tw3.requires_chop_teg_gate(tw3.MODE_TW2_3SLOT) is False
    assert tw3.requires_chop_teg_gate(tw3.MODE_X2LITE_3SLOT) is True
    assert tw3.quality_score_threshold(tw3.MODE_X2LITE_3SLOT) == config.QUALITY_SCORE_THRESHOLD
    assert tw3.quality_score_threshold(tw3.MODE_TW2_3SLOT) == config.QUALITY_SCORE_THRESHOLD
    assert tw3.quality_score_threshold(None) == config.QUALITY_SCORE_THRESHOLD
    # MODES_X2LITE_FAMILY 는 건드리지 않았다 -- N1 을 넣으면 위 값들이 깨진다
    assert tw3.MODES_X2LITE_FAMILY == (tw3.MODE_X2LITE_3SLOT, tw3.MODE_X2LITE_H50_3SLOT)
    assert tw3.MODE_N1_3SLOT not in tw3.MODES_X2LITE_FAMILY
    assert tw3.MODE_N1_3SLOT in tw3.MODES_3SLOT
    assert tw3.MODES_W1A_FAMILY == tw3.MODES_X2LITE_FAMILY + (tw3.MODE_N1_3SLOT,)


def test_existing_modes_etp_sizing_hold_unchanged():
    x2 = _state(time_window_x2lite_filter_enabled=True)
    h50 = _state(time_window_h50_filter_enabled=True)
    tw2 = _state(time_window_3slot_filter_enabled=True)
    assert etp.thresholds(x2) == (1.5, 1.0)
    assert etp.thresholds(h50) == (1.5, 1.0)
    assert etp.thresholds(tw2) == (1.5, 0.8)
    assert etp.is_enabled(x2) is True and etp.is_enabled(h50) is True
    assert etp.is_enabled(tw2) is False
    assert swh.is_active(h50) is True and swh.is_active(x2) is False
    assert ps.is_active(x2) is True and ps.is_active(h50) is True and ps.is_active(tw2) is False
    for s in (x2, h50, tw2):
        assert na.is_active(s) is False


def test_tp1_override_defaults_to_module_constant():
    """tp1_pct_override 를 주지 않으면 기존 동작과 완전히 같다."""
    for net in (2.9, 3.0, 3.4, 3.5, 5.1):
        a = twpm.evaluate_position(session="MORNING", net_return_pct=net, tp1_done=False)
        b = twpm.evaluate_position(session="MORNING", net_return_pct=net, tp1_done=False,
                                   tp1_pct_override=None)
        assert a == b, net
        t1 = twpm.evaluate_take_profit_immediate(session="MORNING", net_return_pct=net,
                                                 tp1_done=False)
        t2 = twpm.evaluate_take_profit_immediate(session="MORNING", net_return_pct=net,
                                                 tp1_done=False, tp1_pct_override=None)
        assert t1 == t2, net


# ── B. 사양값 ─────────────────────────────────────────────────────────────
def test_n1_fixed_ladder_matches_spec():
    ov = tw3.exit_overrides(tw3.MODE_N1_3SLOT)
    assert ov["stop_loss_pct_override"] == pytest.approx(-1.3)
    assert ov["after_tp1_stop_pct_override"] == pytest.approx(2.0)
    assert ov["trailing_stop_pct_override"] == pytest.approx(1.5)   # X2-lite 2.8
    assert ov["afternoon_tp_pct_override"] == pytest.approx(4.0)    # X2-lite 3.0
    assert ov["tp1_sell_ratio_override"] == pytest.approx(0.2)      # 비추세 기본
    assert tw3.morning_tp2_pct_override(tw3.MODE_N1_3SLOT) == pytest.approx(8.0)
    assert tw3.quality_score_threshold(tw3.MODE_N1_3SLOT) == 3
    assert tw3.requires_chop_teg_gate(tw3.MODE_N1_3SLOT) is True
    assert na.thresholds_etp() == (1.5, 0.8)                        # X2-lite (1.5, 1.0)


def test_n1_mode_membership_and_helpers():
    s = _state(time_window_n1_filter_enabled=True)
    assert tw3.active_3slot_mode(s) == tw3.MODE_N1_3SLOT
    assert tw3.is_3slot_enabled(s) is True
    assert tw3.scheduled_entry_supported(s) is False   # 3-SLOT 계열은 예약매수 금지
    assert na.is_active(s) is True
    assert swh.is_active(s) is True                    # H50 HOLD 를 그대로 쓴다
    assert ps.is_active(s) is True                     # W1a 사이징 공유
    assert etp.is_enabled(s) is True
    assert etp.thresholds(s) == (1.5, 0.8)


def test_n1_ladder_values():
    assert na.trend_ladder() == (pytest.approx(3.5), pytest.approx(0.0), pytest.approx(8.0))
    assert na.off_trend_ladder() == (pytest.approx(3.0), pytest.approx(0.2), pytest.approx(4.0))


def test_kill_switch_disables_n1_adaptive(monkeypatch):
    monkeypatch.setattr(config, "N1_ENABLED", False)
    assert na.is_active(_state(time_window_n1_filter_enabled=True)) is False


# ── C/D. adaptive 판정 ────────────────────────────────────────────────────
def _rising(n=80):
    return [10_000.0 * (1.003 ** i) for i in range(n)]


def _falling(n=80):
    return [10_000.0 * (0.997 ** i) for i in range(n)]


def test_regime_trend_up_gives_trend_ladder():
    bars = _bars(_rising())
    snap = na.snapshot(bars, Direction.UP_RED)
    assert snap.insufficient is False
    assert snap.ok is True and snap.reason == na.REGIME_TREND
    assert snap.close > snap.ema_slow and snap.ema_fast > snap.ema_slow and snap.ema_slow_slope > 0
    ld = na.resolve_ladder(bars, Direction.UP_RED)
    assert (ld.tp1_pct, ld.tp1_sell_ratio, ld.tp2_pct) == na.trend_ladder()
    assert ld.regime_ok is True


def test_regime_is_direction_symmetric():
    up, down = _bars(_rising()), _bars(_falling())
    assert na.snapshot(up, Direction.UP_RED).ok is True
    assert na.snapshot(up, Direction.DOWN_BLUE).ok is False
    assert na.snapshot(down, Direction.DOWN_BLUE).ok is True
    assert na.snapshot(down, Direction.UP_RED).ok is False
    # 역방향 보유는 비추세 래더
    assert na.resolve_ladder(up, Direction.DOWN_BLUE).tp2_pct == pytest.approx(4.0)
    assert na.resolve_ladder(down, Direction.DOWN_BLUE).tp2_pct == pytest.approx(8.0)


def test_off_trend_when_structure_broken():
    # 상승하다 급락 -> close < EMA50 이 되어 비추세
    closes = _rising(70) + [10_000.0 * (1.003 ** 70) * 0.90] * 6
    ld = na.resolve_ladder(_bars(closes), Direction.UP_RED)
    assert ld.regime_ok is False and ld.regime_reason == na.REGIME_OFF_TREND
    assert (ld.tp1_pct, ld.tp1_sell_ratio, ld.tp2_pct) == na.off_trend_ladder()


@pytest.mark.parametrize("n", [0, 1, 30, int(config.H50_TREND_EMA_SLOW)])
def test_insufficient_bars_falls_back_to_off_trend(n):
    bars = _bars(_rising(n)) if n else None
    snap = na.snapshot(bars, Direction.UP_RED)
    assert snap.insufficient is True and snap.ok is False
    assert snap.reason == na.REGIME_INSUFFICIENT
    ld = na.resolve_ladder(bars, Direction.UP_RED)
    assert (ld.tp1_pct, ld.tp1_sell_ratio, ld.tp2_pct) == na.off_trend_ladder()


def test_none_direction_is_off_trend():
    assert na.snapshot(_bars(_rising()), None).ok is False


def test_regime_uses_h50_ema_constants_not_new_ones():
    """새 EMA span/계산식을 만들지 않았다 — H50 상수와 production 헬퍼를 쓴다."""
    import inspect
    src = inspect.getsource(na)
    assert "H50_TREND_EMA_FAST" in src and "H50_TREND_EMA_SLOW" in src
    assert "from app.trading.macd2.major_flag_filter import _ema, _prepare_bars" in src
    assert "ewm(" not in src, "모듈 안에서 EMA 를 직접 계산하면 안 된다"
    assert na._span_fast() == config.H50_TREND_EMA_FAST == 20
    assert na._span_slow() == config.H50_TREND_EMA_SLOW == 50


def test_regime_matches_h50_structural_trend_direction():
    """H50 의 구조추세와 같은 방향 판정인지 대조(같은 EMA 상수를 쓰므로)."""
    up = _bars(_rising())
    assert na.snapshot(up, Direction.UP_RED).ok is True
    assert swh._structural_trend(swh._prepare_bars(up)) == swh.TREND_UP


# ── E. state ──────────────────────────────────────────────────────────────
def test_defaults_are_off():
    assert config.N1_3SLOT_FILTER_DEFAULT is False
    s = RuntimeState()
    assert s.time_window_n1_filter_enabled is False
    assert s.n1_regime_state is None and s.n1_effective_tp2 is None
    assert s.n1_last_eval_bar_ts is None


def test_note_eval_and_cached_ladder_roundtrip():
    s = _state(time_window_n1_filter_enabled=True)
    assert na.cached_ladder(s) is None
    dec = na.resolve_ladder(_bars(_rising()), Direction.UP_RED)
    na.note_eval(s, datetime(2026, 9, 18, 10, 30, tzinfo=KST), dec)
    assert s.n1_regime_state == na.REGIME_TREND
    assert s.n1_effective_tp2 == pytest.approx(8.0)
    got = na.cached_ladder(s)
    assert (got.tp1_pct, got.tp1_sell_ratio, got.tp2_pct) == (dec.tp1_pct, dec.tp1_sell_ratio, dec.tp2_pct)
    assert got.regime_ok is True
    na.clear(s)
    assert na.cached_ladder(s) is None
    assert s.time_window_n1_filter_enabled is True     # 토글은 유지


def test_state_store_roundtrip_and_backcompat():
    s = _state(time_window_n1_filter_enabled=True, n1_regime_state=na.REGIME_TREND,
               n1_effective_tp2=8.0, n1_effective_tp1=3.5, n1_effective_tp1_ratio=0.0,
               n1_last_eval_bar_ts="2026-09-18T10:30:00+09:00")
    r = state_store.deserialize(state_store.serialize(s))
    assert r.time_window_n1_filter_enabled is True
    assert r.n1_regime_state == na.REGIME_TREND
    assert r.n1_effective_tp2 == pytest.approx(8.0)
    assert r.time_window_n1_filter_version == config.N1_3SLOT_FILTER_VERSION
    # 과거 state (N1 키 없음) -> 기본 OFF, 로드 실패 없음
    raw = {k: v for k, v in state_store.serialize(s).items()
           if not k.startswith("n1_") and "n1_filter" not in k}
    r2 = state_store.deserialize(raw)
    assert r2.time_window_n1_filter_enabled is False
    assert r2.n1_regime_state is None and r2.n1_effective_tp2 is None
    assert na.is_active(r2) is False


def test_version_bump_resets_to_default():
    s = _state(time_window_n1_filter_enabled=True)
    raw = state_store.serialize(s)
    raw["time_window_n1_filter_version"] = "N1_OLD"
    r = state_store.deserialize(raw)
    assert r.time_window_n1_filter_enabled is bool(config.N1_3SLOT_FILTER_DEFAULT) is False


def test_explicit_other_strategy_wins_over_n1_default():
    s = _state(time_window_n1_filter_enabled=True)
    raw = state_store.serialize(s)
    raw["time_window_h50_filter_enabled"] = True
    assert state_store.deserialize(raw).time_window_n1_filter_enabled is False


def test_day_rollover_clears_adaptive_cache_but_keeps_toggle():
    from app.trading.macd2.worker import _apply_day_rollover
    s = _state(time_window_n1_filter_enabled=True, n1_regime_state=na.REGIME_TREND,
               n1_effective_tp2=8.0, n1_effective_tp1=3.5, n1_effective_tp1_ratio=0.0,
               n1_last_eval_bar_ts="2026-09-18T14:00:00+09:00", session_date="20260918")
    _apply_day_rollover(s, datetime(2026, 9, 21, 8, 50, tzinfo=KST))
    assert s.n1_regime_state is None and s.n1_effective_tp2 is None
    assert s.n1_last_eval_bar_ts is None
    assert s.time_window_n1_filter_enabled is True
