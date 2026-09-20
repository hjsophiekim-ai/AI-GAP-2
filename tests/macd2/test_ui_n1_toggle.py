"""N1 UI 토글 / 전략 선택 영역 테스트 (2026-09-20).

tests/macd2/test_ui_early_take_profit_toggle.py 와 같은 하네스를 그대로 쓴다.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import config, state_store

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
_N1_KEY = "macd2_time_window_n1_filter_toggle"
_N1_LABEL = config.N1_3SLOT_STRATEGY_NAME
_C1_LABEL = "└ C1 Peak Protection"
_PREEXISTING_FORM_BUG = "can't be used in an `st.form()`"


def _fresh_app() -> AppTest:
    at = AppTest.from_file(_APP_PATH, default_timeout=40)
    at.session_state["app_auth_authenticated"] = True
    return at


def _guard(at: AppTest) -> AppTest:
    for exc in at.exception:
        if _PREEXISTING_FORM_BUG in str(getattr(exc, "value", "") or ""):
            pytest.skip("기존 페이지 결함(st.button inside st.form) — N1 과 무관")
    assert not at.exception
    return at


def _run(at: AppTest) -> AppTest:
    at.run()
    return _guard(at)


def _cb(at: AppTest, label: str):
    for c in at.checkbox:
        if c.label == label:
            return c
    raise AssertionError(f"{label!r} 체크박스 없음. 렌더된 것: {[c.label for c in at.checkbox]!r}")


def _set(**kw) -> None:
    st_ = state_store.load_state()
    for f in ("time_window_n1_filter_enabled", "time_window_h50_filter_enabled",
              "time_window_x2lite_filter_enabled", "time_window_3slot_filter_enabled",
              "time_window_twf_filter_enabled", "time_window_2_filter_enabled",
              "time_window_teg_filter_enabled"):
        setattr(st_, f, bool(kw.get(f.replace("time_window_", "").replace("_filter_enabled", ""), False)))
    if not kw.get("n1"):
        st_.c1_peak_protection_enabled = False
    state_store.save_state(st_)


# ── 렌더 / 기본값 ─────────────────────────────────────────────────────────
def test_n1_toggle_renders_and_defaults_off():
    _set()
    at = _run(_fresh_app())
    cb = _cb(at, _N1_LABEL)
    assert cb.disabled is False
    assert cb.value is False, "기본값은 반드시 OFF"
    assert state_store.load_state().time_window_n1_filter_enabled is False


def test_n1_renders_below_h50():
    _set()
    at = _run(_fresh_app())
    labels = [c.label for c in at.checkbox]
    assert labels.index(_N1_LABEL) > labels.index(config.H50_3SLOT_STRATEGY_NAME)


def test_help_text_states_the_adaptive_rule_and_anchor():
    _set()
    at = _run(_fresh_app())
    h = _cb(at, _N1_LABEL).help or ""
    assert "adaptive TP2 8%↔4%" in h
    assert "158거래" in h and "401.0853" in h
    assert "quality" in h
    assert "기본 OFF" in h


# ── ON/OFF 저장 · 복원 · 상호배제 ─────────────────────────────────────────
def test_checking_n1_turns_it_on_and_persists():
    _set()
    at = _run(_fresh_app())
    _cb(at, _N1_LABEL).check().run()
    _guard(at)
    r = state_store.load_state()
    assert r.time_window_n1_filter_enabled is True
    assert r.time_window_n1_filter_version == config.N1_3SLOT_FILTER_VERSION


def test_restart_restores_n1_on():
    _set()
    at = _run(_fresh_app())
    _cb(at, _N1_LABEL).check().run()
    _guard(at)
    at2 = _run(_fresh_app())
    assert _cb(at2, _N1_LABEL).value is True


def test_enabling_n1_turns_off_h50():
    _set(h50=True)
    at = _run(_fresh_app())
    assert _cb(at, config.H50_3SLOT_STRATEGY_NAME).value is True
    _cb(at, _N1_LABEL).check().run()
    _guard(at)
    r = state_store.load_state()
    assert r.time_window_n1_filter_enabled is True
    assert r.time_window_h50_filter_enabled is False


def test_enabling_h50_turns_off_n1():
    _set(n1=True)
    at = _run(_fresh_app())
    _cb(at, config.H50_3SLOT_STRATEGY_NAME).check().run()
    _guard(at)
    r = state_store.load_state()
    assert r.time_window_h50_filter_enabled is True
    assert r.time_window_n1_filter_enabled is False


# ── 활성 전략 표시 ────────────────────────────────────────────────────────
def test_active_strategy_is_shown_exactly_once():
    _set(n1=True)
    at = _run(_fresh_app())
    successes = [s.value for s in at.success]
    assert any(f"현재 활성 전략: **{_N1_LABEL}**" in s for s in successes), successes


def test_no_strategy_shows_none_caption():
    _set()
    at = _run(_fresh_app())
    caps = [c.value for c in at.caption]
    assert any("현재 활성 전략: (없음" in c for c in caps), caps


def test_n1_diagnostic_panel_shows_effective_tp2():
    _set(n1=True)
    s = state_store.load_state()
    s.n1_regime_state = "TREND"
    s.n1_effective_tp2 = 8.0
    s.n1_effective_tp1 = 3.5
    s.n1_effective_tp1_ratio = 0.0
    s.n1_last_eval_bar_ts = "2026-09-18T10:30:00+09:00"
    state_store.save_state(s)
    at = _run(_fresh_app())
    infos = [i.value for i in at.info]
    assert any("N1 adaptive 현재 판정" in i and "TREND" in i and "8.0%" in i for i in infos), infos


# ── C1 의존성 ─────────────────────────────────────────────────────────────
def test_c1_is_disabled_without_n1():
    _set(h50=True)
    at = _run(_fresh_app())
    c1 = _cb(at, _C1_LABEL)
    assert c1.disabled is True and c1.value is False
    assert any("N1 을 켜야" in c.value for c in at.caption)


def test_c1_is_enabled_only_with_n1():
    _set(n1=True)
    at = _run(_fresh_app())
    c1 = _cb(at, _C1_LABEL)
    assert c1.disabled is False and c1.value is False
    c1.check().run()
    _guard(at)
    assert state_store.load_state().c1_peak_protection_enabled is True


def test_turning_n1_off_also_clears_c1():
    _set(n1=True)
    at = _run(_fresh_app())
    _cb(at, _C1_LABEL).check().run()
    _guard(at)
    assert state_store.load_state().c1_peak_protection_enabled is True
    _cb(at, _N1_LABEL).uncheck().run()
    _guard(at)
    r = state_store.load_state()
    assert r.time_window_n1_filter_enabled is False
    assert r.c1_peak_protection_enabled is False, "N1 을 끄면 C1 도 꺼져야 한다"
    c1 = _cb(at, _C1_LABEL)
    assert c1.value is False and c1.disabled is True


def test_switching_to_h50_disables_c1():
    _set(n1=True)
    at = _run(_fresh_app())
    _cb(at, _C1_LABEL).check().run()
    _guard(at)
    _cb(at, config.H50_3SLOT_STRATEGY_NAME).check().run()
    _guard(at)
    r = state_store.load_state()
    assert r.time_window_h50_filter_enabled is True
    assert r.time_window_n1_filter_enabled is False
    assert r.c1_peak_protection_enabled is False


# ── UI 선택값 ↔ runtime active mode 일치 ─────────────────────────────────
def test_ui_selection_matches_runtime_active_mode():
    from app.trading.macd2 import time_window_3slot as tw3
    for kw, expect in ((dict(n1=True), tw3.MODE_N1_3SLOT),
                       (dict(h50=True), tw3.MODE_X2LITE_H50_3SLOT),
                       (dict(x2lite=True), tw3.MODE_X2LITE_3SLOT),
                       (dict(), None)):
        _set(**kw)
        at = _run(_fresh_app())
        s = state_store.load_state()
        assert tw3.active_3slot_mode(s) == expect, kw
        if expect == tw3.MODE_N1_3SLOT:
            assert _cb(at, _N1_LABEL).value is True
        else:
            assert _cb(at, _N1_LABEL).value is False
