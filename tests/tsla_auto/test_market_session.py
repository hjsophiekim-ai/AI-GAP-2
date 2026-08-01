"""Unit tests for app.trading.tsla_auto.market_session."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.trading.tsla_auto import market_session as ms


def test_regular_day_boundaries_match_spec():
    b = ms.session_boundaries(date(2026, 7, 30))  # a normal Thursday
    assert b.market_open_et.time().isoformat() == "09:30:00"
    assert b.market_close_et.time().isoformat() == "16:00:00"
    assert b.new_entry_cutoff_et.time().isoformat() == "15:45:00"
    assert b.forced_liquidation_start_et.time().isoformat() == "15:50:00"
    assert b.final_balance_check_et.time().isoformat() == "15:58:00"
    assert b.is_early_close is False


def test_early_close_day_scales_cutoffs_proportionally():
    early_dates = sorted(ms.us_early_close_dates(2026))
    assert early_dates  # at least one known early-close date exists
    b = ms.session_boundaries(early_dates[0])
    assert b.is_early_close is True
    assert b.market_close_et.time().isoformat() == "13:00:00"
    assert b.new_entry_cutoff_et.time().isoformat() == "12:45:00"
    assert b.forced_liquidation_start_et.time().isoformat() == "12:50:00"
    assert b.final_balance_check_et.time().isoformat() == "12:58:00"


def test_weekend_and_holiday_are_not_trading_days():
    assert ms.is_us_trading_day(date(2026, 8, 1)) is False  # Saturday
    assert ms.is_us_trading_day(date(2026, 8, 2)) is False  # Sunday
    assert ms.is_us_trading_day(date(2026, 1, 1)) is False  # New Year's Day
    assert ms.is_us_trading_day(date(2026, 7, 4)) is False  # Independence Day (Saturday, observed Friday)
    assert ms.is_us_trading_day(date(2026, 7, 3)) is False  # observed Independence Day


def test_regular_weekday_is_a_trading_day():
    assert ms.is_us_trading_day(date(2026, 7, 30)) is True


def test_previous_us_trading_day_skips_weekend_and_holiday():
    # 2026-01-19 is MLK Day (3rd Monday of January) -> previous trading day is the prior Friday.
    prev = ms.previous_us_trading_day(date(2026, 1, 19))
    assert prev == date(2026, 1, 16)


def test_classify_session_status_closed_on_weekend():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=ms.ET)  # Saturday noon ET
    assert ms.classify_session_status(now) == "CLOSED"


def test_classify_session_status_regular_during_market_hours():
    now = datetime(2026, 7, 30, 12, 0, tzinfo=ms.ET)
    assert ms.classify_session_status(now) == "REGULAR"


def test_market_state_summer_regular_day_kst_cutoffs():
    state = ms.get_us_market_state(datetime(2026, 8, 3, 10, 0, tzinfo=ms.ET))
    assert state.phase == ms.USMarketPhase.REGULAR_ENTRY
    assert state.entry_allowed is True
    assert state.is_dst is True
    assert state.timezone_abbr == "EDT"
    assert state.session_open_kst.strftime("%H:%M") == "22:30"
    assert state.session_close_kst.strftime("%H:%M") == "05:00"
    assert state.entry_block_at_kst.strftime("%H:%M") == "04:45"
    assert state.liquidation_at_kst.strftime("%H:%M") == "04:50"


def test_market_state_winter_regular_day_kst_cutoffs():
    state = ms.get_us_market_state(datetime(2026, 12, 1, 10, 0, tzinfo=ms.ET))
    assert state.phase == ms.USMarketPhase.REGULAR_ENTRY
    assert state.entry_allowed is True
    assert state.is_dst is False
    assert state.timezone_abbr == "EST"
    assert state.session_open_kst.strftime("%H:%M") == "23:30"
    assert state.session_close_kst.strftime("%H:%M") == "06:00"
    assert state.entry_block_at_kst.strftime("%H:%M") == "05:45"
    assert state.liquidation_at_kst.strftime("%H:%M") == "05:50"


def test_market_state_dst_transition_dates_keep_et_open_fixed():
    cases = [
        (date(2026, 3, 6), "EST", "23:30"),
        (date(2026, 3, 9), "EDT", "22:30"),
        (date(2026, 10, 30), "EDT", "22:30"),
        (date(2026, 11, 2), "EST", "23:30"),
    ]
    for d, abbr, kst_open in cases:
        state = ms.get_us_market_state(datetime.combine(d, ms.REGULAR_OPEN, tzinfo=ms.ET))
        assert state.session_open_et.time().isoformat() == "09:30:00"
        assert state.timezone_abbr == abbr
        assert state.session_open_kst.strftime("%H:%M") == kst_open


def test_market_state_phase_boundaries_are_half_open():
    d = date(2026, 8, 3)
    b = ms.session_boundaries(d)
    assert ms.get_us_market_state(b.market_open_et - timedelta(seconds=1)).phase == ms.USMarketPhase.PRE_MARKET
    assert ms.get_us_market_state(b.market_open_et).phase == ms.USMarketPhase.REGULAR_ENTRY
    assert ms.get_us_market_state(b.new_entry_cutoff_et - timedelta(seconds=1)).entry_allowed is True
    assert ms.get_us_market_state(b.new_entry_cutoff_et).phase == ms.USMarketPhase.ENTRY_BLOCKED
    assert ms.get_us_market_state(b.forced_liquidation_start_et - timedelta(seconds=1)).phase == ms.USMarketPhase.ENTRY_BLOCKED
    assert ms.get_us_market_state(b.forced_liquidation_start_et).phase == ms.USMarketPhase.FORCE_LIQUIDATION
    assert ms.get_us_market_state(b.market_close_et).phase == ms.USMarketPhase.AFTER_MARKET


def test_market_state_holiday_weekend_and_calendar_unavailable(monkeypatch):
    holiday = ms.get_us_market_state(datetime(2026, 1, 1, 12, 0, tzinfo=ms.ET))
    assert holiday.phase == ms.USMarketPhase.HOLIDAY
    assert holiday.entry_allowed is False
    weekend = ms.get_us_market_state(datetime(2026, 8, 1, 12, 0, tzinfo=ms.ET))
    assert weekend.phase == ms.USMarketPhase.WEEKEND
    assert weekend.entry_allowed is False
    monkeypatch.setattr(ms, "_session_row", lambda d: (_ for _ in ()).throw(RuntimeError("calendar down")))
    down = ms.get_us_market_state(datetime(2026, 8, 3, 12, 0, tzinfo=ms.ET))
    assert down.phase == ms.USMarketPhase.CALENDAR_UNAVAILABLE
    assert down.entry_allowed is False


def test_market_state_early_close_uses_actual_close():
    early = sorted(ms.us_early_close_dates(2026))[0]
    b = ms.session_boundaries(early)
    assert b.market_close_et.time().isoformat() == "13:00:00"
    assert ms.get_us_market_state(b.new_entry_cutoff_et).phase == ms.USMarketPhase.ENTRY_BLOCKED
    assert ms.get_us_market_state(b.forced_liquidation_start_et).phase == ms.USMarketPhase.FORCE_LIQUIDATION


def test_classify_session_status_premarket_and_aftermarket():
    premarket = datetime(2026, 7, 30, 8, 0, tzinfo=ms.ET)
    aftermarket = datetime(2026, 7, 30, 17, 0, tzinfo=ms.ET)
    assert ms.classify_session_status(premarket) == "PREMARKET"
    assert ms.classify_session_status(aftermarket) == "AFTERMARKET"


def test_dst_transition_does_not_break_session_boundaries():
    # 2026-03-08 is a DST "spring forward" Sunday in the US; the following
    # Monday 2026-03-09 is the first trading day under the new offset.
    b = ms.session_boundaries(date(2026, 3, 9))
    assert b.market_open_et.time().isoformat() == "09:30:00"
    assert b.market_close_et.time().isoformat() == "16:00:00"


def test_dual_timezone_iso_returns_et_and_kst():
    dt = datetime(2026, 7, 30, 10, 0, tzinfo=ms.ET)
    out = ms.dual_timezone_iso(dt)
    assert set(out.keys()) == {"et", "kst"}
    assert "-04:00" in out["et"] or "-05:00" in out["et"]  # ET offset (EDT/EST)
    assert "+09:00" in out["kst"]


def test_to_et_and_to_kst_reject_naive_datetime():
    naive = datetime(2026, 7, 30, 10, 0)
    try:
        ms.to_et(naive)
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        ms.to_kst(naive)
        assert False, "expected ValueError"
    except ValueError:
        pass
