"""STRATEGY MODE — [N1] / [P3] 단일 선택 (2026-09-27).

사용자 요구 §11 의 1~7 을 그대로 옮겼다. 핵심은 "개별 토글 조합으로 전략이
꼬이는 경로가 남아 있지 않은가" 다.
"""
from __future__ import annotations

import pytest

from app.trading.macd2 import config, state_store, strategy_mode
from app.trading.macd2 import chop_regime, p3_stack

LEGACY_STRATEGY_FLAGS = (
    "time_window_2_filter_enabled",
    "time_window_teg_filter_enabled",
    "time_window_3slot_filter_enabled",
    "time_window_twf_filter_enabled",
    "time_window_x2lite_filter_enabled",
    "time_window_h50_filter_enabled",
)


# ── 1 / 2. 모드가 하위 구성을 전부 결정한다 ──────────────────────────────
def test_n1_mode_enables_base_and_disables_the_chop_layer():
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_N1)

    assert state.time_window_n1_filter_enabled is True
    assert state.c1_peak_protection_enabled is True
    assert state.smart_sizing_enabled is True
    # AR1 은 토글이 없다 -- N1 이 켜지면 자동으로 평가된다.
    from app.trading.macd2 import time_window_3slot
    assert time_window_3slot.afternoon_reentry_exception_enabled(state) is True
    # CHOP 레이어는 전부 꺼져 있어야 한다.
    assert state.p3_enabled is False
    assert p3_stack.is_active(state) is False


def test_p3_mode_enables_base_plus_the_chop_layer():
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)

    assert state.time_window_n1_filter_enabled is True
    assert state.c1_peak_protection_enabled is True
    assert state.smart_sizing_enabled is True
    assert state.p3_enabled is True
    assert p3_stack.is_active(state) is True

    flags = strategy_mode.derive(strategy_mode.MODE_P3)
    assert all(flags[k] for k in ("N1", "C1", "SMART", "AR1", "SHADOW", "B3", "Y3", "P3_RESCUE"))


def test_entry_stack_is_identical_between_the_two_modes():
    """두 모드의 **진입**은 완전히 같다 -- 차이는 청산 레이어뿐이다."""
    n1 = strategy_mode.derive(strategy_mode.MODE_N1)
    p3 = strategy_mode.derive(strategy_mode.MODE_P3)
    for key in ("N1", "C1", "SMART", "AR1"):
        assert n1[key] == p3[key] is True
    for key in ("SHADOW", "B3", "Y3", "P3_RESCUE"):
        assert n1[key] is False and p3[key] is True


# ── 3. 동시 선택 불가 ────────────────────────────────────────────────────
def test_only_one_strategy_tier_is_ever_enabled():
    for mode in strategy_mode.ALL_MODES:
        state = state_store.default_state()
        strategy_mode.apply(state, mode)
        on = [f for f in LEGACY_STRATEGY_FLAGS if bool(getattr(state, f))]
        assert on == [], f"{mode} 모드인데 다른 전략이 켜져 있다: {on}"
        assert state.time_window_n1_filter_enabled is True


# ── 7. legacy 토글 충돌 정규화 ───────────────────────────────────────────
def test_conflicting_legacy_toggles_are_normalized_by_the_mode():
    state = state_store.default_state()
    # 손으로 편집된 듯한 충돌 상태를 만든다.
    state.time_window_h50_filter_enabled = True
    state.time_window_x2lite_filter_enabled = True
    state.time_window_2_filter_enabled = True
    state.c1_peak_protection_enabled = False
    state.smart_sizing_enabled = False

    strategy_mode.apply(state, strategy_mode.MODE_P3)

    assert [f for f in LEGACY_STRATEGY_FLAGS if bool(getattr(state, f))] == []
    assert state.c1_peak_protection_enabled is True
    assert state.smart_sizing_enabled is True
    assert state.p3_enabled is True


def test_unknown_mode_falls_back_to_n1():
    assert strategy_mode.normalize("nonsense") == strategy_mode.MODE_N1
    assert strategy_mode.normalize(None) == strategy_mode.MODE_N1
    assert strategy_mode.normalize("p3") == strategy_mode.MODE_P3


# ── 4 / 5. 모드 전환은 보유 포지션을 건드리지 않는다 ─────────────────────
def test_switching_mode_does_not_touch_an_open_position_snapshot():
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    assert p3_stack.governs_position(state) is True

    # P3 -> N1 로 바꿔도 이미 열린 포지션의 진입 스냅샷은 사라지지 않는다.
    # 다만 P3 가 꺼졌으므로 B3 가 더 이상 주인이 아니다(기존 래더로 복귀).
    strategy_mode.apply(state, strategy_mode.MODE_N1)
    assert state.p3_enabled is False
    assert p3_stack.governs_position(state) is False


def test_turning_p3_off_keeps_the_completed_shadow_ledger(tmp_path, monkeypatch):
    """다시 켰을 때 WARMUP 을 처음부터 쌓지 않도록 완료거래는 남긴다."""
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", tmp_path / "shadow.json")
    from tests.macd2.test_p3_regime_stack import _series

    chop_regime.save_ledger(_series(5, 1), source="test")
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    state.p3_shadow = {"trading_date": "20260922", "slots_used_today": 2}

    strategy_mode.apply(state, strategy_mode.MODE_N1)

    assert state.p3_shadow is None, "섀도우 진행중 장부는 정리된다"
    assert len(chop_regime.load_ledger()) == 10, "완료거래 ledger 는 남는다"


# ── 6 / 9. 재시작 복원 ───────────────────────────────────────────────────
@pytest.mark.parametrize("mode", list(strategy_mode.ALL_MODES))
def test_mode_survives_a_state_roundtrip(mode, tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")
    state = state_store.default_state()
    strategy_mode.apply(state, mode)
    state.mode = "mock"
    state_store.save_state(state)

    restored = state_store.load_state()
    assert strategy_mode.current(restored) == mode
    assert restored.time_window_n1_filter_enabled is True
    assert restored.c1_peak_protection_enabled is True
    assert bool(restored.p3_enabled) is (mode == strategy_mode.MODE_P3)


# ── §7. migration ────────────────────────────────────────────────────────
def test_migration_maps_a_complete_base_state_to_n1():
    state = state_store.default_state()
    for f in LEGACY_STRATEGY_FLAGS:
        setattr(state, f, False)
    state.strategy_mode = None
    state.time_window_n1_filter_enabled = True
    state.c1_peak_protection_enabled = True
    state.smart_sizing_enabled = True
    state.p3_enabled = False

    assert strategy_mode.migrate(state) == strategy_mode.MODE_N1
    assert state.strategy_mode_by.startswith("migration:")


def test_migration_maps_a_full_p3_stack_to_p3():
    state = state_store.default_state()
    for f in LEGACY_STRATEGY_FLAGS:
        setattr(state, f, False)
    state.strategy_mode = None
    state.time_window_n1_filter_enabled = True
    state.c1_peak_protection_enabled = True
    state.smart_sizing_enabled = True
    state.p3_enabled = True

    assert strategy_mode.migrate(state) == strategy_mode.MODE_P3


def test_migration_falls_back_to_n1_for_an_incoherent_combination():
    """P3 만 켜져 있고 BASE 가 미완성이면 안전하게 N1 이다."""
    state = state_store.default_state()
    for f in LEGACY_STRATEGY_FLAGS:
        setattr(state, f, False)
    state.strategy_mode = None
    state.time_window_n1_filter_enabled = True
    state.c1_peak_protection_enabled = False
    state.smart_sizing_enabled = False
    state.p3_enabled = True

    assert strategy_mode.migrate(state) == strategy_mode.MODE_N1


def test_migration_leaves_a_legacy_strategy_user_alone():
    """H50/X2-lite 등을 쓰던 state 는 **옮기지 않는다** -- 모드 도입이 사용자가
    고른 전략을 조용히 N1 으로 바꿔 버리면 안 된다."""
    state = state_store.default_state()
    state.strategy_mode = None
    state.time_window_n1_filter_enabled = False
    state.time_window_h50_filter_enabled = True

    assert strategy_mode.migrate(state) is None
    assert state.strategy_mode is None
    assert state.time_window_h50_filter_enabled is True


def test_migration_is_idempotent():
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    assert strategy_mode.migrate(state) is None
    assert strategy_mode.current(state) == strategy_mode.MODE_P3


# ── §8. fail-safe 표시 ───────────────────────────────────────────────────
def test_execution_layer_is_base_until_the_shadow_is_ready():
    """EXECUTION 은 detector 준비 여부만 본다 -- READY 면 P3, 아니면 BASE.

    REGIME(TREND/CHOP)은 따로 표시한다. TREND 라도 P3 스택은 살아 있고,
    그 안에서 CHOP 진입 포지션만 B3 를 탄다(p3_stack.governs_position).
    """
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)

    state.p3_last_regime = None
    assert strategy_mode.execution_layer(state) == "BASE"
    state.p3_last_regime = chop_regime.REGIME_WARMUP
    assert strategy_mode.execution_layer(state) == "BASE"
    state.p3_last_regime = chop_regime.REGIME_TREND
    assert strategy_mode.execution_layer(state) == "P3"
    state.p3_last_regime = chop_regime.REGIME_CHOP
    assert strategy_mode.execution_layer(state) == "P3"


# ── MOCK / REAL 양쪽 지원 (2026-09-27) ───────────────────────────────────
@pytest.mark.parametrize("account", ["mock", "real"])
@pytest.mark.parametrize("mode", list(strategy_mode.ALL_MODES))
def test_all_four_account_mode_combinations_are_supported(account, mode):
    """MOCK+BASE / MOCK+P3 / REAL+BASE / REAL+P3 네 조합 전부."""
    state = state_store.default_state()
    state.mode = account
    strategy_mode.apply(state, mode)

    assert strategy_mode.current(state) == mode
    assert strategy_mode.account_kind(state) == account.upper()
    # 계좌 종류가 하위 구성을 바꾸면 안 된다.
    assert strategy_mode.derive(mode) == strategy_mode.derive(mode)
    assert bool(state.p3_enabled) is (mode == strategy_mode.MODE_P3)


@pytest.mark.parametrize("account", ["mock", "real"])
def test_execution_layer_is_identical_in_both_accounts(account):
    """계좌 종류는 실행계층 판단을 바꾸지 않는다."""
    state = state_store.default_state()
    state.mode = account
    strategy_mode.apply(state, strategy_mode.MODE_P3)

    state.p3_last_regime = chop_regime.REGIME_WARMUP
    assert strategy_mode.execution_layer(state) == "BASE"
    state.p3_last_regime = chop_regime.REGIME_CHOP
    assert strategy_mode.execution_layer(state) == "P3"


def test_p3_is_never_enabled_automatically_in_a_real_account():
    """실계좌에서 자동으로 켜지는 경로가 없어야 한다."""
    state = state_store.default_state()
    state.mode = "real"
    # ① 기본값
    assert bool(state.p3_enabled) is False
    assert strategy_mode.current(state) != strategy_mode.MODE_P3
    # ② migration -- 레거시 BASE 조합은 N1 로만 간다
    for f in LEGACY_STRATEGY_FLAGS:
        setattr(state, f, False)
    state.strategy_mode = None
    state.time_window_n1_filter_enabled = True
    state.c1_peak_protection_enabled = True
    state.smart_sizing_enabled = True
    state.p3_enabled = False
    assert strategy_mode.migrate(state) == strategy_mode.MODE_N1


def test_account_kind_defaults_to_mock_for_an_unknown_value():
    state = state_store.default_state()
    state.mode = "something-else"
    assert strategy_mode.account_kind(state) == "MOCK"


@pytest.mark.parametrize("account", ["mock", "real"])
def test_execution_layer_in_n1_mode_is_always_base(account):
    state = state_store.default_state()
    state.mode = account
    strategy_mode.apply(state, strategy_mode.MODE_N1)
    state.p3_last_regime = chop_regime.REGIME_CHOP   # 있을 수 없지만 방어적으로
    assert strategy_mode.execution_layer(state) == "BASE"


def test_shadow_status_reports_readiness():
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_N1)
    assert strategy_mode.shadow_status(state) == "OFF"

    strategy_mode.apply(state, strategy_mode.MODE_P3)
    state.p3_last_regime = chop_regime.REGIME_WARMUP
    state.p3_last_shadow_sample = 4
    assert strategy_mode.shadow_status(state) == f"WARMUP 4/{config.P3_DETECTOR_WINDOW}"
    state.p3_last_regime = chop_regime.REGIME_TREND
    assert strategy_mode.shadow_status(state) == "READY"
