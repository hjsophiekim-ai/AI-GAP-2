"""
us_market_data.py

미국장 휴장/조기폐장 판단 + MU(마이크론) 등 미국 종목의 실시간/분봉/
마지막거래일 데이터를 다중 소스 우선순위(Alpaca -> Polygon -> Finnhub ->
yfinance -> 네이버 해외증시)로 안정적으로 수집한다.

원칙:
  - API 키가 없는 소스는 조용히(debug 로그만) skip하고 다음 fallback으로 이동.
  - 어떤 이유로든 예외를 던지지 않는다 (모든 실패는 dict의 success=False로 표현).
  - 미국장 휴장으로 실시간 분봉이 없는 것은 오류가 아니라 정상 상태다.

이 파일은 market_data_collector.py 에서만 호출되는 보조 모듈이며, 독립
실행 앱이 아니다.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

try:
    from app.utils.us_market_calendar import get_session_kst
except Exception:
    def get_session_kst(dt=None):  # pragma: no cover - fallback only
        return "closed"

FRESH_SECONDS = 180
USABLE_SECONDS = 900


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 미국 공휴일/조기폐장 캘린더 (pandas_market_calendars 우선, 없으면 자체 계산)
# ---------------------------------------------------------------------------

def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm — 부활절(Easter Sunday) 날짜."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """해당 월의 n번째 요일(weekday: 월=0) 날짜."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """해당 월의 마지막 요일(weekday: 월=0) 날짜."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _observed(d: date) -> date:
    """토요일이면 전 금요일, 일요일이면 다음 월요일로 대체(observed)."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _us_market_holidays(year: int) -> set:
    holidays = {
        _observed(date(year, 1, 1)),          # New Year's Day
        _nth_weekday(year, 1, 0, 3),           # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),           # Washington's Birthday (3rd Mon Feb)
        _easter(year) - timedelta(days=2),     # Good Friday
        _last_weekday(year, 5, 0),             # Memorial Day (last Mon May)
        _observed(date(year, 7, 4)),           # Independence Day
        _nth_weekday(year, 9, 0, 1),           # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),          # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),         # Christmas
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    return holidays


def _us_early_close_dates(year: int) -> set:
    """반나절장(대략치) — 독립기념일 전날, 추수감사절 다음날, 크리스마스이브."""
    dates = set()
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    dates.add(thanksgiving + timedelta(days=1))
    for d in (date(year, 7, 3), date(year, 12, 24)):
        if d.weekday() < 5:
            dates.add(d)
    return dates


def _fallback_holiday_check(d: date) -> tuple:
    """자체 계산 캘린더. Returns (is_holiday, is_early_close)."""
    is_holiday = d.weekday() >= 5 or d in _us_market_holidays(d.year)
    is_early_close = (not is_holiday) and d in _us_early_close_dates(d.year)
    return is_holiday, is_early_close


def _pandas_market_calendars_check(d: date) -> Optional[tuple]:
    """가능하면 pandas_market_calendars(NYSE)로 더 정확하게 판정. 미설치/오류 시 None."""
    try:
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar("NYSE")
        schedule = cal.schedule(start_date=d - timedelta(days=5), end_date=d + timedelta(days=1))
        ts = None
        for idx in schedule.index:
            if idx.date() == d:
                ts = idx
                break
        if ts is None:
            return True, False
        row = schedule.loc[ts]
        close_hour = row["market_close"].tz_convert("America/New_York").hour
        is_early = close_hour < 15
        return False, is_early
    except Exception as exc:
        logger.debug("[USMarketData] pandas_market_calendars 미사용/오류: %s", exc)
        return None


def _last_us_trading_day_detailed(now: Optional[datetime] = None) -> tuple:
    """
    KST 기준 가장 최근 완료된 미국 거래일(근사치, DST 미보정)을 계산한다.

    이 시스템은 한국 장 시작 전(08:50~09:25 KST)에 항상 실행되며, 이 시각에는
    미국 시장이 '오늘' 열려있을 수 없다(세션상 aftermarket/closed) — 즉
    직전 KST 캘린더일(D-1)이 실제 미국 거래일의 저녁(미국 동부시간 기준
    당일 정규장)에 해당한다. D-1부터 역순으로 주말/공휴일을 건너뛴다.

    Returns
    -------
    (last_trading_day: date, holiday_skipped: bool) — holiday_skipped는
    주말이 아닌 '진짜 공휴일'을 하나라도 건너뛰었는지 여부.
    """
    now = now or _now()
    d = now.date() - timedelta(days=1)
    holiday_skipped = False
    while True:
        is_holiday, _ = _fallback_holiday_check(d)
        if d.weekday() < 5 and not is_holiday:
            return d, holiday_skipped
        if is_holiday and d.weekday() < 5:
            holiday_skipped = True
        d -= timedelta(days=1)


def get_last_us_trading_day(now: Optional[datetime] = None) -> date:
    """현재 KST 기준 가장 최근 완료된 미국 거래일(근사치, DST 미보정)."""
    return _last_us_trading_day_detailed(now)[0]


def get_us_market_status(now: Optional[datetime] = None) -> dict:
    """
    미국장 개장/휴장/조기폐장 상태.

    is_us_holiday는 "오늘(앞으로 열릴 세션)이 공휴일"이거나 "가장 최근
    완료된 거래일을 찾기 위해 실제 공휴일을 건너뛰었음(전일 휴장 등)"인
    경우 True가 된다 — 두 경우 모두 하류(regime_features)에서
    holiday_mode 로 취급해야 하기 때문이다.

    Returns
    -------
    dict: is_us_market_open, is_us_holiday, is_us_weekend, is_us_early_close,
          last_us_trading_day(YYYY-MM-DD), session, source, timestamp, confidence
    """
    now = now or _now()
    session = get_session_kst(now)
    today = now.date()

    mcal_result = _pandas_market_calendars_check(today)
    if mcal_result is not None:
        today_is_holiday, today_is_early_close = mcal_result
        source = "pandas_market_calendars"
        confidence = 0.95
    else:
        today_is_holiday, today_is_early_close = _fallback_holiday_check(today)
        source = "internal_calendar_fallback"
        confidence = 0.75

    is_weekend = today.weekday() >= 5
    is_market_open = session == "regular" and not today_is_holiday and not is_weekend

    last_trading_day, holiday_skipped = _last_us_trading_day_detailed(now)

    return {
        "is_us_market_open": is_market_open,
        "is_us_holiday": bool(today_is_holiday and not is_weekend) or holiday_skipped,
        "is_us_weekend": bool(is_weekend),
        "is_us_early_close": bool(today_is_early_close),
        "last_us_trading_day": last_trading_day.isoformat(),
        "session": session,
        "source": source,
        "timestamp": _now_iso(),
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 다중 소스 실시간 시세 (Alpaca -> Polygon -> Finnhub -> yfinance -> Naver)
# ---------------------------------------------------------------------------

def _env_true(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


def _fetch_alpaca_quote(symbol: str) -> Optional[dict]:
    if not _env_true("ENABLE_ALPACA_US_DATA", "true"):
        return None
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    feed = os.getenv("ALPACA_DATA_FEED", "iex")
    try:
        import requests
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars/latest?feed={feed}"
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code != 200:
            return None
        bar = resp.json().get("bar")
        if not bar:
            return None
        return {
            "price": float(bar["c"]), "open": float(bar["o"]), "high": float(bar["h"]),
            "low": float(bar["l"]), "volume": int(bar.get("v", 0)),
            "timestamp": bar.get("t"), "source": "alpaca",
        }
    except Exception as exc:
        logger.debug("[USMarketData] Alpaca quote 실패 %s: %s", symbol, exc)
        return None


def _fetch_alpaca_bars(symbol: str, limit: int = 5) -> list:
    if not _env_true("ENABLE_ALPACA_US_DATA", "true"):
        return []
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return []
    feed = os.getenv("ALPACA_DATA_FEED", "iex")
    try:
        import requests
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        url = (
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
            f"?timeframe=1Min&limit={limit}&feed={feed}"
        )
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code != 200:
            return []
        bars = resp.json().get("bars", [])
        return [
            {"open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
             "volume": b.get("v", 0), "time": b.get("t")}
            for b in bars
        ]
    except Exception as exc:
        logger.debug("[USMarketData] Alpaca bars 실패 %s: %s", symbol, exc)
        return []


def _fetch_polygon_quote(symbol: str) -> Optional[dict]:
    if not _env_true("ENABLE_POLYGON_US_DATA", "false"):
        return None
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        return None
    try:
        import requests
        url = f"https://api.polygon.io/v2/last/trade/{symbol}?apiKey={api_key}"
        resp = requests.get(url, timeout=6)
        if resp.status_code != 200:
            return None
        result = resp.json().get("results") or resp.json().get("last")
        if not result:
            return None
        price = result.get("p") or result.get("price")
        ts = result.get("t") or result.get("timestamp")
        if not price:
            return None
        return {"price": float(price), "timestamp": ts, "source": "polygon"}
    except Exception as exc:
        logger.debug("[USMarketData] Polygon quote 실패 %s: %s", symbol, exc)
        return None


def _fetch_polygon_bars(symbol: str, limit: int = 5) -> list:
    if not _env_true("ENABLE_POLYGON_US_DATA", "false"):
        return []
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        return []
    try:
        import requests
        today = date.today().isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/"
            f"{today}/{today}?adjusted=true&sort=desc&limit={limit}&apiKey={api_key}"
        )
        resp = requests.get(url, timeout=6)
        if resp.status_code != 200:
            return []
        results = resp.json().get("results", [])
        return [
            {"open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"],
             "volume": r.get("v", 0), "time": r.get("t")}
            for r in results
        ]
    except Exception as exc:
        logger.debug("[USMarketData] Polygon bars 실패 %s: %s", symbol, exc)
        return []


def _fetch_finnhub_quote(symbol: str) -> Optional[dict]:
    if not _env_true("ENABLE_FINNHUB_US_DATA", "false"):
        return None
    api_key = os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        return None
    try:
        import requests
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}"
        resp = requests.get(url, timeout=6)
        if resp.status_code != 200:
            return None
        data = resp.json()
        price = data.get("c")
        prev_close = data.get("pc")
        if not price:
            return None
        return {
            "price": float(price), "prev_close": float(prev_close) if prev_close else None,
            "timestamp": data.get("t"), "source": "finnhub",
        }
    except Exception as exc:
        logger.debug("[USMarketData] Finnhub quote 실패 %s: %s", symbol, exc)
        return None


def _fetch_yfinance_quote(symbol: str) -> Optional[dict]:
    if not _env_true("ENABLE_YFINANCE_FALLBACK", "true"):
        return None
    try:
        from app.data.naver_global_stock_collector import _fetch_from_yfinance
        result = _fetch_from_yfinance(symbol)
        if result and result.get("price"):
            return {
                "price": result["price"], "change_pct": result.get("return_pct"), "source": "yahoo",
            }
    except Exception as exc:
        logger.debug("[USMarketData] yfinance quote 실패 %s: %s", symbol, exc)
    return None


def _fetch_yfinance_bars(symbol: str, limit: int = 5) -> list:
    if not _env_true("ENABLE_YFINANCE_FALLBACK", "true"):
        return []
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d", interval="1m", auto_adjust=True)
        if hist is None or hist.empty:
            return []
        tail = hist.tail(limit)
        return [
            {
                "open": float(row["Open"]), "high": float(row["High"]), "low": float(row["Low"]),
                "close": float(row["Close"]), "volume": int(row.get("Volume", 0)),
                "time": idx.isoformat(),
            }
            for idx, row in tail.iterrows()
        ]
    except Exception as exc:
        logger.debug("[USMarketData] yfinance bars 실패 %s: %s", symbol, exc)
        return []


def _fetch_naver_last_resort(symbol: str) -> Optional[dict]:
    if not _env_true("ENABLE_NAVER_US_FALLBACK", "true"):
        return None
    try:
        from app.data.naver_global_stock_collector import _fetch_from_naver_world
        result = _fetch_from_naver_world(symbol)
        if result and result.get("price"):
            return {"price": result["price"], "change_pct": result.get("return_pct"), "source": "naver"}
    except Exception as exc:
        logger.debug("[USMarketData] Naver 최후수단 실패 %s: %s", symbol, exc)
    return None


def fetch_us_quote_multi(symbol: str) -> dict:
    """Alpaca -> Polygon -> Finnhub -> yfinance -> Naver 우선순위 시세 조회.

    Returns
    -------
    dict: price, change_pct, source, success, error, timestamp
    """
    now_iso = _now_iso()

    alpaca = _fetch_alpaca_quote(symbol)
    if alpaca and alpaca.get("price"):
        change_pct = None
        return {"price": alpaca["price"], "change_pct": change_pct, "source": "alpaca",
                "success": True, "error": None, "timestamp": now_iso}

    polygon = _fetch_polygon_quote(symbol)
    if polygon and polygon.get("price"):
        return {"price": polygon["price"], "change_pct": None, "source": "polygon",
                "success": True, "error": None, "timestamp": now_iso}

    finnhub = _fetch_finnhub_quote(symbol)
    if finnhub and finnhub.get("price"):
        change_pct = None
        if finnhub.get("prev_close"):
            change_pct = (finnhub["price"] - finnhub["prev_close"]) / finnhub["prev_close"] * 100
        return {"price": finnhub["price"], "change_pct": change_pct, "source": "finnhub",
                "success": True, "error": None, "timestamp": now_iso}

    yf_quote = _fetch_yfinance_quote(symbol)
    if yf_quote and yf_quote.get("price"):
        return {"price": yf_quote["price"], "change_pct": yf_quote.get("change_pct"), "source": "yahoo",
                "success": True, "error": None, "timestamp": now_iso}

    naver_quote = _fetch_naver_last_resort(symbol)
    if naver_quote and naver_quote.get("price"):
        return {"price": naver_quote["price"], "change_pct": naver_quote.get("change_pct"), "source": "naver",
                "success": True, "error": None, "timestamp": now_iso}

    return {"price": None, "change_pct": None, "source": "none", "success": False,
            "error": "all_sources_failed", "timestamp": now_iso}


def fetch_us_minute_bars(symbol: str, limit: int = 5) -> tuple:
    """1분봉 리스트(최신순 아님, 시간순) + source 문자열. 실패 시 ([], 'none').

    우선순위: Alpaca -> Polygon -> yfinance(백업/전일 확인용).
    Finnhub 무료 플랜은 분봉을 지원하지 않아 quote 전용으로만 사용한다
    (fetch_us_quote_multi 참고).
    """
    bars = _fetch_alpaca_bars(symbol, limit=limit)
    if bars:
        return bars, "alpaca"
    bars = _fetch_polygon_bars(symbol, limit=limit)
    if bars:
        return bars, "polygon"
    bars = _fetch_yfinance_bars(symbol, limit=limit)
    if bars:
        return bars, "yahoo"
    return [], "none"


def _parse_bar_time(value, source: str):
    """공급자별 분봉 타임스탬프를 tz-aware datetime(UTC)으로 정규화한다."""
    from datetime import timezone
    try:
        if value is None:
            return None
        if source == "polygon" and isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        if isinstance(value, (int, float)):
            # 밀리초/초 단위 epoch 모두 방어적으로 처리
            ts = value / 1000 if value > 1e12 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(value, str):
            import pandas as pd
            parsed = pd.Timestamp(value)
            if parsed.tzinfo is None:
                parsed = parsed.tz_localize("UTC")
            return parsed.to_pydatetime()
        return None
    except Exception as exc:
        logger.debug("[USMarketData] 분봉 타임스탬프 파싱 실패(%s, %r): %s", source, value, exc)
        return None


def fetch_us_minute_bars_dataframe(symbol: str, limit: int = 60):
    """1분봉을 pandas DataFrame(datetime tz-aware, open/high/low/close/volume)으로 반환.

    MU/NVDA/AMD/AVGO/QQQ 등 어떤 심볼에도 동일하게 쓸 수 있는 공통 함수다.
    실시간 분봉이 전혀 없으면 (None, source)를 반환한다 — 호출부에서 이를
    "휴장/장외 등으로 인한 정상 상태"로 처리할지 "API_FAILURE"로 처리할지
    판단한다 (예: market_data_collector.py, auto_market_collector.py).

    Returns
    -------
    (pandas.DataFrame | None, source: str)
    """
    bars, source = fetch_us_minute_bars(symbol, limit=limit)
    if not bars:
        return None, source
    try:
        import pandas as pd
        rows = []
        for b in bars:
            ts = _parse_bar_time(b.get("time"), source)
            if ts is None:
                continue
            rows.append({
                "datetime": ts, "open": b.get("open"), "high": b.get("high"),
                "low": b.get("low"), "close": b.get("close"), "volume": b.get("volume", 0) or 0,
            })
        if len(rows) < 2:
            return None, source
        return pd.DataFrame(rows), source
    except Exception as exc:
        logger.warning("[USMarketData] %s 분봉 DataFrame 변환 실패(%s): %s", symbol, source, exc)
        return None, source


def is_quote_stale(timestamp: Optional[str], market_open: bool, threshold_seconds: float = USABLE_SECONDS) -> bool:
    """개장일에만 stale 여부를 판단한다 — 휴장일 마지막가는 stale이 아니다."""
    if not market_open or not timestamp:
        return False
    try:
        import pandas as pd
        ts = pd.Timestamp(timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        now = pd.Timestamp.now(tz="UTC")
        return (now - ts).total_seconds() > threshold_seconds
    except Exception:
        return False


def _aggregate_bars(bars: list, n: int) -> Optional[dict]:
    """최근 n개 1분봉을 합쳐 n분봉 근사치로 만든다."""
    if len(bars) < n:
        return None
    chunk = bars[-n:]
    return {
        "open": chunk[0]["open"], "high": max(b["high"] for b in chunk),
        "low": min(b["low"] for b in chunk), "close": chunk[-1]["close"],
        "volume": sum(b.get("volume", 0) for b in chunk),
    }


def fetch_us_realtime_bar(symbol: str, market_open: bool = True) -> dict:
    """
    실시간(또는 최근) 시세 + 분봉 통합 조회.

    market_open=False(휴장/장외)이면 분봉 미수집을 오류로 취급하지 않는다.

    Returns
    -------
    dict: symbol, latest_price, latest_change_pct, latest_bar_1m,
          latest_bar_3m, latest_bar_5m, volume, timestamp, source,
          freshness_seconds, is_stale, success, data_gap_reason
    """
    quote = fetch_us_quote_multi(symbol)
    bars, bar_source = ([], "none")
    if market_open:
        bars, bar_source = fetch_us_minute_bars(symbol, limit=5)

    latest_bar_1m = bars[-1] if bars else None

    if quote.get("success"):
        freshness_seconds = 0.0
        is_stale = False
        gap_reason = "NORMAL" if market_open else "MARKET_CLOSED"
    else:
        freshness_seconds = None
        is_stale = True
        gap_reason = "API_FAILURE"

    return {
        "symbol": symbol,
        "latest_price": quote.get("price"),
        "latest_change_pct": quote.get("change_pct"),
        "latest_bar_1m": latest_bar_1m,
        "latest_bar_3m": _aggregate_bars(bars, 3),
        "latest_bar_5m": _aggregate_bars(bars, 5),
        "volume": latest_bar_1m.get("volume") if latest_bar_1m else None,
        "timestamp": quote.get("timestamp"),
        "source": quote.get("source", "none"),
        "bar_source": bar_source,
        "freshness_seconds": freshness_seconds,
        "is_stale": is_stale,
        "success": quote.get("success", False),
        "data_gap_reason": gap_reason,
    }


def fetch_us_last_session(symbol: str) -> dict:
    """마지막 완료된 미국 거래일 종가/등락률/거래량.

    실시간 데이터가 없어도(휴장 등) 항상 이 함수로 최근 세션 데이터를
    확보할 수 있어야 한다 (yfinance 일봉 히스토리 기반).
    """
    result = {
        "symbol": symbol, "close": None, "change_rate": None, "volume": None,
        "session_date": None, "source": "none", "success": False, "error": None,
        "timestamp": _now_iso(),
    }
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="10d", interval="1d", auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 1:
            result["error"] = "no_history"
            return result
        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) >= 2 else None
        close = float(last["Close"])
        change_rate = None
        if prev is not None and prev["Close"]:
            change_rate = (close - float(prev["Close"])) / float(prev["Close"]) * 100
        result.update({
            "close": close, "change_rate": change_rate,
            "volume": int(last.get("Volume", 0)),
            "session_date": hist.index[-1].strftime("%Y-%m-%d"),
            "source": "yahoo", "success": True,
        })
        return result
    except Exception as exc:
        logger.debug("[USMarketData] %s 마지막거래일 조회 실패: %s", symbol, exc)
        result["error"] = str(exc)
        return result


def fetch_optional_quote(ticker: str) -> dict:
    """holiday_mode_inputs 용 부가 데이터(일본/대만 반도체, 달러인덱스 등). 실패해도 무해."""
    try:
        from app.data.naver_global_stock_collector import fetch_naver_global_quote
        q = fetch_naver_global_quote(ticker)
        return {
            "value": q.get("price"), "change_rate": q.get("return_pct"),
            "source": q.get("source", "unknown"), "timestamp": _now_iso(),
            "success": q.get("status") == "success", "error": q.get("error"), "optional": True,
        }
    except Exception as exc:
        return {"value": None, "change_rate": None, "source": "none", "timestamp": _now_iso(),
                "success": False, "error": str(exc), "optional": True}
