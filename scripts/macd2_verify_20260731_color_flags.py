"""Verify MACD2's 2026-07-31 KIS color-flag parity from full-day 1m replay."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd,
    evaluate_confirmed_macd_color_onset,
    confirmed_macd_flag_condition,
    make_signal_id,
    resample_completed_3m,
)

KST = config.KST
EXPECTED = {
    "09:00": Direction.UP_RED,
    "09:15": Direction.DOWN_BLUE,
    "11:27": Direction.UP_RED,
    "12:45": Direction.DOWN_BLUE,
}


def _load_1m() -> pd.DataFrame:
    frames = []
    for ymd in ("20260730", "20260731"):
        path = ROOT / "data" / "cache" / f"replay_{ymd}_hynix_1m.csv"
        frame = pd.read_csv(path)
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise").dt.tz_localize(KST)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)


def main() -> int:
    df_1m = _load_1m()
    bars_3m = resample_completed_3m(df_1m, now=datetime(2026, 7, 31, 15, 30, tzinfo=KST))
    last_direction: Direction | None = None
    pending_direction: Direction | None = None
    pending_count = 0
    last_regime: str | None = None
    flags: list[tuple[str, Direction, str, tuple[float, float, float]]] = []
    raw_colors: dict[str, Direction] = {}
    debug_rows: list[dict[str, object]] = []
    signal_ids: set[str] = set()
    duplicate_signal_ids: list[str] = []

    for end in range(config.EMA_SLOW, len(bars_3m) + 1):
        snap = calculate_macd(bars_3m.iloc[:end])
        assert snap is not None
        if snap.bar_dt.date() != datetime(2026, 7, 31).date():
            continue
        hhmm = snap.bar_dt.strftime("%H:%M")
        if hhmm == "09:00":
            last_direction = None
            pending_direction = None
            pending_count = 0
            last_regime = None
        raw = confirmed_macd_flag_condition(snap)
        if raw != Direction.HOLD:
            raw_colors[hhmm] = raw
        bar_end = snap.bar_dt + pd.Timedelta(minutes=3)
        previous_state = last_direction
        decision = evaluate_confirmed_macd_color_onset(
            snap,
            last_direction,
            pending_direction,
            pending_count,
            previous_regime=last_regime,
            publishable=bar_end.time() < config.NEW_ENTRY_CUTOFF,
        )
        pending_direction = decision.pending_direction
        pending_count = decision.pending_count
        direction = decision.direction
        if "12:30" <= hhmm <= "12:48":
            debug_rows.append({
                "time": hhmm,
                "macd": snap.macd,
                "signal": snap.signal,
                "hist": snap.hist,
                "raw_color": raw.value,
                "previous_color_state": previous_state.value if previous_state else None,
                "onset": decision.onset,
                "published": direction.value if direction != Direction.HOLD else None,
                "kis_expected": EXPECTED.get(hhmm).value if EXPECTED.get(hhmm) else None,
                "pending": (
                    f"{decision.pending_direction.value}:{decision.pending_count}"
                    if decision.pending_direction else None
                ),
                "required_count": decision.required_count,
                "regime": decision.regime,
            })
        if direction == Direction.HOLD:
            continue
        signal_id = make_signal_id(snap.bar_dt, direction)
        if signal_id in signal_ids:
            duplicate_signal_ids.append(signal_id)
        signal_ids.add(signal_id)
        flags.append((hhmm, direction, signal_id, snap.hist_last3))
        last_direction = direction
        last_regime = decision.regime

    actual = {hhmm: direction for hhmm, direction, _sid, _hist in flags}
    false_positive = [(t, d.value) for t, d in actual.items() if EXPECTED.get(t) != d]
    missed = [(t, d.value) for t, d in EXPECTED.items() if actual.get(t) != d]
    wrong_time = [
        (t, actual.get(t).value if actual.get(t) else None, EXPECTED[t].value)
        for t in EXPECTED
        if t in actual and actual[t] != EXPECTED[t]
    ]

    print("=== MACD2 2026-07-31 full-day color flag verification ===")
    print("Expected:", [(t, d.value) for t, d in EXPECTED.items()])
    print("Actual:", [(t, d.value, sid, hist) for t, d, sid, hist in flags])
    print("12:30~12:48 detail:")
    for row in debug_rows:
        print(row)
    print("Raw expected-bar colors:", {t: raw_colors.get(t).value if raw_colors.get(t) else None for t in EXPECTED})
    print("FALSE_POSITIVE:", false_positive)
    print("MISSED:", missed)
    print("WRONG_TIME:", wrong_time)
    print("DUPLICATE_SIGNAL_ID:", duplicate_signal_ids)

    ok = (
        not false_positive
        and not missed
        and not wrong_time
        and not duplicate_signal_ids
        and len(flags) == len(EXPECTED)
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
