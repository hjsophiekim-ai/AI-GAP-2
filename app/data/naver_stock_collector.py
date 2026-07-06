"""Naver Finance collector for Korean equities."""

from __future__ import annotations

import logging
import re
from io import StringIO
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

HYNIX_CODE = "000660"
HYNIX_PRICE_MIN = 50_000
HYNIX_PRICE_MAX = 5_000_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _valid_hynix_price(price: object) -> bool:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    return HYNIX_PRICE_MIN <= value <= HYNIX_PRICE_MAX


def _decode_response(response: requests.Response) -> str:
    response.encoding = "euc-kr"
    text = response.text
    if not text or "\ufffd" in text[:500]:
        response.encoding = response.apparent_encoding or "euc-kr"
        text = response.text
    return text


def _to_number(value: object) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_naver_current_price(code: str = HYNIX_CODE) -> dict:
    """Fetch current price from Naver Finance."""
    result = {
        "code": code,
        "current_price": None,
        "source": "naver",
        "status": "failed",
        "error": None,
    }

    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        text = _decode_response(response)

        price: Optional[float] = None
        patterns = [
            r'<p[^>]+class="no_today"[^>]*>.*?<span[^>]+class="blind"[^>]*>([\d,]+)</span>',
            r'id="_nowVal"[^>]*>([\d,]+)',
            r"현재가\s*</th>\s*<td[^>]*>\s*<em[^>]*>\s*<span[^>]*>([\d,]+)</span>",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if not match:
                continue
            parsed = _to_number(match.group(1))
            if parsed is not None:
                price = parsed
                break

        if price is None:
            daily = fetch_naver_daily_ohlcv(code, pages=1)
            if daily is not None and not daily.empty:
                price = float(daily.iloc[-1]["close"])

        if not _valid_hynix_price(price):
            result["error"] = "invalid_price"
            return result

        result.update(current_price=float(price), status="success")
        return result
    except Exception as exc:
        logger.warning("Naver current price failed for %s: %s", code, exc)
        result["error"] = str(exc)
        return result


def fetch_hynix_current_price() -> dict:
    """Convenience wrapper for SK Hynix."""
    return fetch_naver_current_price(HYNIX_CODE)


def fetch_naver_daily_ohlcv(code: str = HYNIX_CODE, pages: int = 3) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV rows from Naver Finance.

    Returns a DataFrame with date, datetime, open, high, low, close, volume.
    """
    records: list[dict] = []

    for page in range(1, pages + 1):
        try:
            url = f"https://finance.naver.com/item/sise_day.naver?code={code}&page={page}"
            response = requests.get(url, headers=HEADERS, timeout=10)
            response.raise_for_status()
            html = _decode_response(response)
            tables = pd.read_html(StringIO(html), header=0)
        except Exception as exc:
            logger.debug("Naver daily page %s failed for %s: %s", page, code, exc)
            continue

        for table in tables:
            if table.empty:
                continue
            table = table.dropna(how="all")
            table.columns = [str(col).strip() for col in table.columns]

            col_map: dict[str, str] = {}
            for col in table.columns:
                name = str(col).strip()
                if name in ("날짜", "일자"):
                    col_map[col] = "date"
                elif name == "종가":
                    col_map[col] = "close"
                elif name == "시가":
                    col_map[col] = "open"
                elif name == "고가":
                    col_map[col] = "high"
                elif name == "저가":
                    col_map[col] = "low"
                elif name == "거래량":
                    col_map[col] = "volume"

            normalized = table.rename(columns=col_map)
            if not {"date", "close"}.issubset(normalized.columns):
                continue

            for _, row in normalized.iterrows():
                date_text = str(row.get("date", "")).strip()
                if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", date_text):
                    continue

                close = _to_number(row.get("close"))
                if not _valid_hynix_price(close):
                    continue

                open_price = _to_number(row.get("open")) or close
                high = _to_number(row.get("high")) or close
                low = _to_number(row.get("low")) or close
                volume = _to_number(row.get("volume")) or 0

                records.append(
                    {
                        "date": date_text,
                        "datetime": pd.to_datetime(date_text, format="%Y.%m.%d", errors="coerce"),
                        "open": float(open_price),
                        "high": float(high),
                        "low": float(low),
                        "close": float(close),
                        "volume": int(volume),
                    }
                )

    if not records:
        return None

    df = pd.DataFrame(records).dropna(subset=["datetime"])
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    return df[["date", "datetime", "open", "high", "low", "close", "volume"]] if not df.empty else None


def fetch_hynix_daily_ohlcv(pages: int = 3) -> Optional[pd.DataFrame]:
    """Convenience wrapper for SK Hynix daily candles."""
    return fetch_naver_daily_ohlcv(HYNIX_CODE, pages=pages)
