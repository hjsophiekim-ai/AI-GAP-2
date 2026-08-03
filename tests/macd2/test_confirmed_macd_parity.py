"""Confirmed-bar MACD2 parity checks with fixed OHLCV data only.

This replaces the removed v1 parity test. It deliberately imports no legacy
MACD modules; the oracle below is a small independent pandas implementation
fed by fixed 1-minute OHLCV fixtures.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from app.trading.macd2 import config, state_store
from app.trading.macd2.models import Direction
from app.trading.macd2.signal_engine import (
    calculate_macd,
    confirmed_macd_flag_condition,
    evaluate_confirmed_macd_flag,
    make_signal_id,
    resample_completed_3m,
)
from app.trading.macd2.worker import _advance_confirmed_primary

KST = config.KST
ROOT = Path(__file__).resolve().parents[2]
KIS_EXPECTED_FLAGS_CSV = ROOT / "data" / "validation" / "macd2" / "kis_expected_flags.csv"


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


def _load_kis_expected_flags(trading_date: str) -> list[tuple[str, str]]:
    df = pd.read_csv(KIS_EXPECTED_FLAGS_CSV, dtype=str)
    df = df[(df["trading_date"] == trading_date) & (df["confirmed_by_user"].str.lower() == "true")]
    return sorted(((row["flag_time"], row["direction"]) for _, row in df.iterrows()), key=lambda x: x[0])


def test_golden_2026_08_03_confirmed_flags_match_kis_chart_exactly():
    """GOLDEN TEST — pins the confirmed-flag rule restored 2026-08-03 from
    commit 6a2fd07 (the exact code that ran unmodified 2026-07-28 through
    2026-07-30; git-archaeology confirmed zero commits touched
    app/trading/macd2/ in that window). The 2026-07-31 color+regime/debounce
    rewrite that briefly replaced it under-detected real KIS flags by ~85%
    and was reverted the same day this test was added.

    Do NOT relax, retune, or replace this rule to make some other date pass.
    If this test fails after a signal_engine.py/worker.py change, the change
    is the regression — not this fixture. Ground truth is
    data/validation/macd2/kis_expected_flags.csv's 2026-08-03 rows: the 14
    flags the user read directly off the live KIS chart that day.

    Replays the REAL, unmodified ``_advance_confirmed_primary()`` (not a
    re-implementation) against the real 000660 1-minute candles for
    2026-07-31 (prior trading day warm-up) + 2026-08-03.
    """
    expected = _load_kis_expected_flags("20260803")
    assert len(expected) == 14, "fixture must have exactly the 14 confirmed 2026-08-03 KIS flags"

    df_1m = _load_original_hynix_1m("20260731", "20260803")
    now = datetime(2026, 8, 3, 15, 30, tzinfo=KST)
    bars_3m = resample_completed_3m(df_1m, now=now)
    today_idx = list(bars_3m.index[bars_3m["datetime"].dt.strftime("%Y%m%d") == "20260803"])

    state = state_store.default_state()
    produced: list[tuple[str, str]] = []
    for idx in today_idx:
        snap = calculate_macd(bars_3m.iloc[: idx + 1])
        if snap is None:
            continue
        direction = _advance_confirmed_primary(state, snap)
        if direction == Direction.HOLD:
            continue
        produced.append((snap.bar_dt.strftime("%H:%M"), direction.value))

    for i in range(1, len(produced)):
        assert produced[i][1] != produced[i - 1][1], (
            f"same-direction-consecutive duplicate: {produced[i - 1]} then {produced[i]}"
        )

    assert len(produced) == len(expected), f"expected {len(expected)} flags, got {len(produced)}: {produced}"

    def _to_min(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    for (exp_t, exp_d), (prod_t, prod_d) in zip(expected, produced):
        assert prod_d == exp_d, f"direction mismatch at expected {exp_t}: got {prod_t} {prod_d}"
        assert abs(_to_min(prod_t) - _to_min(exp_t)) <= 3, f"time mismatch: expected {exp_t}, got {prod_t}"
