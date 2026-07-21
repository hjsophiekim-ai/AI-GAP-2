"""
test_hynix_switch_state.py — mock/real 분리 저장, atomic write, 손상 시 안전 기본값 복구 검증.
"""

from __future__ import annotations

import json
import os
import threading
import time

import app.services.hynix_switch_state as switch_state_module
from app.services.hynix_switch_state import load_state, save_state_atomic, default_state, reset_mock_state
from app.trading.hynix_symbols import LONG_SYMBOL, LONG_NAME, SIGNAL_SYMBOL, SIGNAL_NAME, SHORT_SYMBOL


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
    stale_state["position"]["symbol"] = LONG_SYMBOL
    stale_state["position"]["name"] = LONG_NAME
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


def test_previous_day_position_snapshot_is_dropped_on_load(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    stale_state = default_state("mock")
    stale_state["date"] = switch_state_module._today_str()
    stale_state["position"] = {**stale_state["position"], "symbol": None, "quantity": 0}
    stale_state["stop_loss_snapshot"] = {
        "position_snapshot_id": "old",
        "calculated_at": "2026-07-20T11:00:00",
        "hard_stop_triggered": True,
    }
    stale_state["last_stop_loss_signature"] = "old-stop"
    save_state_atomic(stale_state)

    reloaded = load_state(mode="mock")

    assert reloaded["stop_loss_snapshot"] is None
    assert reloaded["last_stop_loss_signature"] is None


def test_stale_nested_intraday_cache_is_cleared_even_when_top_level_date_is_today(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)
    today = switch_state_module._today_str()

    contaminated = default_state("mock")
    contaminated["date"] = today
    contaminated["trend_switch_confirm_tracker"] = {
        "direction": "INVERSE",
        "same_direction_streak": 11,
        "last_signal_at": "2026-07-14T14:46:52",
        "_state_date": "20260714",
    }
    contaminated["trend_switch_frequency_state"] = {
        "round_trips_today": 2,
        "last_entry_at": "2026-07-14T14:39:35",
        "_state_date": "20260714",
    }
    contaminated["last_trend_switch_plan"] = {
        "proceed": False,
        "block_reason": "old pullback wait",
        "pullback_wait_remaining_seconds": 300,
    }
    contaminated["pending_entry"] = {
        "action": "INVERSE_BUY",
        "symbol": "0197X0",
        "since": "2026-07-14T14:45:00",
    }
    contaminated["last_account_equity_snapshot"] = {
        "ok": True,
        "cash": 10_000_000.0,
        "current_equity": 10_000_000.0,
        "as_of": "2026-07-14T14:50:00",
    }
    contaminated["daily_return_calculation"] = {
        "account_snapshot": {"ok": True, "as_of": "2026-07-14T14:50:00"}
    }
    contaminated["last_big_trend_result"] = {
        "log_row": {"timestamp": "2026-07-14T14:42:39"}
    }
    save_state_atomic(contaminated)

    reloaded = load_state(mode="mock")

    assert reloaded["trend_switch_confirm_tracker"] is None
    assert reloaded["trend_switch_frequency_state"] is None
    assert reloaded["last_trend_switch_plan"] is None
    assert reloaded["pending_entry"] is None
    assert reloaded["last_account_equity_snapshot"] is None
    assert reloaded["daily_return_calculation"] is None
    assert reloaded["last_big_trend_result"] is None
    assert reloaded["position_sync_block_new_orders"] is False


def test_synced_flat_position_clears_stale_position_sync_block(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    state = default_state("mock")
    state["position_sync_status"] = "SYNCED"
    state["position_sync_block_new_orders"] = True
    state["position_sync_error"] = "old EGW00201"
    save_state_atomic(state)

    reloaded = load_state(mode="mock")

    assert reloaded["position_sync_status"] == "SYNCED"
    assert reloaded["position_sync_block_new_orders"] is False
    assert reloaded["position_sync_error"] is None


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
    mock_state["position"]["symbol"] = LONG_SYMBOL
    mock_state["position"]["name"] = LONG_NAME
    mock_state["position"]["quantity"] = 1
    save_state_atomic(mock_state)

    real_state = default_state("real")
    real_state["position"]["symbol"] = SHORT_SYMBOL
    real_state["position"]["quantity"] = 1
    save_state_atomic(real_state)

    reloaded_mock = load_state(mode="mock")
    reloaded_real = load_state(mode="real")

    assert reloaded_mock["current_position"] == LONG_SYMBOL
    assert reloaded_mock["current_position_type"] == "HYNIX"
    assert reloaded_real["current_position"] == SHORT_SYMBOL
    assert reloaded_real["current_position_type"] == "INVERSE"
    assert (tmp_path / "hynix_auto_state_mock.json").exists()
    assert (tmp_path / "hynix_auto_state_real.json").exists()


def test_strategy_profile_toggles_are_shared_between_mock_and_real(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    mock_state = default_state("mock")
    mock_state["mode"] = "mock"
    mock_state["active_strategy_enabled"] = True
    mock_state["adaptive_fusion_enabled"] = True
    mock_state["big_trend_holding_enabled"] = True
    mock_state["early_trend_detector_enabled"] = True
    mock_state["early_trend_detector_live"] = True
    mock_state["adaptive_regime_enabled"] = True
    mock_state["adaptive_regime_mode"] = "LIVE"
    mock_state["daily_loss_block_override"] = True
    save_state_atomic(mock_state)

    real_state = load_state(mode="real")

    for key in (
        "active_strategy_enabled",
        "adaptive_fusion_enabled",
        "big_trend_holding_enabled",
        "early_trend_detector_enabled",
        "early_trend_detector_live",
        "adaptive_regime_enabled",
        "daily_loss_block_override",
    ):
        assert real_state[key] is True
    assert real_state["adaptive_regime_mode"] == "LIVE"
    assert real_state["mode"] == "real"


def test_active_mode_restores_execution_toggles_from_stale_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)
    profile_path = tmp_path / "hynix_strategy_profile_common.json"
    profile_path.write_text(
        json.dumps({
            "auto_trade_on": True,
            "trading_mode": "ACTIVE",
            "active_strategy_enabled": False,
            "adaptive_fusion_enabled": False,
            "early_trend_detector_enabled": False,
            "early_trend_detector_live": False,
            "adaptive_regime_enabled": False,
            "adaptive_regime_mode": "SHADOW",
        }),
        encoding="utf-8",
    )

    state = load_state(mode="mock")

    assert state["trading_mode"] == "ACTIVE"
    assert state["active_strategy_enabled"] is True
    assert state["adaptive_fusion_enabled"] is True
    assert state["early_trend_detector_enabled"] is True
    assert state["early_trend_detector_live"] is True
    assert state["adaptive_regime_enabled"] is True
    assert state["adaptive_regime_mode"] == "LIVE"


def test_signal_symbol_is_not_recognized_as_actual_position(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    state = default_state("mock")
    state["position"] = {
        **state["position"],
        "symbol": SIGNAL_SYMBOL,
        "name": SIGNAL_NAME,
        "quantity": 10,
        "avg_price": 100_000,
        "entry_price": 100_000,
    }
    save_state_atomic(state)

    reloaded = load_state(mode="mock")

    assert reloaded["current_position"] is None
    assert reloaded["current_position_type"] == "NONE"
    assert reloaded["symbol"] is None
    assert reloaded["quantity"] == 0


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
    dirty_state["position"]["symbol"] = LONG_SYMBOL
    dirty_state["position"]["quantity"] = 1
    dirty_state["daily_trade_count"] = 5
    save_state_atomic(dirty_state)

    reset = reset_mock_state(budget_krw=5_000_000)

    assert reset["position"]["symbol"] is None
    assert reset["daily_trade_count"] == 0
    assert reset["cash"] == 5_000_000
    assert reset["mock_budget_krw"] == 5_000_000


# ---------------------------------------------------------------------------
# 요구사항6(2026-07-15) — Fast Watcher/3분 사이클이 여러 프로세스에서 동시에 실행돼도
# 같은 state를 동시에 덮어쓰지 않도록 하는 프로세스 간(cross-process) 락 검증.
# threading.RLock은 같은 프로세스 안에서만 유효하므로, 서로 다른 두 "프로세스"를
# 스레드로 흉내 내되 각자 독립된 RLock 딕셔너리를 쓰는 것처럼 파일 락 자체의 상호배제
# 동작만 직접 검증한다.
# ---------------------------------------------------------------------------

def test_cross_process_lock_serializes_concurrent_holders(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    order: list[str] = []
    barrier_entered = threading.Event()

    def _holder_a():
        with switch_state_module._acquire_cross_process_lock("mock"):
            order.append("a-enter")
            barrier_entered.set()
            time.sleep(0.2)
            order.append("a-exit")

    def _holder_b():
        barrier_entered.wait(timeout=2)
        with switch_state_module._acquire_cross_process_lock("mock"):
            order.append("b-enter")

    ta = threading.Thread(target=_holder_a)
    tb = threading.Thread(target=_holder_b)
    ta.start()
    tb.start()
    ta.join(timeout=5)
    tb.join(timeout=5)

    # b는 a가 락을 놓은 뒤에만 들어갈 수 있어야 한다 — 동시에 겹치지 않는다.
    assert order.index("a-exit") < order.index("b-enter")
    assert not switch_state_module._cross_process_lock_path("mock").exists()


def test_cross_process_lock_is_reentrant_within_same_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    with switch_state_module._acquire_cross_process_lock("mock"):
        # 같은 스레드 안에서 중첩 호출해도 데드락 없이 통과해야 한다(with_state_lock이
        # 내부에서 다시 with_state_lock을 호출하는 경우를 흉내).
        with switch_state_module._acquire_cross_process_lock("mock"):
            assert switch_state_module._cross_process_lock_path("mock").exists()
        # 안쪽 컨텍스트를 빠져나와도 바깥쪽이 아직 살아있는 동안은 락 파일이 남아있어야 한다.
        assert switch_state_module._cross_process_lock_path("mock").exists()

    assert not switch_state_module._cross_process_lock_path("mock").exists()


def test_stale_cross_process_lock_is_stolen_not_deadlocked(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(switch_state_module, "_CROSS_PROCESS_LOCK_STALE_SECONDS", 0.05)
    monkeypatch.setattr(switch_state_module, "_CROSS_PROCESS_LOCK_TIMEOUT_SECONDS", 2.0)

    lock_path = switch_state_module._cross_process_lock_path("mock")
    tmp_path.mkdir(parents=True, exist_ok=True)
    # 죽은 프로세스가 남긴 락 파일을 흉내낸다(오래된 mtime).
    lock_path.write_text("99999", encoding="utf-8")
    stale_time = time.time() - 10
    os.utime(lock_path, (stale_time, stale_time))

    with switch_state_module._acquire_cross_process_lock("mock"):
        assert lock_path.exists()

    assert not lock_path.exists()


def test_with_state_lock_uses_cross_process_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(switch_state_module, "_STATE_DIR", tmp_path)

    with switch_state_module.with_state_lock("mock"):
        assert switch_state_module._cross_process_lock_path("mock").exists()

    assert not switch_state_module._cross_process_lock_path("mock").exists()
