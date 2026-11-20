"""MACD2 headless supervisor -- UI 세션과 무관한 worker 감시/복구 (2026-11-16).

2026-11-13 실사고: 09:19 장중 재배포로 옛 프로세스가 내려간 뒤, 새 프로세스는
10:17 에 누군가 UI 를 열 때까지 worker 를 띄우지 않았다. worker 자동복구
(``Macd2Service._auto_recover_worker``)와 history 워치독이 모두
``get_snapshot()/supervisor_status()`` -- 즉 Streamlit 페이지 렌더 경로 -- 안에서만
돌았기 때문이다. Streamlit 은 브라우저 세션이 연결돼야 스크립트를 실행한다.

이 모듈은 그 경로를 **웹 프로세스 안의 감시 스레드 하나**에서 주기적으로 돌린다.

* 시작: ``app/web_main.py`` 가 Streamlit 서버보다 먼저 ``ensure_running()`` 을 부른다
  (UI 세션 0개여도 동작). ``streamlit_app.py`` 도 같은 함수를 부르지만 멱등이다.
* 단일 소유: 같은 프로세스의 ``service.get_service()`` 싱글턴만 쓴다. 별도 worker
  프로세스를 만들지 않으며, worker 디스크 lease(worker._claim_worker_lease)도 그대로다.
* 판정: auto_trade_on=True 인데 worker 스레드가 죽었거나(DEAD) tick 이
  SUPERVISOR_WORKER_STALE_SEC 이상 없으면(STALLED) 비정상. 복구 자체는
  ``Macd2Service.supervisor_tick`` 가 기존 정책(MOCK) / REAL 안전정책으로 한다.
* 경보: 비정상이 SUPERVISOR_ALERT_AFTER_SEC(3분) 이상이면 logger.error +
  ``macd2_supervisor_ledger.csv`` + (설정 시) 웹훅. 복구되면 RECOVERED 기록.
* 하트비트: 매 tick ``macd2_supervisor_heartbeat.json`` 을 원자적으로 쓴다 --
  UI 없이도 '감시가 살아 있는지' 를 디스크로 확인할 수 있다.

REAL 확인 기록(``macd2_supervisor.json``): 사람이 UI 확인문구로 REAL 을 시작하면
service.start() 가 ``record_real_confirmation`` 으로 남기고, 중지(stop)나 사람의 MOCK
시작이 지운다. REAL 자동복구는 이 기록이 있을 때만 한다.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time as _time
import traceback
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from app.logger import logger
from app.trading.macd2 import config, ledger, state_store

KST = config.KST

# ── 이벤트 라벨 ────────────────────────────────────────────────────────────
EV_BOOT = "SUPERVISOR_BOOT"
EV_ABNORMAL = "WORKER_ABNORMAL"            # 경보: 3분 이상 비정상
EV_RECOVERED = "WORKER_RECOVERED"          # 비정상 -> 정상
EV_RECOVER_ATTEMPT = "WORKER_RECOVER_ATTEMPT"
EV_RECOVER_RESULT = "WORKER_RECOVER_RESULT"
EV_REAL_ENTRY_RESTORED = "REAL_RECOVERED_ENTRY_RESTORED"
EV_REAL_MONITOR_ONLY = "REAL_RECOVERED_MONITOR_ONLY"   # 경보: 감시만 복구, 신규진입 OFF
EV_REAL_BLOCKED = "REAL_RECOVER_BLOCKED"                # 경보: REAL 복구 불가(UI 필요)
EV_TICK_ERROR = "SUPERVISOR_TICK_ERROR"

VERDICT_IDLE = "IDLE"          # auto_trade_on=False -- 감시만
VERDICT_OK = "OK"
VERDICT_DEAD = "DEAD"
VERDICT_STALLED = "STALLED"

LEDGER_COLUMNS = ["timestamp", "event", "level", "pid", "mode", "verdict", "detail"]

SUPERVISOR_STORE_NAME = "macd2_supervisor.json"
HEARTBEAT_NAME = "macd2_supervisor_heartbeat.json"
LEDGER_NAME = "macd2_supervisor_ledger.csv"

_io_lock = threading.Lock()


# ── 경로 (테스트가 state_store/ledger 경로를 바꾸면 그대로 따라간다) ─────────

def store_path() -> Path:
    return Path(state_store.STATE_DIR_PATH) / SUPERVISOR_STORE_NAME


def heartbeat_path() -> Path:
    return Path(state_store.STATE_DIR_PATH) / HEARTBEAT_NAME


def ledger_path() -> Path:
    return Path(ledger.LOGS_DIR_PATH) / LEDGER_NAME


def _now() -> datetime:
    return datetime.now(KST)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── 이벤트 기록 / 경보 ──────────────────────────────────────────────────────

def record_event(event: str, *, level: str = "INFO", mode: str = "", verdict: str = "",
                 detail: Any = None, alert: bool = False, now: Optional[datetime] = None) -> None:
    """supervisor 원장 1행 + 로그. alert=True 면 logger.error + 웹훅."""
    at = (now or _now()).isoformat()
    detail_s = detail if isinstance(detail, str) else json.dumps(detail or {}, ensure_ascii=False, default=str)
    row = {"timestamp": at, "event": event, "level": "ALERT" if alert else level, "pid": os.getpid(),
           "mode": mode or "", "verdict": verdict or "", "detail": detail_s}
    msg = f"[MACD2][SUPERVISOR] {event} mode={mode} verdict={verdict} {detail_s}"
    if alert:
        logger.error(msg)
    elif level == "WARNING":
        logger.warning(msg)
    else:
        logger.info(msg)
    try:
        path = ledger_path()
        with _io_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with path.open("a", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
                if new:
                    w.writeheader()
                w.writerow(row)
    except Exception:
        logger.exception("[MACD2][SUPERVISOR] 원장 기록 실패")
    if alert:
        _post_webhook(f"[MACD2 경보] {event} ({mode}) {detail_s}", row)


def send_webhook_sync(text: str, row: Optional[dict] = None, *, timeout: float = 10.0) -> tuple[bool, str]:
    """테스트 경보용 동기 전송. (성공여부, 설명). URL 은 출력하지 않는다."""
    url = config.SUPERVISOR_ALERT_WEBHOOK_URL
    if not url:
        return False, "MACD2_ALERT_WEBHOOK_URL 미설정"
    try:
        body = json.dumps({"text": text, "event": row or {}}, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {getattr(resp, 'status', '?')}"
    except Exception as exc:
        return False, repr(exc)[:300]


def _post_webhook(text: str, row: dict) -> None:
    url = config.SUPERVISOR_ALERT_WEBHOOK_URL
    if not url:
        return

    def _send() -> None:
        try:
            body = json.dumps({"text": text, "event": row}, ensure_ascii=False, default=str).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as exc:  # 경보 실패가 감시를 멈추면 안 된다
            logger.warning(f"[MACD2][SUPERVISOR] 웹훅 전송 실패: {exc!r}")

    threading.Thread(target=_send, name="macd2-supervisor-webhook", daemon=True).start()


# ── REAL 확인 기록 ─────────────────────────────────────────────────────────

def record_real_confirmation(*, runtime_real_mode: bool, runtime_enable_real_buy: bool,
                             runtime_enable_real_sell: bool, now: Optional[datetime] = None) -> None:
    """사람이 UI 확인문구로 REAL 을 성공적으로 시작했다는 기록. 확인문구 자체는 저장하지 않는다."""
    now = now or _now()
    with _io_lock:
        data = _read_json(store_path())
        data["real_confirmation"] = {
            "confirmed_at": now.isoformat(),
            "runtime_real_mode": bool(runtime_real_mode),
            "runtime_enable_real_buy": bool(runtime_enable_real_buy),
            "runtime_enable_real_sell": bool(runtime_enable_real_sell),
        }
        _atomic_write_json(store_path(), data)
    record_event("REAL_CONFIRMATION_RECORDED", mode="real", detail=data["real_confirmation"], now=now)


def clear_real_confirmation(reason: str, *, now: Optional[datetime] = None) -> None:
    with _io_lock:
        data = _read_json(store_path())
        had = data.pop("real_confirmation", None)
        if had is None:
            return
        data["real_confirmation_cleared"] = {"at": (now or _now()).isoformat(), "reason": reason}
        _atomic_write_json(store_path(), data)
    record_event("REAL_CONFIRMATION_CLEARED", mode="real", detail={"reason": reason}, now=now)


def load_real_confirmation() -> Optional[dict]:
    return _read_json(store_path()).get("real_confirmation")


def real_confirmation_usable(conf: Optional[dict], now: datetime) -> tuple[bool, str]:
    if not conf:
        return False, "NO_HUMAN_REAL_START_RECORD"
    try:
        at = datetime.fromisoformat(str(conf.get("confirmed_at")))
    except Exception:
        return False, "BAD_REAL_START_RECORD"
    if at.tzinfo is None:
        at = at.replace(tzinfo=KST)
    if now - at > timedelta(days=config.REAL_RECOVER_CONFIRM_MAX_AGE_DAYS):
        return False, f"REAL_START_RECORD_OLDER_THAN_{config.REAL_RECOVER_CONFIRM_MAX_AGE_DAYS}D"
    return True, ""


# ── REAL 신규진입 재개 안전점검 ─────────────────────────────────────────────

def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except Exception:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=KST)


def assess_real_entry_safety(state: Any, now: datetime, *, broker_positions: Optional[list],
                             broker_error: Optional[str], prev_entry_enabled: bool,
                             prev_entry_armed_at: Any) -> tuple[bool, list[str]]:
    """REAL 복구 직후 신규진입을 되살려도 되는가. 하나라도 걸리면 False(감시만).

    broker_positions: 브로커 잔고 중 TRADE_SYMBOLS 만 [(symbol, qty)], 조회 실패면 None.
    """
    reasons: list[str] = []
    if not prev_entry_enabled:
        reasons.append("ENTRY_WAS_OFF")
    armed = _parse_dt(prev_entry_armed_at)
    if prev_entry_enabled and (armed is None or armed.astimezone(KST).date() != now.astimezone(KST).date()):
        reasons.append("ENTRY_NOT_ARMED_TODAY")
    if getattr(state, "pending_buy_order", None):
        reasons.append("PENDING_BUY_ORDER_UNCONFIRMED")
    if getattr(state, "bot_buy_unconfirmed_symbol", None):
        reasons.append("BOT_BUY_UNCONFIRMED")
    safe_exit = getattr(state, "safe_exit", None)
    if isinstance(safe_exit, dict) and str(safe_exit.get("status") or "") in {"SUBMITTED", "PARTIAL"}:
        reasons.append("SAFE_EXIT_IN_FLIGHT")
    last_order = _parse_dt(getattr(state, "last_broker_order_at", None))
    if last_order is not None and (now - last_order).total_seconds() < config.REAL_RECOVER_RECENT_ORDER_GUARD_SEC:
        reasons.append("RECENT_BROKER_ORDER")
    if broker_positions is None:
        reasons.append(f"BROKER_POSITIONS_UNAVAILABLE:{broker_error or ''}")
    else:
        held = [(s, int(q)) for s, q in broker_positions if int(q) > 0]
        pos = getattr(state, "position", None)
        st_sym = getattr(pos, "symbol", None) if pos is not None else None
        st_qty = int(getattr(pos, "quantity", 0) or 0) if pos is not None else 0
        if len(held) > 1:
            reasons.append("MULTIPLE_BROKER_POSITIONS")
        elif len(held) == 1:
            sym, qty = held[0]
            if st_sym != sym or st_qty != qty:
                reasons.append(f"POSITION_MISMATCH:broker={sym}x{qty},state={st_sym}x{st_qty}")
        elif st_sym and st_qty > 0:
            reasons.append(f"POSITION_MISMATCH:broker=FLAT,state={st_sym}x{st_qty}")
    return (not reasons), reasons


# ── 감시 스레드 ────────────────────────────────────────────────────────────

def supervisor_enabled() -> bool:
    raw = os.getenv(config.SUPERVISOR_ENABLED_ENV)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return bool(os.getenv("RENDER"))   # Render 는 모든 서비스에 RENDER=true 를 넣는다


class _AlertTracker:
    """비정상 지속시간 추적 -> 3분 경보 / 반복 / 복구 기록."""

    def __init__(self) -> None:
        self.abnormal_since: Optional[datetime] = None
        self.abnormal_verdict: str = ""
        self.last_alert_at: Optional[datetime] = None
        self.alert_count: int = 0

    def observe(self, verdict: str, mode: str, now: datetime, detail: dict) -> None:
        abnormal = verdict in (VERDICT_DEAD, VERDICT_STALLED)
        if abnormal:
            if self.abnormal_since is None:
                self.abnormal_since = now
                self.abnormal_verdict = verdict
                record_event("WORKER_ABNORMAL_DETECTED", level="WARNING", mode=mode, verdict=verdict,
                             detail=detail, now=now)
            dur = (now - self.abnormal_since).total_seconds()
            due = dur >= config.SUPERVISOR_ALERT_AFTER_SEC and (
                self.last_alert_at is None
                or (now - self.last_alert_at).total_seconds() >= config.SUPERVISOR_ALERT_REPEAT_SEC)
            if due:
                self.last_alert_at = now
                self.alert_count += 1
                record_event(EV_ABNORMAL, mode=mode, verdict=verdict, alert=True, now=now,
                             detail={**detail, "abnormal_since": self.abnormal_since.isoformat(),
                                     "duration_sec": round(dur, 1), "alert_no": self.alert_count})
            return
        if self.abnormal_since is not None:
            dur = (now - self.abnormal_since).total_seconds()
            record_event(EV_RECOVERED, level="WARNING", mode=mode, verdict=verdict, now=now,
                         alert=self.alert_count > 0,
                         detail={**detail, "abnormal_since": self.abnormal_since.isoformat(),
                                 "duration_sec": round(dur, 1), "was": self.abnormal_verdict})
        self.abnormal_since = None
        self.abnormal_verdict = ""
        self.last_alert_at = None
        self.alert_count = 0


class SupervisorThread:
    def __init__(self, *, boot_reason: str, interval_sec: Optional[float] = None,
                 boot_delay_sec: Optional[float] = None,
                 service_getter: Optional[Callable[[], Any]] = None) -> None:
        self.boot_reason = boot_reason
        self.interval_sec = float(interval_sec if interval_sec is not None else config.SUPERVISOR_INTERVAL_SEC)
        self.boot_delay_sec = float(boot_delay_sec if boot_delay_sec is not None else config.SUPERVISOR_BOOT_DELAY_SEC)
        self._service_getter = service_getter
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.boot_at = _now()
        self.tick_n = 0
        self.error_n = 0
        self.last_tick_at: Optional[datetime] = None
        self.last_result: dict = {}
        self.tracker = _AlertTracker()
        # 2026-11-20: 데이터 진행 경보(완성 3분봉 정체·입력 계약·평가 0회)
        from app.trading.macd2.data_watch import DataWatchTracker
        self.data_tracker = DataWatchTracker(record_event)

    def _service(self):
        if self._service_getter is not None:
            return self._service_getter()
        from app.trading.macd2.service import get_service
        return get_service()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="macd2-supervisor", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=join_timeout)

    def _run(self) -> None:
        record_event(EV_BOOT, detail={"boot_reason": self.boot_reason, "interval_sec": self.interval_sec,
                                      "code_sha": os.getenv("RENDER_GIT_COMMIT", "")[:7],
                                      "webhook_configured": bool(config.SUPERVISOR_ALERT_WEBHOOK_URL)})
        if not config.SUPERVISOR_ALERT_WEBHOOK_URL:
            # 2026-11-20: 경보가 로그/원장에만 남으면 사람이 모른다(11/19). 부팅마다 크게 남긴다.
            record_event("WEBHOOK_NOT_CONFIGURED", level="WARNING",
                         detail={"env": "MACD2_ALERT_WEBHOOK_URL",
                                 "effect": "경보가 logger.error + supervisor 원장에만 남는다"})
        if self._stop.wait(self.boot_delay_sec):
            return
        while not self._stop.is_set():
            started = _time.monotonic()
            self.tick_once()
            self._stop.wait(max(min(0.5, self.interval_sec), self.interval_sec - (_time.monotonic() - started)))

    def tick_once(self, now: Optional[datetime] = None) -> dict:
        """한 번 감시. 어떤 예외도 스레드를 죽이지 않는다."""
        now = now or _now()
        try:
            result = self._service().supervisor_tick(now=now)
        except Exception as exc:
            self.error_n += 1
            result = {"verdict": "ERROR", "error": repr(exc)}
            record_event(EV_TICK_ERROR, level="WARNING", now=now,
                         detail={"error": repr(exc), "trace": traceback.format_exc()[-1500:]})
        self.tick_n += 1
        self.last_tick_at = now
        self.last_result = result
        verdict = str(result.get("verdict") or "")
        if verdict != "ERROR":
            self.tracker.observe(verdict, str(result.get("mode") or ""), now,
                                 {k: result.get(k) for k in ("worker_alive", "last_tick_age_sec",
                                                              "recover_result", "worker_instance_id")})
        if verdict != "ERROR":
            try:
                self.data_tracker.observe(dict(result.get("data_watch") or {}),
                                          str(result.get("mode") or ""), now)
            except Exception as exc:  # 경보 실패가 감시를 멈추면 안 된다
                logger.warning(f"[MACD2][SUPERVISOR] 데이터 경보 처리 실패: {exc!r}")
        self._write_heartbeat(now)
        return result

    def _write_heartbeat(self, now: datetime) -> None:
        r = self.last_result
        payload = {
            "pid": os.getpid(), "boot_reason": self.boot_reason, "boot_at": self.boot_at.isoformat(),
            "tick_n": self.tick_n, "error_n": self.error_n, "last_tick_at": now.isoformat(),
            "interval_sec": self.interval_sec, "verdict": r.get("verdict"), "mode": r.get("mode"),
            "auto_trade_on": r.get("auto_trade_on"), "worker_alive": r.get("worker_alive"),
            "worker_instance_id": r.get("worker_instance_id"), "last_tick_age_sec": r.get("last_tick_age_sec"),
            "entry_enabled": r.get("entry_enabled"), "recover_result": r.get("recover_result"),
            "abnormal_since": self.tracker.abnormal_since.isoformat() if self.tracker.abnormal_since else None,
            "alert_count": self.tracker.alert_count,
            "data_alerts_active": sorted(self.data_tracker.active),
        }
        try:
            _atomic_write_json(heartbeat_path(), payload)
        except Exception:
            logger.exception("[MACD2][SUPERVISOR] 하트비트 기록 실패")


_instance: Optional[SupervisorThread] = None
_instance_lock = threading.Lock()


def ensure_running(boot_reason: str, *, force: bool = False, **kwargs: Any) -> Optional[SupervisorThread]:
    """프로세스당 감시 스레드 1개. 이미 살아 있으면 그대로 반환(멱등)."""
    global _instance
    if not force and not supervisor_enabled():
        return None
    with _instance_lock:
        if _instance is not None and _instance.is_alive():
            return _instance
        _instance = SupervisorThread(boot_reason=boot_reason, **kwargs)
        _instance.start()
        return _instance


def current() -> Optional[SupervisorThread]:
    return _instance


def status_snapshot() -> dict:
    inst = _instance
    hb = _read_json(heartbeat_path())
    return {
        "thread_alive": bool(inst and inst.is_alive()),
        "boot_reason": inst.boot_reason if inst else None,
        "tick_n": inst.tick_n if inst else None,
        "last_tick_at": inst.last_tick_at.isoformat() if inst and inst.last_tick_at else hb.get("last_tick_at"),
        "verdict": (inst.last_result.get("verdict") if inst else hb.get("verdict")),
        "abnormal_since": hb.get("abnormal_since"),
        "alert_count": hb.get("alert_count"),
        "real_confirmation": load_real_confirmation(),
    }
