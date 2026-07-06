"""
us_sector_strength_service.py

전날 미국장 섹터 강도를 자동 분석한다.

데이터 소스 우선순위:
1. Yahoo Finance ETF quote pages (파싱 안정적)
2. 캐시 파일 (data/cache/us_sector_strength_YYYYMMDD.json)
3. 데이터 없음 → us_sector_match_score=0 처리

캐시 유효기간: 24시간

ETF 수집 상태:
  ok            : 섹터 ETF 5개 이상 성공
  partial_failed: 섹터 ETF 5개 미만 → strong_sectors=[], score=0
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ETF → US sector mapping
# ---------------------------------------------------------------------------

ETF_SECTOR_MAP: dict[str, str] = {
    "SMH": "semiconductor",
    "SOXX": "semiconductor",
    "NVDA": "semiconductor",
    "AMD": "semiconductor",
    "MU": "semiconductor",
    "XLK": "ai_data_center",
    "XLC": "ai_data_center",
    "MSFT": "ai_data_center",
    "GOOGL": "ai_data_center",
    "META": "ai_data_center",
    "XLU": "power_grid",
    "NEE": "power_grid",
    "URA": "power_grid",      # uranium/nuclear energy → power_grid
    "ITA": "defense",
    "LMT": "defense",
    "RTX": "defense",
    "BOTZ": "robotics",
    "ARKQ": "robotics",
    "LIT": "battery_ev",
    "TSLA": "battery_ev",
    "ALB": "battery_ev",
    "XLI": "industrials",
    "GE": "industrials",
    "CAT": "industrials",
    "XLV": "healthcare_bio",
    "XLY": "consumer_discretionary",
    "XLP": "consumer_staples",
    "XLE": "energy",
    "XLF": "financials",
    "XLB": "materials_copper",
    "COPX": "materials_copper",
    "FCX": "materials_copper",
    "XLRE": "real_estate",
    "SPY": "_benchmark",
    "QQQ": "_benchmark",
    "IWM": "_benchmark",
}

# Domestic sector → US sector key
DOMESTIC_TO_US_SECTOR_MAP: dict[str, Optional[str]] = {
    "semiconductor": "semiconductor",
    "ai_data_center": "ai_data_center",
    "power_grid": "power_grid",
    "shipbuilding": "industrials",
    "defense": "defense",
    "robotics": "robotics",
    "battery_ev": "battery_ev",
    "auto": "consumer_discretionary",
    "bio_healthcare": "healthcare_bio",
    "finance": "financials",
    "cosmetics_consumer": "consumer_discretionary",
    "entertainment_game": "consumer_discretionary",
    "construction_machinery": "industrials",
    "materials_copper": "materials_copper",
    "holding_company": None,
    "unknown": None,
}

# Full ETF universe (benchmarks + sector ETFs)
_ALL_SYMBOLS = [
    "SPY", "QQQ",          # benchmarks — regime only, NOT sector scoring
    "SMH", "SOXX",         # semiconductor
    "XLK", "XLC",          # tech / ai_data_center
    "XLU",                 # utilities / power_grid
    "ITA",                 # defense
    "BOTZ",                # robotics
    "LIT",                 # battery_ev
    "XLI",                 # industrials
    "XLV",                 # healthcare_bio
    "XLY",                 # consumer_discretionary
    "XLE",                 # energy
    "XLF",                 # financials
    "XLB", "COPX",         # materials_copper
    "XLRE",                # real_estate
    "URA",                 # uranium/nuclear → power_grid
    "XLP",                 # consumer_staples
]

# Symbols used only for regime detection, not sector scoring
_REGIME_ONLY: set[str] = {"SPY", "QQQ"}

# Minimum number of sector ETFs (non-benchmark) that must succeed for "ok" status
_SECTOR_MIN_OK = 5

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_DIR = _ROOT / "data" / "cache"


class USSectorStrengthService:
    """전날 미국장 섹터 강도를 자동 분석한다."""

    def __init__(self, cfg=None):
        if cfg is None:
            try:
                from app.config import get_config
                cfg = get_config()
            except Exception:
                cfg = None
        self.cfg = cfg
        self._us_cfg: dict = self._load_us_cfg()

    def _load_us_cfg(self) -> dict:
        try:
            return self.cfg._raw.get("us_sector_strength", {}) if self.cfg else {}
        except AttributeError:
            return {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_us_sector_strength(self) -> dict:
        """
        전날 미국장 섹터 강도를 반환한다.

        Returns dict with:
          market_regime, data_source_used, us_sector_data_status,
          successful_etf_count, failed_etfs,
          strong_sectors, moderate_sectors, sector_scores,
          sector_etf_changes, spy_change, qqq_change, collected_at
        """
        cache_enabled = self._us_cfg.get("cache_enabled", True)

        # 1. Try Yahoo Finance
        try:
            etf_results = self._fetch_yahoo_etf_changes(_ALL_SYMBOLS)
        except Exception as exc:
            logger.warning("[USSectorStrength] Yahoo 수집 오류: %s", exc)
            etf_results = {}

        if etf_results:
            sector_result = self._compute_from_etf_results(etf_results)
            sector_result["data_source_used"] = "yahoo"
            if cache_enabled and sector_result.get("us_sector_data_status") == "ok":
                self._save_cache(sector_result)
            return sector_result

        # 2. Try cache
        if cache_enabled:
            cached = self._load_cache()
            if cached:
                cached["data_source_used"] = "cache"
                logger.info("[USSectorStrength] 캐시 사용")
                return cached

        # 3. No data — return zero-score result
        logger.warning("[USSectorStrength] 미국 섹터 데이터 없음 → 0점 처리")
        return self._empty_result()

    def get_us_sector_match_score(
        self,
        domestic_sector: str,
        us_result: dict,
        us_sector_match_score_max: int = 20,
    ) -> tuple[int, str, str]:
        """
        국내 섹터와 미국 섹터 매칭 점수를 반환한다.

        Returns:
            (score, matched_us_sector, reason)
        """
        if not us_result or us_result.get("data_source_used") == "none":
            return (0, "", "no_us_data")

        if us_result.get("us_sector_data_status") == "partial_failed":
            return (0, "", "sector_etf_data_missing")

        us_key = DOMESTIC_TO_US_SECTOR_MAP.get(domestic_sector)
        if not us_key:
            return (0, "", "no_us_mapping")

        strong = us_result.get("strong_sectors", [])
        moderate = us_result.get("moderate_sectors", [])
        regime = us_result.get("market_regime", "neutral")

        if us_key in strong:
            base_score = us_sector_match_score_max
            reason = f"us_strong_{us_key}"
        elif us_key in moderate:
            base_score = us_sector_match_score_max // 2
            reason = f"us_moderate_{us_key}"
        else:
            return (0, us_key, f"us_weak_{us_key}")

        if regime == "risk_off":
            base_score = int(base_score * 0.5)
            reason += "+risk_off_penalty"

        return (base_score, us_key, reason)

    # ------------------------------------------------------------------
    # Core computation (unit-testable)
    # ------------------------------------------------------------------

    def _compute_from_etf_results(self, etf_results: dict) -> dict:
        """
        etf_results: {symbol: {"change_pct": float|None, "success": bool, "error": str}}

        섹터 ETF 5개 미만 성공 → partial_failed (strong_sectors=[]).
        SPY/QQQ는 regime 전용, sector scoring에서 제외.
        """
        spy_info = etf_results.get("SPY", {})
        qqq_info = etf_results.get("QQQ", {})
        spy_change = (spy_info.get("change_pct") or 0.0)
        qqq_change = (qqq_info.get("change_pct") or 0.0)

        sector_etfs_ok = {
            sym: info for sym, info in etf_results.items()
            if sym not in _REGIME_ONLY and info.get("success") and info.get("change_pct") is not None
        }
        failed_etfs = [sym for sym, info in etf_results.items() if not info.get("success")]

        regime = self._determine_market_regime(spy_change, qqq_change)

        if len(sector_etfs_ok) < _SECTOR_MIN_OK:
            result = self._empty_result()
            result.update({
                "us_sector_data_status": "partial_failed",
                "successful_etf_count": len(sector_etfs_ok),
                "failed_etfs": failed_etfs,
                "market_regime": regime,
                "spy_change": round(spy_change, 3),
                "qqq_change": round(qqq_change, 3),
                "strong_sectors": [],
                "moderate_sectors": [],
            })
            logger.warning(
                "[USSectorStrength] 섹터 ETF %d개만 성공 (최소 %d개 필요) → partial_failed",
                len(sector_etfs_ok), _SECTOR_MIN_OK,
            )
            return result

        # Build flat change dict for scoring
        etf_changes = {
            sym: info["change_pct"]
            for sym, info in etf_results.items()
            if info.get("success") and info.get("change_pct") is not None
        }

        sector_scores = self._compute_sector_scores(etf_changes, spy_change, qqq_change)
        strong_threshold = float(self._us_cfg.get("strong_threshold", 70))
        moderate_threshold = float(self._us_cfg.get("moderate_threshold", 50))

        strong = [
            s for s, sc in sorted(sector_scores.items(), key=lambda x: -x[1])
            if sc >= strong_threshold
        ]
        moderate = [
            s for s, sc in sorted(sector_scores.items(), key=lambda x: -x[1])
            if moderate_threshold <= sc < strong_threshold
        ]

        return {
            "market_regime": regime,
            "data_source_used": "yahoo",
            "us_sector_data_status": "ok",
            "successful_etf_count": len(sector_etfs_ok),
            "failed_etfs": failed_etfs,
            "strong_sectors": strong,
            "moderate_sectors": moderate,
            "sector_scores": sector_scores,
            "sector_etf_changes": {
                sym: round(info["change_pct"], 3)
                for sym, info in etf_results.items()
                if info.get("success") and info.get("change_pct") is not None
            },
            "spy_change": round(spy_change, 3),
            "qqq_change": round(qqq_change, 3),
            "collected_at": datetime.now().isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_yahoo_etf_changes(self, symbols: list[str]) -> dict[str, dict]:
        """Yahoo Finance v8 chart JSON API로 ETF 등락률 수집.
        JSON 파싱 실패 시 HTML quote 페이지로 폴백.

        Returns {symbol: {"change_pct": float|None, "success": bool, "error": str}}
        """
        results: dict[str, dict] = {}
        timeout = int(self._us_cfg.get("request_timeout_seconds", 8))

        session = requests.Session()
        session.headers.update(_HEADERS)

        for symbol in symbols:
            # 1차: v8 chart JSON API (더 안정적 — SPA 렌더링 불필요)
            try:
                url = (
                    f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
                    f"?interval=1d&range=5d&includePrePost=false"
                )
                resp = session.get(url, timeout=timeout)
                if resp.status_code == 200:
                    change_pct = self._parse_yahoo_v8_json(resp.text, symbol)
                    if change_pct is not None:
                        results[symbol] = {"change_pct": change_pct, "success": True, "error": ""}
                        time.sleep(0.15)
                        continue
                    # JSON 파싱 실패 → HTML 폴백
                elif resp.status_code in (429, 503):
                    # 요청 제한 → 잠시 대기 후 HTML 폴백
                    time.sleep(1.0)
            except Exception as exc:
                logger.debug("[USSectorStrength] %s v8 API 오류: %s", symbol, exc)

            # 2차: HTML quote 페이지 폴백
            try:
                url = f"https://finance.yahoo.com/quote/{symbol}/"
                resp = session.get(url, timeout=timeout)
                if resp.status_code != 200:
                    results[symbol] = {
                        "change_pct": None,
                        "success": False,
                        "error": f"http_{resp.status_code}",
                    }
                    continue
                change_pct = self._parse_yahoo_change(resp.text, symbol)
                if change_pct is not None:
                    results[symbol] = {"change_pct": change_pct, "success": True, "error": ""}
                else:
                    results[symbol] = {"change_pct": None, "success": False, "error": "html_parse_failed"}
                time.sleep(0.3)
            except Exception as exc:
                results[symbol] = {
                    "change_pct": None,
                    "success": False,
                    "error": str(exc)[:100],
                }

        ok_count = sum(1 for r in results.values() if r["success"])
        logger.info("[USSectorStrength] Yahoo 수집 완료: %d/%d", ok_count, len(symbols))
        return results

    def _parse_yahoo_v8_json(self, text: str, symbol: str) -> Optional[float]:
        """Yahoo Finance v8 chart JSON 응답에서 등락률 계산.

        Returns None if parsing fails — caller must treat as failure (not 0%).
        """
        try:
            data = json.loads(text)
            result = data.get("chart", {}).get("result")
            if not result:
                return None
            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price and prev and float(prev) > 0:
                return round((float(price) - float(prev)) / float(prev) * 100.0, 3)
        except Exception as exc:
            logger.debug("[USSectorStrength] %s v8 JSON 파싱 오류: %s", symbol, exc)
        return None

    def _parse_yahoo_change(self, html: str, symbol: str) -> Optional[float]:
        """HTML에서 regularMarketChangePercent를 파싱한다.

        Returns None if all patterns fail — caller must treat as failure (not 0%).
        """
        m = re.search(r'"regularMarketChangePercent"\s*:\s*\{"raw"\s*:\s*([-\d.]+)', html)
        if m:
            return float(m.group(1))

        m = re.search(r'data-field="regularMarketChangePercent"[^>]*data-value="([-\d.]+)"', html)
        if m:
            return float(m.group(1))

        m = re.search(
            r'<fin-streamer[^>]*data-field="regularMarketChangePercent"[^>]*value="([-\d.]+)"',
            html,
        )
        if m:
            return float(m.group(1))

        prev_m = re.search(r'"regularMarketPreviousClose"\s*:\s*\{"raw"\s*:\s*([\d.]+)', html)
        curr_m = re.search(r'"regularMarketPrice"\s*:\s*\{"raw"\s*:\s*([\d.]+)', html)
        if prev_m and curr_m:
            prev = float(prev_m.group(1))
            curr = float(curr_m.group(1))
            if prev > 0:
                return (curr - prev) / prev * 100.0

        logger.debug("[USSectorStrength] %s HTML 등락률 파싱 실패", symbol)
        return None  # 0.0 반환 금지 — 파싱 실패는 실패로 처리해야 함

    def _compute_sector_scores(
        self,
        etf_changes: dict[str, float],
        spy_change: float,
        qqq_change: float,
    ) -> dict[str, float]:
        """섹터별 0-100 점수를 계산한다."""
        sector_avgs: dict[str, list[float]] = {}
        for sym, chg in etf_changes.items():
            sec = ETF_SECTOR_MAP.get(sym)
            if sec and not sec.startswith("_"):
                sector_avgs.setdefault(sec, []).append(chg)

        sector_avg: dict[str, float] = {
            s: sum(vals) / len(vals) for s, vals in sector_avgs.items()
        }

        rel_strength: dict[str, float] = {
            s: avg - spy_change for s, avg in sector_avg.items()
        }

        scores: dict[str, float] = {}
        for sec, rel in rel_strength.items():
            normalized = max(0.0, min(100.0, 50.0 + rel * 16.67))
            scores[sec] = round(normalized, 1)

        return scores

    def _determine_market_regime(self, spy: float, qqq: float) -> str:
        if spy > 0 and qqq > 0:
            return "risk_on"
        if spy < -0.3 and qqq < -0.3:
            return "risk_off"
        return "neutral"

    def _empty_result(self) -> dict:
        return {
            "market_regime": "neutral",
            "data_source_used": "none",
            "us_sector_data_status": "no_data",
            "successful_etf_count": 0,
            "failed_etfs": [],
            "strong_sectors": [],
            "moderate_sectors": [],
            "sector_scores": {},
            "sector_etf_changes": {},
            "spy_change": 0.0,
            "qqq_change": 0.0,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _load_cache(self) -> Optional[dict]:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        cache_file = _CACHE_DIR / f"us_sector_strength_{today}.json"
        if not cache_file.exists():
            return None
        try:
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
            collected_at = datetime.fromisoformat(data.get("collected_at", "2000-01-01"))
            max_age = int(self._us_cfg.get("cache_max_age_hours", 24))
            if datetime.now() - collected_at > timedelta(hours=max_age):
                return None
            return data
        except Exception as exc:
            logger.debug("[USSectorStrength] 캐시 로드 오류: %s", exc)
            return None

    def _save_cache(self, data: dict) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        cache_file = _CACHE_DIR / f"us_sector_strength_{today}.json"
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug("[USSectorStrength] 캐시 저장: %s", cache_file)
        except Exception as exc:
            logger.debug("[USSectorStrength] 캐시 저장 오류: %s", exc)
