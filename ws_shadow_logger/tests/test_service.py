"""상주 서비스 계약 (2026-10-15 Render 연결).

  1·2·3. 자동매매 OFF / 신규진입 OFF / 전략모드 변경 — 로거는 그 값을 읽지 않고 계속 기록한다
  4. UI 접속과 무관 — 시작 조건은 KIS_LOGGER_ENABLED=1 과 시간뿐
  5. 로거 오류는 로거 안에서 끝난다 (재접속 / 종료코드) — production 코드는 로거를 import 하지 않는다
  6. 디스크 쓰기 실패 / 금지 경로 -> 로거만 종료(코드 3)
  7. production 키와 같으면 실행 거부(코드 2)
  8. 재시작 후 같은 날 파일 이어쓰기 + 재접속 횟수 누계
  9. KIS_LOGGER_ENABLED=0 -> 아무 것도 하지 않는다
  + RSS 한도 초과 -> 로거만 종료(코드 4), health 줄, 장외 maintenance
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ws_shadow_logger import config, maintenance, runner, service, stub

PKG = Path(__file__).resolve().parent.parent
REPO = PKG.parent
DAY = "2026-10-15"


class FakeWS:
    """async context manager + recv/send/pong. frames 를 다 주면 끊긴다(ConnectionError)."""

    def __init__(self, frames, *, on_exhausted=None, idle=False):
        self.frames = list(frames)
        self.idle = idle                 # True: 다 준 뒤에도 연결 유지(조용한 장)
        self.sent, self.pongs = [], []
        self.on_exhausted = on_exhausted

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(m)

    async def pong(self, d):
        self.pongs.append(d)

    async def recv(self):
        if self.frames:
            await asyncio.sleep(0)
            return self.frames.pop(0)[1]
        if self.on_exhausted:
            self.on_exhausted()
        if self.idle:
            await asyncio.sleep(3600)        # recv_timeout 으로 취소된다
        raise ConnectionError("fake closed")


def _clock(start: datetime, speed: float = 50.0):
    t0 = time.monotonic()
    return lambda: start + timedelta(seconds=(time.monotonic() - t0) * speed)


def _svc(root, *, connect, now_fn, rss_fn=lambda: 50.0, max_rss=200.0, lines=None, issue=None):
    lines = [] if lines is None else lines
    return service.LoggerService(
        root, config.Credentials("LOGKEY", "LOGSEC"), max_rss_mb=max_rss, connect=connect, now_fn=now_fn,
        issue=issue or (lambda c, r, n: "AK"), rss_fn=rss_fn, out=lines.append,
        health_every=0.05, recv_timeout=0.01, idle_poll=0.02, backoff_max=0.05), lines


def _frames(start, seconds=20):
    return stub.session(start, seconds=seconds)


def _run_until(svc, cond, timeout=10.0):
    async def go():
        task = asyncio.create_task(svc.run())
        t_end = time.monotonic() + timeout
        while not cond() and time.monotonic() < t_end and not task.done():
            await asyncio.sleep(0.02)
        svc.request_stop("test")
        return await task
    return asyncio.run(go())


def _production_state(root: Path, **kw):
    """production 상태파일 흉내 — 로거가 이걸 읽으면 안 된다(읽어도 영향이 없어야 한다)."""
    st = {"auto_trade_on": False, "entry_enabled": False, "strategy_mode": "CHOP", **kw}
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "macd2_runtime.json").write_text(json.dumps(st))


# ── 1·2·3·4 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("prod", [dict(auto_trade_on=False), dict(entry_enabled=False),
                                  dict(strategy_mode="N1"), dict(strategy_mode="E"), dict(auto_trade_on=True)])
def test_collects_regardless_of_trading_state(tmp_path, monkeypatch, prod):
    app_data = tmp_path / "app_data"
    _production_state(app_data, **prod)
    monkeypatch.setenv("AI_GAP_DATA_DIR", str(app_data))
    root = tmp_path / "logger"
    start = datetime(2026, 10, 15, 10, 0, 0)
    ws = FakeWS(_frames(start), idle=True)
    svc, lines = _svc(root, connect=lambda url: ws, now_fn=_clock(start, speed=1.0))
    snap = root / "snapshots" / DAY / "0193T0.csv"
    rc = _run_until(svc, lambda: not ws.frames and snap.exists() and len(snap.read_text().splitlines()) > 2)
    assert rc == 0
    raw = root / "raw" / DAY / "frames_10.txt"
    assert raw.exists() and sum(1 for _ in open(raw, encoding="utf-8")) > 50
    assert (root / "snapshots" / DAY / "0193T0.csv").exists()
    assert len(ws.sent) == len(config.all_subscriptions()) == 8
    assert any("HEALTH LOGGER_RUNNING=1" in x for x in lines)


def test_logger_source_never_reads_trading_or_ui_state():
    banned = ("auto_trade", "entry_enabled", "entry_armed", "strategy_mode", "session_state", "AI_GAP_DATA_DIR",
              "macd2_runtime", "KIS_APP_SECRET", "KIS_REAL_APP_SECRET", "KIS_MOCK_APP_SECRET")
    for p in PKG.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        for b in banned:
            assert b not in src, (p.name, b)


def test_production_code_does_not_import_the_logger():
    for p in (REPO / "app").rglob("*.py"):
        if " (" in p.name:
            continue
        assert "ws_shadow_logger" not in p.read_text(encoding="utf-8", errors="ignore"), p


def test_logger_process_does_not_load_pandas_or_production_modules():
    code = ("import sys, ws_shadow_logger.runner, ws_shadow_logger.service; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('pandas','numpy','app')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ── 5·8 재접속 / 재시작 이어쓰기 ──────────────────────────────────────────
def test_reconnects_and_counts_and_restart_continues_same_day(tmp_path):
    root = tmp_path / "logger"
    start = datetime(2026, 10, 15, 10, 0, 0)
    sessions = [FakeWS(_frames(start, 5)), FakeWS(_frames(start + timedelta(seconds=6), 5))]
    calls = []

    def connect(url):
        calls.append(url)
        if len(calls) == 2:
            raise OSError(111, "connection refused")           # 네트워크 오류 -> 백오프 재접속
        return sessions.pop(0) if sessions else FakeWS([])

    svc, lines = _svc(root, connect=connect, now_fn=_clock(start, speed=1.0))
    _run_until(svc, lambda: len(calls) >= 4)
    c1 = json.loads((root / "state" / f"counters_{DAY}.json").read_text())
    assert c1["reconnect"] >= 1 and c1["raw"] > 0
    log = [json.loads(x) for x in open(root / "logs" / f"{DAY}.jsonl", encoding="utf-8")]
    assert any(e["event"] == "reconnect" for e in log) and any(e["event"] == "disconnect" for e in log)

    # 서비스 재시작(새 프로세스와 같은 조건) — 같은 날 파일/누계에 이어서
    ws3 = FakeWS(_frames(start + timedelta(seconds=30), 5))
    svc2, _ = _svc(root, connect=lambda url: ws3, now_fn=_clock(start + timedelta(seconds=30), speed=1.0))
    _run_until(svc2, lambda: not ws3.frames, timeout=3)
    c2 = json.loads((root / "state" / f"counters_{DAY}.json").read_text())
    assert c2["raw"] > c1["raw"] and c2["reconnect"] == c1["reconnect"] + 1
    lines_ = open(root / "snapshots" / DAY / "000660.csv", encoding="utf-8").read().splitlines()
    ts = [x.split(",", 1)[0] for x in lines_[1:]]
    assert sum(1 for x in lines_ if x.startswith("ts,")) == 1 and len(set(ts)) == len(ts)


def test_out_of_session_does_not_connect(tmp_path):
    called = []
    svc, _ = _svc(tmp_path, connect=lambda url: called.append(url), now_fn=lambda: datetime(2026, 10, 15, 16, 0))
    _run_until(svc, lambda: False, timeout=0.5)
    svc2, _ = _svc(tmp_path, connect=lambda url: called.append(url), now_fn=lambda: datetime(2026, 10, 18, 10, 0))  # 일요일
    _run_until(svc2, lambda: False, timeout=0.3)
    assert called == []


def test_session_window_is_0800_to_1530():
    assert not service.in_session(datetime(2026, 10, 15, 7, 59, 59))
    assert service.in_session(datetime(2026, 10, 15, 8, 0, 0))
    assert service.in_session(datetime(2026, 10, 15, 15, 29, 59))
    assert not service.in_session(datetime(2026, 10, 15, 15, 30, 0))


# ── RSS / health ─────────────────────────────────────────────────────────
def test_rss_over_limit_stops_only_the_logger(tmp_path):
    start = datetime(2026, 10, 15, 10, 0, 0)
    ws = FakeWS(_frames(start, 60))
    svc, lines = _svc(tmp_path, connect=lambda url: ws, now_fn=_clock(start, 1.0), rss_fn=lambda: 500.0, max_rss=100)
    rc = _run_until(svc, lambda: False, timeout=5)
    assert rc == config.EXIT_RSS == 4
    assert any("RSS_LIMIT_EXCEEDED" in x for x in lines)
    health = next(x for x in lines if "HEALTH" in x)
    for k in ("LOGGER_RUNNING=1", "WS_CONNECTED=", "LAST_MESSAGE_AT=", "DATA_DIR=", "TODAY_RAW_COUNT=",
              "TODAY_SNAPSHOT_COUNT=", "RECONNECT_COUNT=", "RSS_MB=500.0", "MAX_RSS_MB=100"):
        assert k in health, k


def test_default_max_rss_is_a_fraction_of_the_container_limit(monkeypatch):
    monkeypatch.delenv(config.ENV_MAX_RSS_MB, raising=False)
    monkeypatch.setattr(config, "container_memory_limit_mb", lambda: 512.0)
    assert config.max_rss_mb()[0] == 92                    # 512MB x 18%
    monkeypatch.setattr(config, "container_memory_limit_mb", lambda: 2048.0)
    assert config.max_rss_mb()[0] == 369
    monkeypatch.setattr(config, "container_memory_limit_mb", lambda: None)
    assert config.max_rss_mb()[0] == config.RSS_FALLBACK_MB
    monkeypatch.setenv(config.ENV_MAX_RSS_MB, "150")
    assert config.max_rss_mb() == (150.0, f"env {config.ENV_MAX_RSS_MB}")


# ── 6·7·9 시작 검사 (runner.main) ─────────────────────────────────────────
def _env(monkeypatch, **kw):
    for k in ("KIS_LOGGER_ENABLED", "KIS_LOGGER_APP_KEY", "KIS_LOGGER_APP_SECRET", "KIS_LOGGER_DATA_DIR",
              "KIS_APP_KEY", "KIS_REAL_APP_KEY", "KIS_MOCK_APP_KEY", "RENDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "_dotenv_values", lambda: {})
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_disabled_does_nothing(tmp_path, monkeypatch):
    _env(monkeypatch, KIS_LOGGER_ENABLED="0", KIS_LOGGER_DATA_DIR=str(tmp_path / "d"))
    out = []
    assert runner.main(["--live"], out=out.append) == 0
    assert "DISABLED" in out[0] and not (tmp_path / "d").exists()


def test_same_key_as_production_is_refused(tmp_path, monkeypatch):
    _env(monkeypatch, KIS_LOGGER_ENABLED="1", KIS_LOGGER_DATA_DIR=str(tmp_path / "d"),
         KIS_LOGGER_APP_KEY="SAME", KIS_LOGGER_APP_SECRET="s", KIS_REAL_APP_KEY="SAME")
    monkeypatch.setattr(config, "_forbidden_roots", lambda: ())
    out = []
    assert runner.main(["--live"], out=out.append) == config.EXIT_CONFIG
    assert any("production" in x for x in out)


def test_temp_or_source_dir_is_refused(tmp_path, monkeypatch):
    _env(monkeypatch, KIS_LOGGER_ENABLED="1", KIS_LOGGER_DATA_DIR=str(tmp_path / "d"),
         KIS_LOGGER_APP_KEY="L", KIS_LOGGER_APP_SECRET="s")
    assert runner.main(["--live"], out=lambda *_: None) == config.EXIT_DISK        # tmp 아래
    monkeypatch.setenv("KIS_LOGGER_DATA_DIR", str(REPO / "ws_shadow_data"))
    assert runner.main(["--live"], out=lambda *_: None) == config.EXIT_DISK        # 소스 아래


def test_separately_mounted_disk_inside_source_tree_is_allowed(tmp_path):
    """Render: Persistent Disk 가 /opt/render/project/src/data (소스 안)에 마운트된다 -> 허용.
    같은 소스 아래라도 별도 마운트가 아니면 계속 거부."""
    fake_repo = tmp_path / "src"
    disk = fake_repo / "data"
    disk.mkdir(parents=True)

    def mount_fn(p):
        p = Path(p)
        return disk if (p == disk or disk in p.parents) else Path(p.anchor)
    ok = config.check_data_dir(str(disk / "ws_shadow_data"), forbidden=(fake_repo,), on_render=True,
                               mount_fn=mount_fn)
    assert ok.ok is False                     # forbidden 은 REPO 일 때만 마운트 예외 -> 가짜 루트로 REPO 를 바꿔 검사
    import ws_shadow_logger.config as cfg
    orig = cfg.REPO
    try:
        cfg.REPO = fake_repo
        ok = config.check_data_dir(str(disk / "ws_shadow_data"), forbidden=(fake_repo,), on_render=True,
                                   mount_fn=mount_fn)
        assert ok.ok and ok.persistent is True and ok.mount_point == str(disk)
        bad = config.check_data_dir(str(fake_repo / "logs" / "x"), forbidden=(fake_repo,), on_render=True,
                                    mount_fn=mount_fn)
        assert bad.ok is False and "금지" in bad.reason
    finally:
        cfg.REPO = orig


def test_unwritable_dir_is_refused(tmp_path, monkeypatch):
    blocker = tmp_path / "file"; blocker.write_text("x")
    _env(monkeypatch, KIS_LOGGER_ENABLED="1", KIS_LOGGER_DATA_DIR=str(blocker / "sub"),
         KIS_LOGGER_APP_KEY="L", KIS_LOGGER_APP_SECRET="s")
    monkeypatch.setattr(config, "_forbidden_roots", lambda: ())
    out = []
    assert runner.main(["--live"], out=out.append) == config.EXIT_DISK
    assert any("FAIL-SAFE" in x for x in out)


def test_render_root_filesystem_is_not_persistent(tmp_path):
    dc = config.check_data_dir(str(tmp_path / "d"), forbidden=(), on_render=True)
    # tmp_path 는 별도 마운트가 아니다 -> Render 였다면 영구 디스크가 아니므로 거부
    assert dc.ok is False and "영구 디스크" in dc.reason


def test_disk_write_failure_mid_session_stops_only_the_logger(tmp_path, monkeypatch):
    start = datetime(2026, 10, 15, 10, 0, 0)
    ws = FakeWS(_frames(start, 30))
    svc, lines = _svc(tmp_path, connect=lambda url: ws, now_fn=_clock(start, 1.0))
    n = {"k": 0}
    orig = svc.store.raw

    def raw(recv, frame):
        n["k"] += 1
        if n["k"] > 20:
            raise OSError(28, "No space left on device")
        return orig(recv, frame)
    monkeypatch.setattr(svc.store, "raw", raw)
    rc = _run_until(svc, lambda: False, timeout=5)
    assert rc == config.EXIT_DISK
    assert any("DISK_ERROR" in x for x in lines)


# ── 장외 정리 ─────────────────────────────────────────────────────────────
def test_maintenance_gzips_only_after_session_and_writes_daily(tmp_path):
    frames = _frames(datetime(2026, 10, 15, 10, 0, 0), 10)
    src = tmp_path / "f.txt"; stub.write(src, frames)
    root = tmp_path / "root"
    runner.replay(str(src), root)
    assert maintenance.run(root, datetime(2026, 10, 15, 14, 0)) == {"skipped": "in_session"}
    assert (root / "raw" / DAY / "frames_10.txt").exists()
    res = maintenance.run(root, datetime(2026, 10, 15, 15, 31))
    assert len(res["gzipped"]) == 1 and not (root / "raw" / DAY / "frames_10.txt").exists()
    import gzip
    assert sum(1 for _ in gzip.open(root / "raw" / DAY / "frames_10.txt.gz", "rt", encoding="utf-8")) == len(frames)
    daily = json.loads((root / "daily" / f"{DAY}.json").read_text(encoding="utf-8"))
    assert daily["symbols"]["0193T0"]["rows"] >= 9 and daily["counters"]["raw"] == len(frames)


def test_real_websockets_client_against_a_local_server(tmp_path, monkeypatch):
    """운영 connect(_default_connect) 그대로 — 로컬 WS 서버가 구독 8건(국내 6 + MU 2)을 받고 프레임/PINGPONG 을 보낸다."""
    websockets = pytest.importorskip("websockets")
    from websockets.asyncio.server import serve
    start = datetime(2026, 10, 15, 10, 0, 0)
    frames = [f for _, f in _frames(start, 5)]
    got = {"subs": 0, "pongs": 0}

    async def handler(ws):
        for _ in range(len(config.all_subscriptions())):
            await ws.recv(); got["subs"] += 1
        for f in frames:
            await ws.send(f)
        await ws.send(json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20261015100005"}}))
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def go():
        async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(config, "WS_URL", f"ws://127.0.0.1:{port}")
            svc, lines = _svc(tmp_path, connect=service._default_connect, now_fn=_clock(start, 1.0))
            task = asyncio.create_task(svc.run())
            for _ in range(300):
                if svc.engine and svc.engine.counts.get("pingpong"):
                    break
                await asyncio.sleep(0.02)
            svc.request_stop("test")
            return await task, svc
    rc, svc = asyncio.run(go())
    assert rc == 0 and got["subs"] == 8
    assert svc.engine.counts["data"] + svc.engine.counts["system"] == len(frames)   # 유실 0 (recv 취소 포함)
    assert svc.engine.counts["pingpong"] == 1
