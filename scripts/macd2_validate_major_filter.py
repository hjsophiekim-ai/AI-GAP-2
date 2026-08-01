#!/usr/bin/env python
"""Read-only MAJOR_FLAG (Hybrid V1) filter validation over recorded 1m bars.

Replays already-recorded 000660 1-minute bars for one or more trading dates,
rebuilds completed 3m bars and confirmed MACD(12,26,9) crossovers exactly the
way production does (``signal_engine.resample_completed_3m`` +
``calculate_macd`` + ``evaluate_macd_crossover``, evaluated bar-by-bar), and
scores every confirmed flag through ``major_flag_filter.evaluate_major_flag``
followed by ``apply_major_trade_gates`` against a sequentially simulated
position/daily-budget state.

Strictly read-only with respect to trading: it constructs no broker, sends no
orders, and never writes to the operational runtime state, ledgers or market
data cache. The only files it creates are the three report artifacts under
``--output-dir``.

Usage
-----
    python scripts/macd2_validate_major_filter.py \
        --dates 20260728 20260729 20260730 \
        --input-dir data/validation/macd2_parity \
        --output-dir data/validation/major_filter
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2.major_flag_filter import (  # noqa: E402
    apply_major_trade_gates,
    evaluate_major_flag,
)
from app.trading.macd2.models import Direction, MajorFlagDecision  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd,
    evaluate_macd_crossover,
    resample_completed_3m,
)

KST = config.KST
SYMBOL = config.WATCH_SYMBOL

DEFAULT_DATES = ("20260728", "20260729", "20260730")
DEFAULT_INPUT_DIR = "data/validation/macd2_parity"
DEFAULT_OUTPUT_DIR = "data/validation/major_filter"

ALL_FLAGS_CSV = "all_flags_scored.csv"
APPROVED_FLAGS_CSV = "approved_flags.csv"
SUMMARY_JSON = "summary.json"

# Reference labels are REPORTING ONLY — they are a human reading of the KIS
# chart, not the filter's approval truth. The summary reports hit/miss against
# them so a human can eyeball drift; nothing here approves a flag because it
# appears in this list.
REFERENCE_LABELS: tuple[tuple[str, str, str], ...] = (
    ("20260728", "11:06", "UP_RED"),
    ("20260728", "13:09", "DOWN_BLUE"),
    ("20260728", "13:42", "UP_RED"),
    ("20260728", "14:18", "DOWN_BLUE"),
    ("20260729", "09:27", "DOWN_BLUE"),
    ("20260729", "12:39", "UP_RED"),
    ("20260730", "09:54", "UP_RED"),
    ("20260730", "11:00", "DOWN_BLUE"),
    ("20260730", "12:27", "UP_RED"),
    ("20260730", "13:09", "DOWN_BLUE"),
)

_DATETIME_COLUMN_CANDIDATES = (
    "datetime", "dt", "timestamp", "time", "bar_at", "bar_dt", "date_time", "체결시간",
)
_COLUMN_ALIASES = {
    "open": ("open", "o", "open_price", "시가"),
    "high": ("high", "h", "high_price", "고가"),
    "low": ("low", "l", "low_price", "저가"),
    "close": ("close", "c", "close_price", "종가", "price", "현재가"),
    "volume": ("volume", "v", "vol", "거래량"),
}

_SCORE_KEYS = (
    "hist_impulse", "price_strength", "body", "volume", "ema10_trend",
    "ema20_or_vwap", "volatility",
)
_METRIC_KEYS = (
    "atr14", "macd", "signal", "hist", "prev_hist", "hist_impulse_atr",
    "breakout", "breakout_up", "breakout_down", "price_impulse_atr",
    "close_3_bars_ago", "body_atr", "volume_ratio", "ema10", "ema10_prev",
    "ema20", "vwap", "ema10_ok", "ema20_or_vwap_ok", "recent_range_ratio",
    "ema_spread_ratio", "atr_median_prev20", "close", "open", "volume",
    "volume_median_prev20",
)

CSV_COLUMNS = (
    ["trading_date", "flag_time", "flag_bar_at", "decision_at", "direction",
     "signal_id", "entry_window_open", "is_reversal", "fast_reversal",
     "score", "required_score", "approved", "decision", "block_reason",
     "sim_position_before", "sim_position_after", "daily_major_entry_count_before",
     "reference_label", "reasons"]
    + [f"score_{key}" for key in _SCORE_KEYS]
    + [f"m_{key}" for key in _METRIC_KEYS]
)


class ValidationDataError(RuntimeError):
    """Raised when the recorded 1m bars needed for a date are missing/unusable."""


# ── input loading ─────────────────────────────────────────────────────────
def _candidate_paths(input_dir: Path, date: str) -> list[Path]:
    names = [
        f"{date}_{SYMBOL}_1m.csv",
        f"{SYMBOL}_{date}_1m.csv",
        f"{date}_{SYMBOL}.csv",
        f"{SYMBOL}_{date}.csv",
        f"{date}_1m.csv",
        f"{date}.csv",
        f"replay_{date}_{SYMBOL}_1m.csv",
        f"macd2_parity_{date}.csv",
    ]
    combined = [
        f"{SYMBOL}_1m.csv",
        f"{SYMBOL}.csv",
        "all_1m.csv",
        "macd2_parity_1m.csv",
    ]
    return [input_dir / name for name in names] + [input_dir / name for name in combined]


def _pick_column(columns: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    lowered = {str(col).strip().lower(): col for col in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _normalize_1m_frame(raw: pd.DataFrame, *, source: Path) -> pd.DataFrame:
    columns = list(raw.columns)
    dt_col = _pick_column(columns, _DATETIME_COLUMN_CANDIDATES)
    if dt_col is None:
        raise ValidationDataError(
            f"{source}: no datetime-like column found (looked for {list(_DATETIME_COLUMN_CANDIDATES)})"
        )

    work = pd.DataFrame()
    work["datetime"] = pd.to_datetime(raw[dt_col], errors="coerce")
    for target, aliases in _COLUMN_ALIASES.items():
        col = _pick_column(columns, aliases)
        if col is None:
            if target == "volume":
                work["volume"] = 0.0
                continue
            raise ValidationDataError(f"{source}: missing required '{target}' column")
        work[target] = pd.to_numeric(raw[col], errors="coerce")

    work = work.dropna(subset=["datetime", "open", "high", "low", "close"])
    if work.empty:
        raise ValidationDataError(f"{source}: no usable 1m rows after parsing")
    if work["datetime"].dt.tz is None:
        work["datetime"] = work["datetime"].dt.tz_localize(KST)
    else:
        work["datetime"] = work["datetime"].dt.tz_convert(KST)
    work["volume"] = work["volume"].fillna(0.0)
    return work.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)


def load_1m_bars(input_dir: Path, date: str) -> tuple[pd.DataFrame, Path]:
    """Locate and load the recorded 1m bars for ``date``. Never fabricates data."""
    tried: list[str] = []
    for path in _candidate_paths(input_dir, date):
        tried.append(path.name)
        if not path.exists():
            continue
        frame = _normalize_1m_frame(pd.read_csv(path), source=path)
        same_day = frame[frame["datetime"].dt.strftime("%Y%m%d") == date].reset_index(drop=True)
        if same_day.empty:
            continue
        return same_day, path
    raise ValidationDataError(
        f"no recorded 1m bars for {date} under {input_dir} "
        f"(tried: {', '.join(dict.fromkeys(tried))})"
    )


# ── confirmed-flag detection (production parity) ───────────────────────────
def _session_end(date: str) -> datetime:
    day = datetime.strptime(date, "%Y%m%d").replace(tzinfo=KST)
    return day.replace(hour=16, minute=0)


def detect_confirmed_flags(bars_3m: pd.DataFrame) -> list[tuple[int, Direction, Any]]:
    """Walk completed 3m bars in order, mirroring worker._advance_confirmed_primary:
    each bar is evaluated exactly once, the first bar with a defined MACD sets
    the direction baseline only, and a repeated same direction is suppressed."""
    flags: list[tuple[int, Direction, Any]] = []
    previous_direction: Optional[Direction] = None
    seen_first_bar = False
    for i in range(len(bars_3m)):
        snap = calculate_macd(bars_3m.iloc[: i + 1])
        if snap is None:
            continue
        if not seen_first_bar:
            seen_first_bar = True  # baseline bar: direction context only, never a flag
            continue
        direction = evaluate_macd_crossover(snap, previous_direction)
        if direction in (Direction.UP_RED, Direction.DOWN_BLUE):
            previous_direction = direction
            flags.append((i, direction, snap))
    return flags


def _entry_window_open(decision_at: datetime) -> bool:
    moment: dtime = decision_at.astimezone(KST).time()
    return config.SESSION_OPEN <= moment < config.NEW_ENTRY_CUTOFF


def _reference_label(date: str, flag_hhmm: str) -> Optional[str]:
    for ref_date, ref_time, ref_direction in REFERENCE_LABELS:
        if ref_date == date and ref_time == flag_hhmm:
            return ref_direction
    return None


def _row_for(
    *,
    date: str,
    bar_dt: datetime,
    decision_at: datetime,
    direction: Direction,
    decision: MajorFlagDecision,
    entry_window_open: bool,
    sim_before: Optional[Direction],
    sim_after: Optional[Direction],
    daily_count_before: int,
    approved: bool,
) -> dict[str, Any]:
    flag_hhmm = bar_dt.strftime("%H:%M")
    row: dict[str, Any] = {
        "trading_date": date,
        "flag_time": flag_hhmm,
        "flag_bar_at": bar_dt.isoformat(),
        "decision_at": decision_at.isoformat(),
        "direction": direction.value,
        "signal_id": f"{bar_dt:%Y%m%d}_{bar_dt:%H%M%S}_{direction.value}",
        "entry_window_open": entry_window_open,
        "is_reversal": decision.is_reversal,
        "fast_reversal": decision.fast_reversal,
        "score": decision.score,
        "required_score": decision.required_score,
        "approved": approved,
        "decision": decision.decision,
        "block_reason": decision.block_reason or "",
        "sim_position_before": sim_before.value if sim_before else "FLAT",
        "sim_position_after": sim_after.value if sim_after else "FLAT",
        "daily_major_entry_count_before": daily_count_before,
        "reference_label": _reference_label(date, flag_hhmm) or "",
        "reasons": " | ".join(decision.reasons or ()),
    }
    for key in _SCORE_KEYS:
        row[f"score_{key}"] = (decision.component_scores or {}).get(key, "")
    metrics = decision.metrics or {}
    for key in _METRIC_KEYS:
        value = metrics.get(key)
        row[f"m_{key}"] = "" if value is None else value
    return row


def validate_date(bars_1m: pd.DataFrame, date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bars_3m = resample_completed_3m(bars_1m, now=_session_end(date))
    flags = detect_confirmed_flags(bars_3m)

    rows: list[dict[str, Any]] = []
    sim_position: Optional[Direction] = None
    last_entry_at: Optional[datetime] = None
    last_exit_at: dict[Direction, datetime] = {}
    daily_count = 0

    for index, direction, _snap in flags:
        flag_bars = bars_3m.iloc[: index + 1]
        bar_dt = pd.Timestamp(flag_bars["datetime"].iloc[-1]).to_pydatetime()
        decision_at = bar_dt + timedelta(minutes=3)
        window_open = _entry_window_open(decision_at)
        # Production force-liquidates at 15:00 regardless of any flag, so the
        # simulated position must be flat for any later bar.
        if sim_position is not None and decision_at.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            last_exit_at[sim_position] = decision_at
            sim_position = None
        sim_before = sim_position
        count_before = daily_count

        decision = evaluate_major_flag(
            flag_bars, direction, sim_position, last_entry_at, daily_count, decision_at,
        )
        decision = apply_major_trade_gates(
            decision,
            flag_direction=direction,
            position_direction=sim_position,
            last_entry_at=last_entry_at,
            last_same_direction_exit_at=last_exit_at.get(direction),
            daily_major_entry_count=daily_count,
            now=decision_at,
        )

        approved = bool(decision.approved and window_open)
        if approved:
            if sim_position is not None and sim_position != direction:
                last_exit_at[sim_position] = decision_at
            sim_position = direction
            last_entry_at = decision_at
            daily_count += 1

        rows.append(_row_for(
            date=date, bar_dt=bar_dt, decision_at=decision_at, direction=direction,
            decision=decision, entry_window_open=window_open,
            sim_before=sim_before, sim_after=sim_position,
            daily_count_before=count_before, approved=approved,
        ))

    block_reasons: dict[str, int] = {}
    for row in rows:
        if row["approved"]:
            continue
        key = str(row["block_reason"] or row["decision"] or "UNKNOWN")
        block_reasons[key] = block_reasons.get(key, 0) + 1

    per_date = {
        "trading_date": date,
        "one_minute_bars": int(len(bars_1m)),
        "completed_3m_bars": int(len(bars_3m)),
        "confirmed_flags": len(rows),
        "approved_flags": sum(1 for row in rows if row["approved"]),
        "simulated_daily_major_entry_count": daily_count,
        "block_reasons": block_reasons,
    }
    return rows, per_date


def _reference_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report-only comparison against the human-read reference labels."""
    by_key = {(row["trading_date"], row["flag_time"]): row for row in rows}
    entries = []
    for date, hhmm, direction in REFERENCE_LABELS:
        row = by_key.get((date, hhmm))
        entries.append({
            "trading_date": date,
            "flag_time": hhmm,
            "reference_direction": direction,
            "confirmed_flag_found": row is not None,
            "direction_matches": bool(row is not None and row["direction"] == direction),
            "approved_by_filter": bool(row is not None and row["approved"]),
            "decision": (row["decision"] if row else ""),
            "block_reason": (row["block_reason"] if row else ""),
            "score": (row["score"] if row else None),
            "required_score": (row["required_score"] if row else None),
        })
    return {
        "note": (
            "Reference labels are a human chart reading used for reporting only. "
            "They are never treated as approval truth and never force an approval."
        ),
        "reference_count": len(REFERENCE_LABELS),
        "confirmed_flag_hits": sum(1 for e in entries if e["direction_matches"]),
        "confirmed_flag_misses": sum(1 for e in entries if not e["direction_matches"]),
        "approved_hits": sum(1 for e in entries if e["approved_by_filter"]),
        "labels": entries,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only MAJOR_FLAG filter validation over recorded 1m bars.",
    )
    parser.add_argument(
        "--dates", nargs="+", default=list(DEFAULT_DATES), metavar="YYYYMMDD",
        help="trading dates to validate (default: %(default)s)",
    )
    parser.add_argument(
        "--input-dir", default=DEFAULT_INPUT_DIR,
        help="directory holding the recorded 000660 1m bar CSVs (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR,
        help="directory the three report artifacts are written to (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    input_dir = Path(args.input_dir)
    if not input_dir.is_absolute():
        input_dir = PROJECT_ROOT / input_dir
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    for date in args.dates:
        if len(date) != 8 or not date.isdigit():
            print(f"ERROR: --dates expects YYYYMMDD values, got {date!r}", file=sys.stderr)
            return 2

    if not input_dir.exists():
        print(
            f"ERROR: input directory does not exist: {input_dir}\n"
            "       Record the 1m bars first; this script never fabricates data.",
            file=sys.stderr,
        )
        return 2

    loaded: dict[str, tuple[pd.DataFrame, Path]] = {}
    failures: list[str] = []
    for date in args.dates:
        try:
            loaded[date] = load_1m_bars(input_dir, date)
        except ValidationDataError as exc:
            failures.append(str(exc))

    if failures:
        print("ERROR: validation input data is missing or unusable:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "No report was written. Nothing is approved on missing data.",
            file=sys.stderr,
        )
        return 2

    all_rows: list[dict[str, Any]] = []
    per_date: list[dict[str, Any]] = []
    for date in args.dates:
        bars_1m, source = loaded[date]
        rows, summary = validate_date(bars_1m, date)
        summary["source_file"] = str(source)
        all_rows.extend(rows)
        per_date.append(summary)
        print(
            f"{date}: {summary['completed_3m_bars']} completed 3m bars, "
            f"{summary['confirmed_flags']} confirmed flags, "
            f"{summary['approved_flags']} approved  ({source.name})"
        )

    approved_rows = [row for row in all_rows if row["approved"]]
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / ALL_FLAGS_CSV, all_rows)
    _write_csv(output_dir / APPROVED_FLAGS_CSV, approved_rows)

    block_reasons: dict[str, int] = {}
    for row in all_rows:
        if row["approved"]:
            continue
        key = str(row["block_reason"] or row["decision"] or "UNKNOWN")
        block_reasons[key] = block_reasons.get(key, 0) + 1

    summary = {
        "generated_at": datetime.now(KST).isoformat(),
        "read_only": True,
        "symbol": SYMBOL,
        "filter_version": config.MAJOR_FILTER_VERSION,
        "thresholds": {
            "entry_score_min": config.MAJOR_ENTRY_SCORE_MIN,
            "reversal_score_min": config.MAJOR_REVERSAL_SCORE_MIN,
            "fast_reversal_score_min": config.MAJOR_FAST_REVERSAL_SCORE_MIN,
            "fast_reversal_window_min": config.MAJOR_FAST_REVERSAL_WINDOW_MIN,
            "max_daily_entries": config.MAJOR_MAX_DAILY_ENTRIES,
            "min_hold_min": config.MAJOR_MIN_HOLD_MIN,
            "same_direction_reentry_min": config.MAJOR_SAME_DIRECTION_REENTRY_MIN,
        },
        "dates": list(args.dates),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "total_confirmed_flags": len(all_rows),
        "total_approved_flags": len(approved_rows),
        "block_reasons": block_reasons,
        "per_date": per_date,
        "reference_labels": _reference_report(all_rows),
    }
    with open(output_dir / SUMMARY_JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print(
        f"\n{len(all_rows)} confirmed flags scored, {len(approved_rows)} approved.\n"
        f"Wrote {ALL_FLAGS_CSV}, {APPROVED_FLAGS_CSV}, {SUMMARY_JSON} to {output_dir}"
    )
    reference = summary["reference_labels"]
    print(
        f"Reference labels (reporting only): "
        f"{reference['confirmed_flag_hits']}/{reference['reference_count']} confirmed-flag hits, "
        f"{reference['approved_hits']} of those approved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
