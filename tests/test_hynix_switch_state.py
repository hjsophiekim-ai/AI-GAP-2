"""
test_hynix_switch_state.py — mock/real 분리 저장, atomic write, 손상 시 안전 기본값 복구 검증.
"""

from __future__ import annotations

import json

import app.services.hynix_switch_state as switch_state_module
from app.services.hynix_switch_state import load_state, save_state_atomic, default_state, reset_mock_state


def test_load_state_missing_file_returns_default(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    state = load_state(mode="mock")

    assert state["position"]["symbol"] is None
    assert state["daily_trade_count"] == 0
    assert state["mode"] == "mock"


def test_load_state_corrupt_file_recovers_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)
    (tmp_path / "hynix_auto_state_mock.json").write_text("{ this is not valid json ]]", encoding="utf-8")

    state = load_state(mode="mock")  # 예외를 던지지 않고 안전 기본값 반환해야 함

    assert state["position"]["symbol"] is None


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    state = default_state("mock")
    state["daily_trade_count"] = 3
    save_state_atomic(state)

    path = tmp_path / "hynix_auto_state_mock.json"
    assert path.exists()
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["daily_trade_count"] == 3


def test_residual_position_flagged_on_new_day(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    stale_state = default_state("mock")
    stale_state["date"] = "20200101"  # 과거 날짜
    stale_state["position"] = {
        "symbol": "000660", "name": "SK하이닉스", "quantity": 10, "avg_price": 100_000,
        "entry_price": 100_000, "entry_time": "2020-01-01T15:00:00",
        "partial_tp1_done": False, "partial_sl1_done": False,
    }
    save_state_atomic(stale_state)

    reloaded = load_state(mode="mock")

    assert reloaded["residual_position_error"] is True
    assert reloaded["daily_trade_count"] == 0  # 신규일자로 카운터 리셋


def test_last_trade_fields_reset_on_new_day(tmp_path, monkeypatch):
    """전일 거래 잔재(Last Buy/Sell/Action Time/Pending Entry)가 오늘 화면에 남지 않아야 한다."""
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    stale_state = default_state("mock")
    stale_state["date"] = "20260709"
    stale_state["last_buy_price"] = 2_211_000.0
    stale_state["last_sell_price"] = 2_186_000.0
    stale_state["last_trade_time"] = "2026-07-09T11:30:00.241536"
    stale_state["last_action"] = "BUY"
    stale_state["last_order_id"] = "DRY-20260709-0001"
    stale_state["pending_entry"] = {"action": "INVERSE_BUY", "symbol": "0197X0", "since": "2026-07-09T09:24:20"}
    stale_state["pending_manual_stop_loss_alert"] = {"symbol": "000660", "reason": "test"}
    save_state_atomic(stale_state)

    reloaded = load_state(mode="mock")

    assert reloaded["last_buy_price"] is None
    assert reloaded["last_sell_price"] is None
    assert reloaded["last_trade_time"] is None
    assert reloaded["last_action"] is None
    assert reloaded["last_order_id"] is None
    assert reloaded["pending_entry"] is None
    assert reloaded["pending_manual_stop_loss_alert"] is None


def test_all_time_last_order_survives_day_rollover(tmp_path, monkeypatch):
    """'오늘 마지막 주문'은 날짜가 바뀌면 리셋되지만 '전체 마지막 주문'은 영구 보존돼야 한다."""
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    stale_state = default_state("mock")
    stale_state["date"] = "20260709"
    stale_state["last_buy_price"] = 2_211_000.0
    stale_state["last_trade_time"] = "2026-07-09T11:30:00.241536"
    stale_state["last_action"] = "BUY"
    stale_state["last_order_id"] = "DRY-20260709-0001"
    save_state_atomic(stale_state)  # _sync_flat_fields가 all_time_* 필드를 채운다

    reloaded = load_state(mode="mock")  # 날짜 롤오버 트리거

    assert reloaded["last_order_id"] is None  # 오늘 마지막 주문 — 리셋됨
    assert reloaded["all_time_last_order_id"] == "DRY-20260709-0001"  # 전체 마지막 주문 — 보존됨
    assert reloaded["all_time_last_action"] == "BUY"
    assert reloaded["all_time_last_buy_price"] == 2_211_000.0


def test_mock_and_real_states_are_separate_files(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    mock_state = default_state("mock")
    mock_state["position"]["symbol"] = "000660"
    save_state_atomic(mock_state)

    real_state = default_state("real")
    real_state["position"]["symbol"] = "0197X0"
    save_state_atomic(real_state)

    reloaded_mock = load_state(mode="mock")
    reloaded_real = load_state(mode="real")

    assert reloaded_mock["position"]["symbol"] == "000660"
    assert reloaded_real["position"]["symbol"] == "0197X0"
    assert (tmp_path / "hynix_auto_state_mock.json").exists()
    assert (tmp_path / "hynix_auto_state_real.json").exists()


def test_active_mode_pointer_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    assert switch_state_module.get_active_mode() == "mock"  # 포인터 없으면 기본 mock
    switch_state_module.set_active_mode("real")
    assert switch_state_module.get_active_mode() == "real"

    state = load_state()  # mode 인자 없이 호출하면 포인터를 따라야 함
    assert state["mode"] == "real"


def test_reset_mock_state_clears_position_and_sets_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)
    import app.trading.dry_run_broker as dry_run_broker_module
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)  # 실제 data/orders/ 삭제 방지

    dirty_state = default_state("mock")
    dirty_state["position"]["symbol"] = "000660"
    dirty_state["daily_trade_count"] = 5
    save_state_atomic(dirty_state)

    reset = reset_mock_state(budget_krw=5_000_000)

    assert reset["position"]["symbol"] is None
    assert reset["daily_trade_count"] == 0
    assert reset["cash"] == 5_000_000
    assert reset["mock_budget_krw"] == 5_000_000
