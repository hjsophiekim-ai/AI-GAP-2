"""1차본 export (2026-11-06): 15:05 장중 export, 최종본과 키·파일 분리, 원본·최종본 보존."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime

from ws_shadow_logger import backup, backup_service
from ws_shadow_logger.tests.test_export_backup import DAY, ENV, FakeS3, make_root

EKEY = f"bk/ws_shadow/2026/10/15/ws_shadow_export_{DAY}_early.zip"
MKEY = f"bk/ws_shadow/2026/10/15/ws_shadow_export_{DAY}.zip"


def _factory(fake):
    tgt = backup.load_target(ENV)
    return lambda: backup.S3Client(tgt, session=fake)


def test_early_export_separate_key_preserved_after_final(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(backup_service, "spawn_ledger_backup", lambda *a, **k: None)   # 원장 백업 subprocess 차단
    root = make_root(tmp_path, daily=False)                    # 15:05 엔 maintenance 표식 없음
    fake = FakeS3()
    factory = _factory(fake)
    srcs_before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    r = backup_service.run_once(root, datetime(2026, 10, 15, 15, 6), env=ENV, client_factory=factory)
    assert r == [{"day": DAY, "action": "EARLY_BACKED_UP"}]
    assert EKEY in fake.objects and MKEY not in fake.objects                          # 최종본 키는 안 건드림
    z = zipfile.ZipFile(io.BytesIO(fake.objects[EKEY]["body"]))
    names = z.namelist()
    assert "snapshots/2026-10-15/000660.csv" in names and "snapshots/2026-10-15/0193T0.csv" in names
    assert "mu/2026-10-15/ticks.csv.gz" in names and "mu/2026-10-15/bars_1m.csv" in names
    assert json.loads(z.read("manifest.json"))["stage"] == "EARLY"
    em = json.loads(fake.objects["bk/ws_shadow/2026/10/15/manifest_early.json"]["body"])
    assert "bk/" + em["uploaded_key"] == EKEY and em["stage"] == "EARLY"
    assert "bk/ws_shadow/2026/10/15/manifest.json" not in fake.objects
    assert not (root / "exports" / f"ws_shadow_export_{DAY}_early.zip").exists()       # 검증 업로드 후 로컬 사본만 삭제
    for p, b in srcs_before.items():                                                   # 원본 데이터 불변
        assert p.read_bytes() == b
    n = len(fake.calls)
    assert backup_service.run_once(root, datetime(2026, 10, 15, 15, 20), env=ENV, client_factory=factory) == []
    assert len(fake.calls) == n                                                          # 성공 후 재실행 없음
    early_body = fake.objects[EKEY]["body"]
    # 장 마감 후 데이터가 늘어난 뒤 최종본 — 1차본은 그대로 남는다
    with open(root / "snapshots" / DAY / "000660.csv", "a", encoding="utf-8") as fh:
        fh.write(f"{DAY}T15:20:00,2\n")
    (root / "daily").mkdir()
    (root / "daily" / f"{DAY}.json").write_text("{}", encoding="utf-8")
    r = backup_service.run_once(root, datetime(2026, 10, 15, 15, 40), env=ENV, client_factory=factory)
    assert r[0]["action"] == "BACKUP_UPLOADED" and MKEY in fake.objects
    assert fake.objects[EKEY]["body"] == early_body
    assert json.loads(fake.objects["bk/ws_shadow/2026/10/15/manifest.json"]["body"])["snapshot_rows"] == 9
    assert (root / "exports" / f"ws_shadow_export_{DAY}.zip").exists()                 # 최종본 로컬 zip 은 유지
    assert "DAILY_EARLY_BACKUP_OK" in capsys.readouterr().out


def test_early_export_window_failure_retry_and_isolation(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    fake = FakeS3(fail_puts=1)
    factory = _factory(fake)
    ee = backup_service.early_export
    assert ee(root, datetime(2026, 10, 15, 15, 4), client_factory=factory) is None
    assert ee(root, datetime(2026, 10, 15, 15, 35), client_factory=factory) is None
    assert ee(root, datetime(2026, 10, 17, 15, 6), client_factory=factory) is None     # 토요일
    assert ee(root, datetime(2026, 10, 15, 15, 6), client_factory=factory)["action"] == "EARLY_FAIL"
    assert ee(root, datetime(2026, 10, 15, 15, 8), client_factory=factory) is None     # 5분 간격
    assert ee(root, datetime(2026, 10, 15, 15, 12), client_factory=factory)["action"] == "EARLY_BACKED_UP"
    assert backup_service.load_state(root, DAY) == {}                                  # 최종본 상태파일 무관
    assert "SECRET-DO-NOT-LOG" not in capsys.readouterr().out


def test_early_export_max_attempts(tmp_path):
    root = make_root(tmp_path)
    fake = FakeS3(fail_puts=99)
    factory = _factory(fake)
    acts = [backup_service.early_export(root, datetime(2026, 10, 15, 15, 5 + 6 * k), client_factory=factory)
            for k in range(5)]
    assert [a["action"] for a in acts if a] == ["EARLY_FAIL"] * backup_service.EARLY_MAX_ATTEMPTS


def test_early_export_without_backup_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_service, "spawn_ledger_backup", lambda *a, **k: None)
    root = make_root(tmp_path)
    assert backup_service.run_once(root, datetime(2026, 10, 15, 15, 6), env={}) == []
    assert not (root / "exports").exists()
