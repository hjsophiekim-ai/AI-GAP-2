"""일별 export + S3/R2 백업 (2026-10-16). 네트워크 없음 — S3 는 가짜 세션으로, 서명은 AWS 공식 예제 벡터로 검증."""
from __future__ import annotations

import gzip
import hashlib
import json
import zipfile
from datetime import datetime

import pytest

from ws_shadow_logger import backup, backup_service, export_day

DAY = "2026-10-15"
AFTER = datetime(2026, 10, 15, 15, 40)
ENV = {"LOGGER_BACKUP_ENABLED": "1", "LOGGER_BACKUP_PROVIDER": "r2", "LOGGER_BACKUP_BUCKET": "bk",
       "LOGGER_BACKUP_ENDPOINT": "https://acct.r2.cloudflarestorage.com", "LOGGER_BACKUP_ACCESS_KEY": "AKID",
       "LOGGER_BACKUP_SECRET_KEY": "SECRET-DO-NOT-LOG", "LOGGER_BACKUP_PREFIX": "ws_shadow/"}


def make_root(root, day=DAY, daily=True):
    s = root / "snapshots" / day; s.mkdir(parents=True)
    for sym, n in (("000660", 5), ("0193T0", 3)):
        (s / f"{sym}.csv").write_text("ts,price\n" + "".join(f"{day}T09:00:0{i},1\n" for i in range(n)), encoding="utf-8")
    m = root / "mu" / day; m.mkdir(parents=True)
    with gzip.open(m / "ticks.csv.gz", "wt", encoding="utf-8") as fh:
        fh.write("recv_kst,feed,last\n" + "".join(f"t{i},RBAQMU,100\n" for i in range(7)))
    (m / "bars_1m.csv").write_text("minute_kst,close\n09:00,1\n09:01,2\n", encoding="utf-8")
    (m / "summary.json").write_text(json.dumps({"RBAQMU": {"bars": 2, "missing_ratio": 0.1}}), encoding="utf-8")
    (root / "logs").mkdir(); (root / "logs" / f"{day}.jsonl").write_text('{"event":"start"}\n{"event":"health"}\n')
    if daily:
        (root / "daily").mkdir()
        (root / "daily" / f"{day}.json").write_text(json.dumps(
            {"day": day, "symbols": {"000660": {"rows": 5, "stale_ratio": 0.2, "max_ts_gap_s": 3.0}}}), encoding="utf-8")
    return root


# ── export ──────────────────────────────────────────────────────────────────
def test_export_zip_manifest_and_originals_kept(tmp_path):
    root = make_root(tmp_path)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    r = export_day.export(root, DAY, now=AFTER)
    assert r["status"] == "CREATED"
    zp = root / "exports" / f"ws_shadow_export_{DAY}.zip"
    assert zp.exists() and (root / "exports" / f"ws_shadow_export_{DAY}.manifest.json").exists()
    with zipfile.ZipFile(zp) as z:
        names = set(z.namelist())
        man = json.loads(z.read("manifest.json"))
    assert {f"snapshots/{DAY}/000660.csv", f"snapshots/{DAY}/0193T0.csv", f"mu/{DAY}/ticks.csv.gz",
            f"mu/{DAY}/bars_1m.csv", f"mu/{DAY}/summary.json", f"logs/{DAY}.jsonl", f"daily/{DAY}.json",
            "manifest.json"} == names
    assert man["file_count"] == 7 and man["snapshot_rows"] == 8 and man["mu_tick_rows"] == 7 and man["mu_bar_rows"] == 2
    assert man["snapshots"]["000660"]["stale_ratio"] == 0.2 and man["mu_summary"]["RBAQMU"]["missing_ratio"] == 0.1
    assert 0 < man["snapshots"]["000660"]["missing_ratio_vs_collect_window"] <= 1
    assert all(len(f["sha256"]) == 64 for f in man["files"])
    after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and "exports" not in p.parts)
    assert before == after                                                         # 원본 삭제·변경 없음


def test_export_idempotent_logs_growth_ignored_data_change_rebuilds(tmp_path):
    root = make_root(tmp_path)
    a = export_day.export(root, DAY, now=AFTER)
    assert export_day.export(root, DAY, now=AFTER)["status"] == "UNCHANGED"
    with open(root / "logs" / f"{DAY}.jsonl", "a") as fh:                       # 장외 health 줄이 붙어도
        fh.write('{"event":"health"}\n')
    assert export_day.export(root, DAY, now=AFTER)["status"] == "UNCHANGED"
    with open(root / "snapshots" / DAY / "0193T0.csv", "a") as fh:              # 데이터가 바뀌면 새로 만든다
        fh.write(f"{DAY}T09:00:09,2\n")
    b = export_day.export(root, DAY, now=AFTER)
    assert b["status"] == "CREATED" and b["manifest"]["zip_sha256"] != a["manifest"]["zip_sha256"]
    assert not list((root / "exports").glob("*.tmp"))


def test_export_not_during_session_and_no_data(tmp_path):
    root = make_root(tmp_path)
    assert export_day.export(root, DAY, now=datetime(2026, 10, 15, 14, 0))["status"] == "NOT_FINISHED"
    assert export_day.export(root, "2026-10-14", now=AFTER)["status"] == "NO_DATA"


def test_export_cli(tmp_path, capsys):
    root = make_root(tmp_path)
    assert export_day.main(["--date", DAY, "--root", str(root)]) == 0      # 오늘이 지난 날짜 → 바로 생성
    assert "[ws_shadow_export] CREATED day=2026-10-15" in capsys.readouterr().out
    assert export_day.main(["--date", DAY, "--root", str(root)]) == 0
    assert "UNCHANGED" in capsys.readouterr().out


# ── SigV4 ───────────────────────────────────────────────────────────────────
def test_sigv4_matches_aws_documented_example():
    """AWS 문서 'Signature Calculations for the Authorization Header — GET Object' 예제."""
    auth = backup.sign("GET", "examplebucket.s3.amazonaws.com", "/test.txt", "",
                       {"range": "bytes=0-9", "x-amz-content-sha256": backup.EMPTY_SHA256,
                        "x-amz-date": "20130524T000000Z"}, backup.EMPTY_SHA256,
                       region="us-east-1", access_key="AKIAIOSFODNN7EXAMPLE",
                       secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", amz_date="20130524T000000Z")
    assert auth == ("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                    "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                    "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_target_config_s3_and_r2():
    t = backup.load_target(ENV)
    assert t.region == "auto" and t.key_for(DAY, "x.zip") == "ws_shadow/2026/10/15/x.zip"
    s3 = backup.load_target(ENV | {"LOGGER_BACKUP_PROVIDER": "s3",
                                   "LOGGER_BACKUP_ENDPOINT": "https://s3.ap-northeast-2.amazonaws.com"})
    assert s3.region == "ap-northeast-2"
    assert "SECRET" not in t.describe() and "AKID" not in t.describe()
    with pytest.raises(backup.BackupConfigError, match="LOGGER_BACKUP_BUCKET"):
        backup.load_target({k: v for k, v in ENV.items() if k != "LOGGER_BACKUP_BUCKET"})
    with pytest.raises(backup.BackupConfigError, match="https"):
        backup.load_target(ENV | {"LOGGER_BACKUP_ENDPOINT": "http://x"})


class FakeS3:
    """requests.Session 대역. 객체를 메모리에 저장하고 호출을 기록한다."""

    def __init__(self, fail_puts=0):
        self.objects, self.calls, self.fail_puts = {}, [], fail_puts

    def request(self, method, url, headers=None, data=None, timeout=None):
        body = data.read() if hasattr(data, "read") else (data or b"")
        self.calls.append((method, url, dict(headers), len(body)))
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
        assert "SECRET" not in json.dumps(headers)
        path = url.split("://", 1)[1].split("/", 1)[1]

        class R:
            pass
        r = R(); r.headers, r.text = {}, ""
        if method == "HEAD":
            o = self.objects.get(path)
            r.status_code = 404 if o is None else 200
            if o:
                r.headers = {"x-amz-meta-sha256": o["sha"]}
            return r
        if self.fail_puts > 0:
            self.fail_puts -= 1
            r.status_code, r.text = 503, "SlowDown"
            return r
        assert int(headers["content-length"]) == len(body)
        self.objects[path] = {"sha": headers["x-amz-meta-sha256"], "body": body}
        r.status_code, r.headers = 200, {"ETag": '"e1"'}
        return r


def test_upload_day_streams_and_is_idempotent(tmp_path):
    root = make_root(tmp_path)
    ex = export_day.export(root, DAY, now=AFTER)
    fake = FakeS3()
    cli = backup.S3Client(backup.load_target(ENV), session=fake)
    r1 = backup.upload_day(cli, root / "exports" / f"ws_shadow_export_{DAY}.zip", DAY, ex["manifest"])
    key = f"bk/ws_shadow/2026/10/15/ws_shadow_export_{DAY}.zip"
    assert r1["status"] == "UPLOADED" and key in fake.objects
    assert fake.objects[key]["body"] == (root / "exports" / f"ws_shadow_export_{DAY}.zip").read_bytes()
    assert hashlib.sha256(fake.objects[key]["body"]).hexdigest() == ex["manifest"]["zip_sha256"]
    remote_man = json.loads(fake.objects["bk/ws_shadow/2026/10/15/manifest.json"]["body"])
    assert remote_man["uploaded_key"] == r1["key"] and remote_man["snapshot_rows"] == 8
    puts = sum(1 for c in fake.calls if c[0] == "PUT")
    r2 = backup.upload_day(cli, root / "exports" / f"ws_shadow_export_{DAY}.zip", DAY, ex["manifest"])
    assert r2["status"] == "ALREADY_PRESENT" and sum(1 for c in fake.calls if c[0] == "PUT") == puts   # 중복 업로드 없음


# ── 상주 서비스 (스케줄·재시도·상태) ───────────────────────────────────────────
def test_service_schedule_retry_and_state(tmp_path, capsys):
    root = make_root(tmp_path)
    fake = FakeS3(fail_puts=1)
    tgt = backup.load_target(ENV)
    factory = lambda: backup.S3Client(tgt, session=fake)  # noqa: E731
    assert backup_service.run_once(root, datetime(2026, 10, 15, 15, 31), env=ENV, client_factory=factory) == []
    r = backup_service.run_once(root, AFTER, env=ENV, client_factory=factory)
    assert r[0]["action"] == "BACKUP_FAIL"
    st = backup_service.load_state(root, DAY)
    assert st["status"] == "BACKUP_FAIL" and st["attempts"] == 1 and st["next_retry_at"] == "2026-10-15T15:45:00"
    assert backup_service.run_once(root, datetime(2026, 10, 15, 15, 42), env=ENV, client_factory=factory)[0]["action"] == "WAIT"
    r = backup_service.run_once(root, datetime(2026, 10, 16, 8, 30), env=ENV, client_factory=factory)  # 다음 날 이어서
    assert r[0]["action"] == "BACKUP_UPLOADED"
    st = backup_service.load_state(root, DAY)
    assert st["status"] == "BACKED_UP" and st["attempts"] == 0
    n = len(fake.calls)
    assert backup_service.run_once(root, datetime(2026, 10, 16, 9, 0), env=ENV, client_factory=factory)[0]["action"] \
        == "ALREADY_BACKED_UP" and len(fake.calls) == n                         # 성공 후엔 네트워크도 안 부른다
    out = capsys.readouterr().out
    assert "DAILY_BACKUP_FAIL" in out and "DAILY_BACKUP_OK" in out and "DAILY_EXPORT_OK" in out
    assert "SECRET-DO-NOT-LOG" not in out
    assert "SECRET-DO-NOT-LOG" not in (root / "state" / f"backup_{DAY}.json").read_text(encoding="utf-8")


def test_service_waits_for_maintenance_then_forces(tmp_path):
    root = make_root(tmp_path, daily=False)
    assert backup_service.candidate_days(root, datetime(2026, 10, 15, 15, 40)) == []
    assert backup_service.candidate_days(root, datetime(2026, 10, 15, 16, 1)) == [DAY]


def test_service_backup_off_exports_only(tmp_path, capsys):
    root = make_root(tmp_path)
    r = backup_service.run_once(root, AFTER, env={})
    assert r[0]["action"] == "EXPORTED" and (root / "exports" / f"ws_shadow_export_{DAY}.zip").exists()
    assert backup_service.load_state(root, DAY)["status"] == "EXPORTED"


def test_service_config_error_exports_only(tmp_path, capsys):
    root = make_root(tmp_path)
    r = backup_service.run_once(root, AFTER, env={"LOGGER_BACKUP_ENABLED": "1"})
    assert r[0]["action"] == "EXPORTED" and "DAILY_BACKUP_SKIP reason=config" in capsys.readouterr().out


def test_service_export_exception_is_isolated(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    monkeypatch.setattr(export_day, "export", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r = backup_service.run_once(root, AFTER, env={})
    assert r[0]["action"] == "EXPORT_FAIL" and "DAILY_BACKUP_FAIL day=2026-10-15 stage=export" in capsys.readouterr().out
