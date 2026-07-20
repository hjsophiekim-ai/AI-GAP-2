"""
test_early_trend_detector_backtest.py — Adaptive-only vs Adaptive+Early 백테스트
비교 하네스 검증.

classify_raw_regime()의 STRONG_UP/DOWN 확정 조건은 정교한 다중 임계값 조합이라
합성 데이터로 특정 장세를 항상 재현하기 어렵다 — 이 테스트는 (1) 백테스트가
어떤 입력에도 예외 없이 필요한 스키마를 반환하는지, (2) 실제로 두 전략 모두
거래가 발생한 경우 지표들이 서로 일관된지를 검증한다(정확한 절대 수익률 값을
검증하지 않는다 — 근사 백테스트이기 때문).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.backtest.early_trend_detector_backtest import run_comparison_backtest
from app.trading.hynix_symbols import LONG_SYMBOL

_METRIC_KEYS = {
    "trade_count", "net_return_pct", "max_drawdown_pct", "avg_entry_delay_seconds",
    "false_signal_loss_pct", "total_trade_cost_pct", "profit_factor",
}


def _rising_bars(minutes: int = 90, start_price: float = 100_000.0, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 7, 20, 9, 5)
    rows = []
    price = start_price
    for i in range(minutes):
        price *= 1.0015  # 완만하고 꾸준한 상승
        vol = 1500.0 + (500.0 if i > minutes // 2 else 0.0)
        rows.append({
            "datetime": start + timedelta(minutes=i), "open": price / 1.0015, "high": price * 1.001,
            "low": price * 0.9992, "close": price, "volume": vol,
        })
    return pd.DataFrame(rows)


def _flat_noisy_bars(minutes: int = 40, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 7, 20, 9, 5)
    rows = []
    base = 100_000.0
    for i in range(minutes):
        wiggle = 20.0 if i % 2 == 0 else -20.0
        price = base + wiggle
        rows.append({
            "datetime": start + timedelta(minutes=i), "open": price, "high": price * 1.0005,
            "low": price * 0.9995, "close": price, "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def test_returns_empty_metrics_schema_when_insufficient_bars():
    df = _rising_bars(minutes=5)
    result = run_comparison_backtest(df, LONG_SYMBOL)
    assert set(result.keys()) == {"adaptive_only", "adaptive_plus_early"}
    for key in ("adaptive_only", "adaptive_plus_early"):
        assert _METRIC_KEYS == set(result[key].keys())
        assert result[key]["trade_count"] == 0


def test_returns_empty_metrics_schema_for_none_or_empty_dataframe():
    assert run_comparison_backtest(None, LONG_SYMBOL)["adaptive_only"]["trade_count"] == 0
    assert run_comparison_backtest(pd.DataFrame(), LONG_SYMBOL)["adaptive_only"]["trade_count"] == 0


def test_runs_end_to_end_without_error_on_rising_series():
    df = _rising_bars(minutes=90)
    result = run_comparison_backtest(df, LONG_SYMBOL)
    for key in ("adaptive_only", "adaptive_plus_early"):
        assert _METRIC_KEYS == set(result[key].keys())
        assert isinstance(result[key]["trade_count"], int)
        assert result[key]["trade_count"] >= 0


def test_runs_end_to_end_without_error_on_flat_noisy_series():
    df = _flat_noisy_bars(minutes=40)
    result = run_comparison_backtest(df, LONG_SYMBOL)
    for key in ("adaptive_only", "adaptive_plus_early"):
        assert _METRIC_KEYS == set(result[key].keys())


def test_profit_factor_and_costs_are_consistent_when_trades_exist():
    df = _rising_bars(minutes=120)
    result = run_comparison_backtest(df, LONG_SYMBOL)
    for key in ("adaptive_only", "adaptive_plus_early"):
        metrics = result[key]
        if metrics["trade_count"] > 0:
            assert metrics["total_trade_cost_pct"] >= 0.0
            if metrics["profit_factor"] is not None and metrics["profit_factor"] != float("inf"):
                assert metrics["profit_factor"] >= 0.0
