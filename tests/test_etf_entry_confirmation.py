"""
test_etf_entry_confirmation.py — 000660(방향판단)/실제 거래 ETF(실행판단) 분리 검증.

절대 000660 분봉을 0193T0/0197X0 데이터로 대체하지 않는지, ETF 자체 데이터가
부족/오래됐을 때 fail-closed(ETF_DATA_INSUFFICIENT)로 신규진입을 막는지,
VWAP/기울기/추격/극값 불일치가 올바른 코드로 차단되는지 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading import etf_entry_confirmation as confirm
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL


def _bars(prices: list[float], volumes: list[float] | None = None, start: datetime | None = None, minute_step: float = 1.0) -> pd.DataFrame:
    start = start or datetime(2026, 7, 20, 9, 30)
    volumes = volumes or [1000.0] * len(prices)
    rows = []
    for i, (p, v) in enumerate(zip(prices, volumes)):
        rows.append({
            "datetime": start + timedelta(minutes=i * minute_step),
            "open": p, "high": p * 1.001, "low": p * 0.999, "close": p, "volume": v,
        })
    return pd.DataFrame(rows)


def _fresh_result(df) -> dict:
    return {"df_1min": df, "source": "kis", "status": "success", "stale": False, "last_bar_time": df["datetime"].iloc[-1].isoformat(), "error": None}


# ── 캐시 분리(요구사항 — 절대 000660으로 대체하지 않는다) ─────────────────────

def test_fetch_etf_minute_bars_uses_genuinely_separate_collector_per_symbol(monkeypatch):
    import app.data_sources.hynix_long_collector as long_collector
    import app.data_sources.hynix_inverse_collector as inverse_collector

    long_called, inverse_called = [], []
    monkeypatch.setattr(long_collector, "collect_long_minute", lambda mode=None: long_called.append(1) or {"df_1min": None, "source": "long_marker"})
    monkeypatch.setattr(inverse_collector, "collect_inverse_minute", lambda mode=None: inverse_called.append(1) or {"df_1min": None, "source": "inverse_marker"})

    long_result = confirm.fetch_etf_minute_bars(LONG_SYMBOL)
    inverse_result = confirm.fetch_etf_minute_bars(INVERSE_SYMBOL)

    assert long_result["source"] == "long_marker" and len(long_called) == 1
    assert inverse_result["source"] == "inverse_marker" and len(inverse_called) == 1
    assert long_result["source"] != inverse_result["source"]


def test_minute_bar_cache_files_are_physically_separate_from_000660_and_each_other():
    import app.data_sources.auto_market_collector as hynix_signal_collector
    import app.data_sources.hynix_long_collector as long_collector
    import app.data_sources.hynix_inverse_collector as inverse_collector

    paths = {
        str(hynix_signal_collector._HYNIX_MINUTE_CSV),
        str(long_collector._MINUTE_CSV),
        str(inverse_collector._MINUTE_CSV),
    }
    assert len(paths) == 3, f"세 캐시 파일이 서로 달라야 하는데 겹친다: {paths}"
    assert "long" in str(long_collector._MINUTE_CSV).lower()
    assert "inverse" in str(inverse_collector._MINUTE_CSV).lower()


# ── fail-closed(ETF_DATA_INSUFFICIENT) ───────────────────────────────────────

def test_missing_dataframe_fails_closed():
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_000.0,
        minute_bars_result={"df_1min": None, "source": None, "status": "unavailable", "stale": False, "last_bar_time": None, "error": "no data"},
    )
    assert result["approved"] is False
    assert result["block_code"] == confirm.ETF_DATA_INSUFFICIENT
    assert result["using_genuine_etf_data"] is False


def test_stale_dataframe_fails_closed_even_if_present():
    df = _bars([10_000.0] * 10)
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_000.0,
        minute_bars_result={"df_1min": df, "source": "cache", "status": "stale_cache", "stale": True, "last_bar_time": "x", "error": "stale"},
    )
    assert result["approved"] is False
    assert result["block_code"] == confirm.ETF_DATA_INSUFFICIENT


def test_too_few_bars_fails_closed():
    df = _bars([10_000.0, 10_010.0])  # MIN_BARS_FOR_CONFIRMATION(5) 미만
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_010.0,
        minute_bars_result=_fresh_result(df),
    )
    assert result["approved"] is False
    assert result["block_code"] == confirm.ETF_DATA_INSUFFICIENT


def test_sufficient_fresh_bars_are_flagged_as_genuine_etf_data():
    df = _bars([10_000.0, 10_010.0, 10_020.0, 10_030.0, 10_040.0, 10_050.0])
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_050.0,
        minute_bars_result=_fresh_result(df),
    )
    assert result["using_genuine_etf_data"] is True


# ── VWAP/기울기 방향 불일치 ───────────────────────────────────────────────────

def test_direction_mismatch_when_below_vwap_but_underlying_says_up():
    # 가격이 하락 추세라 VWAP보다 한참 낮음 — 기초자산은 UP이라고 주장
    df = _bars([10_100.0, 10_080.0, 10_060.0, 10_040.0, 10_020.0, 10_000.0])
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_000.0,
        minute_bars_result=_fresh_result(df),
    )
    assert result["approved"] is False
    assert result["block_code"] == confirm.ETF_DIRECTION_MISMATCH


def test_direction_agrees_passes_vwap_and_slope_checks():
    df = _bars([10_000.0, 10_020.0, 10_040.0, 10_060.0, 10_080.0, 10_100.0])
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_100.0,
        minute_bars_result=_fresh_result(df),
    )
    assert result["block_code"] != confirm.ETF_DIRECTION_MISMATCH


# ── CHASE_BLOCK ───────────────────────────────────────────────────────────────

def test_chase_block_when_moved_past_threshold_since_signal():
    df = _bars([10_000.0, 10_020.0, 10_040.0, 10_060.0, 10_080.0, 10_100.0])
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_100.0,
        signal_reference_price=10_000.0,  # +1.0% > 0.7% 임계
        minute_bars_result=_fresh_result(df),
    )
    assert result["approved"] is False
    assert result["block_code"] == confirm.CHASE_BLOCK


def test_no_chase_block_within_threshold():
    df = _bars([10_000.0, 10_010.0, 10_020.0, 10_030.0, 10_040.0, 10_050.0])
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_050.0,
        signal_reference_price=10_000.0,  # +0.5% < 0.7%
        minute_bars_result=_fresh_result(df),
    )
    assert result["block_code"] != confirm.CHASE_BLOCK


# ── ETF_EXTREME_BLOCK ─────────────────────────────────────────────────────────

def test_extreme_block_near_recent_high_for_up_direction():
    # 직전(현재 봉 제외) 3분 고점에 현재가가 못 미친 채 근접(추격) — 마지막 봉도
    # 여전히 소폭 상승(슬로프 UP 유지)이라 방향 불일치와 겹치지 않게 한다.
    df = _bars([9_800.0, 9_850.0, 9_900.0, 9_950.0, 9_990.0, 9_991.0])
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=9_991.0,
        minute_bars_result=_fresh_result(df),
    )
    assert result["approved"] is False
    assert result["block_code"] == confirm.ETF_EXTREME_BLOCK


def test_extreme_block_near_recent_low_for_down_direction():
    df = _bars([10_200.0, 10_150.0, 10_100.0, 10_050.0, 10_010.0, 10_009.0])
    result = confirm.confirm_etf_entry(
        symbol=INVERSE_SYMBOL, underlying_direction="DOWN", current_price=10_009.0,
        minute_bars_result=_fresh_result(df),
    )
    assert result["approved"] is False
    assert result["block_code"] == confirm.ETF_EXTREME_BLOCK


# ── 실제 배선(run_switch_or_entry) 통합 검증 ──────────────────────────────────

def test_run_switch_or_entry_blocks_fresh_entry_when_etf_data_insufficient(monkeypatch):
    """conftest의 기본 승인 스텁을 이 테스트에서만 재정의해 실제 배선을 검증한다 —
    ETF_DATA_INSUFFICIENT면 run_switch_or_entry가 신규진입 매수를 절대 시도하지
    않아야 한다."""
    import app.trading.hynix_switch_position_manager as position_manager_module
    from app.trading.hynix_switch_position_manager import run_switch_or_entry
    from app.models import OrderResult
    from app.services.hynix_switch_state import default_state

    def _blocked(*, symbol, underlying_direction, current_price, **kwargs):
        return {
            "symbol": symbol, "approved": False, "block_code": confirm.ETF_DATA_INSUFFICIENT,
            "reason": "테스트: ETF 데이터 없음", "source": None, "stale": True, "status": "unavailable",
            "last_bar_time": None, "using_genuine_etf_data": False, "vwap": None, "slope_direction": None,
            "moved_pct_since_signal": None, "recent_high": None, "recent_low": None,
        }

    monkeypatch.setattr(position_manager_module, "confirm_etf_entry", _blocked)

    class _Broker:
        def __init__(self):
            self.buy_calls = []

        def buy(self, symbol, name, quantity, price, order_type="limit"):
            self.buy_calls.append((symbol, quantity, price))
            return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                                side="buy", quantity=quantity, price=price, order_type=order_type, order_id="B1", message="ok")

        def get_buyable_cash(self):
            return 10_000_000.0

    broker = _Broker()
    state = default_state()
    now = datetime(2026, 7, 20, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 100_000.0, 5_000.0, now=now)

    assert result["acted"] is False
    assert result["failure_code"] == confirm.ETF_DATA_INSUFFICIENT
    assert broker.buy_calls == [], "ETF_DATA_INSUFFICIENT면 어떤 매수 주문도 브로커로 전송되면 안 된다"
    assert state["last_etf_entry_confirmation"]["block_code"] == confirm.ETF_DATA_INSUFFICIENT


def test_run_switch_or_entry_proceeds_when_etf_confirmation_approves(monkeypatch):
    import app.trading.hynix_switch_position_manager as position_manager_module
    from app.trading.hynix_switch_position_manager import run_switch_or_entry
    from app.models import OrderResult
    from app.services.hynix_switch_state import default_state

    def _approved(*, symbol, underlying_direction, current_price, **kwargs):
        return {
            "symbol": symbol, "approved": True, "block_code": None, "reason": "ok", "source": "kis",
            "stale": False, "status": "success", "last_bar_time": "x", "using_genuine_etf_data": True,
            "vwap": current_price, "slope_direction": underlying_direction, "moved_pct_since_signal": 0.1,
            "recent_high": None, "recent_low": None,
        }

    monkeypatch.setattr(position_manager_module, "confirm_etf_entry", _approved)

    class _Broker:
        def __init__(self):
            self.buy_calls = []

        def buy(self, symbol, name, quantity, price, order_type="limit"):
            self.buy_calls.append((symbol, quantity, price))
            return OrderResult(success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                                side="buy", quantity=quantity, price=price, order_type=order_type, order_id="B1", message="ok")

        def get_buyable_cash(self):
            return 10_000_000.0

    broker = _Broker()
    state = default_state()
    now = datetime(2026, 7, 20, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 100_000.0, 5_000.0, now=now)

    assert result["acted"] is True
    assert len(broker.buy_calls) == 1
    assert state["last_etf_entry_confirmation"]["approved"] is True


def test_full_confirmation_passes_when_all_conditions_clear():
    # 직전 고점/저점 패딩(high=p*1.001)을 명확히 넘어서는 신규 돌파 — 추격이
    # 아니라 정상 추세추종 진입이므로 어떤 조건에도 걸리지 않아야 한다.
    df = _bars([10_000.0, 10_005.0, 10_010.0, 10_015.0, 10_020.0, 10_100.0])
    result = confirm.confirm_etf_entry(
        symbol=LONG_SYMBOL, underlying_direction="UP", current_price=10_100.0,
        signal_reference_price=10_095.0, minute_bars_result=_fresh_result(df),
    )
    assert result["approved"] is True
    assert result["block_code"] is None
