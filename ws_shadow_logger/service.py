"""상주 로거 서비스 — Render Web Service 안에서 UI 와 **별도 프로세스**로 항상 돈다.

  · 프로세스는 계속 살아 있고, KST 평일 08:00~15:30 에만 WebSocket 에 붙어 구독/저장한다.
  · 장외에는 연결 없이 idle 하고, 15:30 이후 한 번 maintenance(gzip/일별 요약)를 돌린다.
  · 끊기면 지수 백오프(최대 60초)로 재접속 — 접속키는 로거 전용 key 로 필요할 때만 재발급(하루 캐시).
  · 1분마다 HEALTH 한 줄을 stdout(Render Logs)과 logs/ 에 남기고 RSS 를 잰다.
    RSS 가 한도를 넘으면 로거만 종료한다(종료코드 4, 래퍼는 재시작하지 않는다).
  · SIGTERM/SIGINT 를 받으면 연결을 닫고 파일을 flush/close 한 뒤 0 으로 끝난다.

자동매매 / 신규진입 허용 / 전략모드 / UI 세션 상태는 읽지 않는다 — 실행 조건은 시간뿐이다.
주문 API, REST 접근토큰, production 모듈은 import 하지 않는다.
"""
from __future__ import annotations

import asyncio
import errno
import signal
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from . import approval, config, maintenance
from .engine import Engine
from .parser import subscribe_message
from .storage import Store, day_str

KST = timezone(timedelta(hours=9))
TAG = "[ws_shadow_logger]"


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and config.SESSION_START <= now.time() < config.SESSION_END


def symbols():
    return sorted({k for _, k in config.SUBSCRIPTIONS})


def _default_connect(url):
    import websockets  # noqa: PLC0415 — 장중에만 필요
    # max_queue 로 수신 버퍼를 묶고(무제한 금지), 압축은 끈다(zlib 버퍼 절약).
    return websockets.connect(url, ping_interval=None, max_queue=256, compression=None, open_timeout=15)


class LoggerService:
    def __init__(self, root: Path, creds: config.Credentials, *, max_rss_mb: float, rss_source: str = "",
                 connect=_default_connect, now_fn=now_kst, issue=approval.issue, rss_fn=config.current_rss_mb,
                 out=None, health_every: float = config.HEALTH_EVERY_SEC, recv_timeout: float = 0.5,
                 idle_poll: float = 30.0, backoff_max: float = 60.0):
        self.root = Path(root)
        self.creds = creds
        self.max_rss_mb = float(max_rss_mb)
        self.rss_source = rss_source
        self.connect = connect
        self.now = now_fn
        self.issue = issue
        self.rss_fn = rss_fn
        self.out = out or (lambda line: print(line, flush=True))
        self.health_every = health_every
        self.recv_timeout = recv_timeout
        self.idle_poll = idle_poll
        self.backoff_max = backoff_max
        self.store = Store(self.root)
        self.engine: Engine | None = None
        self.ws_connected = False
        self.exit_code = config.EXIT_OK
        self._stop = asyncio.Event()
        self._maintained_day = None

    # ── 제어 ───────────────────────────────────────────────────────────────
    def request_stop(self, why: str = "signal", code: int | None = None):
        if code is not None:
            self.exit_code = code
        if not self._stop.is_set():
            self.out(f"{TAG} STOP requested ({why})")
            try:
                self.store.log(self.now(), "stop_requested", why=why)
            except OSError:
                pass
        self._stop.set()

    def _install_signals(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop, sig.name)
            except (NotImplementedError, RuntimeError, ValueError):     # Windows / 메인스레드 아님
                try:
                    signal.signal(sig, lambda *_: self.request_stop("signal"))
                except (ValueError, OSError):
                    pass

    async def _sleep(self, sec: float) -> bool:
        """stop 이 오면 즉시 깨어난다. stop 이면 True."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, sec))
        except asyncio.TimeoutError:
            pass
        return self._stop.is_set()

    # ── health ─────────────────────────────────────────────────────────────
    def health_line(self) -> tuple[str, float | None]:
        now = self.now()
        self.store.ensure_day(now)
        c = self.store.counters
        rss = self.rss_fn()
        last = self.engine.last_message_at.isoformat(timespec="seconds") if (self.engine and self.engine.last_message_at) else "-"
        return (f"{TAG} HEALTH LOGGER_RUNNING=1 WS_CONNECTED={int(self.ws_connected)} LAST_MESSAGE_AT={last} "
                f"DATA_DIR={self.root} TODAY_RAW_COUNT={c.get('raw', 0)} TODAY_SNAPSHOT_COUNT={c.get('snapshot', 0)} "
                f"RECONNECT_COUNT={c.get('reconnect', 0)} GAP_EVENTS={c.get('gap_events', 0)} "
                f"MAX_GAP_S={c.get('max_gap_s', 0.0)} IN_SESSION={int(in_session(now))} "
                f"RSS_MB={'-' if rss is None else f'{rss:.1f}'} MAX_RSS_MB={self.max_rss_mb:.0f}"), rss

    def health_tick(self):
        line, rss = self.health_line()
        self.out(line)
        now = self.now()
        self.store.log(now, "health", ws_connected=self.ws_connected, rss_mb=rss, max_rss_mb=self.max_rss_mb,
                       counters=dict(self.store.counters))
        self.store.save_counters()
        if rss is not None and rss > self.max_rss_mb:
            self.out(f"{TAG} RSS_LIMIT_EXCEEDED rss={rss:.1f}MB > max={self.max_rss_mb:.0f}MB — logger only exits "
                     f"(UI/auto-trade unaffected)")
            self.store.log(now, "rss_limit_exceeded", rss_mb=rss, max_rss_mb=self.max_rss_mb)
            self.request_stop("rss_limit", config.EXIT_RSS)

    async def _health_loop(self):
        while not self._stop.is_set():
            try:
                self.health_tick()
            except Exception as e:  # noqa: BLE001 — health 실패로 수집을 멈추지 않는다
                self.out(f"{TAG} HEALTH_ERROR {e!r}"[:300])
            if await self._sleep(self.health_every):
                return

    # ── 수집 ───────────────────────────────────────────────────────────────
    def _on_connected(self, now: datetime):
        self.store.ensure_day(now)
        c = self.store.counters
        c["connects"] = c.get("connects", 0) + 1
        if c["connects"] > 1:
            c["reconnect"] = c.get("reconnect", 0) + 1
            self.store.log(now, "reconnect", reconnect_count=c["reconnect"])
            self.out(f"{TAG} RECONNECTED count={c['reconnect']}")
        else:
            self.out(f"{TAG} CONNECTED")
        self.store.log(now, "connected", subs=list(config.SUBSCRIPTIONS))
        self.store.save_counters()

    async def collect_session(self):
        """수집시간이 끝나거나 stop 이 올 때까지. 끊기면 백오프 재접속."""
        self.engine = Engine(self.store, symbols())
        backoff = 1.0
        while not self._stop.is_set() and in_session(self.now()):
            try:
                key = self.issue(self.creds, self.root, self.now())
                async with self.connect(config.WS_URL) as ws:
                    for tr, sym in config.SUBSCRIPTIONS:
                        await ws.send(subscribe_message(key, tr, sym))
                        await asyncio.sleep(0.2)
                    self.ws_connected = True
                    self._on_connected(self.now())
                    backoff = 1.0
                    last_flush = self.now()
                    while not self._stop.is_set():
                        now = self.now()
                        if not in_session(now):
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
                        except asyncio.TimeoutError:
                            raw = None
                        now = self.now()
                        if raw is not None:
                            msg = self.engine.on_frame(raw if isinstance(raw, str) else raw.decode("utf-8"), now)
                            if msg.get("type") == "pingpong":
                                await ws.pong(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
                        self.engine.advance(now)
                        if (now - last_flush).total_seconds() >= 1.0:
                            self.store.flush()
                            last_flush = now
                self.ws_connected = False
                self.store.flush()
            except OSError as e:
                if self._is_disk_error(e):
                    raise
                await self._on_disconnect(e, backoff)
                backoff = min(self.backoff_max, backoff * 2)
            except Exception as e:  # noqa: BLE001 — 끊김/네트워크/접속키 오류는 기록 후 재접속
                await self._on_disconnect(e, backoff)
                backoff = min(self.backoff_max, backoff * 2)
        self.ws_connected = False
        self.store.flush()
        self.store.save_counters()
        if not self._stop.is_set():
            self.store.log(self.now(), "session_end", counts=self.engine.counts)
            self.out(f"{TAG} SESSION_END counts={self.engine.counts}")

    _DISK_ERRNOS = {errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EPERM, getattr(errno, "EDQUOT", -1), errno.EIO}

    def _is_disk_error(self, e: OSError) -> bool:
        fn = str(getattr(e, "filename", "") or "")
        if fn and Path(fn).resolve().is_relative_to(self.root.resolve()):
            return True
        return getattr(e, "errno", None) in self._DISK_ERRNOS

    async def _on_disconnect(self, e: Exception, backoff: float):
        self.ws_connected = False
        now = self.now()
        self.out(f"{TAG} DISCONNECTED {e!r}"[:300] + f" retry_in={backoff:.0f}s")
        try:
            self.store.flush()
            self.store.log(now, "disconnect", error=repr(e)[:300], backoff=backoff)
        except OSError:
            pass
        await self._sleep(backoff)

    # ── 장외 ───────────────────────────────────────────────────────────────
    def maintain_if_due(self, now: datetime):
        if in_session(now) or now.time() < config.SESSION_END and now.weekday() < 5 and now.time() >= dtime(7, 50):
            return None             # 수집 직전/중에는 하지 않는다
        mark = (day_str(now), now.time() >= config.SESSION_END)     # 하루 두 번: 장전(어제분) / 장후(오늘분)
        if self._maintained_day == mark:
            return None
        res = maintenance.run(self.root, now)
        self._maintained_day = mark
        if res.get("gzipped") or res.get("daily"):
            self.store.log(now, "maintenance", **res)
            self.out(f"{TAG} MAINTENANCE gzipped={len(res.get('gzipped', []))} daily={len(res.get('daily', []))}")
        return res

    async def run(self) -> int:
        self._install_signals()
        now = self.now()
        self.store.ensure_day(now)
        self.store.log(now, "start", fp=self.creds.fingerprint, subs=list(config.SUBSCRIPTIONS),
                       max_rss_mb=self.max_rss_mb, rss_source=self.rss_source)
        health = asyncio.create_task(self._health_loop())
        try:
            while not self._stop.is_set():
                now = self.now()
                if in_session(now):
                    await self.collect_session()
                    self.store.close()      # 세션 사이엔 파일 핸들도 놓는다
                else:
                    self.maintain_if_due(now)
                    await self._sleep(self.idle_poll)
        except OSError as e:
            self.out(f"{TAG} DISK_ERROR {e!r} — logger exits (UI/auto-trade unaffected)"[:400])
            self.exit_code = config.EXIT_DISK
        finally:
            self._stop.set()
            health.cancel()
            try:
                await health
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            try:
                self.store.log(self.now(), "stop", exit_code=self.exit_code,
                               counts=(self.engine.counts if self.engine else {}))
                self.store.close()
            except OSError:
                pass
            self.out(f"{TAG} STOPPED exit_code={self.exit_code}")
        return self.exit_code


def run_service(svc: LoggerService) -> int:
    try:
        return asyncio.run(svc.run())
    except KeyboardInterrupt:
        return config.EXIT_OK
    except Exception as e:  # noqa: BLE001 — 예상 못한 오류: 래퍼가 백오프 후 재시작한다
        print(f"{TAG} FATAL {e!r}"[:400], file=sys.stderr, flush=True)
        return 1
