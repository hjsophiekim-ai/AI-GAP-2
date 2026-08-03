"""Unit tests for app.trading.tsla_auto.market_data — fake fetchers only, never real KIS."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.tsla_auto import config, market_data
from app.trading.tsla_auto.market_data import MarketDataService, filter_complete_3m_bars
from app.trading.tsla_auto.signal_engine import resample_completed_3m

ET = config.ET


def _fake_bars_df(start: datetime, n_minutes: int) -> pd.DataFrame:
    rows = []
    for i in range(n_minutes):
        dt = start + timedelta(minutes=i)
        rows.append({"datetime": dt, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 10})
    return pd.DataFrame(rows)


def test_bootstrap_ok_when_enough_bars_present():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    df_1m = _fake_bars_df(start, 310)
    svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (df_1m, {}), fetch_quote=lambda *a: (None, None))
    result = svc.bootstrap(now=start + timedelta(minutes=320))
    assert result.ok is True
    assert result.completed_3m_count >= config.WARMUP_3M_BARS_MIN


def test_bootstrap_fails_on_no_data():
    svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (pd.DataFrame(), {}), fetch_quote=lambda *a: (None, None))
    result = svc.bootstrap(now=datetime(2026, 1, 6, 9, 30, tzinfo=ET))
    assert result.ok is False
    assert result.reason == "NO_1M_BARS"


def test_bootstrap_fails_when_too_few_bars():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    df_1m = _fake_bars_df(start, 10)
    svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (df_1m, {}), fetch_quote=lambda *a: (None, None))
    result = svc.bootstrap(now=start + timedelta(minutes=20))
    assert result.ok is False
    assert result.reason.startswith("WARMUP_1M_LT_")


def test_bootstrap_uses_prior_trading_day_cache_before_us_open():
    prior = datetime(2026, 7, 31, 9, 30, tzinfo=ET)
    prior_df = _fake_bars_df(prior, 390)
    market_data.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prior_df.to_csv(market_data.CACHE_DIR / "TSLA_20260731_1m.csv", index=False)

    empty_live = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (empty_live, {}), fetch_quote=lambda *a: (None, None))
    next_open = datetime(2026, 8, 3, 9, 30, tzinfo=ET)
    result = svc.bootstrap(now=next_open)

    assert result.ok is True
    assert result.prior_day_1m_bars == 390
    assert result.today_1m_bars == 0
    assert result.completed_3m_count >= config.WARMUP_3M_BARS_MIN
    assert svc.get_last_bootstrap_diag()["cached_warmup_count"] == 390


def test_merge_incremental_appends_and_dedups():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    base_df = _fake_bars_df(start, 310)
    svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (base_df, {}), fetch_quote=lambda *a: (None, None))
    svc.bootstrap(now=start + timedelta(minutes=320))

    new_minute = start + timedelta(minutes=310)
    live_df = pd.DataFrame([{"datetime": new_minute, "open": 105.0, "high": 105.0, "low": 105.0, "close": 105.0, "volume": 5}])
    svc._fetch_minute_candles = lambda *a: (live_df, {})
    merged = svc.merge_incremental_1m()
    assert (merged["datetime"] == new_minute).any()
    assert len(merged) == 311  # no duplicate re-count


def test_refresh_quotes_populates_symbols_with_age():
    quotes = {config.SIGNAL_SYMBOL: 250.0, config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0}
    svc = MarketDataService(mode="MOCK", fetch_quote=lambda mode, symbol: (quotes.get(symbol), None))
    svc.refresh_quotes()
    for symbol, price in quotes.items():
        snap = svc.get_quote(symbol)
        assert snap is not None
        assert snap.price == price
        assert snap.age_sec is not None and snap.age_sec >= 0.0
        assert snap.error is None


def test_get_quote_reports_error_without_raising():
    svc = MarketDataService(mode="MOCK", fetch_quote=lambda mode, symbol: (None, "FAKE_ERROR"))
    svc.refresh_quotes(symbols=(config.SIGNAL_SYMBOL,))
    snap = svc.get_quote(config.SIGNAL_SYMBOL)
    assert snap is not None
    assert snap.error == "FAKE_ERROR"
    assert snap.price == 0.0


def test_quote_updater_lifecycle():
    svc = MarketDataService(mode="MOCK", fetch_quote=lambda mode, symbol: (100.0, None))
    assert svc.quote_updater_alive() is False
    svc.start_quote_updater(interval_sec=0.01)
    assert svc.quote_updater_alive() is True
    svc.stop_quote_updater(join_timeout=1.0)
    assert svc.quote_updater_alive() is False


def test_filter_complete_3m_bars_drops_bin_missing_a_1m_bar():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    rows = []
    for i in range(30):
        if i == 13:  # drop the middle minute of the 09:39-09:41 bin
            continue
        rows.append({"datetime": start + timedelta(minutes=i), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 10})
    df_1m = pd.DataFrame(rows)
    now = start + timedelta(minutes=30)
    bars_3m = resample_completed_3m(df_1m, now=now)
    gapped_bar_start = start + timedelta(minutes=12)
    assert (bars_3m["datetime"] == gapped_bar_start).any()

    filtered, dropped = filter_complete_3m_bars(bars_3m, df_1m)
    assert dropped == [pd.Timestamp(gapped_bar_start)]
    assert not (filtered["datetime"] == gapped_bar_start).any()
    assert len(filtered) == len(bars_3m) - 1


def test_filter_complete_3m_bars_keeps_all_when_no_gap():
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    df_1m = _fake_bars_df(start, 30)
    now = start + timedelta(minutes=30)
    bars_3m = resample_completed_3m(df_1m, now=now)
    filtered, dropped = filter_complete_3m_bars(bars_3m, df_1m)
    assert dropped == []
    assert len(filtered) == len(bars_3m)
