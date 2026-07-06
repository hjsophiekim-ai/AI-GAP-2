"""
kospilab_scraper.py — 코스피랩(kospilab.com) 해외 참고가 자동 수집 모듈.

requests + BeautifulSoup 정적 파싱 우선 시도.
JavaScript 렌더링이 필요한 경우 Playwright fallback.
파싱 실패 시 앱을 종료하지 않고 결과에 실패 사유 포함.

1회 예측 실행당 1회 호출 + 5분 인메모리 캐시.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Optional

_CACHE: dict = {}
_CACHE_EXPIRY: float = 0.0
_CACHE_TTL_SECONDS = 300  # 5분

TARGET_URL = "https://kospilab.com/"

_HYNIX_CODE = "000660"

_RESULT_TEMPLATE = {
    "hynix_reference_price":  None,
    "hynix_reference_return": None,
    "samsung_reference_return": None,
    "hyundai_reference_return": None,
    "source_status": "failed",
    "error_message": None,
    "collected_at": None,
}


def fetch_kospilab_data(force_refresh: bool = False) -> dict:
    """
    코스피랩에서 SK하이닉스 해외 참고가 수집.

    Parameters
    ----------
    force_refresh : 캐시 무시하고 재수집

    Returns
    -------
    dict
        hynix_reference_price, hynix_reference_return, source_status 등
    """
    global _CACHE, _CACHE_EXPIRY

    if not force_refresh and _CACHE and time.time() < _CACHE_EXPIRY:
        return _CACHE

    result = dict(_RESULT_TEMPLATE)
    result["collected_at"] = datetime.now().isoformat()

    # 1순위: requests + BeautifulSoup
    try:
        res = _try_requests()
        if res["source_status"] == "success":
            _CACHE = res
            _CACHE_EXPIRY = time.time() + _CACHE_TTL_SECONDS
            return res
    except Exception as e:
        result["error_message"] = f"requests 파싱 실패: {e}"

    # 2순위: Playwright
    try:
        res = _try_playwright()
        if res["source_status"] == "success":
            _CACHE = res
            _CACHE_EXPIRY = time.time() + _CACHE_TTL_SECONDS
            return res
        result["error_message"] = (result.get("error_message") or "") + " | playwright: " + (res.get("error_message") or "")
    except ImportError:
        result["error_message"] = (result.get("error_message") or "") + " | playwright 미설치"
    except Exception as e:
        result["error_message"] = (result.get("error_message") or "") + f" | playwright 오류: {e}"

    result["source_status"] = "failed"
    result["collected_at"] = datetime.now().isoformat()
    _CACHE = result
    _CACHE_EXPIRY = time.time() + 60  # 실패 시 1분 캐시
    return result


def _try_requests() -> dict:
    """requests + BeautifulSoup로 정적 HTML 파싱 시도."""
    import requests as rq
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://kospilab.com/",
    }

    resp = rq.get(TARGET_URL, headers=headers, timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    return _parse_soup(soup, source="requests")


def _try_playwright() -> dict:
    """Playwright 렌더링 후 파싱 시도."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(TARGET_URL, wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(3000)
            content = page.content()
        finally:
            browser.close()

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "html.parser")
    return _parse_soup(soup, source="playwright")


def _parse_soup(soup, source: str = "requests") -> dict:
    """
    BeautifulSoup 객체에서 SK하이닉스 참고가 파싱.
    여러 selector 후보를 순서대로 시도합니다.
    """
    result = dict(_RESULT_TEMPLATE)
    result["collected_at"] = datetime.now().isoformat()

    hynix_price: Optional[float] = None
    hynix_ret:   Optional[float] = None
    samsung_ret: Optional[float] = None
    hyundai_ret: Optional[float] = None

    full_text = soup.get_text(separator=" ")

    # ── 후보 selector 1: 종목 카드 (data-code 속성) ──────────────────────────
    selectors_code = [
        f'[data-code="{_HYNIX_CODE}"]',
        f'[data-ticker="{_HYNIX_CODE}"]',
        f'[data-symbol="{_HYNIX_CODE}"]',
        f'[id*="000660"]',
        f'[class*="000660"]',
    ]
    for sel in selectors_code:
        try:
            card = soup.select_one(sel)
            if card:
                hynix_price, hynix_ret = _extract_price_ret_from_card(card)
                if hynix_price or hynix_ret:
                    break
        except Exception:
            continue

    # ── 후보 selector 2: "SK하이닉스" 텍스트 근처 숫자 ──────────────────────
    if not (hynix_price or hynix_ret):
        hynix_price, hynix_ret = _extract_by_text_search(soup, "SK하이닉스")

    # ── 삼성전자 ──────────────────────────────────────────────────────────────
    _, samsung_ret = _extract_by_text_search(soup, "삼성전자")
    if samsung_ret is None:
        _, samsung_ret = _extract_by_code_search(soup, "005930")

    # ── 현대차 ────────────────────────────────────────────────────────────────
    _, hyundai_ret = _extract_by_text_search(soup, "현대차")
    if hyundai_ret is None:
        _, hyundai_ret = _extract_by_code_search(soup, "005380")

    if hynix_price is not None or hynix_ret is not None:
        result["hynix_reference_price"]  = hynix_price
        result["hynix_reference_return"] = hynix_ret
        result["samsung_reference_return"] = samsung_ret
        result["hyundai_reference_return"] = hyundai_ret
        result["source_status"] = "success"
        result["error_message"] = None
    else:
        result["source_status"] = "failed"
        result["error_message"] = f"[{source}] 파싱 후 하이닉스 데이터 없음. HTML 구조 확인 필요."

    return result


def _extract_price_ret_from_card(card) -> tuple[Optional[float], Optional[float]]:
    """카드 DOM에서 가격과 등락률 추출."""
    text = card.get_text(separator=" ")
    price = _parse_price_from_text(text)
    ret   = _parse_return_from_text(text)
    return price, ret


def _extract_by_text_search(soup, keyword: str) -> tuple[Optional[float], Optional[float]]:
    """페이지 텍스트에서 keyword 근처 숫자 파싱."""
    tags = soup.find_all(string=re.compile(keyword))
    for tag in tags:
        parent = tag.parent
        for _ in range(4):
            if parent is None:
                break
            text = parent.get_text(separator=" ")
            price = _parse_price_from_text(text)
            ret   = _parse_return_from_text(text)
            if price or ret:
                return price, ret
            parent = parent.parent
    return None, None


def _extract_by_code_search(soup, code: str) -> tuple[Optional[float], Optional[float]]:
    """종목 코드로 DOM 검색."""
    for sel in [f'[data-code="{code}"]', f'[data-ticker="{code}"]']:
        card = soup.select_one(sel)
        if card:
            text = card.get_text(separator=" ")
            return _parse_price_from_text(text), _parse_return_from_text(text)
    return None, None


def _parse_price_from_text(text: str) -> Optional[float]:
    """텍스트에서 주가(1000원 이상 정수) 파싱."""
    nums = re.findall(r"[\d,]+", text)
    for n in nums:
        try:
            v = float(n.replace(",", ""))
            if 50_000 <= v <= 500_000:  # SK하이닉스 합리적 범위
                return v
        except ValueError:
            continue
    return None


def _parse_return_from_text(text: str) -> Optional[float]:
    """텍스트에서 등락률(%) 파싱. +/-3자리.2자리 패턴."""
    patterns = [
        r"([+-]?\d{1,3}\.\d{1,2})%",
        r"([+-]?\d{1,3}\.\d{1,2})\s*%",
        r"([+-]?\d{1,3},\d{2})\s*%",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                continue
    return None
