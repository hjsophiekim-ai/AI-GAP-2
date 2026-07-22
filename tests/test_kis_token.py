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
    def _set_real_env(self, monkeypatch, confirm="LIVE"):
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
        cfg._raw["safety"]["real_confirm_text"] = "LIVE"
        cfg._raw["safety"]["real_order_confirm_text"] = "LIVE"
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
        self._set_real_env(monkeypatch, confirm="WRONG")
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


# ─────────────────────────────────────────────────────────────────────────────
# TestTokenExpiredMidSessionAutoRetry — 2026-07-21 실측 수정
#
# 로컬 캐시(메모리/파일)는 여전히 "만료 전"이라고 믿지만, KIS 모의투자 서버가
# 그 토큰을 이미 무효 처리한 경우(msg_cd=EGW00123 "기간이 만료된 token 입니다")
# get_balance()/get_buyable_cash_raw()가 재발급 없이 계속 같은 죽은 토큰으로
# 실패했다(2026-07-16 로그에 40분 이상 반복 관측 — 잔고조회 영구 실패로 이어져
# DAILY_RETURN_UNKNOWN이 risk_manager에서 신규주문을 계속 차단했다).
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenExpiredMidSessionAutoRetry:
    def _client_with_valid_looking_cached_token(self, tmp_path, monkeypatch) -> KISClient:
        from app.utils import data_paths
        monkeypatch.setattr(data_paths, "CACHE_DIR", tmp_path)
        import app.trading.kis_client as kis_client_module
        monkeypatch.setattr(kis_client_module, "_TOKEN_CACHE_DIR", tmp_path)
        client = _make_client("mock")
        client._token = "stale-but-locally-valid-token"
        client._token_expires_at = datetime.now() + timedelta(hours=1)
        return client

    def test_get_balance_retries_once_after_server_side_token_expiry(self, tmp_path, monkeypatch):
        client = self._client_with_valid_looking_cached_token(tmp_path, monkeypatch)
        expired_body = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}
        success_body = {
            "rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.",
            "output1": [], "output2": [{"dnca_tot_amt": "10000000", "ord_psbl_cash": "10000000"}],
        }
        reissue_body = {
            "rt_cd": "0", "msg_cd": "OK", "msg1": "정상",
            "access_token": "fresh-token", "expires_in": 86400,
        }
        # 실측 로그와 동일하게 만료 응답은 HTTP 500으로 온다(2026-07-16 관측).
        get_responses = [_mock_resp(500, expired_body), _mock_resp(200, success_body)]
        with patch.object(client._session, "get", side_effect=get_responses) as mock_get, \
                patch.object(client._session, "post", return_value=_mock_resp(200, reissue_body)) as mock_post, \
                patch.object(client, "_save_token_cache"):
            result = client.get_balance()

        assert mock_get.call_count == 2  # 만료 응답 1회 + 재시도 1회
        assert mock_post.call_count == 1  # 무효화 후 딱 1번만 재발급
        assert client._token == "fresh-token"  # 새 토큰으로 교체됨
        assert result["cash"] == 10_000_000.0
        assert result.get("error") is None

        # 두 번째 GET 호출은 재발급된 새 토큰을 Authorization 헤더로 사용해야 한다.
        second_call_headers = mock_get.call_args_list[1].kwargs["headers"]
        assert second_call_headers["authorization"] == "Bearer fresh-token"

    def test_get_buyable_cash_raw_retries_once_after_server_side_token_expiry(self, tmp_path, monkeypatch):
        client = self._client_with_valid_looking_cached_token(tmp_path, monkeypatch)
        expired_body = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}
        success_body = {
            "rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.",
            "output": {"ord_psbl_cash": "5000000", "nrcvb_buy_amt": "5000000", "psbl_qty": "10"},
        }
        reissue_body = {
            "rt_cd": "0", "msg_cd": "OK", "msg1": "정상",
            "access_token": "fresh-token-2", "expires_in": 86400,
        }
        get_responses = [_mock_resp(500, expired_body), _mock_resp(200, success_body)]
        with patch.object(client._session, "get", side_effect=get_responses) as mock_get, \
                patch.object(client._session, "post", return_value=_mock_resp(200, reissue_body)), \
                patch.object(client, "_save_token_cache"):
            result = client.get_buyable_cash_raw()

        assert mock_get.call_count == 2
        assert result["ord_psbl_cash"] == 5_000_000.0
        assert result.get("error") is None

    def test_non_token_error_does_not_trigger_retry(self, tmp_path, monkeypatch):
        """다른 오류(예: 레이트리밋 EGW00201)는 토큰 재발급 대상이 아니므로 재시도하지 않는다."""
        client = self._client_with_valid_looking_cached_token(tmp_path, monkeypatch)
        rate_limit_body = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}
        with patch.object(client._session, "get", return_value=_mock_resp(500, rate_limit_body)) as mock_get, \
                patch.object(client._session, "post") as mock_post:
            result = client.get_balance()

        assert mock_get.call_count == 1  # 토큰 문제가 아니므로 재시도 없음
        mock_post.assert_not_called()
        assert client._token == "stale-but-locally-valid-token"  # 토큰 무효화되지 않음
        assert result["error"] is not None
