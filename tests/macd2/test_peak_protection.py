"""C1 Peak Protection — 순수 모듈 / config / state 계약 테스트 (2026-09-19).

worker 경로는 tests/macd2/test_peak_protection_worker.py 가 따로 고정한다.

고정하는 계약
-------------
A. 기본값 OFF — config / RuntimeState / state_store 어디서도 저절로 켜지지 않음
B. arm     — MFE < C1_ARM_MFE_PCT 면 미무장, >= 면 무장
C. giveback— peak 대비 < C1_GIVEBACK_PCT 면 HOLD, >= 면 조건 후보
D. gap     — giveback 을 채워도 gap 반전이 없으면 HOLD, 둘 다면 EXIT
             (단순 축소로는 절대 발동하지 않는다)
E. 방향대칭— UP_RED / DOWN_BLUE 가 완전히 대칭
H. restart — arm 상태 저장/복원 라운드트립
I. rollover— 일자변경 시 전일 C1 상태 잔존 0
"""
from __future__ import annotations

import pytest

from app.trading.macd2 import config, peak_protection as pp, state_store
from app.trading.macd2 import time_window_3slot as tw3
from app.trading.macd2.models import Direction, RuntimeState

ARM = float(config.C1_ARM_MFE_PCT)
GIVE = float(config.C1_GIVEBACK_PCT)


def _state(**kw) -> RuntimeState:
    s = RuntimeState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _eval(direction, peak, net, hist):
    return pp.evaluate(
        held_direction=direction, peak_net_return_pct=peak,
        net_return_pct=net, macd_hist=hist,
    )


# ── A. 기본값 OFF ─────────────────────────────────────────────────────────
def test_config_defaults_are_off_and_documented():
    assert config.C1_FILTER_DEFAULT is False
    assert config.C1_ARM_MFE_PCT == pytest.approx(5.0)
    assert config.C1_GIVEBACK_PCT == pytest.approx(1.5)
    assert config.EXIT_C1_PEAK_PROTECTION == "C1_PEAK_PROTECTION_EXIT"
    assert pp.EXIT_C1_PEAK_PROTECTION == config.EXIT_C1_PEAK_PROTECTION


def test_runtime_state_default_is_off():
    s = RuntimeState()
    assert s.c1_peak_protection_enabled is False
    assert s.c1_armed is False
    assert s.c1_peak_net_return == 0.0
    assert s.c1_last_checked_bar_ts is None
    assert s.c1_triggered_at is None


def test_is_active_requires_toggle_and_n1_family_mode():
    s = _state(time_window_h50_filter_enabled=True)
    assert pp.is_active(s) is False                 # 토글 OFF
    s.c1_peak_protection_enabled = True
    assert pp.is_active(s) is True                  # H50 = N1 계열
    s.time_window_h50_filter_enabled = False
    s.time_window_x2lite_filter_enabled = True
    assert pp.is_active(s) is True                  # X2-lite 도 N1 계열
    s.time_window_x2lite_filter_enabled = False
    s.time_window_3slot_filter_enabled = True
    assert pp.is_active(s) is False                 # TW2 3-SLOT 은 대상 아님
    assert pp.is_enabled(s) is True                 # 토글 자체는 켜져 있음


def test_kill_switch_disables_everything(monkeypatch):
    monkeypatch.setattr(config, "C1_ENABLED", False)
    s = _state(time_window_h50_filter_enabled=True, c1_peak_protection_enabled=True)
    assert pp.is_enabled(s) is False
    assert pp.is_active(s) is False


# ── B. arm ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("peak, armed", [
    (0.0, False), (ARM - 0.01, False), (ARM, True), (ARM + 3.0, True),
])
def test_arm_threshold(peak, armed):
    # gap 반전 + 충분한 반납을 줘도 arm 이 안 됐으면 절대 발동하지 않는다.
    d = _eval(Direction.UP_RED, peak, peak - GIVE - 1.0, -10.0)
    assert d.armed is armed
    if not armed:
        assert d.exit_reason is None
        assert d.label == pp.LABEL_NOT_ARMED


# ── C. giveback ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("give, fires", [
    (0.0, False), (GIVE - 0.01, False), (GIVE, True), (GIVE + 2.0, True),
])
def test_giveback_threshold(give, fires):
    peak = ARM + 1.2
    d = _eval(Direction.UP_RED, peak, peak - give, -10.0)
    assert d.armed is True
    assert d.gap_reversed is True
    assert (d.exit_reason == pp.EXIT_C1_PEAK_PROTECTION) is fires
    if not fires:
        assert d.label == pp.LABEL_GIVEBACK_TOO_SMALL


# ── D. gap ────────────────────────────────────────────────────────────────
def test_gap_must_reverse_not_merely_shrink():
    peak = ARM + 1.2
    net = peak - GIVE - 0.5          # 반납 조건은 충분히 만족
    # gap 이 크게 줄었지만 **부호는 여전히 보유방향** -> HOLD
    for hist in (100.0, 10.0, 1.0, 0.0001):
        d = _eval(Direction.UP_RED, peak, net, hist)
        assert d.armed is True
        assert d.gap_reversed is False
        assert d.exit_reason is None
        assert d.label == pp.LABEL_NO_GAP_REVERSAL
    # 부호가 넘어가면(0 포함) 발동
    for hist in (0.0, -0.0001, -50.0):
        d = _eval(Direction.UP_RED, peak, net, hist)
        assert d.gap_reversed is True
        assert d.exit_reason == pp.EXIT_C1_PEAK_PROTECTION
        assert d.sell_fraction == 1.0


def test_gap_reversal_alone_is_not_enough():
    peak = ARM + 1.2
    d = _eval(Direction.UP_RED, peak, peak - 0.1, -50.0)   # 반납 부족
    assert d.gap_reversed is True and d.exit_reason is None


def test_missing_hist_never_fires():
    peak = ARM + 1.2
    d = _eval(Direction.UP_RED, peak, peak - GIVE - 1.0, None)
    assert d.gap_reversed is False and d.exit_reason is None
    assert pp.gap_reversed(None, -1.0) is False
    assert pp.gap_reversed(Direction.UP_RED, "nan-ish") is False


# ── E. 방향대칭 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("direction, fire_hist, hold_hist", [
    (Direction.UP_RED, -25.0, 25.0),
    (Direction.DOWN_BLUE, 25.0, -25.0),
])
def test_direction_symmetry(direction, fire_hist, hold_hist):
    peak = ARM + 1.5
    net = peak - GIVE - 0.2
    fired = _eval(direction, peak, net, fire_hist)
    held = _eval(direction, peak, net, hold_hist)
    assert fired.exit_reason == pp.EXIT_C1_PEAK_PROTECTION
    assert held.exit_reason is None
    # 같은 입력에서 두 방향의 판정 구조가 완전히 대칭인지 직접 확인
    assert fired.armed is held.armed is True
    assert fired.giveback_pct == pytest.approx(held.giveback_pct)


def test_zero_gap_counts_as_reversal_for_both_directions():
    assert pp.gap_reversed(Direction.UP_RED, 0.0) is True
    assert pp.gap_reversed(Direction.DOWN_BLUE, 0.0) is True


# ── 임계값 override ───────────────────────────────────────────────────────
def test_threshold_overrides_are_respected():
    d = pp.evaluate(held_direction=Direction.UP_RED, peak_net_return_pct=4.6,
                    net_return_pct=3.5, macd_hist=-1.0,
                    arm_pct=4.5, giveback_pct=1.0)
    assert d.exit_reason == pp.EXIT_C1_PEAK_PROTECTION
    d2 = pp.evaluate(held_direction=Direction.UP_RED, peak_net_return_pct=4.6,
                     net_return_pct=3.5, macd_hist=-1.0,
                     arm_pct=5.0, giveback_pct=1.0)
    assert d2.exit_reason is None and d2.armed is False
    assert pp.thresholds(RuntimeState()) == (ARM, GIVE)


# ── state 헬퍼 ────────────────────────────────────────────────────────────
def test_note_peak_is_monotonic_and_clear_resets_only_position_state():
    s = _state(c1_peak_protection_enabled=True, time_window_h50_filter_enabled=True)
    pp.note_peak(s, 2.0); pp.note_peak(s, 6.4); pp.note_peak(s, 1.0)
    assert s.c1_peak_net_return == pytest.approx(6.4)
    pp.note_armed(s, "2026-09-19T10:00:00+09:00")
    pp.note_armed(s, "2026-09-19T10:30:00+09:00")   # 최초 시각만 남는다
    assert s.c1_armed_at == "2026-09-19T10:00:00+09:00"
    pp.note_checked_bar(s, "2026-09-19T10:33:00+09:00")
    pp.note_triggered(s, "2026-09-19T10:36:00+09:00")
    assert pp.is_armed(s) is True
    pp.clear(s)
    assert (s.c1_armed, s.c1_armed_at, s.c1_peak_net_return,
            s.c1_last_checked_bar_ts, s.c1_triggered_at) == (False, None, 0.0, None, None)
    # 토글은 사용자 설정이므로 clear 가 건드리지 않는다
    assert s.c1_peak_protection_enabled is True


# ── H. restart 라운드트립 / 하위호환 ──────────────────────────────────────
def test_state_store_roundtrip_preserves_arm():
    s = _state(time_window_h50_filter_enabled=True, c1_peak_protection_enabled=True,
               c1_armed=True, c1_peak_net_return=6.25,
               c1_armed_at="2026-09-19T10:00:00+09:00",
               c1_last_checked_bar_ts="2026-09-19T10:33:00+09:00")
    r = state_store.deserialize(state_store.serialize(s))
    assert r.c1_peak_protection_enabled is True
    assert r.c1_armed is True
    assert r.c1_peak_net_return == pytest.approx(6.25)
    assert r.c1_armed_at == "2026-09-19T10:00:00+09:00"
    assert r.c1_last_checked_bar_ts == "2026-09-19T10:33:00+09:00"
    assert r.c1_peak_protection_version == config.C1_FILTER_VERSION


def test_legacy_state_without_c1_fields_loads_off():
    s = _state(time_window_h50_filter_enabled=True)
    raw = {k: v for k, v in state_store.serialize(s).items() if not k.startswith("c1_")}
    r = state_store.deserialize(raw)
    assert r.c1_peak_protection_enabled is False    # 마이그레이션으로 켜지지 않는다
    assert r.c1_armed is False
    assert r.c1_peak_net_return == 0.0
    assert pp.is_active(r) is False


def test_stored_on_is_forced_off_outside_n1_family():
    s = _state(time_window_h50_filter_enabled=True, c1_peak_protection_enabled=True)
    raw = state_store.serialize(s)
    raw["time_window_h50_filter_enabled"] = False
    raw["time_window_x2lite_filter_enabled"] = False
    raw["time_window_3slot_filter_enabled"] = True
    assert state_store.deserialize(raw).c1_peak_protection_enabled is False


def test_version_bump_resets_toggle_to_default(monkeypatch):
    s = _state(time_window_h50_filter_enabled=True, c1_peak_protection_enabled=True)
    raw = state_store.serialize(s)
    raw["c1_peak_protection_version"] = "C1_SOMETHING_OLD"
    r = state_store.deserialize(raw)
    assert r.c1_peak_protection_enabled is bool(config.C1_FILTER_DEFAULT) is False
    assert r.c1_peak_protection_version == config.C1_FILTER_VERSION


# ── I. day rollover ───────────────────────────────────────────────────────
def test_day_rollover_clears_c1_position_state():
    from datetime import datetime

    from app.trading.macd2.worker import _apply_day_rollover
    s = _state(time_window_h50_filter_enabled=True, c1_peak_protection_enabled=True,
               c1_armed=True, c1_peak_net_return=6.1,
               c1_armed_at="2026-09-18T10:00:00+09:00",
               c1_last_checked_bar_ts="2026-09-18T14:00:00+09:00",
               c1_triggered_at="2026-09-18T14:03:00+09:00",
               session_date="20260918")
    _apply_day_rollover(s, datetime(2026, 9, 19, 8, 50, tzinfo=config.KST))
    assert s.c1_armed is False
    assert s.c1_peak_net_return == 0.0
    assert s.c1_armed_at is None
    assert s.c1_last_checked_bar_ts is None
    assert s.c1_triggered_at is None
    assert s.c1_peak_protection_enabled is True     # 토글은 유지


# ── 모드 판정이 N1 계열 집합과 일치하는가 ────────────────────────────────
def test_supported_mode_matches_x2lite_family():
    for flag, expected in (("time_window_x2lite_filter_enabled", True),
                           ("time_window_h50_filter_enabled", True),
                           ("time_window_3slot_filter_enabled", False),
                           ("time_window_twf_filter_enabled", False)):
        s = _state(**{flag: True})
        assert pp.is_supported_mode(s) is expected, flag
        if expected:
            assert tw3.active_3slot_mode(s) in tw3.MODES_X2LITE_FAMILY
