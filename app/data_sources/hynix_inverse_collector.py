"""
hynix_inverse_collector.py — SOL SK하이닉스선물단일종목인버스2X(0197X0) 가격 수집.

기존 하이닉스(000660) 수집기(auto_market_collector.collect_hynix_daily/minute)와
동일한 우선순위(KIS → Naver → 캐시)를 따르되, 인버스 ETN의 가격대(수천~수만원)에
맞는 별도 유효범위 검증을 사용한다. 실패해도 예외를 던지지 않고 최근 캐시값 +
stale=True 플래그로 대체한다.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from app.logger import logger
from app.utils.data_paths import CACHE_DIR

INVERSE_SYMBOL = "0197X0"
INVERSE_NAME = "SOL SK하이닉스선물단일종목인버스2X"

INVERSE_PRICE_MIN = 500
INVERSE_PRICE_MAX = 200_000

ROOT = Path(__file__).resolve().parent.parent.parent
_CURRENT_JSON = CACHE_DIR / "hynix_inverse_current.json"
_MINUTE_CSV = CACHE_DIR / "hynix_inverse_minute_1m.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _kis_mode() -> Optional[str]:
    for candidate in ("real", "mock"):
        if os.environ.get(f"KIS_{candidate.upper()}_APP_KEY") and os.environ.get(f"KIS_{candidate.upper()}_APP_SECRET"):
            return candidate
    return None


def _valid_inverse_price(price: object) -> bool:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    return INVERSE_PRICE_MIN <= value <= INVERSE_PRICE_MAX


def _decode_response(response: requests.Response) -> str:
    response.encoding = "euc-kr"
    text = response.text
    if not text or "�" in text[:500]:
        response.encoding = response.apparent_encoding or "euc-kr"
        text = response.text
    return text


def _to_number(value: object) -> Optional[float]:
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fetch_naver_inverse_current(code: str = INVERSE_SYMBOL) -> dict:
    """Naver Finance에서 인버스 ETN 현재가 조회 (하이닉스 전용 가격범위 검증 회피)."""
    result = {"code": code, "current_price": None, "source": "naver", "status": "failed", "error": None}
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        text = _decode_response(response)

        price: Optional[float] = None
        patterns = [
            r'<p[^>]+class="no_today"[^>]*>.*?<span[^>]+class="blind"[^>]*>([\d,]+)</span>',
            r'id="_nowVal"[^>]*>([\d,]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if not match:
                continue
            parsed = _to_number(match.group(1))
            if parsed is not None:
                price = parsed
                break

        if not _valid_inverse_price(price):
            result["error"] = "invalid_price"
            return result

        result.update(current_price=float(price), status="success")
        return result
    except Exception as exc:
        logger.debug("[HynixInverse] Naver 현재가 수집 실패: %s", exc)
        result["error"] = str(exc)
        return result


def _write_json_cache(payload: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        import json

        payload = dict(payload)
        payload["cached_at"] = datetime.now().isoformat()
        _CURRENT_JSON.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception as exc:
        logger.debug("[HynixInverse] 현재가 캐시 저장 실패: %s", exc)


def _read_json_cache() -> Optional[dict]:
    try:
        if not _CURRENT_JSON.exists():
            return None
        import json

        return json.loads(_CURRENT_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None


def collect_inverse_current(mode: Optional[str] = None) -> dict:
    """인버스(0197X0) 현재가 수집. 우선순위: KIS → Naver → 최근 캐시(stale=True)."""
    mode = mode or _kis_mode()
    result = {
        "symbol": INVERSE_SYMBOL, "name": INVERSE_NAME,
        "current_price": None, "prev_close": None, "open": None, "high": None, "low": None,
        "volume": None, "change_rate": None,
        "source": None, "status": "unavailable", "stale": False, "error": None,
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
            quote = client.get_current_price(INVERSE_SYMBOL)
            if quote and _valid_inverse_price(quote.get("current_price")):
                result.update(
                    current_price=quote["current_price"], prev_close=quote.get("prev_close"),
                    open=quote.get("open"), high=quote.get("high"), low=quote.get("low"),
                    volume=quote.get("volume"), change_rate=quote.get("change_rate"),
                    source="kis", status="success",
                )
                _write_json_cache(result)
                return result
            result["error"] = "KIS 현재가 응답 없음/유효범위 밖"
        except Exception as exc:
            result["error"] = f"KIS 인버스 현재가 실패: {exc}"
    else:
        result["error"] = "KIS 인증 정보 없음 (mock/real 모두 미설정)"

    naver = _fetch_naver_inverse_current()
    if naver.get("status") == "success":
        result.update(current_price=naver["current_price"], source="naver", status="success", error=None)
        _write_json_cache(result)
        return result
    result["error"] = (result.get("error") or "") + f" | Naver 실패: {naver.get('error')}"

    cached = _read_json_cache()
    if cached and cached.get("current_price"):
        result.update(cached)
        result["source"] = "cache"
        result["status"] = "stale_cache"
        result["stale"] = True
        result["error"] = (result.get("error") or "") + " | 실시간 수집 실패, 캐시 사용(가격 갱신 실패)"
    return result


def _save_inverse_minute(df: pd.DataFrame) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(_MINUTE_CSV, index=False, encoding="utf-8-sig")
    except Exception as exc:
        logger.debug("[HynixInverse] 분봉 캐시 저장 실패: %s", exc)


def _load_inverse_minute_cache() -> Optional[pd.DataFrame]:
    try:
        if not _MINUTE_CSV.exists():
            return None
        age_hours = (datetime.now().timestamp() - _MINUTE_CSV.stat().st_mtime) / 3600.0
        if age_hours > 1.0:
            return None
        df = pd.read_csv(_MINUTE_CSV)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"])
    except Exception:
        return None


def collect_inverse_minute(mode: Optional[str] = None, count: int = 60) -> dict:
    """인버스(0197X0) 1분봉 수집. KIS만 지원(국내 분봉은 JS 렌더링 필요해 Naver 폴백 없음)."""
    mode = mode or _kis_mode()
    result = {
        "df_1min": None, "source": None, "error": None, "status": "unavailable",
        "last_bar_time": None, "stale": False,
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
            candles = client.get_minute_candles(INVERSE_SYMBOL, period_min=1, count=count)
            if candles:
                df_1min = pd.DataFrame(candles)
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
                    _save_inverse_minute(df_1min)
                    result.update(
                        df_1min=df_1min, source="kis", status="success",
                        last_bar_time=df_1min["datetime"].iloc[-1].isoformat(),
                    )
                    return result
            result["error"] = "KIS 인버스 분봉 응답 없음"
        except Exception as exc:
            result["error"] = f"KIS 인버스 분봉 실패: {exc}"
    else:
        result["error"] = "KIS 인증 정보 없음 (mock/real 모두 미설정)"

    cached = _load_inverse_minute_cache()
    if cached is not None and not cached.empty:
        result.update(
            df_1min=cached, source="cache", status="stale_cache", stale=True,
            last_bar_time=cached["datetime"].iloc[-1].isoformat(),
        )
        result["error"] = (result.get("error") or "") + " | 실시간 분봉 수집 실패, 캐시 사용(가격 갱신 실패)"
    return result
