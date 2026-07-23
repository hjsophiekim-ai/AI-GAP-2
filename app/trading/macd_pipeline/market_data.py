"""MarketDataService — quotes/bars into cache ONLY. No signals, no orders."""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from app.logger import logger
from app.trading.macd_hynix_strategy import (
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    WARMUP_1M_BARS,
    WARMUP_3M_BARS,
    resample_completed_3m,
)
from app.utils.data_paths import CACHE_DIR

KIS_MINUTE_API = "inquire-time-itemchartprice"
KIS_PAGE_SIZE = 30
KIS_MAX_PAGES = 20
QUOTE_ORDER_MAX_AGE_SEC = 10.0
QUOTE_UPDATER_INTERVAL_SEC = 1.0
HOT_QUOTE_TIMEOUT_SEC = 10.0
HOT_MINUTE_TIMEOUT_SEC = 10.0
WARMUP_LOOKBACK_DAYS = 8

_QUOTE_SLOTS: tuple[tuple[str, str], ...] = (
    ("hynix", SIGNAL_SYMBOL),
    ("long", LONG_SYMBOL),
    ("inverse", INVERSE_SYMBOL),
)

_KIS_IO_LOCK = threading.RLock()
_history_lock = threading.Lock()
_HISTORY: dict[str, Any] = {
    "session_date": None,
    "df_1m": None,
    "bootstrap_ok": False,
    "diag": {},
}
_quote_lock = threading.Lock()
_QUOTE_CACHE: dict[str, Any] = {
    "quotes": {},
    "updated_at": None,
    "updated_mono": None,
    "last_good": {},
    "last_good_mono": None,
    "last_error": None,
    "mode": None,
}
_quote_thread: Optional[threading.Thread] = None
_quote_stop = threading.Event()


def kis_io_lock() -> threading.RLock:
    return _KIS_IO_LOCK


def _now_kst() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)


def _read_1m_csv(path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path)
        if "datetime" not in df.columns:
            return pd.DataFrame()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.warning("[MACD-MD] csv read failed %s: %s", path, exc)
        return pd.DataFrame()


def _weekday_prior_dates(today: datetime, n: int = WARMUP_LOOKBACK_DAYS) -> list[str]:
    out: list[str] = []
    d = today.date() if hasattr(today, "date") else pd.Timestamp(today).date()
    cur = d
    while len(out) < n:
        cur = cur - timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
    return out


def load_prior_history_1m(now: Optional[datetime] = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    now = now or _now_kst()
    sources: list[str] = []
    frames: list[pd.DataFrame] = []
    days = _weekday_prior_dates(now)
    naver_path = CACHE_DIR / "naver_multi_1m" / "000660_1m.csv"
    naver_df = _read_1m_csv(naver_path) if naver_path.exists() else pd.DataFrame()
    if not naver_df.empty:
        sources.append(str(naver_path))
    for day in days:
        tag = day.replace("-", "")
        replay = CACHE_DIR / f"replay_{tag}_hynix_1m.csv"
        if replay.exists():
            part = _read_1m_csv(replay)
            if not part.empty:
                frames.append(part)
                sources.append(replay.name)
        if not naver_df.empty:
            mask = naver_df["datetime"].dt.strftime("%Y-%m-%d") == day
            part = naver_df.loc[mask]
            if not part.empty:
                frames.append(part)
        merged = (
            pd.concat(frames, ignore_index=True).drop_duplicates("datetime")
            if frames else pd.DataFrame()
        )
        if len(merged) >= WARMUP_1M_BARS:
            break
    if not frames and not naver_df.empty:
        frames.append(naver_df.tail(WARMUP_1M_BARS + 50))
    if not frames:
        return pd.DataFrame(), {
            "received_1m_bars": 0,
            "failure_reason": "NO_PRIOR_1M_CACHE",
            "sources_tried": sources,
            "prior_days_scanned": days,
        }
    df = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    if len(df) > WARMUP_1M_BARS + 30:
        df = df.tail(WARMUP_1M_BARS + 30).reset_index(drop=True)
    t0 = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t1 = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    return df, {
        "received_1m_bars": int(len(df)),
        "failure_reason": None if len(df) >= WARMUP_1M_BARS else "PRIOR_1M_SHORT",
        "sources_tried": list(dict.fromkeys(sources)),
        "time_range": {"first": t0, "last": t1},
        "prior_days_scanned": days,
    }


def _candles_to_df(candles: list, today: str) -> pd.DataFrame:
    rows: list[dict] = []
    for c in candles or []:
        hhmmss = str(c.get("time") or "").strip().replace(":", "")
        if len(hhmmss) < 6:
            continue
        hhmmss = hhmmss[:6]
        raw_date = str(c.get("date") or "").strip().replace("-", "")
        ymd = raw_date[:8] if len(raw_date) >= 8 and raw_date[:8].isdigit() else str(today).replace("-", "")[:8]
        try:
            ts = datetime.strptime(f"{ymd}{hhmmss}", "%Y%m%d%H%M%S")
        except ValueError:
            continue
        rows.append({
            "datetime": ts,
            "open": float(c.get("open") or 0),
            "high": float(c.get("high") or 0),
            "low": float(c.get("low") or 0),
            "close": float(c.get("close") or 0),
            "volume": int(c.get("volume") or 0),
        })
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    )


def fetch_kis_minute_page(
    mode: str,
    today: str,
    *,
    hour1: str = "",
    count: int = KIS_PAGE_SIZE,
    timeout_sec: float = HOT_MINUTE_TIMEOUT_SEC,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    err: Optional[str] = None
    candles: list = []
    try:
        from app.trading.kis_client import create_kis_client

        client = create_kis_client(mode if mode in ("mock", "real") else "mock")
        if client is None:
            err = "kis_client_none"
        else:
            with _KIS_IO_LOCK:
                candles = client.get_minute_candles(
                    SIGNAL_SYMBOL, period_min=1, count=count, hour1=hour1
                ) or []
    except Exception as exc:
        err = repr(exc)
        logger.warning("[MACD-MD] minute fetch failed: %s", exc)
    df = _candles_to_df(candles, today)
    return df, {
        "api_name": KIS_MINUTE_API,
        "requested_to": hour1 or "LATEST",
        "received_count": int(len(df)),
        "oldest": pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None,
        "newest": pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None,
        "failure_reason": err,
    }


def fetch_kis_minute_paged(
    mode: str,
    today: str,
    *,
    target_bars: int = 300,
    require_prior_day: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Page backwards; cursor = oldest - 1m (KIS FID_INPUT_HOUR_1 is inclusive)."""
    pages: list[pd.DataFrame] = []
    diags: list[dict] = []
    hour1 = ""
    today_ymd = str(today).replace("-", "")[:8]
    prev_count = 0
    for page_i in range(KIS_MAX_PAGES):
        part, diag = fetch_kis_minute_page(mode, today, hour1=hour1)
        diag = {**diag, "request_no": page_i + 1}
        diags.append(diag)
        logger.warning(
            "[MACD-MD] page request_no=%s requested_to=%s received_count=%s oldest=%s newest=%s",
            diag["request_no"], diag["requested_to"], diag["received_count"],
            diag["oldest"], diag["newest"],
        )
        if part.empty:
            break
        pages.append(part)
        merged = (
            pd.concat(pages, ignore_index=True)
            .drop_duplicates("datetime")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        if len(merged) <= prev_count:
            diag["failure_reason"] = "PAGE_NO_GROWTH_INCLUSIVE_CURSOR"
            break
        prev_count = len(merged)
        dates = pd.to_datetime(merged["datetime"]).dt.strftime("%Y%m%d")
        has_prior = bool((dates != today_ymd).any())
        if len(merged) >= target_bars and (has_prior or not require_prior_day):
            pages = [merged]
            break
        oldest = pd.Timestamp(part["datetime"].iloc[0])
        next_h = (oldest - timedelta(minutes=1)).strftime("%H%M%S")
        if next_h == hour1:
            break
        hour1 = next_h
        time.sleep(0.12)
    if not pages:
        return pd.DataFrame(), {
            "pages": diags,
            "kis_requests": len(diags),
            "received_1m_bars": 0,
            "failure_reason": diags[-1].get("failure_reason") if diags else "NO_PAGES",
        }
    df = (
        pd.concat(pages, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    dates = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
    return df, {
        "pages": diags,
        "kis_requests": len(diags),
        "received_1m_bars": int(len(df)),
        "prior_day_1m_bars": int((dates != today_ymd).sum()),
        "today_1m_bars": int((dates == today_ymd).sum()),
        "time_range": {
            "first": pd.Timestamp(df["datetime"].iloc[0]).isoformat(),
            "last": pd.Timestamp(df["datetime"].iloc[-1]).isoformat(),
        },
        "failure_reason": None,
    }


def bootstrap_history(mode: str = "mock", *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Once on Start: ≥300×1m + ≥100×3m including prior day. Fail if today-only."""
    now = now or _now_kst()
    today = now.strftime("%Y-%m-%d")
    t0 = time.monotonic()
    prior_df, prior_diag = load_prior_history_1m(now)
    need_kis_prior = prior_df.empty or len(prior_df) < WARMUP_1M_BARS
    live_df, live_diag = fetch_kis_minute_paged(
        mode, today, target_bars=max(WARMUP_1M_BARS, 300), require_prior_day=need_kis_prior
    )
    if live_df.empty:
        cache = CACHE_DIR / "hynix_minute_1m.csv"
        if cache.exists():
            live_df = _read_1m_csv(cache)
            live_diag = {**live_diag, "cache_fallback": str(cache), "received_1m_bars": int(len(live_df))}
    frames = [f for f in (prior_df, live_df) if f is not None and not f.empty]
    if not frames:
        boot = {
            "status": "FAILED", "ok": False, "reason": "NO_1M_BARS",
            "elapsed_sec": round(time.monotonic() - t0, 3),
            "prior": prior_diag, "live": live_diag,
            "received_1m_bars": 0, "completed_3m_count": 0,
        }
        with _history_lock:
            _HISTORY.update({"session_date": today, "df_1m": pd.DataFrame(), "bootstrap_ok": False, "diag": boot})
        return boot
    df = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    if len(df) > WARMUP_1M_BARS + 120:
        df = df.tail(WARMUP_1M_BARS + 120).reset_index(drop=True)
    bars3 = resample_completed_3m(df, now=now)
    today_ymd = today.replace("-", "")[:8]
    dates = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
    prior_n = int((dates != today_ymd).sum())
    today_n = int((dates == today_ymd).sum())
    today_only = prior_n <= 0
    bars_ok = len(df) >= WARMUP_1M_BARS and len(bars3) >= WARMUP_3M_BARS
    ok = bars_ok and not today_only
    if today_only:
        reason = "TODAY_ONLY_NO_PRIOR_DAY"
    elif len(df) < WARMUP_1M_BARS:
        reason = f"WARMUP_1M_LT_{WARMUP_1M_BARS}"
    elif len(bars3) < WARMUP_3M_BARS:
        reason = f"WARMUP_LT_{WARMUP_3M_BARS}"
    else:
        reason = None
    elapsed = round(time.monotonic() - t0, 3)
    boot = {
        "status": "OK" if ok else ("SHORT" if len(df) else "FAILED"),
        "ok": ok,
        "reason": reason,
        "elapsed_sec": elapsed,
        "kis_requests": int((live_diag or {}).get("kis_requests") or 0),
        "received_1m_bars": int(len(df)),
        "prior_day_1m_bars": prior_n,
        "today_1m_bars": today_n,
        "completed_3m_count": int(len(bars3)),
        "time_range": {
            "first": pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None,
            "last": pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None,
        },
        "prior": prior_diag,
        "live": {k: v for k, v in (live_diag or {}).items() if k != "pages"},
    }
    with _history_lock:
        _HISTORY.update({"session_date": today, "df_1m": df.copy(), "bootstrap_ok": ok, "diag": boot})
    logger.warning(
        "[MACD-MD] bootstrap ok=%s 1m=%s prior=%s today=%s 3m=%s reason=%s",
        ok, len(df), prior_n, today_n, len(bars3), reason,
    )
    return boot


def get_history_df() -> pd.DataFrame:
    with _history_lock:
        df = _HISTORY.get("df_1m")
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()


def merge_incremental_1m(mode: str, *, now: Optional[datetime] = None) -> pd.DataFrame:
    """One latest page merge into cache (Worker may call; still data-only)."""
    now = now or _now_kst()
    today = now.strftime("%Y-%m-%d")
    with _history_lock:
        base = _HISTORY.get("df_1m")
        base = base.copy() if isinstance(base, pd.DataFrame) else pd.DataFrame()
    live, _ = fetch_kis_minute_page(mode, today, hour1="")
    if live.empty:
        return base
    df = (
        pd.concat([base, live], ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    if len(df) > WARMUP_1M_BARS + 120:
        df = df.tail(WARMUP_1M_BARS + 120).reset_index(drop=True)
    with _history_lock:
        _HISTORY["df_1m"] = df.copy()
        _HISTORY["session_date"] = today
    return df


def _quote_one(broker, symbol: str) -> dict[str, Any]:
    symbol = str(symbol)
    t0 = time.monotonic()
    try:
        with _KIS_IO_LOCK:
            if hasattr(broker, "kis") and hasattr(broker.kis, "get_current_price"):
                raw = broker.kis.get_current_price(symbol)
            elif hasattr(broker, "get_current_price"):
                raw = broker.get_current_price(symbol)
            else:
                return {"ok": False, "symbol": symbol, "error_message": "no get_current_price", "price": None}
        if isinstance(raw, dict):
            price = float(raw.get("current_price") or raw.get("price") or 0)
            ok = price > 0
            return {
                "ok": ok,
                "price": price if ok else None,
                "change_pct": raw.get("change_rate"),
                "symbol": symbol,
                "updated_at": datetime.now().isoformat(),
                "rt_cd": raw.get("rt_cd"),
                "msg_cd": raw.get("msg_cd"),
                "msg1": raw.get("msg1"),
                "http_status": raw.get("http_status"),
                "elapsed_sec": round(time.monotonic() - t0, 3),
                "url_kind": raw.get("url_kind") or "inquire-price",
                "error_message": None if ok else (raw.get("error") or raw.get("msg1") or "non-positive"),
                "api_function": "broker.kis.get_current_price",
            }
        price = float(raw or 0)
        ok = price > 0
        return {
            "ok": ok, "price": price if ok else None, "symbol": symbol,
            "updated_at": datetime.now().isoformat(),
            "elapsed_sec": round(time.monotonic() - t0, 3),
            "error_message": None if ok else "non-positive",
            "api_function": "broker.get_current_price",
        }
    except Exception as exc:
        api_fn = (
            "broker.kis.get_current_price"
            if hasattr(broker, "kis") and hasattr(getattr(broker, "kis", None), "get_current_price")
            else "broker.get_current_price"
        )
        return {
            "ok": False, "price": None, "symbol": symbol,
            "error_message": repr(exc),
            "exception_class": type(exc).__name__,
            "exception_repr": repr(exc),
            "traceback": traceback.format_exc(),
            "elapsed_sec": round(time.monotonic() - t0, 3),
            "updated_at": datetime.now().isoformat(),
            "api_function": api_fn,
            "retry_count": 1,
        }


def store_quotes(quotes: dict[str, Any], *, mode: str) -> None:
    mono = time.monotonic()
    now = datetime.now().isoformat()
    with _quote_lock:
        _QUOTE_CACHE["quotes"] = dict(quotes)
        _QUOTE_CACHE["updated_at"] = now
        _QUOTE_CACHE["updated_mono"] = mono
        _QUOTE_CACHE["mode"] = mode
        lg = dict(_QUOTE_CACHE.get("last_good") or {})
        for k, q in quotes.items():
            if q.get("ok") and q.get("price"):
                lg[k] = dict(q)
        if lg:
            _QUOTE_CACHE["last_good"] = lg
            _QUOTE_CACHE["last_good_mono"] = mono


def read_quote_cache(*, max_age_sec: float = QUOTE_ORDER_MAX_AGE_SEC) -> dict[str, Any]:
    """Worker reads this — never blocks on live HTTP."""
    with _quote_lock:
        quotes = dict(_QUOTE_CACHE.get("quotes") or {})
        updated_mono = _QUOTE_CACHE.get("updated_mono")
        last_good = dict(_QUOTE_CACHE.get("last_good") or {})
        last_good_mono = _QUOTE_CACHE.get("last_good_mono")
        updated_at = _QUOTE_CACHE.get("updated_at")
    age = (time.monotonic() - float(updated_mono)) if updated_mono is not None else None
    out: dict[str, Any] = {}
    for key, sym in _QUOTE_SLOTS:
        q = quotes.get(key) or {}
        if q.get("ok") and q.get("price") and age is not None and age <= max_age_sec:
            out[key] = {**q, "quote_age_sec": age, "from_cache": True, "cache_fresh": True}
        elif last_good.get(key):
            lg_age = (time.monotonic() - float(last_good_mono)) if last_good_mono is not None else None
            fresh = lg_age is not None and lg_age <= max_age_sec
            lg = dict(last_good[key])
            lg.update({
                "ok": bool(lg.get("price")) and fresh,
                "quote_age_sec": lg_age,
                "from_cache": True,
                "cache_fresh": False,
                "last_good": True,
                "error_message": None if fresh else f"LAST_GOOD_STALE age={lg_age}",
            })
            out[key] = lg
        else:
            out[key] = {
                "ok": False, "price": None, "symbol": sym,
                "error_message": "QUOTE_CACHE_EMPTY", "from_cache": True, "quote_age_sec": age,
            }
    out["_meta"] = {"updated_at": updated_at, "age_sec": age, "max_age_sec": max_age_sec}
    return out


def _quote_updater_loop(mode: str) -> None:
    from app.trading import macd_hynix_order_manager as om

    logger.warning("[MACD-MD] quote updater started mode=%s", mode)
    while not _quote_stop.is_set():
        t0 = time.monotonic()
        try:
            if mode == "mock":
                broker = om.create_macd_broker("mock")
            else:
                st = om.load_state()
                ready = bool(st.get("real_confirm_ok"))
                confirm = ""
                if ready:
                    from app.config import get_config
                    confirm = str(get_config().real_confirm_text() or "")
                broker = om.create_macd_broker(mode, real_confirm_text=confirm, real_ready=ready)
            quotes: dict[str, Any] = {}
            for key, sym in _QUOTE_SLOTS:
                if _quote_stop.is_set():
                    break
                quotes[key] = _quote_one(broker, sym)
            if quotes:
                store_quotes(quotes, mode=mode)
                with _quote_lock:
                    _QUOTE_CACHE["last_error"] = None
        except Exception as exc:
            tb = traceback.format_exc()
            logger.warning("[MACD-MD] quote updater error: %r\n%s", exc, tb)
            with _quote_lock:
                _QUOTE_CACHE["last_error"] = f"{exc!r}\n{tb}"
        wait = max(0.2, QUOTE_UPDATER_INTERVAL_SEC - (time.monotonic() - t0))
        _quote_stop.wait(wait)
    logger.warning("[MACD-MD] quote updater stopped")


def start_quote_updater(mode: str = "mock") -> None:
    global _quote_thread
    stop_quote_updater()
    _quote_stop.clear()
    _quote_thread = threading.Thread(
        target=_quote_updater_loop,
        args=(mode if mode in ("mock", "real") else "mock",),
        name="macd-quote-updater",
        daemon=True,
    )
    _quote_thread.start()


def stop_quote_updater(*, join_timeout: float = 2.0) -> None:
    global _quote_thread
    _quote_stop.set()
    t = _quote_thread
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=join_timeout)
    if t is not None and not t.is_alive():
        _quote_thread = None


def quote_updater_alive() -> bool:
    return bool(_quote_thread and _quote_thread.is_alive())


def clear_history() -> None:
    with _history_lock:
        _HISTORY.update({"session_date": None, "df_1m": None, "bootstrap_ok": False, "diag": {}})


def clear_quote_cache() -> None:
    with _quote_lock:
        _QUOTE_CACHE.update({
            "quotes": {}, "updated_at": None, "updated_mono": None,
            "last_good": {}, "last_good_mono": None, "last_error": None,
        })
