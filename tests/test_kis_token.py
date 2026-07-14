"""
tests/test_kis_token.py

KIS 토큰 발급 관련 단위 테스트.
실제 API 호출 없이 unittest.mock으로 검증.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.config import _parse_account_no
from app.config import Config
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

    def test_mock_account_no_env_priority(self):
        """KIS_MOCK_ACCOUNT_NO가 KIS_MOCK_CANO보다 우선."""
        env = {
            "KIS_MOCK_APP_KEY": "fake_key",
            "KIS_MOCK_APP_SECRET": "fake_secret",
            "KIS_MOCK_CANO": "99887766",
            "KIS_MOCK_ACNT_PRDT_CD": "03",
            "KIS_MOCK_ACCOUNT_NO": "11223344-01",
            "KIS_MOCK_ACCOUNT_PRODUCT_CODE": "",
        }
        with patch.dict(os.environ, env, clear=False):
            from app.config import get_kis_account_config
            cfg = get_kis_account_config("mock")
        assert cfg["account_no"] == "11223344"
        assert cfg["product_code"] == "01"
        assert cfg["cano_source"] == "KIS_MOCK_ACCOUNT_NO"
        assert cfg["account_conflict"] is True


class TestEnhancedRealGate:
    def _set_real_env(self, monkeypatch, confirm="I_UNDERSTAND_REAL_TRADING_RISK"):
        values = {
            "ENABLE_FULL_AUTO": "true",
            "ENABLE_REAL_TRADING": "true",
            "ENABLE_REAL_BUY": "true",
            "ENABLE_REAL_SELL": "true",
            "FULL_AUTO_REAL_CONFIRM_TEXT": confirm,
            "KIS_REAL_APP_KEY": "real_key",
            "KIS_REAL_APP_SECRET": "real_secret",
            "KIS_REAL_ACCOUNT_NO": "12345678-01",
            "KIS_REAL_ACCOUNT_PRODUCT_CODE": "01",
        }
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        for key in ("KIS_REAL_CANO", "KIS_REAL_ACNT_PRDT_CD", "KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRODUCT_CODE"):
            monkeypatch.delenv(key, raising=False)

    def _cfg(self):
        cfg = Config()
        cfg._raw["safety"]["enable_real_trading"] = True
        cfg._raw["safety"]["require_real_confirm"] = True
        cfg._raw["safety"]["require_real_order_confirm_text"] = True
        cfg._raw["safety"]["real_confirm_text"] = "I_UNDERSTAND_REAL_TRADING_RISK"
        cfg._raw["safety"]["real_order_confirm_text"] = "I_UNDERSTAND_REAL_TRADING_RISK"
        cfg._raw["safety"]["real_trading_start_date"] = "2000-01-01"
        cfg._raw["kis"]["real"]["account_no_env"] = "KIS_REAL_ACCOUNT_NO"
        cfg._raw["kis"]["real"]["product_code_env"] = "KIS_REAL_ACCOUNT_PRODUCT_CODE"
        cfg._raw["kis"]["real"]["account_product_code_env"] = "KIS_REAL_ACCOUNT_PRODUCT_CODE"
        return cfg

    def test_all_required_real_gate_conditions_pass(self, monkeypatch):
        self._set_real_env(monkeypatch)
        status = self._cfg().enhanced_real_gate_status(current_mode="real")
        assert status["ready"] is True
        assert status["blocking_reasons"] == []
        assert status["checks"]["enable_full_auto"] is True
        assert status["checks"]["config_enable_real_trading"] is True
        assert status["checks"]["env_enable_real_trading"] is True
        assert status["checks"]["enable_real_buy"] is True
        assert status["checks"]["enable_real_sell"] is True
        assert status["checks"]["confirm_text_matched"] is True
        assert status["checks"]["real_app_key_present"] is True
        assert status["checks"]["real_app_secret_present"] is True
        assert status["checks"]["real_account_present"] is True
        assert status["checks"]["real_trading_start_date_allowed"] is True

    def test_confirm_text_mismatch_blocks_real_gate(self, monkeypatch):
        self._set_real_env(monkeypatch, confirm="live")
        status = self._cfg().enhanced_real_gate_status(current_mode="real")
        assert status["ready"] is False
        assert "FULL_AUTO_REAL_CONFIRM_TEXT_MISMATCH" in status["blocking_reasons"]

    def test_future_real_trading_start_date_blocks_real_gate(self, monkeypatch):
        self._set_real_env(monkeypatch)
        cfg = self._cfg()
        cfg._raw["safety"]["real_trading_start_date"] = "2999-01-01"
        status = cfg.enhanced_real_gate_status(current_mode="real")
        assert status["ready"] is False
        assert status["checks"]["real_trading_start_date_allowed"] is False
        assert "REAL_TRADING_START_DATE_NOT_REACHED(2999-01-01)" in status["blocking_reasons"]


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

    def test_fresh_cache_with_stale_fingerprint_is_reused(self, tmp_path, monkeypatch):
        client = _make_client("mock")
        monkeypatch.setattr("app.trading.kis_client._TOKEN_CACHE_DIR", tmp_path)
        cache_path = tmp_path / "kis_token_mock.json"
        cache_path.write_text(
            json.dumps(
                {
                    "access_token": "cached-token",
                    "expires_at": (datetime.now() + timedelta(hours=6)).isoformat(),
                    "mode": "mock",
                    "base_url": BASE_URL_MOCK,
                    "app_key_hash": client._app_key_hash(),
                    "credential_fingerprint": "stale-account-fp",
                }
            ),
            encoding="utf-8",
        )

        with patch.object(client._session, "post") as mock_post:
            token = client.get_access_token()

        assert token == "cached-token"
        mock_post.assert_not_called()

    def test_403_uses_valid_cache_if_available(self, tmp_path, monkeypatch):
        client = _make_client("mock")
        monkeypatch.setattr("app.trading.kis_client._TOKEN_CACHE_DIR", tmp_path)
        cache_path = tmp_path / "kis_token_mock.json"
        cache_path.write_text(
            json.dumps(
                {
                    "access_token": "cached-after-403",
                    "expires_at": (datetime.now() + timedelta(hours=6)).isoformat(),
                    "mode": "mock",
                    "base_url": BASE_URL_MOCK,
                    "app_key_hash": client._app_key_hash(),
                    "credential_fingerprint": client._credential_fingerprint(),
                }
            ),
            encoding="utf-8",
        )
        real_load_token_cache = client._load_token_cache
        load_calls = {"count": 0}

        def delayed_cache_load():
            load_calls["count"] += 1
            if load_calls["count"] == 1:
                return False
            return real_load_token_cache()

        client._load_token_cache = MagicMock(side_effect=delayed_cache_load)
        client._token = ""
        client._token_expires_at = datetime.min
        body = {"rt_cd": "", "msg_cd": "", "msg1": "1분당 1회"}

        with patch.object(client._session, "post", return_value=_mock_resp(403, body)):
            token = client.get_access_token()

        assert token == "cached-after-403"

    def test_legacy_overseas_cache_format_is_reused(self, tmp_path, monkeypatch):
        client = _make_client("mock")
        monkeypatch.setattr("app.trading.kis_client._TOKEN_CACHE_DIR", tmp_path)
        cache_path = tmp_path / "kis_token_mock.json"
        cache_path.write_text(
            json.dumps(
                {
                    "access_token": "legacy-token",
                    "expires_at": (datetime.now() + timedelta(hours=6)).isoformat(),
                    "mode": "mock",
                }
            ),
            encoding="utf-8",
        )

        with patch.object(client._session, "post") as mock_post:
            token = client.get_access_token()

        assert token == "legacy-token"
        mock_post.assert_not_called()

    def test_overseas_token_writer_uses_shared_cache_metadata(self, tmp_path, monkeypatch):
        from app.data_sources import kis_overseas_minute as overseas

        monkeypatch.setattr(overseas, "_TOKEN_CACHE_DIR", tmp_path)
        overseas._TOKEN_CACHE.clear()
        overseas._TOKEN_EXPIRY.clear()
        monkeypatch.setenv("KIS_MOCK_APP_KEY", "test_key")
        monkeypatch.setenv("KIS_MOCK_APP_SECRET", "test_secret")
        body = {
            "access_token": "overseas-token",
            "expires_in": 86400,
        }

        with patch("app.data_sources.kis_overseas_minute.requests.post", return_value=_mock_resp(200, body)):
            token = overseas._get_access_token("mock")

        assert token == "overseas-token"
        saved = json.loads((tmp_path / "kis_token_mock.json").read_text(encoding="utf-8"))
        assert saved["app_key_hash"] == _make_client("mock")._app_key_hash()
        assert saved["credential_fingerprint"] == _make_client("mock")._credential_fingerprint()
        assert saved["base_url"] == BASE_URL_MOCK


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
