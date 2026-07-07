"""
test_micron_price_validation.py — MU(마이크론) 가격 수집·검증 테스트.

MU_PRICE_MAX/MU_PRICE_HARD_MAX는 20~2000/5000USD로 넓게 잡혀 있다 — 과거에는
500/1000을 상한으로 썼으나, 이 저장소가 다루는 시뮬레이션 시점(2026년)에는
MU 실제가가 이미 900~1000USD대에 도달해 있어 그 값을 "단위 오류"로 오판해
잘못 보정(예: 984.75 -> 98.475)하는 실제 버그가 있었다. 이 테스트는 그 넓어진
범위를 기준으로, 진짜 10배/100배 단위 오류만 보정 대상이 됨을 검증한다.
"""

from __future__ import annotations

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

from app.data.market_data_validator import (
    validate_mu_price,
    auto_fix_mu_price,
    parse_mu_price_str,
    MU_PRICE_MIN,
    MU_PRICE_MAX,
    MU_PRICE_HARD_MAX,
)


# ── parse_mu_price_str 테스트 ─────────────────────────────────────────────────

class TestParseMuPriceStr:
    """KIS API 응답 문자열 파싱 검증."""

    @pytest.mark.parametrize("raw,expected_range", [
        ("101.25",     (100, 110)),     # 정상 가격(저가대)
        ("985.50",     (980, 990)),     # 정상 가격(2026년 시뮬레이션 시점 실제가대) — 보정 없이 그대로
        ("10000.00",   (900, 1100)),    # 10배 단위 오류 의심 → /10 자동 보정
        ("98550.0",    (900, 1100)),    # 100배 단위 오류 의심 → /100 자동 보정
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

    def test_valid_no_change_at_current_real_price_level(self):
        # 2026년 시뮬레이션 시점의 실제 MU 가격대(900~1000USD대)는 보정 없이 그대로 통과해야 한다.
        assert auto_fix_mu_price(984.75) == 984.75

    def test_x10_fix(self):
        result = auto_fix_mu_price(10_000.0)
        assert result is not None
        assert 900 <= result <= 1100

    def test_x100_fix(self):
        result = auto_fix_mu_price(98_500.0)
        assert result is not None
        assert 900 <= result <= 1100

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

    def test_current_real_price_level_not_auto_fixed(self):
        """984.75달러(2026년 시뮬레이션 시점 실제가) → 보정 없이 그대로 통과해야 합니다."""
        from app.data_sources.kis_overseas_minute import fetch_mu_1min_bars

        item = self._make_output2_item("984.75")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output2": [item]}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            with patch("app.data_sources.kis_overseas_minute._get_access_token", return_value="token"):
                with patch("app.data_sources.kis_overseas_minute._load_credentials",
                           return_value={"app_key": "k", "app_secret": "s", "base_url": "http://test"}):
                    df = fetch_mu_1min_bars(mode="real")

        assert df is not None and not df.empty
        assert abs(df.iloc[0]["close"] - 984.75) < 0.01, (
            f"984.75 → 잘못 보정되어 {df.iloc[0]['close']:.3f}로 축소됨(회귀 버그)"
        )

    def test_high_price_auto_fixed(self):
        """10,000달러(10배 단위 오류 의심) → 자동 보정으로 1,000달러대가 되어야 합니다."""
        from app.data_sources.kis_overseas_minute import fetch_mu_1min_bars

        item = self._make_output2_item("10000.00")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output2": [item]}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            with patch("app.data_sources.kis_overseas_minute._get_access_token", return_value="token"):
                with patch("app.data_sources.kis_overseas_minute._load_credentials",
                           return_value={"app_key": "k", "app_secret": "s", "base_url": "http://test"}):
                    df = fetch_mu_1min_bars(mode="real")

        if df is not None and not df.empty:
            assert df.iloc[0]["close"] < MU_PRICE_MAX, (
                f"10000.00 → 보정 후 {df.iloc[0]['close']:.2f} ({MU_PRICE_MAX:.0f} 미만이어야 함)"
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
            assert all(MU_PRICE_MIN <= c <= MU_PRICE_MAX for c in df["close"]), (
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
        (20.0,    True),
        (100.0,   True),
        (985.50,  True),   # 2026년 시뮬레이션 시점 실제가대
        (1999.99, True),
        (2000.0,  True),
        (2000.01, False),
        (5000.0,  False),
        (5001.0,  False),
        (19.99,   False),
    ])
    def test_boundary(self, price, expected_ok):
        ok, _ = validate_mu_price(price)
        assert ok is expected_ok, f"price={price}: expected ok={expected_ok}, got {ok}"

    def test_boundary_uses_current_constants(self):
        # 상수 자체가 넓어진 값(2000/5000)인지 회귀 확인 — 과거 500/1000으로
        # 되돌아가면 984.75같은 정상가가 다시 잘못 보정되는 버그가 재발한다.
        assert MU_PRICE_MAX == 2000.0
        assert MU_PRICE_HARD_MAX == 5000.0
