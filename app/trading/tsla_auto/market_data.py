"""TSLA_AUTO market data service — the ONLY module that calls KIS overseas APIs.

Owns bootstrap (prior-day + today 1m history for TSLA), incremental 1m merge,
and a 3-symbol (TSLA/TSLL/TSLZ) quote cache with staleness tracking. worker.py
never calls KIS directly — it only reads this service's cached snapshots via
get_history_df()/get_quote(). Structure mirrors app/trading/macd2/market_data.py
(docs/TSLA_AUTO_COPY_MAP.md — REWRITE_FOR_KIS_OVERSEAS) but every network call
goes through app.trading.tsla_auto.kis_overseas_adapter, never
app.trading.kis_client (domestic) and never app.trading.macd2.*.

Tests must inject fake fetch_minute_candles/fetch_quote callables — never
call the real KIS overseas endpoints in tests.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from app.trading.tsla_auto import config
from app.trading.tsla_auto import market_session
from app.trading.tsla_auto.models import QuoteSnapshot
from app.trading.tsla_auto.signal_engine import resample_completed_3m
from app.utils.data_paths import data_path

ET = config.ET

_1M_COLUMNS = ("datetime", "open", "high", "low", "close", "volume")

MinuteCandleFetcher = Callable[[str, str, int], "tuple[pd.DataFrame, dict[str, Any]]"]
QuoteFetcher = Callable[[str, str], "tuple[Optional[float], Optional[str]]"]

KIS_PAGE_SIZE = 120  # KIS 해외분봉조회 1회 최대 응답건수(확인된 값, docs §KIS 해외 API)
KIS_MAX_PAGES = 4  # 120*4 = 480분 — 하루 세션(09:30~16:00=390분)을 여유있게 커버
MAX_TRADING_DATE_LOOKBACK_DAYS = 10

CACHE_DIR = data_path("cache", "tsla_auto")


def _cache_path(symbol: str, trading_day: date) -> Any:
    return CACHE_DIR / f"{symbol.upper()}_{trading_day:%Y%m%d}_1m.csv"


def _normalize_1m_frame(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_1m_frame()
    work = df.copy()
    missing = [c for c in _1M_COLUMNS if c not in work.columns]
    if missing:
        return _empty_1m_frame()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    if work["datetime"].dt.tz is None:
        return _empty_1m_frame()
    for col in ("open", "high", "low", "close", "volume"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = (
        work.dropna(subset=["datetime", "open", "high", "low", "close"])
        .sort_values("datetime")
        .drop_duplicates(subset=["datetime"], keep="last")
        .reset_index(drop=True)
    )
    return work[list(_1M_COLUMNS)] if not work.empty else _empty_1m_frame()


def _empty_1m_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_1M_COLUMNS))


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
    """Owns all network I/O for TSLA_AUTO market data (docs §7)."""

    def __init__(
        self,
        mode: str = "MOCK",
        *,
        fetch_minute_candles: Optional[MinuteCandleFetcher] = None,
        fetch_quote: Optional[QuoteFetcher] = None,
    ) -> None:
        self.mode = mode
        self._fetch_minute_candles = fetch_minute_candles or self._default_fetch_minute_candles
        self._fetch_quote = fetch_quote or self._default_fetch_quote
        self._io_lock = threading.RLock()
        self._history_lock = threading.RLock()
        self._quote_lock = threading.RLock()
        self._df_1m: pd.DataFrame = _empty_1m_frame()
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._quote_updater_thread: Optional[threading.Thread] = None
        self._quote_updater_stop = threading.Event()
        self._history_updater_thread: Optional[threading.Thread] = None
        self._history_updater_stop = threading.Event()
        self._last_bootstrap_diag: dict[str, Any] = {}

    def _read_cached_day(self, symbol: str, trading_day: date) -> pd.DataFrame:
        path = _cache_path(symbol, trading_day)
        if not path.exists():
            return _empty_1m_frame()
        try:
            return _normalize_1m_frame(pd.read_csv(path))
        except Exception:
            return _empty_1m_frame()

    def _write_cache_days(self, symbol: str, df: pd.DataFrame) -> None:
        work = _normalize_1m_frame(df)
        if work.empty:
            return
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        days = work["datetime"].dt.tz_convert(ET).dt.date
        for trading_day in sorted(set(days)):
            day_df = work.loc[days == trading_day].copy()
            if day_df.empty:
                continue
            path = _cache_path(symbol, trading_day)
            day_df.to_csv(path, index=False)

    def _cached_warmup(self, symbol: str, now: datetime) -> pd.DataFrame:
        """Load recent TSLA 1m cache as MACD warmup seed.

        The overseas minute endpoint may return no regular-session bars before
        the US open. Prior regular-session cache lets the next session's first
        completed 3m bar be evaluated immediately after 09:33 ET instead of
        waiting for a same-day 100-bar warmup.
        """
        now_et = now.astimezone(ET)
        days: list[date] = []
        prev = market_session.previous_us_trading_day(now_et.date())
        if prev is not None:
            days.append(prev)
        days.append(now_et.date())
        frames = [self._read_cached_day(symbol, d) for d in days]
        frames = [df for df in frames if not df.empty]
        if not frames:
            return _empty_1m_frame()
        return _normalize_1m_frame(pd.concat(frames, ignore_index=True))

    def _kis_mode(self) -> str:
        # kis_overseas_adapter expects "real"/"mock" (lowercase, matching
        # app/data_sources/kis_overseas_minute.py's own mode convention) —
        # TSLA_AUTO's own RuntimeState.mode is READ_ONLY/MOCK/REAL.
        return "real" if str(self.mode).upper() == "REAL" else "mock"

    def quote_statuses(
        self, symbols: tuple[str, ...] = (config.SIGNAL_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL),
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
        self, symbols: tuple[str, ...] = (config.SIGNAL_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL),
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
        return dict(self._last_bootstrap_diag)

    def _default_fetch_minute_candles(self, mode: str, symbol: str, count: int) -> tuple[pd.DataFrame, dict[str, Any]]:
        from app.trading.tsla_auto.kis_overseas_adapter import fetch_overseas_minute_candles

        del mode  # self.mode already selects the client mode
        exchange = config.QUOTE_EXCHANGE_BY_SYMBOL.get(symbol, config.EXCHANGE_CODE)
        return fetch_overseas_minute_candles(self._kis_mode(), symbol, exchange_code=exchange, nrec=count)

    def _default_fetch_quote(self, mode: str, symbol: str) -> tuple[Optional[float], Optional[str]]:
        from app.trading.tsla_auto.kis_overseas_adapter import fetch_overseas_current_price

        del mode
        exchange = config.QUOTE_EXCHANGE_BY_SYMBOL.get(symbol, config.EXCHANGE_CODE)
        if symbol == config.INVERSE_SYMBOL and not exchange:
            return None, config.TSLZ_EXCHANGE_UNRESOLVED
        quote, error = fetch_overseas_current_price(self._kis_mode(), symbol, exchange_code=exchange)
        return (quote.price if quote else None), error

    # ── history (bootstrap + incremental) ──────────────────────────────

    def _fetch_paged(self, symbol: str) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """KIS 해외분봉조회는 MACD2의 국내 API와 달리 backward hour1 커서가
        없다 — 한 번 호출로 최근 최대 120건(약 2시간)을 돌려주므로, 여러 번
        호출해도 항상 "지금 기준 최근 N건"만 반환된다(페이지네이션 커서
        NEXT/KEYB의 실제 동작은 docs §KIS 해외 API에서
        KIS_OVERSEAS_API_CONFIRMATION_REQUIRED로 표시). 따라서 여기서는
        1회 호출 결과만 사용한다 — MACD2처럼 backward paging 루프를 돌리지
        않는다(과도한 재시도로 없는 과거를 만들어내지 않기 위함)."""
        page_diags: list[dict[str, Any]] = []
        with self._io_lock:
            part, diag = self._fetch_minute_candles(self.mode, symbol, KIS_PAGE_SIZE)
        page_diags.append({
            "request_no": 1, "received_count": int(len(part)),
            "oldest": part["datetime"].iloc[0].isoformat() if not part.empty else None,
            "newest": part["datetime"].iloc[-1].isoformat() if not part.empty else None,
            "error": diag.get("error"),
        })
        return part, page_diags

    def bootstrap(self, now: Optional[datetime] = None) -> BootstrapResult:
        """Once on Start: merge whatever prior-day + today 1m bars the
        overseas minute endpoint returns (docs §7 — a single 120-bar window,
        not a MACD2-style backward-paging walk; see _fetch_paged)."""
        now = now or datetime.now(ET)
        t0 = datetime.now(ET)
        today_ymd = now.astimezone(ET).strftime("%Y%m%d")

        cached = self._cached_warmup(config.SIGNAL_SYMBOL, now)
        live_df, page_diags = self._fetch_paged(config.SIGNAL_SYMBOL)
        frames = [df for df in (cached, live_df) if not df.empty]
        df = _normalize_1m_frame(pd.concat(frames, ignore_index=True)) if frames else _empty_1m_frame()
        self._write_cache_days(config.SIGNAL_SYMBOL, df)
        elapsed = (datetime.now(ET) - t0).total_seconds()

        self._last_bootstrap_diag = {
            "requested_trading_date": today_ymd,
            "pages": page_diags,
            "cached_warmup_count": int(len(cached)),
            "live_1m_count": int(len(live_df)),
            "merged_oldest": df["datetime"].iloc[0].isoformat() if not df.empty else None,
            "merged_newest": df["datetime"].iloc[-1].isoformat() if not df.empty else None,
        }

        with self._history_lock:
            self._df_1m = df

        if df.empty:
            return BootstrapResult(False, "NO_1M_BARS", 0, 0, 0, 0, round(elapsed, 3))

        dates = df["datetime"].dt.tz_convert(ET).dt.strftime("%Y%m%d")
        prior_n = int((dates != today_ymd).sum())
        today_n = int((dates == today_ymd).sum())
        bars3 = resample_completed_3m(df, now=now)
        completed_3m_count = int(len(bars3))

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
        """Latest-page-only merge — never re-walks full history (docs §7)."""
        now = now or datetime.now(ET)
        with self._io_lock:
            live_df, _diag = self._fetch_minute_candles(self.mode, config.SIGNAL_SYMBOL, KIS_PAGE_SIZE)
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
            self._write_cache_days(config.SIGNAL_SYMBOL, merged)
            return merged.copy()

    def get_history_df(self) -> pd.DataFrame:
        with self._history_lock:
            return self._df_1m.copy()

    def clear_history(self) -> None:
        with self._history_lock:
            self._df_1m = _empty_1m_frame()

    # ── quotes ──────────────────────────────────────────────────────────

    def refresh_quotes(
        self, symbols: tuple[str, ...] = (config.SIGNAL_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL),
    ) -> dict[str, QuoteSnapshot]:
        updated: dict[str, QuoteSnapshot] = {}
        for symbol in symbols:
            with self._io_lock:
                price, error = self._fetch_quote(self.mode, symbol)
            success = error is None and price is not None and float(price) > 0
            fetched_at = datetime.now(ET)
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
        age = (datetime.now(ET) - snap.fetched_at).total_seconds()
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

        self._quote_updater_thread = threading.Thread(target=_loop, daemon=True, name="tsla-auto-quote-updater")
        self._quote_updater_thread.start()

    def stop_quote_updater(self, join_timeout: float = 2.0) -> None:
        self._quote_updater_stop.set()
        thread = self._quote_updater_thread
        if thread is not None:
            thread.join(timeout=join_timeout)
        self._quote_updater_thread = None

    def quote_updater_alive(self) -> bool:
        return bool(self._quote_updater_thread and self._quote_updater_thread.is_alive())

    def start_history_updater(self, interval_sec: float = config.WORKER_INTERVAL_SEC) -> None:
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

        self._history_updater_thread = threading.Thread(target=_loop, daemon=True, name="tsla-auto-history-updater")
        self._history_updater_thread.start()

    def stop_history_updater(self, join_timeout: float = 2.0) -> None:
        self._history_updater_stop.set()
        thread = self._history_updater_thread
        if thread is not None:
            thread.join(timeout=join_timeout)
        self._history_updater_thread = None

    def history_updater_alive(self) -> bool:
        return bool(self._history_updater_thread and self._history_updater_thread.is_alive())


def filter_complete_3m_bars(
    bars_3m: pd.DataFrame, one_minute_bars: pd.DataFrame,
) -> tuple[pd.DataFrame, list[Any]]:
    """Drop any completed 3-minute bar whose 3 constituent 1-minute bars are
    not ALL present with a valid close in ``one_minute_bars`` (docs §7 —
    same HISTORY_GAP principle as MACD2's 2026-07-31 fix, re-implemented here
    independently). Never fills/interpolates a gap.
    """
    if bars_3m is None or bars_3m.empty:
        return bars_3m, []
    if one_minute_bars is None or one_minute_bars.empty or "datetime" not in one_minute_bars.columns:
        return bars_3m.iloc[0:0].reset_index(drop=True), list(bars_3m["datetime"])

    work_1m = one_minute_bars.copy()
    work_1m["datetime"] = pd.to_datetime(work_1m["datetime"], errors="coerce")
    if "close" in work_1m.columns:
        valid_closes = pd.to_numeric(work_1m["close"], errors="coerce")
        work_1m = work_1m.loc[work_1m["datetime"].notna() & valid_closes.notna()]
    else:
        work_1m = work_1m.loc[work_1m["datetime"].notna()]
    have = set(work_1m["datetime"])

    keep_mask: list[bool] = []
    dropped: list[Any] = []
    for bar_start in bars_3m["datetime"]:
        needed = [pd.Timestamp(bar_start) + timedelta(minutes=i) for i in range(3)]
        complete = all(minute in have for minute in needed)
        keep_mask.append(complete)
        if not complete:
            dropped.append(bar_start)
    filtered = bars_3m.loc[keep_mask].reset_index(drop=True)
    return filtered, dropped
