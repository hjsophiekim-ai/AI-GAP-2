"""Unit tests for app.trading.tsla_auto.market_session."""
from __future__ import annotations

from datetime import date, datetime

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
