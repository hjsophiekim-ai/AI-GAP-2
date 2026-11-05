"""MACD2 원장 일별 R2 백업 (2026-11-05) — 가짜 S3 클라이언트로만 검증(네트워크 없음)."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ledger_backup import core as lb
from ws_shadow_logger import backup

DAY = "2026-11-05"


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}

    def json(self):
        return json.loads(self._b)

    def iter_lines(self):
        return iter(self._b.splitlines())


class FakeClient:
    def __init__(self, fail=()):
        self.t = backup.Target("r2", "https://x.r2.cloudflarestorage.com", "b", "auto", "k", "s", "ai-gap-2-ledger/")
        self.objs, self.meta, self.fail, self.puts = {}, {}, set(fail), []

    def head_sha256(self, key):
        return self.meta.get(key)

    def put_file(self, key, path, sha, content_type=""):
        if any(f in key for f in self.fail):
            raise IOError("PUT 503")
        self.objs[key] = Path(path).read_bytes(); self.meta[key] = sha; self.puts.append(key)

    def put_bytes(self, key, body, content_type=""):
        self.objs[key] = body; self.meta[key] = hashlib.sha256(body).hexdigest(); self.puts.append(key)

    def _req(self, method, key, **kw):
        return FakeResp(200, self.objs[key]) if key in self.objs else FakeResp(404)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(lb, "PUT_RETRIES", (0.0, 0.0))


def _root(tmp_path, *, with_exec=True):
    root = tmp_path / "data"
    (root / "logs").mkdir(parents=True); (root / "state").mkdir()
    with open(root / "logs/macd2_signal_ledger.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["trading_date", "signal_id", "worker_code_sha", "note"])
        w.writerow(["20261104", "a", "old", "x"]); w.writerow(["20261105", "b", "sha1", "멀티\n라인, 쉼표"])
        w.writerow(["20261105", "c", "sha2", ""])
    if with_exec:
        with open(root / "logs/macd2_execution_ledger.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh); w.writerow(["order_id", "timestamp", "side", "exit_reason"])
            w.writerow(["1", "2026-11-05T10:33:01+09:00", "BUY", ""]); w.writerow(["2", "2026-11-04T09:00:00+09:00", "SELL", "X"])
    (root / "state/macd2_runtime.json").write_text(json.dumps(
        {"strategy_mode": "CHOP", "mu_assist_enabled": True, "mu_assist_xs_enabled": True, "position": {"big": 1},
         "token_like": "x"}), encoding="utf-8")
    return root


def _run(tmp_path, client, root=None):
    root = root or _root(tmp_path)
    return lb.backup_day(DAY, root=root, work=tmp_path / "work", client=client,
                         env={"RENDER_GIT_COMMIT": "abc123", "RENDER_GIT_BRANCH": "main-MACD2",
                              "MACD2_MU_GAP_FALLBACK_ENABLED": "0", "MACD2_KIS_APP_KEY": "SECRET"},
                         clock=lambda: datetime(2026, 11, 5, 15, 40))


def test_slices_day_rows_and_manifest(tmp_path):
    c = FakeClient()
    man = _run(tmp_path, c)
    f = {x["filename"]: x for x in man["files"]}
    assert man["status"] == "OK" and man["date_kst"] == DAY
    assert man["worker_code_sha"] == "sha2" and man["render_git_commit"] == "abc123" and man["git_branch"] == "main-MACD2"
    assert f["macd2_signal_ledger.csv"]["row_count"] == 2 and f["macd2_execution_ledger.csv"]["row_count"] == 1
    sl = tmp_path / "work/state/ledger_backup" / DAY / "macd2_signal_ledger.csv"
    assert f["macd2_signal_ledger.csv"]["sha256"] == hashlib.sha256(sl.read_bytes()).hexdigest()
    rows = list(csv.DictReader(open(sl, encoding="utf-8")))
    assert [r["signal_id"] for r in rows] == ["b", "c"] and rows[0]["note"] == "멀티\n라인, 쉼표"      # 따옴표 줄바꿈 보존
    flags = man["flags"]
    assert flags["runtime_state"]["strategy_mode"] == "CHOP" and flags["runtime_state"]["mu_assist_xs_enabled"] is True
    assert "position" not in flags["runtime_state"] and "token_like" not in flags["runtime_state"]
    assert flags["env"] == {"MACD2_MU_GAP_FALLBACK_ENABLED": "0"}                           # 비밀 env 제외
    assert c.t.key_for(DAY, "manifest.json") == "ai-gap-2-ledger/2026/11/05/manifest.json"
    assert "ai-gap-2-ledger/2026/11/05/macd2_signal_ledger.csv" in c.objs
    assert man["daily_zip"]["key"] == "ai-gap-2-ledger/daily/2026-11-05.zip" and man["daily_zip"]["upload_status"] == "UPLOADED"
    src = (tmp_path / "data/logs/macd2_signal_ledger.csv").read_bytes()
    assert b"20261104" in src                                                                # 원본 무변경


def test_missing_files_recorded_not_fatal(tmp_path):
    man = _run(tmp_path, FakeClient(), root=_root(tmp_path, with_exec=False))
    f = {x["filename"]: x for x in man["files"]}
    assert f["macd2_execution_ledger.csv"]["upload_status"] == "MISSING" and f["macd2_execution_ledger.csv"]["exists"] is False
    assert f["macd2_mu_assist_ledger.csv"]["upload_status"] == "MISSING"
    assert man["status"] == "OK"                                                             # 없는 파일은 실패가 아니다


def test_rerun_same_content_skips(tmp_path):
    c = FakeClient()
    root = _root(tmp_path)
    _run(tmp_path, c, root)
    n = len(c.puts)
    man = _run(tmp_path, c, root)
    assert {x["upload_status"] for x in man["files"] if x["exists"]} == {"SKIP_SAME"}
    assert man["daily_zip"]["upload_status"] in ("SKIP_SAME", "UPLOADED")
    assert len(c.puts) - n <= 2                                                              # manifest(+zip 메타 시각차)만


def test_partial_failure(tmp_path, capsys):
    c = FakeClient(fail=("macd2_execution_ledger.csv",))
    man = _run(tmp_path, c)
    f = {x["filename"]: x for x in man["files"]}
    assert man["status"] == "PARTIAL" and f["macd2_execution_ledger.csv"]["upload_status"] == "FAILED"
    assert f["macd2_signal_ledger.csv"]["upload_status"] == "UPLOADED"
    out = capsys.readouterr().out
    assert "[DAILY_LEDGER_BACKUP] date=2026-11-05 status=PARTIAL" in out and "macd2_execution_ledger.csv" in out


def test_run_due_isolated_and_bounded(tmp_path, monkeypatch):
    env = {"LOGGER_BACKUP_ENABLED": "1", "LEDGER_BACKUP_DATA_ROOT": str(_root(tmp_path))}
    work = tmp_path / "work"
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 30), env=env, client_factory=FakeClient) is None   # 15:40 전
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 41), env={"LOGGER_BACKUP_ENABLED": "0"}) is None  # 꺼짐

    def boom():
        raise RuntimeError("R2 down")
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 41), env=env, client_factory=boom) is None       # 예외 밖으로 안 나감
    c = FakeClient(fail=("macd2_signal_ledger.csv",))
    t = datetime(2026, 11, 5, 15, 41)
    results = []
    for k in range(6):                                                                        # 5분 간격 재시도, 최대 3회
        results.append(lb.run_due(work, t, env=env, client_factory=lambda: c))
        t = t + timedelta(minutes=6)
    assert sum(r is not None for r in results) == lb.MAX_ATTEMPTS
    ok = FakeClient()
    assert lb.run_due(work, datetime(2026, 11, 6, 15, 41), env=env, client_factory=lambda: ok)["status"] == "OK"
    assert lb.run_due(work, datetime(2026, 11, 6, 15, 50), env=env, client_factory=lambda: ok) is None  # OK 면 재실행 안 함


def test_verify_and_list_past(tmp_path, capsys):
    c = FakeClient()
    root = _root(tmp_path)
    _run(tmp_path, c, root)
    assert lb.verify(DAY, c)["ok"] is True
    res = lb.list_past(root)
    assert res["macd2_signal_ledger.csv"] == {"20261104": 1, "20261105": 2}
    assert res["macd2_execution_ledger.csv"] == {"20261105": 1, "20261104": 1}
    assert "VERIFY date=2026-11-05 result=OK" in capsys.readouterr().out


def test_backup_service_spawns_separate_process_and_survives(tmp_path):
    """backup_service 는 원장 백업을 별도 프로세스로만 실행하고, 실패·타임아웃이 루프를 깨지 않는다."""
    from ws_shadow_logger import backup_service as bs
    env = {"LOGGER_BACKUP_ENABLED": "1"}
    calls = []

    class R:
        returncode, stdout = 0, "[DAILY_LEDGER_BACKUP] date=2026-11-05 status=OK"

    def ok_runner(cmd, **kw):
        calls.append(cmd)
        return R()
    assert bs.spawn_ledger_backup(tmp_path, datetime(2026, 11, 5, 15, 30), env=env, runner=ok_runner) is None
    assert bs.spawn_ledger_backup(tmp_path, datetime(2026, 11, 5, 15, 41), env=env, runner=ok_runner) == "rc=0"
    assert calls[0][1:] == ["-m", "ledger_backup", "--auto"]
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state/ledger_backup_2026-11-05.json").write_text('{"status": "OK", "attempts": 1}', encoding="utf-8")
    assert bs.spawn_ledger_backup(tmp_path, datetime(2026, 11, 5, 15, 50), env=env, runner=ok_runner) is None

    def boom(cmd, **kw):
        import subprocess
        raise subprocess.TimeoutExpired(cmd, 600)
    assert bs.spawn_ledger_backup(tmp_path, datetime(2026, 11, 6, 15, 41), env=env, runner=boom) == "error"
    assert bs.spawn_ledger_backup(tmp_path, datetime(2026, 11, 6, 15, 41), env={"LOGGER_BACKUP_ENABLED": "0"},
                                  runner=boom) is None
