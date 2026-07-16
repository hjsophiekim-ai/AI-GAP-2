"""historical_data_loader.py — SK하이닉스 ML 학습용 과거 1년치 데이터 수집.

중요한 현실적 제약(반드시 읽을 것):
  KIS `inquire-daily-price` TR은 요청 기간과 무관하게 응답이 최근 ~100행
  으로 제한된다(API 자체 한계). 따라서 "1년치"는 KIS 단독으로는 불가능하고,
  pykrx/yfinance의 장기 일봉 히스토리에 의존한다. 반면 분봉(1분/5분)은
  yfinance가 최근 7~60일 정도만 제공하고 KIS 분봉 API도 장기 백필을
  지원하지 않는다 — 즉 "1년치 분봉"은 어떤 소스로도 구할 수 없다.

  그래서 이 로더는:
    - 일봉(daily): 최대한 1년(lookback_days)에 가깝게 수집 (close/next_open
      타깃과 recovery/기술적 지표 feature의 근간).
    - 분봉(intraday, 1m/5m): "가능한 최근 기간"만 수집(수십 일 수준) —
      30분/1시간/3시간 타깃은 이 짧은 구간의 실제 표본으로만 학습된다.
  이 한계를 감추지 않고 반환 dict의 `intraday_days_available`/`daily_days_available`
  에 실제 확보한 기간을 그대로 남긴다.

데이터 소스 우선순위:
  국내: KIS(최근 구간만) -> pykrx(장기 일봉) -> 네이버(현재가만, 히스토리 아님) -> 로컬 캐시
  해외: Alpaca(최근 분봉) -> Polygon -> Finnhub(호가만) -> yfinance(장기 일봉+최근 분봉) -> 로컬 캐시

어떤 소스도 실패해도 예외를 던지지 않는다 — 항상 다음 fallback으로 넘어가고,
전부 실패하면 빈 DataFrame과 함께 error를 남긴다.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    logger = logging.getLogger(__name__)

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from app.utils.data_paths import HISTORICAL_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
HIST_RAW_DIR = HISTORICAL_DIR / "raw"

DOMESTIC_SYMBOLS = {
    "hynix": ("000660", "SK하이닉스"),
    "samsung": ("005930", "삼성전자"),
    "hanmi": ("042700", "한미반도체"),
}
OVERSEAS_SYMBOLS = {
    "mu": "MU", "nvda": "NVDA", "amd": "AMD", "avgo": "AVGO", "qqq": "QQQ",
}
# SOXX 우선, 실패 시 SMH로 대체(명세: "SOXX 또는 SMH")
SOX_PROXY_CANDIDATES = ["SOXX", "SMH"]

MIN_FULL_YEAR_ROWS = 200  # 이보다 적으면 "1년치로 보기엔 부족" -> 다음 소스로 폴백


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _env_true(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


def _kis_mode() -> Optional[str]:
    for mode in ("real", "mock"):
        if os.environ.get(f"KIS_{mode.upper()}_APP_KEY") and os.environ.get(f"KIS_{mode.upper()}_APP_SECRET"):
            return mode
    return None


def _cache_path(key: str) -> Path:
    return HIST_RAW_DIR / f"{key}_daily_1y.parquet"


def _save_cache(key: str, df: pd.DataFrame) -> None:
    try:
        HIST_RAW_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(_cache_path(key))
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] %s 캐시 저장 실패(무해): %s", key, exc)


def _load_cache(key: str) -> Optional[pd.DataFrame]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] %s 캐시 로드 실패: %s", key, exc)
        return None


# ---------------------------------------------------------------------------
# 국내 일봉(1년) — KIS(최근) -> pykrx(장기) -> yfinance(장기) -> 캐시
# ---------------------------------------------------------------------------

def _fetch_domestic_daily_kis(symbol: str, lookback_days: int, mode: str) -> Optional[pd.DataFrame]:
    try:
        import requests as rq
        from app.trading.kis_client import KISClient

        app_key = os.environ.get(f"KIS_{mode.upper()}_APP_KEY", "")
        app_secret = os.environ.get(f"KIS_{mode.upper()}_APP_SECRET", "")
        client = KISClient(
            app_key=app_key, app_secret=app_secret,
            account_no=os.environ.get("KIS_ACCOUNT_NO", "00000000"),
            product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01"), mode=mode,
        )
        token = client.get_access_token()
        headers = {
            "Content-Type": "application/json; charset=utf-8", "authorization": f"Bearer {token}",
            "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010400",
        }
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start_date, "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
        }
        resp = rq.get(f"{client.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                       headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        rows = resp.json().get("output2") or resp.json().get("output") or []
        records = []
        for row in rows:
            close = float(row.get("stck_clpr") or 0)
            if close <= 0:
                continue
            records.append({
                "datetime": pd.to_datetime(str(row.get("stck_bsop_date", "")), format="%Y%m%d", errors="coerce"),
                "open": float(row.get("stck_oprc") or close), "high": float(row.get("stck_hgpr") or close),
                "low": float(row.get("stck_lwpr") or close), "close": close,
                "volume": int(float(row.get("acml_vol") or 0)),
            })
        if not records:
            return None
        return pd.DataFrame(records).dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] KIS 일봉 실패(%s): %s", symbol, exc)
        return None


def _fetch_domestic_daily_pykrx(symbol: str, lookback_days: int) -> Optional[pd.DataFrame]:
    try:
        from pykrx import stock
        end = datetime.now()
        start = end - timedelta(days=lookback_days)
        df = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), symbol)
        if df is None or df.empty:
            return None
        col_map = {"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"}
        df = df.rename(columns=col_map)[list(col_map.values())].reset_index().rename(columns={"날짜": "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        return df.sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] pykrx 일봉 실패(%s): %s", symbol, exc)
        return None


def _fetch_domestic_daily_yfinance(symbol: str, lookback_days: int) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{symbol}.KS")
        hist = ticker.history(period=f"{lookback_days + 10}d", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        df = hist.reset_index().rename(columns={
            "Date": "datetime", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
        })
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        return df[["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] yfinance 일봉 실패(%s): %s", symbol, exc)
        return None


def collect_domestic_daily_1y(key: str, lookback_days: int = 365) -> dict:
    """key: "hynix"|"samsung"|"hanmi". Returns dict(df, source, days, error)."""
    symbol, name = DOMESTIC_SYMBOLS[key]
    mode = _kis_mode()

    if mode:
        df = _fetch_domestic_daily_kis(symbol, lookback_days, mode)
        if df is not None and len(df) >= MIN_FULL_YEAR_ROWS:
            _save_cache(key, df)
            return {"df": df, "source": "kis", "days": len(df), "symbol": symbol, "name": name,
                    "collected_at": _now_iso(), "error": None}

    df = _fetch_domestic_daily_pykrx(symbol, lookback_days)
    if df is not None and len(df) >= 30:
        _save_cache(key, df)
        return {"df": df, "source": "pykrx", "days": len(df), "symbol": symbol, "name": name,
                "collected_at": _now_iso(), "error": None}

    df = _fetch_domestic_daily_yfinance(symbol, lookback_days)
    if df is not None and len(df) >= 30:
        _save_cache(key, df)
        return {"df": df, "source": "yfinance", "days": len(df), "symbol": symbol, "name": name,
                "collected_at": _now_iso(), "error": None}

    cached = _load_cache(key)
    if cached is not None and not cached.empty:
        return {"df": cached, "source": "cache", "days": len(cached), "symbol": symbol, "name": name,
                "collected_at": _now_iso(), "error": "live_collection_failed_used_cache"}

    return {"df": pd.DataFrame(), "source": "none", "days": 0, "symbol": symbol, "name": name,
            "collected_at": _now_iso(), "error": "all_sources_failed"}


def collect_domestic_index_1y(index_key: str, lookback_days: int = 365) -> dict:
    """index_key: "KOSPI"|"KOSDAQ"|"KOSPI200"(선물 근사=현물지수). pykrx -> yfinance -> 캐시."""
    ticker_map = {"KOSPI": "1001", "KOSDAQ": "2001", "KOSPI200": "1028"}
    yf_map = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11", "KOSPI200": None}
    cache_key = f"index_{index_key}"

    try:
        from pykrx import stock
        end = datetime.now()
        start = end - timedelta(days=lookback_days)
        df = stock.get_index_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker_map[index_key])
        if df is not None and not df.empty:
            close_col = "종가" if "종가" in df.columns else "Close"
            out = df[[close_col]].rename(columns={close_col: "close"}).reset_index().rename(columns={"날짜": "datetime"})
            out["datetime"] = pd.to_datetime(out["datetime"])
            out = out.sort_values("datetime").reset_index(drop=True)
            _save_cache(cache_key, out)
            return {"df": out, "source": "pykrx", "days": len(out), "collected_at": _now_iso(), "error": None,
                    "is_futures_proxy": index_key == "KOSPI200"}
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] pykrx 지수 실패(%s): %s", index_key, exc)

    yf_symbol = yf_map.get(index_key)
    if yf_symbol:
        try:
            import yfinance as yf
            hist = yf.Ticker(yf_symbol).history(period=f"{lookback_days + 10}d", interval="1d", auto_adjust=True)
            if hist is not None and not hist.empty:
                out = hist.reset_index().rename(columns={"Date": "datetime", "Close": "close"})[["datetime", "close"]]
                out["datetime"] = pd.to_datetime(out["datetime"]).dt.tz_localize(None)
                out = out.sort_values("datetime").reset_index(drop=True)
                _save_cache(cache_key, out)
                return {"df": out, "source": "yfinance", "days": len(out), "collected_at": _now_iso(), "error": None,
                        "is_futures_proxy": index_key == "KOSPI200"}
        except Exception as exc:
            logger.debug("[HistoricalDataLoader] yfinance 지수 실패(%s): %s", index_key, exc)

    cached = _load_cache(cache_key)
    if cached is not None and not cached.empty:
        return {"df": cached, "source": "cache", "days": len(cached), "collected_at": _now_iso(),
                "error": "live_collection_failed_used_cache", "is_futures_proxy": index_key == "KOSPI200"}
    return {"df": pd.DataFrame(), "source": "none", "days": 0, "collected_at": _now_iso(),
            "error": "all_sources_failed", "is_futures_proxy": index_key == "KOSPI200"}


def collect_usdkrw_1y(lookback_days: int = 365) -> dict:
    try:
        import yfinance as yf
        hist = yf.Ticker("KRW=X").history(period=f"{lookback_days + 10}d", interval="1d", auto_adjust=True)
        if hist is not None and not hist.empty:
            out = hist.reset_index().rename(columns={"Date": "datetime", "Close": "close"})[["datetime", "close"]]
            out["datetime"] = pd.to_datetime(out["datetime"]).dt.tz_localize(None)
            out = out.sort_values("datetime").reset_index(drop=True)
            _save_cache("usdkrw", out)
            return {"df": out, "source": "yfinance", "days": len(out), "collected_at": _now_iso(), "error": None}
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] USD/KRW 실패: %s", exc)
    cached = _load_cache("usdkrw")
    if cached is not None and not cached.empty:
        return {"df": cached, "source": "cache", "days": len(cached), "collected_at": _now_iso(),
                "error": "live_collection_failed_used_cache"}
    return {"df": pd.DataFrame(), "source": "none", "days": 0, "collected_at": _now_iso(), "error": "all_sources_failed"}


# ---------------------------------------------------------------------------
# 해외 일봉(1년) — Alpaca/Polygon(장기 aggs, 키 있을 때만) -> yfinance(장기, 항상 시도) -> 캐시
# ---------------------------------------------------------------------------

def _fetch_overseas_daily_alpaca(symbol: str, lookback_days: int) -> Optional[pd.DataFrame]:
    if not _env_true("ENABLE_ALPACA_US_DATA", "true"):
        return None
    key, secret = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    try:
        import requests
        end = datetime.now()
        start = end - timedelta(days=lookback_days)
        url = (f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
               f"?timeframe=1Day&start={start.date()}&end={end.date()}&limit=10000&feed=iex")
        resp = requests.get(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=15)
        if resp.status_code != 200:
            return None
        bars = resp.json().get("bars", [])
        if not bars:
            return None
        df = pd.DataFrame([{
            "datetime": pd.Timestamp(b["t"]).tz_localize(None), "open": b["o"], "high": b["h"],
            "low": b["l"], "close": b["c"], "volume": b.get("v", 0),
        } for b in bars])
        return df.sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] Alpaca 일봉 실패(%s): %s", symbol, exc)
        return None


def _fetch_overseas_daily_polygon(symbol: str, lookback_days: int) -> Optional[pd.DataFrame]:
    if not _env_true("ENABLE_POLYGON_US_DATA", "false"):
        return None
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        return None
    try:
        import requests
        end = datetime.now()
        start = end - timedelta(days=lookback_days)
        url = (f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
               f"{start.date()}/{end.date()}?adjusted=true&sort=asc&limit=50000&apiKey={api_key}")
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            return None
        df = pd.DataFrame([{
            "datetime": pd.Timestamp(r["t"], unit="ms"), "open": r["o"], "high": r["h"],
            "low": r["l"], "close": r["c"], "volume": r.get("v", 0),
        } for r in results])
        return df.sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] Polygon 일봉 실패(%s): %s", symbol, exc)
        return None


def _fetch_overseas_daily_yfinance(symbol: str, lookback_days: int) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period=f"{lookback_days + 10}d", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        df = hist.reset_index().rename(columns={
            "Date": "datetime", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
        })
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        return df[["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime").reset_index(drop=True)
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] yfinance 일봉 실패(%s): %s", symbol, exc)
        return None


def collect_overseas_daily_1y(symbol: str, lookback_days: int = 365) -> dict:
    cache_key = f"overseas_{symbol}"
    for source_name, fetch_fn in (
        ("alpaca", _fetch_overseas_daily_alpaca), ("polygon", _fetch_overseas_daily_polygon),
        ("yfinance", _fetch_overseas_daily_yfinance),
    ):
        df = fetch_fn(symbol, lookback_days)
        if df is not None and len(df) >= 30:
            _save_cache(cache_key, df)
            return {"df": df, "source": source_name, "days": len(df), "symbol": symbol,
                    "collected_at": _now_iso(), "error": None}
    cached = _load_cache(cache_key)
    if cached is not None and not cached.empty:
        return {"df": cached, "source": "cache", "days": len(cached), "symbol": symbol,
                "collected_at": _now_iso(), "error": "live_collection_failed_used_cache"}
    return {"df": pd.DataFrame(), "source": "none", "days": 0, "symbol": symbol,
            "collected_at": _now_iso(), "error": "all_sources_failed"}


def collect_sox_proxy_1y(lookback_days: int = 365) -> dict:
    """SOXX 우선, 실패 시 SMH — 그래도 실패하면 QQQ까지 상위 호출부에서 대체 가능."""
    for symbol in SOX_PROXY_CANDIDATES:
        result = collect_overseas_daily_1y(symbol, lookback_days)
        if result["source"] != "none":
            result["proxy_symbol"] = symbol
            return result
    return {"df": pd.DataFrame(), "source": "none", "days": 0, "symbol": None,
            "collected_at": _now_iso(), "error": "sox_proxy_all_failed", "proxy_symbol": None}


# ---------------------------------------------------------------------------
# 최근 구간 분봉(1m -> 5m -> 일봉) — "1년치 분봉"은 어떤 소스로도 불가능하므로
# 확보 가능한 최근 기간만 수집한다.
# ---------------------------------------------------------------------------

def collect_recent_intraday(symbol: str, is_domestic: bool = False) -> dict:
    """
    1분봉 우선 -> 5분봉 -> 일봉(최후) 순으로 시도. 국내 종목은 KIS 분봉만 지원
    (장기 백필 불가, 당일 위주), 해외 종목은 yfinance가 최근 며칠~수십일의
    분봉을 제공한다.
    """
    if is_domestic:
        return _collect_domestic_recent_intraday(symbol)
    return _collect_overseas_recent_intraday(symbol)


def _collect_domestic_recent_intraday(symbol: str) -> dict:
    mode = _kis_mode()
    if not mode:
        return {"df": None, "granularity": "none", "source": "none", "error": "kis_credentials_missing"}
    try:
        from app.trading.kis_client import KISClient
        app_key = os.environ.get(f"KIS_{mode.upper()}_APP_KEY", "")
        app_secret = os.environ.get(f"KIS_{mode.upper()}_APP_SECRET", "")
        client = KISClient(app_key=app_key, app_secret=app_secret,
                            account_no=os.environ.get("KIS_ACCOUNT_NO", "00000000"),
                            product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01"), mode=mode)
        candles = client.get_minute_candles(symbol, period_min=1, count=120)
        if candles:
            df = pd.DataFrame(candles)
            return {"df": df, "granularity": "1m", "source": "kis", "error": None}
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] 국내 분봉 실패(%s): %s", symbol, exc)
    return {"df": None, "granularity": "none", "source": "none", "error": "domestic_intraday_unavailable"}


def _collect_overseas_recent_intraday(symbol: str) -> dict:
    try:
        from app.market import us_market_data as umd
        df, source = umd.fetch_us_minute_bars_dataframe(symbol, limit=500)
        if df is not None and not df.empty:
            return {"df": df, "granularity": "1m", "source": source, "error": None}
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] 해외 실시간 분봉 실패(%s): %s", symbol, exc)

    try:
        import yfinance as yf
        for interval, period, gran in (("5m", "60d", "5m"), ("1d", "400d", "daily")):
            hist = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
            if hist is not None and not hist.empty:
                df = hist.reset_index().rename(columns={
                    hist.index.name or "Datetime": "datetime", "Open": "open", "High": "high",
                    "Low": "low", "Close": "close", "Volume": "volume",
                })
                if "datetime" not in df.columns and "Date" in df.columns:
                    df = df.rename(columns={"Date": "datetime"})
                df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
                return {"df": df, "granularity": gran, "source": "yfinance", "error": None}
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] yfinance 분봉 실패(%s): %s", symbol, exc)

    return {"df": None, "granularity": "none", "source": "none", "error": "overseas_intraday_unavailable"}


# ---------------------------------------------------------------------------
# 전체 수집 오케스트레이션
# ---------------------------------------------------------------------------

def load_all_from_cache() -> dict:
    """네트워크 호출 없이 로컬 캐시(data/historical/raw/*.parquet)만으로 구성한다.
    재학습을 반복할 때 매번 API를 다시 두드리지 않도록 하기 위한 빠른 경로다."""
    result: dict = {"collected_at": _now_iso(), "lookback_days": None, "from_cache_only": True}
    for key in DOMESTIC_SYMBOLS:
        cached = _load_cache(key)
        result[key] = {"df": cached if cached is not None else pd.DataFrame(),
                        "source": "cache" if cached is not None else "none",
                        "days": len(cached) if cached is not None else 0, "error": None if cached is not None else "no_cache"}
        result[f"{key}_intraday"] = {"df": None, "granularity": "none", "source": "none", "error": "cache_only_mode_no_intraday"}
    for idx_key in ("kospi", "kosdaq", "kospi200"):
        cached = _load_cache(f"index_{idx_key.upper()}")
        result[idx_key] = {"df": cached if cached is not None else pd.DataFrame(),
                            "source": "cache" if cached is not None else "none",
                            "days": len(cached) if cached is not None else 0, "error": None if cached is not None else "no_cache"}
    cached = _load_cache("usdkrw")
    result["usdkrw"] = {"df": cached if cached is not None else pd.DataFrame(),
                         "source": "cache" if cached is not None else "none",
                         "days": len(cached) if cached is not None else 0, "error": None if cached is not None else "no_cache"}
    for key, symbol in OVERSEAS_SYMBOLS.items():
        cached = _load_cache(f"overseas_{symbol}")
        result[key] = {"df": cached if cached is not None else pd.DataFrame(),
                        "source": "cache" if cached is not None else "none",
                        "days": len(cached) if cached is not None else 0, "error": None if cached is not None else "no_cache"}
        result[f"{key}_intraday"] = {"df": None, "granularity": "none", "source": "none", "error": "cache_only_mode_no_intraday"}
    sox_cached = None
    for symbol in SOX_PROXY_CANDIDATES:
        sox_cached = _load_cache(f"overseas_{symbol}")
        if sox_cached is not None:
            break
    result["sox_proxy"] = {"df": sox_cached if sox_cached is not None else pd.DataFrame(),
                            "source": "cache" if sox_cached is not None else "none",
                            "days": len(sox_cached) if sox_cached is not None else 0,
                            "error": None if sox_cached is not None else "no_cache"}
    return result


def collect_all_historical(lookback_days: int = 365) -> dict:
    """1년치 학습에 필요한 모든 원자료를 수집해 하나의 dict로 반환한다.
    실패한 항목은 error 필드로 표시되며 전체 수집을 중단시키지 않는다."""
    result: dict = {"collected_at": _now_iso(), "lookback_days": lookback_days}

    for key in DOMESTIC_SYMBOLS:
        result[key] = collect_domestic_daily_1y(key, lookback_days)
        result[f"{key}_intraday"] = collect_recent_intraday(DOMESTIC_SYMBOLS[key][0], is_domestic=True)

    for idx_key in ("KOSPI", "KOSDAQ", "KOSPI200"):
        result[idx_key.lower()] = collect_domestic_index_1y(idx_key, lookback_days)

    result["usdkrw"] = collect_usdkrw_1y(lookback_days)

    for key, symbol in OVERSEAS_SYMBOLS.items():
        result[key] = collect_overseas_daily_1y(symbol, lookback_days)
        result[f"{key}_intraday"] = collect_recent_intraday(symbol, is_domestic=False)

    result["sox_proxy"] = collect_sox_proxy_1y(lookback_days)

    try:
        HIST_RAW_DIR.mkdir(parents=True, exist_ok=True)
        meta = {k: {"source": v.get("source"), "days": v.get("days"), "error": v.get("error")}
                for k, v in result.items() if isinstance(v, dict) and "source" in v}
        (HIST_RAW_DIR / "collection_meta.json").write_text(
            json.dumps({"collected_at": result["collected_at"], "sources": meta}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("[HistoricalDataLoader] 수집 메타 저장 실패(무해): %s", exc)

    return result
