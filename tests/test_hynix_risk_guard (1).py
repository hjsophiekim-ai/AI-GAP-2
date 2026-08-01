"""
test_hynix_risk_guard.py — 리스크 가드 모듈 테스트.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.hynix_risk_guard import check_risk_guards


def _sources(price: float) -> dict:
    return {"KIS": price, "naver": price, "yfinance": price}


class TestPriceErrorGuard:
    def test_extreme_price_move_blocks_all(self):
        result = check_risk_guards(
            prev_close=100_000.0, current_price=140_000.0,
            source_prices=_sources(140_000.0),
            minute_bar_timestamp=datetime.now(), now=datetime.now(),
        )
        assert result["blocks_buy"] is True
        assert result["blocks_sell"] is True

    def test_normal_move_does_not_block(self):
        result = check_risk_guards(
            prev_close=100_000.0, current_price=101_000.0,
            source_prices=_sources(101_000.0),
            minute_bar_timestamp=datetime.now(), now=datetime.now(),
        )
        assert result["blocks_buy"] is False
        assert result["blocks_sell"] is False


class TestSourceDivergenceGuard:
    def test_divergent_sources_block_all(self):
        result = check_risk_guards(
            prev_close=100_000.0, current_price=100_000.0,
            source_prices={"KIS": 100_000.0, "naver": 103_000.0, "yfinance": 100_500.0},
            minute_bar_timestamp=datetime.now(), now=datetime.now(),
        )
        assert result["blocks_buy"] is True
        assert result["blocks_sell"] is True


class TestMinuteStaleGuard:
    def test_stale_minute_blocks_all(self):
        now = datetime.now()
        result = check_risk_guards(
            prev_close=100_000.0, current_price=100_500.0,
            source_prices=_sources(100_500.0),
            minute_bar_timestamp=now - timedelta(minutes=15), now=now,
        )
        assert result["blocks_buy"] is True
        assert result["blocks_sell"] is True

    def test_missing_minute_timestamp_blocks_all(self):
        result = check_risk_guards(
            prev_close=100_000.0, current_price=100_500.0,
            source_prices=_sources(100_500.0),
            minute_bar_timestamp=None, now=datetime.now(),
        )
        assert result["blocks_buy"] is True
        assert result["blocks_sell"] is True


class TestDailyLossGuard:
    def test_daily_loss_blocks_buy_only(self):
        now = datetime.now()
        result = check_risk_guards(
            prev_close=100_000.0, current_price=100_500.0,
            source_prices=_sources(100_500.0),
            minute_bar_timestamp=now, now=now,
            total_equity=100_000_000.0, daily_pnl_pct=-4.0,
        )
        assert result["blocks_buy"] is True
        assert result["blocks_sell"] is False

    def test_no_daily_loss_does_not_block(self):
        now = datetime.now()
        result = check_risk_guards(
            prev_close=100_000.0, current_price=100_500.0,
            source_prices=_sources(100_500.0),
            minute_bar_timestamp=now, now=now,
            total_equity=100_000_000.0, daily_pnl_pct=-1.0,
        )
        assert result["blocks_buy"] is False
        assert result["blocks_sell"] is False


class TestMissingPrice:
    def test_missing_current_price_blocks_all(self):
        result = check_risk_guards(
            prev_close=100_000.0, current_price=None,
            source_prices={}, minute_bar_timestamp=datetime.now(),
        )
        assert result["blocks_buy"] is True
        assert result["blocks_sell"] is True
