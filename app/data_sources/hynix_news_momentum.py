"""SK Hynix news/업황 momentum scorer (0~10점).

뉴스 스크래핑은 다른 필수 데이터보다 실패 확률이 높으므로, 실패 시 예측을
차단하지 않고 중립값(5점)으로 대체한다. `success=False`일 때 UI에서
"뉴스 데이터 수집 실패 - 중립값 사용" 경고를 표시해야 한다.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from app.utils.data_paths import CACHE_DIR

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_PATH = CACHE_DIR / "hynix_news_momentum.json"
_CACHE_MAX_AGE_HOURS = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

NEUTRAL_SCORE = 5.0

# 키워드(스펙 명시분: HBM, DRAM가격, 실적, AI서버, 공급과잉, 투자확대) 극성 사전.
# 값은 헤드라인 1건당 가중치. 최종 합산은 [-5, +5]로 클램프 후 5(중립)에 더한다.
_POSITIVE_PATTERNS = [
    (r"HBM", 0.6),
    (r"AI\s*서버", 0.5),
    (r"투자\s*확대", 0.5),
    (r"(호실적|실적\s*개선|어닝\s*서프라이즈|사상\s*최대)", 0.6),
    (r"(가격\s*상승|가격\s*인상)", 0.4),
    (r"(수주|공급\s*계약|증설)", 0.3),
]
_NEGATIVE_PATTERNS = [
    (r"공급\s*과잉", -0.7),
    (r"(실적\s*부진|어닝\s*쇼크|적자)", -0.6),
    (r"(가격\s*하락|가격\s*인하)", -0.4),
    (r"(재고\s*증가|수요\s*둔화|한파)", -0.4),
    (r"(감산|투자\s*축소|구조조정)", -0.5),
]
_ALL_PATTERNS = _POSITIVE_PATTERNS + _NEGATIVE_PATTERNS


def _read_cache() -> Optional[dict]:
    if not _CACHE_PATH.exists():
        return None
    age_hours = (time.time() - _CACHE_PATH.stat().st_mtime) / 3600
    if age_hours > _CACHE_MAX_AGE_HOURS:
        return None
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(payload: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("news momentum cache write failed: %s", exc)


def _fetch_headlines(query: str = "SK하이닉스", limit: int = 20) -> list[str]:
    url = "https://search.naver.com/search.naver"
    params = {"where": "news", "query": query, "sort": "1"}
    response = requests.get(url, headers=HEADERS, params=params, timeout=10)
    response.raise_for_status()
    text = response.text
    titles = re.findall(r'class="news_tit"[^>]*title="([^"]+)"', text)
    if not titles:
        titles = re.findall(r'class="news_tit"[^>]*>([^<]+)<', text)
    return titles[:limit]


def compute_news_momentum_score(query: str = "SK하이닉스") -> dict:
    """Return {"score": 0~10, "success": bool, "source": str, "keywords_found": [...], "error": str|None}."""
    try:
        headlines = _fetch_headlines(query)
        if len(headlines) < 3:
            raise ValueError(f"insufficient headlines ({len(headlines)})")

        total = 0.0
        keywords_found: list[str] = []
        for title in headlines:
            for pattern, weight in _ALL_PATTERNS:
                if re.search(pattern, title):
                    total += weight
                    keywords_found.append(f"{pattern}:{title}")

        total = max(-5.0, min(5.0, total))
        score = NEUTRAL_SCORE + total
        result = {
            "score": round(score, 2),
            "success": True,
            "source": "naver_news",
            "headline_count": len(headlines),
            "keywords_found": keywords_found[:10],
            "error": None,
            "computed_at": datetime.now().isoformat(),
        }
        _write_cache(result)
        return result
    except Exception as exc:
        logger.warning("[NEWS] SK하이닉스 뉴스 모멘텀 수집 실패: %s", exc)
        cached = _read_cache()
        if cached:
            cached = dict(cached)
            cached["success"] = False
            cached["source"] = "cache"
            cached["error"] = str(exc)
            return cached
        return {
            "score": NEUTRAL_SCORE,
            "success": False,
            "source": "fallback_neutral",
            "headline_count": 0,
            "keywords_found": [],
            "error": str(exc),
            "computed_at": datetime.now().isoformat(),
        }
