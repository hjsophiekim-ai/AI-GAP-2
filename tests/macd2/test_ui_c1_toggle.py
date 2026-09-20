"""C1 Peak Protection UI 토글 렌더링 테스트 (2026-09-19).

tests/macd2/test_ui_early_take_profit_toggle.py 와 같은 하네스를 그대로 쓴다
(streamlit.testing.v1.AppTest + conftest.py 의 autouse tmp_path 격리).
실제 KIS/브로커/Worker 는 전혀 건드리지 않는다.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import config, state_store

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
_TOGGLE_KEY = "macd2_c1_peak_protection_toggle"
_LABEL = "└ C1 Peak Protection"

# test_ui_early_take_profit_toggle.py 와 같은 기존 페이지 결함(전체 스위트에서
# st.button inside st.form 으로 렌더 자체가 죽는다) 가드. C1 과 무관하다.
_PREEXISTING_FORM_BUG = "can't be used in an `st.form()`"


def _fresh_app() -> AppTest:
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["app_auth_authenticated"] = True
    return at


def _guard(at: AppTest) -> AppTest:
    for exc in at.exception:
        if _PREEXISTING_FORM_BUG in str(getattr(exc, "value", "") or ""):
            pytest.skip("기존 페이지 결함(st.button inside st.form) — C1 토글과 무관")
    assert not at.exception
    return at


def _run(at: AppTest) -> AppTest:
    at.run()
    return _guard(at)


def _toggle(at: AppTest):
    for cb in at.checkbox:
        if cb.label == _LABEL:
            return cb
    raise AssertionError(
        f"C1 체크박스가 렌더링되지 않았다. 렌더된 체크박스: {[c.label for c in at.checkbox]!r}")


def _set_mode(*, n1: bool = False, h50: bool = False, x2lite: bool = False,
              tw2_3slot: bool = False) -> None:
    state = state_store.load_state()
    state.time_window_n1_filter_enabled = bool(n1)
    state.time_window_h50_filter_enabled = bool(h50)
    state.time_window_x2lite_filter_enabled = bool(x2lite)
    state.time_window_3slot_filter_enabled = bool(tw2_3slot)
    state.time_window_twf_filter_enabled = False
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    if not n1:
        state.c1_peak_protection_enabled = False
    state_store.save_state(state)


# ── 렌더 / 기본값 ─────────────────────────────────────────────────────────
def test_toggle_renders_and_defaults_off_on_n1():
    _set_mode(n1=True)
    at = _run(_fresh_app())
    cb = _toggle(at)
    assert cb.disabled is False
    assert cb.value is False, "기본값은 반드시 OFF"
    assert state_store.load_state().c1_peak_protection_enabled is False


def test_toggle_is_disabled_outside_n1():
    _set_mode(tw2_3slot=True)
    at = _run(_fresh_app())
    cb = _toggle(at)
    assert cb.disabled is True, "N1 계열이 아니면 비활성이어야 한다"
    assert cb.value is False
    assert any("N1 을 켜야" in c.value for c in at.caption), (
        f"자동 비활성 안내 캡션이 없다: {[c.value for c in at.caption]!r}")


def test_toggle_renders_below_the_n1_toggle():
    _set_mode(n1=True)
    at = _run(_fresh_app())
    labels = [c.label for c in at.checkbox]
    assert _LABEL in labels
    assert labels.index(_LABEL) > labels.index(config.N1_3SLOT_STRATEGY_NAME), (
        f"C1 토글이 N1 토글보다 위에 있다: {labels!r}")


# ── ON/OFF 저장 · 재시작 복원 ─────────────────────────────────────────────
def test_checking_turns_it_on_and_persists():
    _set_mode(n1=True)
    at = _run(_fresh_app())
    _toggle(at).check().run()
    _guard(at)
    reloaded = state_store.load_state()
    assert reloaded.c1_peak_protection_enabled is True
    assert reloaded.c1_peak_protection_version == config.C1_FILTER_VERSION
    assert _toggle(at).value is True


def test_unchecking_turns_it_off_and_persists():
    _set_mode(n1=True)
    state = state_store.load_state()
    state.c1_peak_protection_enabled = True
    state.c1_peak_protection_version = config.C1_FILTER_VERSION
    state.c1_armed = True
    state.c1_peak_net_return = 6.1
    state_store.save_state(state)

    at = _run(_fresh_app())
    assert _toggle(at).value is True
    _toggle(at).uncheck().run()
    _guard(at)
    reloaded = state_store.load_state()
    assert reloaded.c1_peak_protection_enabled is False
    # 끄면 보유기간 상태도 정리된다(포지션은 건드리지 않는다)
    assert reloaded.c1_armed is False
    assert reloaded.c1_peak_net_return == 0.0


def test_restart_restores_the_on_state():
    """재시작 = 새 AppTest 인스턴스로 state 를 다시 읽는 것."""
    _set_mode(n1=True)
    at = _run(_fresh_app())
    _toggle(at).check().run()
    _guard(at)
    at2 = _run(_fresh_app())
    assert _toggle(at2).value is True
    assert state_store.load_state().c1_peak_protection_enabled is True


def test_switching_away_from_n1_clears_the_widget_state():
    _set_mode(n1=True)
    at = _run(_fresh_app())
    _toggle(at).check().run()
    _guard(at)
    assert state_store.load_state().c1_peak_protection_enabled is True

    _set_mode(tw2_3slot=True)          # 모드 전환(다른 경로로 바뀐 상황)
    at2 = _run(_fresh_app())
    cb = _toggle(at2)
    assert cb.value is False and cb.disabled is True
    try:
        widget_state = at2.session_state[_TOGGLE_KEY]
    except KeyError:
        widget_state = False
    assert widget_state is False, "위젯 상태가 True 로 남아 재활성화를 시도하면 안 된다"
    at2.run(); _guard(at2)
    assert state_store.load_state().c1_peak_protection_enabled is False


# ── 도움말 / 진단 캡션 ────────────────────────────────────────────────────
def test_help_text_states_rule_thresholds_priority_and_caveats():
    _set_mode(n1=True)
    at = _run(_fresh_app())
    help_text = _toggle(at).help or ""
    assert f"+{config.C1_ARM_MFE_PCT:.1f}%" in help_text
    assert f"{config.C1_GIVEBACK_PCT:.1f}%p" in help_text
    assert "MACD gap" in help_text and "반전" in help_text
    assert "단순 gap 축소로는 발동하지 않습니다" in help_text
    assert "기존 청산이 항상 우선" in help_text
    assert "PROMISING" in help_text, "등급/한계가 도움말에 남아 있어야 한다"
    assert "7건" in help_text, "발동 표본이 7건뿐이라는 한계가 있어야 한다"
    assert "기본 OFF" in help_text


def test_diagnostic_caption_shows_thresholds_and_arm_state():
    _set_mode(n1=True)
    state = state_store.load_state()
    state.c1_peak_protection_enabled = True
    state.c1_peak_protection_version = config.C1_FILTER_VERSION
    state.time_window_position_active = True
    state.time_window_active_mode = "N1_3SLOT"
    state.c1_peak_net_return = 6.25
    state.c1_armed = True
    state_store.save_state(state)

    at = _run(_fresh_app())
    captions = [c.value for c in at.caption]
    assert any("C1 Peak Protection=ON" in c for c in captions), f"상태 캡션 없음: {captions!r}"
    assert any("현재 MFE +6.25%" in c for c in captions)
    assert any("ARMED" in c for c in captions)


# ── 청산사유 한글 라벨 ────────────────────────────────────────────────────
def test_exit_reason_label_is_localized():
    src = Path(_APP_PATH).read_text(encoding="utf-8")
    assert 'macd2_config.EXIT_C1_PEAK_PROTECTION: "C1 고점보호"' in src


def test_off_state_does_not_change_existing_ui_behaviour():
    """C1 OFF 면 기존 토글/캡션이 그대로다 — 조기익절 토글이 같은 자리에
    같은 조건으로 계속 렌더되는지 확인."""
    _set_mode(tw2_3slot=True)
    at = _run(_fresh_app())
    labels = [c.label for c in at.checkbox]
    assert "└ 조기익절 필터" in labels
    assert labels.index("└ C1 Peak Protection") == labels.index("└ 조기익절 필터") + 1
    assert state_store.load_state().c1_peak_protection_enabled is False
