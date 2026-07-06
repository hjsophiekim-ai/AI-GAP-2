from __future__ import annotations

import logging

import pandas as pd
import pytest

from app.data.market_data_validator import (
    DataValidationError,
    validate_hynix_current_sources,
    validate_prediction_prices,
)
from app.data_sources.auto_market_collector import _validate_real_candles
from app.features.micron_premarket_features import compute_micron_features
from app.ml.hynix_forecast_engine import run_forecast
from app.models.hynix_predictor import predict_hynix


def _micron_features() -> dict:
    return {
        "micron_premarket_return": 1.0,
        "micron_premarket_open_to_now": 1.0,
        "micron_premarket_high_to_now": -0.2,
        "micron_premarket_low_to_now": 1.2,
        "micron_premarket_30m_momentum": 0.5,
        "micron_premarket_60m_momentum": 0.7,
        "micron_premarket_vwap": 102.0,
        "micron_premarket_volume_change": 10.0,
        "micron_regular_return": None,
        "micron_aftermarket_return": None,
        "micron_session_strength_score": 65.0,
    }


def _daily(n: int = 30, price: float = 250_000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.date_range("2026-05-01", periods=n, freq="B"),
            "open": [price] * n,
            "high": [price * 1.01] * n,
            "low": [price * 0.99] * n,
            "close": [price] * n,
            "volume": [1_000_000] * n,
        }
    )


def _mu_intraday(n: int = 90, price: float = 100.0) -> pd.DataFrame:
    rows = []
    for i, ts in enumerate(pd.date_range("2026-06-26 08:00", periods=n, freq="1min")):
        close = price + i * 0.02
        rows.append(
            {
                "datetime": ts,
                "open": close - 0.01,
                "high": close + 0.03,
                "low": close - 0.03,
                "close": close,
                "volume": 1000 + i,
                "source": "test",
                "session": "premarket",
            }
        )
    return pd.DataFrame(rows)


def _mu_daily(n: int = 30, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.date_range("2026-05-01", periods=n, freq="B"),
            "open": [price + i * 0.1 for i in range(n)],
            "high": [price + i * 0.1 + 1.0 for i in range(n)],
            "low": [price + i * 0.1 - 1.0 for i in range(n)],
            "close": [price + i * 0.1 for i in range(n)],
            "volume": [1_000_000 + i for i in range(n)],
            "source": ["test"] * n,
        }
    )


def test_current_250k_rejects_980k_prediction_price():
    with pytest.raises(DataValidationError):
        validate_prediction_prices({"today_close_expected": 980_000}, 250_000)


def test_current_250k_prices_are_near_current_anchor():
    pred = predict_hynix(
        micron_features=_micron_features(),
        hynix_current_price=250_000,
        hynix_prev_close=248_000,
        sox_return_pct=0.5,
        nvda_return_pct=0.4,
    )
    for key in ("today_open_expected", "today_high_expected", "today_low_expected", "today_close_expected"):
        assert 250_000 * 0.85 <= pred[key] <= 250_000 * 1.15
    assert pred["base_price"] == pytest.approx(250_000)
    assert pred["base_price_source"] == "current_price"


def test_mu_quote_replicated_minute_data_is_synthetic_rejected():
    df = pd.DataFrame(
        {
            "datetime": pd.date_range("2026-06-26 09:30", periods=30, freq="1min"),
            "open": [100.0] * 30,
            "high": [100.0] * 30,
            "low": [100.0] * 30,
            "close": [100.0] * 30,
            "volume": [1000] * 30,
        }
    )
    ok, reason, clean = _validate_real_candles(df, "test")
    assert not ok
    assert "synthetic rejected" in reason
    assert clean is None


def test_mu_unavailable_does_not_create_fake_features():
    features = compute_micron_features(df_1min=None, current_price={"price": 100.0})
    assert all(value is None for value in features.values())


def test_hynix_current_sources_block_when_spread_exceeds_one_percent():
    ok, message, detail = validate_hynix_current_sources(
        {"KIS": 250_000, "naver": 250_500, "yfinance": 253_000}
    )
    assert not ok
    assert "spread" in message
    assert detail["max_diff_pct"] >= 1.0


def test_forecast_blocks_when_mu_daily_missing():
    market = {
        "mu": {
            "df_1min": _mu_intraday(),
            "df_3min": _mu_intraday().iloc[::3].reset_index(drop=True),
            "df_daily": None,
            "current_price": {"price": 101.8},
            "source": "yfinance",
        },
        "nvda": {"current_price": 120.0, "regular_return": 0.5, "source": "yfinance"},
        "index": {"qqq_return": 0.4, "sox_return": 0.5, "usdkrw_change": -0.1, "source": "yfinance"},
        "hynix": {
            "df_daily": _daily(),
            "prev_close": 250_000,
            "current_price": 251_000,
            "source": "naver",
            "stock_identity": {"code": "000660", "name": "SK하이닉스", "ok": True, "message": "ok"},
            "price_validation": {"ok": True, "message": "ok"},
        },
        "kospilab": {"hynix_reference_return": 0.2, "hynix_reference_price": None, "source_status": "success"},
        "errors": [],
    }
    result = run_forecast(market)
    assert result["status"] == "blocked"
    assert "MU daily" in result["message"]


def test_hynix_price_logs_are_emitted(caplog):
    market = {
        "mu": {
            "df_1min": _mu_intraday(),
            "df_3min": _mu_intraday().iloc[::3].reset_index(drop=True),
            "df_daily": _mu_daily(),
            "current_price": {"price": 101.8},
            "source": "yfinance",
            "minute_1m_status": "real candle success",
            "minute_3m_status": "real candle success",
            "daily_status": "real candle success",
        },
        "nvda": {"current_price": 120.0, "regular_return": 0.5, "source": "yfinance"},
        "index": {"qqq_return": 0.4, "sox_return": 0.5, "usdkrw_change": -0.1, "source": "yfinance"},
        "hynix": {
            "df_daily": _daily(),
            "prev_close": 250_000,
            "current_price": 251_000,
            "source": "naver",
            "source_detail": {"current_price": "naver", "daily_ohlcv": "naver"},
            "stock_identity": {"code": "000660", "name": "SK하이닉스", "ok": True, "message": "ok"},
            "price_validation": {
                "ok": True,
                "message": "ok",
                "source_prices": {"KIS": 251000, "naver": 251000, "yfinance": 251000},
                "selected_source": "KIS",
                "selected_price": 251000,
                "max_diff_pct": 0.0,
            },
        },
        "kospilab": {"hynix_reference_return": 0.2, "hynix_reference_price": None, "source_status": "success"},
        "errors": [],
    }
    with caplog.at_level(logging.WARNING, logger="hynix_prediction_debug"):
        run_forecast(market)
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "[PREDICT_BASE]" in log_text
    assert "current_price=251000" in log_text
    assert "prev_close=250000" in log_text
