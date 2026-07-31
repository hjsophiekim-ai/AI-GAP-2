"""TSLA_AUTO US market calendar/session module — pure functions only.

Internal timezone is America/New_York (zoneinfo, DST auto-applied — never a
fixed KST offset). No network, no state file.

docs/TSLA_AUTO_LOGIC.md §KIS_OVERSEAS_API_CONFIRMATION_REQUIRED: whether KIS
itself exposes a US holiday/early-close TR was not confirmed against official
docs in this session, so this module maintains its own NYSE holiday/early-close
calendar (standard, publicly documented NYSE rules — not KIS data) as the
starting implementation. If KIS later confirms an official TR, this module's
functions should be backed by that call instead, keeping the same signatures.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil.easter import easter

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

# ── Regular session (strategy-fixed) ────────────────────────────────────────
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# ── Relative-to-close cutoffs (docs §6/§11) ─────────────────────────────────
NEW_ENTRY_CUTOFF_BEFORE_CLOSE_MIN = 15  # 신규진입 차단: 장 종료 15분 전
FORCED_LIQUIDATION_BEFORE_CLOSE_MIN = 10  # 강제청산 시작: 장 종료 10분 전
FINAL_BALANCE_CHECK_BEFORE_CLOSE_MIN = 2  # 최종 잔고 0 확인: 장 종료 2분 전


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """``weekday``: Monday=0..Sunday=6. ``n``: 1-indexed occurrence in the month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d = d + timedelta(days=offset + 7 * (n - 1))
    return d


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _observed(d: date) -> date:
    """Federal-holiday observance rule: Saturday -> preceding Friday, Sunday -> following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_market_holidays(year: int) -> set[date]:
    """NYSE full-closure holidays for ``year`` (standard, publicly documented
    NYSE holiday schedule — not sourced from a KIS API call, see module
    docstring)."""
    good_friday = easter(year) - timedelta(days=2)
    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),  # MLK Day: 3rd Monday of January
        _nth_weekday_of_month(year, 2, 0, 3),  # Washington's Birthday: 3rd Monday of February
        good_friday,
        _last_weekday_of_month(year, 5, 0),  # Memorial Day: last Monday of May
        _observed(date(year, 6, 19)),  # Juneteenth (observed since 2021)
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),  # Labor Day: 1st Monday of September
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving: 4th Thursday of November
        _observed(date(year, 12, 25)),  # Christmas
    }
    return holidays


def us_early_close_dates(year: int) -> set[date]:
    """Well-known NYSE early-close (13:00 ET) dates — the Friday after
    Thanksgiving, and Christmas Eve (Dec 24) when it falls on a weekday and is
    not itself a holiday. This is a conservative starting set (see module
    docstring) — extend only from confirmed KIS/NYSE data, never guessed."""
    thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)
    day_after_thanksgiving = thanksgiving + timedelta(days=1)
    christmas_eve = date(year, 12, 24)
    candidates = {day_after_thanksgiving, christmas_eve}
    holidays = us_market_holidays(year)
    return {d for d in candidates if d.weekday() < 5 and d not in holidays}


def is_us_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in us_market_holidays(d.year)


def market_close_time_et(d: date) -> time:
    """16:00 ET normally, 13:00 ET on a known early-close date."""
    if d in us_early_close_dates(d.year):
        return EARLY_CLOSE
    return REGULAR_CLOSE


def market_open_time_et(d: date) -> time:
    return REGULAR_OPEN


@dataclass(frozen=True)
class SessionBoundaries:
    trading_day: date
    market_open_et: datetime
    market_close_et: datetime
    new_entry_cutoff_et: datetime
    forced_liquidation_start_et: datetime
    final_balance_check_et: datetime
    is_early_close: bool


def session_boundaries(d: date) -> SessionBoundaries:
    """All session-relevant instants for calendar date ``d`` — cutoffs are
    always computed relative to that day's actual close (16:00, or 13:00 on
    an early-close day), never a hardcoded absolute time (docs §6)."""
    close_t = market_close_time_et(d)
    is_early = close_t == EARLY_CLOSE
    market_open = datetime.combine(d, market_open_time_et(d), tzinfo=ET)
    market_close = datetime.combine(d, close_t, tzinfo=ET)
    return SessionBoundaries(
        trading_day=d,
        market_open_et=market_open,
        market_close_et=market_close,
        new_entry_cutoff_et=market_close - timedelta(minutes=NEW_ENTRY_CUTOFF_BEFORE_CLOSE_MIN),
        forced_liquidation_start_et=market_close - timedelta(minutes=FORCED_LIQUIDATION_BEFORE_CLOSE_MIN),
        final_balance_check_et=market_close - timedelta(minutes=FINAL_BALANCE_CHECK_BEFORE_CLOSE_MIN),
        is_early_close=is_early,
    )


def now_et() -> datetime:
    return datetime.now(ET)


def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"to_et: naive datetime not allowed: {dt!r}")
    return dt.astimezone(ET)


def to_kst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"to_kst: naive datetime not allowed: {dt!r}")
    return dt.astimezone(KST)


def dual_timezone_iso(dt: datetime) -> dict[str, str]:
    """{'et': iso, 'kst': iso} — every user-facing time field must carry both
    (docs §6 UI 표시 요구사항)."""
    return {"et": to_et(dt).isoformat(), "kst": to_kst(dt).isoformat()}


def classify_session_status(now: Optional[datetime] = None) -> str:
    """"PREMARKET" | "REGULAR" | "AFTERMARKET" | "CLOSED" — CLOSED covers
    weekends, holidays, and outside 04:00-20:00 ET (approximate premarket/
    aftermarket bounds; only REGULAR is ever order-authoritative)."""
    now = to_et(now) if now is not None else now_et()
    d = now.date()
    if not is_us_trading_day(d):
        return "CLOSED"
    bounds = session_boundaries(d)
    t = now.time()
    if bounds.market_open_et.time() <= t < bounds.market_close_et.time():
        return "REGULAR"
    if time(4, 0) <= t < bounds.market_open_et.time():
        return "PREMARKET"
    if bounds.market_close_et.time() <= t < time(20, 0):
        return "AFTERMARKET"
    return "CLOSED"


def previous_us_trading_day(d: date, *, max_lookback_days: int = 10) -> Optional[date]:
    """Most recent US trading day strictly before ``d`` — bounded search so
    consecutive holidays can never loop forever (mirrors MACD2's
    market_data._prior_weekday_candidates bound)."""
    cursor = d
    for _ in range(max_lookback_days):
        cursor = cursor - timedelta(days=1)
        if is_us_trading_day(cursor):
            return cursor
    return None
