"""MACD2 cross-process worker ownership lock (2026-08-26 incident fix).

Real incident (2026-08-26): a Render redeploy/restart left TWO live
Macd2Worker-owning processes running concurrently for several minutes
against the SAME mock KIS account. state_store.py's/ledger.py's
LIVE_WORKER_MARKER_ENV only ever proves "I am a genuine worker/service
process" (an env var scoped to this process's own pid) -- it never proved
"I am the ONLY one". Both processes independently detected the same 09:09
UP_RED TW confirmation and both dispatched a BUY with the same signal_id;
both independently rediscovered the other's fills via
worker.reconcile_position_state()'s RECOVERED_FROM_BROKER path, logging
duplicate RECONCILE_DISCOVERED rows a few seconds apart; the resulting
oversized, uncoordinated position (994 + 542 shares stacked from repeated
uncoordinated buys, instead of one budget-sized order) is what actually
produced the day's real loss when the otherwise entirely-correct
TIME_WINDOW_AFTER_TP1_STOP exit closed it out -- it was never a sizing or
exit-logic bug, only ever a "two brains, one account" bug.

This module is the fix: a lease/heartbeat lock file on the SAME Persistent
Disk state directory state_store.py already uses (app.utils.data_paths.
STATE_DIR -- survives redeploys/restarts, unlike container-local disk).

Deliberately a HEARTBEAT LEASE, not an OS advisory lock (flock/fcntl) held
open for a process's entire lifetime -- Render's mounted disk should not be
assumed to give POSIX lock semantics reliably across a container
kill/restart, and a lease that must be periodically RENEWED degrades safely
(a dead process simply stops renewing) where a held OS lock depends
entirely on the OS reliably releasing it on process death.

Staleness is judged ONLY by the wall-clock age of the lock's own
``last_heartbeat_at`` field -- NEVER by comparing PIDs. Render assigns PIDs
per-container; a brand-new container can be handed the exact same PID a
just-killed old container's process had (each is typically pid 1 inside its
own container), so "is this PID still running" can never prove an old lock
is genuinely abandoned, and comparing PIDs at all would be actively
misleading. ``instance_id`` (a fresh uuid4 per Macd2Worker construction,
the SAME id already used as RuntimeState.worker_instance_id elsewhere in
this codebase) is the only thing that identifies WHO currently holds the
lease -- pid/hostname/started_at are recorded purely for human debugging
and are never consulted in any ownership/staleness decision.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config
from app.utils.data_paths import STATE_DIR

LOCK_FILENAME = "macd2_worker_lock.json"
CLAIM_MARKER_SUFFIX = ".claim"

STATE_DIR_PATH: Path = STATE_DIR
LOCK_PATH: Path = STATE_DIR_PATH / LOCK_FILENAME

# A claim marker abandoned mid-takeover (the claiming process died between
# creating it and either writing the new lock or removing the marker again)
# must never permanently wedge every future takeover attempt. That window
# is only ever a handful of fast local file operations, so anything older
# than this is unambiguously abandoned, never a genuine in-flight claim.
CLAIM_MARKER_ABANDONED_AFTER_SEC = 10.0


@dataclass(frozen=True)
class LockInfo:
    instance_id: str
    pid: int
    hostname: str
    started_at: str
    last_heartbeat_at: str


@dataclass(frozen=True)
class LockResult:
    owned: bool
    reason: str
    current: Optional[LockInfo]


def _claim_marker_path() -> Path:
    return LOCK_PATH.with_name(LOCK_PATH.name + CLAIM_MARKER_SUFFIX)


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return ""


def new_instance_id() -> str:
    return uuid.uuid4().hex[:12]


def ensure_paths() -> None:
    STATE_DIR_PATH.mkdir(parents=True, exist_ok=True)


def read_lock() -> Optional[LockInfo]:
    """Never raises -- a missing/corrupt lock file is always treated as
    'unheld', the same way state_store.load_state() recovers a corrupt
    state.json to a fresh default rather than crashing the Worker loop."""
    try:
        raw = LOCK_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data: Any = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LockInfo(
            instance_id=str(data.get("instance_id") or ""),
            pid=int(data.get("pid") or 0),
            hostname=str(data.get("hostname") or ""),
            started_at=str(data.get("started_at") or ""),
            last_heartbeat_at=str(data.get("last_heartbeat_at") or ""),
        )
    except (TypeError, ValueError):
        return None


def _write_lock_atomic(info: LockInfo) -> None:
    ensure_paths()
    tmp = LOCK_PATH.with_suffix(LOCK_PATH.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LOCK_PATH)


def _create_exclusive(info: LockInfo) -> bool:
    """Atomic 'claim only if truly absent' -- O_EXCL is the actual mutual
    exclusion primitive here (two processes racing to create a brand-new
    lock can never both succeed), unlike the plain tmp+os.replace pattern
    _write_lock_atomic uses for a RENEW/TAKEOVER, which only guarantees the
    final file is never a torn/mixed write, not that only one writer wins."""
    ensure_paths()
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(info), ensure_ascii=False, indent=2))
        return True
    except OSError:
        return False


def _age_sec(iso_ts: str, now: datetime) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=config.KST)
    return (now - ts).total_seconds()


def _claim_marker_age_sec(now: datetime) -> Optional[float]:
    try:
        mtime = _claim_marker_path().stat().st_mtime
    except OSError:
        return None
    marker_time = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone(config.KST)
    return (now - marker_time).total_seconds()


def _attempt_takeover(instance_id: str, stale_current: LockInfo, *, now: datetime, stale_after_sec: float) -> bool:
    """Only reached once the caller has already decided ``stale_current``
    looks stale. Uses a SEPARATE claim-marker file (created via the same
    O_EXCL exclusivity _create_exclusive relies on) purely as a mutex around
    the takeover itself -- two processes racing to take over the SAME stale
    lock must not both believe they won."""
    ensure_paths()
    claim_path = _claim_marker_path()
    marker_age = _claim_marker_age_sec(now)
    if marker_age is not None and marker_age > CLAIM_MARKER_ABANDONED_AFTER_SEC:
        try:
            claim_path.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return False  # another process is mid-takeover right now -- back off, retry next tick
    try:
        # Double-check under the claim mutex: the "original" owner may have
        # renewed (still alive, just slow) or someone else may have already
        # completed a takeover between our staleness read and now.
        latest = read_lock()
        if latest is not None and latest.instance_id != stale_current.instance_id:
            return False
        if latest is not None:
            age = _age_sec(latest.last_heartbeat_at, now)
            if age is not None and age < stale_after_sec:
                return False
        info = LockInfo(
            instance_id=instance_id, pid=os.getpid(), hostname=_hostname(),
            started_at=now.isoformat(), last_heartbeat_at=now.isoformat(),
        )
        _write_lock_atomic(info)
        return True
    finally:
        try:
            claim_path.unlink()
        except OSError:
            pass


def try_acquire_or_renew(
    instance_id: str, *, now: Optional[datetime] = None, stale_after_sec: Optional[float] = None,
) -> LockResult:
    """The single entry point every AUTOMATED MACD2 order-dispatch path
    (Macd2Worker._run_loop, Macd2Service._attempt_bootstrap's one-shot
    restart catch-up tick) must call once per tick, BEFORE loading state or
    evaluating any signal.

    Returns ``owned=True`` when ``instance_id`` may proceed to act as the
    sole live worker this tick (it already held the lease and this just
    renewed its heartbeat, or it freshly acquired an unheld or genuinely
    stale one). Returns ``owned=False`` when some OTHER instance_id
    currently holds an unexpired lease -- the caller must sit this tick out
    entirely: no state load, no signal evaluation, no order.

    FAIL-CLOSED CONTRACT: ANY unexpected error while determining or
    claiming the lease (disk I/O error, permission error, an unmounted
    Persistent Disk, whatever) makes this function return ``owned=False``
    -- it is never allowed to raise out to the caller and it never has a
    code path that assumes ownership just because lock state could not be
    determined. This is an explicit, self-contained guarantee of this
    function (not merely relying on the caller happening to wrap it in its
    own try/except) precisely because a silent "couldn't check the lock,
    order anyway" fallback is the one failure mode that would make this
    whole mechanism worthless -- see 2026-08-26 incident.
    """
    try:
        return _try_acquire_or_renew_inner(instance_id, now=now, stale_after_sec=stale_after_sec)
    except Exception as exc:  # noqa: BLE001 -- see FAIL-CLOSED CONTRACT above
        return LockResult(False, f"ERROR:{exc!r}", None)


def _try_acquire_or_renew_inner(
    instance_id: str, *, now: Optional[datetime], stale_after_sec: Optional[float],
) -> LockResult:
    now = now or datetime.now(config.KST)
    stale_after_sec = config.WORKER_LOCK_STALE_AFTER_SEC if stale_after_sec is None else stale_after_sec
    ensure_paths()

    current = read_lock()
    if current is not None and current.instance_id == instance_id:
        renewed = replace(current, last_heartbeat_at=now.isoformat())
        _write_lock_atomic(renewed)
        return LockResult(True, "RENEWED", renewed)

    if current is None:
        info = LockInfo(
            instance_id=instance_id, pid=os.getpid(), hostname=_hostname(),
            started_at=now.isoformat(), last_heartbeat_at=now.isoformat(),
        )
        if _create_exclusive(info):
            return LockResult(True, "ACQUIRED_NEW", info)
        # Lost a race to create the file first -- re-read and fall through
        # to the normal contention/staleness check below.
        current = read_lock()
        if current is not None and current.instance_id == instance_id:
            return LockResult(True, "ACQUIRED_NEW", current)
        if current is None:
            return LockResult(False, "ACQUIRE_RACE_UNREADABLE", None)

    age = _age_sec(current.last_heartbeat_at, now)
    if age is None or age < stale_after_sec:
        return LockResult(False, "HELD_BY_OTHER", current)

    if _attempt_takeover(instance_id, current, now=now, stale_after_sec=stale_after_sec):
        return LockResult(True, "TAKEOVER", read_lock())
    return LockResult(False, "TAKEOVER_RACE_LOST", read_lock())


def is_current_owner(instance_id: str) -> bool:
    """Cheap, read-only re-check for immediately before an actual broker
    order call (docs: defense-in-depth against ownership being lost
    mid-tick -- e.g. this tick's own KIS retry/poll loop ran long enough
    that another instance legitimately took over the lease while this call
    was still in flight). See LockGuardedBroker below, its real caller."""
    current = read_lock()
    return bool(current is not None and current.instance_id == instance_id)


def release(instance_id: str) -> bool:
    """Best-effort clean release on graceful Worker shutdown -- lets the
    NEXT process (if any) take over immediately instead of waiting out
    stale_after_sec. Never removes a lock some OTHER instance_id now holds
    (e.g. it already took over from us for some other reason) -- a
    missing/foreign lock here is a safe no-op, never an error, since the
    heartbeat timeout is always the ultimate backstop regardless."""
    current = read_lock()
    if current is None or current.instance_id != instance_id:
        return False
    try:
        LOCK_PATH.unlink()
        return True
    except OSError:
        return False


class LockGuardedBroker:
    """Transparent broker proxy used ONLY for the automated Macd2Worker tick
    loop / Macd2Service's one-shot restart catch-up tick ("주문 직전
    재확인" -- 2026-08-26 incident follow-up). Every order-PLACING call
    re-checks ``is_current_owner`` immediately before delegating to the real
    broker; every other method (get_positions/get_quote/get_current_price/
    cancel_order/...) passes straight through untouched -- this never alters
    read-only or cleanup broker behavior, only new-order calls.

    Manual/admin UI actions (e.g. "즉시 청산") use the RAW, unwrapped
    broker/adapter and are deliberately never subject to this gate -- an
    operator's explicit emergency action must never be silently dropped
    just because this process instance does not currently hold order
    authority.
    """

    def __init__(self, broker: Any, instance_id: str) -> None:
        self._broker = broker
        self._instance_id = instance_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._broker, name)

    def _refused(self, method: str, symbol: str, side: str, qty: int):
        from app.trading.macd2.broker_adapter import BrokerOrderResult

        return BrokerOrderResult(
            success=False, order_id="", symbol=symbol, side=side,
            requested_qty=int(qty), executed_qty=0, executed_price=0.0,
            message="WORKER_LOCK_NOT_OWNED",
            raw={"block_reason": "WORKER_LOCK_NOT_OWNED", "guarded_method": method},
        )

    def buy_market(self, symbol: str, qty: int, client_order_id: str):
        if not is_current_owner(self._instance_id):
            return self._refused("buy_market", symbol, "BUY", qty)
        return self._broker.buy_market(symbol, qty, client_order_id)

    def buy_ioc_limit(self, symbol: str, qty: int, price: float, client_order_id: str):
        if not is_current_owner(self._instance_id):
            return self._refused("buy_ioc_limit", symbol, "BUY", qty)
        return self._broker.buy_ioc_limit(symbol, qty, price, client_order_id)

    def buy_limit(self, symbol: str, qty: int, price: float, client_order_id: str):
        if not is_current_owner(self._instance_id):
            return self._refused("buy_limit", symbol, "BUY", qty)
        return self._broker.buy_limit(symbol, qty, price, client_order_id)

    def sell_market(self, symbol: str, qty: int, client_order_id: str):
        if not is_current_owner(self._instance_id):
            return self._refused("sell_market", symbol, "SELL", qty)
        return self._broker.sell_market(symbol, qty, client_order_id)
