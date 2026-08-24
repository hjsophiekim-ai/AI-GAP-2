"""Unit tests for app.services.macd2_daily_archiver — fake KIS client only,
never a real network call. Written for the 2026-08-24 research-archive
verification pass (see MEMORY / verification task): confirms the archiver
(a) never raises even when the fake client's fetch raises for one symbol,
(b) is idempotent on merge (no duplicate rows across two runs), and
(c) always writes a _manifest.json with a top-level "sections" key.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config
from app.services import macd2_daily_archiver as archiver
import scripts.run_tw2_today as run_tw2

KST = config.KST
DATE_YMD = "20260819"  # a Wednesday -- arbitrary, no real trading data needed


def _bars_1m(date_ymd: str, start_hm: tuple[int, int], n_minutes: int, base_price: float) -> pd.DataFrame:
    start = datetime.strptime(date_ymd, "%Y%m%d").replace(
        hour=start_hm[0], minute=start_hm[1], tzinfo=KST
    )
    rows = []
    for i in range(n_minutes):
        dt = start + timedelta(minutes=i)
        px = base_price + (i % 5) * 0.1
        rows.append({
            "datetime": dt, "open": px, "high": px + 0.2, "low": px - 0.2,
            "close": px, "volume": 100 + i,
        })
    return pd.DataFrame(rows)


class _DummyClient:
    """Never used for a real network call -- fetch_full_day and
    find_prior_trading_day are monkeypatched below to canned data, so this
    object only needs to exist as a placeholder `client` argument."""


def _install_fake_fetchers(monkeypatch, *, hynix_raises: bool = False):
    canned = {
        config.WATCH_SYMBOL: _bars_1m(DATE_YMD, (9, 0), 30, 100.0),
        config.LONG_SYMBOL: _bars_1m(DATE_YMD, (9, 0), 30, 10000.0),
        config.INVERSE_SYMBOL: _bars_1m(DATE_YMD, (9, 0), 30, 5000.0),
    }

    def fake_fetch_full_day(client, symbol, date_ymd):
        del client
        if hynix_raises and symbol == config.WATCH_SYMBOL:
            raise RuntimeError("simulated KIS fetch failure for hynix")
        return canned.get(symbol, pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])).copy()

    monkeypatch.setattr(archiver, "fetch_full_day", fake_fetch_full_day)
    monkeypatch.setattr(
        run_tw2, "find_prior_trading_day",
        lambda client, date_ymd, max_lookback=10: (None, pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])),
    )
    return canned


def test_run_daily_archive_never_raises_when_one_symbol_fetch_fails(tmp_path, monkeypatch):
    _install_fake_fetchers(monkeypatch, hynix_raises=True)
    client = _DummyClient()

    manifest = archiver.run_daily_archive(client, DATE_YMD, out_root=tmp_path)

    assert manifest["sections"]["hynix_1m"]["status"] == "FETCH_ERROR"
    # the failure in one section must not stop the rest of the run, and must
    # never propagate as an exception out of run_daily_archive.
    assert manifest["sections"]["long_1m"]["status"] == "SAVED"
    assert manifest["sections"]["inverse_1m"]["status"] == "SAVED"
    assert (tmp_path / DATE_YMD / "_manifest.json").exists()


def test_run_daily_archive_idempotent_merge_no_duplicate_rows(tmp_path, monkeypatch):
    _install_fake_fetchers(monkeypatch, hynix_raises=False)
    client = _DummyClient()

    archiver.run_daily_archive(client, DATE_YMD, out_root=tmp_path)
    archiver.run_daily_archive(client, DATE_YMD, out_root=tmp_path)

    for tag in ("hynix", "long", "inverse"):
        path = tmp_path / DATE_YMD / f"{tag}_1m.parquet"
        assert path.exists()
        df = pd.read_parquet(path)
        assert df["datetime"].duplicated().sum() == 0
        # 30 canned 1-minute bars in, 30 rows out -- re-running with the same
        # fetched data must not duplicate or drop rows.
        assert len(df) == 30


def test_run_daily_archive_always_writes_manifest_with_sections_key(tmp_path, monkeypatch):
    _install_fake_fetchers(monkeypatch, hynix_raises=False)
    client = _DummyClient()

    manifest = archiver.run_daily_archive(client, DATE_YMD, out_root=tmp_path)

    manifest_path = tmp_path / DATE_YMD / "_manifest.json"
    assert manifest_path.exists()
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "sections" in on_disk
    assert "sections" in manifest
    assert set(on_disk["sections"].keys()) >= {"hynix_1m", "long_1m", "inverse_1m", "mu_1m"}


def test_run_daily_archive_never_raises_when_all_fetches_raise(tmp_path, monkeypatch):
    def always_raise(client, symbol, date_ymd):
        raise RuntimeError("simulated total KIS outage")

    monkeypatch.setattr(archiver, "fetch_full_day", always_raise)
    client = _DummyClient()

    manifest = archiver.run_daily_archive(client, DATE_YMD, out_root=tmp_path)

    assert manifest["sections"]["hynix_1m"]["status"] == "FETCH_ERROR"
    assert manifest["sections"]["long_1m"]["status"] == "FETCH_ERROR"
    assert manifest["sections"]["inverse_1m"]["status"] == "FETCH_ERROR"
    assert manifest["sections"]["flags_and_tw"]["status"] == "SKIPPED_MISSING_1M_DATA"
    assert (tmp_path / DATE_YMD / "_manifest.json").exists()
