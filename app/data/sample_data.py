"""
sample_data.py

Generates realistic mock Korean stock gap-up data for dry_run mode.
No API keys required. No imports from app.models.
"""

import random
from datetime import date, datetime, timedelta


# ---------------------------------------------------------------------------
# Internal seed data
# ---------------------------------------------------------------------------

_REGULAR_STOCKS = [
    ("005930", "삼성전자", "KOSPI", "반도체", 72000),
    ("000660", "SK하이닉스", "KOSPI", "반도체", 182000),
    ("035420", "NAVER", "KOSPI", "인터넷", 210000),
    ("035720", "카카오", "KOSPI", "인터넷", 54000),
    ("051910", "LG화학", "KOSPI", "화학", 320000),
    ("006400", "삼성SDI", "KOSPI", "2차전지", 380000),
    ("207940", "삼성바이오로직스", "KOSPI", "바이오", 780000),
    ("068270", "셀트리온", "KOSPI", "바이오", 168000),
    ("323410", "카카오뱅크", "KOSPI", "금융", 23000),
    ("003550", "LG", "KOSPI", "지주", 87000),
    ("034730", "SK", "KOSPI", "지주", 145000),
    ("096770", "SK이노베이션", "KOSPI", "에너지", 115000),
    ("017670", "SK텔레콤", "KOSPI", "통신", 56000),
    ("030200", "KT", "KOSPI", "통신", 38000),
    ("032830", "삼성생명", "KOSPI", "보험", 92000),
    ("055550", "신한지주", "KOSPI", "금융", 46000),
    ("105560", "KB금융", "KOSPI", "금융", 77000),
    ("086790", "하나금융지주", "KOSPI", "금융", 61000),
    ("316140", "우리금융지주", "KOSPI", "금융", 14500),
    ("000270", "기아", "KOSPI", "자동차", 98000),
    ("012330", "현대모비스", "KOSPI", "자동차부품", 245000),
    ("010130", "고려아연", "KOSPI", "비철금속", 580000),
    ("011200", "HMM", "KOSPI", "해운", 18500),
    ("047810", "한국항공우주", "KOSPI", "항공우주", 62000),
    ("042660", "한화오션", "KOSPI", "조선", 32000),
    ("009540", "HD한국조선해양", "KOSPI", "조선", 175000),
    ("267250", "HD현대", "KOSPI", "지주", 72000),
    ("011070", "LG이노텍", "KOSPI", "전자부품", 220000),
    ("003490", "대한항공", "KOSPI", "항공", 26000),
    ("021240", "코웨이", "KOSPI", "생활가전", 62000),
    ("178920", "PI첨단소재", "KOSDAQ", "화학소재", 32000),
    ("196170", "알테오젠", "KOSDAQ", "바이오", 145000),
    ("086520", "에코프로", "KOSDAQ", "2차전지", 95000),
    ("247540", "에코프로비엠", "KOSDAQ", "2차전지", 220000),
    ("066970", "엘앤에프", "KOSDAQ", "2차전지", 145000),
    ("357780", "솔브레인", "KOSDAQ", "반도체소재", 82000),
    ("041510", "에스엠", "KOSDAQ", "엔터", 78000),
    ("035900", "JYP Ent.", "KOSDAQ", "엔터", 68000),
    ("122870", "와이지엔터테인먼트", "KOSDAQ", "엔터", 52000),
    ("263750", "펄어비스", "KOSDAQ", "게임", 42000),
    ("293490", "카카오게임즈", "KOSDAQ", "게임", 21000),
    ("112040", "위메이드", "KOSDAQ", "게임", 31000),
    ("095700", "제이씨현시스템", "KOSDAQ", "IT서비스", 12000),
    ("214150", "클래시스", "KOSDAQ", "의료기기", 36000),
    ("145020", "휴젤", "KOSDAQ", "바이오", 280000),
]

_ETF_STOCKS = [
    ("069500", "KODEX 200", "KOSPI", "ETF"),
    ("102110", "TIGER 200", "KOSPI", "ETF"),
    ("091160", "KODEX 반도체", "KOSPI", "ETF"),
    ("091230", "TIGER 반도체", "KOSPI", "ETF"),
    ("229200", "KODEX 코스닥150", "KOSDAQ", "ETF"),
]

_PREFERRED_STOCKS = [
    ("005935", "삼성전자우", "KOSPI", "반도체", 65000),
    ("000270A", "기아우", "KOSPI", "자동차", 89000),  # simplified code for sample
]

_SPAC_STOCKS = [
    ("413630", "에스케이증권기업인수목적28호스팩", "KOSDAQ", "스팩", 2050),
    ("417200", "미래에셋대우기업인수목적25호스팩", "KOSDAQ", "스팩", 2080),
]

_WARNING_SYMBOLS = [
    ("900110", "이스트아시아홀딩스", "KOSDAQ", "지주", 450),
    ("950130", "코인원글로벌", "KOSDAQ", "핀테크", 1200),
]


def _today_str() -> str:
    return date.today().strftime("%Y%m%d")


def _rng_seed(date_str: str) -> random.Random:
    """Create a seeded RNG so output is reproducible per date."""
    seed = int(date_str) if date_str else int(_today_str())
    return random.Random(seed)


def generate_sample_gap_stocks(date_str: str = None, n: int = 30) -> list:
    """
    Returns a list of dicts representing gap-up stocks for dry_run mode.

    Keys per dict:
        symbol, name, market, previous_close, open, high, low,
        current_price, volume, trade_value, change_rate, gap_rate,
        sector, is_etf, is_etn, is_preferred, is_spac, is_reit,
        is_warning, is_halt, source, date, time
    """
    if date_str is None:
        date_str = _today_str()

    rng = _rng_seed(date_str)
    results = []

    # --- 5 ETF stocks ---
    etf_base_prices = [42000, 16800, 12200, 11900, 8500]
    for i, (symbol, name, market, sector) in enumerate(_ETF_STOCKS):
        prev = etf_base_prices[i]
        gap = rng.uniform(0.5, 4.0)
        results.append(_build_stock(
            rng, symbol, name, market, sector, prev, gap,
            is_etf=True, date_str=date_str,
        ))

    # --- 2 preferred stocks ---
    for symbol, name, market, sector, prev in _PREFERRED_STOCKS:
        gap = rng.uniform(1.0, 5.0)
        results.append(_build_stock(
            rng, symbol, name, market, sector, prev, gap,
            is_preferred=True, date_str=date_str,
        ))

    # --- 2 SPAC stocks ---
    for symbol, name, market, sector, prev in _SPAC_STOCKS:
        gap = rng.uniform(0.5, 3.0)
        results.append(_build_stock(
            rng, symbol, name, market, sector, prev, gap,
            is_spac=True, date_str=date_str,
        ))

    # --- 2 warning stocks ---
    for symbol, name, market, sector, prev in _WARNING_SYMBOLS:
        gap = rng.uniform(2.0, 10.0)
        results.append(_build_stock(
            rng, symbol, name, market, sector, prev, gap,
            is_warning=True, date_str=date_str,
        ))

    # --- 15 valid candidate stocks (gap 3-15%) with varied trade_value ---
    valid_pool = rng.sample(_REGULAR_STOCKS, min(15, len(_REGULAR_STOCKS)))
    trade_value_tiers = (
        [5_000_000_000] * 3    # 5B won  — below typical threshold
        + [30_000_000_000] * 7  # 30B won — mid range
        + [100_000_000_000] * 5 # 100B won — high liquidity
    )
    rng.shuffle(trade_value_tiers)

    for idx, (symbol, name, market, sector, prev) in enumerate(valid_pool):
        gap = rng.uniform(3.0, 15.0)
        tv = trade_value_tiers[idx]
        results.append(_build_stock(
            rng, symbol, name, market, sector, prev, gap,
            trade_value_override=tv, date_str=date_str,
        ))

    # --- fill remainder up to n with low-gap or out-of-range stocks ---
    remaining_pool = [s for s in _REGULAR_STOCKS if s not in valid_pool]
    rng.shuffle(remaining_pool)
    needed = n - len(results)
    for symbol, name, market, sector, prev in remaining_pool[:needed]:
        # mix: some below 3%, some above 15%
        if rng.random() < 0.4:
            gap = rng.uniform(0.5, 2.9)
        else:
            gap = rng.uniform(15.1, 18.0)
        results.append(_build_stock(
            rng, symbol, name, market, sector, prev, gap,
            date_str=date_str,
        ))

    # Shuffle so special types are not always at the front
    rng.shuffle(results)
    return results[:n]


def _build_stock(
    rng: random.Random,
    symbol: str,
    name: str,
    market: str,
    sector: str,
    previous_close: int,
    gap_rate: float,
    *,
    is_etf: bool = False,
    is_etn: bool = False,
    is_preferred: bool = False,
    is_spac: bool = False,
    is_reit: bool = False,
    is_warning: bool = False,
    is_halt: bool = False,
    trade_value_override: int = None,
    date_str: str = None,
) -> dict:
    """Build a single stock dict with realistic intraday prices."""
    open_price = round(previous_close * (1 + gap_rate / 100))
    high_price = round(open_price * (1 + rng.uniform(0.005, 0.03)))
    low_price = round(open_price * (1 - rng.uniform(0.001, 0.01)))
    current_price = rng.randint(low_price, high_price)

    # change_rate relative to previous_close
    change_rate = round((current_price - previous_close) / previous_close * 100, 2)

    # Volume derived from trade_value / average of open & current
    avg_price = (open_price + current_price) / 2
    if trade_value_override is not None:
        trade_value = trade_value_override
    else:
        # realistic range: 1B to 80B won
        trade_value = rng.randint(1_000_000_000, 80_000_000_000)
    volume = int(trade_value / avg_price) if avg_price > 0 else 0

    return {
        "symbol": symbol,
        "name": name,
        "market": market,
        "previous_close": previous_close,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "current_price": current_price,
        "volume": volume,
        "trade_value": trade_value,
        "change_rate": change_rate,
        "gap_rate": round(gap_rate, 2),
        "sector": sector,
        "is_etf": is_etf,
        "is_etn": is_etn,
        "is_preferred": is_preferred,
        "is_spac": is_spac,
        "is_reit": is_reit,
        "is_warning": is_warning,
        "is_halt": is_halt,
        "source": "sample",
        "date": date_str or _today_str(),
        "time": "09:05",
    }


# ---------------------------------------------------------------------------
# OHLCV history for backtesting
# ---------------------------------------------------------------------------

def generate_sample_ohlcv_history(symbol: str, days: int = 60) -> list:
    """
    Returns a list of daily OHLCV dicts for the given symbol spanning `days`
    calendar days back from today.

    Keys per dict: date, open, high, low, close, volume, trade_value
    """
    # Derive a stable base price from the symbol string
    seed_val = sum(ord(c) * (i + 1) for i, c in enumerate(symbol))
    rng = random.Random(seed_val)

    # Pick a realistic base price
    base_price = rng.choice([
        5000, 8000, 12000, 18000, 25000, 35000, 50000,
        75000, 100000, 150000, 200000,
    ])

    today = date.today()
    records = []
    close = base_price

    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        # Skip weekends
        if day.weekday() >= 5:
            continue

        daily_ret = rng.gauss(0.001, 0.018)  # slight upward drift
        close_new = max(100, round(close * (1 + daily_ret)))
        open_price = round(close * (1 + rng.gauss(0.0, 0.008)))
        intraday_range = abs(rng.gauss(0, 0.012))
        high_price = round(max(open_price, close_new) * (1 + intraday_range))
        low_price = round(min(open_price, close_new) * (1 - intraday_range))

        avg_price = (open_price + close_new) / 2 or 1
        trade_value = rng.randint(1_000_000_000, 60_000_000_000)
        volume = int(trade_value / avg_price)

        records.append({
            "date": day.strftime("%Y%m%d"),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_new,
            "volume": volume,
            "trade_value": trade_value,
        })
        close = close_new

    return records
