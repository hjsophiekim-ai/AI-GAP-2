"""MU_MACD market data — KIS overseas WebSocket (official TR_ID=HDFSCNT0,
tr_key="RBAQMU", per koreainvestment/open-trading-api examples_user/
overseas_stock/overseas_stock_functions_ws.py) tick stream, aggregated into
real 1-minute OHLCV bars entirely in-process (no REST minute-chart fallback
— REST's EXCD=NAS/BAQ paths are both confirmed unable to backfill the day
session; see the 2026-08-12 research scratchpad for that verification).

Completely separate from app.trading.macd2.market_data.MarketDataService —
no shared instance, no shared file, no shared symbol history. Only the
approval_key REST call reuses app.data_sources.kis_overseas_minute's
credential loader (same underlying KIS account, not macd2-specific).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from typing import Optional

import pandas as pd
import requests

from app.data_sources import kis_overseas_minute as _kis_overseas
from app.trading.mu_macd import config

KST = config.KST


@dataclass
class MinuteBar:
    date: str  # YYYYMMDD (KIS kymd — Korea date)
    minute: str  # HHMM (Korea time)
    open: float
    high: float
    low: float
    close: float
    volume: int


def get_approval_key(mode: str = "real") -> str:
    creds = _kis_overseas._load_credentials(mode)
    url = f"{creds['base_url']}/oauth2/Approval"
    body = {"grant_type": "client_credentials", "appkey": creds["app_key"], "secretkey": creds["app_secret"]}
    resp = requests.post(url, data=json.dumps(body), headers={"content-type": "application/json; charset=utf-8"}, timeout=10)
    resp.raise_for_status()
    j = resp.json()
    key = j.get("approval_key")
    if not key:
        raise RuntimeError(f"no approval_key in response: {j}")
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

    def stop(self, join_timeout: float = 3.0) -> None:
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
                subscribe_msg = {
                    "header": {"approval_key": approval_key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
                    "body": {"input": {"tr_id": config.WS_TR_ID, "tr_key": config.WS_TR_KEY}},
                }
                async with websockets.connect(config.WS_URL) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    self.ws_connected = True
                    self.ws_subscribed_at = datetime.now(KST)
                    while self._stop_event is not None and not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        self._handle_raw_message(raw, ws)
            except Exception as e:
                self.ws_connected = False
                self.ws_last_error = repr(e)
                await asyncio.sleep(3.0)

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
