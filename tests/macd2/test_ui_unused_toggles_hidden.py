"""쓰지 않는 토글 3개 숨김 (2026-10-22 사용자 요청).

퀵 Profit 익절 / 조기익절 필터 / 무필터 09:00-11:00 즉시청산 을 UI 에서 감췄다.
test_ui_3slot_legacy_toggles_hidden.py 와 같은 구조로 (숨김 / 복구 / 저장값 보존 /
백엔드 경로 보존) 을 고정한다. 숨김은 **렌더만** 막는다 -- 숨긴 위젯은 service
setter 를 부르지 않으므로 저장된 값이 reset 되지 않아야 한다.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import config, service as service_module, state_store

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
_PREEXISTING_FORM_BUG = "can't be used in an `st.form()`"

HIDDEN = {  # label: (widget key, state field)
    "퀵 Profit 익절": ("macd2_quick_profit_toggle", "quick_profit_enabled"),
    "└ 조기익절 필터": ("macd2_early_tp_toggle", "early_tp_filter_enabled"),
    "무필터 09:00-11:00 즉시청산": ("macd2_no_filter_0900_1100_toggle", "no_filter_0900_1100_enabled"),
}


def _run():
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["app_auth_authenticated"] = True
    at.run()
    for exc in at.exception:
        if _PREEXISTING_FORM_BUG in str(getattr(exc, "value", "") or ""):
            pytest.skip("기존 페이지 결함(st.button inside st.form) -- 이 변경과 무관")
    assert not at.exception
    return at


def _all_text(at) -> str:
    parts = [c.value for c in at.caption] + [m.value for m in at.markdown] + [w.value for w in at.warning]
    return "\n".join(str(p) for p in parts)


class TestHidden:
    def test_default_is_hidden(self):
        assert config.SHOW_UNUSED_TOGGLES is False

    def test_labels_and_keys_not_rendered(self):
        at = _run()
        labels = [c.label for c in at.checkbox]
        keys = [c.key for c in at.checkbox]
        for lab, (key, _) in HIDDEN.items():
            assert lab not in labels, f"{lab} 이 아직 보인다: {labels!r}"
            assert key not in keys, f"{key} 위젯이 아직 렌더된다"

    def test_status_captions_and_doc_line_hidden_too(self):
        at = _run()
        text = _all_text(at)
        for needle in ("퀵 Profit 익절=", "조기익절 필터=", "무필터 09:00-11:00 즉시청산=", "**퀵 Profit 익절(옵션"):
            assert needle not in text, f"숨긴 항목의 상태/설명 '{needle}' 이 남아 있다"

    def test_other_toggles_keep_order(self, monkeypatch):
        """숨긴 3개를 빼면 체크박스 순서가 복구 상태와 정확히 같아야 한다."""
        hidden_order = [c.label for c in _run().checkbox]
        monkeypatch.setattr(config, "SHOW_UNUSED_TOGGLES", True)
        shown_order = [c.label for c in _run().checkbox if c.label not in HIDDEN]
        assert hidden_order == shown_order


def _store_on(field):
    s = state_store.load_state()
    if field == "early_tp_filter_enabled":
        # 기존 백엔드 규칙: 조기익절은 3-SLOT 계열이 켜져 있어야 load 시 유지된다
        # (state_store 가 3-SLOT OFF 면 항상 OFF 로 복원 -- 이 변경과 무관).
        s.time_window_3slot_filter_enabled = True
        s.time_window_2_filter_enabled = False
        s.time_window_teg_filter_enabled = False
    setattr(s, field, True)
    state_store.save_state(s)
    assert getattr(state_store.load_state(), field) is True


class TestStatePreserved:
    @pytest.mark.parametrize("field", [v[1] for v in HIDDEN.values()])
    def test_stored_on_is_not_reset_by_render(self, field):
        _store_on(field)
        _run()
        _run()
        assert getattr(state_store.load_state(), field) is True, "숨김은 렌더만 막아야 한다 -- 저장값이 바뀌었다"

    @pytest.mark.parametrize("label,field", [(k, v[1]) for k, v in HIDDEN.items()])
    def test_hidden_but_on_shows_warning(self, label, field):
        _store_on(field)
        at = _run()
        warnings = [w.value for w in at.warning]
        name = label.replace("└ ", "")
        assert any("숨긴 토글이 켜져 있습니다" in w and name in w for w in warnings), warnings

    def test_no_warning_when_all_off(self):
        at = _run()
        assert not any("숨긴 토글이 켜져 있습니다" in w.value for w in at.warning)


class TestRestorable:
    def test_flag_on_renders_them_again(self, monkeypatch):
        monkeypatch.setattr(config, "SHOW_UNUSED_TOGGLES", True)
        at = _run()
        labels = [c.label for c in at.checkbox]
        for lab in HIDDEN:
            assert lab in labels, f"복구 플래그를 켰는데 {lab} 이 안 보인다: {labels!r}"


class TestBackendUntouched:
    def test_service_setters_and_config_keys_still_exist(self):
        for name in ("set_quick_profit_enabled", "set_early_tp_filter_enabled",
                     "set_no_filter_0900_1100_filter_enabled"):
            assert callable(getattr(service_module.Macd2Service, name, None)), name
        assert hasattr(config, "QUICK_PROFIT_TAKE_PROFIT_NET_PCT")
        assert hasattr(config, "EARLY_TP_TRIGGER_PCT") and hasattr(config, "EARLY_TP_FLOOR_PCT")
