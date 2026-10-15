"""production 분리 보증: app import 없음, 주문/REST 토큰 경로 없음, production 키로는 실행 거부, 접속키는 로거 캐시만."""
import re
from datetime import datetime
from pathlib import Path

import pytest

from ws_shadow_logger import approval, config

PKG = Path(__file__).resolve().parent.parent


def test_no_production_imports_or_order_paths():
    for p in PKG.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+app(\.|\s)", src, re.M), p.name
        for bad in ("tokenP", "order-cash", "TTTC", "VTTC", "kis_client", "hashkey"):
            assert bad not in src, (p.name, bad)


def _clear(monkeypatch):
    for k in ("KIS_LOGGER_APP_KEY", "KIS_LOGGER_APP_SECRET", "KIS_REAL_APP_KEY", "KIS_APP_KEY", "KIS_MOCK_APP_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_refuses_production_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "_dotenv_values",
                        lambda: {"KIS_REAL_APP_KEY": "PROD", "KIS_LOGGER_APP_KEY": "PROD", "KIS_LOGGER_APP_SECRET": "s"})
    with pytest.raises(config.ConfigError, match="production"):
        config.load_credentials()


def test_missing_key_not_ready(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(config, "_dotenv_values", lambda: {})
    with pytest.raises(config.ConfigError, match="KIS_LOGGER_APP_KEY"):
        config.load_credentials()


def test_approval_posts_only_approval_and_caches(tmp_path):
    calls = []

    class R:
        status_code = 200

        def json(self):
            return {"approval_key": "AK1"}

    def post(url, body):
        calls.append((url, body)); return R()

    c = config.Credentials("LOGKEY", "LOGSEC"); now = datetime(2026, 10, 15, 7, 55)
    assert approval.issue(c, tmp_path, now, post=post) == "AK1"
    assert approval.issue(c, tmp_path, now, post=post) == "AK1"            # 캐시 재사용
    assert len(calls) == 1 and calls[0][0] == "https://openapi.koreainvestment.com:9443/oauth2/Approval"
    assert calls[0][1] == {"grant_type": "client_credentials", "appkey": "LOGKEY", "secretkey": "LOGSEC"}
    assert (tmp_path / "state" / "approval_20261015.json").exists()
