"""app.services.tw_shadow_forward_test -- read-only shadow forward-test that
replays each new archived trading day through the current-production T+3
baseline AND the 09:00-10:00-immediate-entry hybrid research candidate,
without ever touching real trading state/ledgers/orders. Verifies: dates
without a complete 1m archive are skipped (never fabricated), a recorded
date is never re-simulated (idempotent), compare_accumulated() withholds a
verdict until min_days is reached, and the 09:00-10:00 slice filters by the
KST wall-clock entry time actually recorded.
"""
from __future__ import annotations

import pytest

from app.services import tw_shadow_forward_test as shadow


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "SHADOW_LOG_PATH", tmp_path / "tw_shadow_forward_test_log.json")
    yield


def test_run_for_date_returns_none_when_archive_incomplete():
    """A date with no (or partial) 1m archive must never be fabricated --
    real data/cache is used as-is (read-only), so a date far outside the
    archived range is guaranteed incomplete."""
    result = shadow.run_for_date("20200101")
    assert result is None
    assert shadow.recorded_dates() == set()


def test_run_for_date_records_real_archived_day_and_is_idempotent():
    """20260814 is a real already-archived trading day in data/cache (used
    throughout this session's TW-filter backtests) -- replays both
    evaluators and records exactly one log entry; a second call is a
    cache-hit, not a re-simulation (log stays at length 1)."""
    first = shadow.run_for_date("20260814")
    assert first is not None
    assert first["date"] == "20260814"
    assert isinstance(first["baseline_trades"], list)
    assert isinstance(first["hybrid_trades"], list)
    assert shadow.recorded_dates() == {"20260814"}

    second = shadow.run_for_date("20260814")
    # second is the cache-hit branch's in-memory dict (never round-tripped
    # through JSON), first's own legs are tuples that a real reload would
    # come back as lists -- compare recorded_at (only set once) instead of
    # deep dict equality, which is what actually proves no re-simulation ran.
    assert second["recorded_at"] == first["recorded_at"]
    assert len(shadow._load_log()) == 1  # not duplicated


def test_run_pending_skips_incomplete_and_already_recorded(monkeypatch):
    calls = []

    def _fake_candidate_dates(explicit=None, *, now=None):
        return ["20260812", "20260813", "20260814"]

    def _fake_run_for_date(date_ymd):
        calls.append(date_ymd)
        return {"date": date_ymd, "baseline_trades": [], "hybrid_trades": []}

    monkeypatch.setattr("app.services.minute_bar_archiver.candidate_dates", _fake_candidate_dates)
    monkeypatch.setattr(shadow, "run_for_date", _fake_run_for_date)
    # Pre-seed one date as already recorded -- run_pending must not re-run it.
    shadow._save_log([{"date": "20260812", "baseline_trades": [], "hybrid_trades": []}])

    newly = shadow.run_pending()

    assert "20260812" not in calls  # already recorded -- skipped before calling run_for_date
    assert set(calls) == {"20260813", "20260814"}
    assert set(newly) == {"20260813", "20260814"}


def _fake_trade(entry_time: str, net_return_pct: float, window: str = "W1_MORNING_AGGRESSIVE"):
    return {
        "trading_date": entry_time[:10].replace("-", ""), "direction": "UP_RED", "flag_time": entry_time,
        "entry_time": entry_time, "entry_symbol": "0193T0", "entry_price": 1000.0,
        "window": window, "quality_score": 4, "flag_seq_of_day": 1,
        "tp1_hit": False, "tp2_hit": False, "exit_time": entry_time, "exit_price": 1010.0,
        "exit_reason": "TIME_WINDOW_TP2_FULL", "net_return_pct": net_return_pct, "legs": [[1.0, 1010.0, "TIME_WINDOW_TP2_FULL"]],
    }


def test_compare_accumulated_withholds_verdict_until_min_days():
    entries = [
        {"date": f"2026080{i}", "baseline_trades": [_fake_trade(f"2026-08-0{i}T09:30:00+09:00", 1.0)], "hybrid_trades": [_fake_trade(f"2026-08-0{i}T09:15:00+09:00", 1.5)]}
        for i in range(1, 6)
    ]
    shadow._save_log(entries)

    assert shadow.compare_accumulated(min_days=20) is None
    result = shadow.compare_accumulated(min_days=5)
    assert result is not None
    assert result["days_recorded"] == 5
    assert result["baseline"]["total_entries"] == 5
    assert result["hybrid"]["total_entries"] == 5


def test_compare_accumulated_0900_1000_slice_filters_by_entry_time():
    entries = [
        {
            "date": "20260801",
            "baseline_trades": [
                _fake_trade("2026-08-01T09:30:00+09:00", 2.0),  # inside 09:00-10:00
                _fake_trade("2026-08-01T11:00:00+09:00", -1.0),  # outside
            ],
            "hybrid_trades": [
                _fake_trade("2026-08-01T09:10:00+09:00", 3.0),  # inside
            ],
        },
    ]
    shadow._save_log(entries)

    result = shadow.compare_accumulated(min_days=1)
    assert result["baseline"]["total_entries"] == 2
    assert result["baseline_0900_1000_only"]["total_entries"] == 1
    assert result["hybrid_0900_1000_only"]["total_entries"] == 1


def test_is_0900_1000_entry_boundary():
    import scripts.tw_gate_relaxed_optimization as base

    inside = base.Trade(**_fake_trade("2026-08-01T09:59:00+09:00", 1.0))
    at_boundary = base.Trade(**_fake_trade("2026-08-01T10:00:00+09:00", 1.0))
    before_open = base.Trade(**_fake_trade("2026-08-01T08:59:00+09:00", 1.0))

    assert shadow._is_0900_1000_entry(inside) is True
    assert shadow._is_0900_1000_entry(at_boundary) is False
    assert shadow._is_0900_1000_entry(before_open) is False
