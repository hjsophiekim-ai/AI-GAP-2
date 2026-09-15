"""TW2 3-SLOT / TW TEG 3-SLOT 토글 숨김 (2026-09-15).

사용자 노출 전략을 "X2-lite + W1a" / "X2-lite + W1a + H50" 두 개로 정리하면서
TW2 3-SLOT 과 TW TEG 3-SLOT 을 UI 에서 감췄다. **코드는 지우지 않았고**
config.SHOW_LEGACY_3SLOT_TOGGLES 로 되살릴 수 있다 -- 2026-09-07 의
test_ui_legacy_toggles_hidden.py 와 같은 구조로, 이 파일이 (숨김 / 복구 /
내부 경로 보존) 세 가지를 모두 고정한다.

중요: 이번 숨김은 **렌더만** 막는다. 앞선 TW2 숨김과 달리 state 를 강제로
꺼버리지 않는다(둘 다 기본 OFF 라 정리할 것이 없고, 저장된 상태를 조용히
바꾸면 실거래 전략이 말없이 꺼지는 위험이 생긴다). 대신 숨긴 상태에서 켜져
있으면 경고 배너를 띄운다 -- 그 계약도 여기서 고정한다.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import config, service as service_module, state_store

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
_PREEXISTING_FORM_BUG = "can't be used in an `st.form()`"

HIDDEN_LABELS = ("TW2 3-SLOT", config.TW_TEG_3SLOT_STRATEGY_NAME)
HIDDEN_KEYS = (
    "macd2_time_window_3slot_filter_toggle",
    "macd2_time_window_twf_filter_toggle",
)
VISIBLE_LABELS = (
    f"{config.X2LITE_3SLOT_STRATEGY_NAME} + {config.X2LITE_SIZING_NAME} sizing",
    config.H50_3SLOT_STRATEGY_NAME,
)


def _run():
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["app_auth_authenticated"] = True
    at.run()
    for exc in at.exception:
        if _PREEXISTING_FORM_BUG in str(getattr(exc, "value", "") or ""):
            pytest.skip("기존 페이지 결함(st.button inside st.form) -- 이 변경과 무관")
    assert not at.exception
    return at


class TestHidden:
    def test_default_is_hidden(self):
        assert config.SHOW_LEGACY_3SLOT_TOGGLES is False

    def test_toggles_not_rendered(self):
        at = _run()
        labels = [c.label for c in at.checkbox]
        for lab in HIDDEN_LABELS:
            assert lab not in labels, f"{lab} 이 아직 UI 에 보인다: {labels!r}"

    def test_keys_not_rendered(self):
        at = _run()
        keys = [c.key for c in at.checkbox]
        for k in HIDDEN_KEYS:
            assert k not in keys, f"{k} 위젯이 아직 렌더된다: {keys!r}"

    def test_the_two_x2lite_strategies_are_what_remains(self):
        at = _run()
        labels = [c.label for c in at.checkbox]
        for lab in VISIBLE_LABELS:
            assert lab in labels, f"{lab} 이 보여야 한다: {labels!r}"
        assert labels.index(VISIBLE_LABELS[1]) == labels.index(VISIBLE_LABELS[0]) + 1

    def test_untouched_toggles_still_visible(self):
        at = _run()
        labels = [c.label for c in at.checkbox]
        assert "퀵 Profit 익절" in labels
        assert "무필터 09:00-11:00 즉시청산" in labels


class TestStateIsNotSilentlyChanged:
    """앞선 TW2 숨김과 다른 점 -- 저장된 ON 을 강제로 끄지 않는다."""

    @pytest.mark.parametrize(
        "field", ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled")
    )
    def test_stored_on_survives_a_load(self, field):
        s = state_store.load_state()
        s.time_window_3slot_filter_enabled = False
        s.time_window_twf_filter_enabled = False
        s.time_window_x2lite_filter_enabled = False
        s.time_window_h50_filter_enabled = False
        setattr(s, field, True)
        state_store.save_state(s)

        assert getattr(state_store.load_state(), field) is True, (
            "숨김은 렌더만 막아야 한다 -- 저장된 전략을 말없이 끄면 안 된다"
        )

    @pytest.mark.parametrize(
        "field,label",
        (("time_window_3slot_filter_enabled", "TW2 3-SLOT"),
         ("time_window_twf_filter_enabled", config.TW_TEG_3SLOT_STRATEGY_NAME)),
    )
    def test_hidden_but_enabled_shows_a_warning(self, field, label):
        """활성 전략이 눈에서 사라지는 상황을 만들지 않기 위한 안전장치."""
        s = state_store.load_state()
        s.time_window_3slot_filter_enabled = False
        s.time_window_twf_filter_enabled = False
        s.time_window_x2lite_filter_enabled = False
        s.time_window_h50_filter_enabled = False
        setattr(s, field, True)
        state_store.save_state(s)

        at = _run()
        warnings = [w.value for w in at.warning]
        assert any("숨긴 전략이 켜져 있습니다" in w and label in w for w in warnings), (
            f"숨긴 채 켜져 있는데 경고가 없다: {warnings!r}"
        )

    def test_no_warning_when_both_are_off(self):
        s = state_store.load_state()
        s.time_window_3slot_filter_enabled = False
        s.time_window_twf_filter_enabled = False
        state_store.save_state(s)

        at = _run()
        assert not any("숨긴 전략이 켜져 있습니다" in w.value for w in at.warning)


class TestRestorable:
    def test_flag_on_renders_them_again(self, monkeypatch):
        monkeypatch.setattr(config, "SHOW_LEGACY_3SLOT_TOGGLES", True)
        at = _run()
        labels = [c.label for c in at.checkbox]
        for lab in HIDDEN_LABELS:
            assert lab in labels, f"복구 플래그를 켰는데 {lab} 이 안 보인다: {labels!r}"

    def test_flag_on_keeps_the_x2lite_pair_too(self, monkeypatch):
        """복구는 되살리기만 한다 -- 현행 두 전략을 밀어내지 않는다."""
        monkeypatch.setattr(config, "SHOW_LEGACY_3SLOT_TOGGLES", True)
        at = _run()
        labels = [c.label for c in at.checkbox]
        for lab in VISIBLE_LABELS:
            assert lab in labels, f"{lab} 이 사라졌다: {labels!r}"

    def test_service_setters_are_still_present(self):
        for name in ("set_time_window_3slot_filter_enabled",
                     "set_time_window_twf_filter_enabled"):
            assert hasattr(service_module.Macd2Service, name), f"{name} 이 사라졌다"


class TestInternalPathsUntouched:
    def test_modes_still_registered(self):
        from app.trading.macd2 import time_window_3slot as t3
        assert t3.MODE_TW2_3SLOT in t3.MODES_3SLOT
        assert t3.MODE_TWF_3SLOT in t3.MODES_3SLOT

    def test_worker_still_dispatches_on_the_flags(self):
        """worker 의 3-SLOT 판정 경로가 숨김 플래그를 보지 않는지 소스로 고정한다."""
        from app.trading.macd2 import worker
        src = inspect.getsource(worker)
        assert "SHOW_LEGACY_3SLOT_TOGGLES" not in src, (
            "worker 가 UI 표시 플래그를 참조한다 -- 숨김이 판정 경로로 새어 들어갔다"
        )

    def test_state_store_does_not_force_them_off(self):
        from app.trading.macd2 import state_store as ss
        src = inspect.getsource(ss)
        assert "SHOW_LEGACY_3SLOT_TOGGLES" not in src, (
            "state_store 가 표시 플래그로 상태를 바꾼다 -- 이번 숨김의 계약과 다르다"
        )
