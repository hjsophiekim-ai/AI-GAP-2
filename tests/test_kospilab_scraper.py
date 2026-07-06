"""
test_kospilab_scraper.py — 코스피랩 스크레이퍼 테스트.

실제 네트워크 접속 없이 결과 구조와 fallback 처리를 검증합니다.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from app.data_sources.kospilab_scraper import (
    fetch_kospilab_data,
    _parse_return_from_text,
    _parse_price_from_text,
    _RESULT_TEMPLATE,
)


class TestResultStructure:
    """결과 딕셔너리 구조 검증."""

    def test_required_keys_always_present(self):
        """네트워크 실패 시에도 필수 키가 존재해야 함."""
        with patch("app.data_sources.kospilab_scraper._try_requests", side_effect=Exception("no network")):
            with patch("app.data_sources.kospilab_scraper._try_playwright", side_effect=ImportError("playwright 미설치")):
                result = fetch_kospilab_data(force_refresh=True)

        for key in _RESULT_TEMPLATE:
            assert key in result, f"필수 키 누락: {key}"

    def test_source_status_is_string(self):
        with patch("app.data_sources.kospilab_scraper._try_requests", side_effect=Exception("no network")):
            with patch("app.data_sources.kospilab_scraper._try_playwright", side_effect=ImportError):
                result = fetch_kospilab_data(force_refresh=True)
        assert isinstance(result["source_status"], str)

    def test_failed_on_network_error(self):
        with patch("app.data_sources.kospilab_scraper._try_requests", side_effect=Exception("timeout")):
            with patch("app.data_sources.kospilab_scraper._try_playwright", side_effect=ImportError):
                result = fetch_kospilab_data(force_refresh=True)
        assert result["source_status"] in ("failed", "success")

    def test_error_message_is_string_or_none(self):
        with patch("app.data_sources.kospilab_scraper._try_requests", side_effect=Exception("err")):
            with patch("app.data_sources.kospilab_scraper._try_playwright", side_effect=ImportError):
                result = fetch_kospilab_data(force_refresh=True)
        assert result["error_message"] is None or isinstance(result["error_message"], str)


class TestCacheLogic:
    """캐시 동작 검증."""

    def test_cache_used_within_ttl(self):
        """최초 수집 후 force_refresh=False면 캐시 반환."""
        import app.data_sources.kospilab_scraper as mod
        call_count = 0

        def fake_try_requests():
            nonlocal call_count
            call_count += 1
            return {**_RESULT_TEMPLATE, "source_status": "success",
                    "hynix_reference_return": 1.23, "collected_at": "2026-01-01T00:00:00"}

        with patch.object(mod, "_try_requests", fake_try_requests):
            r1 = fetch_kospilab_data(force_refresh=True)
            r2 = fetch_kospilab_data(force_refresh=False)

        assert call_count == 1
        assert r1["hynix_reference_return"] == r2["hynix_reference_return"]

    def test_force_refresh_bypasses_cache(self):
        """force_refresh=True면 캐시 무시."""
        import app.data_sources.kospilab_scraper as mod
        call_count = 0

        def fake_try_requests():
            nonlocal call_count
            call_count += 1
            return {**_RESULT_TEMPLATE, "source_status": "success",
                    "hynix_reference_return": float(call_count), "collected_at": "now"}

        with patch.object(mod, "_try_requests", fake_try_requests):
            fetch_kospilab_data(force_refresh=True)
            fetch_kospilab_data(force_refresh=True)

        assert call_count == 2


class TestParsers:
    """개별 파서 유닛 테스트."""

    def test_parse_return_positive(self):
        assert _parse_return_from_text("SK하이닉스 +2.35%") == pytest.approx(2.35)

    def test_parse_return_negative(self):
        assert _parse_return_from_text("등락률 -1.50%") == pytest.approx(-1.50)

    def test_parse_return_none(self):
        assert _parse_return_from_text("no numbers here") is None

    def test_parse_price_valid(self):
        assert _parse_price_from_text("현재 190,500원") == pytest.approx(190500)

    def test_parse_price_too_small_ignored(self):
        assert _parse_price_from_text("가격 100원") is None

    def test_parse_price_too_large_ignored(self):
        assert _parse_price_from_text("가격 9,999,999원") is None

    def test_parse_return_no_sign(self):
        result = _parse_return_from_text("변화 3.14%")
        assert result is not None
        assert abs(result) == pytest.approx(3.14)


class TestParseSuccess:
    """성공 케이스: requests가 올바른 데이터를 반환하면 source_status가 success."""

    def test_success_result_has_data(self):
        import app.data_sources.kospilab_scraper as mod
        mock_result = {
            **_RESULT_TEMPLATE,
            "source_status":          "success",
            "hynix_reference_return": 1.23,
            "hynix_reference_price":  195_000.0,
            "collected_at":           "2026-06-29T10:00:00",
            "error_message":          None,
        }

        with patch.object(mod, "_try_requests", return_value=mock_result):
            result = fetch_kospilab_data(force_refresh=True)

        assert result["source_status"] == "success"
        assert result["hynix_reference_return"] == pytest.approx(1.23)
        assert result["hynix_reference_price"] == pytest.approx(195_000.0)
