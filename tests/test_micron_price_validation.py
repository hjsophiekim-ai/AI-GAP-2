"""
test_micron_price_validation.py — MU(마이크론) 가격 수집·검증 테스트.

MU 가격이 1,000USD 이상이면 소수점/환산 오류로 처리하고
auto_fix 또는 None 반환을 검증합니다.
"""

from __future__ import annotations

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

from app.data.market_data_validator import (
    validate_mu_price,
    auto_fix_mu_price,
    parse_mu_price_str,
)


# ── parse_mu_price_str 테스트 ─────────────────────────────────────────────────

class TestParseMuPriceStr:
    """KIS API 응답 문자열 파싱 검증."""

    @pytest.mark.parametrize("raw,expected_range", [
        ("101.25",    (100, 110)),   # 정상 가격
        ("110.00",    (100, 120)),   # 정상 가격
        ("1,100.00",  (100, 120)),   # 콤마 포함 → /10 자동 보정
        ("10950.0",   (100, 120)),   # /100 자동 보정
    ])
    def test_normal_prices(self, raw, expected_range):
        result = parse_mu_price_str(raw)
        assert result is not None, f"raw={raw!r} → None이 되어선 안 됩니다"
        lo, hi = expected_range
        assert lo <= result <= hi, f"raw={raw!r} → {result} (expected {lo}~{hi})"

    @pytest.mark.parametrize("raw", [
        None, "", "0", "0.0", "N/A", "—", "abc",
    ])
    def test_invalid_strings(self, raw):
        assert parse_mu_price_str(raw) is None, f"raw={raw!r}는 None이어야 합니다"

    def test_unrecoverable_high_price(self):
        assert parse_mu_price_str("999999") is None

    def test_below_min_price(self):
        # 10달러 → auto_fix 불가 (10/10=1, 10/100=0.1)
        assert parse_mu_price_str("10") is None

    def test_integer_input(self):
        result = parse_mu_price_str(110)
        assert result == 110.0


# ── auto_fix_mu_price 테스트 ─────────────────────────────────────────────────

class TestAutoFixMuPrice:
    def test_valid_no_change(self):
        assert auto_fix_mu_price(110.0) == 110.0

    def test_x10_fix(self):
        result = auto_fix_mu_price(1100.0)
        assert result is not None
        assert 100 <= result <= 120

    def test_x100_fix(self):
        result = auto_fix_mu_price(11000.0)
        assert result is not None
        assert 100 <= result <= 120

    def test_none_returns_none(self):
        assert auto_fix_mu_price(None) is None

    def test_unrecoverable(self):
        assert auto_fix_mu_price(1_000_000.0) is None

    def test_below_min_after_fix(self):
        # 1.0 → /10=0.1 → /100=0.01 → 모두 범위 밖
        assert auto_fix_mu_price(1.0) is None


# ── KIS 파싱 통합 테스트 (mocked) ────────────────────────────────────────────

class TestKisMinuteParsing:
    """kis_overseas_minute.py 가격 파싱 수정 사항 검증."""

    def _make_output2_item(self, last: str, open_: str = None, high: str = None, low: str = None) -> dict:
        return {
            "kymd": "20260622",
            "khms": "220000",
            "last": last,
            "open": open_ or last,
            "high": high or last,
            "low":  low or last,
            "evol": "100000",
        }

    def test_normal_price_parsed(self):
        from app.data_sources.kis_overseas_minute import fetch_mu_1min_bars

        item = self._make_output2_item("101.25")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output2": [item]}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            with patch("app.data_sources.kis_overseas_minute._get_access_token", return_value="token"):
                with patch("app.data_sources.kis_overseas_minute._load_credentials",
                           return_value={"app_key": "k", "app_secret": "s", "base_url": "http://test"}):
                    df = fetch_mu_1min_bars(mode="real")

        assert df is not None
        assert not df.empty
        assert 90 <= df.iloc[0]["close"] <= 120

    def test_high_price_auto_fixed(self):
        """1,100달러 → 자동 보정으로 110달러가 되어야 합니다."""
        from app.data_sources.kis_overseas_minute import fetch_mu_1min_bars

        item = self._make_output2_item("1100.00")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output2": [item]}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            with patch("app.data_sources.kis_overseas_minute._get_access_token", return_value="token"):
                with patch("app.data_sources.kis_overseas_minute._load_credentials",
                           return_value={"app_key": "k", "app_secret": "s", "base_url": "http://test"}):
                    df = fetch_mu_1min_bars(mode="real")

        if df is not None and not df.empty:
            assert df.iloc[0]["close"] < 500, (
                f"1100.00 → 보정 후 {df.iloc[0]['close']:.2f} (500 미만이어야 함)"
            )

    def test_invalid_price_skipped(self):
        """완전히 잘못된 가격(범위 밖)은 건너뜀."""
        from app.data_sources.kis_overseas_minute import fetch_mu_1min_bars

        item_bad  = self._make_output2_item("999999")
        item_good = {**self._make_output2_item("110.0"), "khms": "220100"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output2": [item_bad, item_good]}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            with patch("app.data_sources.kis_overseas_minute._get_access_token", return_value="token"):
                with patch("app.data_sources.kis_overseas_minute._load_credentials",
                           return_value={"app_key": "k", "app_secret": "s", "base_url": "http://test"}):
                    df = fetch_mu_1min_bars(mode="real")

        if df is not None:
            # 잘못된 봉은 제외되고 정상 봉만 남아야 함
            assert all(20 <= c <= 500 for c in df["close"]), (
                f"비정상 가격 포함: {df['close'].tolist()}"
            )

    def test_empty_output2_returns_none(self):
        from app.data_sources.kis_overseas_minute import fetch_mu_1min_bars

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output2": []}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            with patch("app.data_sources.kis_overseas_minute._get_access_token", return_value="token"):
                with patch("app.data_sources.kis_overseas_minute._load_credentials",
                           return_value={"app_key": "k", "app_secret": "s", "base_url": "http://test"}):
                    df = fetch_mu_1min_bars(mode="real")

        assert df is None


# ── validate_mu_price 경계값 테스트 ──────────────────────────────────────────

class TestValidateMuPriceBoundary:
    @pytest.mark.parametrize("price,expected_ok", [
        (20.0,   True),
        (100.0,  True),
        (499.99, True),
        (500.0,  True),
        (500.01, False),
        (1000.0, False),
        (1001.0, False),
        (19.99,  False),
    ])
    def test_boundary(self, price, expected_ok):
        ok, _ = validate_mu_price(price)
        assert ok is expected_ok, f"price={price}: expected ok={expected_ok}, got {ok}"
