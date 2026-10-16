"""운영 R2 설정 그대로의 검증 (2026-10-16) — 버킷 macd2-shadow-data, prefix ws_shadow/, provider r2. 네트워크 없음."""
from __future__ import annotations

from datetime import datetime

import pytest

from ws_shadow_logger import backup, backup_service, export_day
from ws_shadow_logger.tests.test_export_backup import AFTER, DAY, FakeS3, make_root

ACCESS = "0123456789abcdef0123456789abcdef"                                    # R2 Access Key ID 형식(32 hex)
SECRET = "f" * 60 + "beef"                                                      # R2 Secret Access Key 형식(64 hex)
PROD = {"LOGGER_BACKUP_ENABLED": "1", "LOGGER_BACKUP_PROVIDER": "r2", "LOGGER_BACKUP_BUCKET": "macd2-shadow-data",
        "LOGGER_BACKUP_ENDPOINT": "https://0a1b2c3d4e5f.r2.cloudflarestorage.com",
        "LOGGER_BACKUP_ACCESS_KEY": ACCESS, "LOGGER_BACKUP_SECRET_KEY": SECRET, "LOGGER_BACKUP_PREFIX": "ws_shadow/"}


class R2Fake(FakeS3):
    """FakeS3 + 운영 키 ID 로 서명했는지 확인."""

    def request(self, method, url, headers=None, data=None, timeout=None):
        assert headers["Authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={ACCESS}/")
        assert "/auto/s3/aws4_request" in headers["Authorization"]              # R2 region = auto
        assert url.startswith("https://0a1b2c3d4e5f.r2.cloudflarestorage.com/macd2-shadow-data/")
        assert SECRET not in str(headers)
        headers = dict(headers, Authorization="AWS4-HMAC-SHA256 Credential=AKID/" + headers["Authorization"].split("/", 1)[1])
        return super().request(method, url, headers=headers, data=data, timeout=timeout)


def test_prod_target_keys_and_region():
    t = backup.load_target(PROD)
    assert (t.provider, t.region, t.bucket, t.prefix) == ("r2", "auto", "macd2-shadow-data", "ws_shadow/")
    assert t.key_for(DAY, f"ws_shadow_export_{DAY}.zip") == "ws_shadow/2026/10/15/ws_shadow_export_2026-10-15.zip"
    assert SECRET not in t.describe() and ACCESS not in t.describe()


@pytest.mark.parametrize("key", ["LOGGER_BACKUP_ACCESS_KEY", "LOGGER_BACKUP_SECRET_KEY"])
def test_rejects_cloudflare_api_token_without_echoing_it(key):
    tok = "cfat_THIS-IS-AN-API-TOKEN-NOT-S3-KEY"
    with pytest.raises(backup.BackupConfigError, match="cfat_") as e:
        backup.load_target(PROD | {key: tok})
    assert tok not in str(e.value) and "Access Key ID" in str(e.value)


@pytest.mark.parametrize("ep, msg", [
    ("https://0a1b2c3d4e5f.r2.cloudflarestorage.com/macd2-shadow-data", "경로 없이"),
    ("https://dash.cloudflare.com", "r2.cloudflarestorage.com"),
])
def test_rejects_wrong_r2_endpoint(ep, msg):
    with pytest.raises(backup.BackupConfigError, match=msg):
        backup.load_target(PROD | {"LOGGER_BACKUP_ENDPOINT": ep})


def test_prod_upload_paths_logs_retry_restart_and_no_secret(tmp_path, capsys):
    root = make_root(tmp_path)
    fake = R2Fake(fail_puts=1)
    tgt = backup.load_target(PROD)
    factory = lambda: backup.S3Client(tgt, session=fake)  # noqa: E731
    assert backup_service.run_once(root, AFTER, env=PROD, client_factory=factory)[0]["action"] == "BACKUP_FAIL"
    # 프로세스 재시작 = 상태파일만 남은 새 호출. 다음 날 아침 이어서 처리
    r = backup_service.run_once(root, datetime(2026, 10, 16, 7, 0), env=PROD, client_factory=factory)
    assert r[0]["action"] == "BACKUP_UPLOADED"
    assert set(fake.objects) == {"macd2-shadow-data/ws_shadow/2026/10/15/ws_shadow_export_2026-10-15.zip",
                                 "macd2-shadow-data/ws_shadow/2026/10/15/manifest.json"}
    n = len(fake.calls)
    assert backup_service.run_once(root, datetime(2026, 10, 16, 7, 1), env=PROD,
                                   client_factory=factory)[0]["action"] == "ALREADY_BACKED_UP"
    assert len(fake.calls) == n                                                   # 성공한 날짜는 다시 올리지 않는다
    out = capsys.readouterr().out
    assert "DAILY_BACKUP_FAIL provider=r2 day=2026-10-15 stage=upload attempt=1" in out
    assert "DAILY_BACKUP_OK provider=r2 day=2026-10-15 result=UPLOADED key=ws_shadow/2026/10/15/" in out
    st = (root / "state" / f"backup_{DAY}.json").read_text(encoding="utf-8")
    assert '"provider": "r2"' in st
    for s in (SECRET, ACCESS):
        assert s not in out and s not in st
    assert (root / "snapshots" / DAY / "000660.csv").exists()                     # 원본 유지


def test_prod_missing_secret_exports_only_and_skips(tmp_path, capsys):
    root = make_root(tmp_path)
    env = {k: v for k, v in PROD.items() if k != "LOGGER_BACKUP_SECRET_KEY"}
    r = backup_service.run_once(root, AFTER, env=env)
    assert r[0]["action"] == "EXPORTED" and (root / "exports" / f"ws_shadow_export_{DAY}.zip").exists()
    out = capsys.readouterr().out
    assert "DAILY_BACKUP_SKIP reason=config provider=r2" in out and "LOGGER_BACKUP_SECRET_KEY" in out
    assert ACCESS not in out


def test_real_s3client_path_through_service_main_factory(tmp_path, monkeypatch, capsys):
    """client_factory 없이(운영 경로) run_once 가 load_target → S3Client 를 만드는지 — 세션만 가짜로."""
    root = make_root(tmp_path)
    fake = R2Fake()
    orig = backup.S3Client.__init__
    monkeypatch.setattr(backup.S3Client, "__init__", lambda self, t, session=None, clock=None: orig(self, t, session=fake))
    r = backup_service.run_once(root, AFTER, env=PROD)
    assert r[0]["action"] == "BACKUP_UPLOADED" and "DAILY_BACKUP_OK provider=r2" in capsys.readouterr().out
    assert export_day.zip_path(root, DAY).exists()
