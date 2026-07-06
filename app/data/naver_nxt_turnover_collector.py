"""
naver_nxt_turnover_collector.py

네이버 거래대금 상위 종목 수집기.
1차 URL: https://finance.naver.com/sise/sise_quant.naver  (거래대금 상위)
fallback: https://finance.naver.com/sise/sise_quant_high.naver  (거래량급증)

수집 항목:
  rank, symbol, name, current_price, change_rate, volume, trading_value,
  market, collected_at, source_url,
  is_etf, is_etn, is_preferred, is_spac, is_reit

인코딩 순차 시도: cp949 → euc-kr → utf-8
timeout: 8초
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# ── URL 설정 ──────────────────────────────────────────────────────────────────
_NXT_URL = "https://finance.naver.com/sise/nxt_sise_quant.naver"
_FALLBACK_URL = "https://finance.naver.com/sise/sise_quant_high.naver"
_TIMEOUT = 8

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

_ENCODINGS = ["cp949", "euc-kr", "utf-8"]

_ETF_ETN_KEYWORDS = [
    "KODEX", "TIGER", "ACE", "SOL", "HANARO", "KBSTAR", "ARIRANG", "PLUS",
    "ETN", "ETF", "레버리지", "인버스", "선물", "합성", "TR",
    "RISE", "FOCUS", "TREX", "TIMEFOLIO", "WOORI", "KOSEF",
]


def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _fetch_with_encoding(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    """URL 요청 후 인코딩 순차 시도(cp949→euc-kr→utf-8)."""
    try:
        resp = session.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("[NxtTurnoverCollector] HTTP 오류: %s — %s", url, exc)
        return None

    for enc in _ENCODINGS:
        try:
            resp.encoding = enc
            text = resp.text
            soup = BeautifulSoup(text, "html.parser")
            # 최소 파싱 검증: 종목 링크가 있는지
            if soup.find("a", href=re.compile(r"code=\d{6}")):
                return soup
        except Exception:
            continue
    logger.warning("[NxtTurnoverCollector] 모든 인코딩 실패: %s", url)
    return None


def _safe_float(text: str) -> float:
    if not text:
        return 0.0
    try:
        cleaned = re.sub(r"[^0-9.\-+]", "", text.replace(",", ""))
        return float(cleaned) if cleaned not in ("", "-", "+", ".") else 0.0
    except (ValueError, AttributeError):
        return 0.0


def _safe_int(text: str) -> int:
    try:
        cleaned = re.sub(r"[^0-9]", "", text.replace(",", ""))
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


def _parse_nxt_row(row, rank_counter: list, date_str: str, source_url: str) -> Optional[dict]:
    """
    거래대금 상위(sise_quant.naver) 테이블 행 파싱.

    실제 컬럼 구조 (10컬럼):
      [0] 순위
      [1] 종목명 (a 태그)
      [2] 현재가
      [3] 전일비
      [4] 등락률(%)
      [5] 거래량
      [6] 거래대금(백만원)
      [7] 전일거래량
      [8] 시가
      [9] 고가
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
        if len(cols) < 6:
            return None

        def txt(i: int) -> str:
            return cols[i].get_text(strip=True) if i < len(cols) else ""

        # 현재가
        current_price = _safe_float(txt(2))
        if current_price <= 0:
            return None

        # 등락률
        change_rate = _safe_float(txt(4))

        # 거래량
        volume = _safe_int(txt(5))

        # 거래대금: 백만원 단위
        trading_value_raw = _safe_float(txt(6))
        trading_value = trading_value_raw * 1_000_000

        # 순위
        rank_counter[0] += 1
        rank = rank_counter[0]

        type_flags = _detect_type(name)

        return {
            "rank": rank,
            "symbol": symbol,
            "name": name,
            "current_price": current_price,
            "change_rate": change_rate,
            "volume": volume,
            "trading_value": trading_value,
            "market": "",        # 네이버 거래대금 페이지는 시장 구분 없음
            "collected_at": date_str,
            "source_url": source_url,
            **type_flags,
        }
    except Exception as exc:
        logger.debug("[NxtTurnoverCollector] 행 파싱 오류: %s", exc)
        return None


def _parse_fallback_row(row, rank_counter: list, date_str: str, source_url: str) -> Optional[dict]:
    """
    거래량급증(sise_quant_high.naver) fallback 행 파싱.

    실제 컬럼 구조 (11컬럼):
      [0] 순위
      [1] 전일거래량비(배)
      [2] 종목명
      [3] 현재가
      [4] 전일비
      [5] 등락률
      [6] 시가
      [7] 고가
      [8] 거래량
      [9] 거래대금(백만원)
      [10] 전일거래량
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

        change_rate = _safe_float(txt(5))
        volume = _safe_int(txt(8))
        trading_value = _safe_float(txt(9)) * 1_000_000

        rank_counter[0] += 1
        rank = rank_counter[0]

        type_flags = _detect_type(name)

        return {
            "rank": rank,
            "symbol": symbol,
            "name": name,
            "current_price": current_price,
            "change_rate": change_rate,
            "volume": volume,
            "trading_value": trading_value,
            "market": "",
            "collected_at": date_str,
            "source_url": source_url,
            **type_flags,
        }
    except Exception as exc:
        logger.debug("[NxtTurnoverCollector] fallback 행 파싱 오류: %s", exc)
        return None


class NaverNxtTurnoverCollector:
    """
    네이버 거래대금 상위 페이지 수집기.

    1. sise_quant.naver 파싱 시도
    2. 실패 시 sise_quant_high.naver fallback
    3. 인코딩 cp949 → euc-kr → utf-8 순차 시도
    """

    def __init__(self, primary_url: str = _NXT_URL, fallback_url: str = _FALLBACK_URL):
        self._primary_url = primary_url
        self._fallback_url = fallback_url
        self._used_fallback = False
        self._session = _get_session()

    @property
    def primary_url(self) -> str:
        return self._primary_url

    @property
    def used_fallback(self) -> bool:
        return self._used_fallback

    def collect(
        self,
        max_pages: int = 5,
        max_stocks: int = 100,
        delay: float = 0.4,
    ) -> list[dict]:
        """
        거래대금 상위 종목 수집.

        Returns
        -------
        list[dict] — rank, symbol, name, current_price, change_rate,
                     volume, trading_value, market, collected_at, source_url,
                     is_etf, is_etn, is_preferred, is_spac, is_reit
        """
        self._used_fallback = False
        date_str = datetime.now().strftime("%Y%m%d %H:%M")

        # 1차 시도: sise_quant.naver
        result = self._collect_from(
            base_url=self._primary_url,
            max_pages=max_pages,
            max_stocks=max_stocks,
            delay=delay,
            date_str=date_str,
            row_parser=_parse_nxt_row,
        )

        if not result:
            logger.warning(
                "[NxtTurnoverCollector] 1차 URL 수집 실패 또는 0개 — fallback 시작: %s",
                self._fallback_url,
            )
            self._used_fallback = True
            result = self._collect_from(
                base_url=self._fallback_url,
                max_pages=max_pages,
                max_stocks=max_stocks,
                delay=delay,
                date_str=date_str,
                row_parser=_parse_fallback_row,
            )
            if not result:
                logger.error("[NxtTurnoverCollector] fallback도 실패. 빈 결과 반환.")

        logger.info(
            "[NxtTurnoverCollector] 수집 완료: %d개 (fallback=%s)",
            len(result),
            self._used_fallback,
        )
        return result

    def _collect_from(
        self,
        base_url: str,
        max_pages: int,
        max_stocks: int,
        delay: float,
        date_str: str,
        row_parser,
    ) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        rank_counter = [0]

        for page in range(1, max_pages + 1):
            url = f"{base_url}?page={page}"
            soup = _fetch_with_encoding(url, self._session)
            if soup is None:
                break

            tables = soup.find_all("table")
            found_any = False
            for table in tables:
                rows = table.find_all("tr")
                for row in rows:
                    if len(results) >= max_stocks:
                        break
                    parsed = row_parser(row, rank_counter, date_str, base_url)
                    if parsed and parsed["symbol"] not in seen:
                        seen.add(parsed["symbol"])
                        results.append(parsed)
                        found_any = True

            if not found_any or len(results) >= max_stocks:
                break
            if page < max_pages:
                time.sleep(delay)

        return results


# ── 모듈 수준 편의 함수 ────────────────────────────────────────────────────────

_default_collector: Optional[NaverNxtTurnoverCollector] = None


def collect_nxt_turnover_stocks(
    max_pages: int = 5,
    max_stocks: int = 100,
) -> list[dict]:
    """
    네이버 거래대금 상위 종목 수집 (모듈 수준 편의 함수).

    Returns
    -------
    list[dict]  — NaverNxtTurnoverCollector.collect() 와 동일 구조
    """
    global _default_collector
    if _default_collector is None:
        _default_collector = NaverNxtTurnoverCollector()
    return _default_collector.collect(max_pages=max_pages, max_stocks=max_stocks)
