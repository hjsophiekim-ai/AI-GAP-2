"""X1 CONTEXT UI 토글 — 2026-09-23.

SMART 와 같은 관례를 따른다: **N1 + C1 이 둘 다 켜져 있어야** 켤 수 있고,
기본 OFF 이며, 상태는 state_store 에 영속된다. SHADOW 는 본토글과 **독립**이라
주문을 전혀 바꾸지 않고 관찰만 켤 수 있어야 한다.

주의: 이 디렉토리에서는 ``monkeypatch.undo()`` 를 쓰지 않는다 — 값 교체가
필요하면 ``_Knobs`` holder 패턴을 쓴다.
"""
from __future__ import annotations

import io

import pytest

from app.trading.macd2 import (
    config,
    service as sv,
    state_store,
    x1_context as X,
)
from app.trading.macd2.models import RuntimeState


class _Knobs:
    """monkeypatch.undo() 를 쓰지 않기 위한 holder."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def x1(self, value: bool):
        self.mp.setattr(config, "X1_ENABLED", bool(value), raising=False)


def _state(*, n1=True, c1=True, x1=False, shadow=False) -> RuntimeState:
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = 10_000_000.0
    for f in ("time_window_2_filter_enabled", "time_window_teg_filter_enabled",
              "time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
              "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled"):
        setattr(s, f, False)
    s.time_window_n1_filter_enabled = bool(n1)
    s.c1_peak_protection_enabled = bool(c1)
    s.x1_context_enabled = bool(x1)
    s.x1_shadow_mode_enabled = bool(shadow)
    state_store.save_state(s)
    return s


# ── 기본값 ─────────────────────────────────────────────────────────────────
def test_default_is_off():
    assert RuntimeState().x1_context_enabled is False
    assert RuntimeState().x1_shadow_mode_enabled is False
    assert config.X1_ENABLED is False
    assert config.X1_SHADOW_MODE is False


# ── N1 + C1 가드 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("n1,c1", [(True, False), (False, True), (False, False)])
def test_toggle_on_is_refused_without_n1_and_c1(n1, c1):
    _state(n1=n1, c1=c1)
    res = sv.Macd2Service().set_x1_context_enabled(True)
    assert res["ok"] is False
    assert res["reason"] == "X1_REQUIRES_N1_AND_C1"
    assert state_store.load_state().x1_context_enabled is False


def test_toggle_on_succeeds_with_n1_and_c1():
    _state(n1=True, c1=True)
    res = sv.Macd2Service().set_x1_context_enabled(True, changed_by="test")
    assert res["ok"] is True
    after = state_store.load_state()
    assert after.x1_context_enabled is True
    assert after.x1_context_enabled_by == "test"
    assert after.x1_context_enabled_at
    assert res["version"] == config.X1_FILTER_VERSION


def test_toggle_off_always_allowed():
    _state(n1=True, c1=True, x1=True)
    res = sv.Macd2Service().set_x1_context_enabled(False)
    assert res["ok"] is True
    assert state_store.load_state().x1_context_enabled is False


def test_runtime_gate_also_requires_n1_and_c1(monkeypatch):
    """state 토글과 별개로 런타임 헬퍼도 같은 조건을 강제한다(이중 방어)."""
    _Knobs(monkeypatch).x1(True)
    assert X.x1_active(n1_enabled=True, c1_enabled=True) is True
    assert X.x1_active(n1_enabled=True, c1_enabled=False) is False
    assert X.x1_active(n1_enabled=False, c1_enabled=True) is False


# ── SHADOW 는 본토글과 독립 ────────────────────────────────────────────────
def test_shadow_toggles_independently():
    _state(n1=True, c1=True)
    res = sv.Macd2Service().set_x1_context_enabled(True, shadow_only=True)
    assert res["ok"] is True
    after = state_store.load_state()
    assert after.x1_shadow_mode_enabled is True
    assert after.x1_context_enabled is False       # 본토글은 그대로 OFF


def test_shadow_on_is_also_gated_by_n1_c1():
    _state(n1=True, c1=False)
    res = sv.Macd2Service().set_x1_context_enabled(True, shadow_only=True)
    assert res["ok"] is False
    assert state_store.load_state().x1_shadow_mode_enabled is False


# ── state 영속 ─────────────────────────────────────────────────────────────
def test_state_round_trip_persists_x1_fields():
    s = _state(n1=True, c1=True, x1=True, shadow=True)
    s.x1_context_enabled_by = "ui"
    s.x1_context_enabled_at = "2026-09-23T10:00:00+09:00"
    state_store.save_state(s)
    back = state_store.load_state()
    assert back.x1_context_enabled is True
    assert back.x1_shadow_mode_enabled is True
    assert back.x1_context_enabled_by == "ui"
    assert back.x1_context_enabled_at == "2026-09-23T10:00:00+09:00"


def test_old_state_without_x1_keys_loads_as_off():
    """X1 이전에 저장된 state 를 읽어도 안전하게 OFF 로 떨어진다."""
    s = _state(n1=True, c1=True)
    raw = state_store.serialize(s)
    for key in ("x1_context_enabled", "x1_context_enabled_at",
                "x1_context_enabled_by", "x1_shadow_mode_enabled"):
        raw.pop(key, None)
    restored = state_store.deserialize(raw)
    assert restored.x1_context_enabled is False
    assert restored.x1_shadow_mode_enabled is False


# ── UI 화면 계약 ───────────────────────────────────────────────────────────
def _ui_src() -> str:
    return io.open("app/ui/pages/11_MACD_자동매매2.py", encoding="utf-8").read()


def test_ui_renders_the_toggle_and_guards_it():
    src = _ui_src()
    assert 'key="macd2_x1_context_toggle"' in src
    assert 'key="macd2_x1_shadow_toggle"' in src
    assert "service.set_x1_context_enabled(" in src
    # N1 + C1 둘 다 있어야 활성
    assert "_x1_ready = _x1_n1 and _x1_c1" in src
    assert "disabled=not _x1_ready" in src
    # 조건 미충족이면 세션 상태까지 꺼 둔다(체크가 남아 보이지 않게)
    assert 'st.session_state["macd2_x1_context_toggle"] = False' in src
    # 최근 판정 표시 4종 (사양 §8)
    for label in ("Premarket trend", "Flip count", "Context score", "Reason"):
        assert label in src


def test_ui_lists_x1_after_n1_c1_smart():
    """토글 순서: N1 -> C1 -> SMART -> X1 CONTEXT (사양 §8)."""
    src = _ui_src()
    i_c1 = src.index('key="macd2_c1_peak_protection_toggle"')
    i_sm = src.index('key="macd2_smart_sizing_toggle"')
    i_x1 = src.index('key="macd2_x1_context_toggle"')
    assert i_c1 < i_sm < i_x1
