"""scripts/render_start_with_logger.sh — 실제 bash 로 돌려 본다 (web/logger 는 가짜 파이썬 프로세스).

  · KIS_LOGGER_ENABLED=0 -> 로거 미기동, web 명령 그대로 exec, web 종료코드 그대로
  · 로거가 죽어도(rc=1) web 은 끝까지 돌고, 로거는 백오프 재시작된다
  · 로거 rc=2/3/4 -> 재시작 안 함, web 은 계속
  · web 이 죽으면(rc=7) 로거를 정리하고 7 로 끝난다 -> Render 가 재시작
  · SIGTERM -> web/로거 둘 다 정리하고 끝난다
  · 백업(backup_service) 은 로거와 별개 감독: 죽어도 로거·web 무관, LOGGER_BACKUP_ENABLED=1 만으로도 단독 기동
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "render_start_with_logger.sh"
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if (sys.platform == "win32" and GIT_BASH.exists()) else shutil.which("bash")
PY = sys.executable.replace("\\", "/")

pytestmark = pytest.mark.skipif(not BASH, reason="bash 없음")

FAKE = r'''
import os, sys, time
mode, marker = sys.argv[1], sys.argv[2]
with open(marker, "a") as f: f.write(f"start {mode}\n")
if mode.startswith("exit"):
    sys.exit(int(mode[4:]))
secs = float(mode[3:]) if mode.startswith("run") else 3600
time.sleep(secs)
with open(marker, "a") as f: f.write(f"done {mode}\n")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


def _run(tmp_path, *, enabled, logger_mode, web_secs, web_rc=0, timeout=60, extra_env=None, backup_mode="run999",
         backup_enabled="0"):
    fake = tmp_path / "fake.py"; fake.write_text(FAKE)
    lm, wm, bm = tmp_path / "logger.txt", tmp_path / "web.txt", tmp_path / "backup.txt"
    env = dict(os.environ, KIS_LOGGER_ENABLED=enabled, WS_LOGGER_RESTART_DELAY="1", WS_LOGGER_STOP_GRACE_SEC="3",
               WS_LOGGER_CMD=f"{PY} {fake.as_posix()} {logger_mode} {lm.as_posix()}", FAKE_RC=str(web_rc),
               WS_BACKUP_CMD=f"{PY} {fake.as_posix()} {backup_mode} {bm.as_posix()}",
               LOGGER_BACKUP_ENABLED=backup_enabled, **(extra_env or {}))
    t0 = time.monotonic()
    p = subprocess.run([BASH, SCRIPT.as_posix(), PY, fake.as_posix(), f"run{web_secs}", wm.as_posix()],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    rd = lambda f: f.read_text() if f.exists() else ""
    _run.backup = rd(bm)
    return p.returncode, p.stdout + p.stderr, rd(lm), rd(wm), time.monotonic() - t0


def test_disabled_execs_web_unchanged_and_never_starts_logger(tmp_path):
    rc, out, lg, web, _ = _run(tmp_path, enabled="0", logger_mode="run999", web_secs=0.2, web_rc=7)
    assert rc == 7
    assert lg == "" and "start run0.2" in web and "done run0.2" in web
    assert "exec web command unchanged" in out and "ws_shadow_logger started" not in out
    assert _run.backup == "" and "ws_shadow_backup started" not in out


def test_logger_crash_is_restarted_and_web_keeps_running(tmp_path):
    rc, out, lg, web, _ = _run(tmp_path, enabled="1", logger_mode="exit1", web_secs=15)
    assert rc == 0 and "done run15" in web
    assert lg.count("start exit1") >= 2, "죽은 로거는 다시 띄운다"
    assert "DIED rc=1" in out


@pytest.mark.parametrize("code", [2, 3, 4])
def test_fail_safe_exit_codes_are_not_restarted(tmp_path, code):
    rc, out, lg, web, _ = _run(tmp_path, enabled="1", logger_mode=f"exit{code}", web_secs=10)
    assert rc == 0 and "done run10" in web
    assert lg.count(f"start exit{code}") == 1 and "not restarting" in out


def test_web_death_stops_logger_and_propagates_rc(tmp_path):
    rc, out, lg, web, took = _run(tmp_path, enabled="1", logger_mode="run999", web_secs=2, web_rc=7)
    assert rc == 7 and "web exited rc=7" in out
    assert "start run999" in lg and "done run999" not in lg
    assert "start run999" in _run.backup and "done run999" not in _run.backup        # 백업도 정리
    assert took < 30


def test_sigterm_stops_web_and_logger(tmp_path):
    fake = tmp_path / "fake.py"; fake.write_text(FAKE)
    lm, wm = tmp_path / "logger.txt", tmp_path / "web.txt"
    bm = tmp_path / "backup.txt"
    inner = (f"KIS_LOGGER_ENABLED=1 WS_LOGGER_STOP_GRACE_SEC=3 "
             f"WS_BACKUP_CMD='{PY} {fake.as_posix()} run999 {bm.as_posix()}' "
             f"WS_LOGGER_CMD='{PY} {fake.as_posix()} run999 {lm.as_posix()}' "
             f"bash '{SCRIPT.as_posix()}' '{PY}' '{fake.as_posix()}' run999 '{wm.as_posix()}' & p=$!; "
             f"sleep 4; kill -TERM $p; wait $p; echo WRAPPER_RC=$?")
    t0 = time.monotonic()
    out = subprocess.run([BASH, "-c", inner], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert time.monotonic() - t0 < 30
    s = out.stdout + out.stderr
    assert "received SIGTERM" in s and "WRAPPER_RC=" in s
    assert "done run999" not in wm.read_text() and "done run999" not in lm.read_text()
    assert "start run999" in bm.read_text() and "done run999" not in bm.read_text()


def test_backup_crash_is_isolated_from_logger_and_web(tmp_path):
    rc, out, lg, web, _ = _run(tmp_path, enabled="1", logger_mode="run999", web_secs=12, backup_mode="exit1")
    assert rc == 0 and "done run12" in web
    assert lg.count("start run999") == 1, "백업이 죽어도 로거는 재시작되지 않는다"
    assert _run.backup.count("start exit1") >= 2 and "ws_shadow_backup DIED rc=1" in out


def test_backup_fail_safe_code_not_restarted(tmp_path):
    rc, out, lg, web, _ = _run(tmp_path, enabled="1", logger_mode="run999", web_secs=6, backup_mode="exit3")
    assert rc == 0 and _run.backup.count("start exit3") == 1 and "ws_shadow_backup exited rc=3" in out


def test_backup_only_mode_without_logger(tmp_path):
    rc, out, lg, web, _ = _run(tmp_path, enabled="0", logger_mode="run999", web_secs=3, backup_enabled="1")
    assert rc == 0 and "done run3" in web
    assert lg == "" and "start run999" in _run.backup and "exec web command unchanged" not in out
