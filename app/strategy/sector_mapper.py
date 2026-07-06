"""
sector_mapper.py

종목 → 섹터/서브테마 매핑 모듈.

우선순위:
  1. symbol_overrides (종목코드 직접 매핑)
  2. kr_name_patterns (종목명 직접 매칭)
  3. industry 업종명 키워드
  4. kr_keywords (종목명 내 키워드)
  5. fallback → "unknown"

설정 파일: config/kr_sector_map.yaml
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG = _PROJECT_ROOT / "config" / "kr_sector_map.yaml"

# 내장 fallback (YAML 로드 실패 시 사용)
_BUILTIN_KEYWORDS: dict[str, list[str]] = {
    "semiconductor": ["반도체", "HBM", "메모리", "낸드", "파운드리", "웨이퍼", "후공정"],
    "ai_data_center": ["AI", "인공지능", "클라우드", "데이터센터", "광모듈"],
    "power_grid": ["전력기기", "변압기", "전선", "원전", "전력망"],
    "shipbuilding": ["조선", "LNG선", "선박"],
    "defense": ["방산", "방위", "항공우주", "미사일", "레이더"],
    "robotics": ["로봇", "감속기", "협동로봇", "스마트팩토리"],
    "battery_ev": ["2차전지", "배터리", "양극재", "음극재", "전기차", "리튬"],
    "auto": ["자동차", "타이어", "변속기"],
    "bio_healthcare": ["바이오", "제약", "의료기기", "헬스케어", "신약"],
    "finance": ["은행", "금융", "증권", "보험", "캐피탈"],
    "cosmetics_consumer": ["화장품", "뷰티", "코스메틱", "소비재"],
    "entertainment_game": ["엔터", "게임", "K-pop", "아이돌"],
    "construction_machinery": ["건설", "기계", "플랜트", "인프라"],
    "materials_copper": ["구리", "비철금속", "소재", "전선", "철강"],
    "holding_company": ["홀딩스", "지주"],
}

_BUILTIN_OVERRIDES: dict[str, str] = {
    "000660": "semiconductor",
    "005930": "semiconductor",
    "042700": "semiconductor",
    "012450": "defense",
    "079550": "defense",
    "047810": "defense",
    "267250": "shipbuilding",
    "009540": "shipbuilding",
    "010140": "shipbuilding",
    "042660": "shipbuilding",
    "373220": "battery_ev",
    "086520": "battery_ev",
    "247540": "battery_ev",
    "003670": "battery_ev",
    "066970": "battery_ev",
    "005380": "auto",
    "000270": "auto",
    "207940": "bio_healthcare",
    "068270": "bio_healthcare",
    "105560": "finance",
    "055550": "finance",
    "086790": "finance",
    "090430": "cosmetics_consumer",
    "051900": "cosmetics_consumer",
    "069960": "entertainment_game",
    "035420": "ai_data_center",
    "035720": "ai_data_center",
    "010120": "power_grid",
    "103140": "materials_copper",
    "010130": "materials_copper",
}


class SectorMapper:
    """
    종목 → 섹터/서브테마 매핑기.

    Parameters
    ----------
    config_path : str | Path | None
        kr_sector_map.yaml 경로. None이면 기본 경로 사용.
    """

    def __init__(self, config_path: Optional[str | Path] = None):
        self._cfg: dict = {}
        self._symbol_overrides: dict[str, str] = {}
        self._sector_keywords: dict[str, list[str]] = {}
        self._sector_name_patterns: dict[str, list[str]] = {}
        self._subtheme_keywords: dict[str, dict] = {}
        self._load_config(config_path)

    # ── 설정 로드 ──────────────────────────────────────────────────────────────

    def _load_config(self, config_path: Optional[str | Path]) -> None:
        path = Path(config_path) if config_path else _DEFAULT_CONFIG
        if not _HAS_YAML:
            logger.warning("[SectorMapper] PyYAML 없음 — 내장 fallback 사용")
            self._use_builtin()
            return

        if not path.exists():
            logger.warning("[SectorMapper] 설정파일 없음 (%s) — 내장 fallback 사용", path)
            self._use_builtin()
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                self._cfg = yaml.safe_load(f) or {}
            self._symbol_overrides = self._cfg.get("symbol_overrides", {})
            # 섹터별 키워드 / 이름패턴 구성
            for sector, defn in self._cfg.get("sector_definitions", {}).items():
                self._sector_keywords[sector] = [
                    kw.lower() for kw in defn.get("kr_keywords", [])
                ]
                self._sector_name_patterns[sector] = defn.get("kr_name_patterns", [])
            # 서브테마
            self._subtheme_keywords = self._cfg.get("subtheme_definitions", {})
            logger.info("[SectorMapper] 설정 로드 완료: %s", path)
        except Exception as exc:
            logger.warning("[SectorMapper] 설정 로드 오류(%s) — 내장 fallback: %s", path, exc)
            self._use_builtin()

    def _use_builtin(self) -> None:
        self._symbol_overrides = _BUILTIN_OVERRIDES.copy()
        self._sector_keywords = {
            s: [kw.lower() for kw in kws]
            for s, kws in _BUILTIN_KEYWORDS.items()
        }
        self._sector_name_patterns = {}

    # ── 섹터 판단 ──────────────────────────────────────────────────────────────

    def get_sector(
        self,
        symbol: str,
        name: str = "",
        industry: str = "",
    ) -> str:
        """종목코드/이름/업종 → 섹터 키 반환. 미매핑 시 'unknown'."""
        # 1. symbol_overrides 직접 매핑
        if symbol in self._symbol_overrides:
            return self._symbol_overrides[symbol]

        # 2. 종목명 직접 매핑 (kr_name_patterns)
        if name:
            for sector, patterns in self._sector_name_patterns.items():
                for ptn in patterns:
                    if ptn and ptn in name:
                        return sector

        # 3. 업종명 키워드 매칭
        if industry:
            industry_lower = industry.lower()
            for sector, keywords in self._sector_keywords.items():
                for kw in keywords:
                    if kw and kw in industry_lower:
                        return sector

        # 4. 종목명 키워드 매칭
        if name:
            name_lower = name.lower()
            for sector, keywords in self._sector_keywords.items():
                for kw in keywords:
                    if kw and kw in name_lower:
                        return sector

        return "unknown"

    def get_subtheme(
        self,
        symbol: str,
        name: str = "",
        industry: str = "",
    ) -> str:
        """종목 서브테마 반환. 미매핑 시 ''."""
        combined = (name + " " + industry).lower()
        for subtheme, defn in self._subtheme_keywords.items():
            # 심볼 직접 매핑
            if symbol in defn.get("symbols", []):
                return subtheme
            # 키워드 매칭
            for kw in defn.get("keywords", []):
                if kw.lower() in combined:
                    return subtheme
        return ""

    # ── 일괄 분류 ──────────────────────────────────────────────────────────────

    def classify_stocks(self, stocks: list[dict]) -> list[dict]:
        """
        stocks 리스트에 'sector', 'subtheme' 필드를 추가해 반환.
        원본 dict를 변경하지 않고 복사본에 추가.
        """
        result = []
        for s in stocks:
            sym = s.get("symbol", "")
            name = s.get("name", "")
            industry = s.get("industry", "") or s.get("sector_raw", "")
            sector = self.get_sector(sym, name, industry)
            subtheme = self.get_subtheme(sym, name, industry)
            result.append({**s, "sector": sector, "subtheme": subtheme})
        return result

    def get_us_sector_key(self, sector: str) -> str:
        """한국 섹터 → 미국 섹터 ETF 매핑 키 반환."""
        defn = self._cfg.get("sector_definitions", {}).get(sector, {})
        return defn.get("us_sector_key", "") or ""

    def get_us_sector_key_alt(self, sector: str) -> list[str]:
        """한국 섹터 → 대체 미국 섹터 키 리스트."""
        defn = self._cfg.get("sector_definitions", {}).get(sector, {})
        return defn.get("us_sector_key_alt", []) or []


# ── 모듈 수준 편의 함수 ────────────────────────────────────────────────────────

_default_mapper: Optional[SectorMapper] = None


def _get_mapper() -> SectorMapper:
    global _default_mapper
    if _default_mapper is None:
        _default_mapper = SectorMapper()
    return _default_mapper


def classify_stocks(stocks: list[dict]) -> list[dict]:
    """모듈 수준 편의 함수: 일괄 섹터 분류."""
    return _get_mapper().classify_stocks(stocks)


def get_sector(symbol: str, name: str = "", industry: str = "") -> str:
    """모듈 수준 편의 함수: 단일 종목 섹터 조회."""
    return _get_mapper().get_sector(symbol, name, industry)
