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
2026-07-23 code-reuse audit.

Tests must inject a fake ``fetch_minute_candles``/``fetch_quote`` callable —
see tests/macd2/test_market_data.py. Never call the real KIS fetchers there.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from app.logger import logger
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
# inquire-time-itemchartprice / 주식일별분봉조회 both actually return ~30 rows
# per call regardless of the requested count (KIS 서버측 고정 page size —
# 2026-07-27 발견: 장 시작 후 3시간(6 pages * 30 rows = 180분)이 지나면 이후
# paging walk가 오늘 데이터의 앞부분을 빠뜨렸다). NXT 포함(market_div="NX")
# 전환 이후 세션은 08:00~20:00 = 720분으로 늘어났다 (2026-08-20) -> needs
# >=24 pages at 30 rows/page; sized with margin.
KIS_MAX_PAGES = 30
KIS_PAGE_MINUTES = 30  # matches the ~30-rows-per-page fact above

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


def _parse_hour1(hour1: str) -> datetime:
    """``hour1`` cursor ("HHMMSS") -> a time-of-day anchor to back off from
    when a page fails. Empty ``hour1`` means "latest" (no cursor sent yet);
    anchor that case at NXT session close (20:00, market_div="NX" 2026-08-20)
    so the very first page skips backward from end-of-session, same as any
    other stuck boundary."""
    return datetime.strptime(hour1 or "200000", "%H%M%S")


def _load_prior_day_1m_cache(watch_symbol: str, today_ymd: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fallback B (docs §21 2026-07-24 warm-up fix): the prior trading day's
    1m bars for ``watch_symbol`` from a local historical cache
    (``data/cache/naver_multi_1m/{symbol}_1m.csv``) — plain, generic
    market-data for this symbol (not MACD-v1 production code; MACD v1's own
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


def _trim_to_recent_trading_days(df: pd.DataFrame, max_days: int = 2) -> pd.DataFrame:
    """Bound retained 1m history to the most recent ``max_days`` distinct
    calendar dates (KST) actually present in ``df``.

    2026-08-24 fix (real incident: MACD2 left running overnight, Render
    memory usage climbed 40%->80% with no restart). bootstrap() itself only
    ever loads ONE prior trading day + today (_load_prior_trading_day), and
    nothing else in this module ever reads further back than that — but
    merge_incremental_1m() unconditionally concatenates each cycle's live
    page onto the existing _df_1m with no retention cap, so a long-running
    process accumulated one full day's worth of extra rows every single day
    forever. NX market_div's continuous 08:00-20:00 session (2026-08-20 fix)
    made each accumulated day ~85% larger than the old J-only 09:00-15:30
    window, worsening this. Trimming to the 2 most recent trading dates after
    every merge only ever discards days that were already unused by any
    consumer (signal calc, UI, ledger) — never today's or yesterday's rows.
    """
    if df.empty:
        return df
    dates = df["datetime"].dt.strftime("%Y%m%d")
    keep_dates = set(sorted(dates.unique())[-max_days:])
    if len(keep_dates) >= dates.nunique():
        return df
    return df.loc[dates.isin(keep_dates)].reset_index(drop=True)


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
        self._watch_history_client: Any = None
        self._watch_history_client_lock = threading.RLock()
        self._fetch_minute_candles = fetch_minute_candles or self._default_fetch_minute_candles
        self._fetch_minute_candles_for_date = (
            fetch_minute_candles_for_date or self._default_fetch_minute_candles_for_date
        )
        self._fetch_quote = fetch_quote or self._default_fetch_quote
        # 2026-08-25 fix (real incident: order_block_reason stuck at
        # HISTORY_GAP all morning, every TW/TW2 T+3 confirmation stuck at
        # PENDING_CONFIRMATION forever). A single self._io_lock used to wrap
        # BOTH the quote path (refresh_quotes) AND the history path
        # (merge_incremental_1m/bootstrap paging) end-to-end, INCLUDING
        # kis_client._get_with_rate_limit_retry's internal rate-limit
        # backoff sleeps (up to ~40s under sustained KIS mock-mode rate
        # limiting: 8 attempts x 5s). One symbol's stuck retry therefore
        # held the SAME lock the OTHER path needed, blocking history's
        # merge_incremental_1m() (or quotes) for that entire ~40s window —
        # under sustained contention this compounded into the history
        # updater never completing a fresh merge for minutes at a time,
        # which is exactly what leaves a completed 3m bar's minutes
        # perpetually incomplete (filter_complete_3m_bars drops it ->
        # HISTORY_GAP -> the T+3 bar this flag needs to confirm on never
        # appears). Splitting into two independent locks means a stuck
        # quote retry no longer blocks history's own progress and vice
        # versa; each path's own KIS calls still only ever run on their own
        # single dedicated updater thread (no new concurrency introduced,
        # no possibility of two calls racing for the SAME lock), and actual
        # request PACING/no-spam is still enforced exactly as before by
        # kis_client.py's own process-wide _throttle() (a separate, already
        # cross-thread-safe rate gate keyed by mode, unaffected by this
        # split) -- this only removes MarketDataService's own additional,
        # redundant serialization between its two independent fetch paths.
        self._quote_fetch_lock = threading.RLock()
        self._history_fetch_lock = threading.RLock()
        self._history_lock = threading.RLock()
        self._quote_lock = threading.RLock()
        self._df_1m: pd.DataFrame = _empty_1m_frame()
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._quote_updater_thread: Optional[threading.Thread] = None
        self._quote_updater_stop = threading.Event()
        self._history_updater_thread: Optional[threading.Thread] = None
        self._history_updater_stop = threading.Event()
        self._last_bootstrap_diag: dict[str, Any] = {}
        self._quote_normalization_diag: dict[str, Any] = {}
        # 2026-08-26 diagnostic-logging-only addition (docs: today's flag-
        # detection incident) -- pure observability, never read by any
        # decision path. last-success timestamps per quote symbol / history,
        # and the last quote_status() value actually logged, so a
        # READY/PARTIAL_STALE/DEAD transition is logged only on CHANGE
        # (never every ~1s cycle) to keep log volume sane over a full day.
        self._last_quote_success_at: dict[str, datetime] = {}
        self._last_history_success_at: Optional[datetime] = None
        self._last_logged_quote_status: Optional[str] = None
        # 2026-08-26 follow-up: whether the PREVIOUS attempt failed, per
        # quote symbol / history -- success is logged only on the first-ever
        # attempt or a recovery from a prior failure, never on every routine
        # cycle (quote: ~8s; history: WORKER_INTERVAL_SEC=5s -- effectively
        # "every tick" if logged unconditionally).
        self._quote_last_attempt_failed: dict[str, bool] = {}
        self._history_last_attempt_failed: Optional[bool] = None

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
        # 2026-08-24 fix (real incident: UI quote_status badge flapped
        # PARTIAL_STALE/READY all morning while orders were never actually at
        # risk): default used to include WATCH_SYMBOL(000660), but that quote
        # is signal-source-only -- worker.py's own order-dispatch gate
        # (_required_quote_symbols) already excludes it for the exact same
        # reason (2026-08-12 fix). Including it here just made the
        # diagnostic badge flip on a symbol that never blocks an order.
        symbols: tuple[str, ...] = (config.LONG_SYMBOL, config.INVERSE_SYMBOL),
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

    def quote_normalization_diag(self) -> dict[str, Any]:
        return dict(self._quote_normalization_diag)

    def _latest_history_close(self) -> tuple[Optional[datetime], Optional[float]]:
        with self._history_lock:
            df = self._df_1m.copy()
        if df.empty or "datetime" not in df.columns or "close" not in df.columns:
            return None, None
        closes = pd.to_numeric(df["close"], errors="coerce")
        valid = df.loc[closes.notna()].copy()
        if valid.empty:
            return None, None
        last = valid.sort_values("datetime").iloc[-1]
        return pd.Timestamp(last["datetime"]).to_pydatetime(), float(last["close"])

    def _normalize_quote_price(self, symbol: str, price: float) -> float:
        if symbol != config.WATCH_SYMBOL or price <= 0:
            return price
        last_dt, last_close = self._latest_history_close()
        diag = {
            "symbol": symbol,
            "raw_quote": float(price),
            "last_1m_at": last_dt.isoformat() if last_dt else None,
            "last_1m_close": last_close,
            "scale_factor": 1.0,
            "normalized_quote": float(price),
            "reason": "",
        }
        if last_close and last_close > 0:
            ratio = float(price) / float(last_close)
            if 9.5 <= ratio <= 10.5:
                price = float(price) / 10.0
                diag.update({"scale_factor": 0.1, "normalized_quote": price, "reason": "QUOTE_10X_HISTORY_CLOSE"})
            elif 0.095 <= ratio <= 0.105:
                price = float(price) * 10.0
                diag.update({"scale_factor": 10.0, "normalized_quote": price, "reason": "QUOTE_0_1X_HISTORY_CLOSE"})
        self._quote_normalization_diag = diag
        return price

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

    def _get_watch_symbol_history_client(self) -> Any:
        """WATCH_SYMBOL (000660) prior-day warm-up (주식일별분봉조회) read-only
        client — never used for orders or the traded ETF symbols.

        000660 is signal-input-only, never traded directly, so its candle
        DATA is identical whether read via the REAL or MOCK KIS endpoint (KIS
        모의투자 mirrors real market data). Verified empirically 2026-07-27:
        MOCK vs REAL 000660 1m OHLC matched exactly (0.000% diff) at every bar
        checked around today's 3 flag times. But the MOCK/virtual server's
        date-scoped historical-minute-chart endpoint intermittently 500s in
        ways the REAL endpoint for the SAME public data does not — silently
        corrupting prior-day warm-up (wrong seed day). This ONE-SHOT
        per-bootstrap fetch is the only thing routed through REAL; today's
        repeated live-paging walk stays on the MOCK endpoint (routing that
        one through REAL instead hit REAL-account rate limits and made it
        LESS reliable, not more). All order/balance/quote calls for the
        traded ETFs still go through ``_get_kis_client()`` unchanged."""
        if self.mode == "real":
            return self._get_kis_client()
        with self._watch_history_client_lock:
            if self._watch_history_client is None:
                from app.trading.kis_client import create_kis_client

                self._watch_history_client = create_kis_client("real")
            if self._watch_history_client is not None:
                return self._watch_history_client
        # no REAL credentials configured in this deployment — fall back to
        # the mode-selected (mock) client rather than fetching nothing.
        return self._get_kis_client()

    def _default_fetch_minute_candles(self, mode: str, symbol: str, count: int, hour1: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Real KIS call — the one and only network entry point for minute bars.

        Today's live paging walk fires several rapid successive requests
        (backward cursor), which hit REAL-account rate limits harder than the
        MOCK endpoint (verified 2026-07-27: switching this path to REAL
        caused it to intermittently miss the early session instead). Only
        the ONE-SHOT prior-day warm-up fetch
        (_default_fetch_minute_candles_for_date) uses the REAL client — this
        path stays on self.mode's regular client, unchanged."""
        del mode  # self.mode already selected the shared client via _get_kis_client()
        client = self._get_kis_client()
        if client is None:
            return _empty_1m_frame(), {"error": "kis_client_none"}
        try:
            candles = client.get_minute_candles(
                symbol, period_min=1, count=count, hour1=hour1,
                market_div=config.NXT_MARKET_DIV_CODE,
            ) or []
        except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
            return _empty_1m_frame(), {"error": repr(exc)}
        df = _candles_to_df(candles)
        if df.empty:
            # get_minute_candles() itself never raises (docs: 실패 시 [] 반환) —
            # a transient HTTP/network failure (KIS 500, timeout, ...) is
            # otherwise indistinguishable here from a genuine empty page
            # (today's own paging walk truly has no more bars), which would
            # silently stop the backward walk early instead of retrying and
            # truncate today's history (2026-08-03 발견: 정상 재시도 없이 당일
            # 09:00 이후 분봉이 통째로 누락됨). last_minute_candle_error is set
            # by get_minute_candles() right before returning [] on failure.
            client_error = getattr(client, "last_minute_candle_error", None)
            if client_error:
                return df, {"error": client_error}
        return df, {"received_count": int(len(df))}

    def _default_fetch_minute_candles_for_date(
        self, mode: str, symbol: str, date_ymd: str, count: int, hour1: str,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Real KIS call — 주식일별분봉조회 (fallback A), the only network
        entry point that can return a SPECIFIC calendar date's minute bars."""
        del mode
        try:
            client = self._get_watch_symbol_history_client()
            if client is None:
                return _empty_1m_frame(), {"error": "kis_client_none"}
            candles = client.get_minute_candles_for_date(
                symbol, date_ymd, period_min=1, count=count, hour1=hour1,
                market_div=config.NXT_MARKET_DIV_CODE,
            ) or []
        except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
            return _empty_1m_frame(), {"error": repr(exc)}
        df = _candles_to_df(candles)
        if df.empty:
            # Same silent-swallow gap as _default_fetch_minute_candles above —
            # get_minute_candles_for_date() never raises either, so surface
            # its last_minute_candle_error the same way (this is exactly the
            # 2026-07-27 "20260724 조회가 500으로 실패해 20260723으로 잘못
            # 대체됨" bug config.py already documents; PRIOR_DAY_FETCH_RETRIES
            # never actually retried anything without this signal).
            client_error = getattr(client, "last_minute_candle_error", None)
            if client_error:
                return df, {"error": client_error}
        return df, {"received_count": int(len(df))}

    def _default_fetch_quote(self, mode: str, symbol: str) -> tuple[Optional[float], Optional[str]]:
        """Real KIS call — the one and only network entry point for a live quote.

        WATCH_SYMBOL(000660)만 NX(NXT 포함 실시간 체결가)로 조회한다 — 이
        값이 MACD 계산의 입력이자 대시보드에 표시되는 "현재가"이므로, 1분봉
        히스토리(market_div="NX", 2026-08-20 fix)와 같은 소스여야 정규장
        마감 이후에도 계속 갱신된다. 실제 매매 대상인 LONG_SYMBOL/
        INVERSE_SYMBOL(ETF)은 이번 변경 범위 밖이라 그대로 "J"를 쓴다."""
        del mode
        client = self._get_kis_client()
        if client is None:
            return None, "kis_client_none"
        market_div = config.NXT_MARKET_DIV_CODE if symbol == config.WATCH_SYMBOL else "J"
        try:
            result = client.get_current_price(symbol, market_div=market_div)
            if not result:
                return None, "kis_client_empty_response"
            # 2026-08-20 fix: this used to hardcode error=None on every
            # non-exception return, even when get_current_price() itself
            # already reported a real failure (e.g. rate-limited/EGW00201,
            # empty output) via result["error"] with current_price left at 0.
            # refresh_quotes()'s price>0 check still correctly rejected that
            # as a failed fetch, but silently discarded WHY it failed,
            # reporting the generic "QUOTE_FETCH_FAILED" in QuoteSnapshot.error
            # instead of the actual KIS reason — surfacing it here makes a
            # future incident like this one directly diagnosable from state.
            return float(result["current_price"]), result.get("error")
        except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
            return None, repr(exc)

    # ── history (bootstrap + incremental) ──────────────────────────────

    def _fetch_trading_day_candles(self, date_ymd: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        """One full page-backwards walk of KIS's 주식일별분봉조회 for a SINGLE
        specific calendar date — same no-growth/cursor-stuck bounded loop as
        the live today-only walk, just against the date-scoped endpoint.

        A transient fetch error (e.g. KIS 500 / rate limit) is retried up to
        ``PRIOR_DAY_FETCH_RETRIES`` times before this date is treated as
        empty — otherwise a one-off server hiccup on the true prior trading
        day gets silently mistaken for "no data / holiday" and the caller
        falls back to an earlier, wrong date, seeding EMA warm-up off a
        different day than KIS's own chart uses (2026-07-27 fix)."""
        pages: list[pd.DataFrame] = []
        page_diags: list[dict[str, Any]] = []
        hour1 = ""
        prev_count = 0
        consecutive_error_skips = 0
        for page_i in range(KIS_MAX_PAGES):
            if page_i > 0:
                time.sleep(config.KIS_PAGE_FETCH_PACING_SEC)
            part = _empty_1m_frame()
            _diag: dict[str, Any] = {}
            for retry_i in range(config.PRIOR_DAY_FETCH_RETRIES):
                with self._history_fetch_lock:
                    part, _diag = self._fetch_minute_candles_for_date(
                        self.mode, config.WATCH_SYMBOL, date_ymd, KIS_PAGE_SIZE, hour1,
                    )
                if not part.empty or not _diag.get("error"):
                    break
                if retry_i < config.PRIOR_DAY_FETCH_RETRIES - 1:
                    time.sleep(config.PRIOR_DAY_FETCH_RETRY_DELAY_SEC)
            page_diags.append({
                "request_no": page_i + 1, "requested_date": date_ymd,
                "requested_hour1": hour1 or "LATEST", "received_count": int(len(part)),
                "oldest": part["datetime"].iloc[0].isoformat() if not part.empty else None,
                "newest": part["datetime"].iloc[-1].isoformat() if not part.empty else None,
                "error": _diag.get("error"),
            })
            if part.empty:
                if _diag.get("error") and consecutive_error_skips < config.MAX_CONSECUTIVE_PAGE_ERROR_SKIPS:
                    # Retries exhausted on a genuine fetch error (transient
                    # KIS 500/timeout), not a legitimate "no earlier data"
                    # signal -- 2026-08-10 fix (real incident: 000660's
                    # itemchartprice intermittently 500s at one specific
                    # hour1 boundary while other symbols sail through,
                    # permanently truncating everything earlier than that
                    # boundary for the rest of the session since only this
                    # one-shot walk ever populates it). Skip past the stuck
                    # boundary by one page-width instead of giving up the
                    # whole walk, so an isolated bad page never silently
                    # amputates the rest of the trading day. Capped at
                    # MAX_CONSECUTIVE_PAGE_ERROR_SKIPS so a genuinely
                    # fully-down endpoint still gives up promptly.
                    page_diags[-1]["stop_reason"] = "FETCH_ERROR_SKIPPED"
                    consecutive_error_skips += 1
                    base = _parse_hour1(hour1)
                    hour1 = (base - timedelta(minutes=KIS_PAGE_MINUTES)).strftime("%H%M%S")
                    continue
                break
            consecutive_error_skips = 0
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
        else:
            # Same 2026-08-20 condition-6 fix as bootstrap()'s today-walk:
            # the loop consumed every one of KIS_MAX_PAGES pages without ever
            # hitting a genuine stop signal, meaning data was still growing
            # when the page budget ran out -- there may be earlier history
            # (e.g. this date's own NXT premarket/evening) we simply never
            # asked for. Flag it so the caller does not treat this date as a
            # clean, complete fetch.
            if pages:
                page_diags[-1]["stop_reason"] = "MAX_PAGES_EXHAUSTED"

        df = (
            pd.concat(pages, ignore_index=True)
            .drop_duplicates(subset=["datetime"], keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
            if pages else _empty_1m_frame()
        )
        page_budget_exhausted = bool(page_diags) and page_diags[-1].get("stop_reason") == "MAX_PAGES_EXHAUSTED"
        return df, {
            "date": date_ymd, "pages": page_diags, "received_count": int(len(df)),
            "page_budget_exhausted": page_budget_exhausted,
        }

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
                    "page_budget_exhausted": bool(diag.get("page_budget_exhausted")),
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
        consecutive_error_skips = 0
        today_page_exhausted = False
        for page_i in range(KIS_MAX_PAGES):
            if page_i > 0:
                time.sleep(config.KIS_PAGE_FETCH_PACING_SEC)
            part = _empty_1m_frame()
            _diag: dict[str, Any] = {}
            for retry_i in range(config.PRIOR_DAY_FETCH_RETRIES):
                with self._history_fetch_lock:
                    part, _diag = self._fetch_minute_candles(self.mode, config.WATCH_SYMBOL, KIS_PAGE_SIZE, hour1)
                if not part.empty or not _diag.get("error"):
                    break
                if retry_i < config.PRIOR_DAY_FETCH_RETRIES - 1:
                    time.sleep(config.PRIOR_DAY_FETCH_RETRY_DELAY_SEC)
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
                if _diag.get("error") and consecutive_error_skips < config.MAX_CONSECUTIVE_PAGE_ERROR_SKIPS:
                    # Same 2026-08-10 fix as _fetch_trading_day_candles above
                    # -- retries exhausted on a genuine fetch error, not a
                    # real "no earlier data today" signal. Skip past the
                    # stuck boundary by one page-width instead of truncating
                    # every earlier bar of today's session. Capped at
                    # MAX_CONSECUTIVE_PAGE_ERROR_SKIPS so a genuinely
                    # fully-down endpoint still gives up promptly.
                    page_diags[-1]["stop_reason"] = "FETCH_ERROR_SKIPPED"
                    consecutive_error_skips += 1
                    base = _parse_hour1(hour1)
                    hour1 = (base - timedelta(minutes=KIS_PAGE_MINUTES)).strftime("%H%M%S")
                    continue
                break
            consecutive_error_skips = 0
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
        else:
            # for-loop exhausted every one of KIS_MAX_PAGES iterations without
            # ever hitting a genuine stop signal (PAGE_NO_GROWTH/CURSOR_NOT_
            # MOVING/legitimate-empty-break above) -- every page up to the
            # last still returned fresh, growing data, so there may well be
            # MORE history earlier than what we stopped asking for (2026-08-20
            # condition 6: market_div="NX" extends a full session to 08:00-
            # 20:00/720min, so an insufficient page budget here would silently
            # truncate exactly the premarket window this whole fix depends
            # on). Flag it explicitly rather than reporting success.
            if pages:
                page_diags[-1]["stop_reason"] = "MAX_PAGES_EXHAUSTED"
                today_page_exhausted = True

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

        if today_n > 0 and now.time() > config.SESSION_OPEN:
            today_start = df[dates == today_ymd]["datetime"].iloc[0].astimezone(KST)
            if today_start.time() > config.SESSION_OPEN:
                self._last_bootstrap_diag["today_history_warning"] = (
                    f"TODAY_1M_START_AFTER_OPEN:{today_start.strftime('%H:%M:%S')}"
                )

        # 페이지 예산 소진으로 인한 조용한 결측 검증 (2026-08-20, 조건 6) —
        # 아래 두 페이징 루프 모두 KIS_MAX_PAGES를 다 쓰고도(page_i가 마지막
        # 인덱스에 도달) 여전히 더 받아올 데이터가 남아있는 것처럼 보이며
        # 끝난 경우(PAGE_NO_GROWTH/CURSOR_NOT_MOVING 같은 "진짜 끝" 신호 없이
        # for 루프 자체가 그냥 소진된 경우), 실제로는 더 이전 시각의 데이터가
        # 남아있을 수 있는데도 조용히 "성공"으로 보고될 위험이 있다 — market_
        # div="NX" 전환으로 하루 세션이 08:00~20:00(720분)까지 늘어났으므로
        # (기존 09:00~15:30 390분 가정보다 훨씬 김) 이 위험이 실제로 커졌다.
        # stop_reason 없이(=자연스러운 종료 신호 없이) 루프가 그냥 다 돌았다면
        # 하드 실패로 처리해 결측을 성공으로 위장하지 않는다.
        nxt_gap_reason: Optional[str] = None
        if today_page_exhausted:
            nxt_gap_reason = "NXT_TODAY_PAGE_BUDGET_EXHAUSTED"
        elif prior_diag.get("page_budget_exhausted"):
            nxt_gap_reason = "NXT_PRIOR_DAY_PAGE_BUDGET_EXHAUSTED"
        if nxt_gap_reason is not None:
            self._last_bootstrap_diag["nxt_coverage_gap"] = nxt_gap_reason
            return BootstrapResult(
                False, nxt_gap_reason, int(len(df)), prior_n, today_n,
                completed_3m_count, round(elapsed, 3),
            )

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
        """Latest-page-only merge — never re-walks the full bootstrap history (docs §4).

        2026-08-11 fix (real incident: a confirmed flag's 3m bin silently
        never became actionable — no exit of an already-held position, no
        new entry — because the one KIS fetch this function makes had no
        retry at all; a single transient 500 on this exact call left
        _df_1m stale until some LATER cycle happened to succeed on its
        own). Retries on a genuine fetch error the same way the bootstrap/
        prior-day paging walks already do (PRIOR_DAY_FETCH_RETRIES /
        PRIOR_DAY_FETCH_RETRY_DELAY_SEC) — an empty response with NO error
        (legitimately nothing new yet) still returns immediately, unretried.
        """
        now = now or datetime.now(KST)
        live_df = _empty_1m_frame()
        _diag: dict[str, Any] = {}
        for retry_i in range(config.PRIOR_DAY_FETCH_RETRIES):
            with self._history_fetch_lock:
                live_df, _diag = self._fetch_minute_candles(self.mode, config.WATCH_SYMBOL, 10, "")
            if not live_df.empty or not _diag.get("error"):
                break
            if retry_i < config.PRIOR_DAY_FETCH_RETRIES - 1:
                time.sleep(config.PRIOR_DAY_FETCH_RETRY_DELAY_SEC)
        if live_df.empty and _diag.get("error"):
            # 2026-08-26 diagnostic log (docs: today's flag-detection
            # incident) -- pure logging, no behavior change: every retry
            # already happened above exactly as before this line existed.
            logger.warning(
                "[MACD2][history] merge_incremental_1m failed at %s after %d retries: %s "
                "(last success: %s)",
                now.isoformat(), config.PRIOR_DAY_FETCH_RETRIES, _diag.get("error"),
                self._last_history_success_at.isoformat() if self._last_history_success_at else "never",
            )
            self._history_last_attempt_failed = True
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
            merged = _trim_to_recent_trading_days(merged)
            self._df_1m = merged
            self._last_history_success_at = now
            # 2026-08-26 follow-up: only log on the FIRST-EVER success or a
            # recovery from a prior failure -- not every cycle a new bar
            # happens to merge in (this call runs every WORKER_INTERVAL_SEC,
            # i.e. effectively every tick; unconditional success logging
            # here was exactly the "매 tick마다 과도한 로그" the diagnostic
            # logging was supposed to avoid).
            if self._history_last_attempt_failed is not False:
                newest_bar = merged["datetime"].iloc[-1] if not merged.empty else None
                logger.info(
                    "[MACD2][history] merge_incremental_1m ok at %s: %d bars fetched, newest bar=%s",
                    now.isoformat(), len(live_df), newest_bar.isoformat() if newest_bar is not None else "-",
                )
            self._history_last_attempt_failed = False
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
            with self._quote_fetch_lock:
                price, error = self._fetch_quote(self.mode, symbol)
            success = error is None and price is not None and float(price) > 0
            fetched_at = datetime.now(KST)
            if success:
                price = self._normalize_quote_price(symbol, float(price))
                updated[symbol] = QuoteSnapshot(
                    symbol=symbol, price=float(price), fetched_at=fetched_at, age_sec=0.0, source="kis", error=None,
                )
                self._last_quote_success_at[symbol] = fetched_at
                # 2026-08-26 diagnostic log (docs: today's flag-detection
                # incident) -- WATCH_SYMBOL(000660) only (the one explicitly
                # asked for; the traded ETFs refresh ~1/sec each, far too
                # noisy to log every success), and only on the first-ever
                # success or a recovery from a prior failure -- not every
                # routine cycle.
                if symbol == config.WATCH_SYMBOL and self._quote_last_attempt_failed.get(symbol, True):
                    logger.info("[MACD2][quote] %s ok at %s: price=%s", symbol, fetched_at.isoformat(), price)
                self._quote_last_attempt_failed[symbol] = False
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
            # 2026-08-26 diagnostic log -- every failure, every symbol
            # (failures are the rare/interesting case; volume is bounded by
            # how often KIS actually fails, never by the 1s poll interval).
            last_success = self._last_quote_success_at.get(symbol)
            logger.warning(
                "[MACD2][quote] %s FAILED at %s: %s (last success: %s)",
                symbol, fetched_at.isoformat(), error or "QUOTE_FETCH_FAILED",
                last_success.isoformat() if last_success else "never",
            )
            self._quote_last_attempt_failed[symbol] = True
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
        # 2026-08-21 fix (real incident: Render OOM after this thread quietly
        # multiplied for hours): this used to clear() the SAME self._quote_
        # updater_stop Event every restart. worker.py's own stuck-thread
        # self-heal (_run_loop, "stalest_age > QUOTE_UPDATER_STALL_AGE_SEC")
        # calls stop_quote_updater()+start_quote_updater() every tick while a
        # refresh_quotes() call is stuck deep in the (now-retrying, up to
        # ~40s/attempt-cycle under sustained KIS rate limiting)
        # _get_with_rate_limit_retry loop -- stop_quote_updater()'s
        # join(timeout=0.5) can't wait that long, so it gives up and returns
        # while the OLD thread is still blocked inside that same loop. The
        # very next start_quote_updater() then called .clear() on that SAME
        # Event out from under it -- so once the OLD thread's blocked call
        # finally returned and it re-checked the loop condition, the flag it
        # was waiting on had been reset to "keep going" and it just kept
        # looping FOREVER, alongside the brand new thread. Every subsequent
        # stall re-triggered this, so live-orphaned "macd2-quote-updater"
        # threads accumulated unbounded over hours -- each still polling KIS
        # (worsening the very rate limiting that caused this), each held
        # forever by the Python interpreter. A fresh, private Event per
        # generation means stop_quote_updater() only ever retires the thread
        # generation it actually targets: an old thread's own captured
        # reference stays set even after a new generation clears ITS OWN new
        # Event, so the old thread genuinely exits the first time it gets to
        # recheck the loop condition, instead of resuming.
        stop_event = threading.Event()
        self._quote_updater_stop = stop_event

        # 2026-08-24 fix (real incident: LONG/INVERSE ETF quote age sat at
        # 16-19s all morning under KIS mock-mode rate-limit contention from
        # other bots sharing this process's mock throttle -- see
        # config.WATCH_SYMBOL_QUOTE_REFRESH_EVERY_N_CYCLES). WATCH_SYMBOL is
        # diagnostic-display-only (never read by order dispatch), so it
        # doesn't need every cycle -- only the two traded ETFs do.
        traded_symbols = (config.LONG_SYMBOL, config.INVERSE_SYMBOL)
        all_symbols = (config.WATCH_SYMBOL,) + traded_symbols
        cycle_n = 0

        def _loop() -> None:
            nonlocal cycle_n
            while not stop_event.is_set():
                try:
                    symbols = (
                        all_symbols
                        if cycle_n % config.WATCH_SYMBOL_QUOTE_REFRESH_EVERY_N_CYCLES == 0
                        else traded_symbols
                    )
                    self.refresh_quotes(symbols=symbols)
                except Exception as exc:
                    # 2026-08-26 diagnostic log (docs: today's flag-detection
                    # incident) -- pure logging; the loop still swallows and
                    # continues exactly as it always did (no control-flow
                    # change, no re-raise).
                    logger.warning(
                        "[MACD2][quote-updater loop] %s: %s", type(exc).__name__, exc,
                    )
                # 2026-08-26 diagnostic log -- log a READY/PARTIAL_STALE/
                # PARTIAL_ERROR/DEAD transition only on CHANGE (quote_status()
                # itself is unchanged/pure; this just observes its result).
                try:
                    current_status = self.quote_status()
                    if current_status != self._last_logged_quote_status:
                        logger.info(
                            "[MACD2][quote_status] %s -> %s at %s",
                            self._last_logged_quote_status, current_status, datetime.now(KST).isoformat(),
                        )
                        self._last_logged_quote_status = current_status
                except Exception:
                    pass
                cycle_n += 1
                stop_event.wait(interval_sec)

        logger.info("[MACD2][quote-updater] starting new thread, interval_sec=%s", interval_sec)
        self._quote_updater_thread = threading.Thread(target=_loop, daemon=True, name="macd2-quote-updater")
        self._quote_updater_thread.start()

    def stop_quote_updater(self, join_timeout: float = 2.0) -> bool:
        """Returns True only if the thread is confirmed dead after the join.

        2026-08-24 fix (real incident: Render memory climbed 20%->60% over
        ~2h under sustained KIS mock-mode rate limiting): this used to
        unconditionally drop ``self._quote_updater_thread`` to None on
        return, regardless of whether ``join()`` actually succeeded. A
        caller stuck deep in a KIS retry chain (up to ~40s per symbol, worse
        under sustained contention) routinely outlives this method's
        short join_timeout, so the discarded reference was still a live,
        running thread -- callers (worker.py's per-tick self-heal,
        Macd2Service._auto_recover_worker) had no way to tell "stopped" from
        "still running, orphaned" and unconditionally started a brand-new
        updater/instance on top of it every restart cycle, the exact
        mechanism the 2026-08-21 fix above only rate-limited (to once per
        QUOTE_UPDATER_STALL_AGE_SEC) instead of eliminating -- under
        contention lasting longer than that cooldown, orphans still
        accumulated net-positive. Keeping the reference alive (and
        quote_updater_alive() reporting True) when the thread hasn't
        actually died lets callers refuse to pile another thread on top."""
        self._quote_updater_stop.set()
        thread = self._quote_updater_thread
        if thread is None:
            return True
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            logger.warning(
                "[MACD2][quote-updater] stop NOT confirmed after join_timeout=%ss -- thread still alive (orphaned)",
                join_timeout,
            )
            return False
        self._quote_updater_thread = None
        logger.info("[MACD2][quote-updater] stopped and confirmed dead")
        return True

    def quote_updater_alive(self) -> bool:
        return bool(self._quote_updater_thread and self._quote_updater_thread.is_alive())

    # ── history updater (background 1m refresh; Worker only reads the cache) ──

    def start_history_updater(self, interval_sec: float = config.WORKER_INTERVAL_SEC) -> None:
        """Background thread that periodically calls merge_incremental_1m() —
        the only place that happens now that worker.py no longer triggers it
        itself (docs: Worker tick에서 KIS network 호출 제거)."""
        if self._history_updater_thread is not None and self._history_updater_thread.is_alive():
            return
        # Same fresh-Event-per-generation fix as start_quote_updater() above —
        # a shared Event across restarts lets an old, still-blocked thread
        # resume forever instead of exiting once its current call returns.
        stop_event = threading.Event()
        self._history_updater_stop = stop_event

        def _loop() -> None:
            while not stop_event.is_set():
                try:
                    self.merge_incremental_1m()
                except Exception as exc:
                    # 2026-08-26 diagnostic log -- pure logging; the loop
                    # still swallows and continues exactly as before.
                    logger.warning(
                        "[MACD2][history-updater loop] %s: %s", type(exc).__name__, exc,
                    )
                stop_event.wait(interval_sec)

        logger.info("[MACD2][history-updater] starting new thread, interval_sec=%s", interval_sec)
        self._history_updater_thread = threading.Thread(target=_loop, daemon=True, name="macd2-history-updater")
        self._history_updater_thread.start()

    def stop_history_updater(self, join_timeout: float = 2.0) -> bool:
        """Returns True only if the thread is confirmed dead after the join
        -- same orphan-detection fix as stop_quote_updater() above."""
        self._history_updater_stop.set()
        thread = self._history_updater_thread
        if thread is None:
            return True
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            logger.warning(
                "[MACD2][history-updater] stop NOT confirmed after join_timeout=%ss -- thread still alive (orphaned)",
                join_timeout,
            )
            return False
        self._history_updater_thread = None
        logger.info("[MACD2][history-updater] stopped and confirmed dead")
        return True

    def history_updater_alive(self) -> bool:
        return bool(self._history_updater_thread and self._history_updater_thread.is_alive())


# 2026-08-26 diagnostic-logging-only addition (docs: today's flag-detection
# incident) -- module-level (filter_complete_3m_bars is a free function, not
# a MarketDataService method, so it has no instance to hold this on) set of
# bar_start values already logged as a HISTORY_GAP. run_once() calls this
# function every WORKER_INTERVAL_SEC with the FULL day's bars_3m, so a
# persistently-incomplete bar would otherwise be re-logged every single tick
# for as long as it stays incomplete -- this caps each bar_start to exactly
# one log line ever. Never read by any decision path (filter_complete_3m_bars'
# own return values are completely unaffected by this set's contents).
_HISTORY_GAP_LOGGED: set[Any] = set()


def filter_complete_3m_bars(
    bars_3m: pd.DataFrame, one_minute_bars: pd.DataFrame,
) -> tuple[pd.DataFrame, list[Any]]:
    """Drop any completed 3-minute bar whose 3 constituent 1-minute bars are
    not ALL present with a valid close in ``one_minute_bars`` (docs §4: an
    API error or a dropped/empty page must never silently masquerade as a
    real, complete 3-minute bar — never fill or interpolate the gap). A bar
    is only ever treated as "confirmed" when its own 3 one-minute bars are
    all actually there; anything else is simply excluded from the returned
    frame (never included with partial data), so the caller's MACD/EMA
    series only ever runs over genuinely complete bars.

    Returns ``(filtered_bars_3m, dropped_bar_starts)`` — the caller decides
    how to surface a non-empty ``dropped_bar_starts`` (e.g. HISTORY_GAP).

    2026-07-31: a volume>0 variant of this check was tried (treating a
    zero-volume 1-minute bar as incomplete) to explain a 000660 opening-auction
    data artifact, but it did not reproduce KIS's own observed 09:00 UP_RED /
    09:15 DOWN_BLUE flags against real data — reverted, see the startup-gate
    fix in service.py/worker.py for that day's actual root cause (Worker never
    confirmed started, docs §startup-lifecycle).
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
            # 2026-08-26 diagnostic log (docs: today's flag-detection
            # incident) -- pure logging; which specific 1-minute bar(s) are
            # missing for this dropped 3-minute bin, never fabricated/filled.
            # Logged at most once ever per bar_start (see _HISTORY_GAP_LOGGED
            # above) -- this function is called every tick with the full
            # day's bars_3m, so without this a persistent gap would
            # otherwise re-log identically every ~5s for as long as it
            # stays incomplete.
            if bar_start not in _HISTORY_GAP_LOGGED:
                _HISTORY_GAP_LOGGED.add(bar_start)
                missing = [m for m in needed if m not in have]
                logger.warning(
                    "[MACD2][HISTORY_GAP] 3m bar %s dropped -- missing 1m bar(s): %s",
                    bar_start, [m.isoformat() for m in missing],
                )
    filtered = bars_3m.loc[keep_mask].reset_index(drop=True)
    return filtered, dropped
