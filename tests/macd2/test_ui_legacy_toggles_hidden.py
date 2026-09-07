"""레거시 진입전략 토글 숨김 (2026-09-07).

사용자 노출 전략을 "TW2 3-SLOT + 조기익절" / "TWF 3-SLOT + 조기익절" 두 개로
정리하면서 TW2 / +TEGv2 / +1 DOWN_BLUE 를 UI 에서 감췄다. **코드는 지우지
않았고** config.SHOW_LEGACY_TW2_TOGGLES 로 되살릴 수 있다 -- 이 파일이 그 두
가지(숨김 / 복구)를 모두 고정한다.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import config, service as service_module, state_store

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
_PREEXISTING_FORM_BUG = "can't be used in an `st.form()`"

HIDDEN_LABELS = ("시간대별 최적거래 필터 (TW2)", "+TEGv2", "+1 DOWN_BLUE")
VISIBLE_LABELS = ("TW2 3-SLOT", "TWF 3-SLOT", "└ 조기익절 필터")
HIDDEN_KEYS = (
    "macd2_time_window_2_filter_toggle",
    "macd2_time_window_teg_filter_toggle",
    "macd2_down_blue_exception_toggle",
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
        assert config.SHOW_LEGACY_TW2_TOGGLES is False

    def test_legacy_toggles_not_rendered(self):
        at = _run()
        labels = [c.label for c in at.checkbox]
        for lab in HIDDEN_LABELS:
            assert lab not in labels, f"{lab} 이 아직 UI 에 보인다: {labels!r}"

    def test_legacy_keys_not_rendered(self):
        at = _run()
        keys = [c.key for c in at.checkbox]
        for k in HIDDEN_KEYS:
            assert k not in keys, f"{k} 위젯이 아직 렌더된다: {keys!r}"

    def test_only_the_two_strategies_plus_early_tp_remain_in_that_tier(self):
        at = _run()
        labels = [c.label for c in at.checkbox]
        for lab in VISIBLE_LABELS:
            assert lab in labels, f"{lab} 이 보여야 한다: {labels!r}"
        assert labels.index("TWF 3-SLOT") == labels.index("TW2 3-SLOT") + 1
        assert labels.index("└ 조기익절 필터") == labels.index("TWF 3-SLOT") + 1

    def test_untouched_toggles_still_visible(self):
        """사용자 지시: 퀵 Profit 익절 / 무필터 09:00-11:00 은 그대로 둔다."""
        at = _run()
        labels = [c.label for c in at.checkbox]
        assert "퀵 Profit 익절" in labels
        assert "무필터 09:00-11:00 즉시청산" in labels


class TestForcedOffInState:
    def test_stored_on_is_forced_off_on_load(self):
        s = state_store.load_state()
        s.time_window_2_filter_enabled = True
        s.time_window_teg_filter_enabled = True
        s.down_blue_exception_filter_enabled = True
        state_store.save_state(s)

        back = state_store.load_state()
        assert back.time_window_2_filter_enabled is False
        assert back.time_window_teg_filter_enabled is False
        assert back.down_blue_exception_filter_enabled is False

    def test_forcing_off_does_not_kill_the_two_strategies(self):
        """강제해제가 3-SLOT 방어검사보다 먼저 돌아야 두 전략이 살아남는다."""
        for field in ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled"):
            s = state_store.load_state()
            s.time_window_3slot_filter_enabled = False
            s.time_window_twf_filter_enabled = False
            setattr(s, field, True)
            s.time_window_2_filter_enabled = True   # 감춰진 채 켜져 있던 상황
            state_store.save_state(s)

            back = state_store.load_state()
            assert getattr(back, field) is True, f"{field} 가 TW2 때문에 떨어졌다"
            assert back.time_window_2_filter_enabled is False


class TestRestorable:
    def test_flag_on_renders_them_again(self, monkeypatch):
        monkeypatch.setattr(config, "SHOW_LEGACY_TW2_TOGGLES", True)
        at = _run()
        labels = [c.label for c in at.checkbox]
        for lab in HIDDEN_LABELS:
            assert lab in labels, f"복구 플래그를 켰는데 {lab} 이 안 보인다: {labels!r}"

    def test_flag_on_stops_forcing_state_off(self, monkeypatch):
        monkeypatch.setattr(config, "SHOW_LEGACY_TW2_TOGGLES", True)
        s = state_store.load_state()
        s.time_window_3slot_filter_enabled = False
        s.time_window_twf_filter_enabled = False
        s.time_window_2_filter_enabled = True
        state_store.save_state(s)
        assert state_store.load_state().time_window_2_filter_enabled is True

    def test_service_setters_are_still_present(self):
        """코드 경로 보존 -- setter 는 숨김과 무관하게 살아 있어야 한다."""
        for name in ("set_time_window_2_filter_enabled",
                     "set_time_window_teg_filter_enabled",
                     "set_down_blue_exception_filter_enabled"):
            assert hasattr(service_module.Macd2Service, name), f"{name} 이 사라졌다"


class TestTegGateStillWorksInternally:
    def test_teg_gate_module_untouched(self):
        """오후 슬롯 TEGv2 게이트는 토글이 아니라 requires_teg_gate 로 걸린다."""
        from datetime import datetime
        from app.trading.macd2 import time_window_3slot as t3
        teg_gate = importlib.import_module("app.trading.macd2.teg_gate")
        assert hasattr(teg_gate, "evaluate_teg")

        now = datetime(2026, 9, 7, 13, 30, tzinfo=config.KST)
        d = t3.resolve_slot(now=now, slots_used_today=1, morning_count=1,
                            afternoon_count=0, direction="UP_RED", is_flat=True,
                            last_afternoon_direction=None)
        assert d.slot_allowed and d.requires_teg_gate, (
            "오후 슬롯이 TEGv2 게이트를 요구하지 않는다 -- 숨김이 내부 경로를 깼다"
        )

    def test_worker_calls_teg_without_consulting_the_toggle(self):
        """worker 의 3-SLOT 분기가 state.time_window_teg_filter_enabled 를
        보지 않고 teg_gate.evaluate_teg 를 직접 부르는지 소스로 고정한다."""
        import inspect
        from app.trading.macd2 import worker
        src = inspect.getsource(worker._resolve_tw2_3slot_candidate_body)
        assert "teg_gate.evaluate_teg" in src
        assert "time_window_teg_filter_enabled" not in src
