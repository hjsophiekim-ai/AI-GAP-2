"""
naver_volume_spike_collector.py

네이버 거래량 급증 종목 수집기.
URL: https://finance.naver.com/sise/sise_quant_high.naver

페이지 테이블 컬럼 (type_2, 7컬럼):
  [0] 종목명   - 링크(href=code=XXXXXX)
  [1] 현재가
  [2] 전일비
  [3] 등락률(%)
  [4] 거래량
  [5] 전일거래량비(배)
  [6] 거래대금(백만원)
"""

import re
import time
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app.logger import logger

VOLUME_SPIKE_URL = "https://finance.naver.com/sise/sise_quant_high.naver"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

_ETF_ETN_KEYWORDS = [
    "KODEX", "TIGER", "ACE", "SOL", "HANARO", "KBSTAR", "ARIRANG", "PLUS",
    "ETN", "ETF", "레버리지", "인버스", "선물", "합성", "TR",
    "RISE", "FOCUS", "TREX", "TIMEFOLIO", "WOORI", "KOSEF",
]


def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _fetch(url: str, session: requests.Session, timeout: int = 15) -> Optional[BeautifulSoup]:
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = "cp949"
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        logger.warning("[VolumeSpikeCollector] HTTP 오류: %s — %s", url, exc)
        return None


def _safe_float(text: str) -> float:
    if not text:
        return 0.0
    try:
        cleaned = re.sub(r"[^0-9.\-+]", "", text)
        return float(cleaned) if cleaned and cleaned not in ("-", "+", ".") else 0.0
    except (ValueError, AttributeError):
        return 0.0


def _safe_int(text: str) -> int:
    try:
        cleaned = re.sub(r"[^0-9]", "", text)
        return int(cleaned) if cleaned else 0
    except (ValueError, AttributeError):
        return 0


def _detect_type(name: str) -> dict:
    name_u = name.upper()
    is_etf = any(kw.upper() in name_u for kw in _ETF_ETN_KEYWORDS)
    is_etn = "ETN" in name_u
    is_preferred = bool(re.search(r"(?:우B?|\d+우B?)$", name))
    is_spac = "스팩" in name or bool(re.search(r"\d+호스팩", name))
    is_reit = "리츠" in name or "reits" in name.lower()
    return {
        "is_etf": is_etf,
        "is_etn": is_etn,
        "is_preferred": is_preferred,
        "is_spac": is_spac,
        "is_reit": is_reit,
    }


def _parse_row(row, date_str: str = "") -> Optional[dict]:
    """
    sise_quant_high.naver <tr> 한 행 파싱.

    실제 테이블 컬럼 구조 (11컬럼):
      col[0]: 순위
      col[1]: 전일거래량비(배)
      col[2]: 종목명 (a 태그 포함)
      col[3]: 현재가
      col[4]: 전일비
      col[5]: 등락률(%)
      col[6]: 시가
      col[7]: 고가
      col[8]: 거래량
      col[9]: 거래대금(백만원)
      col[10]: 전일거래량
    """
    try:
        name_tag = row.find("a")
        if not name_tag:
            return None
        name = name_tag.get_text(strip=True)
        href = name_tag.get("href", "")
        m = re.search(r"code=(\d{6})", href)
        if not m or not name:
            return None
        symbol = m.group(1)

        cols = row.find_all("td")
        if len(cols) < 10:
            return None

        def txt(i: int) -> str:
            return cols[i].get_text(strip=True) if i < len(cols) else ""

        current_price = _safe_float(txt(3))
        if current_price <= 0:
            return None

        change_rate = _safe_float(txt(5))  # 등락률(%) — "+30.00%" → 30.0
        volume = _safe_int(txt(8))          # 거래량

        # 거래대금: col[9] = 백만원 단위
        trade_value = _safe_float(txt(9)) * 1_000_000

        type_flags = _detect_type(name)

        return {
            "symbol": symbol,
            "name": name,
            "current_price": current_price,
            "change_rate": change_rate,
            "volume": volume,
            "trade_value": trade_value,
            "date": date_str or datetime.now().strftime("%Y%m%d"),
            **type_flags,
        }
    except Exception as exc:
        logger.debug("[VolumeSpikeCollector] 행 파싱 오류: %s", exc)
        return None


def collect_volume_spike_stocks(
    max_pages: int = 3,
    max_stocks: int = 80,
    delay: float = 0.5,
) -> list[dict]:
    """
    네이버 거래량 급증 종목을 수집해 raw dict 리스트로 반환.
    각 dict에는 symbol, name, current_price, change_rate, volume, trade_value,
    is_etf, is_etn, is_preferred, is_spac, is_reit 포함.
    """
    session = _get_session()
    date_str = datetime.now().strftime("%Y%m%d")
    results: list[dict] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        url = f"{VOLUME_SPIKE_URL}?page={page}"
        soup = _fetch(url, session)
        if soup is None:
            break

        tables = soup.find_all("table")
        rows_found = False
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                if len(results) >= max_stocks:
                    break
                parsed = _parse_row(row, date_str)
                if parsed and parsed["symbol"] not in seen:
                    seen.add(parsed["symbol"])
                    results.append(parsed)
                    rows_found = True

        if not rows_found or len(results) >= max_stocks:
            break
        if page < max_pages:
            time.sleep(delay)

    logger.info("[VolumeSpikeCollector] 수집 완료: %d개 (최대 %d페이지)", len(results), max_pages)
    return results
