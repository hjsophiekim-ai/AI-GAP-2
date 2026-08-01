"""
test_kst_timezone_and_account_diagnostics.py — Render(UTC) 운영환경의 시간대/계좌조회/
일일초기화 회귀 테스트(2026-07-16).

증상 재현 및 회귀 방지:
  1) Render 서버 UTC 23:12 == KST 08:12로 변환되는지(naive datetime.now() 오사용 금지)
  2) KST 08:12에 "14:50 이후" 오판(신규진입 차단)이 발생하지 않는지
  3) Micron 캔들 age가 음수(시계/타임존 불일치)일 때 fresh로 오판하지 않는지
  4) KST 자정에 cycle_count_today/liquidation_done/pending_entry/Micron 스냅샷이 리셋되는지
  5) 매수가능금액 "조회 실패"와 "실제 0원"이 구분되는지
  6) 정규장 시간대 + 계좌 정상이면 매수가능금액이 정상적으로 산정되는지(주문 허용)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 1) Render UTC 23:12 -> KST 08:12
# ---------------------------------------------------------------------------

def test_render_utc_2312_maps_to_kst_0812(monkeypatch):
    import app.utils.time_utils as tu

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            utc_fixed = datetime(2026, 7, 15, 23, 12, tzinfo=timezone.utc)
            return utc_fixed.astimezone(tz) if tz else utc_fixed

    monkeypatch.setattr(tu, "datetime", FakeDatetime)
    result = tu.kst_now()
    assert (result.hour, result.minute) == (8, 12)
    # KST가 하루 넘어간다(UTC 7/15 23:12 -> KST 7/16 08:12)
    assert result.day == 16


# ---------------------------------------------------------------------------
# 2) KST 08:12에는 "14:50 이후" 차단이 발생하지 않아야 한다
# ---------------------------------------------------------------------------

def test_no_after_1450_block_at_0812_kst():
    """요구사항(2026-07-20) — 09:00~09:10 관망(watch-only) 규칙은 완전히
    삭제됐다. 08:12는 09:00 장 시작 전이라 신규진입은 아직 허용 안 되지만, 그
    이유가 "14:50 이후"가 아니라 "장 시작 전"이어야 한다."""
    from app.trading.hynix_switch_risk_gate import (
        is_new_entry_allowed, get_liquidation_phase, is_within_operating_window,
    )

    now = datetime(2026, 7, 16, 8, 12)
    assert is_new_entry_allowed(now) is False
    assert get_liquidation_phase(now) == "normal"
    # 운영창(08:50~15:30) 이전이므로 heartbeat-only 여야 한다.
    assert is_within_operating_window(now) is False
    # 09:30에는 정상적으로 신규진입 허용 + 운영창 안
    mid_session = datetime(2026, 7, 16, 9, 30)
    assert is_new_entry_allowed(mid_session) is True
    assert is_within_operating_window(mid_session) is True


# ---------------------------------------------------------------------------
# 3) Micron age가 음수면 fresh로 취급하면 안 된다(DATA_TIME_ERROR)
# ---------------------------------------------------------------------------

def test_negative_micron_age_is_not_treated_as_fresh(monkeypatch):
    import app.models.hynix_micron_realtime_score as mscore

    fixed_now = datetime(2026, 7, 16, 8, 12)
    monkeypatch.setattr(mscore, "kst_now", lambda: fixed_now)

    # 캔들 시각이 "현재"보다 미래(예: 서버가 UTC로 착각해 KST 캔들을 앞선 것처럼 계산)
    future_ts = datetime(2026, 7, 16, 17, 30)
    df = pd.DataFrame({
        "datetime": [future_ts],
        "open": [100.0], "close": [101.0], "volume": [1000],
    })

    age = mscore._age_minutes(df)
    assert age is not None and age < 0

    # 음수 age는 어떤 stale_minutes 기준을 넣어도 "fresh"가 아니어야 한다.
    assert mscore._is_fresh(df, stale_minutes=999999) is False


def test_enhanced_score_micron_negative_age_treated_as_stale(monkeypatch):
    """hynix_enhanced_score.py의 _is_micron_stale_for_orders()가 별도로 같은 클래스의
    버그(음수 age를 fresh로 오판)를 갖고 있었다 — hynix_micron_realtime_score.py와는
    다른 모듈이라 최초 KST 정리 때 놓쳤다(2026-07-16 후속 발견)."""
    import app.models.hynix_enhanced_score as es

    fixed_now = datetime(2026, 7, 16, 9, 30)
    monkeypatch.setattr(es, "kst_now", lambda: fixed_now)

    future_ts = (fixed_now.replace(hour=17, minute=0)).isoformat()
    micron_result = {"micron_last_update_time": future_ts, "micron_data_status": "OK"}

    age = es._micron_age_minutes(micron_result)
    assert age is not None and age < 0
    assert es._is_micron_stale_for_orders(micron_result) is True


def test_positive_fresh_age_still_works(monkeypatch):
    import app.models.hynix_micron_realtime_score as mscore

    fixed_now = datetime(2026, 7, 16, 8, 12)
    monkeypatch.setattr(mscore, "kst_now", lambda: fixed_now)

    recent_ts = datetime(2026, 7, 16, 8, 10)  # 2분 전 — 신선함
    df = pd.DataFrame({
        "datetime": [recent_ts],
        "open": [100.0], "close": [101.0], "volume": [1000],
    })
    assert mscore._is_fresh(df, stale_minutes=15.0) is True

    old_ts = datetime(2026, 7, 16, 7, 30)  # 42분 전 — 오래됨(그러나 음수는 아님)
    df_old = pd.DataFrame({
        "datetime": [old_ts],
        "open": [100.0], "close": [101.0], "volume": [1000],
    })
    assert mscore._is_fresh(df_old, stale_minutes=15.0) is False


# ---------------------------------------------------------------------------
# 4) KST 자정에 일일 상태(사이클 카운트/청산/보류진입/Micron 스냅샷) 리셋
# ---------------------------------------------------------------------------

def test_kst_midnight_resets_daily_state_and_micron_snapshot(tmp_path, monkeypatch):
    import app.services.hynix_switch_state as state_module

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    yesterday = datetime(2026, 7, 15, 20, 0)
    monkeypatch.setattr(state_module, "kst_now", lambda: yesterday)
    state = state_module.default_state("mock")
    state["date"] = "20260715"
    state["liquidation_done"] = True
    state["pending_entry"] = {"since": "2026-07-15T13:00:00", "symbol": "0193T0"}
    state["trend_switch_confirm_tracker"] = {"same_direction_streak": 3, "last_signal_at": "2026-07-15T13:00:00"}
    state["last_micron_proxy_snapshot"] = {"effective_micron_score": 62.0, "calculated_at": "2026-07-15T13:00:00"}
    state_module.save_state_atomic(state)

    today = datetime(2026, 7, 16, 8, 30)
    monkeypatch.setattr(state_module, "kst_now", lambda: today)
    reloaded = state_module.load_state(mode="mock")

    assert reloaded["date"] == "20260716"
    assert reloaded["liquidation_done"] is False
    assert reloaded["pending_entry"] is None
    assert reloaded["trend_switch_confirm_tracker"] is None
    assert reloaded["last_micron_proxy_snapshot"] is None


def test_scheduler_cycle_count_resets_on_new_kst_day():
    import app.services.hynix_auto_trade_scheduler as scheduler_module

    with scheduler_module._status_lock:
        scheduler_module._status["_cycle_count_date"] = "20260715"
        scheduler_module._status["cycle_count_today"] = 284

    scheduler_module._reset_cycle_count_if_new_kst_day(datetime(2026, 7, 16, 0, 5))

    with scheduler_module._status_lock:
        assert scheduler_module._status["cycle_count_today"] == 0
        assert scheduler_module._status["_cycle_count_date"] == "20260716"


def test_scheduler_skips_full_cycle_outside_operating_window(tmp_path, monkeypatch):
    import app.services.hynix_switch_state as state_module
    import app.services.hynix_auto_trade_scheduler as scheduler_module
    import app.services.hynix_switch_engine as engine_module

    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["stopped"] = False
    state_module.save_state_atomic(state)

    calls = []
    monkeypatch.setattr(engine_module, "update_hynix_auto_trade_loop", lambda **kw: calls.append(kw) or {})

    off_hours = datetime(2026, 7, 16, 3, 0)  # 새벽 3시 KST — 운영창 밖
    monkeypatch.setattr(scheduler_module, "kst_now", lambda: off_hours)

    thread = scheduler_module.HynixAutoTradeCycleThread(interval_seconds=999)
    thread._run_cycle_if_enabled()

    assert calls == []  # 장외에는 전체 사이클(시세/주문/계좌조회)을 돌리지 않는다
    status = scheduler_module.get_status()
    assert status["within_operating_window"] is False
    assert status["cycle_count_today"] == 0


# ---------------------------------------------------------------------------
# 5) 매수가능금액 "조회 실패" vs "실제 0원" 구분
# ---------------------------------------------------------------------------

def test_kis_client_buyable_cash_status_distinguishes_api_error_from_zero(monkeypatch):
    from app.trading.kis_client import KISClient

    client = KISClient(app_key="dummy", app_secret="dummy", account_no="12345678", mode="mock")

    # (a) API 오류(예외/HTTP 실패) — error 필드가 채워짐
    monkeypatch.setattr(client, "get_buyable_cash_raw", lambda **kw: {
        "output": {}, "ord_psbl_cash": 0.0, "nrcvb_buy_amt": 0.0, "psbl_qty": 0,
        "rt_cd": "", "msg_cd": "", "msg1": "", "params_used": {}, "error": "HTTP 500",
    })
    status = client.get_buyable_cash_status()
    assert status["ok"] is False
    assert status["status"] == "API_ERROR"

    # (b) rt_cd != 0 — 명시적 API 오류
    monkeypatch.setattr(client, "get_buyable_cash_raw", lambda **kw: {
        "output": {}, "ord_psbl_cash": 0.0, "nrcvb_buy_amt": 0.0, "psbl_qty": 0,
        "rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다", "params_used": {},
    })
    status = client.get_buyable_cash_status()
    assert status["ok"] is False
    assert status["status"] == "API_ERROR"
    assert status["msg_cd"] == "EGW00201"

    # (c) 정상 응답, 실제로 잔고가 0원
    monkeypatch.setattr(client, "get_buyable_cash_raw", lambda **kw: {
        "output": {"ord_psbl_cash": "0", "nrcvb_buy_amt": "0"}, "ord_psbl_cash": 0.0, "nrcvb_buy_amt": 0.0,
        "psbl_qty": 0, "rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다", "params_used": {},
    })
    status = client.get_buyable_cash_status()
    assert status["ok"] is True
    assert status["status"] == "OK"
    assert status["value"] == 0.0

    # (d) 정상 응답, 실제 잔고 1000만원
    monkeypatch.setattr(client, "get_buyable_cash_raw", lambda **kw: {
        "output": {"ord_psbl_cash": "10000000", "nrcvb_buy_amt": "10000000"},
        "ord_psbl_cash": 10_000_000.0, "nrcvb_buy_amt": 10_000_000.0,
        "psbl_qty": 100, "rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다", "params_used": {},
    })
    status = client.get_buyable_cash_status()
    assert status["ok"] is True
    assert status["value"] == 10_000_000.0


class _FakeBrokerWithCashStatus:
    """get_buyable_cash_status()를 지원하는 브로커(KisMockBroker/KisRealBroker 흉내)."""

    def __init__(self, status: dict):
        self._status = status

    def get_buyable_cash_status(self, symbol=None, price=0):
        return dict(self._status)

    def get_buyable_cash(self):
        return self._status.get("value", 0.0)


def test_query_buyable_cash_does_not_paper_over_genuine_zero(monkeypatch):
    """조회는 정상(ok=True)인데 실제 잔고가 0원이면, state의 mock_budget_krw(1000만원)로
    대체하면 안 된다 — 그건 "조회 실패"에만 쓰는 안전장치다."""
    from app.trading.hynix_switch_position_manager import _query_buyable_cash

    broker = _FakeBrokerWithCashStatus({"value": 0.0, "ok": True, "status": "OK", "source": "kis_mock"})
    state = {"mode": "mock", "mock_budget_krw": 10_000_000.0, "cash": None}

    cash, source = _query_buyable_cash(broker, symbol="0193T0", current_price=100.0, state=state)

    assert cash == 0.0
    assert "zero" in source
    assert state["buyable_cash_diagnostic"]["ok"] is True
    assert state["buyable_cash_diagnostic"]["status"] == "OK"


def test_query_buyable_cash_falls_back_when_api_actually_fails(monkeypatch):
    """API 실패(ok=False)일 때는 기존처럼 state 캐시(mock_budget_krw 등)로 폴백해
    9시 정각 매매가 계좌조회 일시 오류만으로 완전히 막히지 않게 한다."""
    from app.trading.hynix_switch_position_manager import _query_buyable_cash

    broker = _FakeBrokerWithCashStatus({
        "value": 0.0, "ok": False, "status": "API_ERROR", "source": "kis_mock",
        "rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다",
        "error": "rt_cd=1: 초당 거래건수를 초과하였습니다",
    })
    state = {"mode": "mock", "mock_budget_krw": 10_000_000.0, "cash": 10_000_000.0}

    cash, source = _query_buyable_cash(broker, symbol="0193T0", current_price=100.0, state=state)

    assert cash == 10_000_000.0
    assert source.startswith("state_fallback")
    assert state["buyable_cash_diagnostic"]["ok"] is False
    assert state["buyable_cash_diagnostic"]["status"] == "API_ERROR"
    assert state["buyable_cash_diagnostic"]["msg_cd"] == "EGW00201"


# ---------------------------------------------------------------------------
# 6) 정규장 + 계좌 정상 -> 매수가능금액 정상 산정(주문 허용)
# ---------------------------------------------------------------------------

def test_order_allowed_when_market_open_and_account_healthy():
    from app.trading.hynix_switch_risk_gate import is_new_entry_allowed
    from app.trading.hynix_switch_position_manager import _query_buyable_cash

    market_open_time = datetime(2026, 7, 16, 10, 0)
    assert is_new_entry_allowed(market_open_time) is True

    broker = _FakeBrokerWithCashStatus({
        "value": 10_000_000.0, "ok": True, "status": "OK", "source": "kis_mock",
        "rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다",
    })
    state = {"mode": "mock", "mock_budget_krw": 10_000_000.0, "cash": 10_000_000.0}
    cash, source = _query_buyable_cash(broker, symbol="0193T0", current_price=100.0, state=state)

    assert cash == 10_000_000.0
    assert state["buyable_cash_diagnostic"]["ok"] is True
