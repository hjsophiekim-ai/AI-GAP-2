"""Regression: Enhanced OFF must persist for MACD handoff.

Covers:
- ON → load_state True
- OFF (set_control / stop_auto_trade) → same mode+common files False
- Still False after simulated background save / page-reload load_state
- Background must not restore False→True
- Enhanced OFF → MACD Start succeeds
"""

from __future__ import annotations

import json
import time

import app.services.hynix_auto_trade_service as hats
import app.services.hynix_switch_state as hss
import app.trading.macd_hynix_order_manager as om
import app.trading.macd_hynix_worker as worker
from app.services.hynix_switch_engine import set_control


def _seed_enhanced_on(tmp_path, monkeypatch):
    monkeypatch.setattr(hss, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hats, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hats, "_STOP_FLAG_PATH", tmp_path / "hynix_auto_trade_stopped.flag")
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "MUTEX_PATH", tmp_path / "macd_hynix_mutex.json")
    monkeypatch.setattr(om, "STATE_PATH", tmp_path / "macd_hynix_state.json")
    (tmp_path / "hynix_auto_state_active_mode.json").write_text(
        json.dumps({"mode": "mock"}), encoding="utf-8"
    )
    state = hss.default_state("mock")
    state["auto_trade_on"] = True
    hss.save_state_atomic(state, allow_enable_auto_trade=True)
    return state


def test_enhanced_on_load_state_true(tmp_path, monkeypatch):
    _seed_enhanced_on(tmp_path, monkeypatch)
    loaded = hss.load_state()
    assert loaded["auto_trade_on"] is True
    mode_raw = json.loads((tmp_path / "hynix_auto_state_mock.json").read_text(encoding="utf-8"))
    common_raw = json.loads((tmp_path / "hynix_strategy_profile_common.json").read_text(encoding="utf-8"))
    assert mode_raw["auto_trade_on"] is True
    assert common_raw["auto_trade_on"] is True


def test_off_click_writes_same_file_false(tmp_path, monkeypatch):
    _seed_enhanced_on(tmp_path, monkeypatch)
    before = bool(hss.load_state().get("auto_trade_on"))
    assert before is True

    state = set_control(auto_trade_on=False)
    verify = state.get("_control_verify") or {}
    assert verify.get("before") is True
    assert verify.get("after") is False
    assert verify.get("ok") is True
    assert str(verify.get("path", "")).endswith("hynix_auto_state_mock.json")
    assert verify.get("mtime")

    loaded = hss.load_state()
    assert loaded["auto_trade_on"] is False
    mode_raw = json.loads((tmp_path / "hynix_auto_state_mock.json").read_text(encoding="utf-8"))
    common_raw = json.loads((tmp_path / "hynix_strategy_profile_common.json").read_text(encoding="utf-8"))
    assert mode_raw["auto_trade_on"] is False
    assert common_raw["auto_trade_on"] is False


def test_stop_auto_trade_persists_false(tmp_path, monkeypatch):
    _seed_enhanced_on(tmp_path, monkeypatch)
    verify = hats.stop_auto_trade()
    assert verify.get("ok") is True
    assert verify.get("after") is False
    assert hss.load_state()["auto_trade_on"] is False
    assert hats.is_stopped() is True


def test_still_false_after_background_ticks(tmp_path, monkeypatch):
    """Simulate background cycle saves that still have auto_trade_on=True in memory."""
    _seed_enhanced_on(tmp_path, monkeypatch)
    set_control(auto_trade_on=False)
    assert hss.load_state()["auto_trade_on"] is False

    # Stale in-memory snapshot from before OFF (classic lost-update / page-rerun path).
    stale = hss.default_state("mock")
    stale["auto_trade_on"] = True
    stale["daily_trade_count"] = 99
    saved = hss.save_state_atomic(stale)  # allow_enable defaults False
    assert saved is True

    loaded = hss.load_state()
    assert loaded["auto_trade_on"] is False, "background must not restore False→True"
    # Other fields from the stale save may apply, but control flag must stay OFF.
    mode_raw = json.loads((tmp_path / "hynix_auto_state_mock.json").read_text(encoding="utf-8"))
    common_raw = json.loads((tmp_path / "hynix_strategy_profile_common.json").read_text(encoding="utf-8"))
    assert mode_raw["auto_trade_on"] is False
    assert common_raw["auto_trade_on"] is False

    # Survive a short wall-clock window (multiple reload ticks).
    deadline = time.time() + 1.0
    while time.time() < deadline:
        assert hss.load_state()["auto_trade_on"] is False
        time.sleep(0.05)


def test_still_false_after_page_refresh_reload(tmp_path, monkeypatch):
    _seed_enhanced_on(tmp_path, monkeypatch)
    set_control(auto_trade_on=False)
    # Simulate Streamlit page refresh: brand-new load_state()
    assert hss.load_state()["auto_trade_on"] is False
    assert hss.load_state(mode="mock")["auto_trade_on"] is False


def test_enhanced_off_macd_start_succeeds(tmp_path, monkeypatch):
    _seed_enhanced_on(tmp_path, monkeypatch)
    assert om.can_start_macd("mock")[0] is False

    verify = hats.stop_auto_trade()
    assert verify.get("after") is False
    ok, msg = om.can_start_macd("mock")
    assert ok is True, msg

    res = worker.start_auto_trade(mode="mock", budget=1_000_000)
    assert res["ok"] is True
    assert om.read_mutex().get("enabled") is True
    worker.stop_auto_trade("test")


def test_only_explicit_start_may_enable(tmp_path, monkeypatch):
    _seed_enhanced_on(tmp_path, monkeypatch)
    set_control(auto_trade_on=False)

    # Non-control save cannot re-enable
    stale = hss.load_state()
    stale["auto_trade_on"] = True
    hss.save_state_atomic(stale)
    assert hss.load_state()["auto_trade_on"] is False

    # Explicit set_control(True) can enable
    on = set_control(auto_trade_on=True)
    assert on.get("_control_verify", {}).get("after") is True
    assert hss.load_state()["auto_trade_on"] is True
