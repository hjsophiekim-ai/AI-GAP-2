from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def kst_now() -> datetime:
    """Asia/Seoul 기준 현재 시각(naive datetime).

    하이닉스 Enhanced 자동매매(14:50 신규매수 금지, 15:15 전량청산 등)의 시간
    판정은 배포 서버(OS)의 타임존과 무관하게 항상 KST 기준이어야 한다. 서버가
    UTC로 배포되면 naive datetime.now()는 9시간 어긋난 시각을 반환하므로, 이
    함수를 통해서만 "지금이 몇 시인지"를 판정한다. tzinfo는 제거해 반환한다 —
    호출부가 기존에 naive datetime과 비교/직렬화하던 코드를 그대로 쓸 수 있게 한다.
    """
    return datetime.now(KST).replace(tzinfo=None)


def now_str() -> str:
    """Current time as 'HH:MM'."""
    return datetime.now().strftime("%H:%M")


def today_str() -> str:
    """Today's date as 'YYYYMMDD'."""
    return datetime.now().strftime("%Y%m%d")


def parse_time(t: str) -> tuple:
    """Parse 'HH:MM' string into (hour, minute) tuple."""
    parts = t.strip().split(":")
    return int(parts[0]), int(parts[1])


def time_gte(t1: str, t2: str) -> bool:
    """Return True if time string t1 >= t2 (both 'HH:MM')."""
    return parse_time(t1) >= parse_time(t2)


def is_market_open() -> bool:
    """Korean stock market open: 09:00 ~ 15:30."""
    now = datetime.now()
    h, m = now.hour, now.minute
    current = h * 60 + m
    return (9 * 60) <= current <= (15 * 60 + 30)


def is_pre_market() -> bool:
    """Return True before 09:00 market open."""
    now = datetime.now()
    return now.hour * 60 + now.minute < 9 * 60


def is_buy_window(cfg=None) -> bool:
    """Return True if current time is within the configured buy window.

    cfg may be a dict or object with buy_start_time / buy_end_time attributes
    (strings 'HH:MM').  Defaults to 09:00 ~ 09:30 if not supplied.
    """
    defaults = ("09:00", "09:30")

    if cfg is None:
        start_str, end_str = defaults
    elif isinstance(cfg, dict):
        start_str = cfg.get("buy_start_time", defaults[0])
        end_str = cfg.get("buy_end_time", defaults[1])
    else:
        start_str = getattr(cfg, "buy_start_time", defaults[0])
        end_str = getattr(cfg, "buy_end_time", defaults[1])

    current = now_str()
    return time_gte(current, start_str) and not time_gte(current, end_str)
