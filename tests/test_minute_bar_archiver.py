"""app.services.minute_bar_archiver -- safety-guard tests (per 2026-08-18
user requirement): date-mismatch never saved, partial fetch never saved
(no cross-symbol split files), duplicate/already-saved dates never
re-fetched, and every run is logged persistently."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.services import minute_bar_archiver as archiver


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(archiver, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(archiver, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(archiver, "ARCHIVE_LOG_PATH", tmp_path / "state" / "minute_bar_archive_log.json")
    yield


class _FakeClient:
    """candles_by_symbol: {symbol: list[dict]} -- a single page's worth,
    dated as if already filtered to the requested date (tests exercise
    fetch_full_day's date-mismatch filter directly at the candle level)."""

    def __init__(self, candles_by_symbol: dict[str, list[dict]], call_log: list[str] | None = None):
        self.candles_by_symbol = candles_by_symbol
        self.call_log = call_log if call_log is not None else []

    def get_minute_candles_for_date(self, symbol, date_ymd, period_min=1, count=120, hour1=""):
        self.call_log.append(f"{symbol}:{date_ymd}:{hour1}")
        if hour1:  # only ever return data on the first (cursor-less) page in these tests
            return []
        return self.candles_by_symbol.get(symbol, [])


def _candle(date_ymd: str, hhmmss: str, price: float) -> dict:
    return {"date": date_ymd, "time": hhmmss, "open": price, "high": price, "low": price, "close": price, "volume": 100}


def test_date_mismatch_is_never_saved():
    """KIS silently returning a DIFFERENT date's candles (observed on
    holidays) must never be written to that date's files."""
    requested = "20260817"
    wrong_date_candles = [_candle("20260814", "090000", 100.0)]
    client = _FakeClient({sym: wrong_date_candles for sym in archiver.SYMBOLS.values()})

    result = archiver.save_one_date(client, requested)

    assert result["status"].startswith("no_data")
    assert not archiver.is_complete(requested)
    assert not archiver.CACHE_DIR.exists() or list(archiver.CACHE_DIR.glob("*.csv")) == []


def test_partial_fetch_is_never_saved():
    """If even one of the 3 symbols comes back empty, NOTHING for that date
    is written -- never a 2-of-3 partial save."""
    date = "20260817"
    good = [_candle(date, "090000", 100.0)]
    candles = {archiver.SYMBOLS["hynix"]: good, archiver.SYMBOLS["long"]: good, archiver.SYMBOLS["inverse"]: []}
    client = _FakeClient(candles)

    result = archiver.save_one_date(client, date)

    assert result["status"] == "partial_fetch_not_saved"
    assert not archiver.is_complete(date)
    assert not (archiver.CACHE_DIR / f"replay_{date}_hynix_1m.csv").exists()
    assert not (archiver.CACHE_DIR / f"replay_{date}_long_1m.csv").exists()


def test_successful_fetch_saves_all_three_and_is_idempotent():
    date = "20260817"
    good = [_candle(date, "090000", 100.0), _candle(date, "090100", 101.0)]
    call_log: list[str] = []
    client = _FakeClient({sym: good for sym in archiver.SYMBOLS.values()}, call_log=call_log)

    result = archiver.save_one_date(client, date)
    assert result["status"] == "saved"
    assert archiver.is_complete(date)
    df = pd.read_csv(archiver.CACHE_DIR / f"replay_{date}_hynix_1m.csv")
    assert list(df.columns) == ["datetime", "open", "high", "low", "close", "volume"]
    assert len(df) == 2

    calls_before = len(call_log)
    result2 = archiver.save_one_date(client, date)
    assert result2["status"] == "already_saved"
    assert len(call_log) == calls_before  # no re-fetch for an already-saved date


def test_run_archive_persists_a_log_entry_per_date():
    date = "20260817"
    good = [_candle(date, "090000", 100.0)]
    client = _FakeClient({sym: good for sym in archiver.SYMBOLS.values()})

    results = archiver.run_archive(client, [date], source="unit_test")

    assert results[0]["status"] == "saved"
    assert archiver.ARCHIVE_LOG_PATH.exists()
    log = json.loads(archiver.ARCHIVE_LOG_PATH.read_text(encoding="utf-8"))
    assert log[-1]["source"] == "unit_test"
    assert log[-1]["result"]["date"] == date
    assert log[-1]["result"]["status"] == "saved"


def test_candidate_dates_skips_weekends_and_excludes_today_before_close():
    import datetime as _dt

    monday_before_close = _dt.datetime(2026, 8, 17, 10, 0, tzinfo=archiver.KST)  # a Monday
    dates = archiver.candidate_dates(now=monday_before_close)
    assert dates[-1] != "20260817"  # today excluded -- market not closed yet
    assert all(_dt.datetime.strptime(d, "%Y%m%d").weekday() < 5 for d in dates)

    monday_after_close = _dt.datetime(2026, 8, 17, 16, 30, tzinfo=archiver.KST)
    dates_after = archiver.candidate_dates(now=monday_after_close)
    assert dates_after[-1] == "20260817"  # today included once market has closed
