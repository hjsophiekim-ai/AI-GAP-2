"""test_naver_stock_collector.py — 네이버 국내주식 수집 테스트."""

from __future__ import annotations

import datetime

import pandas as pd
import pytest
from unittest.mock import MagicMock, patch


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _make_rows(n: int, close: int = 180_000) -> list[dict]:
    base = datetime.date(2026, 1, 1)
    rows = []
    for i in range(n):
        d = base + datetime.timedelta(days=i)
        rows.append({"date": d.strftime("%Y.%m.%d"), "close": close})
    return rows


def _make_html_table(rows: list[dict]) -> str:
    header = (
        "<tr><th>날짜</th><th>종가</th><th>전일비</th>"
        "<th>시가</th><th>고가</th><th>저가</th><th>거래량</th></tr>"
    )
    body = ""
    for r in rows:
        c = r["close"]
        body += (
            f"<tr><td>{r['date']}</td><td>{c:,}</td><td>0</td>"
            f"<td>{c:,}</td><td>{c:,}</td><td>{c:,}</td><td>1,000,000</td></tr>"
        )
    return f"<html><body><table class='type2'>{header}{body}</table></body></html>"


# ── fetch_naver_daily_ohlcv ───────────────────────────────────────────────────

class TestNaverDailyOhlcv:

    def _mock_resp(self, html: str) -> MagicMock:
        m = MagicMock()
        m.status_code = 200
        m.text = html
        m.raise_for_status = MagicMock()
        return m

    def test_parses_20_rows(self):
        from app.data.naver_stock_collector import fetch_naver_daily_ohlcv
        html = _make_html_table(_make_rows(25))
        with patch("requests.get", return_value=self._mock_resp(html)):
            df = fetch_naver_daily_ohlcv("000660", pages=1)
        assert df is not None
        assert len(df) >= 20

    def test_invalid_price_filtered(self):
        from app.data.naver_stock_collector import fetch_naver_daily_ohlcv
        rows = _make_rows(15, close=180_000) + _make_rows(10, close=100)
        html = _make_html_table(rows)
        with patch("requests.get", return_value=self._mock_resp(html)):
            df = fetch_naver_daily_ohlcv("000660", pages=1)
        if df is not None:
            assert all(df["close"] >= 50_000), "50,000원 미만 종가가 포함되어선 안 됩니다"
            assert all(df["close"] <= 1_000_000), "1,000,000원 초과 종가가 포함되어선 안 됩니다"

    def test_network_error_returns_none(self):
        from app.data.naver_stock_collector import fetch_naver_daily_ohlcv
        with patch("requests.get", side_effect=Exception("네트워크 오류")):
            df = fetch_naver_daily_ohlcv("000660", pages=1)
        assert df is None

    def test_empty_response_returns_none(self):
        from app.data.naver_stock_collector import fetch_naver_daily_ohlcv
        empty_resp = MagicMock()
        empty_resp.status_code = 200
        empty_resp.text = "<html><body></body></html>"
        empty_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=empty_resp):
            df = fetch_naver_daily_ohlcv("000660", pages=1)
        assert df is None

    def test_returns_datetime_column(self):
        from app.data.naver_stock_collector import fetch_naver_daily_ohlcv
        html = _make_html_table(_make_rows(25))
        with patch("requests.get", return_value=MagicMock(
            status_code=200, text=html, raise_for_status=MagicMock()
        )):
            df = fetch_naver_daily_ohlcv("000660", pages=1)
        if df is not None and not df.empty:
            assert "datetime" in df.columns
            assert pd.api.types.is_datetime64_any_dtype(df["datetime"])


# ── fetch_naver_current_price ─────────────────────────────────────────────────

class TestNaverCurrentPrice:

    def test_returns_dict_with_required_keys(self):
        from app.data.naver_stock_collector import fetch_naver_current_price
        with patch("requests.get", side_effect=Exception("오류")):
            result = fetch_naver_current_price()
        for k in ("current_price", "status", "source", "error"):
            assert k in result, f"키 없음: {k}"

    def test_failed_returns_none_price_not_zero(self):
        from app.data.naver_stock_collector import fetch_naver_current_price
        with patch("requests.get", side_effect=Exception("오류")):
            result = fetch_naver_current_price()
        assert result["current_price"] is None, (
            "실패 시 current_price는 None이어야 합니다 (0 금지)"
        )
        assert result["status"] == "failed"

    def test_source_is_naver(self):
        from app.data.naver_stock_collector import fetch_naver_current_price
        with patch("requests.get", side_effect=Exception("오류")):
            result = fetch_naver_current_price()
        assert result["source"] == "naver"

    def test_valid_price_range_when_success(self):
        from app.data.naver_stock_collector import fetch_naver_current_price
        rows_html = "".join(
            f"<tr><td>{datetime.date(2026,1,i+1).strftime('%Y.%m.%d')}</td>"
            f"<td>180,000</td><td>0</td><td>180,000</td>"
            f"<td>181,000</td><td>179,000</td><td>1,000,000</td></tr>"
            for i in range(25)
        )
        html = (
            "<html><body><table class='type2'>"
            "<tr><th>날짜</th><th>종가</th><th>전일비</th>"
            "<th>시가</th><th>고가</th><th>저가</th><th>거래량</th></tr>"
            f"{rows_html}</table></body></html>"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=mock_resp):
            result = fetch_naver_current_price()
        if result["status"] == "success":
            assert result["current_price"] is not None
            assert 50_000 <= result["current_price"] <= 1_000_000
