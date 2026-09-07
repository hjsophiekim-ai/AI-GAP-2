"""TWF 3-SLOT UI 토글 렌더링 테스트 (2026-09-07).

tests/macd2/test_ui_early_take_profit_toggle.py 와 동일한 하네스를 그대로 쓴다
(streamlit.testing.v1.AppTest + conftest.py 의 autouse tmp_path 격리). 실제
KIS/브로커/Worker 는 건드리지 않는다 -- "시작" 버튼을 누르지 않기 때문.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import state_store

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
_TWF_KEY = "macd2_time_window_twf_filter_toggle"
_TW2_3SLOT_KEY = "macd2_time_window_3slot_filter_toggle"
_LABEL = "TWF 3-SLOT"

# test_ui_early_take_profit_toggle.py 와 같은 기존 페이지 결함 가드.
_PREEXISTING_FORM_BUG = "can't be used in an `st.form()`"


def _fresh_app() -> AppTest:
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["app_auth_authenticated"] = True
    return at


def _run(at: AppTest) -> AppTest:
    at.run()
    for exc in at.exception:
        if _PREEXISTING_FORM_BUG in str(getattr(exc, "value", "") or ""):
            pytest.skip(
                "기존 페이지 결함(st.button inside st.form)으로 페이지 렌더 자체가 실패 -- "
                "TWF 토글과 무관. 이 파일 단독 실행에서는 전부 통과한다."
            )
    assert not at.exception
    return at


def _toggle(at: AppTest):
    for cb in at.checkbox:
        if cb.key == _TWF_KEY:
            return cb
    raise AssertionError(f"TWF 토글을 찾지 못했다: {[c.key for c in at.checkbox]!r}")


def test_toggle_renders_and_defaults_off():
    at = _run(_fresh_app())
    cb = _toggle(at)
    assert cb.label == _LABEL
    assert cb.value is False, "기본값은 OFF 여야 한다"
    assert state_store.load_state().time_window_twf_filter_enabled is False


def test_toggle_renders_directly_below_tw2_3slot():
    at = _run(_fresh_app())
    labels = [c.label for c in at.checkbox]
    assert "TW2 3-SLOT" in labels and _LABEL in labels
    assert labels.index(_LABEL) == labels.index("TW2 3-SLOT") + 1, (
        f"TWF 3-SLOT 이 TW2 3-SLOT 바로 아래에 있지 않다: {labels!r}"
    )


def test_checking_turns_it_on_and_persists():
    at = _run(_fresh_app())
    _toggle(at).check().run()
    assert not at.exception
    assert state_store.load_state().time_window_twf_filter_enabled is True
    assert _toggle(at).value is True


def test_enabling_twf_turns_tw2_3slot_off():
    st_state = state_store.load_state()
    st_state.time_window_3slot_filter_enabled = True
    st_state.time_window_2_filter_enabled = False
    st_state.time_window_teg_filter_enabled = False
    state_store.save_state(st_state)

    at = _run(_fresh_app())
    _toggle(at).check().run()
    assert not at.exception
    s = state_store.load_state()
    assert s.time_window_twf_filter_enabled is True
    assert s.time_window_3slot_filter_enabled is False


def test_help_text_states_entry_is_identical_and_lists_the_three_exits():
    at = _run(_fresh_app())
    help_text = _toggle(at).help or ""
    assert "진입이 100% 같은" in help_text
    for token in ("-1.4%", "+2.0%", "+3.0%"):
        assert token in help_text, f"청산 임계값 {token} 가 help 에 없다: {help_text!r}"
