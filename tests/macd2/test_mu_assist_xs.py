"""MU Assist XS (2026-10-27) — STRONG 후보 우선 통과, MU Assist 하위 토글.

고정하는 계약
  · 기본 OFF (env 미설정), 저장값 우선 / 저장값 없으면 env, 재시작·모드 전환 후 유지
  · MU Assist 가 OFF 면 XS 저장값이 ON 이어도 효력 없음
  · STRONG = 체결강도 방향일치 ∧ MU ENTRY_CONFIRM. 하나라도 아니거나 판정 불가면 STRONG 아님
  · TEG: 사유가 EMA stack / VWAP / recent_cross / 9분 계열뿐일 때만 통과. gap·spread·tw2 사유가 섞이면 거절 유지
  · TW2 추가 veto: VWAP veto / 최근교차 veto 만 통과 대상
  · UI: MU Assist 토글 바로 아래에 보이고, MU Assist OFF 면 잠김, 바꾸면 저장
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.trading.macd2 import (config, exec_strength, mu_assist, service as service_module, state_store,
                               strategy_mode, teg_gate, worker)
from app.trading.macd2.models import Direction

KST = config.KST
NOW = datetime(2026, 10, 26, 12, 39, 0, tzinfo=KST)
SID = "20261026_123300_UP_RED:TW2_3SLOT_CONFIRM"
XS_LABEL = "MU Assist XS (STRONG 우선 통과)"
MU_LABEL = "MU Assist"

# 10/26 12:39 RED 의 실제 TEG 사유
REASONS_1239 = (teg_gate.COND_MIN_INTERVAL, teg_gate.COND_EMA_STACK, teg_gate.COND_VWAP)


@pytest.fixture(autouse=True)
def _fresh():
    mu_assist.reset_for_tests()
    yield
    mu_assist.reset_for_tests()


def _snaps(root: Path, cttr: float, now: datetime = NOW) -> None:
    d = root / "snapshots" / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    base = now.replace(tzinfo=None) - timedelta(seconds=60)
    rows = ["ts,symbol,price,cttr"] + [f"{(base + timedelta(seconds=i)).isoformat()},660,1,{cttr}" for i in range(60)]
    (d / "000660.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _state(mu: bool = True, xs: bool = True):
    s = state_store.default_state()
    s.mu_assist_enabled = mu
    s.mu_assist_xs_enabled = xs
    return s


@pytest.fixture
def strong(tmp_path, monkeypatch):
    """STRONG 조건 충족: RED + cttr 120 + MU ENTRY_CONFIRM."""
    monkeypatch.setattr(config, "EXEC_STRENGTH_DATA_DIR", str(tmp_path))
    _snaps(tmp_path, 120.0)
    monkeypatch.setattr(mu_assist, "entry_status_for", lambda sid: mu_assist.ENTRY_CONFIRM)
    return tmp_path


# ── 토글 / 저장 ───────────────────────────────────────────────────────────

def test_default_off():
    assert config.MU_ASSIST_XS_ENABLED is False
    assert state_store.default_state().mu_assist_xs_enabled is False
    assert state_store.load_state().mu_assist_xs_enabled is False


def test_saved_value_wins_over_env_and_old_file_follows_env(monkeypatch):
    st = state_store.load_state()
    state_store.save_state(st)
    monkeypatch.setattr(config, "MU_ASSIST_XS_ENABLED", True)
    assert state_store.load_state().mu_assist_xs_enabled is False, "저장값 우선"
    raw = json.loads(state_store.STATE_PATH.read_text(encoding="utf-8"))
    raw.pop("mu_assist_xs_enabled", None)
    state_store.STATE_PATH.write_text(json.dumps(raw), encoding="utf-8")
    assert state_store.load_state().mu_assist_xs_enabled is True, "키 없는 기존 파일은 env 기본값"


def test_service_setter_persists_and_keeps_mu_assist_value():
    svc = service_module.Macd2Service.__new__(service_module.Macd2Service)
    st = state_store.load_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    st.mu_assist_enabled = True
    state_store.save_state(st)
    out = svc.set_mu_assist_xs_enabled(True, changed_by="test")
    assert out["ok"] and out["mu_assist_xs_enabled"] is True and out["previous"] is False
    re = state_store.load_state()
    assert re.mu_assist_xs_enabled is True and re.mu_assist_xs_changed_by == "test" and re.mu_assist_enabled is True
    svc.set_strategy_mode(strategy_mode.MODE_P3, changed_by="test")
    assert state_store.load_state().mu_assist_xs_enabled is True, "모드 전환 후 유지"
    assert svc.set_mu_assist_xs_enabled(False)["mu_assist_xs_enabled"] is False


@pytest.mark.parametrize("mu,xs,expect", [(True, True, True), (True, False, False), (False, True, False), (False, False, False)])
def test_xs_requires_mu_assist(mu, xs, expect):
    assert mu_assist.xs_enabled(_state(mu, xs)) is expect


# ── STRONG 판정 ───────────────────────────────────────────────────────────

def test_strong_when_match_and_mu_confirm(strong):
    cs = worker._mu_xs_strong(_state(), SID, Direction.UP_RED, NOW)
    assert cs is not None and cs["state"] == exec_strength.MATCH


def test_not_strong_when_cttr_mismatch(strong):
    """10/26 14:33 BLUE 형태: cttr 110.9 → BLUE 불일치 → STRONG 아님."""
    _snaps(strong, 110.9)
    assert worker._mu_xs_strong(_state(), SID, Direction.DOWN_BLUE, NOW) is None


@pytest.mark.parametrize("status", [mu_assist.ENTRY_WAIT, mu_assist.ENTRY_BAD, mu_assist.ENTRY_MU_UNAVAILABLE, ""])
def test_not_strong_without_mu_entry_confirm(strong, monkeypatch, status):
    monkeypatch.setattr(mu_assist, "entry_status_for", lambda sid: status)
    assert worker._mu_xs_strong(_state(), SID, Direction.UP_RED, NOW) is None


def test_not_strong_when_cttr_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_STRENGTH_DATA_DIR", str(tmp_path))      # 파일 없음
    monkeypatch.setattr(mu_assist, "entry_status_for", lambda sid: mu_assist.ENTRY_CONFIRM)
    assert worker._mu_xs_strong(_state(), SID, Direction.UP_RED, NOW) is None


def test_not_strong_when_toggles_off(strong):
    assert worker._mu_xs_strong(_state(xs=False), SID, Direction.UP_RED, NOW) is None
    assert worker._mu_xs_strong(_state(mu=False), SID, Direction.UP_RED, NOW) is None


# ── TEG 통과 범위 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("reasons", [REASONS_1239, (teg_gate.COND_EMA_STACK,), (teg_gate.COND_VWAP,),
                                     (teg_gate.COND_RECENT_CROSS,), (teg_gate.COND_EMA_STACK, teg_gate.COND_VWAP)])
def test_teg_override_for_strong(strong, reasons):
    m: dict = {}
    assert worker._mu_xs_teg_override(_state(), SID, Direction.UP_RED, NOW, reasons, m) is True
    assert m["mu_xs_teg_override"] == sorted(reasons)


@pytest.mark.parametrize("extra", [teg_gate.COND_MACD_GAP_EXPANDING, teg_gate.COND_EMA_SPREAD_EXPANDING,
                                   teg_gate.COND_TW2_CONFIRMED])
def test_teg_override_never_for_gap_spread_tw2(strong, extra):
    assert worker._mu_xs_teg_override(_state(), SID, Direction.UP_RED, NOW, (teg_gate.COND_VWAP, extra), {}) is False


def test_teg_override_needs_reasons(strong):
    assert worker._mu_xs_teg_override(_state(), SID, Direction.UP_RED, NOW, (), {}) is False


def test_teg_override_off_keeps_rejection(strong):
    assert worker._mu_xs_teg_override(_state(xs=False), SID, Direction.UP_RED, NOW, REASONS_1239, {}) is False


def test_tw2_veto_scope_is_vwap_and_recent_crosses_only():
    assert worker._MU_XS_OVERRIDABLE_TW2_VETO == {config.TW2_REJECT_VWAP_VETO, config.TW2_REJECT_RECENT_CROSSES}


# ── UI ─────────────────────────────────────────────────────────────────────

def _ui(mu: bool, xs: bool, monkeypatch):
    from tests.macd2.test_ui_strategy_mode import _run as _ui_run
    monkeypatch.setattr(config, "MU_ASSIST_DATA_DIR", "")
    monkeypatch.setattr(config, "EXEC_STRENGTH_DATA_DIR", "")
    st = state_store.load_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    st.mu_assist_enabled = mu
    st.mu_assist_xs_enabled = xs
    state_store.save_state(st)
    return _ui_run()


def test_ui_xs_toggle_sits_right_below_mu_assist_and_persists(monkeypatch):
    at = _ui(True, False, monkeypatch)
    labels = [t.label for t in at.toggle]
    assert XS_LABEL in labels and labels.index(XS_LABEL) == labels.index(MU_LABEL) + 1, "MU Assist 바로 아래"
    tg = [t for t in at.toggle if t.label == XS_LABEL][0]
    assert tg.value is False and not tg.disabled
    assert any("MU Assist XS: OFF" in str(c.value) for c in at.caption)
    tg.set_value(True).run()
    assert state_store.load_state().mu_assist_xs_enabled is True
    assert any("MU Assist XS: 🟢 ON" in str(m.value) for m in at.markdown)
    assert any("체결강도 데이터가 없습니다" in str(w.value) for w in at.warning), "데이터 없음 경고"


def test_ui_xs_toggle_locked_when_mu_assist_off(monkeypatch):
    at = _ui(False, True, monkeypatch)
    tg = [t for t in at.toggle if t.label == XS_LABEL][0]
    assert tg.disabled
    assert any("MU Assist 가 OFF 라 효력 없음" in str(c.value) for c in at.caption)
    assert state_store.load_state().mu_assist_xs_enabled is True, "잠겨도 저장값은 유지"
