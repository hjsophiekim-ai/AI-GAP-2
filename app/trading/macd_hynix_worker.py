"""Isolated 5-second fixed-schedule worker for MACD Hynix auto trading.

Does not call Enhanced / WOC / Early / Active / Fusion / Regime / Prediction.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.logger import logger
from app.trading import macd_hynix_order_manager as om
from app.trading.macd_hynix_strategy import (
    CONTINUATION_REENTRY_ENABLED,
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    ENTRY_CONTINUATION,
    ENTRY_INITIAL,
    ENTRY_OPEN_IMMEDIATE,
    ENTRY_OPEN_SCALE,
    EXIT_OPPOSITE,
    EXIT_PROFIT_LOCK,
    EXIT_SL,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    OPEN_IMMEDIATE_BUDGET_FRACTION,
    OPENING_PROBE_ENABLED,
    SIGNAL_SOURCE_CONTINUATION,
    SIGNAL_SOURCE_OPEN_IMMEDIATE,
    SIGNAL_SYMBOL,
    WARMUP_1M_BARS,
    WARMUP_3M_BARS,
    compute_warmup_macd,
    evaluate_continuation_reentry,
    evaluate_macd_direction,
    evaluate_opening_probe,
    evaluate_position_exits,
    first_regular_3m_bar_closed,
    in_open_probe_window,
    open_probe_window_expired,
    opening_probe_b_confirms,
    opposite_symbol,
    resample_completed_3m,
    tail_prior_day_1m,
    target_symbol_for_direction,
)
from app.trading.macd_hynix_order_manager import SIGNAL_SOURCE

KST = ZoneInfo("Asia/Seoul")
TICK_SECONDS = 5.0
SESSION_START = (9, 0)
NO_NEW_SWITCH_AFTER = (14, 55)
FORCE_LIQUIDATE_AT = (15, 0)
# Prior trading days to scan for warm-up 1m caches (weekends skipped).
WARMUP_LOOKBACK_DAYS = 8
KIS_MINUTE_API = "inquire-time-itemchartprice"

_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_wake_event = threading.Event()
_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "alive": False,
    "last_tick_at": None,
    "tick_intervals": [],
    "started_at": None,
    "last_error": None,
    "main_cycle_3m_wait_count": 0,
}


def _now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def _hhmm(dt: datetime) -> tuple[int, int]:
    return dt.hour, dt.minute


def in_trading_session(now: Optional[datetime] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    return hm >= SESSION_START and hm < (15, 30)


def allow_new_switch(now: Optional[datetime] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    return hm >= SESSION_START and hm < NO_NEW_SWITCH_AFTER


def should_force_liquidate(now: Optional[datetime] = None, done_date: Optional[str] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    today = now.strftime("%Y-%m-%d")
    if done_date == today:
        return False
    return hm >= FORCE_LIQUIDATE_AT


def _p95(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _avg(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def get_worker_status() -> dict[str, Any]:
    with _status_lock:
        intervals = list(_status.get("tick_intervals") or [])
        return {
            **dict(_status),
            "tick_intervals": intervals[-40:],
            "avg_interval": _avg(intervals[-20:]),
            "p95_interval": _p95(intervals[-20:]),
            "main_cycle_3m_wait_count": int(_status.get("main_cycle_3m_wait_count") or 0),
            "thread_alive": bool(_worker_thread and _worker_thread.is_alive()),
        }


def _load_minute_df(mode: str, count: int = 120) -> pd.DataFrame:
    """Fetch 000660 1m bars via KIS; fall back to local cache CSV."""
    df, _diag = load_macd_minute_history(mode, count=count, now=_now_kst())
    return df


def _weekday_prior_dates(today: datetime, n: int = WARMUP_LOOKBACK_DAYS) -> list[str]:
    """Recent Mon–Fri calendar dates strictly before ``today`` (KST naive)."""
    out: list[str] = []
    d = today.date() if hasattr(today, "date") else today
    from datetime import date as date_cls

    if not isinstance(d, date_cls):
        d = pd.Timestamp(today).date()
    cur = d
    while len(out) < n:
        cur = cur - timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
    return out


def _read_1m_csv(path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path)
        if "datetime" not in df.columns:
            return pd.DataFrame()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.warning("[MACDHynix] 1m csv read failed %s: %s", path, exc)
        return pd.DataFrame()


def _load_prior_day_minute_df(mode: str, day: str) -> pd.DataFrame:
    """Prior trading day 000660 1m from replay cache (warm-up)."""
    from app.utils.data_paths import CACHE_DIR

    tag = day.replace("-", "")
    path = CACHE_DIR / f"replay_{tag}_hynix_1m.csv"
    if path.exists():
        return _read_1m_csv(path)
    return pd.DataFrame()


def _load_prior_history_1m(now: datetime) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load prior trading-day 1m bars until ≥ WARMUP_1M_BARS (or best effort).

    Sources (in order per day): replay_YYYYMMDD_hynix_1m.csv, then naver multi 1m.
    """
    from app.utils.data_paths import CACHE_DIR

    sources_tried: list[str] = []
    frames: list[pd.DataFrame] = []
    days = _weekday_prior_dates(now, WARMUP_LOOKBACK_DAYS)
    naver_path = CACHE_DIR / "naver_multi_1m" / "000660_1m.csv"
    naver_df = _read_1m_csv(naver_path) if naver_path.exists() else pd.DataFrame()
    if not naver_df.empty:
        sources_tried.append(str(naver_path))

    for day in days:
        day_frames: list[pd.DataFrame] = []
        replay = _load_prior_day_minute_df("mock", day)
        if not replay.empty:
            day_frames.append(replay)
            sources_tried.append(f"replay_{day.replace('-', '')}_hynix_1m.csv")
        if not naver_df.empty:
            mask = naver_df["datetime"].dt.strftime("%Y-%m-%d") == day
            part = naver_df.loc[mask]
            if not part.empty:
                day_frames.append(part)
        if day_frames:
            frames.extend(day_frames)
        merged_so_far = (
            pd.concat(frames, ignore_index=True).drop_duplicates("datetime")
            if frames else pd.DataFrame()
        )
        if len(merged_so_far) >= WARMUP_1M_BARS:
            break

    if not frames:
        # Last-resort: entire naver multi file (may span many days)
        if not naver_df.empty:
            frames.append(naver_df.tail(WARMUP_1M_BARS + 50))

    if not frames:
        return pd.DataFrame(), {
            "api_name": "local_prior_1m",
            "sources_tried": sources_tried,
            "prior_days_scanned": days,
            "received_1m_bars": 0,
            "time_range": None,
            "failure_reason": "NO_PRIOR_1M_CACHE",
        }

    df = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    # Prefer most recent warm-up window
    if len(df) > WARMUP_1M_BARS + 30:
        df = df.tail(WARMUP_1M_BARS + 30).reset_index(drop=True)
    t0 = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t1 = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    return df, {
        "api_name": "local_prior_1m",
        "sources_tried": list(dict.fromkeys(sources_tried)),
        "prior_days_scanned": days,
        "received_1m_bars": int(len(df)),
        "time_range": {"first": t0, "last": t1},
        "failure_reason": None if len(df) >= WARMUP_1M_BARS else "PRIOR_1M_SHORT",
    }


def _fetch_kis_minute_1m(mode: str, count: int, today: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Live KIS 1m bars for 000660 (today session). Uses stck_bsop_date when present."""
    rows: list[dict] = []
    err: Optional[str] = None
    requested = int(count)
    try:
        from app.trading.kis_client import create_kis_client

        client = create_kis_client(mode if mode in ("mock", "real") else "mock")
        if client is None:
            err = "kis_client_none"
        else:
            candles = client.get_minute_candles(SIGNAL_SYMBOL, period_min=1, count=count) or []
            for c in candles:
                hhmmss = str(c.get("time") or "").strip().replace(":", "")
                if len(hhmmss) < 6:
                    continue
                hhmmss = hhmmss[:6]
                raw_date = str(c.get("date") or "").strip().replace("-", "")
                if len(raw_date) >= 8 and raw_date[:8].isdigit():
                    ymd = raw_date[:8]
                else:
                    # ``today`` may be ISO (YYYY-MM-DD); strip to YYYYMMDD for strptime.
                    ymd = str(today).replace("-", "")[:8]
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
    except Exception as exc:
        err = str(exc)
        logger.warning("[MACDHynix] minute fetch failed: %s", exc)

    df = pd.DataFrame()
    if rows:
        df = (
            pd.DataFrame(rows)
            .drop_duplicates("datetime")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
    t0 = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t1 = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    return df, {
        "api_name": KIS_MINUTE_API,
        "requested_1m_bars": requested,
        "received_1m_bars": int(len(df)),
        "time_range": {"first": t0, "last": t1},
        "failure_reason": err,
    }


def load_macd_minute_history(
    mode: str,
    *,
    count: int = 120,
    now: Optional[datetime] = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Prior-day warm-up 1m + live/today 1m merged for MACD EMA / signals.

    Always preloads prior trading-day history first so open does not wait for
    ~78 minutes of same-day bars (WARMUP_LT_26).
    """
    now = now or _now_kst()
    today = now.strftime("%Y-%m-%d")
    prior_df, prior_diag = _load_prior_history_1m(now)
    live_df, live_diag = _fetch_kis_minute_1m(mode, count, today)

    # Cache fallback when KIS empty (typical pre-open)
    cache_diag: dict[str, Any] = {"used": False}
    if live_df.empty:
        from app.utils.data_paths import CACHE_DIR

        cache = CACHE_DIR / "hynix_minute_1m.csv"
        if cache.exists():
            cached = _read_1m_csv(cache)
            if not cached.empty:
                live_df = cached
                cache_diag = {
                    "used": True,
                    "path": str(cache),
                    "received_1m_bars": int(len(cached)),
                }

    frames = [f for f in (prior_df, live_df) if f is not None and not f.empty]
    if not frames:
        diag = {
            "api_name": KIS_MINUTE_API,
            "prior": prior_diag,
            "live": live_diag,
            "cache": cache_diag,
            "received_1m_bars": 0,
            "completed_3m_count": 0,
            "failure_reason": "NO_1M_BARS",
        }
        return pd.DataFrame(), diag

    df = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    bars3 = resample_completed_3m(df, now=now)
    t0 = pd.Timestamp(df["datetime"].iloc[0]).isoformat() if len(df) else None
    t1 = pd.Timestamp(df["datetime"].iloc[-1]).isoformat() if len(df) else None
    diag = {
        "api_name": KIS_MINUTE_API,
        "prior": prior_diag,
        "live": live_diag,
        "cache": cache_diag,
        "requested_1m_bars": WARMUP_1M_BARS,
        "received_1m_bars": int(len(df)),
        "completed_1m_count": int(len(df)),
        "completed_3m_count": int(len(bars3)),
        "time_range": {"first": t0, "last": t1},
        "last_bar_time": t1,
        "resample_boundary": "3min floor; bar included when open+3m <= now",
        "failure_reason": None if len(bars3) >= WARMUP_3M_BARS else (
            f"WARMUP_LT_{WARMUP_3M_BARS}" if len(bars3) >= 3 else "DATA_INSUFFICIENT"
        ),
    }
    return df, diag


def _prior_session_date(today: datetime) -> str:
    days = _weekday_prior_dates(today, 1)
    return days[0] if days else (today - timedelta(days=1)).strftime("%Y-%m-%d")


def _ensure_macd_warmup(
    state: dict[str, Any],
    df_1m: pd.DataFrame,
    now: datetime,
    *,
    load_diag: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Always-on warm-up (independent of OPENING_PROBE_ENABLED).

    Sets opening_probe.warmup_ready when ≥100 completed 3m bars are available
    from prior+live history — never waits for same-day intraday accumulation.
    """
    op = state.setdefault("opening_probe", {})
    # Prefer pure prior-day tail for probe hist snapshot; fall back to full df.
    prior_day = _prior_session_date(now)
    prev_df = _load_prior_day_minute_df("mock", prior_day)
    warmup_1m = tail_prior_day_1m(prev_df) if not prev_df.empty else pd.DataFrame()
    if warmup_1m.empty and df_1m is not None and not df_1m.empty:
        # Use all non-today bars from the merged feed as warm-up seed
        today = now.strftime("%Y-%m-%d")
        mask = pd.to_datetime(df_1m["datetime"]).dt.strftime("%Y-%m-%d") < today
        warmup_1m = df_1m.loc[mask].tail(WARMUP_1M_BARS).reset_index(drop=True)
        if warmup_1m.empty:
            warmup_1m = df_1m.tail(WARMUP_1M_BARS).reset_index(drop=True)

    warm = compute_warmup_macd(warmup_1m, now=None, diagnostics=load_diag or {})
    # If dedicated prior warm-up short but merged feed already has ≥100 3m, mark ready.
    if not warm.get("ok") and df_1m is not None and not df_1m.empty:
        merged_bars = resample_completed_3m(df_1m, now=now)
        if len(merged_bars) >= WARMUP_3M_BARS:
            warm = compute_warmup_macd(
                df_1m.tail(max(WARMUP_1M_BARS, len(df_1m))).reset_index(drop=True),
                now=now,
                diagnostics=load_diag or {},
            )

    op["warmup_ready"] = bool(warm.get("ok"))
    op["warmup_reason"] = warm.get("reason")
    op["warmup_hist_last2"] = warm.get("hist_last2") or []
    op["warmup_hist_deltas"] = warm.get("hist_deltas") or []
    if warm.get("hist_last3"):
        op["warmup_hist_last3"] = warm.get("hist_last3")
    state["opening_warmup_macd"] = warm
    state["macd_warmup_diagnostics"] = warm.get("diagnostics") or load_diag or {}
    return warm


def _refresh_opening_warmup(state: dict[str, Any], df_1m: pd.DataFrame, now: datetime, mode: str) -> None:
    """Compat wrapper for opening-probe path — delegates to always-on warm-up."""
    _ensure_macd_warmup(state, df_1m, now)


def _record_hynix_sample(state: dict[str, Any], now: datetime, price: Optional[float]) -> None:
    if price is None or price <= 0:
        return
    op = state.setdefault("opening_probe", {})
    samples = list(op.get("price_samples_5s") or [])
    samples.append([now.isoformat(), float(price)])
    op["price_samples_5s"] = samples[-12:]


def _quote_cache_path(symbol: str) -> Optional[Path]:
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2] / "data" / "cache"
    mapping = {
        SIGNAL_SYMBOL: root / "hynix_current.json",
        LONG_SYMBOL: root / "hynix_long_current.json",
        INVERSE_SYMBOL: root / "hynix_inverse_current.json",
    }
    return mapping.get(symbol)


def _quote_from_local_cache(symbol: str, *, max_age_sec: float = 600.0) -> Optional[dict[str, Any]]:
    """Fallback ETF/hynix quote from data/cache/*.json when KIS inquire-price fails."""
    path = _quote_cache_path(symbol)
    if path is None or not path.exists():
        return None
    try:
        import json as _json

        raw = _json.loads(path.read_text(encoding="utf-8"))
        price = float(raw.get("current_price") or raw.get("price") or 0)
        if price <= 0:
            return None
        cached_at = str(raw.get("cached_at") or "")
        if cached_at:
            try:
                age = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds()
                if age > max_age_sec:
                    return None
            except Exception:
                pass
        return {
            "price": price,
            "change_pct": raw.get("change_rate") or raw.get("change_pct"),
            "bid": None,
            "ask": None,
            "updated_at": cached_at or datetime.now().isoformat(),
            "ok": True,
            "api_function": f"local_cache:{path.name}",
            "symbol": symbol,
            "response_code": None,
            "error_message": None,
            "retry_count": 0,
            "from_cache": True,
        }
    except Exception:
        return None


def _quote_from_broker(broker, symbol: str, *, retries: int = 2) -> dict[str, Any]:
    """Fetch one symbol quote; on failure try local cache, else concrete error fields."""
    price = None
    change_pct = None
    bid = None
    ask = None
    last_error: Optional[str] = None
    api_fn = "broker.get_current_price"
    response_code: Optional[str] = None
    attempts = 0

    for attempt in range(1, max(1, retries) + 1):
        attempts = attempt
        try:
            if hasattr(broker, "get_current_price"):
                api_fn = f"{type(broker).__name__}.get_current_price"
                raw = broker.get_current_price(symbol)
                if isinstance(raw, dict):
                    response_code = str(
                        raw.get("rt_cd") or raw.get("msg_cd") or raw.get("code") or ""
                    ) or None
                    price = float(raw.get("current_price") or raw.get("price") or 0)
                    change_pct = raw.get("change_pct")
                    bid = raw.get("bid") or raw.get("bid_price")
                    ask = raw.get("ask") or raw.get("ask_price")
                    if raw.get("error") or raw.get("message"):
                        last_error = str(raw.get("error") or raw.get("message"))
                elif raw is not None:
                    price = float(raw)
            if price is not None and price > 0:
                break
            if price is not None and price <= 0:
                last_error = f"non-positive price={price}"
                price = None
        except Exception as exc:
            last_error = str(exc)
            price = None

        if (price is None or price <= 0) and hasattr(broker, "kis"):
            try:
                api_fn = "broker.kis.get_current_price"
                raw = broker.kis.get_current_price(symbol)
                if isinstance(raw, dict):
                    response_code = str(
                        raw.get("rt_cd") or raw.get("msg_cd") or raw.get("code") or ""
                    ) or None
                    price = float(raw.get("current_price") or raw.get("price") or 0)
                    change_pct = raw.get("change_rate") or raw.get("change_pct")
                    if raw.get("msg1") or raw.get("message"):
                        last_error = str(raw.get("msg1") or raw.get("message"))
                if price is not None and price > 0:
                    break
                if price is not None and price <= 0:
                    last_error = f"non-positive price={price}"
                    price = None
            except Exception as exc:
                last_error = str(exc)
                price = None

        if attempt < retries:
            time.sleep(0.15)

    ok = price is not None and price > 0
    if not ok:
        cached = _quote_from_local_cache(symbol)
        if cached:
            return cached

    result = {
        "price": price if ok else None,
        "change_pct": change_pct,
        "bid": bid,
        "ask": ask,
        "updated_at": datetime.now().isoformat(),
        "ok": ok,
        "api_function": api_fn,
        "symbol": symbol,
        "response_code": response_code,
        "error_message": None if ok else (last_error or "quote unavailable"),
        "retry_count": attempts,
    }
    return result


def _refresh_quotes(broker, state: dict[str, Any]) -> dict[str, Any]:
    hynix = _quote_from_broker(broker, SIGNAL_SYMBOL)
    long_q = _quote_from_broker(broker, LONG_SYMBOL)
    inv_q = _quote_from_broker(broker, INVERSE_SYMBOL)
    quotes = {"hynix": hynix, "long": long_q, "inverse": inv_q}
    state["prices"] = {
        "hynix": hynix.get("price"),
        "long": long_q.get("price"),
        "inverse": inv_q.get("price"),
        "updated_at": datetime.now().isoformat(),
    }
    errors = []
    for key, q in quotes.items():
        if not q.get("ok"):
            errors.append({
                "api_function": q.get("api_function"),
                "symbol": q.get("symbol"),
                "response_code": q.get("response_code"),
                "error_message": q.get("error_message"),
                "retry_count": q.get("retry_count"),
                "slot": key,
            })
    state["quote_errors"] = errors
    # Only hard-block when hynix OR both ETFs are missing — a single ETF blip must not
    # freeze the morning (orders validate the symbols they actually need).
    critical = (not hynix.get("ok")) or (not long_q.get("ok") and not inv_q.get("ok"))
    if critical and errors:
        state["order_block_reason"] = (
            f"QUOTE_ERROR: {errors[0].get('symbol')} via {errors[0].get('api_function')}: "
            f"{errors[0].get('error_message')} (code={errors[0].get('response_code')}, "
            f"retries={errors[0].get('retry_count')})"
        )
    elif str(state.get("order_block_reason") or "").startswith("QUOTE_ERROR:"):
        state["order_block_reason"] = None
    return quotes



def _next_action_label(state: dict[str, Any]) -> str:
    if state.get("force_liquidate_pending"):
        return "청산 대기"
    op = state.get("opening_probe") or {}
    if op.get("awaiting_09_03_confirm"):
        return "09:03 B 확인 대기"
    if op.get("window_active") and not op.get("immediate_fired_today"):
        return "09:00 개시 프로브"
    direction = state.get("pending_signal_direction") or state.get("display_direction")
    pos = state.get("position") or {}
    held = pos.get("symbol")
    target = target_symbol_for_direction(direction)
    if state.get("order_block_reason"):
        return "주문 보류(ORDER_DATA_INVALID)"
    reentry = state.get("reentry") or {}
    if reentry.get("eligible") and not held:
        return "연속재진입 대기"
    if direction == DIR_UP and held != LONG_SYMBOL:
        return "KODEX 매수"
    if direction == DIR_DOWN and held != INVERSE_SYMBOL:
        return "SOL 매수"
    if held:
        return "기존 보유 유지"
    return "대기"


def _held_etf_price(quotes: dict[str, Any], symbol: Optional[str]) -> Optional[float]:
    if symbol == LONG_SYMBOL:
        px = (quotes.get("long") or {}).get("price")
    elif symbol == INVERSE_SYMBOL:
        px = (quotes.get("inverse") or {}).get("price")
    else:
        return None
    try:
        val = float(px)
        return val if val > 0 else None
    except Exception:
        return None


def run_once(
    *,
    broker=None,
    now: Optional[datetime] = None,
    df_1m: Optional[pd.DataFrame] = None,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Single worker tick. Injectable for tests/replay."""
    now = now or _now_kst()
    state = state if state is not None else om.load_state()
    mode = str(state.get("mode") or "mock")
    result: dict[str, Any] = {"ok": True, "now": now.isoformat(), "actions": []}
    reentry_enabled = bool(
        state.get("continuation_reentry_enabled")
        if state.get("continuation_reentry_enabled") is not None
        else CONTINUATION_REENTRY_ENABLED
    )
    state["continuation_reentry_enabled"] = reentry_enabled
    probe_enabled = bool(
        state.get("opening_probe_enabled")
        if state.get("opening_probe_enabled") is not None
        else OPENING_PROBE_ENABLED
    )
    state["opening_probe_enabled"] = probe_enabled

    if not state.get("auto_trade_on") and not state.get("force_liquidate_pending"):
        result["skipped"] = "auto_trade_off"
        return result

    # Build broker if needed
    own_broker = False
    if broker is None:
        try:
            # UI stores real_confirm_ok after phrase match; worker must pass the
            # configured confirm text into KisRealBroker gate 4 (empty string fails).
            real_ready = bool(state.get("real_confirm_ok"))
            confirm_text = ""
            if mode == "real" and real_ready:
                from app.config import get_config

                confirm_text = str(get_config().real_confirm_text() or "")
            broker = om.create_macd_broker(
                mode,
                real_confirm_text=confirm_text,
                real_ready=real_ready,
            )
            own_broker = True
            # Clear stale gate errors from a prior failed create / session.
            if state.get("order_block_reason"):
                state["order_block_reason"] = None
        except Exception as exc:
            state["order_block_reason"] = f"broker create failed: {exc}"
            om.refresh_runtime_status(state, worker_alive=True)
            om.save_state(state)
            result["ok"] = False
            result["error"] = str(exc)
            return result

    try:
        quotes = _refresh_quotes(broker, state)

        # Always compute MACD/direction for UI display (even outside session).
        # Orders remain gated by session / allow_new_switch below.
        session_date = now.strftime("%Y-%m-%d")
        load_diag: dict[str, Any] = {}
        if df_1m is None:
            df_1m, load_diag = load_macd_minute_history(mode, count=120, now=now)
        warm = _ensure_macd_warmup(state, df_1m, now, load_diag=load_diag)
        eval_res = evaluate_macd_direction(
            df_1m,
            now=now,
            last_signal_direction=state.get("last_signal_direction"),
            last_signal_bar_ts=state.get("last_signal_bar_ts"),
            session_date=session_date,
        )
        state["display_direction"] = eval_res.get("display_direction") or DIR_HOLD
        state["macd"] = {
            "macd": eval_res.get("macd"),
            "signal": eval_res.get("signal"),
            "hist": eval_res.get("hist"),
            "hist_last3": eval_res.get("hist_last3") or [],
            "hist_deltas": eval_res.get("hist_deltas") or [],
            "reason": eval_res.get("reason"),
            "bar_ts": eval_res.get("bar_ts"),
        }
        # If evaluate still warm-up-starved, surface rich diagnostics (not bare WARMUP_LT_26).
        if not eval_res.get("ok"):
            diag = state.get("macd_warmup_diagnostics") or load_diag or {}
            reason = eval_res.get("reason") or "SIGNAL_INACTIVE"
            state["macd"]["reason"] = reason
            state["macd"]["diagnostics"] = diag
            if warm.get("ok") and warm.get("macd") is not None:
                # Warm-up MACD numbers available for UI even if last bar gated oddly.
                state["macd"].update({
                    "macd": warm.get("macd"),
                    "signal": warm.get("signal"),
                    "hist": warm.get("hist"),
                    "hist_last3": warm.get("hist_last3") or [],
                    "hist_deltas": warm.get("hist_deltas") or [],
                    "reason": warm.get("reason") or reason,
                })
        result["macd"] = eval_res
        # Per-tick visibility: prove 3m bars + flags are computed every cycle
        state["last_macd_bars_ok"] = bool(eval_res.get("ok"))
        state["last_flag"] = eval_res.get("display_direction") or DIR_HOLD
        state["last_new_signal"] = bool(eval_res.get("new_signal"))
        state["last_signal_eval"] = {
            "bar_ts": eval_res.get("bar_ts"),
            "bar_close_ts": eval_res.get("bar_close_ts"),
            "hist_last3": eval_res.get("hist_last3") or [],
            "hist_deltas": eval_res.get("hist_deltas") or [],
            "flag": eval_res.get("display_direction"),
            "new_signal": bool(eval_res.get("new_signal")),
            "signal_id": eval_res.get("signal_id"),
            "reason": eval_res.get("reason"),
            "at": now.isoformat(),
        }
        state["worker_code_sha"] = om._git_sha()
        om.refresh_runtime_status(state, worker_alive=True)

        # Opening-probe path still refreshes pre-09:00 when probe enabled
        if probe_enabled and now.hour < 9:
            _refresh_opening_warmup(state, df_1m, now, mode)

        # 15:00 force liquidate — always highest priority
        if should_force_liquidate(now, state.get("force_liquidate_done_date")) or state.get("force_liquidate_pending"):
            state["force_liquidate_pending"] = True
            liq = om.force_liquidate_all(broker, mode=mode, quotes=quotes, state=state)
            result["actions"].append({"force_liquidate": liq})
            state["next_action"] = "청산 대기" if not liq.get("success") else "청산 완료"
            om.refresh_runtime_status(state, worker_alive=True)
            om.save_state(state)
            return result

        if not in_trading_session(now):
            result["skipped"] = "outside_session"
            state["next_action"] = _next_action_label(state)
            om.refresh_runtime_status(state, worker_alive=True)
            # Pre-open / after-close: orders are gated; surface MARKET_CLOSED clearly.
            if state.get("strategy_enabled") and not state.get("force_liquidate_pending"):
                state["primary_block_reason"] = "MARKET_CLOSED"
                state["order_execution_enabled"] = False
            om.save_state(state)
            return result

        op = state.setdefault("opening_probe", {})
        hynix_px = (quotes.get("hynix") or {}).get("price")
        try:
            hynix_px_f = float(hynix_px) if hynix_px is not None else None
        except Exception:
            hynix_px_f = None

        if probe_enabled:
            if now.hour == 9 and now.minute == 0 and hynix_px_f and hynix_px_f > 0:
                if op.get("day_open_price") is None:
                    op["day_open_price"] = hynix_px_f
            if not op.get("warmup_ready"):
                _refresh_opening_warmup(state, df_1m, now, mode)
            if in_open_probe_window(now):
                op["window_active"] = True
                _record_hynix_sample(state, now, hynix_px_f)
            elif open_probe_window_expired(now) and op.get("window_active") and not op.get("immediate_fired_today"):
                op["window_abandoned"] = True
                op["window_active"] = False

        pos = state.get("position") or {}
        held_symbol = pos.get("symbol")
        held_qty = int(pos.get("quantity") or 0)
        entry_px = float(pos.get("avg_price") or 0)

        # Opposite MACD B confirm — priority over SL / profit lock
        opposite_pending = False
        if eval_res.get("new_signal") and eval_res.get("signal_direction") and held_symbol and held_qty > 0:
            new_dir = eval_res["signal_direction"]
            new_target = target_symbol_for_direction(new_dir)
            if new_target and new_target != held_symbol:
                sid = eval_res["signal_id"]
                processed = set(state.get("processed_signal_ids") or [])
                # Do NOT re-stamp pending_signal_at when already armed — that prevented
                # next-tick execution (age never reached 0.5*TICK) so blue/red flags
                # showed in UI with no ETF order.
                if sid not in processed and sid != state.get("pending_signal_id"):
                    state["pending_signal_id"] = sid
                    state["pending_signal_direction"] = new_dir
                    state["pending_signal_at"] = now.isoformat()
                    state["pending_entry_kind"] = ENTRY_INITIAL
                    state["pending_signal_source"] = SIGNAL_SOURCE
                    state["last_signal_at"] = now.isoformat()
                    state["last_signal_bar_ts"] = eval_res.get("bar_ts")
                    state["last_signal_direction"] = new_dir
                    state["last_signal_id"] = sid
                    om.begin_order_latency(
                        state,
                        signal_id=sid,
                        completed_3m_bar_at=eval_res.get("bar_close_ts"),
                        signal_detected_at=now.isoformat(),
                    )
                    om.set_pipeline_stage(state, "Signal", True, sid)
                    result["actions"].append({"opposite_signal": sid, "direction": new_dir})
                    opposite_pending = True
                    if op.get("awaiting_09_03_confirm"):
                        op["awaiting_09_03_confirm"] = False
                        op["confirm_checked"] = True
                elif sid == state.get("pending_signal_id"):
                    # Already armed — keep pending age so execute_now can fire this tick.
                    opposite_pending = True

        # SL / profit-lock every tick while in position (skip when opposite armed this tick)
        # Priority after 15:00 + opposite: SL then PROFIT_LOCK (no fixed +3% TP)
        if not opposite_pending and held_symbol and held_qty > 0 and entry_px > 0:
            cur_px = _held_etf_price(quotes, held_symbol)
            if cur_px is not None:
                pl_prev = state.get("profit_lock") or {}
                exit_eval = evaluate_position_exits(
                    held_symbol,
                    entry_px,
                    cur_px,
                    held_qty,
                    peak_net_return=float(pl_prev.get("peak_net_return") or 0.0),
                    profit_lock_active=bool(pl_prev.get("profit_lock_active")),
                )
                state["profit_lock"] = {
                    "peak_net_return": exit_eval["peak_net_return"],
                    "current_net_return": exit_eval["current_net_return"],
                    "giveback_pct": exit_eval["giveback_pct"],
                    "profit_lock_active": exit_eval["profit_lock_active"],
                }
                result["profit_lock"] = state["profit_lock"]
                exit_hit = exit_eval.get("exit_reason")
                if exit_hit:
                    exit_res = om.exit_position_full(
                        broker,
                        mode=mode,
                        quotes=quotes,
                        state=state,
                        reason=exit_hit,
                        signal_id=f"{exit_hit}:{pos.get('signal_id') or now.isoformat()}",
                    )
                    result["actions"].append({"exit": exit_res, "reason": exit_hit})
                    state["next_action"] = _next_action_label(state)
                    om.refresh_runtime_status(state, worker_alive=True)
                    om.save_state(state)
                    # After SL / profit-lock, continue tick for signals (no return)
                    if not exit_res.get("success"):
                        return result

        # Keep last_signal_direction after SL/profit-lock/flat — same-dir re-entry forbidden;
        # a new episode arms only on opposite confirmed B signal.
        ep = state.get("direction_episode") or {}

        pos = state.get("position") or {}
        flat = not pos.get("symbol") or int(pos.get("quantity") or 0) <= 0

        # Continuation re-entry diagnostics (every tick when flat after TP)
        cont = evaluate_continuation_reentry(
            df_1m,
            direction=str(ep.get("direction") or eval_res.get("display_direction") or ""),
            episode=ep,
            now=now,
            enabled=reentry_enabled,
        )
        state["reentry"] = {
            "eligible": bool(cont.get("eligible")),
            "block_reason": cont.get("block_reason"),
            "bars_since_tp": cont.get("bars_since_tp") or 0,
            "hist_contracted": bool(cont.get("hist_contracted")),
            "hist_last3": cont.get("hist_last3") or [],
            "enabled": reentry_enabled,
            "episode_reentry_used": bool(ep.get("continuation_reentry_used")),
        }
        result["reentry"] = state["reentry"]

        # ── Opening probe: 09:00 immediate 50% ─────────────────────────────
        if (
            probe_enabled
            and in_open_probe_window(now)
            and not op.get("immediate_fired_today")
            and flat
            and not state.get("pending_signal_id")
            and (quotes.get("hynix") or {}).get("ok")
        ):
            warm = state.get("opening_warmup_macd") or {}
            day_open = op.get("day_open_price")
            if day_open and hynix_px_f and warm.get("ok"):
                samples = [(s[0], s[1]) for s in (op.get("price_samples_5s") or [])]
                probe = evaluate_opening_probe(
                    warm,
                    hynix_price=hynix_px_f,
                    day_open_price=float(day_open),
                    long_quote=quotes.get("long"),
                    inverse_quote=quotes.get("inverse"),
                    price_samples_5s=samples,
                    now=now,
                )
                op["last_eval_at"] = now.isoformat()
                op["last_eval_reason"] = probe.get("reason")
                op["last_eval_signal"] = probe.get("signal")
                result["opening_probe"] = probe
                if probe.get("ok_to_trade") and probe.get("direction"):
                    sid = f"OPEN_IMM:{probe['signal']}:{now.strftime('%Y%m%d%H%M%S')}"
                    state["pending_signal_id"] = sid
                    state["pending_signal_direction"] = probe["direction"]
                    state["pending_signal_at"] = now.isoformat()
                    state["pending_entry_kind"] = ENTRY_OPEN_IMMEDIATE
                    state["pending_signal_source"] = SIGNAL_SOURCE_OPEN_IMMEDIATE
                    state["pending_budget_fraction"] = OPEN_IMMEDIATE_BUDGET_FRACTION
                    om.begin_order_latency(
                        state,
                        signal_id=sid,
                        completed_3m_bar_at=None,
                        signal_detected_at=now.isoformat(),
                    )
                    om.set_pipeline_stage(state, "Signal", True, sid)
                    result["actions"].append({"opening_probe": sid, "direction": probe["direction"]})

        # ── 09:03 first completed 3m bar: confirm scale or flatten ───────────
        if (
            probe_enabled
            and op.get("awaiting_09_03_confirm")
            and not op.get("confirm_checked")
            and first_regular_3m_bar_closed(now)
            and held_symbol
            and held_qty > 0
            and pos.get("opening_probe")
            and not state.get("pending_signal_id")
        ):
            probe_dir = op.get("immediate_direction") or state.get("direction_episode", {}).get("direction")
            if probe_dir and opening_probe_b_confirms(eval_res, probe_dir):
                sid = f"OPEN_SCALE:{probe_dir}:{now.strftime('%Y%m%d%H%M%S')}"
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = probe_dir
                state["pending_signal_at"] = now.isoformat()
                state["pending_entry_kind"] = ENTRY_OPEN_SCALE
                state["pending_signal_source"] = SIGNAL_SOURCE_OPEN_IMMEDIATE
                state["pending_budget_fraction"] = OPEN_IMMEDIATE_BUDGET_FRACTION
                state["pending_open_scale"] = True
                om.begin_order_latency(
                    state,
                    signal_id=sid,
                    completed_3m_bar_at=eval_res.get("bar_close_ts"),
                    signal_detected_at=now.isoformat(),
                )
                result["actions"].append({"opening_scale": sid, "direction": probe_dir})
            else:
                flat_res = om.flatten_opening_probe_unconfirmed(
                    broker, mode=mode, quotes=quotes, state=state,
                )
                result["actions"].append({"opening_unconfirmed_exit": flat_res})
                op["confirm_checked"] = True
                pos = state.get("position") or {}
                flat = not pos.get("symbol") or int(pos.get("quantity") or 0) <= 0

        # Arm new MACD first-turn signal (Strategy B unchanged)
        if (
            eval_res.get("new_signal")
            and eval_res.get("signal_id")
            and not opposite_pending
            and not (probe_enabled and op.get("awaiting_09_03_confirm") and not op.get("confirm_checked"))
        ):
            sid = eval_res["signal_id"]
            processed = set(state.get("processed_signal_ids") or [])
            if sid not in processed and sid != state.get("pending_signal_id"):
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = eval_res["signal_direction"]
                state["pending_signal_at"] = now.isoformat()
                state["pending_entry_kind"] = ENTRY_INITIAL
                state["pending_signal_source"] = SIGNAL_SOURCE
                state["last_signal_at"] = now.isoformat()
                state["last_signal_bar_ts"] = eval_res.get("bar_ts")
                # Arm direction immediately so the same UP/DOWN streak cannot re-fire.
                state["last_signal_direction"] = eval_res["signal_direction"]
                state["last_signal_id"] = sid
                om.begin_order_latency(
                    state,
                    signal_id=sid,
                    completed_3m_bar_at=eval_res.get("bar_close_ts"),
                    signal_detected_at=now.isoformat(),
                )
                om.set_pipeline_stage(state, "Signal", True, sid)
                result["actions"].append({"signal": sid, "direction": eval_res["signal_direction"]})

        # Arm continuation re-entry once (no new MACD color-flip required)
        elif (
            flat
            and reentry_enabled
            and cont.get("eligible")
            and cont.get("signal_id")
            and not state.get("pending_signal_id")
        ):
            sid = cont["signal_id"]
            processed = set(state.get("processed_signal_ids") or [])
            if sid not in processed:
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = ep.get("direction")
                state["pending_signal_at"] = now.isoformat()
                state["pending_entry_kind"] = ENTRY_CONTINUATION
                state["pending_signal_source"] = SIGNAL_SOURCE_CONTINUATION
                om.begin_order_latency(
                    state,
                    signal_id=sid,
                    completed_3m_bar_at=eval_res.get("bar_close_ts"),
                    signal_detected_at=now.isoformat(),
                )
                om.set_pipeline_stage(state, "Signal", True, sid)
                result["actions"].append({
                    "continuation_reentry": sid,
                    "direction": ep.get("direction"),
                })

        # Execute pending switch ASAP on the arming tick (no age gate).
        # Pending is idempotent via processed_signal_ids; delaying only caused missed buys.
        pending_id = state.get("pending_signal_id")
        pending_dir = state.get("pending_signal_direction")
        pending_at = state.get("pending_signal_at")
        pending_kind = state.get("pending_entry_kind") or ENTRY_INITIAL
        pending_src = state.get("pending_signal_source") or SIGNAL_SOURCE
        pending_frac = float(state.get("pending_budget_fraction") or 1.0)
        pending_scale = bool(state.get("pending_open_scale"))
        execute_now = bool(pending_id and pending_dir)

        if execute_now and pending_id and pending_dir:
            if not allow_new_switch(now):
                state["order_block_reason"] = "NO_NEW_SWITCH_AFTER_14:55"
                result["actions"].append({"blocked": "after_14:55"})
            else:
                # Opposite MACD while holding → sell reason OPPOSITE_SWITCH
                cur_pos = state.get("position") or {}
                cur_held = cur_pos.get("symbol")
                target = target_symbol_for_direction(pending_dir)
                sell_reason = None
                if cur_held and target and cur_held != target and pending_kind in (ENTRY_INITIAL, ENTRY_OPEN_IMMEDIATE):
                    sell_reason = EXIT_OPPOSITE
                if pending_scale and pending_kind == ENTRY_OPEN_SCALE:
                    switch_res = om.scale_opening_probe(
                        broker,
                        pending_dir,
                        mode=mode,
                        budget=float(state.get("budget") or 10_000_000),
                        quotes=quotes,
                        signal_id=pending_id,
                        state=state,
                    )
                else:
                    switch_res = om.switch_to_direction(
                        broker,
                        pending_dir,
                        mode=mode,
                        budget=float(state.get("budget") or 10_000_000),
                        quotes=quotes,
                        signal_id=pending_id,
                        state=state,
                        entry_kind=pending_kind,
                        signal_source=pending_src,
                        sell_reason=sell_reason,
                        budget_fraction=pending_frac,
                    )
                result["actions"].append({"switch": switch_res})
                state["last_order_attempt_at"] = now.isoformat()
                if not switch_res.get("success") and not switch_res.get("duplicate") and not switch_res.get("skipped_same_direction"):
                    state["last_order_error"] = str(switch_res.get("message") or "switch_failed")
                else:
                    state["last_order_error"] = None
                if switch_res.get("success") or switch_res.get("duplicate") or switch_res.get("skipped_same_direction"):
                    if pending_kind == ENTRY_OPEN_IMMEDIATE:
                        op["immediate_fired_today"] = True
                        op["immediate_direction"] = pending_dir
                        op["immediate_signal_id"] = pending_id
                        op["immediate_at"] = now.isoformat()
                        op["awaiting_09_03_confirm"] = True
                        op["window_active"] = False
                        state["last_signal_direction"] = pending_dir
                        state["last_signal_id"] = pending_id
                    state["pending_signal_id"] = None
                    state["pending_signal_direction"] = None
                    state["pending_entry_kind"] = None
                    state["pending_signal_source"] = None
                    state["pending_budget_fraction"] = None
                    state["pending_open_scale"] = None
                    if pending_kind not in (ENTRY_OPEN_IMMEDIATE,):
                        state["last_signal_direction"] = pending_dir
                        state["last_signal_id"] = pending_id
                elif switch_res.get("order_data_invalid"):
                    state["order_block_reason"] = switch_res.get("message")

        state["next_action"] = _next_action_label(state)
        om.refresh_runtime_status(state, worker_alive=True)
        om.save_state(state)
        return result
    finally:
        if own_broker:
            pass


def _worker_loop() -> None:
    logger.info("[MACDHynix] worker started (fixed %ss schedule)", TICK_SECONDS)
    with _status_lock:
        _status["alive"] = True
        _status["started_at"] = datetime.now().isoformat()
        _status["last_error"] = None

    # Align to wall-clock 5s grid
    next_tick = time.monotonic()
    last_mono = None
    while not _stop_event.is_set():
        now_mono = time.monotonic()
        if now_mono < next_tick:
            _wake_event.wait(timeout=min(0.2, next_tick - now_mono))
            _wake_event.clear()
            continue

        tick_started = time.monotonic()
        if last_mono is not None:
            interval = tick_started - last_mono
            with _status_lock:
                intervals = list(_status.get("tick_intervals") or [])
                intervals.append(interval)
                _status["tick_intervals"] = intervals[-40:]
        last_mono = tick_started

        try:
            state = om.load_state()
            state.setdefault("worker", {})
            state["worker"]["alive"] = True
            state["worker"]["last_tick_at"] = datetime.now().isoformat()
            today = _now_kst().strftime("%Y-%m-%d")
            if state.get("session_date") != today:
                # Day change: clear mutex ownership if strategy is off
                if state.get("session_date") and not state.get("auto_trade_on"):
                    om.clear_mutex(mode=str(state.get("mode") or "mock"), reason="day_change")
                # Reset direction_state so first signed-B onset after 09:00 can enter
                # (start_auto_trade must use the same helper — do not set session_date alone).
                om.apply_macd_session_day_rollover(state, session_date=today)
            with _status_lock:
                intervals = list(_status.get("tick_intervals") or [])
                state["worker"]["tick_intervals"] = [round(x, 3) for x in intervals[-40:]]
                state["worker"]["avg_interval"] = _avg(intervals[-20:])
                state["worker"]["p95_interval"] = _p95(intervals[-20:])
                # Confirm: MACD uses fixed 5s worker only — never waits on 3m main cycle.
                state["worker"]["main_cycle_3m_wait_count"] = 0
                _status["alive"] = True
                _status["last_tick_at"] = state["worker"]["last_tick_at"]
                _status["main_cycle_3m_wait_count"] = 0
            if state.get("auto_trade_on") or state.get("force_liquidate_pending"):
                run_once(state=state)
            else:
                om.refresh_runtime_status(state, worker_alive=True)
                om.save_state(state)
        except Exception as exc:
            logger.exception("[MACDHynix] tick error: %s", exc)
            with _status_lock:
                _status["last_error"] = str(exc)

        # Fixed schedule: advance by 5s slots (catch up at most one skipped slot)
        next_tick += TICK_SECONDS
        behind = time.monotonic() - next_tick
        if behind > TICK_SECONDS:
            skipped = int(behind // TICK_SECONDS)
            next_tick += skipped * TICK_SECONDS

    with _status_lock:
        _status["alive"] = False
    try:
        state = om.load_state()
        state.setdefault("worker", {})["alive"] = False
        om.save_state(state)
    except Exception:
        pass
    logger.info("[MACDHynix] worker stopped")


def ensure_worker_running(*, force_restart: bool = False) -> dict[str, Any]:
    """Start daemon worker; when ``force_restart`` kill old thread so new bytecode loads."""
    global _worker_thread
    if force_restart:
        stop_worker()
        if _worker_thread and _worker_thread.is_alive():
            _worker_thread.join(timeout=3.0)
        _worker_thread = None
        _stop_event.clear()
    elif _worker_thread and _worker_thread.is_alive():
        return get_worker_status()
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="macd-hynix-worker", daemon=True)
    _worker_thread.start()
    return get_worker_status()


def stop_worker() -> None:
    _stop_event.set()
    _wake_event.set()


def repair_phantom_initial_entry(state: dict[str, Any], broker) -> dict[str, Any]:
    """Clear phantom INITIAL_ENTRY lock when broker+local are flat (test/pollution / failed fill).

    Keeps real same-dir episode lock after a genuine exit. Only unlocks when last_event
    claims INITIAL_ENTRY but neither broker nor local book holds the target ETF.
    """
    out = {"repaired": False}
    pos = state.get("position") or {}
    local_qty = int(pos.get("quantity") or 0)
    if local_qty > 0 and pos.get("symbol"):
        return out
    if str(state.get("last_event") or "") != "INITIAL_ENTRY":
        return out
    sid = state.get("last_signal_id")
    direction = state.get("last_signal_direction")
    target = target_symbol_for_direction(direction)
    if not target or not sid:
        return out
    live = om.get_held_quantity(broker, target)
    if live is None:
        return out
    if int(live) > 0:
        return out
    other = opposite_symbol(target)
    other_qty = om.get_held_quantity(broker, other) if other else 0
    if other and other_qty is not None and int(other_qty) > 0:
        return out

    processed = [x for x in (state.get("processed_signal_ids") or []) if x != sid]
    state["processed_signal_ids"] = processed
    state["last_signal_direction"] = None
    state["last_signal_bar_ts"] = None
    state["last_signal_id"] = None
    state["last_event"] = None
    state["direction_episode"] = om.default_state()["direction_episode"]
    state["pipeline"] = om.default_state()["pipeline"]
    state["position"] = om.default_state()["position"]
    out.update({"repaired": True, "cleared_signal_id": sid, "cleared_direction": direction})
    logger.warning(
        "[MACDHynix] repaired phantom INITIAL_ENTRY lock sid=%s dir=%s (broker flat)",
        sid,
        direction,
    )
    return out


def start_auto_trade(
    *,
    mode: str = "mock",
    budget: float = 10_000_000,
    real_confirm_ok: bool = False,
    masked_account: str = "",
) -> dict[str, Any]:
    mode = "real" if mode == "real" else "mock"
    if mode == "real" and not real_confirm_ok:
        return {"ok": False, "message": "REAL requires confirm phrase"}

    # Force disk re-read of Enhanced auto_trade_on (no stale OR-scan of profile files).
    ok, reason = om.can_start_macd(mode)
    if not ok:
        state = om.load_state()
        state["primary_block_reason"] = reason
        state["legacy_truth_debug"] = om.legacy_auto_trade_truth(force_disk=True)
        om.refresh_runtime_status(state)
        om.save_state(state)
        return {"ok": False, "message": reason, "primary_block_reason": reason}

    # Kill old daemon thread so Start always loads current worker/strategy bytecode.
    ensure_worker_running(force_restart=True)

    state = om.load_state()
    today = _now_kst().strftime("%Y-%m-%d")
    # Must rollover BEFORE first run_once. Setting session_date alone used to skip the
    # worker day-change path and leave overnight last_signal_direction=UP/DOWN, so a
    # morning signed-B onset never armed despite UI showing RED/BLUE.
    om.apply_macd_session_day_rollover(state, session_date=today)
    if not state.get("session_date"):
        state["session_date"] = today
    state["auto_trade_on"] = True
    state["mode"] = mode
    state["budget"] = float(budget)
    state["stopped"] = False
    state["stopped_reason"] = None
    state["real_confirm_ok"] = bool(real_confirm_ok) if mode == "real" else False
    state["masked_account"] = masked_account
    state["primary_block_reason"] = None
    state["order_block_reason"] = None
    state["last_order_error"] = None
    state["worker_code_sha"] = om._git_sha()
    state["legacy_truth_debug"] = om.legacy_auto_trade_truth(force_disk=True)
    om.write_mutex(macd_on=True, mode=mode, reason="macd_started")
    om.refresh_runtime_status(state, worker_alive=True)
    om.save_state(state)

    # First tick immediately after start: fetch quotes + compute MACD (do not wait 5s).
    try:
        # Build broker early so phantom repair can see live holdings.
        real_ready = bool(state.get("real_confirm_ok"))
        confirm_text = ""
        if mode == "real" and real_ready:
            from app.config import get_config

            confirm_text = str(get_config().real_confirm_text() or "")
        broker = om.create_macd_broker(
            mode,
            real_confirm_text=confirm_text,
            real_ready=real_ready,
        )
        repair_phantom_initial_entry(state, broker)
        om.save_state(state)
        run_once(broker=broker, state=state)
    except Exception as exc:
        logger.warning("[MACDHynix] immediate first tick failed: %s", exc)
        state = om.load_state()
        state["order_block_reason"] = state.get("order_block_reason") or f"first_tick_error: {exc}"
        state["last_order_error"] = str(exc)
        om.refresh_runtime_status(state, worker_alive=True)
        om.save_state(state)
    _wake_event.set()
    return {"ok": True, "state": om.load_state()}


def stop_auto_trade(reason: str = "user_stop") -> dict[str, Any]:
    state = om.load_state()
    state["auto_trade_on"] = False
    state["stopped"] = True
    state["stopped_reason"] = reason
    state["pending_signal_id"] = None
    state["pending_signal_direction"] = None
    om.clear_mutex(mode=str(state.get("mode") or "mock"), reason=reason)
    om.refresh_runtime_status(state, worker_alive=True)
    om.save_state(state)
    stop_worker()
    return {"ok": True, "state": state}


def request_force_liquidate() -> dict[str, Any]:
    state = om.load_state()
    state["force_liquidate_pending"] = True
    om.save_state(state)
    ensure_worker_running()
    _wake_event.set()
    return {"ok": True}
