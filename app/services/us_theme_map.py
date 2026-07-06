"""
US Theme → Korean Sector/Keyword Mapping.

Defines:
  - US_UNIVERSE: list of (symbol, exchange_code, theme) tuples
  - US_THEME_DEFINITIONS: per-theme metadata (kr_keywords, kr_rep_names)
  - KR_THEME_KEYWORDS: flat keyword → theme mapping for fast lookup
"""

from typing import NamedTuple


class SymbolInfo(NamedTuple):
    symbol: str
    excd: str   # NAS / NYS / AMS
    theme: str
    is_index: bool = False  # True for SPY/QQQ/IWM (benchmark, not scored as theme)


# ---------------------------------------------------------------------------
# US Universe  (symbol, exchange_code, theme, is_index)
# ---------------------------------------------------------------------------

US_UNIVERSE: list[SymbolInfo] = [
    # ── Semiconductor ────────────────────────────────────────────────────
    SymbolInfo("SMH",  "AMS", "semiconductor"),
    SymbolInfo("SOXX", "AMS", "semiconductor"),
    SymbolInfo("NVDA", "NAS", "semiconductor"),
    SymbolInfo("AMD",  "NAS", "semiconductor"),
    SymbolInfo("AVGO", "NAS", "semiconductor"),
    SymbolInfo("MU",   "NAS", "semiconductor"),
    # ── AI / Cloud Platform ──────────────────────────────────────────────
    SymbolInfo("XLK",  "AMS", "ai_cloud_platform"),
    SymbolInfo("XLC",  "AMS", "ai_cloud_platform"),
    SymbolInfo("MSFT", "NAS", "ai_cloud_platform"),
    SymbolInfo("GOOGL","NAS", "ai_cloud_platform"),
    SymbolInfo("META", "NAS", "ai_cloud_platform"),
    SymbolInfo("AMZN", "NAS", "ai_cloud_platform"),
    SymbolInfo("PLTR", "NAS", "ai_cloud_platform"),
    # ── Robotics / Automation ────────────────────────────────────────────
    SymbolInfo("BOTZ", "NAS", "robotics_automation"),
    SymbolInfo("ARKQ", "AMS", "robotics_automation"),
    # ── Battery / EV ─────────────────────────────────────────────────────
    SymbolInfo("LIT",  "AMS", "battery_ev"),
    SymbolInfo("TSLA", "NAS", "battery_ev"),
    SymbolInfo("ALB",  "NYS", "battery_ev"),
    # ── Defense / Aerospace ──────────────────────────────────────────────
    SymbolInfo("ITA",  "AMS", "defense_aerospace"),
    SymbolInfo("LMT",  "NYS", "defense_aerospace"),
    SymbolInfo("RTX",  "NYS", "defense_aerospace"),
    SymbolInfo("NOC",  "NYS", "defense_aerospace"),
    # ── Power Grid / Energy Infra ────────────────────────────────────────
    SymbolInfo("XLU",  "AMS", "power_grid_energy"),
    SymbolInfo("NEE",  "NYS", "power_grid_energy"),
    SymbolInfo("URA",  "AMS", "power_grid_energy"),   # nuclear
    SymbolInfo("ICLN", "NAS", "power_grid_energy"),   # clean energy
    # ── Materials / Copper ───────────────────────────────────────────────
    SymbolInfo("XLB",  "AMS", "materials_copper"),
    SymbolInfo("COPX", "AMS", "materials_copper"),
    SymbolInfo("FCX",  "NYS", "materials_copper"),
    SymbolInfo("SCCO", "NYS", "materials_copper"),
    # ── Industrials ──────────────────────────────────────────────────────
    SymbolInfo("XLI",  "AMS", "industrials"),
    SymbolInfo("GE",   "NYS", "industrials"),
    SymbolInfo("CAT",  "NYS", "industrials"),
    SymbolInfo("DE",   "NYS", "industrials"),
    # ── Healthcare / Bio ─────────────────────────────────────────────────
    SymbolInfo("XLV",  "AMS", "healthcare_bio"),
    # ── Consumer Discretionary ───────────────────────────────────────────
    SymbolInfo("XLY",  "AMS", "consumer_discretionary"),
    # ── Energy ───────────────────────────────────────────────────────────
    SymbolInfo("XLE",  "AMS", "energy"),
    # ── Financials ───────────────────────────────────────────────────────
    SymbolInfo("XLF",  "AMS", "financials"),
    # ── Market Benchmarks (not scored as themes, used for relative strength) ──
    SymbolInfo("SPY",  "AMS", "market_spy",  is_index=True),
    SymbolInfo("QQQ",  "NAS", "market_qqq",  is_index=True),
    SymbolInfo("IWM",  "AMS", "market_iwm",  is_index=True),
    SymbolInfo("DIA",  "AMS", "market_dia",  is_index=True),
]


# ---------------------------------------------------------------------------
# Theme Definitions  (Korean keywords / representative stock names)
# ---------------------------------------------------------------------------

US_THEME_DEFINITIONS: dict[str, dict] = {
    "semiconductor": {
        "label_kr": "AI반도체",
        "drivers": ["SMH", "SOXX", "NVDA", "AMD", "MU"],
        "kr_keywords": [
            "반도체", "HBM", "메모리", "낸드", "파운드리", "후공정",
            "장비", "소재", "칩", "AI반도체", "웨이퍼", "포토레지스트",
        ],
        "kr_rep_names": [
            "SK하이닉스", "삼성전자", "한미반도체", "이오테크닉스", "ISC",
            "리노공업", "HPSP", "주성엔지니어링", "원익IPS", "하나마이크론",
            "심텍", "동진쎄미켐", "솔브레인", "피에스케이", "테스나",
        ],
    },
    "ai_cloud_platform": {
        "label_kr": "AI/클라우드",
        "drivers": ["XLK", "MSFT", "GOOGL", "META", "AMZN"],
        "kr_keywords": [
            "AI", "인공지능", "클라우드", "데이터센터", "서버", "소프트웨어",
            "플랫폼", "통신장비", "광모듈", "냉각", "전력기기",
        ],
        "kr_rep_names": [
            "삼성전자", "SK하이닉스", "네이버", "카카오", "케이아이엔엑스",
            "가온전선", "오이솔루션", "파이버프로",
        ],
    },
    "robotics_automation": {
        "label_kr": "로봇/자동화",
        "drivers": ["BOTZ", "ARKQ"],
        "kr_keywords": [
            "로봇", "자동화", "스마트팩토리", "감속기", "협동로봇",
            "산업용로봇", "액추에이터", "로보틱스",
        ],
        "kr_rep_names": [
            "레인보우로보틱스", "두산로보틱스", "로보스타", "에스피지",
            "하이젠알앤엠", "현대위아", "에스비비테크",
        ],
    },
    "battery_ev": {
        "label_kr": "2차전지/EV",
        "drivers": ["LIT", "TSLA", "ALB"],
        "kr_keywords": [
            "2차전지", "배터리", "양극재", "음극재", "전해액",
            "전기차", "리튬", "장비", "이차전지", "ESS",
        ],
        "kr_rep_names": [
            "LG에너지솔루션", "삼성SDI", "SK이노베이션", "에코프로비엠",
            "포스코퓨처엠", "엘앤에프", "에코프로", "솔브레인",
            "씨아이에스", "피엔티", "필에너지",
        ],
    },
    "defense_aerospace": {
        "label_kr": "방산/항공우주",
        "drivers": ["ITA", "LMT", "RTX"],
        "kr_keywords": [
            "방산", "방위", "항공", "우주", "미사일", "레이더",
            "위성", "군수", "함정", "탄약",
        ],
        "kr_rep_names": [
            "한화에어로스페이스", "LIG넥스원", "한국항공우주", "현대로템",
            "한화시스템", "빅텍", "LIG넥스원", "휴니드",
        ],
    },
    "power_grid_energy": {
        "label_kr": "전력/에너지인프라",
        "drivers": ["XLU", "NEE", "URA"],
        "kr_keywords": [
            "전력기기", "변압기", "전선", "전력망", "원전", "전력",
            "데이터센터전력", "냉각", "LS", "중전기", "초전도",
        ],
        "kr_rep_names": [
            "HD현대일렉트릭", "LS ELECTRIC", "효성중공업", "일진전기",
            "대한전선", "LS", "두산에너빌리티", "LS일렉트릭",
            "제룡전기", "광명전기",
        ],
    },
    "materials_copper": {
        "label_kr": "구리/소재",
        "drivers": ["COPX", "FCX", "XLB"],
        "kr_keywords": [
            "구리", "비철금속", "소재", "원자재", "전선",
            "동", "알루미늄", "니켈", "아연",
        ],
        "kr_rep_names": [
            "풍산", "LS", "고려아연", "대한전선", "이구산업",
        ],
    },
    "industrials": {
        "label_kr": "산업재/인프라",
        "drivers": ["XLI", "GE", "CAT"],
        "kr_keywords": [
            "인프라", "건설기계", "조선", "철강", "기계",
            "플랜트", "중공업", "산업재",
        ],
        "kr_rep_names": [
            "HD현대중공업", "두산밥캣", "현대건설기계", "포스코홀딩스",
        ],
    },
    "healthcare_bio": {
        "label_kr": "바이오/헬스케어",
        "drivers": ["XLV"],
        "kr_keywords": [
            "바이오", "제약", "의료기기", "헬스케어", "신약",
            "항체", "유전", "임상", "진단키트",
        ],
        "kr_rep_names": [
            "삼성바이오로직스", "셀트리온", "유한양행", "한미약품",
        ],
    },
    "consumer_discretionary": {
        "label_kr": "소비재/엔터",
        "drivers": ["XLY"],
        "kr_keywords": [
            "자동차", "화장품", "의류", "소비재", "여행",
            "카지노", "엔터", "콘텐츠", "K-pop", "미디어",
        ],
        "kr_rep_names": [
            "현대차", "기아", "LG생활건강", "아모레퍼시픽", "하이브",
        ],
    },
    "energy": {
        "label_kr": "에너지",
        "drivers": ["XLE"],
        "kr_keywords": [
            "에너지", "석유", "화학", "정유", "가스",
            "태양광", "수소", "LNG",
        ],
        "kr_rep_names": [
            "S-Oil", "SK이노베이션", "GS칼텍스", "한화솔루션",
        ],
    },
    "financials": {
        "label_kr": "금융",
        "drivers": ["XLF"],
        "kr_keywords": [
            "금융", "은행", "보험", "증권", "투자",
            "캐피탈", "저축은행",
        ],
        "kr_rep_names": [
            "KB금융", "신한지주", "하나금융지주", "삼성생명",
        ],
    },
}


# ---------------------------------------------------------------------------
# Flat keyword → theme lookup (built once at import time)
# ---------------------------------------------------------------------------

def _build_keyword_theme_map() -> dict[str, list[str]]:
    """Returns {keyword_lower: [theme1, theme2, ...]} for fast name matching."""
    result: dict[str, list[str]] = {}
    for theme, defn in US_THEME_DEFINITIONS.items():
        for kw in defn.get("kr_keywords", []):
            key = kw.lower()
            result.setdefault(key, [])
            if theme not in result[key]:
                result[key].append(theme)
        for name in defn.get("kr_rep_names", []):
            key = name.lower()
            result.setdefault(key, [])
            if theme not in result[key]:
                result[key].append(theme)
    return result


KR_KEYWORD_TO_THEMES: dict[str, list[str]] = _build_keyword_theme_map()


def match_kr_stock_to_themes(stock_name: str, sector: str = "") -> list[str]:
    """
    Given a Korean stock name (and optional sector string), return matching US theme keys.

    Parameters
    ----------
    stock_name : str
        Korean stock name, e.g. "SK하이닉스"
    sector : str
        Sector/industry string from the data source

    Returns
    -------
    list of matched theme keys (may be empty)
    """
    name_lower = stock_name.lower()
    sector_lower = (sector or "").lower()
    matched: list[str] = []

    for kw, themes in KR_KEYWORD_TO_THEMES.items():
        if kw in name_lower or (sector_lower and kw in sector_lower):
            for t in themes:
                if t not in matched:
                    matched.append(t)

    return matched
