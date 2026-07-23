"""MACD2 market data service — the ONLY module that calls the KIS network.

Owns bootstrap (prior-day + today 1m history for the signal symbol),
incremental 1m merge, and a 3-symbol quote cache with staleness tracking.
worker.py never calls KIS directly (docs §8/§11/§13) — it only reads this
service's cached snapshots. A single I/O lock serializes all KIS calls (the
underlying KISClient is not documented thread-safe).

Reuses app.trading.kis_client.create_kis_client / KISClient.get_minute_candles
/ get_current_price directly — generic, non-MACD-v1 KIS wrappers per the
2026-07-23 code-reuse audit. Never imports from app.trading.macd_hynix_* or
app.trading.macd_pipeline.*.

Tests must inject a fake ``fetch_minute_candles``/``fetch_quote`` callable —
see tests/macd2/test_market_data.py. Never call the real KIS fetchers there.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.models import QuoteSnapshot
from app.trading.macd2.signal_engine import resample_completed_3m

KST = config.KST

_1M_COLUMNS = ("datetime", "open", "high", "low", "close", "volume")

# fetch_minute_candles(mode, symbol, count, hour1) -> (DataFrame[_1M_COLUMNS], diag)
MinuteCandleFetcher = Callable[[str, str, int, str], "tuple[pd.DataFrame, dict[str, Any]]"]
# fetch_quote(mode, symbol) -> (price_or_None, error_or_None)
QuoteFetcher = Callable[[str, str], "tuple[Optional[float], Optional[str]]"]

KIS_PAGE_SIZE = 120
KIS_MAX_PAGES = 6


def _empty_1m_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_1M_COLUMNS))


def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    rows = []
    for c in candles or []:
        date_raw = str(c.get("date") or "").strip()
        time_raw = str(c.get("time") or "").strip().replace(":", "")
        if len(date_raw) != 8 or len(time_raw) < 6:
            continue
        try:
            dt = datetime.strptime(f"{date_raw}{time_raw[:6]}", "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            continue
        rows.append({
            "datetime": dt,
            "open": float(c.get("open") or 0),
            "high": float(c.get("high") or 0),
            "low": float(c.get("low") or 0),
            "close": float(c.get("close") or 0),
            "volume": int(c.get("volume") or 0),
        })
    if not rows:
        return _empty_1m_frame()
    return (
        pd.DataFrame(rows)
        .drop_duplicates(subset=["datetime"], keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )


def _default_fetch_minute_candles(mode: str, symbol: str, count: int, hour1: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Real KIS call — the one and only network entry point for minute bars."""
    from app.trading.kis_client import create_kis_client

    client = create_kis_client(mode if mode in ("mock", "real") else "mock")
    if client is None:
        return _empty_1m_frame(), {"error": "kis_client_none"}
    try:
        candles = client.get_minute_candles(symbol, period_min=1, count=count, hour1=hour1) or []
    except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
        return _empty_1m_frame(), {"error": repr(exc)}
    df = _candles_to_df(candles)
    return df, {"received_count": int(len(df))}


def _default_fetch_quote(mode: str, symbol: str) -> tuple[Optional[float], Optional[str]]:
    """Real KIS call — the one and only network entry point for a live quote."""
    from app.trading.kis_client import create_kis_client

    client = create_kis_client(mode if mode in ("mock", "real") else "mock")
    if client is None:
        return None, "kis_client_none"
    try:
        result = client.get_current_price(symbol)
        return (float(result["current_price"]) if result else None), None
    except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
        return None, repr(exc)


@dataclass(frozen=True)
class BootstrapResult:
    ok: bool
    reason: Optional[str]
    received_1m_bars: int
    prior_day_1m_bars: int
    today_1m_bars: int
    completed_3m_count: int
    elapsed_sec: float


class MarketDataService:
    """Owns all network I/O for MACD2 market data (docs §8)."""

    def __init__(
        self,
        mode: str = "mock",
        *,
        fetch_minute_candles: Optional[MinuteCandleFetcher] = None,
        fetch_quote: Optional[QuoteFetcher] = None,
    ) -> None:
        self.mode = mode
        self._fetch_minute_candles = fetch_minute_candles or _default_fetch_minute_candles
        self._fetch_quote = fetch_quote or _default_fetch_quote
        self._io_lock = threading.RLock()  # single KIS I/O lock — no nested pools, no concurrent KIS calls
        self._history_lock = threading.RLock()
        self._quote_lock = threading.RLock()
        self._df_1m: pd.DataFrame = _empty_1m_frame()
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._quote_updater_thread: Optional[threading.Thread] = None
        self._quote_updater_stop = threading.Event()

    # ── history (bootstrap + incremental) ──────────────────────────────

    def bootstrap(self, now: Optional[datetime] = None) -> BootstrapResult:
        """Once on Start: page backwards until >=300 1m bars including prior day,
        and >=100 completed 3m bars. TODAY_ONLY data is never reported as ok=True
        (docs §4/§8).
        """
        now = now or datetime.now(KST)
        t0 = datetime.now(KST)
        pages: list[pd.DataFrame] = []
        hour1 = ""
        prev_count = 0
        for _ in range(KIS_MAX_PAGES):
            with self._io_lock:
                part, _diag = self._fetch_minute_candles(self.mode, config.WATCH_SYMBOL, KIS_PAGE_SIZE, hour1)
            if part.empty:
                break
            pages.append(part)
            merged = (
                pd.concat(pages, ignore_index=True)
                .drop_duplicates(subset=["datetime"], keep="last")
                .sort_values("datetime")
                .reset_index(drop=True)
            )
            if len(merged) <= prev_count:
                break  # cursor stopped making progress
            prev_count = len(merged)
            today_ymd = now.strftime("%Y%m%d")
            has_prior = bool((merged["datetime"].dt.strftime("%Y%m%d") != today_ymd).any())
            if len(merged) >= config.WARMUP_1M_BARS_MIN and has_prior:
                break
            oldest = merged["datetime"].iloc[0]
            hour1 = (oldest - timedelta(minutes=1)).strftime("%H%M%S")

        df = (
            pd.concat(pages, ignore_index=True)
            .drop_duplicates(subset=["datetime"], keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
            if pages else _empty_1m_frame()
        )
        elapsed = (datetime.now(KST) - t0).total_seconds()

        if df.empty:
            with self._history_lock:
                self._df_1m = df
            return BootstrapResult(False, "NO_1M_BARS", 0, 0, 0, 0, round(elapsed, 3))

        today_ymd = now.strftime("%Y%m%d")
        dates = df["datetime"].dt.strftime("%Y%m%d")
        prior_n = int((dates != today_ymd).sum())
        today_n = int((dates == today_ymd).sum())
        bars3 = resample_completed_3m(df, now=now)
        completed_3m_count = int(len(bars3))

        with self._history_lock:
            self._df_1m = df

        if prior_n <= 0:
            return BootstrapResult(
                False, "TODAY_ONLY_NO_PRIOR_DAY", int(len(df)), prior_n, today_n,
                completed_3m_count, round(elapsed, 3),
            )
        if len(df) < config.WARMUP_1M_BARS_MIN:
            return BootstrapResult(
                False, f"WARMUP_1M_LT_{config.WARMUP_1M_BARS_MIN}", int(len(df)), prior_n, today_n,
                completed_3m_count, round(elapsed, 3),
            )
        if completed_3m_count < config.WARMUP_3M_BARS_MIN:
            return BootstrapResult(
                False, f"WARMUP_3M_LT_{config.WARMUP_3M_BARS_MIN}", int(len(df)), prior_n, today_n,
                completed_3m_count, round(elapsed, 3),
            )
        return BootstrapResult(True, None, int(len(df)), prior_n, today_n, completed_3m_count, round(elapsed, 3))

    def merge_incremental_1m(self, now: Optional[datetime] = None) -> pd.DataFrame:
        """Latest-page-only merge — never re-walks the full bootstrap history (docs §4)."""
        now = now or datetime.now(KST)
        with self._io_lock:
            live_df, _diag = self._fetch_minute_candles(self.mode, config.WATCH_SYMBOL, 10, "")
        with self._history_lock:
            base = self._df_1m
            if live_df.empty:
                return base.copy()
            merged = (
                pd.concat([base, live_df], ignore_index=True)
                .drop_duplicates(subset=["datetime"], keep="last")
                .sort_values("datetime")
                .reset_index(drop=True)
            )
            self._df_1m = merged
            return merged.copy()

    def get_history_df(self) -> pd.DataFrame:
        with self._history_lock:
            return self._df_1m.copy()

    def clear_history(self) -> None:
        with self._history_lock:
            self._df_1m = _empty_1m_frame()

    # ── quotes ──────────────────────────────────────────────────────────

    def refresh_quotes(
        self,
        symbols: tuple[str, ...] = (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL),
    ) -> dict[str, QuoteSnapshot]:
        now = datetime.now(KST)
        updated: dict[str, QuoteSnapshot] = {}
        for symbol in symbols:
            with self._io_lock:
                price, error = self._fetch_quote(self.mode, symbol)
            updated[symbol] = QuoteSnapshot(
                symbol=symbol, price=float(price) if price is not None else 0.0,
                fetched_at=now, age_sec=0.0, source="kis", error=error,
            )
        with self._quote_lock:
            self._quotes.update(updated)
        return updated

    def get_quote(self, symbol: str) -> Optional[QuoteSnapshot]:
        with self._quote_lock:
            snap = self._quotes.get(symbol)
        if snap is None:
            return None
        age = (datetime.now(KST) - snap.fetched_at).total_seconds()
        return QuoteSnapshot(
            symbol=snap.symbol, price=snap.price, fetched_at=snap.fetched_at,
            age_sec=age, source=snap.source, error=snap.error,
        )

    def clear_quotes(self) -> None:
        with self._quote_lock:
            self._quotes.clear()

    def start_quote_updater(self, interval_sec: float = 1.0) -> None:
        if self._quote_updater_thread is not None and self._quote_updater_thread.is_alive():
            return
        self._quote_updater_stop.clear()

        def _loop() -> None:
            while not self._quote_updater_stop.is_set():
                try:
                    self.refresh_quotes()
                except Exception:
                    pass
                self._quote_updater_stop.wait(interval_sec)

        self._quote_updater_thread = threading.Thread(target=_loop, daemon=True, name="macd2-quote-updater")
        self._quote_updater_thread.start()

    def stop_quote_updater(self, join_timeout: float = 2.0) -> None:
        self._quote_updater_stop.set()
        thread = self._quote_updater_thread
        if thread is not None:
            thread.join(timeout=join_timeout)
        self._quote_updater_thread = None

    def quote_updater_alive(self) -> bool:
        return bool(self._quote_updater_thread and self._quote_updater_thread.is_alive())
