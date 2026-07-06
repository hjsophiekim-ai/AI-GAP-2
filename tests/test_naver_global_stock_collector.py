"""test_naver_global_stock_collector.py — 해외주식 수집 테스트."""

from __future__ import annotations

import pytest
from unittest.mock import patch


class TestFetchNaverGlobalQuote:

    def test_returns_required_keys(self):
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        with patch("app.data.naver_global_stock_collector._fetch_from_naver_world", return_value=None):
            with patch("app.data.naver_global_stock_collector._fetch_from_yfinance", return_value=None):
                result = fetch_naver_global_quote("MU")
        for k in ("symbol", "price", "return_pct", "source", "status", "error"):
            assert k in result, f"키 없음: {k}"

    def test_failed_returns_none_not_zero(self):
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        with patch("app.data.naver_global_stock_collector._fetch_from_naver_world", return_value=None):
            with patch("app.data.naver_global_stock_collector._fetch_from_yfinance", return_value=None):
                result = fetch_naver_global_quote("MU")
        assert result["price"] is None, "실패 시 price는 None이어야 합니다 (0 금지)"
        assert result["status"] == "failed"

    def test_yfinance_fallback_when_naver_fails(self):
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        with patch("app.data.naver_global_stock_collector._fetch_from_naver_world", return_value=None):
            with patch("app.data.naver_global_stock_collector._fetch_from_yfinance",
                       return_value={"price": 110.0, "return_pct": 1.5}):
                result = fetch_naver_global_quote("MU")
        assert result["status"] == "success"
        assert result["source"] == "yfinance"
        assert result["price"] == 110.0

    def test_naver_success_returns_naver_source(self):
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        with patch("app.data.naver_global_stock_collector._fetch_from_naver_world",
                   return_value={"price": 109.5, "return_pct": -0.5}):
            result = fetch_naver_global_quote("MU")
        assert result["status"] == "success"
        assert result["source"] == "naver_global"
        assert result["price"] == 109.5

    def test_naver_preferred_over_yfinance(self):
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        with patch("app.data.naver_global_stock_collector._fetch_from_naver_world",
                   return_value={"price": 108.0, "return_pct": -1.0}):
            with patch("app.data.naver_global_stock_collector._fetch_from_yfinance",
                       return_value={"price": 111.0, "return_pct": 1.5}):
                result = fetch_naver_global_quote("MU")
        assert result["source"] == "naver_global"
        assert result["price"] == 108.0

    def test_unknown_symbol_skips_naver_uses_yfinance(self):
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        with patch("app.data.naver_global_stock_collector._fetch_from_yfinance",
                   return_value={"price": 200.0, "return_pct": 0.3}):
            result = fetch_naver_global_quote("AAPL")
        assert result["status"] == "success"
        assert result["source"] == "yfinance"

    def test_symbol_preserved_in_result(self):
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        with patch("app.data.naver_global_stock_collector._fetch_from_naver_world", return_value=None):
            with patch("app.data.naver_global_stock_collector._fetch_from_yfinance", return_value=None):
                result = fetch_naver_global_quote("NVDA")
        assert result["symbol"] == "NVDA"

    def test_error_message_when_all_fail(self):
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        with patch("app.data.naver_global_stock_collector._fetch_from_naver_world", return_value=None):
            with patch("app.data.naver_global_stock_collector._fetch_from_yfinance", return_value=None):
                result = fetch_naver_global_quote("MU")
        assert result["error"] is not None
        assert len(result["error"]) > 0
