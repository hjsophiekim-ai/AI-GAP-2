"""
tests/test_kis_token.py

KIS 토큰 발급 관련 단위 테스트.
실제 API 호출 없이 unittest.mock으로 검증.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.config import _parse_account_no
from app.trading.kis_client import (
    BASE_URL_MOCK,
    BASE_URL_REAL,
    KISClient,
    KISTokenError,
)


# ─────────────────────────────────────────────────────────────────────────────
# TestAccountNumberParsing
# ─────────────────────────────────────────────────────────────────────────────

class TestAccountNumberParsing:
    def test_hyphen_format(self):
        cano, prdt = _parse_account_no("64282746-01")
        assert cano == "64282746"
        assert prdt == "01"

    def test_10digit_format(self):
        cano, prdt = _parse_account_no("6428274601")
        assert cano == "64282746"
        assert prdt == "01"

    def test_8digit_format(self):
        cano, prdt = _parse_account_no("64282746")
        assert cano == "64282746"
        assert prdt == "01"

    def test_hyphen_with_explicit_product_code_ignored_if_parsed(self):
        # "64282746-02" — 하이픈 이후 값을 product_code로 사용
        cano, prdt = _parse_account_no("64282746-02")
        assert cano == "64282746"
        assert prdt == "02"

    def test_external_product_code_applied_to_8digit(self):
        # 8자리 + 외부 product_code 주입
        cano, prdt = _parse_account_no("64282746", product_code="02")
        assert cano == "64282746"
        assert prdt == "02"

    def test_mock_cano_env_priority(self):
        """KIS_MOCK_CANO가 있으면 KIS_MOCK_ACCOUNT_NO보다 우선."""
        env = {
            "KIS_MOCK_APP_KEY": "fake_key",
            "KIS_MOCK_APP_SECRET": "fake_secret",
            "KIS_MOCK_CANO": "99887766",
            "KIS_MOCK_ACNT_PRDT_CD": "03",
            "KIS_MOCK_ACCOUNT_NO": "11223344-01",  # 이것보다 CANO 우선
            "KIS_MOCK_ACCOUNT_PRODUCT_CODE": "",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.config import get_kis_account_config
            cfg = get_kis_account_config("mock")
        assert cfg["account_no"] == "99887766"
        assert cfg["product_code"] == "03"
        assert cfg["cano_source"] == "CANO_env"


# ─────────────────────────────────────────────────────────────────────────────
# TestKISTokenErrorAttributes
# ─────────────────────────────────────────────────────────────────────────────

def _make_client(mode: str = "mock") -> KISClient:
    return KISClient(
        app_key="test_key",
        app_secret="test_secret",
        account_no="12345678",
        product_code="01",
        mode=mode,
    )


def _mock_resp(status: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = (200 <= status < 300)
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class TestKISTokenErrorAttributes:
    def test_403_raises_with_attributes(self):
        client = _make_client("mock")
        body = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "앱키 오류"}
        with patch.object(client._session, "post", return_value=_mock_resp(403, body)):
            with pytest.raises(KISTokenError) as exc_info:
                client.get_access_token()
        err = exc_info.value
        assert err.http_status == 403
        assert err.rt_cd == "1"
        assert err.msg_cd == "EGW00123"
        assert "앱키 오류" in err.msg1
        assert BASE_URL_MOCK in err.base_url_used

    def test_500_raises_with_attributes(self):
        client = _make_client("mock")
        body = {"rt_cd": "9", "msg_cd": "SRVERR", "msg1": "서버 오류"}
        with patch.object(client._session, "post", return_value=_mock_resp(500, body)):
            with pytest.raises(KISTokenError) as exc_info:
                client.get_access_token()
        err = exc_info.value
        assert err.http_status == 500
        assert err.msg_cd == "SRVERR"

    def test_200_no_token_raises(self):
        client = _make_client("mock")
        body = {"rt_cd": "0", "msg_cd": "OK", "msg1": "정상", "access_token": ""}
        with patch.object(client._session, "post", return_value=_mock_resp(200, body)):
            with pytest.raises(KISTokenError) as exc_info:
                client.get_access_token()
        err = exc_info.value
        assert err.http_status == 200

    def test_200_with_token_succeeds(self):
        client = _make_client("mock")
        body = {
            "rt_cd": "0",
            "msg_cd": "OK",
            "msg1": "정상",
            "access_token": "eyJhbGci...token",
            "expires_in": 86400,
        }
        with patch.object(client._session, "post", return_value=_mock_resp(200, body)):
            with patch.object(client, "_save_token_cache"):
                token = client.get_access_token()
        assert token == "eyJhbGci...token"


# ─────────────────────────────────────────────────────────────────────────────
# TestModeSeparation
# ─────────────────────────────────────────────────────────────────────────────

class TestModeSeparation:
    def test_mock_uses_mock_base_url(self):
        client = _make_client("mock")
        assert client.base_url == BASE_URL_MOCK
        assert "openapivts" in client.base_url

    def test_real_uses_real_base_url(self):
        client = _make_client("real")
        assert client.base_url == BASE_URL_REAL
        assert "openapi.koreainvestment" in client.base_url
        assert "vts" not in client.base_url

    def test_mock_token_request_hits_mock_url(self):
        client = _make_client("mock")
        body = {
            "rt_cd": "0", "msg_cd": "OK", "msg1": "정상",
            "access_token": "tok", "expires_in": 86400,
        }
        with patch.object(client._session, "post", return_value=_mock_resp(200, body)) as mock_post:
            with patch.object(client, "_save_token_cache"):
                client.get_access_token()
        called_url = mock_post.call_args[0][0]
        assert BASE_URL_MOCK in called_url

    def test_real_token_request_hits_real_url(self):
        client = _make_client("real")
        body = {
            "rt_cd": "0", "msg_cd": "OK", "msg1": "정상",
            "access_token": "tok", "expires_in": 86400,
        }
        with patch.object(client._session, "post", return_value=_mock_resp(200, body)) as mock_post:
            with patch.object(client, "_save_token_cache"):
                client.get_access_token()
        called_url = mock_post.call_args[0][0]
        assert BASE_URL_REAL in called_url
