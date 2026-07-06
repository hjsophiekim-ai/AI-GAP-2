"""Naver Finance helper for global stocks with yfinance fallback."""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

NAVER_WORLD_SYMBOLS = {
    "MU": "MU.O",
    "NVDA": "NVDA.O",
    "QQQ": "QQQ.O",
    "SOXX": "SOXX.O",
}

YFINANCE_SYMBOLS = {
    "MU": "MU",
    "NVDA": "NVDA",
    "QQQ": "QQQ",
    "SOXX": "SOXX",
    "SOX": "^SOX",
    "USDKRW": "USDKRW=X",
    "USD/KRW": "USDKRW=X",
}


def _decode_response(response: requests.Response) -> str:
    response.encoding = "euc-kr"
    text = response.text
    if not text or "\ufffd" in text[:500]:
        response.encoding = response.apparent_encoding or "euc-kr"
        text = response.text
    return text


def _parse_float(raw: object) -> Optional[float]:
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(raw))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def search_naver_finance_keyword(keyword: str) -> list[dict]:
    """Search Naver Finance autocomplete."""
    try:
        url = (
            "https://ac.finance.naver.com/ac"
            f"?q={quote_plus(keyword)}&q_enc=UTF-8&st=111&sug_num=10"
        )
        response = requests.get(url, headers=HEADERS, timeout=7)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.debug("Naver finance search failed for %s: %s", keyword, exc)
        return []

    results: list[dict] = []
    for group in payload.get("items", []):
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, list) or not item:
                continue
            results.append(
                {
                    "name": item[0] if len(item) > 0 else None,
                    "code": item[1] if len(item) > 1 else None,
                    "market": item[2] if len(item) > 2 else None,
                    "raw": item,
                }
            )
    return results


def _fetch_from_naver_world(symbol: str) -> Optional[dict]:
    naver_symbol = NAVER_WORLD_SYMBOLS.get(symbol.upper())
    if not naver_symbol:
        return None

    try:
        url = f"https://finance.naver.com/world/sise.naver?symbol={naver_symbol}"
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        text = _decode_response(response)
    except Exception as exc:
        logger.debug("Naver world fetch failed for %s: %s", symbol, exc)
        return None

    price = None
    for pattern in (
        r'class="no_today"[^>]*>.*?<span[^>]+class="blind"[^>]*>([\d,.]+)</span>',
        r'<p[^>]+class="no_today"[^>]*>.*?<em[^>]*>([\d,.]+)</em>',
        r'"now"\s*:\s*"?([\d,.]+)',
    ):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            price = _parse_float(match.group(1))
            if price and price > 0:
                break

    return_pct = None
    for pattern in (
        r'class="rate_info"[^>]*>.*?<span[^>]+class="blind"[^>]*>([-+\d.]+)</span>\s*%',
        r'"rate"\s*:\s*"?([-+\d.]+)',
    ):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return_pct = _parse_float(match.group(1))
            break

    if price and price > 0:
        return {"price": float(price), "return_pct": return_pct}
    return None


def _fetch_from_yfinance(symbol: str) -> Optional[dict]:
    yf_symbol = YFINANCE_SYMBOLS.get(symbol.upper(), symbol)
    try:
        import yfinance as yf

        ticker = yf.Ticker(yf_symbol)
        info = ticker.fast_info
        price = _parse_float(_fast_info_value(info, "last_price"))
        prev = _parse_float(_fast_info_value(info, "previous_close"))
        if price is None:
            hist = ticker.history(period="5d", interval="1d", auto_adjust=True)
            if not hist.empty:
                closes = hist["Close"].dropna()
                price = float(closes.iloc[-1]) if len(closes) else None
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else prev
        if price is None or price <= 0:
            return None
        return_pct = round((price / prev - 1) * 100, 2) if prev and prev > 0 else None
        return {"price": float(price), "return_pct": return_pct}
    except Exception as exc:
        logger.debug("yfinance quote failed for %s: %s", symbol, exc)
        return None


def _fast_info_value(info: object, key: str) -> object:
    value = getattr(info, key, None)
    if value is not None:
        return value
    getter = getattr(info, "get", None)
    if callable(getter):
        return getter(key)
    return None


def fetch_naver_global_quote(symbol: str) -> dict:
    """Fetch global quote from Naver first, then yfinance."""
    normalized = symbol.upper()
    result = {
        "symbol": normalized,
        "price": None,
        "return_pct": None,
        "source": "failed",
        "status": "failed",
        "error": None,
    }
    errors: list[str] = []

    naver_data = _fetch_from_naver_world(normalized)
    if naver_data and naver_data.get("price") is not None:
        result.update(naver_data)
        result.update(source="naver_global", status="success")
        return result
    errors.append("naver_global_no_data")

    yf_data = _fetch_from_yfinance(normalized)
    if yf_data and yf_data.get("price") is not None:
        result.update(yf_data)
        result.update(source="yfinance", status="success")
        return result
    errors.append("yfinance_no_data")

    result["error"] = " | ".join(errors)
    return result
