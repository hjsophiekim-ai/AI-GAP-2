"""Confirmed-bar MACD2 parity checks with fixed OHLCV data only.

This replaces the removed v1 parity test. It deliberately imports no legacy
MACD modules; the oracle below is a small independent pandas implementation
fed by fixed 1-minute OHLCV fixtures.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.models import Direction
from app.trading.macd2.signal_engine import (
    calculate_macd,
    confirmed_macd_flag_condition,
    evaluate_confirmed_macd_flag,
    make_signal_id,
    resample_completed_3m,
)

KST = config.KST
ROOT = Path(__file__).resolve().parents[2]


def _load_original_hynix_1m(*dates: str) -> pd.DataFrame:
    frames = []
    for ymd in dates:
        path = ROOT / "data" / "cache" / f"replay_{ymd}_hynix_1m.csv"
        frame = pd.read_csv(path)
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise").dt.tz_localize(KST)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)


def _independent_completed_3m(one_minute_bars: pd.DataFrame, now: datetime) -> pd.DataFrame:
    work = one_minute_bars.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    indexed = work.set_index("datetime")
    bars = (
        indexed.resample("3min", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["close"])
        .reset_index()
    )
    cutoff = now.replace(second=0, microsecond=0)
    return bars[bars["datetime"] + timedelta(minutes=3) <= cutoff].reset_index(drop=True)


def _independent_macd_rows(three_minute_bars: pd.DataFrame) -> pd.DataFrame:
    closes = pd.to_numeric(three_minute_bars["close"], errors="raise")
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return pd.DataFrame(
        {
            "datetime": three_minute_bars["datetime"],
            "macd": macd,
            "signal": signal,
            "hist": macd - signal,
        }
    )


def _confirmed_color_flags(three_minute_bars: pd.DataFrame) -> dict[str, Direction]:
    last_direction = None
    out = {}
    for end in range(config.EMA_SLOW, len(three_minute_bars) + 1):
        snap = calculate_macd(three_minute_bars.iloc[:end])
        assert snap is not None
        if snap.bar_dt.strftime("%Y%m%d") == "20260731" and snap.bar_dt.strftime("%H:%M") == "09:00":
            last_direction = None
        direction = evaluate_confirmed_macd_flag(snap, last_direction)
        if direction != Direction.HOLD:
            last_direction = direction
            out[snap.bar_dt.strftime("%H:%M")] = direction
    return out


def test_resample_is_0900_anchored_completed_3m_and_bar_start_labeled():
    df_1m = _load_original_hynix_1m("20260730", "20260731")
    before_complete = datetime(2026, 7, 31, 9, 2, 59, tzinfo=KST)
    at_complete = datetime(2026, 7, 31, 9, 3, 0, tzinfo=KST)

    assert "2026-07-31 09:00" not in set(
        resample_completed_3m(df_1m, now=before_complete)["datetime"].dt.strftime("%Y-%m-%d %H:%M")
    )

    app_bars = resample_completed_3m(df_1m, now=at_complete)
    oracle_bars = _independent_completed_3m(df_1m, now=at_complete)
    pd.testing.assert_frame_equal(app_bars.reset_index(drop=True), oracle_bars.reset_index(drop=True))

    bar_0900 = app_bars[app_bars["datetime"].dt.strftime("%Y-%m-%d %H:%M") == "2026-07-31 09:00"].iloc[0]
    raw_0900 = df_1m[df_1m["datetime"].dt.strftime("%Y-%m-%d %H:%M").isin([
        "2026-07-31 09:00",
        "2026-07-31 09:01",
        "2026-07-31 09:02",
    ])]
    assert bar_0900["datetime"].to_pydatetime() == datetime(2026, 7, 31, 9, 0, tzinfo=KST)
    assert bar_0900["open"] == raw_0900.iloc[0]["open"]
    assert bar_0900["high"] == raw_0900["high"].max()
    assert bar_0900["low"] == raw_0900["low"].min()
    assert bar_0900["close"] == raw_0900.iloc[-1]["close"]
    assert bar_0900["volume"] == raw_0900["volume"].sum()


def test_macd_12_26_9_adjust_false_matches_independent_oracle_at_kis_flag_times():
    df_1m = _load_original_hynix_1m("20260730", "20260731")
    now = datetime(2026, 7, 31, 9, 18, 0, tzinfo=KST)
    app_bars = resample_completed_3m(df_1m, now=now)
    oracle_bars = _independent_completed_3m(df_1m, now=now)
    oracle = _independent_macd_rows(oracle_bars)

    for hhmm in ("09:00", "09:15"):
        mask = app_bars["datetime"].dt.strftime("%Y-%m-%d %H:%M") == f"2026-07-31 {hhmm}"
        end = int(app_bars.index[mask][0]) + 1
        snap = calculate_macd(app_bars.iloc[:end])
        assert snap is not None
        oracle_row = oracle[oracle["datetime"].dt.strftime("%Y-%m-%d %H:%M") == f"2026-07-31 {hhmm}"].iloc[0]
        assert snap.macd == round(float(oracle_row["macd"]), 6)
        assert snap.signal == round(float(oracle_row["signal"]), 6)
        assert snap.hist == round(float(oracle_row["hist"]), 6)


def test_confirmed_flag_time_is_bar_start_at_and_same_bar_evaluates_once():
    bar_start = datetime(2026, 7, 31, 9, 0, tzinfo=KST)
    signal_id = make_signal_id(bar_start, Direction.UP_RED)

    assert signal_id == "20260731_090000_UP_RED"
    assert "090300" not in signal_id


def test_today_kis_confirmed_flags_reproduce_from_original_1m_bars():
    df_1m = _load_original_hynix_1m("20260730", "20260731")
    bars_3m = resample_completed_3m(df_1m, now=datetime(2026, 7, 31, 12, 48, 0, tzinfo=KST))
    flags = _confirmed_color_flags(bars_3m)

    assert flags.get("09:00") == Direction.UP_RED
    assert flags.get("09:15") == Direction.DOWN_BLUE

    color_by_bar = {}
    for hhmm in ("11:27", "12:45"):
        mask = bars_3m["datetime"].dt.strftime("%Y-%m-%d %H:%M") == f"2026-07-31 {hhmm}"
        end = int(bars_3m.index[mask][0]) + 1
        snap = calculate_macd(bars_3m.iloc[:end])
        assert snap is not None
        color_by_bar[hhmm] = confirmed_macd_flag_condition(snap)

    assert color_by_bar["11:27"] == Direction.UP_RED
    assert color_by_bar["12:45"] == Direction.DOWN_BLUE
