"""MU_MACD market data — KIS overseas WebSocket (official TR_ID=HDFSCNT0,
tr_key="RBAQMU" + "DNASMU" on the same connection, per koreainvestment/
open-trading-api examples_user/overseas_stock/overseas_stock_functions_ws.py)
tick stream, aggregated into real 1-minute OHLCV bars entirely in-process
(no REST minute-chart fallback — REST's EXCD=NAS/BAQ paths are both
confirmed unable to backfill the day session; see the 2026-08-12 research
scratchpad for that verification). See config.py's WS_TR_KEY_EXTENDED note
for why DNASMU was added (pre-10:00 KST warm-up gap) and its scope (warm-up
only -- RBAQMU alone still gates live 10:00-16:00 signals).

Completely separate from app.trading.macd2.market_data.MarketDataService —
no shared instance, no shared file, no shared symbol history. Only the
approval_key REST call reuses app.data_sources.kis_overseas_minute's
credential loader (same underlying KIS account, not macd2-specific).

2026-08-13 fix: get_approval_key() used to call KIS's oauth2/Approval fresh
on every single WS reconnect attempt (every few seconds once a connection
drops), with no caching and no backoff — unlike app.trading.kis_client's
already-hardened REST token (memory+file cache, 5-min buffer) and its
EGW00201 rate-limit backoff, both added after a real 2026-07-16 incident.
A dropped WS connection here could retry-storm oauth2/Approval indefinitely,
plausibly triggering (and then perpetuating) a KIS-side lockout — this
matches the observed symptom of "~40s of ticks then permanently stuck,
restart doesn't recover." approval_key is now cached in-memory + on disk
(same CACHE_DIR convention as kis_overseas_minute's kis_token_{mode}.json)
and reconnects back off exponentially instead of a flat 3s retry.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from app.data_sources import kis_overseas_minute as _kis_overseas
from app.trading.mu_macd import config
from app.utils.data_paths import CACHE_DIR as _APPROVAL_KEY_CACHE_DIR

KST = config.KST

# ── approval_key cache (memory + file) — same day-scoped assumption KIS uses
# for its REST access token; see module docstring above. ────────────────────
_APPROVAL_KEY_CACHE: dict[str, str] = {}
_APPROVAL_KEY_ISSUED_AT: dict[str, datetime] = {}
_APPROVAL_KEY_TTL = timedelta(hours=20)  # comfortably under the ~24h KIS validity, with margin
_APPROVAL_KEY_LOCK = threading.Lock()

# ── reconnect backoff (replaces the old flat 3s retry) ───────────────────────
_WS_RECONNECT_BASE_SEC = 3.0
_WS_RECONNECT_MAX_SEC = 60.0


async def _interruptible_sleep(stop_event: Optional[threading.Event], seconds: float) -> None:
    """Sleep in short increments so a pending stop_event is noticed within
    ~0.5s instead of blocking the full backoff duration (up to
    _WS_RECONNECT_MAX_SEC) — otherwise stop()'s thread.join() would have to
    wait out the whole backoff before the thread could exit."""
    remaining = seconds
    while remaining > 0 and (stop_event is None or not stop_event.is_set()):
        chunk = min(0.5, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


@dataclass
class MinuteBar:
    date: str  # YYYYMMDD (KIS kymd — Korea date)
    minute: str  # HHMM (Korea time)
    open: float
    high: float
    low: float
    close: float
    volume: int


def _approval_key_cache_path(mode: str) -> Path:
    return _APPROVAL_KEY_CACHE_DIR / f"mu_macd_approval_key_{mode}.json"


def _load_approval_key_file_cache(mode: str) -> Optional[str]:
    path = _approval_key_cache_path(mode)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        key = data.get("approval_key")
        issued_at = datetime.fromisoformat(data.get("issued_at", ""))
    except Exception:
        return None
    if not key or datetime.now() >= issued_at + _APPROVAL_KEY_TTL:
        return None
    _APPROVAL_KEY_CACHE[mode] = key
    _APPROVAL_KEY_ISSUED_AT[mode] = issued_at
    return key


def _save_approval_key_file_cache(mode: str, key: str, issued_at: datetime) -> None:
    try:
        _APPROVAL_KEY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _approval_key_cache_path(mode)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps({"approval_key": key, "issued_at": issued_at.isoformat(), "mode": mode}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(path)
    except Exception:
        pass  # cache-write failure is never fatal -- the key itself is still returned


def get_approval_key(mode: str = "real") -> str:
    """Cached (memory + file, ~20h TTL) — a fresh WS reconnect must NEVER
    re-request this on every retry (that retry-storms KIS's oauth2/Approval
    and can trigger/perpetuate a lockout; see module docstring)."""
    with _APPROVAL_KEY_LOCK:
        now = datetime.now()
        cached = _APPROVAL_KEY_CACHE.get(mode)
        if cached and now < _APPROVAL_KEY_ISSUED_AT.get(mode, datetime.min) + _APPROVAL_KEY_TTL:
            return cached

        file_cached = _load_approval_key_file_cache(mode)
        if file_cached:
            return file_cached

        creds = _kis_overseas._load_credentials(mode)
        url = f"{creds['base_url']}/oauth2/Approval"
        body = {"grant_type": "client_credentials", "appkey": creds["app_key"], "secretkey": creds["app_secret"]}
        resp = requests.post(url, data=json.dumps(body), headers={"content-type": "application/json; charset=utf-8"}, timeout=10)
        resp.raise_for_status()
        j = resp.json()
        key = j.get("approval_key")
        if not key:
            raise RuntimeError(f"no approval_key in response: {j}")

        issued_at = now
        _APPROVAL_KEY_CACHE[mode] = key
        _APPROVAL_KEY_ISSUED_AT[mode] = issued_at
        _save_approval_key_file_cache(mode, key, issued_at)
        return key


class MUMarketDataService:
    """mode="mock": no network at all — tests drive it via on_tick()/
    inject_1m_bar() directly. mode="real": start() spawns a background
    thread running the WebSocket subscriber; stop() tears it down cleanly.
    """

    def __init__(self, mode: str = "mock") -> None:
        self.mode = mode
        self._lock = threading.RLock()
        self._current_minute_key: Optional[str] = None
        self._current_bar: Optional[dict] = None
        self._bars: list[MinuteBar] = []
        self._last_tvol: Optional[int] = None
        self._minute_start_tvol: Optional[int] = None

        self.ws_connected = False
        self.ws_last_tick_at: Optional[datetime] = None
        self.ws_last_error: Optional[str] = None
        self.ws_subscribed_at: Optional[datetime] = None
        self.last_price: Optional[float] = None
        self.last_tvol: Optional[int] = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._ws_reconnect_failures = 0

    # ── tick aggregation (pure, unit-testable, shared by real WS handler and mock tests) ──
    def on_tick(self, khms: str, last_price: float, tvol: int, kymd: str, recv_at: Optional[datetime] = None) -> None:
        with self._lock:
            minute_key = khms[:4]  # HHMM (Korea time)
            if minute_key != self._current_minute_key:
                if self._current_bar is not None:
                    self._current_bar["volume"] = max(0, (self._last_tvol or 0) - (self._minute_start_tvol or 0))
                    self._bars.append(MinuteBar(**self._current_bar))
                self._current_minute_key = minute_key
                self._minute_start_tvol = self._last_tvol
                self._current_bar = {
                    "date": kymd, "minute": minute_key,
                    "open": last_price, "high": last_price, "low": last_price, "close": last_price,
                    "volume": 0,
                }
            else:
                self._current_bar["high"] = max(self._current_bar["high"], last_price)
                self._current_bar["low"] = min(self._current_bar["low"], last_price)
                self._current_bar["close"] = last_price
            self._last_tvol = tvol
            self.last_price = last_price
            self.last_tvol = tvol
            self.ws_last_tick_at = recv_at or datetime.now(KST)

    def inject_1m_bar(self, date: str, minute: str, open_: float, high: float, low: float, close: float, volume: int = 0) -> None:
        """Test-only convenience: append one already-finalized 1-min bar
        directly, bypassing tick-by-tick aggregation. Never used by the real
        WebSocket path."""
        with self._lock:
            self._bars.append(MinuteBar(date=date, minute=minute, open=open_, high=high, low=low, close=close, volume=volume))

    def get_history_df(self) -> pd.DataFrame:
        with self._lock:
            bars = list(self._bars)
        if not bars:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        rows = []
        for b in bars:
            dt = pd.Timestamp(f"{b.date[:4]}-{b.date[4:6]}-{b.date[6:]} {b.minute[:2]}:{b.minute[2:]}:00", tz=KST)
            rows.append({"datetime": dt, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume})
        return pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)

    def warmup_bars_1m_count(self) -> int:
        with self._lock:
            return len(self._bars)

    def is_stale(self, now: datetime, max_age_sec: float) -> bool:
        if self.ws_last_tick_at is None:
            return True
        return (now - self.ws_last_tick_at).total_seconds() > max_age_sec

    # ── real WebSocket connection (official spec, no invented TR/endpoint) ──
    def start(self) -> None:
        if self.mode != "real":
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_ws_loop, daemon=True, name="mu-macd-ws")
        self._thread.start()

    def stop(self, join_timeout: float = 6.0) -> None:
        # join_timeout > the 5.0s ws.recv() wait_for below, so a thread
        # blocked in a single recv() at the moment stop() is called still
        # has room to notice _stop_event before start() spins up a new one.
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        self._thread = None
        self.ws_connected = False

    def _run_ws_loop(self) -> None:
        try:
            asyncio.run(self._ws_main())
        except Exception as e:  # pragma: no cover - defensive top-level guard
            self.ws_last_error = repr(e)
            self.ws_connected = False

    async def _ws_main(self) -> None:
        import websockets

        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                approval_key = get_approval_key(self.mode if self.mode == "real" else "real")

                def _subscribe_msg(tr_key: str) -> str:
                    return json.dumps({
                        "header": {"approval_key": approval_key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
                        "body": {"input": {"tr_id": config.WS_TR_ID, "tr_key": tr_key}},
                    })

                async with websockets.connect(config.WS_URL) as ws:
                    # RBAQMU: live day-session (10:00-16:00 KST) -- gates real signals.
                    await ws.send(_subscribe_msg(config.WS_TR_KEY))
                    # DNASMU: pre/after-hours delayed feed on the SAME connection --
                    # warm-up bars only, see config.py's WS_TR_KEY_EXTENDED note.
                    await ws.send(_subscribe_msg(config.WS_TR_KEY_EXTENDED))
                    self.ws_connected = True
                    self.ws_subscribed_at = datetime.now(KST)
                    self._ws_reconnect_failures = 0  # connect+subscribe worked -- approval_key is good
                    while self._stop_event is not None and not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        self._handle_raw_message(raw, ws)
            except Exception as e:
                self.ws_connected = False
                self.ws_last_error = repr(e)
                self._ws_reconnect_failures += 1
                backoff = min(_WS_RECONNECT_BASE_SEC * (2 ** (self._ws_reconnect_failures - 1)), _WS_RECONNECT_MAX_SEC)
                await _interruptible_sleep(self._stop_event, backoff)

    def _handle_raw_message(self, raw: str, ws) -> None:
        if raw and raw[0] in ("0", "1"):
            parts = raw.split("|")
            if len(parts) < 4:
                return
            try:
                df = pd.read_csv(StringIO(parts[3]), header=None, sep="^", names=list(config.WS_COLUMNS), dtype=object)
            except Exception as e:
                self.ws_last_error = f"parse_error: {e!r}"
                return
            for _, r in df.iterrows():
                try:
                    last_price = float(r["LAST"])
                    tvol = int(r["TVOL"])
                    khms = str(r["KHMS"])
                    kymd = str(r["KYMD"])
                except (TypeError, ValueError, KeyError):
                    continue
                self.on_tick(khms, last_price, tvol, kymd, recv_at=datetime.now(KST))
        else:
            try:
                rdic = json.loads(raw)
            except Exception:
                return
            hdr = rdic.get("header", {})
            if hdr.get("tr_id") == "PINGPONG":
                asyncio.ensure_future(ws.pong(raw))
