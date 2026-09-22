"""SMART 사이징 UI 토글 — 2026-09-22 (P2 토글을 대체).

P2 슬롯 배분과 toxic 감액을 **하나의 SMART 정책**으로 합친 단일 토글.
토글은 **사이징 전용**이고, N1 + C1 이 둘 다 켜져 있을 때만 유효하다.

우선순위: state 토글 **또는** env 중 하나라도 SMART(구 "P2" alias 포함)면 SMART.
env 는 비상 강제용으로 남긴다 — UI 를 못 여는 상황에서도 켤 수 있어야 하고,
반대로 env 를 못 건드리는 상황에서도 UI 로 켤 수 있어야 하기 때문이다.
"""
from __future__ import annotations

import io

import pytest

from app.trading.macd2 import (
    config,
    peak_protection,
    position_sizing as PS,
    service as sv,
    state_store,
    time_window_3slot as tw3,
)
from app.trading.macd2.models import RuntimeState

MORNING, AFTERNOON = tw3.SESSION_MORNING, tw3.SESSION_AFTERNOON


class _Knobs:
    """monkeypatch.undo() 를 쓰지 않기 위한 holder."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def env(self, value: str):
        self.mp.setattr(config, "MACD2_SIZING_MODE", value, raising=False)


def _state(*, n1=True, c1=True, p2=False, smart=None) -> RuntimeState:
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = 10_000_000.0
    for f in ("time_window_2_filter_enabled", "time_window_teg_filter_enabled",
              "time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
              "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled"):
        setattr(s, f, False)
    s.time_window_n1_filter_enabled = bool(n1)
    s.c1_peak_protection_enabled = bool(c1)
    s.smart_sizing_enabled = bool(p2 if smart is None else smart)
    s.smart_sizing_enabled = bool(s.smart_sizing_enabled)
    state_store.save_state(s)
    return s


# ════════════════════════════════════════════════════════════════════════════
# 기본값 — 토글은 꺼져 있고, 켜기 전에는 아무것도 바뀌지 않는다
# ════════════════════════════════════════════════════════════════════════════
def test_default_is_off():
    assert RuntimeState().smart_sizing_enabled is False
    s = _state()
    assert PS.sizing_mode(s) == config.SIZING_MODE_BASE
    assert PS.p2_active(s) is False
    assert PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING).applied == 1.0


def test_toggle_on_activates_p2_multipliers():
    s = _state(p2=True)
    assert PS.sizing_mode(s) == config.SIZING_MODE_SMART
    assert PS.p2_active(s) is True
    ev = lambda slot, sess: PS.evaluate(s, entry_chop=False, slot_number=slot,
                                        session=sess).applied
    assert ev(1, MORNING) == pytest.approx(config.P2_SIZING_SLOT12_MULT)
    assert ev(2, AFTERNOON) == pytest.approx(config.P2_SIZING_SLOT12_MULT)
    assert ev(3, MORNING) == pytest.approx(config.P2_SIZING_MORNING_SLOT3_MULT)
    assert ev(3, AFTERNOON) == pytest.approx(config.P2_SIZING_AFTERNOON_SLOT3_MULT)


# ════════════════════════════════════════════════════════════════════════════
# state 와 env 의 우선순위
# ════════════════════════════════════════════════════════════════════════════
def test_env_alone_still_works(monkeypatch):
    """UI 를 못 여는 상황의 비상 경로 — env 만으로도 켜진다."""
    _Knobs(monkeypatch).env(config.SIZING_MODE_SMART)
    s = _state(p2=False)
    assert PS.forced_by_env() is True
    assert PS.sizing_mode(s) == config.SIZING_MODE_SMART
    assert PS.p2_active(s) is True


def test_toggle_alone_works_without_env():
    s = _state(p2=True)
    assert PS.forced_by_env() is False
    assert PS.sizing_mode(s) == config.SIZING_MODE_SMART


def test_both_off_means_base(monkeypatch):
    _Knobs(monkeypatch).env(config.SIZING_MODE_BASE)
    assert PS.sizing_mode(_state(p2=False)) == config.SIZING_MODE_BASE


def test_sizing_mode_without_state_falls_back_to_env(monkeypatch):
    """state 를 안 넘기는 기존 호출부는 env 만 본다 (하위호환)."""
    _Knobs(monkeypatch).env(config.SIZING_MODE_BASE)
    assert PS.sizing_mode() == config.SIZING_MODE_BASE
    _Knobs(monkeypatch).env(config.SIZING_MODE_SMART)
    assert PS.sizing_mode() == config.SIZING_MODE_SMART


# ════════════════════════════════════════════════════════════════════════════
# N1 + C1 가드
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("n1,c1", [(True, False), (False, True), (False, False)])
def test_toggle_on_is_refused_without_n1_and_c1(n1, c1):
    _state(n1=n1, c1=c1)
    res = sv.Macd2Service().set_smart_sizing_enabled(True)
    assert res["ok"] is False
    assert res["reason"] == "SMART_REQUIRES_N1_AND_C1"
    assert state_store.load_state().p2_sizing_enabled is False


def test_toggle_on_succeeds_with_n1_and_c1():
    _state(n1=True, c1=True)
    res = sv.Macd2Service().set_smart_sizing_enabled(True, changed_by="test")
    assert res["ok"] is True
    after = state_store.load_state()
    assert after.p2_sizing_enabled is True
    assert after.p2_sizing_enabled_by == "test"
    assert after.p2_sizing_enabled_at
    assert res["slot12_mult"] == pytest.approx(1.05)
    assert res["morning_slot3_mult"] == pytest.approx(0.25)
    assert res["afternoon_slot3_mult"] == pytest.approx(1.00)
    assert res["daily_capital"] == pytest.approx(30_000_000.0)


def test_toggle_off_always_allowed():
    _state(n1=True, c1=True, p2=True)
    res = sv.Macd2Service().set_smart_sizing_enabled(False)
    assert res["ok"] is True
    assert state_store.load_state().p2_sizing_enabled is False


def test_p2_inert_when_c1_turned_off_after_enabling():
    """토글이 켜진 채로 C1 을 끄면 P2 는 즉시 무효가 된다 (상태는 남아도 적용 안 됨)."""
    s = _state(n1=True, c1=True, p2=True)
    assert PS.p2_active(s) is True
    s.c1_peak_protection_enabled = False
    assert PS.p2_active(s) is False
    assert PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING).applied == 1.0


def test_p2_inert_for_other_strategies():
    s = _state(n1=True, c1=True, p2=True)
    s.time_window_n1_filter_enabled = False
    s.time_window_x2lite_filter_enabled = True
    assert PS.p2_active(s) is False
    assert PS.evaluate(s, entry_chop=False, slot_number=1, session=MORNING).applied == 1.0


# ════════════════════════════════════════════════════════════════════════════
# 영속화
# ════════════════════════════════════════════════════════════════════════════
def test_toggle_survives_state_roundtrip():
    s = _state(p2=True)
    s.smart_sizing_enabled_by = "ui"
    state_store.save_state(s)
    after = state_store.load_state()
    assert after.p2_sizing_enabled is True
    assert after.p2_sizing_enabled_by == "ui"
    assert PS.sizing_mode(after) == config.SIZING_MODE_SMART


def test_missing_field_in_old_state_file_defaults_to_off():
    """이 필드가 없던 시절의 state 파일도 그대로 로드되고 OFF 로 읽힌다."""
    raw = state_store.serialize(state_store.default_state())
    raw.pop("smart_sizing_enabled", None)
    raw.pop("p2_sizing_enabled_at", None)
    raw.pop("p2_sizing_enabled_by", None)
    restored = state_store.deserialize(raw)
    assert restored.p2_sizing_enabled is False
    assert PS.sizing_mode(restored) == config.SIZING_MODE_BASE


# ════════════════════════════════════════════════════════════════════════════
# UI 화면 계약
# ════════════════════════════════════════════════════════════════════════════
def _ui_src() -> str:
    return io.open("app/ui/pages/11_MACD_자동매매2.py", encoding="utf-8").read()


def test_ui_renders_the_toggle_and_guards_it():
    src = _ui_src()
    assert 'key="macd2_smart_sizing_toggle"' in src
    assert "service.set_smart_sizing_enabled(" in src
    # N1 + C1 둘 다 있어야 활성
    assert "_sm_ready = _sm_n1 and _sm_c1" in src
    assert "disabled=not _sm_ready" in src
    # 조건 미충족이면 세션 상태까지 꺼 둔다(체크가 남아 보이지 않게)
    assert 'st.session_state["macd2_smart_sizing_toggle"] = False' in src
    # env 강제 ON 은 화면에 알린다
    assert "MACD2_SIZING_MODE" in src


def test_ui_help_states_the_grade_honestly():
    """등급을 PROMISING 으로 적고 ADOPT 라고 하지 않는다."""
    src = _ui_src()
    i = src.index('key="macd2_smart_sizing_toggle"')
    seg = src[max(0, i - 3000):i + 3000]
    assert "PROMISING" in seg
    assert "toxic" in seg.lower()
    assert "84.5%" in seg          # 최근 30일 부트스트랩 (기준 95% 미달)
    assert "기본 OFF" in seg
