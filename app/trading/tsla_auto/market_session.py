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

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil.easter import easter
import pandas as pd
import pandas_market_calendars as mcal

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


class USMarketPhase(str, Enum):
    HOLIDAY = "HOLIDAY"
    WEEKEND = "WEEKEND"
    PRE_MARKET = "PRE_MARKET"
    REGULAR_ENTRY = "REGULAR_ENTRY"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    FORCE_LIQUIDATION = "FORCE_LIQUIDATION"
    AFTER_MARKET = "AFTER_MARKET"
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"


@dataclass(frozen=True)
class USMarketSessionState:
    checked_at_utc: datetime
    checked_at_et: datetime
    checked_at_kst: datetime
    phase: USMarketPhase
    is_trading_day: bool
    is_holiday: bool
    is_weekend: bool
    is_early_close: bool
    is_dst: bool
    timezone_abbr: str
    utc_offset: str
    session_date_et: Optional[date]
    session_open_et: Optional[datetime]
    session_close_et: Optional[datetime]
    session_open_kst: Optional[datetime]
    session_close_kst: Optional[datetime]
    entry_block_at_et: Optional[datetime]
    entry_block_at_kst: Optional[datetime]
    liquidation_at_et: Optional[datetime]
    liquidation_at_kst: Optional[datetime]
    next_open_et: Optional[datetime]
    next_open_kst: Optional[datetime]
    entry_allowed: bool
    liquidation_required: bool
    reason_code: str
    reason_text_ko: str
    holiday_name: Optional[str] = None
    seconds_to_next_transition: Optional[int] = None

    def to_dict(self) -> dict:
        out = asdict(self)
        out["phase"] = self.phase.value
        for key, value in list(out.items()):
            if isinstance(value, (datetime, date)):
                out[key] = value.isoformat()
        return out


MARKET_PREMARKET_BLOCK = "MARKET_PREMARKET_BLOCK"
MARKET_AFTER_HOURS_BLOCK = "MARKET_AFTER_HOURS_BLOCK"
MARKET_HOLIDAY_BLOCK = "MARKET_HOLIDAY_BLOCK"
MARKET_WEEKEND_BLOCK = "MARKET_WEEKEND_BLOCK"
MARKET_ENTRY_CUTOFF_BLOCK = "MARKET_ENTRY_CUTOFF_BLOCK"
MARKET_LIQUIDATION_BLOCK = "MARKET_LIQUIDATION_BLOCK"
MARKET_CALENDAR_UNAVAILABLE_BLOCK = "MARKET_CALENDAR_UNAVAILABLE_BLOCK"

_NYSE = mcal.get_calendar("NYSE")


def _require_aware(dt: datetime, label: str) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{label}: naive datetime not allowed: {dt!r}")
    return dt


def _utc_offset_label(dt: datetime) -> str:
    offset = dt.utcoffset()
    if offset is None:
        return "UTC?"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _schedule(start: date, end: date) -> pd.DataFrame:
    return _NYSE.schedule(start_date=start.isoformat(), end_date=end.isoformat())


def _session_row(d: date) -> Optional[pd.Series]:
    sched = _schedule(d, d)
    if sched.empty:
        return None
    return sched.iloc[0]


def _next_session_open(after: datetime, *, max_days: int = 14) -> Optional[datetime]:
    after_et = _require_aware(after, "next_session_open(after)").astimezone(ET)
    sched = _schedule(after_et.date(), after_et.date() + timedelta(days=max_days))
    for _, row in sched.iterrows():
        open_et = pd.Timestamp(row["market_open"]).to_pydatetime().astimezone(ET)
        if open_et > after_et:
            return open_et
    return None


def is_us_trading_day(d: date) -> bool:  # type: ignore[no-redef]
    return _session_row(d) is not None


def us_market_holidays(year: int) -> set[date]:  # type: ignore[no-redef]
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    trading = {pd.Timestamp(idx).date() for idx in _schedule(start, end).index}
    weekdays = {start + timedelta(days=i) for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5}
    return weekdays - trading


def us_early_close_dates(year: int) -> set[date]:  # type: ignore[no-redef]
    sched = _schedule(date(year, 1, 1), date(year, 12, 31))
    out: set[date] = set()
    for idx, row in sched.iterrows():
        close_et = pd.Timestamp(row["market_close"]).to_pydatetime().astimezone(ET)
        if close_et.time() < REGULAR_CLOSE:
            out.add(pd.Timestamp(idx).date())
    return out


def _calendar_boundaries(d: date) -> Optional[SessionBoundaries]:
    row = _session_row(d)
    if row is None:
        return None
    open_et = pd.Timestamp(row["market_open"]).to_pydatetime().astimezone(ET)
    close_et = pd.Timestamp(row["market_close"]).to_pydatetime().astimezone(ET)
    return SessionBoundaries(
        trading_day=d,
        market_open_et=open_et,
        market_close_et=close_et,
        new_entry_cutoff_et=close_et - timedelta(minutes=NEW_ENTRY_CUTOFF_BEFORE_CLOSE_MIN),
        forced_liquidation_start_et=close_et - timedelta(minutes=FORCED_LIQUIDATION_BEFORE_CLOSE_MIN),
        final_balance_check_et=close_et - timedelta(minutes=FINAL_BALANCE_CHECK_BEFORE_CLOSE_MIN),
        is_early_close=close_et.time() < REGULAR_CLOSE,
    )


def session_boundaries(d: date) -> SessionBoundaries:  # type: ignore[no-redef]
    bounds = _calendar_boundaries(d)
    if bounds is None:
        raise ValueError(f"no NYSE/Nasdaq regular session for {d.isoformat()}")
    return bounds


def get_us_market_state(now: Optional[datetime] = None) -> USMarketSessionState:
    """Single authoritative TSLA_AUTO US market status.

    All callers must use this object rather than re-implementing time windows.
    Calendar failures fail closed instead of allowing new entries.
    """
    now_utc = datetime.now(ZoneInfo("UTC")) if now is None else _require_aware(now, "get_us_market_state(now)").astimezone(ZoneInfo("UTC"))
    now_et_ = now_utc.astimezone(ET)
    now_kst = now_utc.astimezone(KST)
    tz_abbr = now_et_.tzname() or "ET"
    common = {
        "checked_at_utc": now_utc,
        "checked_at_et": now_et_,
        "checked_at_kst": now_kst,
        "is_dst": bool(now_et_.dst() and now_et_.dst().total_seconds() != 0),
        "timezone_abbr": tz_abbr,
        "utc_offset": _utc_offset_label(now_et_),
    }
    try:
        row = _session_row(now_et_.date())
        next_open = _next_session_open(now_et_)
    except Exception as exc:
        return USMarketSessionState(
            **common,
            phase=USMarketPhase.CALENDAR_UNAVAILABLE,
            is_trading_day=False,
            is_holiday=False,
            is_weekend=False,
            is_early_close=False,
            session_date_et=None,
            session_open_et=None,
            session_close_et=None,
            session_open_kst=None,
            session_close_kst=None,
            entry_block_at_et=None,
            entry_block_at_kst=None,
            liquidation_at_et=None,
            liquidation_at_kst=None,
            next_open_et=None,
            next_open_kst=None,
            entry_allowed=False,
            liquidation_required=False,
            reason_code=MARKET_CALENDAR_UNAVAILABLE_BLOCK,
            reason_text_ko=f"미국 장 운영상태 확인 실패 - 안전상 신규진입 차단 ({exc})",
            seconds_to_next_transition=None,
        )

    is_weekend = now_et_.weekday() >= 5
    if row is None:
        phase = USMarketPhase.WEEKEND if is_weekend else USMarketPhase.HOLIDAY
        reason = MARKET_WEEKEND_BLOCK if is_weekend else MARKET_HOLIDAY_BLOCK
        text = "미국 증시 주말 휴장 - 신규진입 차단" if is_weekend else "미국 증시 휴장일 - 신규진입 차단"
        delta = int((next_open - now_et_).total_seconds()) if next_open else None
        return USMarketSessionState(
            **common,
            phase=phase,
            is_trading_day=False,
            is_holiday=not is_weekend,
            is_weekend=is_weekend,
            is_early_close=False,
            session_date_et=None,
            session_open_et=None,
            session_close_et=None,
            session_open_kst=None,
            session_close_kst=None,
            entry_block_at_et=None,
            entry_block_at_kst=None,
            liquidation_at_et=None,
            liquidation_at_kst=None,
            next_open_et=next_open,
            next_open_kst=next_open.astimezone(KST) if next_open else None,
            entry_allowed=False,
            liquidation_required=False,
            reason_code=reason,
            reason_text_ko=text,
            seconds_to_next_transition=delta,
        )

    bounds = session_boundaries(now_et_.date())
    entry_block = bounds.new_entry_cutoff_et
    liquidation = bounds.forced_liquidation_start_et
    if now_et_ < bounds.market_open_et:
        phase = USMarketPhase.PRE_MARKET
        allowed = False
        liquidation_required = False
        reason = MARKET_PREMARKET_BLOCK
        text = "프리마켓 - 신규진입 차단"
        next_transition = bounds.market_open_et
    elif bounds.market_open_et <= now_et_ < entry_block:
        phase = USMarketPhase.REGULAR_ENTRY
        allowed = True
        liquidation_required = False
        reason = "MARKET_REGULAR_ENTRY"
        text = "미국 정규장 - 신규진입 가능"
        next_transition = entry_block
    elif entry_block <= now_et_ < liquidation:
        phase = USMarketPhase.ENTRY_BLOCKED
        allowed = False
        liquidation_required = False
        reason = MARKET_ENTRY_CUTOFF_BLOCK
        text = "정규장 종료 15분 전 - 신규진입 차단"
        next_transition = liquidation
    elif liquidation <= now_et_ < bounds.market_close_et:
        phase = USMarketPhase.FORCE_LIQUIDATION
        allowed = False
        liquidation_required = True
        reason = MARKET_LIQUIDATION_BLOCK
        text = "정규장 종료 10분 전 - 전 종목 강제청산"
        next_transition = bounds.market_close_et
    else:
        phase = USMarketPhase.AFTER_MARKET
        allowed = False
        liquidation_required = False
        reason = MARKET_AFTER_HOURS_BLOCK
        text = "애프터마켓 - 신규진입 차단"
        next_transition = next_open
    return USMarketSessionState(
        **common,
        phase=phase,
        is_trading_day=True,
        is_holiday=False,
        is_weekend=False,
        is_early_close=bounds.is_early_close,
        session_date_et=bounds.trading_day,
        session_open_et=bounds.market_open_et,
        session_close_et=bounds.market_close_et,
        session_open_kst=bounds.market_open_et.astimezone(KST),
        session_close_kst=bounds.market_close_et.astimezone(KST),
        entry_block_at_et=entry_block,
        entry_block_at_kst=entry_block.astimezone(KST),
        liquidation_at_et=liquidation,
        liquidation_at_kst=liquidation.astimezone(KST),
        next_open_et=next_open,
        next_open_kst=next_open.astimezone(KST) if next_open else None,
        entry_allowed=allowed,
        liquidation_required=liquidation_required,
        reason_code=reason,
        reason_text_ko=text,
        seconds_to_next_transition=int((next_transition - now_et_).total_seconds()) if next_transition else None,
    )


def classify_session_status(now: Optional[datetime] = None) -> str:  # type: ignore[no-redef]
    state = get_us_market_state(now)
    if state.phase == USMarketPhase.REGULAR_ENTRY:
        return "REGULAR"
    if state.phase == USMarketPhase.PRE_MARKET:
        return "PREMARKET"
    if state.phase == USMarketPhase.AFTER_MARKET:
        return "AFTERMARKET"
    return "CLOSED"
