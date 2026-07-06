"""
Naver Finance item_gap.naver 갭상승 종목 수집기.

https://finance.naver.com/sise/item_gap.naver
컬럼 구조 (11컬럼, table class="type_5"):
  col[0]  N         - 순위
  col[1]  종목명    - 종목명 + <a href="/item/main.naver?code=XXXXXX">
  col[2]  현재가    - 현재가 (원)
  col[3]  전일비    - 전일비 ("상한가4,570" / "1,230" / "-500" 형태)
  col[4]  등락률    - 등락률 ("+29.99%" / "-3.01%")
  col[5]  거래량    - 거래량 (주)
  col[6]  시가      - 시가 (원) ← 갭 계산에 사용
  col[7]  고가      - 고가 (원)
  col[8]  저가      - 저가 (원)
  col[9]  PER       - 무시
  col[10] ROE       - 무시
"""

import re
import time
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app.logger import logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAP_URL = "https://finance.naver.com/sise/item_gap.naver"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_pre_market() -> bool:
    return datetime.now().hour < 9


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
        logger.warning(f"[NaverGapCollector] HTTP 오류: {url} — {exc}")
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


def detect_stock_type(name: str, symbol: str) -> dict:
    result = {
        "is_etf": False,
        "is_etn": False,
        "is_preferred": False,
        "is_spac": False,
        "is_reit": False,
    }
    etf_kws = ["KODEX", "TIGER", "ACE", "SOL", "PLUS", "KBSTAR", "KOSEF",
               "HANARO", "ARIRANG", "ETF", "레버리지", "인버스", "선물", "합성", "TR"]
    name_u = name.upper()
    for kw in etf_kws:
        if kw.upper() in name_u:
            result["is_etf"] = True
            break
    if "ETN" in name_u:
        result["is_etn"] = True
    if re.search(r"(?:우B?|\d+우B?)$", name):
        result["is_preferred"] = True
    if "스팩" in name:
        result["is_spac"] = True
    if "리츠" in name:
        result["is_reit"] = True
    return result


# ---------------------------------------------------------------------------
# Row parser  (item_gap.naver 전용, 11컬럼)
# ---------------------------------------------------------------------------

def _parse_stock_row(row, date_str: str = "") -> Optional[dict]:
    """
    item_gap.naver 의 <tr> 행을 파싱해 dict 반환.
    파싱 실패 시 None.
    """
    try:
        cols = row.find_all("td")
        if len(cols) < 9:
            return None

        # --- 종목코드 / 종목명 ---
        name_tag = row.find("a")
        if not name_tag:
            return None
        name = name_tag.get_text(strip=True)
        href = name_tag.get("href", "")
        sym_m = re.search(r"code=(\d+)", href)
        if not sym_m:
            return None
        symbol = sym_m.group(1)
        if not name or not symbol:
            return None

        def txt(i: int) -> str:
            return cols[i].get_text(strip=True) if i < len(cols) else ""

        # col[2] 현재가
        current_price = _safe_float(txt(2))
        if current_price <= 0:
            return None

        # col[4] 등락률 ("+29.99%" → 29.99)
        change_rate = _safe_float(txt(4))

        # col[5] 거래량
        volume = _safe_int(txt(5))

        # col[6] 시가
        open_price = _safe_float(txt(6))

        # col[7] 고가
        high = _safe_float(txt(7))

        # col[8] 저가
        low = _safe_float(txt(8))

        # 전일종가 역산: prev_close = current_price / (1 + change_rate/100)
        if change_rate != -100 and abs(change_rate) < 500:
            previous_close = current_price / (1.0 + change_rate / 100.0)
        else:
            previous_close = current_price

        # 갭률: (시가 - 전일종가) / 전일종가 * 100
        if previous_close > 0 and open_price > 0:
            gap_rate = (open_price - previous_close) / previous_close * 100.0
        else:
            gap_rate = 0.0

        # 거래대금 (원)
        trade_value = float(current_price) * float(volume)

        stock_types = detect_stock_type(name, symbol)
        now = datetime.now()
        date_out = date_str or now.strftime("%Y-%m-%d")

        return {
            "symbol":         symbol,
            "name":           name,
            "market":         "",
            "previous_close": round(previous_close, 2),
            "open":           open_price,
            "high":           high,
            "low":            low,
            "current_price":  current_price,
            "volume":         volume,
            "trade_value":    trade_value,
            "change_rate":    change_rate,
            "gap_rate":       round(gap_rate, 2),
            "sector":         "",
            "is_etf":         stock_types["is_etf"],
            "is_etn":         stock_types["is_etn"],
            "is_preferred":   stock_types["is_preferred"],
            "is_spac":        stock_types["is_spac"],
            "is_reit":        stock_types["is_reit"],
            "is_warning":     False,
            "is_halt":        False,
            "source":         "naver",
            "date":           date_out,
            "time":           now.strftime("%H:%M:%S"),
        }
    except Exception as exc:
        logger.debug(f"[NaverGapCollector] 행 파싱 실패: {exc}")
        return None


# ---------------------------------------------------------------------------
# Table extractor
# ---------------------------------------------------------------------------

def _extract_rows(soup: BeautifulSoup) -> list:
    """item_gap.naver 의 type_5 테이블에서 데이터 행 추출."""
    try:
        # item_gap.naver 는 type_5 클래스 사용
        table = soup.find("table", class_="type_5")
        if table is None:
            # fallback: 가장 큰 테이블
            tables = soup.find_all("table")
            for t in tables:
                if len(t.find_all("tr")) > 5:
                    table = t
                    break

        if table is None:
            logger.warning("[NaverGapCollector] 테이블을 찾을 수 없습니다.")
            return []

        rows = table.find_all("tr")
        data_rows = [
            r for r in rows
            if r.find("a") and "code=" in (r.find("a").get("href") or "")
        ]
        return data_rows
    except Exception as exc:
        logger.warning(f"[NaverGapCollector] 테이블 추출 실패: {exc}")
        return []


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------

class NaverGapCollector:
    """네이버 증권 갭상승 종목 수집기 (item_gap.naver)."""

    GAP_URL = GAP_URL

    def __init__(self):
        self._session = _get_session()

    def collect_gap_stocks(self, date_str: str = None) -> list[dict]:
        """갭상승 종목 수집. 실패 시 빈 리스트 반환."""
        logger.info(f"[NaverGapCollector] 갭상승 수집: {self.GAP_URL}")
        soup = _fetch(self.GAP_URL, self._session)
        if soup is None:
            logger.warning("[NaverGapCollector] 페이지 로드 실패")
            return []

        rows = _extract_rows(soup)
        logger.info(f"[NaverGapCollector] 데이터 행 {len(rows)}개 발견")

        results = []
        for row in rows:
            parsed = _parse_stock_row(row, date_str)
            if parsed:
                results.append(parsed)

        logger.info(f"[NaverGapCollector] 갭상승 {len(results)}개 파싱 완료")
        return results

    def collect_all(self, date_str: str = None) -> list[dict]:
        """collect_gap_stocks의 alias (DataCollector 호환성)."""
        return self.collect_gap_stocks(date_str)

    def get_gap_candidates(self, date_str: str = None) -> list[dict]:
        """collect_gap_stocks의 alias (레거시 호환성)."""
        return self.collect_gap_stocks(date_str)

    def collect_volume_top_stocks(self, date_str: str = None) -> list[dict]:
        """갭상승 목록에서 거래대금 상위 종목. item_gap.naver 에서 파생."""
        all_stocks = self.collect_gap_stocks(date_str)
        return sorted(all_stocks, key=lambda s: s.get("trade_value", 0), reverse=True)
