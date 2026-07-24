"""MACD2 market data service — the ONLY module that calls the KIS network.

Owns bootstrap (prior-day + today 1m history for the signal symbol),
incremental 1m merge, and a 3-symbol quote cache with staleness tracking.
worker.py never calls KIS directly and never triggers the incremental merge
either (docs §8/§11/§13) — it only reads this service's cached snapshots via
get_history_df()/get_quote(). start_history_updater()/start_quote_updater()
are the only two background threads that actually call KIS, each on its own
centralized interval (never per-Worker-tick), so quote polling never
compounds toward a rate limit. A single I/O lock serializes all KIS calls
(the underlying KISClient is not documented thread-safe), and exactly one
KISClient instance is created lazily (_get_kis_client()) and reused for the
lifetime of this service instance — never re-created per call.

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
from app.utils.data_paths import CACHE_DIR

KST = config.KST

_1M_COLUMNS = ("datetime", "open", "high", "low", "close", "volume")

# fetch_minute_candles(mode, symbol, count, hour1) -> (DataFrame[_1M_COLUMNS], diag)
MinuteCandleFetcher = Callable[[str, str, int, str], "tuple[pd.DataFrame, dict[str, Any]]"]
# fetch_minute_candles_for_date(mode, symbol, date_ymd, count, hour1) -> (DataFrame[_1M_COLUMNS], diag)
MinuteCandleForDateFetcher = Callable[[str, str, str, int, str], "tuple[pd.DataFrame, dict[str, Any]]"]
# fetch_quote(mode, symbol) -> (price_or_None, error_or_None)
QuoteFetcher = Callable[[str, str], "tuple[Optional[float], Optional[str]]"]

KIS_PAGE_SIZE = 120
KIS_MAX_PAGES = 6

# Bounds how far back _load_prior_trading_day() searches for the most recent
# actual trading day (docs §21 2026-07-24 warm-up fix: 주말·공휴일이면 과거
# 날짜를 순차 탐색 — bounded so consecutive holidays can never loop forever).
MAX_TRADING_DATE_LOOKBACK_DAYS = 10


def _prior_weekday_candidates(today_ymd: str, max_candidates: int) -> list[str]:
    """Calendar dates before ``today_ymd``, most-recent first, skipping
    Sat/Sun — a cheap first filter only. Actual holiday detection still
    relies on the KIS API returning empty for that date (the caller moves on
    to the next candidate); this list is just a bounded search space."""
    today = datetime.strptime(today_ymd, "%Y%m%d").date()
    out: list[str] = []
    d = today
    guard = 0
    while len(out) < max_candidates and guard < max_candidates * 3:
        d = d - timedelta(days=1)
        guard += 1
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return out


def _empty_1m_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_1M_COLUMNS))


def _load_prior_day_1m_cache(watch_symbol: str, today_ymd: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fallback B (docs §21 2026-07-24 warm-up fix): the prior trading day's
    1m bars for ``watch_symbol`` from a local historical cache
    (``data/cache/naver_multi_1m/{symbol}_1m.csv``) — plain, generic
    market-data for this symbol (not MACD-v1 production code; MACD v1's own
    app.trading.macd_pipeline.market_data reads the same file, but this
    function is an independent, MACD2-only implementation, never an import
    from that module). Only consulted when fallback A (KIS's official
    주식일별분봉조회, ``_fetch_trading_day_candles``) fails to find any prior
    trading day at all — this cache is a fallback, never a requirement: a
    machine that has never run MACD2/collected this cache before must still
    be able to warm up purely from fallback A.
    """
    path = CACHE_DIR / "naver_multi_1m" / f"{watch_symbol}_1m.csv"
    if not path.exists():
        return _empty_1m_frame(), {"path": str(path), "error": "NO_PRIOR_DAY_CACHE", "received_count": 0}
    try:
        raw = pd.read_csv(path)
    except Exception as exc:
        return _empty_1m_frame(), {"path": str(path), "error": repr(exc), "received_count": 0}
    if "datetime" not in raw.columns:
        return _empty_1m_frame(), {"path": str(path), "error": "MALFORMED_CACHE_NO_DATETIME_COLUMN", "received_count": 0}

    raw["datetime"] = pd.to_datetime(raw["datetime"], errors="coerce")
    raw = raw.dropna(subset=["datetime"])
    prior_only = raw[raw["datetime"].dt.strftime("%Y%m%d") < today_ymd]
    if prior_only.empty:
        return _empty_1m_frame(), {"path": str(path), "error": "CACHE_HAS_NO_PRIOR_DAY_ROWS", "received_count": 0}

    prior_trading_date = sorted(prior_only["datetime"].dt.strftime("%Y%m%d").unique())[-1]
    day_df = prior_only[prior_only["datetime"].dt.strftime("%Y%m%d") == prior_trading_date]
    day_df = day_df.sort_values("datetime").reset_index(drop=True)
    day_df["datetime"] = day_df["datetime"].dt.tz_localize(KST)
    day_df = day_df[list(_1M_COLUMNS)]

    return day_df, {
        "path": str(path),
        "prior_trading_date": prior_trading_date,
        "received_count": int(len(day_df)),
        "oldest": day_df["datetime"].iloc[0].isoformat(),
        "newest": day_df["datetime"].iloc[-1].isoformat(),
        "error": None,
    }


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
        fetch_minute_candles_for_date: Optional[MinuteCandleForDateFetcher] = None,
        fetch_quote: Optional[QuoteFetcher] = None,
    ) -> None:
        self.mode = mode
        self._kis_client: Any = None
        self._kis_client_lock = threading.RLock()
        self._fetch_minute_candles = fetch_minute_candles or self._default_fetch_minute_candles
        self._fetch_minute_candles_for_date = (
            fetch_minute_candles_for_date or self._default_fetch_minute_candles_for_date
        )
        self._fetch_quote = fetch_quote or self._default_fetch_quote
        self._io_lock = threading.RLock()  # single KIS I/O lock — no nested pools, no concurrent KIS calls
        self._history_lock = threading.RLock()
        self._quote_lock = threading.RLock()
        self._df_1m: pd.DataFrame = _empty_1m_frame()
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._quote_updater_thread: Optional[threading.Thread] = None
        self._quote_updater_stop = threading.Event()
        self._history_updater_thread: Optional[threading.Thread] = None
        self._history_updater_stop = threading.Event()
        self._last_bootstrap_diag: dict[str, Any] = {}

    def quote_statuses(
        self,
        symbols: tuple[str, ...] = (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL),
    ) -> dict[str, str]:
        statuses: dict[str, str] = {}
        for symbol in symbols:
            snap = self.get_quote(symbol)
            if snap is None:
                statuses[symbol] = "MISSING"
            elif snap.error or snap.price <= 0:
                statuses[symbol] = "ERROR"
            elif snap.age_sec is not None and snap.age_sec > config.QUOTE_MAX_AGE_SEC:
                statuses[symbol] = "STALE"
            else:
                statuses[symbol] = "VALID"
        return statuses

    def quote_status(
        self,
        symbols: tuple[str, ...] = (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL),
    ) -> str:
        if not self.quote_updater_alive():
            return "DEAD"
        statuses = self.quote_statuses(symbols)
        vals = set(statuses.values())
        if vals == {"VALID"}:
            return "READY"
        if "ERROR" in vals or "MISSING" in vals:
            return "PARTIAL_ERROR"
        if "STALE" in vals:
            return "PARTIAL_STALE"
        return "PARTIAL_ERROR"

    def get_last_bootstrap_diag(self) -> dict[str, Any]:
        """Per-request diagnostics from the most recent bootstrap() call —
        prior-day cache load result + every KIS page (date/hour1/count/
        oldest/newest/error). Empty dict before bootstrap() has ever run."""
        return dict(self._last_bootstrap_diag)

    def _get_kis_client(self) -> Any:
        """Exactly one KIS client per service instance (docs: created once at
        service start, reused — never re-created per tick/request). Created
        lazily on the first real network call and cached for every
        subsequent bootstrap/incremental/quote call this instance makes."""
        with self._kis_client_lock:
            if self._kis_client is None:
                from app.trading.kis_client import create_kis_client

                self._kis_client = create_kis_client(self.mode if self.mode in ("mock", "real") else "mock")
            return self._kis_client

    def _default_fetch_minute_candles(self, mode: str, symbol: str, count: int, hour1: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Real KIS call — the one and only network entry point for minute bars."""
        del mode  # self.mode already selected the shared client via _get_kis_client()
        client = self._get_kis_client()
        if client is None:
            return _empty_1m_frame(), {"error": "kis_client_none"}
        try:
            candles = client.get_minute_candles(symbol, period_min=1, count=count, hour1=hour1) or []
        except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
            return _empty_1m_frame(), {"error": repr(exc)}
        df = _candles_to_df(candles)
        return df, {"received_count": int(len(df))}

    def _default_fetch_minute_candles_for_date(
        self, mode: str, symbol: str, date_ymd: str, count: int, hour1: str,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Real KIS call — 주식일별분봉조회 (fallback A), the only network
        entry point that can return a SPECIFIC calendar date's minute bars."""
        del mode
        try:
            client = self._get_kis_client()
            if client is None:
                return _empty_1m_frame(), {"error": "kis_client_none"}
            candles = client.get_minute_candles_for_date(symbol, date_ymd, period_min=1, count=count, hour1=hour1) or []
        except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
            return _empty_1m_frame(), {"error": repr(exc)}
        df = _candles_to_df(candles)
        return df, {"received_count": int(len(df))}

    def _default_fetch_quote(self, mode: str, symbol: str) -> tuple[Optional[float], Optional[str]]:
        """Real KIS call — the one and only network entry point for a live quote."""
        del mode
        client = self._get_kis_client()
        if client is None:
            return None, "kis_client_none"
        try:
            result = client.get_current_price(symbol)
            return (float(result["current_price"]) if result else None), None
        except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
            return None, repr(exc)

    # ── history (bootstrap + incremental) ──────────────────────────────

    def _fetch_trading_day_candles(self, date_ymd: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        """One full page-backwards walk of KIS's 주식일별분봉조회 for a SINGLE
        specific calendar date — same no-growth/cursor-stuck bounded loop as
        the live today-only walk, just against the date-scoped endpoint."""
        pages: list[pd.DataFrame] = []
        page_diags: list[dict[str, Any]] = []
        hour1 = ""
        prev_count = 0
        for page_i in range(KIS_MAX_PAGES):
            with self._io_lock:
                part, _diag = self._fetch_minute_candles_for_date(
                    self.mode, config.WATCH_SYMBOL, date_ymd, KIS_PAGE_SIZE, hour1,
                )
            page_diags.append({
                "request_no": page_i + 1, "requested_date": date_ymd,
                "requested_hour1": hour1 or "LATEST", "received_count": int(len(part)),
                "oldest": part["datetime"].iloc[0].isoformat() if not part.empty else None,
                "newest": part["datetime"].iloc[-1].isoformat() if not part.empty else None,
                "error": _diag.get("error"),
            })
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
                page_diags[-1]["stop_reason"] = "PAGE_NO_GROWTH"
                break
            prev_count = len(merged)
            oldest = merged["datetime"].iloc[0]
            next_hour1 = (oldest - timedelta(minutes=1)).strftime("%H%M%S")
            if next_hour1 == hour1:
                page_diags[-1]["stop_reason"] = "CURSOR_NOT_MOVING"
                break
            hour1 = next_hour1

        df = (
            pd.concat(pages, ignore_index=True)
            .drop_duplicates(subset=["datetime"], keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
            if pages else _empty_1m_frame()
        )
        return df, {"date": date_ymd, "pages": page_diags, "received_count": int(len(df))}

    def _load_prior_trading_day(self, today_ymd: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Fallback chain (docs §21 2026-07-24 warm-up fix) — never requires
        a previous MACD2 run or a pre-existing local cache:

          A. KIS 주식일별분봉조회 (``_fetch_trading_day_candles``) for each
             candidate weekday before today, most recent first, stopping at
             the first date with any data (a real trading day — a weekday
             holiday simply returns empty and the search moves on). Bounded
             by MAX_TRADING_DATE_LOOKBACK_DAYS so consecutive holidays can
             never loop forever.
          B. The local persistent cache (``_load_prior_day_1m_cache``) —
             only consulted if every candidate in A came back empty.
          C. Neither succeeded — caller reports TODAY_ONLY_WARMING_UP, not a
             hard error; today's own bars keep accumulating regardless.
        """
        candidates = _prior_weekday_candidates(today_ymd, MAX_TRADING_DATE_LOOKBACK_DAYS)
        attempts: list[dict[str, Any]] = []
        for date_ymd in candidates:
            df, diag = self._fetch_trading_day_candles(date_ymd)
            attempts.append(diag)
            if not df.empty:
                return df, {
                    "source": "KIS_DAILY_MINUTE_CHART", "selected_date": date_ymd,
                    "candidates_tried": len(attempts), "attempts": attempts,
                    "received_count": int(len(df)),
                    "oldest": df["datetime"].iloc[0].isoformat(), "newest": df["datetime"].iloc[-1].isoformat(),
                }

        cache_df, cache_diag = _load_prior_day_1m_cache(config.WATCH_SYMBOL, today_ymd)
        if not cache_df.empty:
            return cache_df, {
                "source": "PERSISTENT_CACHE", "candidates_tried": len(attempts),
                "attempts": attempts, "cache": cache_diag, "received_count": int(len(cache_df)),
            }

        return _empty_1m_frame(), {
            "source": "NONE", "candidates_tried": len(attempts), "attempts": attempts,
            "cache": cache_diag, "received_count": 0,
        }

    def bootstrap(self, now: Optional[datetime] = None) -> BootstrapResult:
        """Once on Start: the most recent actual trading day's 1m bars (see
        ``_load_prior_trading_day`` — KIS's official 주식일별분봉조회 first,
        local cache only as a fallback, docs §21) + today's 1m bars paged
        live from KIS (inquire-time-itemchartprice has no date parameter and
        only ever returns TODAY, no matter what ``hour1`` cursor is sent),
        merged into >=300 1m bars including prior day and >=100 completed
        3m bars. A prior day that could not be found at all (fallback A and B
        both empty) is reported as TODAY_ONLY_WARMING_UP, not a hard error —
        neither a previous MACD2 run nor a pre-existing cache is ever
        required for bootstrap to succeed. Every request is recorded in
        ``get_last_bootstrap_diag()``.
        """
        now = now or datetime.now(KST)
        t0 = datetime.now(KST)
        today_ymd = now.strftime("%Y%m%d")

        prior_df, prior_diag = self._load_prior_trading_day(today_ymd)
        page_diags: list[dict[str, Any]] = []

        pages: list[pd.DataFrame] = []
        hour1 = ""
        prev_count = 0
        for page_i in range(KIS_MAX_PAGES):
            with self._io_lock:
                part, _diag = self._fetch_minute_candles(self.mode, config.WATCH_SYMBOL, KIS_PAGE_SIZE, hour1)
            page_diags.append({
                "request_no": page_i + 1,
                "requested_date": today_ymd,
                "requested_hour1": hour1 or "LATEST",
                "received_count": int(len(part)),
                "oldest": part["datetime"].iloc[0].isoformat() if not part.empty else None,
                "newest": part["datetime"].iloc[-1].isoformat() if not part.empty else None,
                "error": _diag.get("error"),
            })
            if part.empty:
                break
            pages.append(part)
            merged_today = (
                pd.concat(pages, ignore_index=True)
                .drop_duplicates(subset=["datetime"], keep="last")
                .sort_values("datetime")
                .reset_index(drop=True)
            )
            if len(merged_today) <= prev_count:
                page_diags[-1]["stop_reason"] = "PAGE_NO_GROWTH"
                break  # cursor stopped making progress
            prev_count = len(merged_today)
            oldest = merged_today["datetime"].iloc[0]
            next_hour1 = (oldest - timedelta(minutes=1)).strftime("%H%M%S")
            if next_hour1 == hour1:
                page_diags[-1]["stop_reason"] = "CURSOR_NOT_MOVING"
                break  # never repeat an identical request (today-only data would loop forever)
            hour1 = next_hour1

        today_df = (
            pd.concat(pages, ignore_index=True)
            .drop_duplicates(subset=["datetime"], keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
            if pages else _empty_1m_frame()
        )
        _non_empty = [frame for frame in (prior_df, today_df) if not frame.empty]
        df = (
            pd.concat(_non_empty, ignore_index=True)
            .drop_duplicates(subset=["datetime"], keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
            if _non_empty else _empty_1m_frame()
        )
        elapsed = (datetime.now(KST) - t0).total_seconds()

        self._last_bootstrap_diag = {
            "requested_trading_date": today_ymd,
            "prior_trading_day": prior_diag,
            "kis_pages": page_diags,
            "merged_oldest": df["datetime"].iloc[0].isoformat() if not df.empty else None,
            "merged_newest": df["datetime"].iloc[-1].isoformat() if not df.empty else None,
        }

        if df.empty:
            with self._history_lock:
                self._df_1m = df
            return BootstrapResult(False, "NO_1M_BARS", 0, 0, 0, 0, round(elapsed, 3))

        dates = df["datetime"].dt.strftime("%Y%m%d")
        prior_n = int((dates != today_ymd).sum())
        today_n = int((dates == today_ymd).sum())
        bars3 = resample_completed_3m(df, now=now)
        completed_3m_count = int(len(bars3))

        with self._history_lock:
            self._df_1m = df

        if prior_n <= 0:
            # Fallback A (KIS official date-scoped API) and fallback B
            # (persistent cache) both came back empty — not a hard error,
            # today's own bars keep accumulating and a later retry (or the
            # next scheduled bootstrap) may well succeed once more of today
            # has elapsed (docs §21: never require a prior run/cache).
            return BootstrapResult(
                False, "TODAY_ONLY_WARMING_UP", int(len(df)), prior_n, today_n,
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
        updated: dict[str, QuoteSnapshot] = {}
        for symbol in symbols:
            with self._io_lock:
                price, error = self._fetch_quote(self.mode, symbol)
            success = error is None and price is not None and float(price) > 0
            fetched_at = datetime.now(KST)
            if success:
                updated[symbol] = QuoteSnapshot(
                    symbol=symbol, price=float(price), fetched_at=fetched_at, age_sec=0.0, source="kis", error=None,
                )
                continue
            with self._quote_lock:
                previous = self._quotes.get(symbol)
            if previous is not None and previous.price > 0:
                updated[symbol] = QuoteSnapshot(
                    symbol=symbol, price=previous.price, fetched_at=previous.fetched_at,
                    age_sec=(fetched_at - previous.fetched_at).total_seconds(), source=previous.source,
                    error=error or "QUOTE_FETCH_FAILED",
                )
            else:
                updated[symbol] = QuoteSnapshot(
                    symbol=symbol, price=0.0, fetched_at=fetched_at, age_sec=0.0, source="kis",
                    error=error or "QUOTE_FETCH_FAILED",
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

    # ── history updater (background 1m refresh; Worker only reads the cache) ──

    def start_history_updater(self, interval_sec: float = config.WORKER_INTERVAL_SEC) -> None:
        """Background thread that periodically calls merge_incremental_1m() —
        the only place that happens now that worker.py no longer triggers it
        itself (docs: Worker tick에서 KIS network 호출 제거)."""
        if self._history_updater_thread is not None and self._history_updater_thread.is_alive():
            return
        self._history_updater_stop.clear()

        def _loop() -> None:
            while not self._history_updater_stop.is_set():
                try:
                    self.merge_incremental_1m()
                except Exception:
                    pass
                self._history_updater_stop.wait(interval_sec)

        self._history_updater_thread = threading.Thread(target=_loop, daemon=True, name="macd2-history-updater")
        self._history_updater_thread.start()

    def stop_history_updater(self, join_timeout: float = 2.0) -> None:
        self._history_updater_stop.set()
        thread = self._history_updater_thread
        if thread is not None:
            thread.join(timeout=join_timeout)
        self._history_updater_thread = None

    def history_updater_alive(self) -> bool:
        return bool(self._history_updater_thread and self._history_updater_thread.is_alive())
