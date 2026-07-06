"""Korean stock market utility helpers."""


def format_price(price: float) -> str:
    """Format a price as '70,000원'."""
    return f"{int(price):,}원"


def format_amount(amount: float) -> str:
    """Format an amount as '1,200,000원'."""
    return f"{int(amount):,}원"


def format_rate(rate: float) -> str:
    """Format a rate as '+3.25%' or '-1.10%'."""
    sign = "+" if rate >= 0 else ""
    return f"{sign}{rate:.2f}%"


def calc_gap_rate(open: float, prev_close: float) -> float:
    """Gap rate = (open - prev_close) / prev_close * 100."""
    if prev_close == 0:
        return 0.0
    return (open - prev_close) / prev_close * 100


def calc_profit_rate(current: float, avg: float) -> float:
    """Profit rate = (current - avg) / avg * 100."""
    if avg == 0:
        return 0.0
    return (current - avg) / avg * 100


def get_tick_size(price: float) -> int:
    """Return the tick size (호가 단위) for a given Korean stock price.

    Rules (KRX standard as of 2023):
        price <    2,000  ->    1
        price <    5,000  ->    5
        price <   20,000  ->   10
        price <   50,000  ->   50
        price <  200,000  ->  100
        price <  500,000  ->  500
        price >= 500,000  -> 1,000
    """
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def add_ticks(price: float, n: int = 1) -> float:
    """Return price adjusted by n ticks (positive = up, negative = down).

    The tick size is determined by the *starting* price level.
    """
    tick = get_tick_size(price)
    return price + n * tick
