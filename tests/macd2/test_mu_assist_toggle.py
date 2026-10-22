"""MU ASSIST UI 토글 (2026-10-23) — 저장 state 기준 ON/OFF, env 는 저장값이 없을 때의 기본값.

고정하는 계약
  · 기본 OFF (env 미설정)
  · 저장값이 없으면 env MACD2_MU_ASSIST_ENABLED 를 따른다 (배포 직후 env=1 이면 ON 이 이어진다)
  · 저장값이 있으면 env 보다 우선한다 (UI 에서 끈 값이 재시작 후에도 유지)
  · worker 는 state 값을 본다 (env ON / state OFF → 훅 무호출, env OFF / state ON → 동작)
  · UI: 모든 전략모드에서 토글이 보이고, 바꾸면 저장된다. 데이터 폴더가 없으면 경고.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, mu_assist, service as service_module, state_store, strategy_mode, worker
from app.trading.macd2.worker import run_once
from tests.macd2.test_mu_assist import _broker, _patch, _prime, _t2, _t3, _mu_rows
from tests.macd2.test_tw2_3slot_worker_regression import tw2_3slot_market_data  # noqa: F401

ALL = [strategy_mode.MODE_N1, strategy_mode.MODE_P3, strategy_mode.MODE_E, strategy_mode.MODE_CHOP]


@pytest.fixture(autouse=True)
def _fresh():
    mu_assist.reset_for_tests()
    yield
    mu_assist.reset_for_tests()


def test_default_off_without_env():
    assert config.MU_ASSIST_ENABLED is False
    assert state_store.default_state().mu_assist_enabled is False
    assert state_store.load_state().mu_assist_enabled is False


def test_env_seeds_only_when_nothing_is_stored(monkeypatch):
    st = state_store.load_state()
    state_store.save_state(st)                      # 저장 파일에 mu_assist_enabled=False 가 들어간다
    monkeypatch.setattr(config, "MU_ASSIST_ENABLED", True)
    assert state_store.default_state().mu_assist_enabled is True, "새 state 는 env 기본값"
    assert state_store.load_state().mu_assist_enabled is False, "저장값이 env 보다 우선"


def test_missing_key_in_old_state_file_follows_env(monkeypatch):
    st = state_store.load_state()
    state_store.save_state(st)
    import json
    path = state_store.STATE_PATH                    # conftest 가 tmp 로 돌려 둔 경로
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("mu_assist_enabled", None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(config, "MU_ASSIST_ENABLED", True)
    assert state_store.load_state().mu_assist_enabled is True, "기존 state 파일 + env=1 → ON 이 이어진다"


def test_service_setter_persists_and_survives_mode_change():
    svc = service_module.Macd2Service.__new__(service_module.Macd2Service)
    st = state_store.load_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    state_store.save_state(st)
    out = svc.set_mu_assist_enabled(True, changed_by="test")
    assert out["ok"] and out["mu_assist_enabled"] is True and out["previous"] is False
    re = state_store.load_state()
    assert re.mu_assist_enabled is True and re.mu_assist_changed_by == "test"
    svc.set_strategy_mode(strategy_mode.MODE_N1, changed_by="test")
    assert state_store.load_state().mu_assist_enabled is True, "모드를 바꿔도 유지"
    assert svc.set_mu_assist_enabled(False)["mu_assist_enabled"] is False
    assert state_store.load_state().mu_assist_enabled is False


def test_is_enabled_reads_state_over_env(monkeypatch):
    s = state_store.default_state()
    monkeypatch.setattr(config, "MU_ASSIST_ENABLED", True)
    s.mu_assist_enabled = False
    assert mu_assist.is_enabled(s) is False
    monkeypatch.setattr(config, "MU_ASSIST_ENABLED", False)
    s.mu_assist_enabled = True
    assert mu_assist.is_enabled(s) is True
    assert mu_assist.is_enabled() is False          # state 없이 부르면 env


def test_worker_follows_state_not_env(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    _patch(monkeypatch)
    monkeypatch.setattr(worker.mu_assist, "load_mu_1m", lambda *a, **k: None)

    # env ON, state OFF → 훅이 전혀 돌지 않는다
    monkeypatch.setattr(config, "MU_ASSIST_ENABLED", True)
    st = state_store.default_state()
    st.auto_trade_on, st.budget = True, 10_000_000.0
    strategy_mode.apply(st, strategy_mode.MODE_N1)
    st.mu_assist_enabled = False

    def _boom(*_a, **_k):
        raise AssertionError("state OFF 인데 훅이 돌았다")

    monkeypatch.setattr(worker, "_advance_mu_assist_entry_shadow", _boom)
    _prime(st, now0)
    run_once(broker=_broker(), market_data=svc, state=st, now=_t2(now0))
    assert _mu_rows() == []


def test_worker_runs_when_state_on_even_if_env_off(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    _patch(monkeypatch)
    monkeypatch.setattr(worker.mu_assist, "load_mu_1m", lambda *a, **k: None)
    monkeypatch.setattr(config, "MU_ASSIST_ENABLED", False)
    st = state_store.default_state()
    st.auto_trade_on, st.budget = True, 10_000_000.0
    strategy_mode.apply(st, strategy_mode.MODE_N1)
    st.mu_assist_enabled = True
    _prime(st, now0)
    run_once(broker=_broker(), market_data=svc, state=st, now=_t2(now0))
    rows = _mu_rows()
    assert len(rows) == 1 and rows[0]["mu_entry_assist_status"] == mu_assist.ENTRY_MU_UNAVAILABLE


TOGGLE_LABEL = "MU Assist"


@pytest.mark.parametrize("mode", ALL)
def test_ui_toggle_is_shown_in_every_mode_and_persists(mode, monkeypatch):
    from tests.macd2.test_ui_strategy_mode import _run as _ui_run
    monkeypatch.setattr(config, "MU_ASSIST_DATA_DIR", "")
    st = state_store.load_state()
    strategy_mode.apply(st, mode)
    st.mu_assist_enabled = False
    state_store.save_state(st)
    at = _ui_run()
    tg = [t for t in at.toggle if t.label == TOGGLE_LABEL]
    assert len(tg) == 1 and tg[0].value is False
    assert any("MU Assist: OFF" in str(c.value) for c in at.caption)
    tg[0].set_value(True).run()
    assert state_store.load_state().mu_assist_enabled is True
    assert any("MU Assist: 🟢 ON" in str(m.value) for m in at.markdown)
    assert any("MU 데이터 폴더를 찾지 못했습니다" in str(w.value) for w in at.warning), "폴더 없음 경고"
