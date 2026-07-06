"""
naver_market_collector.py

네이버증권에서 지수/환율 레벨 데이터를 수집한다 (KOSPI, KOSDAQ, KOSPI200,
원/달러 환율). 모든 함수는 실패 시 예외를 던지지 않고
{"success": False, "error": ...} 형태로 반환한다.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import requests

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
_TIMEOUT = 8

_INDEX_CODES = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "KOSPI200": "KPI200"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_float(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-+]", "", text.replace(",", ""))
    if cleaned in ("", "-", "+", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _decode(resp: requests.Response) -> str:
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            resp.encoding = enc
            text = resp.text
            if text and "�" not in text[:1000]:
                return text
        except Exception:
            continue
    return resp.text


def fetch_index_snapshot(index_name: str) -> dict:
    """KOSPI/KOSDAQ/KOSPI200 지수 스냅샷.

    KOSPI200은 선물 데이터가 아닌 현물지수를 근사치로 사용한다
    (KIS 지수선물 시세 TR 미보유 — market_data_collector에서 명시적으로
    'kospi200_futures_proxy' 로 라벨링됨).

    Returns
    -------
    dict: value, change_rate, change_amount, high, low,
          advancers, decliners, unchanged, source, timestamp, success, error
    """
    code = _INDEX_CODES.get(index_name.upper(), index_name.upper())
    result = {
        "value": None, "change_rate": None, "change_amount": None,
        "high": None, "low": None,
        "advancers": None, "decliners": None, "unchanged": None,
        "source": "naver", "timestamp": _now_iso(), "success": False, "error": None,
    }
    try:
        url = f"https://finance.naver.com/sise/sise_index.naver?code={code}"
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        html = _decode(resp)

        m = re.search(r'id="now_value"[^>]*>([\d,.]+)', html)
        if not m:
            m = re.search(r'class="num"[^>]*>([\d,.]+)', html)
        value = _safe_float(m.group(1)) if m else None

        m2 = re.search(r'id="change_value_and_rate"[\s\S]{0,300}?([+-]?[\d.]+)%', html)
        change_rate = _safe_float(m2.group(1)) if m2 else None
        if change_rate is not None:
            down = bool(re.search(r'(하락|down|_2)', html[max(0, (m2.start() if m2 else 0) - 200):(m2.start() if m2 else 0)]))
            # 네이버 페이지 마크업은 하락 시 별도 클래스 사용 — 부호 없는 percent만 잡힐 수 있어 보정
            if "하락" in html[max(0, m.start() - 400):m.start()] if m else False:
                change_rate = -abs(change_rate)

        m3 = re.search(r'상승\s*<[^>]*>\s*(\d+)', html)
        advancers = int(m3.group(1)) if m3 else None
        m4 = re.search(r'하락\s*<[^>]*>\s*(\d+)', html)
        decliners = int(m4.group(1)) if m4 else None
        m5 = re.search(r'보합\s*<[^>]*>\s*(\d+)', html)
        unchanged = int(m5.group(1)) if m5 else None

        if value is None:
            result["error"] = "parse_failed"
            return result

        result.update({
            "value": value,
            "change_rate": change_rate,
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "success": True,
        })
        return result
    except Exception as exc:
        logger.warning("[NaverMarketCollector] %s 지수 조회 실패: %s", index_name, exc)
        result["error"] = str(exc)
        return result


def fetch_kospi200_futures_proxy() -> dict:
    """KOSPI200 선물 등락률 근사치 (현물 KOSPI200 지수로 대체).

    실제 선물 시세는 KIS 지수선물 TR이 필요하며 현재 kis_market_collector가
    이를 지원하지 않는다. source 필드에 'kospi200_index_proxy'를 명시한다.
    """
    snap = fetch_index_snapshot("KOSPI200")
    snap["source"] = "kospi200_index_proxy"
    return snap


def fetch_usdkrw() -> dict:
    """원/달러 환율 스냅샷. https://finance.naver.com/marketindex/ 파싱."""
    result = {
        "value": None, "change_rate": None, "source": "naver",
        "timestamp": _now_iso(), "success": False, "error": None,
    }
    try:
        url = "https://finance.naver.com/marketindex/"
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        html = _decode(resp)

        m = re.search(r'USD[\s\S]{0,400}?class="value">([\d,.]+)', html)
        value = _safe_float(m.group(1)) if m else None

        m2 = re.search(r'USD[\s\S]{0,600}?class="change">([\d,.]+)</span>\s*<span[^>]*class="(blind|point_(dn|up))"', html)
        change_amount = _safe_float(m2.group(1)) if m2 else None
        is_down = bool(m2 and "dn" in (m2.group(3) or "") and m2.group(3))
        if value is None:
            result["error"] = "parse_failed"
            return result
        if change_amount is not None and value:
            prev = value - change_amount if not is_down else value + change_amount
            change_rate = (value - prev) / prev * 100 if prev else None
        else:
            change_rate = None
        result.update({"value": value, "change_rate": change_rate, "success": True})
        return result
    except Exception as exc:
        logger.warning("[NaverMarketCollector] 환율 조회 실패: %s", exc)
        result["error"] = str(exc)
        return result
