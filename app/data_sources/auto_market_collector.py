"""Automatic market data collector for the SK Hynix forecast tab."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

try:
    from app.data.safe_api import safe_json as _safe_json
except ImportError:
    def _safe_json(response):  # type: ignore[misc]
        try:
            return response.json() if response else None
        except Exception:
            return None

try:
    from app.data.naver_stock_collector import (
        fetch_naver_current_price as _naver_current_price,
        fetch_naver_daily_ohlcv as _naver_daily_ohlcv,
    )
except ImportError:
    def _naver_current_price(code="000660"):  # type: ignore[misc]
        return {"current_price": None, "status": "failed", "source": "naver", "error": "import_failed"}

    def _naver_daily_ohlcv(code="000660", pages=3):  # type: ignore[misc]
        return None

try:
    from app.data.naver_global_stock_collector import fetch_naver_global_quote as _naver_global_quote
except ImportError:
    def _naver_global_quote(symbol):  # type: ignore[misc]
        return {"symbol": symbol, "price": None, "return_pct": None, "source": "failed", "status": "failed", "error": "import_failed"}

try:
    from app.data.market_data_validator import (
        validate_hynix_current_sources,
        validate_hynix_dataframe,
        validate_hynix_price,
        auto_fix_hynix_price,
        normalize_hynix_dataframe_prices,
        validate_hynix_unit_consistency,
        validate_stock_identity,
    )
except ImportError:
    def validate_hynix_dataframe(df):  # type: ignore[misc]
        if df is None or df.empty or "close" not in df.columns:
            return False, "daily data missing", df
        ok = df[df["close"].apply(lambda x: 50_000 <= float(x) <= 1_000_000)].reset_index(drop=True)
        return len(ok) >= 20, f"valid rows={len(ok)}", ok

    def validate_hynix_price(price):  # type: ignore[misc]
        return price is not None and 50_000 <= float(price) <= 1_000_000, "ok"

    def auto_fix_hynix_price(price):  # type: ignore[misc]
        if price is None:
            return None
        value = float(price)
        if 50_000 <= value <= 5_000_000:
            return value
        return None

    def normalize_hynix_dataframe_prices(df):  # type: ignore[misc]
        return df

    def validate_hynix_unit_consistency(current_price, reference_price):  # type: ignore[misc]
        try:
            cur = float(current_price)
            ref = float(reference_price)
        except (TypeError, ValueError):
            return False, "DATA_UNIT_MISMATCH: missing or non-numeric 000660 price"
        ratio = cur / ref if ref else 0.0
        if 0.08 <= ratio <= 0.12 or 8.0 <= ratio <= 12.0:
            return False, f"DATA_UNIT_MISMATCH: 000660 price ratio {ratio:.4f}"
        return True, "ok"

    def validate_hynix_current_sources(source_prices, tolerance_pct=1.0):  # type: ignore[misc]
        return False, "validator unavailable", {"source_prices": source_prices}

    def validate_stock_identity(code, name):  # type: ignore[misc]
        return code == "000660" and name == "SK하이닉스", "ok"

ROOT = Path(__file__).resolve().parent.parent.parent
MICRON_DIR = ROOT / "data" / "micron"
CACHE_DIR = ROOT / "data" / "cache"
LEGACY_HYNIX_DIR = ROOT / "data" / "hynix"

_HYNIX_DAILY_CSV = CACHE_DIR / "hynix_daily.csv"
_HYNIX_CURRENT_JSON = CACHE_DIR / "hynix_current.json"
_MU_1MIN_CSV = CACHE_DIR / "mu_1min.csv"
_GLOBAL_QUOTES_JSON = CACHE_DIR / "global_quotes.json"
_HYNIX_MINUTE_CSV = CACHE_DIR / "hynix_minute_1m.csv"
_HYNIX_INVESTOR_FLOW_JSON = CACHE_DIR / "hynix_investor_flow.json"
_DOMESTIC_INDEX_JSON = CACHE_DIR / "domestic_index.json"

HYNIX_SYMBOL = "000660"


def _configure_yfinance_cache() -> None:
    try:
        import yfinance as yf

        cache_dir = CACHE_DIR / "yfinance"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(cache_dir))
    except Exception:
        pass


def _has_kis_real_keys() -> bool:
    return bool(os.environ.get("KIS_REAL_APP_KEY")) and bool(os.environ.get("KIS_REAL_APP_SECRET"))


def _has_kis_mock_keys() -> bool:
    return bool(os.environ.get("KIS_MOCK_APP_KEY")) and bool(os.environ.get("KIS_MOCK_APP_SECRET"))


def _kis_mode() -> Optional[str]:
    if _has_kis_real_keys():
        return "real"
    if _has_kis_mock_keys():
        return "mock"
    return None


def _cache_age_hours(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600


def _fresh_cache(path: Path, max_hours: float = 24.0) -> bool:
    age = _cache_age_hours(path)
    return age is not None and age <= max_hours


def _read_json_cache(path: Path) -> Optional[dict]:
    if not _fresh_cache(path):
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json_cache(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(payload)
        data["cached_at"] = datetime.now().isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("json cache write failed for %s: %s", path, exc)


def collect_mu_data(mode: Optional[str] = None) -> dict:
    """
    기존 MU 1분/3분봉 + 현재가 수집(_collect_mu_data_core)에 세션 구분
    장외(프리마켓/애프터마켓) 데이터와 mu_extended_hours_score를 추가로
    병합한다(완전히 추가적 — 기존 필드/동작은 그대로 유지).

    확장 수집이 실패해도 기존 result는 그대로 반환된다(항상 예외 없음).
    """
    result = _collect_mu_data_core(mode)
    try:
        from app.data_sources.mu_extended_hours_collector import collect_mu_extended_hours

        extended = collect_mu_extended_hours(mode=mode)
        result["extended_hours"] = extended
        result["session_type"] = extended.get("session_type")
        result["mu_extended_hours_score"] = extended.get("mu_extended_hours_score")
        result["is_extended_hours_realtime"] = extended.get("is_realtime")
        result["is_extended_hours_delayed"] = extended.get("is_delayed")
    except Exception as exc:
        logger.debug("[AutoMarketCollector] MU 장외 확장 수집 실패(무해, 기존 결과 유지): %s", exc)
        result["extended_hours"] = None
        result["session_type"] = None
        result["mu_extended_hours_score"] = None
        result["is_extended_hours_realtime"] = None
        result["is_extended_hours_delayed"] = None
    return result


def _collect_mu_data_core(mode: Optional[str] = None) -> dict:
    """Collect MU one-minute and three-minute bars.

    Priority: KIS overseas minute -> Alpaca/Polygon/yfinance 1m(다중소스) ->
    Alpaca/Polygon/Finnhub/yfinance/Naver quote(다중소스) -> cache.

    미국장 휴장/주말이면 실시간 분봉이 없는 것을 오류로 보지 않는다
    (data_gap_reason=US_HOLIDAY 등). 개장일인데 마지막 데이터가 15분 이상
    stale이면 경고 로그를 남기고 is_stale=True로 표시한다(호출부에서 이를
    데이터 품질/신뢰도 하향에 반영할 수 있다).
    """
    mode = mode or _kis_mode()
    result = {
        "df_1min": None,
        "df_3min": None,
        "current_price": None,
        "source": None,
        "error": None,
        "fallback_chain": [],
        "current_price_status": "failed",
        "minute_1m_status": "unavailable",
        "minute_3m_status": "unavailable",
        "daily_status": "unavailable",
        "minute_error": None,
        "df_daily": None,
        "is_stale": False,
        "data_gap_reason": "NORMAL",
        "last_session": None,
    }
    market_open = _us_market_open_now()

    if mode:
        try:
            from app.data_sources.kis_overseas_minute import collect_and_save_mu, fetch_mu_current_price

            collect_and_save_mu(mode=mode)
            current_price = fetch_mu_current_price(mode=mode)
            df_1min = pd.read_csv(MICRON_DIR / "MU_1min.csv") if (MICRON_DIR / "MU_1min.csv").exists() else None
            df_3min = pd.read_csv(MICRON_DIR / "MU_3min.csv") if (MICRON_DIR / "MU_3min.csv").exists() else None
            ok_1m, reason_1m, df_1min = _validate_real_candles(df_1min, "kis")
            ok_3m, reason_3m, df_3min = _validate_real_candles(df_3min, "kis")
            if ok_1m:
                df_daily = _fetch_yfinance_daily("MU", period="90d")
                ok_daily, reason_daily, df_daily = _validate_real_daily_candles(df_daily, "yfinance")
                _save_mu_1min(df_1min)
                result.update(
                    df_1min=df_1min,
                    df_3min=df_3min if ok_3m else _resample_3min(df_1min),
                    df_daily=df_daily if ok_daily else None,
                    current_price=current_price,
                    source="kis",
                    current_price_status="success" if current_price else "failed",
                    minute_1m_status="real candle success",
                    minute_3m_status="real candle success",
                    daily_status="real candle success" if ok_daily else "unavailable",
                    minute_error=None if ok_daily else reason_daily,
                )
                result["is_stale"] = _check_mu_staleness(df_1min, market_open)
                result["fallback_chain"].append("KIS: success")
                return result
            result["minute_error"] = reason_1m
            result["fallback_chain"].append(f"KIS: minute rejected ({reason_1m})")
        except Exception as exc:
            result["error"] = f"KIS MU failed: {exc}"
            result["fallback_chain"].append(f"KIS: failed ({exc})")
    else:
        result["fallback_chain"].append("KIS: skipped (credentials missing)")

    try:
        raw_df = _fetch_multi_provider_intraday("MU", limit=120)
        if raw_df is not None and not raw_df.empty and "source" in raw_df.columns:
            provider_source = raw_df["source"].iloc[0]
        else:
            provider_source = "multi_provider"
        ok_1m, reason_1m, df_1min = _validate_real_candles(raw_df, provider_source)
        if ok_1m:
            df_3min = _resample_3min(df_1min)
            ok_3m, reason_3m, df_3min = _validate_real_candles(df_3min, provider_source)
            df_daily = _fetch_yfinance_daily("MU", period="90d")
            ok_daily, reason_daily, df_daily = _validate_real_daily_candles(df_daily, "yfinance")
            last = df_1min.iloc[-1]
            current_price = {
                "price": float(last["close"]),
                "open": float(df_1min.iloc[0]["open"]),
                "high": float(df_1min["high"].max()),
                "low": float(df_1min["low"].min()),
            }
            _save_mu_1min(df_1min)
            result.update(
                df_1min=df_1min,
                df_3min=df_3min if ok_3m else None,
                df_daily=df_daily if ok_daily else None,
                current_price=current_price,
                source=provider_source,
                current_price_status="success",
                minute_1m_status="real candle success",
                minute_3m_status="real candle success" if ok_3m else "unavailable",
                daily_status="real candle success" if ok_daily else "unavailable",
                minute_error=None if (ok_3m and ok_daily) else "; ".join(x for x in [None if ok_3m else reason_3m, None if ok_daily else reason_daily] if x),
                is_stale=_check_mu_staleness(df_1min, market_open),
            )
            result["fallback_chain"].append(f"{provider_source}_1m: success")
            return result
        if not market_open:
            # 휴장/장외 시간대에 분봉이 없는 것은 정상 상태 — 오류로 보지 않는다.
            result["minute_1m_status"] = "unavailable"
            result["data_gap_reason"] = _mu_holiday_gap_reason()
            result["fallback_chain"].append(f"multi_provider_1m: no data ({result['data_gap_reason']}, 정상)")
        else:
            result["minute_1m_status"] = "synthetic rejected" if reason_1m and "constant" in reason_1m else "unavailable"
            result["minute_error"] = reason_1m
            result["data_gap_reason"] = "API_FAILURE"
            result["fallback_chain"].append(f"multi_provider_1m: rejected ({reason_1m})")
    except Exception as exc:
        result["error"] = (result.get("error") or "") + f" | multi-provider MU minute failed: {exc}"
        result["data_gap_reason"] = "API_FAILURE" if market_open else _mu_holiday_gap_reason()
        result["fallback_chain"].append(f"multi_provider_1m: failed ({exc})")

    quote = _multi_provider_quote_then_naver_yfinance("MU")
    if quote.get("price") is not None:
        result.update(
            current_price={"price": quote["price"], "open": None, "high": None, "low": None},
            source=quote["source"],
            current_price_status="success",
            minute_1m_status="unavailable",
            minute_3m_status="unavailable",
        )
        result["fallback_chain"].append(f"{quote['source']}: quote success; minute unavailable")
        if not market_open:
            result["data_gap_reason"] = _mu_holiday_gap_reason()
        return result

    # 실시간 분봉/시세 모두 실패 — 마지막 거래일 데이터를 참고용으로 확보한다.
    try:
        from app.market import us_market_data as umd
        result["last_session"] = umd.fetch_us_last_session("MU")
    except Exception as exc:
        logger.debug("[AutoMarketCollector] MU 마지막거래일 조회 실패: %s", exc)

    cached = _load_mu_1min_cache()
    if cached is not None:
        ok_cache, reason_cache, cached = _validate_real_candles(cached, "cache")
        if not ok_cache:
            result["minute_1m_status"] = "synthetic rejected" if "constant" in reason_cache else "unavailable"
            result["minute_error"] = reason_cache
            result["fallback_chain"].append(f"cache: rejected ({reason_cache})")
            return result
        df_3min = _resample_3min(cached)
        current_price = {"price": float(cached.iloc[-1]["close"]), "open": None, "high": None, "low": None}
        result["fallback_chain"].append("cache: present but rejected for live prediction")
        if market_open:
            result["error"] = (result.get("error") or "") + " | MU live minute collection failed; cache not allowed"
        else:
            result["data_gap_reason"] = _mu_holiday_gap_reason()
    elif _MU_1MIN_CSV.exists():
        result["fallback_chain"].append(f"cache: stale ({_cache_age_hours(_MU_1MIN_CSV):.1f}h)")
        if not market_open:
            result["data_gap_reason"] = _mu_holiday_gap_reason()
        else:
            result["error"] = (result.get("error") or "") + " | MU cache stale"
    return result


def collect_nvda_data(mode: Optional[str] = None) -> dict:
    """Collect NVDA quote. Priority: KIS, Naver global, yfinance, cache."""
    mode = mode or _kis_mode()
    result = {"current_price": None, "premarket_return": None, "regular_return": None, "source": None, "error": None}

    if mode:
        try:
            from app.data_sources.kis_overseas_minute import BASE_URL_MOCK, BASE_URL_REAL, _get_access_token, _load_credentials
            import requests as rq

            base_url = BASE_URL_REAL if mode == "real" else BASE_URL_MOCK
            creds = _load_credentials(mode)
            token = _get_access_token(mode)
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": creds["app_key"],
                "appsecret": creds["app_secret"],
                "tr_id": "HHDFS00000300",
            }
            params = {"AUTH": "", "EXCD": "NAS", "SYMB": "NVDA"}
            response = rq.get(f"{base_url}/uapi/overseas-stock/v1/quotations/price", headers=headers, params=params, timeout=10)
            body = _safe_json(response)
            if body is None:
                raise ValueError("KIS response is not JSON")
            out = body.get("output", {})
            price = _float_or_none(out.get("last") or out.get("zdiv"))
            if price:
                ret = _float_or_none(out.get("rate") or out.get("diff_rate"))
                result.update(current_price=price, regular_return=ret, source="kis")
                _save_global_quote("NVDA", price, ret, "kis")
                return result
        except Exception as exc:
            result["error"] = f"KIS NVDA failed: {exc}"

    quote = _multi_provider_quote_then_naver_yfinance("NVDA")
    if quote.get("price") is not None:
        result.update(current_price=quote["price"], regular_return=quote.get("return_pct"), source=quote["source"], error=None)
        return result

    cached = _load_global_quote("NVDA")
    if cached:
        result.update(current_price=cached.get("price"), regular_return=cached.get("return_pct"), source="cache")
    return result


def collect_index_data() -> dict:
    """Collect Nasdaq futures, SOXX/SOX, and USD/KRW. Priority: Naver global, yfinance."""
    result = {
        "qqq_return": None,
        "sox_return": None,
        "usdkrw_change": None,
        "source": None,
        "error": None,
        "fallback_detail": {},
        "source_detail": {},
    }

    qqq = _quote_with_naver_then_yfinance("NQ=F")
    nasdaq_proxy = None
    if qqq.get("return_pct") is None:
        nasdaq_proxy = _multi_provider_quote_then_naver_yfinance("QQQ")
        if nasdaq_proxy.get("return_pct") is not None:
            qqq = dict(nasdaq_proxy)
            qqq["source"] = f"{qqq.get('source')}_qqq_proxy"
    soxx = _multi_provider_quote_then_naver_yfinance("SOXX")
    if soxx.get("return_pct") is None:
        soxx = _multi_provider_quote_then_naver_yfinance("SOX")
    usdkrw = _quote_with_naver_then_yfinance("USDKRW")

    values = {
        "NASDAQ_FUTURES": ("qqq_return", qqq),
        "SOXX": ("sox_return", soxx),
        "USDKRW": ("usdkrw_change", usdkrw),
    }
    sources = []
    for symbol, (field, quote) in values.items():
        value = quote.get("return_pct")
        result[field] = value
        ok = value is not None
        if symbol == "NASDAQ_FUTURES" and nasdaq_proxy and ok:
            result["fallback_detail"][symbol] = "QQQ proxy success"
        else:
            result["fallback_detail"][symbol] = "success" if ok else "failed"
        result["source_detail"][symbol] = quote.get("source") if ok else "failed"
        if ok:
            sources.append(quote.get("source"))

    if sources:
        result["source"] = "mixed" if len(set(sources)) > 1 else sources[0]
    else:
        result["error"] = "Nasdaq futures/SOXX/USDKRW collection failed"
    return result


def collect_hynix_daily(mode: Optional[str] = None, n_days: int = 70) -> dict:
    """Collect SK Hynix daily candles and current price.

    Priority: KIS, Naver Finance, yfinance, fresh cache.
    """
    mode = mode or _kis_mode()
    result = {
        "df_daily": None,
        "prev_close": None,
        "current_price": None,
        "source": None,
        "error": None,
        "fallback_chain": [],
        "source_detail": {"current_price": None, "daily_ohlcv": None},
        "stock_identity": {"code": "000660", "name": "SK하이닉스", "ok": False, "message": None},
        "price_validation": {"ok": False, "message": "not collected", "source_prices": {}},
        "current_price_sources": {},
        "collected_at": None,
        "cache_stale": False,
    }
    identity_ok, identity_msg = validate_stock_identity("000660", "SK하이닉스")
    result["stock_identity"] = {"code": "000660", "name": "SK하이닉스", "ok": identity_ok, "message": identity_msg}
    if not identity_ok:
        result["error"] = f"Hynix stock identity validation failed: {identity_msg}"
        return result

    def accept(df: Optional[pd.DataFrame], source: str, current_price: Optional[float] = None, current_source: Optional[str] = None) -> bool:
        identity_ok, identity_msg = validate_stock_identity("000660", "SK하이닉스")
        result["stock_identity"] = {"code": "000660", "name": "SK하이닉스", "ok": identity_ok, "message": identity_msg}
        if not identity_ok:
            result["fallback_chain"].append(f"{source}: identity failed ({identity_msg})")
            return False
        df = normalize_hynix_dataframe_prices(df)
        valid, msg, df_ok = validate_hynix_dataframe(df)
        if not valid:
            result["fallback_chain"].append(f"{source}: validation failed ({msg})")
            return False
        last_close = float(df_ok.iloc[-1]["close"])
        if current_price is None:
            result["fallback_chain"].append(f"{source}: current price missing")
            return False
        price = auto_fix_hynix_price(current_price)
        price_ok, price_msg = validate_hynix_price(price)
        if not price_ok:
            result["fallback_chain"].append(f"{source}: invalid current price ({price_msg})")
            return False
        unit_ok, unit_msg = validate_hynix_unit_consistency(price, last_close)
        if not unit_ok:
            result["error"] = unit_msg
            result["data_gap_reason"] = "DATA_UNIT_MISMATCH"
            result["fallback_chain"].append(f"{source}: {unit_msg}")
            return False

        _save_hynix_daily(df_ok)
        _save_hynix_current(price, current_source or source)
        result.update(df_daily=df_ok, prev_close=last_close, current_price=float(price), source=source)
        result["source_detail"] = {"current_price": current_source or source, "daily_ohlcv": source}
        result["collected_at"] = datetime.now().isoformat()
        result["fallback_chain"].append(f"{source}: success")
        # 이전 단계(예: KIS)가 실패해도 최종적으로 다른 소스에서 성공했다면
        # 남아 있는 이전 오류 메시지를 지운다 — 성공 결과에 오류가 남지 않도록.
        result["error"] = None
        logger.warning("[HYNIX_PRICE] current_price source=%s value=%s", current_source or source, price)
        logger.warning("[HYNIX_DAILY] last_close=%s prev_close=%s date=%s", last_close, last_close, df_ok.iloc[-1].get("datetime"))
        return True

    kis_current_price = None
    if mode:
        kis_current_price = _fetch_hynix_current_from_kis(mode)
        if kis_current_price is not None:
            result["fallback_chain"].append("KIS current: success")
            logger.warning("[HYNIX_PRICE] current_price source=KIS value=%s", kis_current_price)
        else:
            result["fallback_chain"].append("KIS current: failed")

    df_kis = None
    if mode:
        try:
            df_kis = _fetch_hynix_daily_from_kis(mode, n_days)
            result["fallback_chain"].append("KIS daily: collected")
        except Exception as exc:
            result["error"] = f"KIS Hynix daily failed: {exc}"
            result["fallback_chain"].append(f"KIS: failed ({exc})")
    else:
        result["fallback_chain"].append("KIS: skipped (credentials missing)")

    naver_current_price = None
    try:
        current = _naver_current_price("000660")
        if current.get("status") == "success" and current.get("current_price") is not None:
            naver_current_price = float(current["current_price"])
            result["fallback_chain"].append("Naver current: success")
            logger.warning("[HYNIX_PRICE] current_price source=naver value=%s", naver_current_price)
        else:
            result["fallback_chain"].append(f"Naver current: failed ({current.get('error')})")
    except Exception as exc:
        result["fallback_chain"].append(f"Naver current: failed ({exc})")

    yahoo_current_price = None
    try:
        yahoo_current_price = _fetch_hynix_current_from_yfinance()
        if yahoo_current_price is not None:
            result["fallback_chain"].append("Yahoo current: success")
            logger.warning("[HYNIX_PRICE] current_price source=yfinance value=%s", yahoo_current_price)
        else:
            result["fallback_chain"].append("Yahoo current: failed")
    except Exception as exc:
        result["fallback_chain"].append(f"Yahoo current: failed ({exc})")

    current_sources = {"KIS": kis_current_price, "naver": naver_current_price, "yfinance": yahoo_current_price}
    price_ok, price_msg, price_detail = validate_hynix_current_sources(current_sources)
    result["current_price_sources"] = current_sources
    result["price_validation"] = {"ok": price_ok, "message": price_msg, **price_detail}
    if (price_detail or {}).get("data_gap_reason") == "DATA_UNIT_MISMATCH" or "DATA_UNIT_MISMATCH" in str(price_msg):
        result["error"] = price_msg
        result["data_gap_reason"] = "DATA_UNIT_MISMATCH"
        result["fallback_chain"].append(f"current price cross-check: blocked ({price_msg})")
        return result

    # 교차검증 실패(소스간 가격 불일치)는 더 이상 전체 수집을 중단시키지 않는다.
    # API(KIS)를 우선 시도하고, 실패/불충분하면 네이버증권 → yfinance 순으로
    # "개별 소스 자체 검증"(validate_hynix_price/validate_hynix_dataframe)만
    # 통과하면 그 소스를 그대로 사용한다. 교차검증 결과는 진단 로그로만 남긴다.
    if not price_ok:
        logger.warning(
            "[HYNIX_PRICE] 소스간 가격 불일치(참고용, 수집은 계속 진행): %s | sources=%s",
            price_msg, current_sources,
        )
        result["fallback_chain"].append(f"current price cross-check: mismatch ignored ({price_msg})")
        fallback_anchor_price = None
        fallback_anchor_source = None
    else:
        fallback_anchor_price = price_detail["selected_price"]
        fallback_anchor_source = price_detail["selected_source"]

    # ── 1순위: KIS(API) ──────────────────────────────────────────────────
    if df_kis is not None:
        kis_price = kis_current_price if kis_current_price is not None else fallback_anchor_price
        kis_src = "KIS" if kis_current_price is not None else (fallback_anchor_source or "KIS")
        if accept(df_kis, "KIS", kis_price, kis_src):
            return result

    # ── 2순위: 네이버증권(https://finance.naver.com/) ───────────────────
    try:
        df_naver = _naver_daily_ohlcv("000660", pages=4)
        naver_price = naver_current_price if naver_current_price is not None else fallback_anchor_price
        naver_src = "naver" if naver_current_price is not None else (fallback_anchor_source or "naver")
        if accept(df_naver, "naver", naver_price, naver_src):
            return result
    except Exception as exc:
        result["error"] = (result.get("error") or "") + f" | Naver Hynix daily failed: {exc}"
        result["fallback_chain"].append(f"Naver daily: failed ({exc})")

    # ── 3순위: yfinance ──────────────────────────────────────────────────
    try:
        import yfinance as yf

        hist = yf.Ticker("000660.KS").history(period=f"{n_days + 30}d", interval="1d", auto_adjust=True)
        if hist is not None and not hist.empty:
            df_yf = _normalize_yf_ohlcv(hist)
            yf_price = yahoo_current_price if yahoo_current_price is not None else fallback_anchor_price
            yf_src = "yfinance" if yahoo_current_price is not None else (fallback_anchor_source or "yfinance")
            if accept(df_yf, "yfinance", yf_price, yf_src):
                return result
        else:
            result["fallback_chain"].append("yfinance: no data")
    except Exception as exc:
        result["error"] = (result.get("error") or "") + f" | yfinance Hynix failed: {exc}"
        result["fallback_chain"].append(f"yfinance: failed ({exc})")

    if _HYNIX_DAILY_CSV.exists():
        result["cache_stale"] = True
        result["fallback_chain"].append("cache: present but rejected for live prediction")
        result["error"] = (result.get("error") or "") + " | Hynix live daily collection failed; cache not allowed"

    return result


def collect_amd_data(mode: Optional[str] = None) -> dict:
    """Collect AMD quote. Priority: KIS overseas, Naver global, yfinance, cache."""
    mode = mode or _kis_mode()
    result = {"current_price": None, "regular_return": None, "source": None, "error": None}

    if mode:
        try:
            from app.data_sources.kis_overseas_minute import BASE_URL_MOCK, BASE_URL_REAL, _get_access_token, _load_credentials
            import requests as rq

            base_url = BASE_URL_REAL if mode == "real" else BASE_URL_MOCK
            creds = _load_credentials(mode)
            token = _get_access_token(mode)
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": creds["app_key"],
                "appsecret": creds["app_secret"],
                "tr_id": "HHDFS00000300",
            }
            params = {"AUTH": "", "EXCD": "NAS", "SYMB": "AMD"}
            response = rq.get(f"{base_url}/uapi/overseas-stock/v1/quotations/price", headers=headers, params=params, timeout=10)
            body = _safe_json(response)
            if body is None:
                raise ValueError("KIS response is not JSON")
            out = body.get("output", {})
            price = _float_or_none(out.get("last") or out.get("zdiv"))
            if price:
                ret = _float_or_none(out.get("rate") or out.get("diff_rate"))
                result.update(current_price=price, regular_return=ret, source="kis")
                _save_global_quote("AMD", price, ret, "kis")
                return result
        except Exception as exc:
            result["error"] = f"KIS AMD failed: {exc}"

    quote = _multi_provider_quote_then_naver_yfinance("AMD")
    if quote.get("price") is not None:
        result.update(current_price=quote["price"], regular_return=quote.get("return_pct"), source=quote["source"], error=None)
        return result

    cached = _load_global_quote("AMD")
    if cached:
        result.update(current_price=cached.get("price"), regular_return=cached.get("return_pct"), source="cache")
    return result


def collect_avgo_data(mode: Optional[str] = None) -> dict:
    """Collect Broadcom(AVGO) quote. Priority: KIS overseas, multi-provider(Alpaca/Polygon/Finnhub), Naver/yfinance, cache."""
    mode = mode or _kis_mode()
    result = {"current_price": None, "regular_return": None, "source": None, "error": None}

    if mode:
        try:
            from app.data_sources.kis_overseas_minute import BASE_URL_MOCK, BASE_URL_REAL, _get_access_token, _load_credentials
            import requests as rq

            base_url = BASE_URL_REAL if mode == "real" else BASE_URL_MOCK
            creds = _load_credentials(mode)
            token = _get_access_token(mode)
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": creds["app_key"],
                "appsecret": creds["app_secret"],
                "tr_id": "HHDFS00000300",
            }
            params = {"AUTH": "", "EXCD": "NAS", "SYMB": "AVGO"}
            response = rq.get(f"{base_url}/uapi/overseas-stock/v1/quotations/price", headers=headers, params=params, timeout=10)
            body = _safe_json(response)
            if body is None:
                raise ValueError("KIS response is not JSON")
            out = body.get("output", {})
            price = _float_or_none(out.get("last") or out.get("zdiv"))
            if price:
                ret = _float_or_none(out.get("rate") or out.get("diff_rate"))
                result.update(current_price=price, regular_return=ret, source="kis")
                _save_global_quote("AVGO", price, ret, "kis")
                return result
        except Exception as exc:
            result["error"] = f"KIS AVGO failed: {exc}"

    quote = _multi_provider_quote_then_naver_yfinance("AVGO")
    if quote.get("price") is not None:
        result.update(current_price=quote["price"], regular_return=quote.get("return_pct"), source=quote["source"], error=None)
        return result

    cached = _load_global_quote("AVGO")
    if cached:
        result.update(current_price=cached.get("price"), regular_return=cached.get("return_pct"), source="cache")
    return result


def collect_domestic_index_data() -> dict:
    """Collect KOSPI/KOSPI200 return %. Priority: pykrx, yfinance(KOSPI only), cache."""
    result = {"kospi_return": None, "kospi200_return": None, "source": None, "error": None}

    def _pykrx_index_return(ticker: str) -> Optional[float]:
        from pykrx import stock

        end = datetime.now()
        start = end - timedelta(days=10)
        df = stock.get_index_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker)
        if df is None or df.empty or len(df) < 2:
            return None
        close_col = "종가" if "종가" in df.columns else "Close"
        closes = df[close_col].dropna()
        if len(closes) < 2:
            return None
        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        if prev <= 0:
            return None
        return (last / prev - 1.0) * 100

    try:
        kospi_ret = _pykrx_index_return("1001")
        kospi200_ret = _pykrx_index_return("1028")
        if kospi_ret is not None or kospi200_ret is not None:
            result.update(kospi_return=kospi_ret, kospi200_return=kospi200_ret, source="pykrx")
            _write_json_cache(_DOMESTIC_INDEX_JSON, {"kospi_return": kospi_ret, "kospi200_return": kospi200_ret, "source": "pykrx"})
            return result
    except Exception as exc:
        result["error"] = f"pykrx index failed: {exc}"

    try:
        yf_quote = _fetch_global_quote_from_yfinance("^KS11")
        if yf_quote.get("status") == "success" and yf_quote.get("return_pct") is not None:
            result.update(kospi_return=yf_quote["return_pct"], source="yfinance")
            _write_json_cache(_DOMESTIC_INDEX_JSON, {"kospi_return": yf_quote["return_pct"], "kospi200_return": None, "source": "yfinance"})
            return result
    except Exception as exc:
        result["error"] = (result.get("error") or "") + f" | yfinance KOSPI failed: {exc}"

    cached = _read_json_cache(_DOMESTIC_INDEX_JSON)
    if cached:
        result.update(kospi_return=cached.get("kospi_return"), kospi200_return=cached.get("kospi200_return"), source="cache")
    return result


def collect_hynix_minute(mode: Optional[str] = None, count: int = 60) -> dict:
    """Collect SK Hynix 1/3/5-minute candles. Priority: KIS, fresh cache.

    Naver domestic minute charts require JS-rendered pages, so no Naver
    fallback is implemented; missing data is reported via `error`/`status`.
    """
    mode = mode or _kis_mode()
    result = {
        "df_1min": None, "df_3min": None, "df_5min": None,
        "source": None, "error": None, "status": "unavailable",
        "last_bar_time": None,
    }

    if mode:
        try:
            app_key = os.environ.get(f"KIS_{mode.upper()}_APP_KEY", "")
            app_secret = os.environ.get(f"KIS_{mode.upper()}_APP_SECRET", "")
            if not app_key or not app_secret:
                raise ValueError("KIS 인증 정보 없음")
            from app.trading.kis_client import KISClient

            client = KISClient(
                app_key=app_key, app_secret=app_secret,
                account_no=os.environ.get("KIS_ACCOUNT_NO", "00000000"),
                product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01"),
                mode=mode,
            )
            candles = client.get_minute_candles(HYNIX_SYMBOL, period_min=1, count=count)
            if candles:
                df_1min = pd.DataFrame(candles).rename(columns={"time": "time"})
                df_1min["volume"] = pd.to_numeric(df_1min["volume"], errors="coerce")
                for col in ["open", "high", "low", "close"]:
                    df_1min[col] = pd.to_numeric(df_1min[col], errors="coerce")
                today = datetime.now().strftime("%Y-%m-%d")
                df_1min["datetime"] = pd.to_datetime(
                    today + " " + df_1min["time"].astype(str).str.zfill(6).str[:2]
                    + ":" + df_1min["time"].astype(str).str.zfill(6).str[2:4]
                    + ":" + df_1min["time"].astype(str).str.zfill(6).str[4:6],
                    errors="coerce",
                )
                df_1min = df_1min.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
                if not df_1min.empty:
                    df_3min = _resample_minutes(df_1min, 3)
                    df_5min = _resample_minutes(df_1min, 5)
                    _save_hynix_minute(df_1min)
                    result.update(
                        df_1min=df_1min, df_3min=df_3min, df_5min=df_5min,
                        source="kis", status="success",
                        last_bar_time=df_1min["datetime"].iloc[-1].isoformat(),
                    )
                    return result
            result["error"] = "KIS 분봉 응답 없음"
        except Exception as exc:
            result["error"] = f"KIS Hynix minute failed: {exc}"
    else:
        result["error"] = "KIS 인증 정보 없음 (mock/real 모두 미설정)"

    cached = _load_hynix_minute_cache()
    if cached is not None and not cached.empty:
        df_3min = _resample_minutes(cached, 3)
        df_5min = _resample_minutes(cached, 5)
        result.update(
            df_1min=cached, df_3min=df_3min, df_5min=df_5min,
            source="cache", status="stale_cache",
            last_bar_time=cached["datetime"].iloc[-1].isoformat(),
        )
        result["error"] = (result.get("error") or "") + " | 실시간 분봉 수집 실패, 캐시 사용(지연 가능)"
        return result

    seeded = _build_hynix_minute_seed_from_current(mode=mode)
    if seeded is not None and not seeded.empty:
        df_3min = _resample_minutes(seeded, 3)
        df_5min = _resample_minutes(seeded, 5)
        result.update(
            df_1min=seeded, df_3min=df_3min, df_5min=df_5min,
            source="quote_seed", status="quote_seed",
            last_bar_time=seeded["datetime"].iloc[-1].isoformat(),
        )
        result["error"] = (
            (result.get("error") or "")
            + " | live minute unavailable; using same-day current-price seed only"
        )
    return result


def _build_hynix_minute_seed_from_current(mode: Optional[str] = None, bars: int = 5) -> Optional[pd.DataFrame]:
    """Build neutral same-day minute bars from current quote when KIS minute is empty."""
    price = None
    source = None

    if _fresh_cache(_HYNIX_CURRENT_JSON, max_hours=1.0):
        cached = _load_hynix_current_cache() or {}
        try:
            price = float(cached.get("current_price") or 0)
            source = cached.get("source") or "current_cache"
        except Exception:
            price = None

    if not price and mode:
        try:
            price = _fetch_hynix_current_from_kis(mode)
            source = "KIS"
        except Exception:
            price = None

    if not price:
        try:
            current = _naver_current_price(HYNIX_SYMBOL)
            if current.get("status") == "success" and current.get("current_price") is not None:
                price = float(current["current_price"])
                source = "naver"
        except Exception:
            price = None

    if not price or price <= 0:
        return None

    now = datetime.now().replace(second=0, microsecond=0)
    rows = []
    for offset in range(max(2, int(bars)) - 1, -1, -1):
        ts = now - pd.Timedelta(minutes=offset)
        rows.append({
            "time": ts.strftime("%H%M%S"),
            "open": float(price),
            "high": float(price),
            "low": float(price),
            "close": float(price),
            "volume": 0,
            "datetime": ts,
            "source": f"{source or 'quote'}_seed",
        })
    return pd.DataFrame(rows)


def collect_investor_flow(mode: Optional[str] = None, symbol: str = HYNIX_SYMBOL) -> dict:
    """Collect foreign/institutional net-buy for SK Hynix. Priority: KIS, Naver, cache."""
    mode = mode or _kis_mode()
    result = {"foreign_net_buy": None, "institution_net_buy": None, "source": None, "error": None}

    if mode:
        try:
            app_key = os.environ.get(f"KIS_{mode.upper()}_APP_KEY", "")
            app_secret = os.environ.get(f"KIS_{mode.upper()}_APP_SECRET", "")
            if not app_key or not app_secret:
                raise ValueError("KIS 인증 정보 없음")
            from app.trading.kis_client import KISClient

            client = KISClient(
                app_key=app_key, app_secret=app_secret,
                account_no=os.environ.get("KIS_ACCOUNT_NO", "00000000"),
                product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01"),
                mode=mode,
            )
            rows = client.get_investor_trend(symbol)
            if rows:
                latest = rows[0]
                result.update(
                    foreign_net_buy=latest.get("foreign_net_buy"),
                    institution_net_buy=latest.get("institution_net_buy"),
                    source="kis",
                )
                _write_json_cache(_HYNIX_INVESTOR_FLOW_JSON, result)
                return result
        except Exception as exc:
            result["error"] = f"KIS investor flow failed: {exc}"

    try:
        from app.data_sources.naver_investor_flow import fetch_naver_investor_flow

        naver_flow = fetch_naver_investor_flow(symbol)
        if naver_flow.get("status") == "success":
            result.update(
                foreign_net_buy=naver_flow.get("foreign_net_buy"),
                institution_net_buy=naver_flow.get("institution_net_buy"),
                source="naver",
                error=None,
            )
            _write_json_cache(_HYNIX_INVESTOR_FLOW_JSON, result)
            return result
        result["error"] = (result.get("error") or "") + f" | Naver investor flow failed: {naver_flow.get('error')}"
    except Exception as exc:
        result["error"] = (result.get("error") or "") + f" | Naver investor flow failed: {exc}"

    cached = _read_json_cache(_HYNIX_INVESTOR_FLOW_JSON)
    if cached:
        result.update(
            foreign_net_buy=cached.get("foreign_net_buy"),
            institution_net_buy=cached.get("institution_net_buy"),
            source="cache",
        )
    return result


def collect_kospilab_data(force_refresh: bool = False) -> dict:
    try:
        from app.data_sources.kospilab_scraper import fetch_kospilab_data

        return fetch_kospilab_data(force_refresh=force_refresh)
    except Exception as exc:
        return {
            "hynix_reference_price": None,
            "hynix_reference_return": None,
            "samsung_reference_return": None,
            "hyundai_reference_return": None,
            "source_status": "failed",
            "error_message": str(exc),
        }


def collect_all(mode: Optional[str] = None) -> dict:
    mu = collect_mu_data(mode=mode)
    nvda = collect_nvda_data(mode=mode)
    amd = collect_amd_data(mode=mode)
    avgo = collect_avgo_data(mode=mode)
    index = collect_index_data()
    domestic_index = collect_domestic_index_data()
    hynix = collect_hynix_daily(mode=mode)
    hynix_minute = collect_hynix_minute(mode=mode)
    investor_flow = collect_investor_flow(mode=mode)
    kospilab = collect_kospilab_data()

    try:
        from app.data_sources.hynix_news_momentum import compute_news_momentum_score

        news = compute_news_momentum_score()
    except Exception as exc:
        news = {"score": 5.0, "success": False, "source": "fallback_neutral", "error": str(exc), "keywords_found": []}

    errors = [
        f"MU: {mu['error']}" if mu.get("error") else None,
        f"NVDA: {nvda['error']}" if nvda.get("error") else None,
        f"AMD: {amd['error']}" if amd.get("error") else None,
        f"AVGO: {avgo['error']}" if avgo.get("error") else None,
        f"Index: {index['error']}" if index.get("error") else None,
        f"DomesticIndex: {domestic_index['error']}" if domestic_index.get("error") else None,
        f"Hynix: {hynix['error']}" if hynix.get("error") else None,
        f"HynixMinute: {hynix_minute['error']}" if hynix_minute.get("error") else None,
        f"InvestorFlow: {investor_flow['error']}" if investor_flow.get("error") else None,
        f"Kospilab: {kospilab.get('error_message')}" if kospilab.get("source_status") == "failed" else None,
        f"News: {news['error']}" if news.get("error") else None,
    ]

    return {
        "mu": mu,
        "nvda": nvda,
        "amd": amd,
        "avgo": avgo,
        "index": index,
        "domestic_index": domestic_index,
        "hynix": hynix,
        "hynix_minute": hynix_minute,
        "investor_flow": investor_flow,
        "kospilab": kospilab,
        "news": news,
        "collected_at": datetime.now().isoformat(),
        "errors": [err for err in errors if err],
    }


def _quote_with_naver_then_yfinance(symbol: str) -> dict:
    quote = _naver_global_quote(symbol)
    if quote.get("status") == "success" and quote.get("price") is not None:
        _save_global_quote(symbol, quote.get("price"), quote.get("return_pct"), quote.get("source"))
        return quote
    yf_quote = _fetch_global_quote_from_yfinance(symbol)
    if yf_quote.get("status") == "success" and yf_quote.get("price") is not None:
        _save_global_quote(symbol, yf_quote.get("price"), yf_quote.get("return_pct"), yf_quote.get("source"))
        return yf_quote
    return quote


def _fetch_global_quote_from_yfinance(symbol: str) -> dict:
    yf_symbol = {
        "SOX": "^SOX",
        "USDKRW": "KRW=X",
    }.get(symbol.upper(), symbol)
    result = {"symbol": symbol, "price": None, "return_pct": None, "source": "yfinance", "status": "failed", "error": None}
    try:
        _configure_yfinance_cache()
        import yfinance as yf

        hist = yf.Ticker(yf_symbol).history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            result["error"] = "empty history"
            return result
        close = hist["Close"].dropna()
        if close.empty:
            result["error"] = "missing close"
            return result
        price = float(close.iloc[-1])
        return_pct = None
        if len(close) >= 2 and float(close.iloc[-2]) > 0:
            return_pct = (price / float(close.iloc[-2]) - 1.0) * 100
        result.update(price=price, return_pct=return_pct, status="success")
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def _multi_provider_quote_then_naver_yfinance(symbol: str) -> dict:
    """Alpaca -> Polygon -> Finnhub 우선 시도, 실패 시 기존 네이버/yfinance 체인으로 폴백.

    MU/NVDA/AMD/QQQ 등 모든 해외 시세 조회에 공통으로 쓰는 함수.
    API 키가 없는 소스는 조용히 skip되고(us_market_data 내부에서 처리) 다음
    소스로 넘어가므로 여기서는 결과의 source만 확인하면 된다.
    """
    try:
        from app.market import us_market_data as umd
        q = umd.fetch_us_quote_multi(symbol)
        if q.get("success") and q.get("price") is not None and q.get("source") in ("alpaca", "polygon", "finnhub"):
            return {
                "symbol": symbol, "price": q["price"], "return_pct": q.get("change_pct"),
                "source": q["source"], "status": "success", "error": None,
            }
    except Exception as exc:
        logger.debug("[AutoMarketCollector] %s 다중소스 시세 조회 실패(무시): %s", symbol, exc)
    return _quote_with_naver_then_yfinance(symbol)


def _fetch_multi_provider_intraday(symbol: str, limit: int = 120) -> Optional[pd.DataFrame]:
    """Alpaca -> Polygon -> yfinance(기존 백업 경로) 우선순위로 1분봉을 가져온다.

    Finnhub 무료 플랜은 분봉을 지원하지 않으므로 quote 전용으로만 쓰인다
    (_multi_provider_quote_then_naver_yfinance 참고). Alpaca/Polygon 키가
    없거나 실패하면 기존에 검증된 _fetch_yfinance_intraday()로 폴백한다
    (yfinance는 항상 이 함수를 통해서만 호출 — 백업/전일 확인용).
    """
    try:
        from app.market import us_market_data as umd

        bars: list = []
        source = "none"
        try:
            bars = umd._fetch_alpaca_bars(symbol, limit=limit)
            if bars:
                source = "alpaca"
        except Exception as exc:
            logger.debug("[AutoMarketCollector] %s Alpaca 분봉 실패: %s", symbol, exc)
        if not bars:
            try:
                bars = umd._fetch_polygon_bars(symbol, limit=limit)
                if bars:
                    source = "polygon"
            except Exception as exc:
                logger.debug("[AutoMarketCollector] %s Polygon 분봉 실패: %s", symbol, exc)

        if bars:
            rows = []
            for b in bars:
                ts = umd._parse_bar_time(b.get("time"), source)
                if ts is None:
                    continue
                rows.append({
                    "datetime": ts, "open": b.get("open"), "high": b.get("high"),
                    "low": b.get("low"), "close": b.get("close"), "volume": b.get("volume", 0) or 0,
                })
            if len(rows) >= 10:
                df = pd.DataFrame(rows)
                df["source"] = source
                df["session"] = df["datetime"].apply(_classify_us_session)
                return df[["datetime", "open", "high", "low", "close", "volume", "source", "session"]]
    except Exception as exc:
        logger.debug("[AutoMarketCollector] us_market_data 다중소스 분봉 로드 실패(%s): %s", symbol, exc)

    # Alpaca/Polygon 미설정/실패 -> 기존 yfinance 백업 경로(변경 없음)
    yf_df = _fetch_yfinance_intraday(symbol, period="5d", interval="1m")
    if yf_df is not None and not yf_df.empty and "source" not in yf_df.columns:
        yf_df = yf_df.copy()
        yf_df["source"] = "yfinance"
    return yf_df


def _us_market_open_now() -> bool:
    """미국장이 현재(정규장 기준) 열려 있는지. 판단 실패 시 True(보수적으로 오류 취급 방지 안함)."""
    try:
        from app.market import us_market_data as umd
        return bool(umd.get_us_market_status().get("is_us_market_open"))
    except Exception:
        return True


def _check_mu_staleness(df_1min: Optional[pd.DataFrame], market_open: bool, symbol: str = "MU") -> bool:
    """개장일에만 stale 여부를 판단하고, stale이면 경고 로그를 남긴다.

    휴장/주말이면 마지막 데이터가 오래된 것이 정상이므로 stale로 취급하지 않는다.
    """
    if not market_open or df_1min is None or df_1min.empty or "datetime" not in df_1min.columns:
        return False
    try:
        last_ts = pd.Timestamp(df_1min["datetime"].max())
        now = pd.Timestamp.now(tz=last_ts.tzinfo) if last_ts.tzinfo is not None else pd.Timestamp.now()
        age_seconds = (now - last_ts).total_seconds()
        if age_seconds > 900:
            logger.warning(
                "[AutoMarketCollector] %s 데이터가 %.1f분 경과(15분 초과, 개장일) — stale 처리",
                symbol, age_seconds / 60,
            )
            return True
    except Exception as exc:
        logger.debug("[AutoMarketCollector] %s staleness 판단 실패: %s", symbol, exc)
    return False


def _mu_holiday_gap_reason() -> str:
    """개장일이 아닐 때 데이터 공백 사유를 분류한다."""
    try:
        from app.market import us_market_data as umd
        status = umd.get_us_market_status()
        if status.get("is_us_holiday"):
            return "US_HOLIDAY"
        if status.get("is_us_weekend"):
            return "WEEKEND"
        if status.get("is_us_early_close"):
            return "EARLY_CLOSE"
    except Exception:
        pass
    return "MARKET_CLOSED"


def _fetch_yfinance_intraday(symbol: str, period: str = "1d", interval: str = "1m") -> Optional[pd.DataFrame]:
    try:
        _configure_yfinance_cache()
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period=period, interval=interval, prepost=True)
        if hist is None or hist.empty:
            return None
        df = hist.reset_index()
        df.columns = [str(col).lower() for col in df.columns]
        dt_col = "datetime" if "datetime" in df.columns else ("date" if "date" in df.columns else df.columns[0])
        df = df.rename(columns={dt_col: "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["source"] = "yfinance"
        df["session"] = df["datetime"].apply(_classify_us_session)
        return df[["datetime", "open", "high", "low", "close", "volume", "source", "session"]]
    except Exception as exc:
        logger.debug("[AutoMarketCollector] yfinance intraday fetch failed(%s): %s", symbol, exc)
        return None


def _fetch_yfinance_daily(symbol: str, period: str = "90d") -> Optional[pd.DataFrame]:
    _configure_yfinance_cache()
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
    if hist is None or hist.empty:
        return None
    df = hist.reset_index()
    df.columns = [str(col).lower() for col in df.columns]
    dt_col = next((col for col in df.columns if "date" in col or "datetime" in col), df.columns[0])
    df = df.rename(columns={dt_col: "datetime"})
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["source"] = "yfinance"
    return df[["datetime", "open", "high", "low", "close", "volume", "source"]]


def _fetch_yfinance_quote(symbol: str) -> Optional[dict]:
    quote = _quote_with_naver_then_yfinance(symbol)
    if quote.get("price") is None:
        return None
    return {
        "current_price": quote.get("price"),
        "premarket_return": None,
        "regular_return": quote.get("return_pct"),
    }


def _fetch_hynix_daily_from_kis(mode: str, n_days: int) -> Optional[pd.DataFrame]:
    import requests as rq

    app_key = os.environ.get(f"KIS_{mode.upper()}_APP_KEY", "")
    app_secret = os.environ.get(f"KIS_{mode.upper()}_APP_SECRET", "")
    if not app_key or not app_secret:
        raise ValueError("KIS 인증 정보 없음")

    from app.trading.kis_client import KISClient

    account_no = os.environ.get("KIS_ACCOUNT_NO", "00000000")
    product_code = os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01")
    client = KISClient(app_key=app_key, app_secret=app_secret, account_no=account_no, product_code=product_code, mode=mode)
    token = client.get_access_token()

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010400",
    }
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=n_days + 30)).strftime("%Y%m%d")
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": "000660",
        "FID_INPUT_DATE_1": start_date,
        "FID_INPUT_DATE_2": end_date,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    response = rq.get(
        f"{client.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
        headers=headers,
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    body = _safe_json(response)
    if body is None:
        raise ValueError("KIS daily response is not JSON")
    rows = body.get("output2") or body.get("output") or []
    records = []
    for row in rows:
        close = _float_or_none(row.get("stck_clpr"))
        if close is None or close <= 0:
            continue
        records.append(
            {
                "date": str(row.get("stck_bsop_date", "")),
                "datetime": pd.to_datetime(str(row.get("stck_bsop_date", "")), format="%Y%m%d", errors="coerce"),
                "open": _float_or_none(row.get("stck_oprc")) or close,
                "high": _float_or_none(row.get("stck_hgpr")) or close,
                "low": _float_or_none(row.get("stck_lwpr")) or close,
                "close": close,
                "volume": int(_float_or_none(row.get("acml_vol")) or 0),
            }
        )
    if not records:
        return None
    df = pd.DataFrame(records).dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    return df


def _fetch_hynix_current_from_kis(mode: str) -> Optional[float]:
    try:
        app_key = os.environ.get(f"KIS_{mode.upper()}_APP_KEY", "")
        app_secret = os.environ.get(f"KIS_{mode.upper()}_APP_SECRET", "")
        if not app_key or not app_secret:
            return None
        from app.trading.kis_client import KISClient

        client = KISClient(
            app_key=app_key,
            app_secret=app_secret,
            account_no=os.environ.get("KIS_ACCOUNT_NO", "00000000"),
            product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01"),
            mode=mode,
        )
        data = client.get_current_price("000660")
        price = auto_fix_hynix_price(_float_or_none((data or {}).get("current_price")))
        ok, _ = validate_hynix_price(price)
        return price if ok else None
    except Exception as exc:
        logger.warning("[HYNIX_PRICE] current_price source=KIS error=%s", exc)
        return None


def _fetch_hynix_current_from_yfinance() -> Optional[float]:
    try:
        _configure_yfinance_cache()
        import yfinance as yf

        hist = yf.Ticker("000660.KS").history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        close = hist["Close"].dropna()
        if close.empty:
            return None
        price = auto_fix_hynix_price(_float_or_none(close.iloc[-1]))
        ok, _ = validate_hynix_price(price)
        return price if ok else None
    except Exception as exc:
        logger.warning("[HYNIX_PRICE] current_price source=yfinance error=%s", exc)
        return None


def _fetch_hynix_current_from_pykrx() -> Optional[float]:
    try:
        from pykrx import stock

        end = datetime.now()
        start = end - timedelta(days=10)
        df = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "000660")
        if df is None or df.empty:
            return None
        close_col = "종가" if "종가" in df.columns else "Close"
        price = auto_fix_hynix_price(_float_or_none(df.iloc[-1][close_col]))
        ok, _ = validate_hynix_price(price)
        return price if ok else None
    except Exception as exc:
        logger.warning("[HYNIX_PRICE] current_price source=pykrx error=%s", exc)
        return None


def _normalize_yf_ohlcv(hist: pd.DataFrame) -> pd.DataFrame:
    df = hist.reset_index()
    df.columns = [str(col).lower() for col in df.columns]
    dt_col = next((col for col in df.columns if "date" in col or "datetime" in col), df.columns[0])
    df = df.rename(columns={dt_col: "datetime"})
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["date"] = df["datetime"].dt.strftime("%Y.%m.%d")
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = None
    return df[["date", "datetime", "open", "high", "low", "close", "volume"]]


def _resample_3min(df_1min: pd.DataFrame) -> pd.DataFrame:
    return _resample_minutes(df_1min, 3)


def _resample_minutes(df_1min: pd.DataFrame, minutes: int) -> pd.DataFrame:
    work = df_1min.copy()
    work["datetime"] = pd.to_datetime(work["datetime"])
    result = (
        work.resample(f"{minutes}min", on="datetime")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["close"])
        .reset_index()
    )
    source = work["source"].iloc[-1] if "source" in work.columns and not work.empty else None
    result["source"] = source or "resampled"
    return result


def _validate_real_candles(df: Optional[pd.DataFrame], source: str) -> tuple[bool, str, Optional[pd.DataFrame]]:
    if df is None or df.empty:
        return False, "unavailable", None
    required = {"datetime", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        return False, f"missing columns: {sorted(missing)}", None
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["datetime", "open", "high", "low", "close"])
    if len(work) < 10:
        return False, f"row count < 10 ({len(work)})", None
    if "price" in work.columns and not {"open", "high", "low", "close"}.issubset(work.columns):
        return False, "price-only quote data", None
    close_span = float(work["close"].max() - work["close"].min())
    close_mean = float(work["close"].mean())
    if close_mean <= 0 or close_span / close_mean < 0.0005:
        return False, "constant close values; synthetic rejected", None
    if work["volume"].fillna(0).sum() <= 0:
        return False, "volume missing or zero", None
    if ((work["open"] == work["high"]) & (work["high"] == work["low"]) & (work["low"] == work["close"])).mean() > 0.95:
        return False, "OHLC values copied from one quote; synthetic rejected", None
    work["source"] = source
    if "session" not in work.columns:
        work["session"] = work["datetime"].apply(_classify_us_session)
    return True, "real candle success", work.reset_index(drop=True)


def _validate_real_daily_candles(df: Optional[pd.DataFrame], source: str) -> tuple[bool, str, Optional[pd.DataFrame]]:
    ok, reason, work = _validate_real_candles(df, source)
    if not ok:
        return ok, reason, work
    if work is None or len(work) < 20:
        return False, f"daily row count < 20 ({0 if work is None else len(work)})", None
    return True, "real candle success", work


def _classify_us_session(ts) -> str:
    try:
        t = pd.Timestamp(ts)
        if t.tzinfo is not None:
            t = t.tz_convert("America/New_York")
        hour_min = t.hour * 60 + t.minute
        if hour_min < 9 * 60 + 30:
            return "premarket"
        if hour_min <= 16 * 60:
            return "regular"
        return "aftermarket"
    except Exception:
        return "regular"


def _float_or_none(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _save_hynix_daily(df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_HYNIX_DAILY_CSV, index=False, encoding="utf-8-sig")
    try:
        LEGACY_HYNIX_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(LEGACY_HYNIX_DIR / "hynix_daily.csv", index=False, encoding="utf-8-sig")
    except Exception:
        pass


def _load_hynix_daily_cache() -> Optional[pd.DataFrame]:
    if not _fresh_cache(_HYNIX_DAILY_CSV):
        return None
    try:
        df = pd.read_csv(_HYNIX_DAILY_CSV)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df
    except Exception:
        return None


def _save_hynix_current(price: float, source: str) -> None:
    _write_json_cache(_HYNIX_CURRENT_JSON, {"current_price": float(price), "source": source})


def _load_hynix_current_cache() -> Optional[dict]:
    return _read_json_cache(_HYNIX_CURRENT_JSON)


def _save_hynix_minute(df: pd.DataFrame) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(_HYNIX_MINUTE_CSV, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def _load_hynix_minute_cache() -> Optional[pd.DataFrame]:
    if not _fresh_cache(_HYNIX_MINUTE_CSV, max_hours=1.0):
        return None
    try:
        df = pd.read_csv(_HYNIX_MINUTE_CSV)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"])
    except Exception:
        return None


def _save_mu_1min(df: pd.DataFrame) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(_MU_1MIN_CSV, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def _load_mu_1min_cache() -> Optional[pd.DataFrame]:
    if not _fresh_cache(_MU_1MIN_CSV):
        return None
    try:
        df = pd.read_csv(_MU_1MIN_CSV)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"])
    except Exception:
        return None


def _save_global_quote(symbol: str, price: Optional[float], return_pct: Optional[float], source: Optional[str]) -> None:
    if price is None and return_pct is None:
        return
    payload = _read_json_cache(_GLOBAL_QUOTES_JSON) or {}
    payload[symbol.upper()] = {
        "price": price,
        "return_pct": return_pct,
        "source": source,
        "updated_at": datetime.now().isoformat(),
    }
    _write_json_cache(_GLOBAL_QUOTES_JSON, payload)


def _load_global_quote(symbol: str) -> Optional[dict]:
    payload = _read_json_cache(_GLOBAL_QUOTES_JSON)
    if not payload:
        return None
    return payload.get(symbol.upper())


def _load_complete_index_cache() -> Optional[dict]:
    qqq = _load_global_quote("QQQ")
    soxx = _load_global_quote("SOXX")
    usdkrw = _load_global_quote("USDKRW")
    if not (qqq and soxx and usdkrw):
        return None
    if any(item.get("return_pct") is None for item in (qqq, soxx, usdkrw)):
        return None
    return {
        "qqq_return": qqq.get("return_pct"),
        "sox_return": soxx.get("return_pct"),
        "usdkrw_change": usdkrw.get("return_pct"),
        "error": None,
    }
