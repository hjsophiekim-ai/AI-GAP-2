"""
test_market_data_validator.py — market_data_validator.py 단위 테스트.
"""

from __future__ import annotations

import pytest
import pandas as pd

from app.data.market_data_validator import (
    validate_mu_price,
    validate_hynix_price,
    validate_price_zones,
    validate_hynix_dataframe,
    validate_swing_result,
    auto_fix_mu_price,
    parse_mu_price_str,
    MU_PRICE_MIN,
    MU_PRICE_MAX,
    MU_PRICE_HARD_MAX,
    HYNIX_PRICE_MIN,
    HYNIX_PRICE_MAX,
)


# ── validate_mu_price ─────────────────────────────────────────────────────────

class TestValidateMuPrice:
    def test_valid_range(self):
        ok, msg = validate_mu_price(105.0)
        assert ok is True
        assert msg == "ok"

    def test_min_boundary(self):
        ok, _ = validate_mu_price(MU_PRICE_MIN)
        assert ok is True

    def test_max_boundary(self):
        ok, _ = validate_mu_price(MU_PRICE_MAX)
        assert ok is True

    def test_hard_max_blocked(self):
        ok, msg = validate_mu_price(MU_PRICE_HARD_MAX + 1)
        assert ok is False
        assert "소수점" in msg or "환산" in msg

    def test_above_500_invalid(self):
        ok, msg = validate_mu_price(501.0)
        assert ok is False

    def test_below_20_invalid(self):
        ok, msg = validate_mu_price(10.0)
        assert ok is False
        assert "저가" in msg

    def test_none_invalid(self):
        ok, msg = validate_mu_price(None)
        assert ok is False
        assert "없음" in msg


# ── auto_fix_mu_price ─────────────────────────────────────────────────────────

class TestAutoFixMuPrice:
    def test_already_valid(self):
        assert auto_fix_mu_price(110.0) == 110.0

    def test_divide_by_10(self):
        result = auto_fix_mu_price(1100.0)
        assert result is not None
        assert MU_PRICE_MIN <= result <= MU_PRICE_MAX

    def test_divide_by_100(self):
        result = auto_fix_mu_price(11000.0)
        assert result is not None
        assert MU_PRICE_MIN <= result <= MU_PRICE_MAX

    def test_unrecoverable_returns_none(self):
        assert auto_fix_mu_price(999_999.0) is None

    def test_none_returns_none(self):
        assert auto_fix_mu_price(None) is None


# ── parse_mu_price_str ────────────────────────────────────────────────────────

class TestParseMuPriceStr:
    def test_plain_string(self):
        result = parse_mu_price_str("105.50")
        assert result is not None
        assert 100 <= result <= 110

    def test_comma_in_string(self):
        result = parse_mu_price_str("1,100.50")
        # 1100.50 / 10 = 110.05 → 유효 범위
        assert result is not None
        assert MU_PRICE_MIN <= result <= MU_PRICE_MAX

    def test_none_input(self):
        assert parse_mu_price_str(None) is None

    def test_zero_string(self):
        assert parse_mu_price_str("0") is None

    def test_empty_string(self):
        assert parse_mu_price_str("") is None

    def test_invalid_string(self):
        assert parse_mu_price_str("N/A") is None

    def test_integer_input(self):
        result = parse_mu_price_str(110)
        assert result == 110.0


# ── validate_hynix_price ─────────────────────────────────────────────────────

class TestValidateHynixPrice:
    def test_valid(self):
        ok, msg = validate_hynix_price(180_000)
        assert ok is True

    def test_below_min(self):
        ok, msg = validate_hynix_price(HYNIX_PRICE_MIN - 1)
        assert ok is False
        assert "저가" in msg

    def test_above_max(self):
        ok, msg = validate_hynix_price(HYNIX_PRICE_MAX + 1)
        assert ok is False
        assert "고가" in msg

    def test_none_invalid(self):
        ok, _ = validate_hynix_price(None)
        assert ok is False


# ── validate_hynix_dataframe ──────────────────────────────────────────────────

class TestValidateHynixDataframe:
    def _make_df(self, n: int, close: float = 180_000) -> pd.DataFrame:
        import numpy as np
        return pd.DataFrame({
            "datetime": pd.date_range("2026-01-01", periods=n, freq="D"),
            "close":    [close] * n,
        })

    def test_valid_20_rows(self):
        df = self._make_df(25)
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is True
        assert len(result) >= 20

    def test_too_few_rows(self):
        df = self._make_df(10)
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is False
        assert "20개" in msg

    def test_invalid_price_filtered(self):
        df = self._make_df(25)
        df.loc[0:4, "close"] = 100  # 5개 비정상 (100원)
        ok, msg, result = validate_hynix_dataframe(df)
        assert len(result) == 20

    def test_all_invalid_prices(self):
        df = self._make_df(25, close=100)  # 100원 → 모두 비정상
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is False

    def test_none_input(self):
        ok, msg, result = validate_hynix_dataframe(None)
        assert ok is False

    def test_empty_df(self):
        ok, msg, result = validate_hynix_dataframe(pd.DataFrame())
        assert ok is False


# ── validate_price_zones ──────────────────────────────────────────────────────

class TestValidatePriceZones:
    def test_valid_zones(self):
        ok, msg = validate_price_zones(200_000, 180_000)
        assert ok is True

    def test_stop_loss_equals_target(self):
        ok, msg = validate_price_zones(200_000, 200_000)
        assert ok is False
        assert "≥" in msg

    def test_stop_loss_above_target(self):
        ok, msg = validate_price_zones(180_000, 200_000)
        assert ok is False

    def test_none_values_ok(self):
        ok, msg = validate_price_zones(None, None)
        assert ok is True


# ── validate_swing_result ─────────────────────────────────────────────────────

class TestValidateSwingResult:
    def test_valid(self):
        swing = {"target_price": 200_000, "stop_loss_price": 180_000}
        ok, msg = validate_swing_result(swing)
        assert ok is True

    def test_invalid(self):
        swing = {"target_price": 314_000, "stop_loss_price": 338_000}
        ok, msg = validate_swing_result(swing)
        assert ok is False
        assert "338" in msg or "≥" in msg
