"""MACD2 원장 1차본 (2026-11-06): 15:05 early/ 키, 최종본과 분리·보존."""
from __future__ import annotations

import csv
from datetime import datetime

import pytest

from ledger_backup import core as lb
from ledger_backup.tests.test_ledger_backup import DAY, FakeClient, _root

PRE = "ai-gap-2-ledger/2026/11/05/"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(lb, "PUT_RETRIES", (0.0, 0.0))


def test_early_backup_separate_keys_and_preserved(tmp_path):
    c = FakeClient()
    root = _root(tmp_path)
    env = {"RENDER_GIT_COMMIT": "abc"}
    man = lb.backup_day(DAY, root=root, work=tmp_path / "work", client=c, env=env,
                        clock=lambda: datetime(2026, 11, 5, 15, 6), early=True)
    assert man["status"] == "OK" and man["stage"] == "EARLY"
    early_keys = set(c.objs)
    for name in ("macd2_execution_ledger.csv", "macd2_signal_ledger.csv", "macd2_runtime.json", "manifest.json"):
        assert PRE + "early/" + name in early_keys
    assert all(k.startswith(PRE + "early/") for k in early_keys)                       # 최종본 키·daily zip 없음
    snap = {k: c.objs[k] for k in early_keys}
    with open(root / "logs/macd2_execution_ledger.csv", "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(["3", "2026-11-05T15:00:05+09:00", "SELL", "FORCE"])  # 15:00 청산 뒤 행 증가
    fin = lb.backup_day(DAY, root=root, work=tmp_path / "work", client=c, env=env,
                        clock=lambda: datetime(2026, 11, 5, 15, 40))
    assert fin["status"] == "OK" and fin["stage"] == "FINAL"
    assert {k: c.objs[k] for k in early_keys} == snap                                   # 1차본 그대로 보존
    assert c.objs[PRE + "macd2_execution_ledger.csv"].count(b"FORCE") == 1
    assert b"FORCE" not in c.objs[PRE + "early/macd2_execution_ledger.csv"]
    assert "ai-gap-2-ledger/daily/2026-11-05.zip" in c.objs
    assert (root / "logs/macd2_execution_ledger.csv").exists()                          # 원본 누적 원장 유지


def test_early_run_due_window_and_independent_state(tmp_path):
    env = {"LOGGER_BACKUP_ENABLED": "1", "LEDGER_BACKUP_DATA_ROOT": str(_root(tmp_path))}
    work = tmp_path / "work"
    c = FakeClient()
    f = lambda: c  # noqa: E731
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 4), env=env, client_factory=f, early=True) is None
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 40), env=env, client_factory=f, early=True) is None
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 6), env=env, client_factory=f) is None     # 최종본은 15:40 전 X
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 6), env=env, client_factory=f, early=True)["stage"] == "EARLY"
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 20), env=env, client_factory=f, early=True) is None  # OK 1회
    assert (work / "state/ledger_backup_early_2026-11-05.json").exists()
    assert not (work / "state/ledger_backup_2026-11-05.json").exists()
    assert lb.run_due(work, datetime(2026, 11, 5, 15, 41), env=env, client_factory=f)["stage"] == "FINAL"


def test_backup_service_spawns_early_ledger_backup(tmp_path):
    from ws_shadow_logger import backup_service as bs
    env = {"LOGGER_BACKUP_ENABLED": "1"}
    calls = []

    class R:
        returncode, stdout = 0, ""

    def ok_runner(cmd, **kw):
        calls.append(cmd)
        return R()
    sp = bs.spawn_ledger_backup
    assert sp(tmp_path, datetime(2026, 11, 5, 15, 4), env=env, runner=ok_runner, early=True) is None
    assert sp(tmp_path, datetime(2026, 11, 5, 15, 6), env=env, runner=ok_runner, early=True) == "rc=0"
    assert calls[-1][1:] == ["-m", "ledger_backup", "--auto-early"]
    assert sp(tmp_path, datetime(2026, 11, 5, 15, 41), env=env, runner=ok_runner, early=True) is None
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state/ledger_backup_early_2026-11-05.json").write_text('{"status": "OK"}', encoding="utf-8")
    assert sp(tmp_path, datetime(2026, 11, 5, 15, 20), env=env, runner=ok_runner, early=True) is None
    assert sp(tmp_path, datetime(2026, 11, 5, 15, 41), env=env, runner=ok_runner) == "rc=0"   # 최종본은 별개
    assert calls[-1][1:] == ["-m", "ledger_backup", "--auto"]
