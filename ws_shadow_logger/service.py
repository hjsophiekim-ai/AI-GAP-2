"""상주 로거 서비스 — Render Web Service 안에서 UI 와 **별도 프로세스**로 항상 돈다.

  · 프로세스는 계속 살아 있고, KST 평일 08:00~15:30 에만 WebSocket 에 붙어 구독/저장한다.
  · 장외에는 연결 없이 idle 하고, 15:30 이후 한 번 maintenance(gzip/일별 요약)를 돌린다.
  · 끊기면 지수 백오프(최대 60초)로 재접속 — 접속키는 로거 전용 key 로 필요할 때만 재발급(하루 캐시).
  · 1분마다 HEALTH 한 줄을 stdout(Render Logs)과 logs/ 에 남기고 RSS 를 잰다.
    RSS 가 한도를 넘으면 로거만 종료한다(종료코드 4, 래퍼는 재시작하지 않는다).
  · SIGTERM/SIGINT 를 받으면 연결을 닫고 파일을 flush/close 한 뒤 0 으로 끝난다.

  · MU 체결(HDFSCNT0 RBAQMU/DNASMU)과 MU 1호가(HDFSASP0 RBAQMU/DNASMU)를 같은 세션에 국내 6건
    **뒤에** 등록한다(체결 먼저, 호가 나중). 두 기록 경로의 오류는 Engine 이 **각각** 삼키고,
    MU 를 붙인 뒤 연결이 연속으로 금방 끊기면 그날은 MU(체결+호가) 등록을 빼고 국내만 다시 붙는다.
  · MU watchdog(2026-10-21): 장중 RBAQMU 체결·호가가 둘 다 멈추면 RBAQMU 2건만 해제·재등록한다.
    전체 재접속은 하지 않는다 — 국내 6건과 DNASMU 는 그대로다. 구간은 mu/<날짜>/watchdog.json 에 남는다.

자동매매 / 신규진입 허용 / 전략모드 / UI 세션 상태는 읽지 않는다 — 실행 조건은 시간뿐이다.
주문 API, REST 접근토큰, production 모듈은 import 하지 않는다.
"""
from __future__ import annotations

import asyncio
import errno
import json
import signal
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from . import approval, config, maintenance
from .engine import Engine
from .mu_quote import feed_of
from .mu_watchdog import QUOTE_TR, TRADE_TR, MuWatchdog
from .parser import subscribe_message, unsubscribe_message
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
        self.mu_on = config.mu_enabled()
        self.mu_quote_on = config.mu_quote_enabled()
        self._mu_off_day = None          # MU 를 그날만 뺀 날짜
        self._mu_short = 0               # MU 등록 뒤 연속으로 짧게 끝난 연결 수
        self.mu_sub = {}                 # HDFSCNT0 tr_key -> "OK" / "FAIL(msg_cd)" (가장 최근 구독응답)
        self.mu_quote_sub = {}           # HDFSASP0 tr_key -> 같은 형식
        self.wd: MuWatchdog | None = None   # MU watchdog (그날 것) — MU 체결+호가가 둘 다 켜진 날만
        self._wd_day = None
        self._sub_registry = {}          # (tr_id, tr_key) -> "1"(등록) / "2"(해제) — 이 연결에서 마지막으로 보낸 것

    def subscriptions(self, now: datetime | None = None) -> tuple:
        now = now or self.now()
        return config.all_subscriptions(mu=self.mu_on and self._mu_off_day != day_str(now),
                                        mu_quote=self.mu_quote_on)

    def _note_connection_end(self, started: datetime, subs: tuple):
        """MU 를 포함한 연결이 MU_SHORT_SESSION_SEC 안에 끝나기를 연속 MU_MAX_SHORT_SESSIONS 번 -> 그날 MU 제외.
        체결·호가는 한 묶음으로 본다 — 둘 다 빼고 국내 6건만 다시 붙는다."""
        if not any(tr in config.MU_TR_IDS for tr, _ in subs):
            return
        now = self.now()
        if (now - started).total_seconds() >= config.MU_SHORT_SESSION_SEC:
            self._mu_short = 0
            return
        self._mu_short += 1
        if self._mu_short >= config.MU_MAX_SHORT_SESSIONS:
            self._mu_off_day = day_str(now)
            self._mu_short = 0
            self.out(f"{TAG} MU_DISABLED_TODAY reason=short_sessions — domestic subscriptions continue")
            try:
                self.store.log(now, "mu_disabled", reason="short_sessions")
            except OSError:
                pass

    # ── MU watchdog ────────────────────────────────────────────────────────
    def _watchdog(self, now: datetime) -> MuWatchdog | None:
        """그날의 watchdog. MU 체결·호가가 둘 다 등록되는 날에만 존재한다(꺼진 날은 None = 판정 없음)."""
        if not (config.mu_watchdog_enabled() and self.mu_on and self.mu_quote_on
                and self._mu_off_day != day_str(now)):
            return None
        day = day_str(now)
        if self._wd_day != day:
            self.wd = MuWatchdog(feed=config.MU_WATCHDOG_FEED, stale_sec=config.mu_stale_sec(),
                                 retry_delays=config.MU_RESUB_DELAYS, retry_every=config.MU_RESUB_EVERY,
                                 max_attempts=config.MU_RESUB_MAX, reply_timeout=config.MU_RESUB_REPLY_TIMEOUT,
                                 watch_start=config.MU_WATCH_START, watch_end=config.MU_WATCH_END)
            try:                         # 같은 날 재시작 — 앞서 닫힌 구간을 이어 붙인다
                old = json.loads(self._wd_path(day).read_text(encoding="utf-8"))
                self.wd.intervals = [iv for iv in old.get("mu_stale_intervals", []) if iv.get("stale_end")]
                self.wd.resubscribe_total = int(old.get("resubscribe_count") or 0)
            except (OSError, ValueError):
                pass
            self._wd_day = day
        return self.wd

    def _wd_path(self, day: str) -> Path:
        return self.root / "mu" / day / "watchdog.json"

    def _wd_emit(self, events: list, now: datetime):
        if not events:
            return
        for e in events:
            kv = " ".join(f"{k}={v}" for k, v in e.items() if k != "kind")
            self.out(f"{TAG}[MU_WATCHDOG] {e['kind']} {kv}"[:600])
            try:
                self.store.log(now, "mu_watchdog", **e)
            except OSError:
                pass
        self._wd_save(now)

    def _wd_save(self, now: datetime):
        if self.wd is None:
            return
        p = self._wd_path(day_str(now))
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({**self.wd.summary(), "saved_at": now.isoformat(timespec="seconds")},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            pass

    async def _wd_resubscribe(self, ws, key: str, feed: str):
        """해당 feed 의 체결·호가 2건만 해제 -> 재등록. registry 로 중복 등록을 막는다:
        '등록' 상태면 먼저 해제를 보내고, 해제가 나간 뒤에만 다시 등록한다."""
        for tr in (TRADE_TR, QUOTE_TR):
            k = (tr, feed)
            if self._sub_registry.get(k) == "1":
                await ws.send(unsubscribe_message(key, tr, feed))
                self._sub_registry[k] = "2"
                await asyncio.sleep(0.2)
            if self._sub_registry.get(k) != "1":
                await ws.send(subscribe_message(key, tr, feed))
                self._sub_registry[k] = "1"
                await asyncio.sleep(0.2)

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
        mu = self.engine.mu if self.engine else None
        mu_last = mu.last_tick_at.isoformat(timespec="seconds") if (mu and mu.last_tick_at) else "-"
        mu_state = ("OFF" if not self.mu_on else "OFF_TODAY" if self._mu_off_day == day_str(now)
                    else "ERR_OFF" if (self.engine and self.engine.mu_disabled) else "ON")
        ob = mu.observe() if mu else {"feeds": {}, "lag": None}
        sub = ",".join(f"{k}:{v}" for k, v in sorted(self.mu_sub.items())) or "-"
        feeds = ",".join(f"{f}:{d['ticks']}t/{d['bars']}b/{d['span']}m/miss={d['missing_ratio']}"
                         for f, d in ob["feeds"].items()) or "-"
        lag = ob["lag"]
        lag_s = f"p50={lag['p50']}s/p95={lag['p95']}s/max={lag['max']}s" if lag else "-"
        muq = self.engine.mu_quote if self.engine else None
        muq_state = ("OFF" if not (self.mu_on and self.mu_quote_on) else "OFF_TODAY"
                     if self._mu_off_day == day_str(now)
                     else "ERR_OFF" if (self.engine and self.engine.mu_quote_disabled) else "ON")
        qob = muq.observe() if muq else {"feeds": {}, "lag": None, "no_rsym": 0}
        qsub = ",".join(f"{k}:{v}" for k, v in sorted(self.mu_quote_sub.items())) or "-"
        qfeeds = ",".join(f"{f}:{d['rows']}q" for f, d in qob["feeds"].items()) or "-"
        qlag = qob["lag"]
        qlag_s = f"p50={qlag['p50']}s/p95={qlag['p95']}s/max={qlag['max']}s" if qlag else "-"
        muq_line = (f"MUQ={muq_state} MUQ_ROWS={muq.counts['quotes'] if muq else 0} "
                    f"MUQ_NO_RSYM={qob['no_rsym']} MUQ_ERRORS={c.get('mu_quote_errors', 0)} "
                    f"MUQ_SUB={qsub} MUQ_FEEDS={qfeeds} MUQ_LAG={qlag_s} ")
        return (f"{TAG} HEALTH LOGGER_RUNNING=1 WS_CONNECTED={int(self.ws_connected)} LAST_MESSAGE_AT={last} "
                f"DATA_DIR={self.root} TODAY_RAW_COUNT={c.get('raw', 0)} TODAY_SNAPSHOT_COUNT={c.get('snapshot', 0)} "
                f"RECONNECT_COUNT={c.get('reconnect', 0)} GAP_EVENTS={c.get('gap_events', 0)} "
                f"MAX_GAP_S={c.get('max_gap_s', 0.0)} IN_SESSION={int(in_session(now))} "
                f"MU={mu_state} MU_LAST_TICK_AT={mu_last} MU_TICKS={mu.counts['ticks'] if mu else 0} "
                f"MU_BARS={mu.counts['bars'] if mu else 0} MU_ERRORS={c.get('mu_errors', 0)} "
                f"MU_SUB={sub} MU_FEEDS={feeds} MU_LAG={lag_s} " + muq_line +
                f"MU_WD={(self.wd.health() if (self.wd and self._wd_day == day_str(now)) else 'OFF')} " +
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
        self.store.log(now, "connected", subs=list(self.subscriptions(now)))
        self.store.save_counters()

    def _on_mu_subscribe_reply(self, msg: dict):
        ok = str(msg.get("rt_cd")) == "0"
        key = msg.get("tr_key") or "?"
        quote = msg.get("tr_id") == "HDFSASP0"
        (self.mu_quote_sub if quote else self.mu_sub)[key] = "OK" if ok else f"FAIL({msg.get('msg_cd')})"
        label = "MU_QUOTE_SUBSCRIBE" if quote else "MU_SUBSCRIBE"
        self.out(f"{TAG} {label} {'OK' if ok else 'FAIL'} tr_key={key} msg_cd={msg.get('msg_cd')} "
                 f"msg1={msg.get('msg1')}")

    async def collect_session(self):
        """수집시간이 끝나거나 stop 이 올 때까지. 끊기면 백오프 재접속."""
        self.engine = Engine(self.store, symbols())
        backoff = 1.0
        while not self._stop.is_set() and in_session(self.now()):
            started, subs = None, ()
            try:
                key = self.issue(self.creds, self.root, self.now())
                subs = self.subscriptions()
                started = self.now()
                async with self.connect(config.WS_URL) as ws:
                    self._sub_registry = {}
                    for tr, sym in subs:
                        await ws.send(subscribe_message(key, tr, sym))
                        self._sub_registry[(tr, sym)] = "1"
                        await asyncio.sleep(0.2)
                    self.ws_connected = True
                    self._on_connected(self.now())
                    wd = self._watchdog(self.now())
                    if wd is not None:
                        wd.on_connected(self.now())
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
                        wd = self._watchdog(now)
                        if raw is not None:
                            msg = self.engine.on_frame(raw if isinstance(raw, str) else raw.decode("utf-8"), now)
                            if msg.get("type") == "system" and msg.get("tr_id") in config.MU_TR_IDS:
                                self._on_mu_subscribe_reply(msg)
                                if wd is not None:
                                    self._wd_emit(wd.note_reply(msg.get("tr_id"), msg.get("tr_key"),
                                                                str(msg.get("rt_cd")) == "0", msg.get("msg1"), now), now)
                            if msg.get("type") == "data" and msg.get("tr_id") in config.MU_TR_IDS and wd is not None:
                                per = {}
                                for rec in msg.get("records") or ():
                                    f = feed_of(rec)
                                    per[f] = per.get(f, 0) + 1
                                for f, n in per.items():
                                    self._wd_emit(wd.note_data(msg["tr_id"], f, now, n), now)
                            if msg.get("type") == "pingpong":
                                await ws.pong(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
                        if wd is not None:
                            ev = wd.poll(now)
                            self._wd_emit(ev, now)
                            for e in ev:
                                if e["kind"] == "RESUBSCRIBE_ATTEMPT":
                                    await self._wd_resubscribe(ws, key, e["feed"])
                        self.engine.advance(now)
                        if (now - last_flush).total_seconds() >= 1.0:
                            self.engine.flush()
                            last_flush = now
                self.ws_connected = False
                self.engine.flush()
            except OSError as e:
                if self._is_disk_error(e):
                    raise
                if started is not None:
                    self._note_connection_end(started, subs)
                await self._on_disconnect(e, backoff)
                backoff = min(self.backoff_max, backoff * 2)
            except Exception as e:  # noqa: BLE001 — 끊김/네트워크/접속키 오류는 기록 후 재접속
                if started is not None:
                    self._note_connection_end(started, subs)
                await self._on_disconnect(e, backoff)
                backoff = min(self.backoff_max, backoff * 2)
        self.ws_connected = False
        self.engine.flush()
        self.store.save_counters()
        if self.wd is not None and self._wd_day == day_str(self.now()):
            self._wd_emit(self.wd.close(self.now(), reason="session_end"), self.now())
            self._wd_save(self.now())
        if not self._stop.is_set():
            self.store.log(self.now(), "session_end", counts=self.engine.counts, mu=self.engine.mu.observe(),
                           mu_quote=self.engine.mu_quote.observe())
            self.out(f"{TAG} SESSION_END counts={self.engine.counts}")
            self.out(f"{TAG} MU_SESSION_END ticks={self.engine.mu.counts['ticks']} bars={self.engine.mu.counts['bars']} "
                     f"errors={self.engine.mu_errors} observe={self.engine.mu.observe()}")
            self.out(f"{TAG} MU_QUOTE_SESSION_END quotes={self.engine.mu_quote.counts['quotes']} "
                     f"no_rsym={self.engine.mu_quote.counts['no_rsym']} errors={self.engine.mu_quote_errors} "
                     f"observe={self.engine.mu_quote.observe()}")

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
        if res.get("gzipped") or res.get("daily") or res.get("mu"):
            self.store.log(now, "maintenance", **res)
            self.out(f"{TAG} MAINTENANCE gzipped={len(res.get('gzipped', []))} daily={len(res.get('daily', []))}")
            for day in res.get("mu", []):
                for name, label in (("summary.json", "MU_DAY_SUMMARY"),
                                    ("quote_summary.json", "MU_QUOTE_DAY_SUMMARY")):
                    p = self.root / "mu" / day / name
                    try:
                        self.out(f"{TAG} {label} {p.read_text(encoding='utf-8').replace(chr(10), '')}"[:1000])
                    except OSError:
                        pass
        return res

    async def run(self) -> int:
        self._install_signals()
        now = self.now()
        self.store.ensure_day(now)
        self.store.log(now, "start", fp=self.creds.fingerprint, subs=list(self.subscriptions(now)),
                       max_rss_mb=self.max_rss_mb, rss_source=self.rss_source)
        health = asyncio.create_task(self._health_loop())
        try:
            while not self._stop.is_set():
                now = self.now()
                if in_session(now):
                    await self.collect_session()
                    self.engine.close()     # MU 진행 중 분봉(complete=0) + 핸들
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
                if self.engine:
                    self.engine.close()
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
