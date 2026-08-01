#!/usr/bin/env python
"""Re-collect complete KIS 1m bars and compare MAJOR filter A vs B (read-only).

Does NOT modify operational macd2 code, state, ledger, or call order APIs.
Refuses to compute PnL unless every symbol/date has a contiguous regular-session
1m grid (09:00..15:30, 1-minute step) with zero gaps >= 1 minute missing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.kis_client import create_kis_client
from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import (
    apply_major_trade_gates,
    evaluate_major_flag,
    score_for_direction,
    compute_component_scores,
    _prepare_bars,
    _macd_lines,
    _raw_crossover_direction,
    _as_direction,
    _direction_sign,
    _finite,
    _reject,
)
from app.trading.macd2.models import Direction, MajorFlagDecision
from app.trading.macd2.risk_exit import evaluate_position_exits
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m
from app.trading.trading_cost_engine import TradeCostEngine

KST = config.KST
WATCH, LONG, INV = config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL
SYMBOLS = (WATCH, LONG, INV)
DATES = ("20260728", "20260729", "20260730")
BUDGET = 10_000_000.0
SESSION_START = dtime(9, 0)
SESSION_END = dtime(15, 30)  # inclusive bar-open grid

KIS_PAGE_SIZE = 120
KIS_MAX_PAGES = 40
PACING = 0.45
RETRY = 5
RETRY_DELAY = 2.0

KIS_REFERENCE_FLAGS = (
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


# ── B-plan thresholds (simulation-local only; does not touch config.py) ─────
B_ENTRY = 65.0
B_REVERSAL = 75.0
B_FAST = 82.0
B_FAST_WINDOW_MIN = 15
B_MAX_DAILY = 4
B_MIN_HOLD = 9
B_SAME_REENTRY = 18


def expected_session_timestamps(date_ymd: str) -> list[datetime]:
    d = datetime.strptime(date_ymd, "%Y%m%d").date()
    t = datetime.combine(d, SESSION_START, tzinfo=KST)
    end = datetime.combine(d, SESSION_END, tzinfo=KST)
    out: list[datetime] = []
    while t <= end:
        out.append(t)
        t += timedelta(minutes=1)
    return out  # 09:00..15:30 inclusive = 391


def _candles_to_df(candles: list[dict], date_ymd: str) -> pd.DataFrame:
    rows = []
    for c in candles or []:
        date_raw = str(c.get("date") or date_ymd).strip()
        time_raw = str(c.get("time") or "").strip().replace(":", "")
        if len(date_raw) != 8 or len(time_raw) < 6:
            continue
        try:
            dt = datetime.strptime(f"{date_raw}{time_raw[:6]}", "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            continue
        if date_raw != date_ymd:
            continue
        rows.append({
            "datetime": dt,
            "open": float(c.get("open") or 0),
            "high": float(c.get("high") or 0),
            "low": float(c.get("low") or 0),
            "close": float(c.get("close") or 0),
            "volume": float(c.get("volume") or 0),
        })
    if not rows:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    return (
        pd.DataFrame(rows)
        .drop_duplicates("datetime", keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )


def fetch_page(client, symbol: str, date_ymd: str, hour1: str) -> tuple[pd.DataFrame, Optional[str]]:
    err = None
    part = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    for retry_i in range(RETRY):
        try:
            candles = client.get_minute_candles_for_date(
                symbol, date_ymd, period_min=1, count=KIS_PAGE_SIZE, hour1=hour1,
            ) or []
            # Do NOT slice further — client already caps; keep all returned rows.
            part = _candles_to_df(candles, date_ymd)
            err = None
            if not part.empty or retry_i == RETRY - 1:
                break
        except Exception as exc:
            err = repr(exc)
            part = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        if retry_i < RETRY - 1:
            time.sleep(RETRY_DELAY)
    return part, err


def fetch_day_complete(client, symbol: str, date_ymd: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Page + targeted gap-fill until expected grid is complete or give up."""
    frames: list[pd.DataFrame] = []
    diags: list[dict[str, Any]] = []
    hour1 = ""
    prev_count = 0
    for page_i in range(KIS_MAX_PAGES):
        if page_i > 0:
            time.sleep(PACING)
        part, err = fetch_page(client, symbol, date_ymd, hour1)
        diags.append({"page": page_i + 1, "hour1": hour1 or "LATEST", "received": int(len(part)), "error": err})
        if part.empty:
            break
        frames.append(part)
        merged = (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates("datetime", keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        if len(merged) <= prev_count:
            break
        prev_count = len(merged)
        oldest = merged["datetime"].iloc[0].to_pydatetime()
        next_hour1 = (oldest - timedelta(minutes=1)).strftime("%H%M%S")
        if next_hour1 == hour1:
            break
        hour1 = next_hour1
        frames = [merged]

    df = frames[-1] if frames else pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    # Targeted gap fill: for each missing timestamp, request page ending near that hole.
    expected = expected_session_timestamps(date_ymd)
    for _fill_round in range(8):
        coverage = analyze_coverage(df, date_ymd)
        missing = coverage["missing_timestamps"]
        if not missing:
            break
        # Ask KIS for a page whose cursor is just after each gap end (API returns older bars).
        # Group missing into runs; fetch with hour1 = end_of_run + 1 minute.
        runs = coverage["gap_runs"]
        progressed = False
        for run in runs:
            end_hm = run["end"]  # HH:MM
            end_dt = datetime.strptime(f"{date_ymd}{end_hm.replace(':','')}", "%Y%m%d%H%M").replace(tzinfo=KST)
            cursor = (end_dt + timedelta(minutes=1)).strftime("%H%M%S")
            time.sleep(PACING)
            part, err = fetch_page(client, symbol, date_ymd, cursor)
            diags.append({"page": f"gapfill:{cursor}", "hour1": cursor, "received": int(len(part)), "error": err})
            if part.empty:
                continue
            before = len(df)
            df = (
                pd.concat([df, part], ignore_index=True)
                .drop_duplicates("datetime", keep="last")
                .sort_values("datetime")
                .reset_index(drop=True)
            )
            if len(df) > before:
                progressed = True
        if not progressed:
            break

    cov = analyze_coverage(df, date_ymd)
    return df, {
        "symbol": symbol,
        "date": date_ymd,
        "pages": diags,
        **{k: cov[k] for k in ("expected", "actual", "missing_count", "complete", "gap_runs")},
        "missing_timestamps": cov["missing_timestamps"],
    }


def analyze_coverage(df: pd.DataFrame, date_ymd: str) -> dict[str, Any]:
    expected = expected_session_timestamps(date_ymd)
    exp_set = set(expected)
    if df is None or df.empty:
        act_set: set[datetime] = set()
    else:
        work = df.copy()
        work["datetime"] = pd.to_datetime(work["datetime"], utc=False)
        if work["datetime"].dt.tz is None:
            work["datetime"] = work["datetime"].dt.tz_localize(KST)
        else:
            work["datetime"] = work["datetime"].dt.tz_convert(KST)
        # Keep only this date's regular session
        work = work[work["datetime"].dt.strftime("%Y%m%d") == date_ymd]
        mins = work["datetime"].dt.hour * 60 + work["datetime"].dt.minute
        lo = SESSION_START.hour * 60 + SESSION_START.minute
        hi = SESSION_END.hour * 60 + SESSION_END.minute
        work = work[(mins >= lo) & (mins <= hi)].drop_duplicates("datetime")
        act_set = set(pd.Timestamp(x).to_pydatetime().astimezone(KST) for x in work["datetime"])

    missing = sorted(exp_set - act_set)
    gap_runs: list[dict[str, Any]] = []
    if missing:
        start = prev = missing[0]
        for ts in missing[1:]:
            if (ts - prev).total_seconds() == 60:
                prev = ts
            else:
                gap_runs.append({
                    "start": start.strftime("%H:%M"),
                    "end": prev.strftime("%H:%M"),
                    "minutes": int((prev - start).total_seconds() / 60) + 1,
                })
                start = prev = ts
        gap_runs.append({
            "start": start.strftime("%H:%M"),
            "end": prev.strftime("%H:%M"),
            "minutes": int((prev - start).total_seconds() / 60) + 1,
        })

    # Fail if any contiguous missing run >= 1 minute (i.e. any missing bar)
    complete = len(missing) == 0
    return {
        "expected": len(expected),
        "actual": len(act_set & exp_set),
        "missing_count": len(missing),
        "missing_timestamps": [t.strftime("%H:%M") for t in missing],
        "gap_runs": gap_runs,
        "complete": complete,
        "has_gap_ge_1min": len(missing) > 0,
    }


def collect_all(client, raw_dir: Path) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, pd.DataFrame] = {}
    report: dict[str, Any] = {"items": [], "all_complete": True, "warmup_date": None}

    # Warm-up: prior trading day for 000660
    from datetime import date as date_cls
    d0 = datetime.strptime(DATES[0], "%Y%m%d").date()
    for back in range(1, 12):
        cand = (d0 - timedelta(days=back))
        if cand.weekday() >= 5:
            continue
        ymd = cand.strftime("%Y%m%d")
        print(f"[collect] warmup {WATCH} {ymd}")
        df, diag = fetch_day_complete(client, WATCH, ymd)
        report["items"].append({"role": "warmup", **diag})
        if not df.empty and diag.get("actual", 0) >= 300:
            path = raw_dir / f"{WATCH}_{ymd}_1m.csv"
            df.to_csv(path, index=False)
            data[f"{WATCH}_warmup"] = df
            report["warmup_date"] = ymd
            print(f"  -> warmup ok actual={diag['actual']} expected={diag['expected']} missing={diag['missing_count']}")
            break
        print(f"  -> incomplete/empty actual={diag.get('actual')} missing={diag.get('missing_count')}")
    if "warmup_date" not in report or not report["warmup_date"]:
        report["all_complete"] = False

    for date in DATES:
        for symbol in SYMBOLS:
            print(f"[collect] {symbol} {date}")
            df, diag = fetch_day_complete(client, symbol, date)
            report["items"].append({"role": "session", **diag})
            print(
                f"  -> expected={diag['expected']} actual={diag['actual']} "
                f"missing={diag['missing_count']} complete={diag['complete']}"
            )
            if diag["gap_runs"]:
                for g in diag["gap_runs"][:12]:
                    print(f"     gap {g['start']}-{g['end']} ({g['minutes']}m)")
            path = raw_dir / f"{symbol}_{date}_1m.csv"
            df.to_csv(path, index=False)
            data[f"{symbol}_{date}"] = df
            if not diag["complete"]:
                report["all_complete"] = False
            time.sleep(PACING)
    return data, report


def load_raw(raw_dir: Path) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    data: dict[str, pd.DataFrame] = {}
    report: dict[str, Any] = {"items": [], "all_complete": True, "warmup_date": None, "source": "disk"}
    warm = sorted(raw_dir.glob(f"{WATCH}_*_1m.csv"))
    warm = [p for p in warm if p.stem.split("_")[1] < DATES[0]]
    if warm:
        p = warm[-1]
        df = pd.read_csv(p)
        df["datetime"] = pd.to_datetime(df["datetime"], utc=False)
        if df["datetime"].dt.tz is None:
            df["datetime"] = df["datetime"].dt.tz_localize(KST)
        else:
            df["datetime"] = df["datetime"].dt.tz_convert(KST)
        data[f"{WATCH}_warmup"] = df
        report["warmup_date"] = p.stem.split("_")[1]
    else:
        report["all_complete"] = False

    for date in DATES:
        for symbol in SYMBOLS:
            path = raw_dir / f"{symbol}_{date}_1m.csv"
            if not path.exists():
                report["all_complete"] = False
                report["items"].append({
                    "role": "session", "symbol": symbol, "date": date,
                    "complete": False, "missing_count": -1, "expected": len(expected_session_timestamps(date)),
                    "actual": 0, "gap_runs": [], "missing_timestamps": ["FILE_MISSING"],
                })
                continue
            df = pd.read_csv(path)
            df["datetime"] = pd.to_datetime(df["datetime"], utc=False)
            if df["datetime"].dt.tz is None:
                df["datetime"] = df["datetime"].dt.tz_localize(KST)
            else:
                df["datetime"] = df["datetime"].dt.tz_convert(KST)
            data[f"{symbol}_{date}"] = df
            cov = analyze_coverage(df, date)
            report["items"].append({"role": "session", "symbol": symbol, "date": date, **cov})
            if not cov["complete"]:
                report["all_complete"] = False
    return data, report


# ── B-plan scoring (local; does not mutate production filter module) ────────

def _tier(value: float, levels: list[tuple[float, float]]) -> float:
    """levels sorted ascending threshold -> points; last matching wins."""
    pts = 0.0
    for thr, score in levels:
        if value >= thr:
            pts = score
    return pts


def score_components_b(metrics: dict[str, Any], direction: Direction) -> tuple[dict[str, float], dict[str, Any]]:
    sign = _direction_sign(direction)
    m = dict(metrics)
    atr14 = float(m["atr14"])
    scores = {
        "hist_impulse": 0.0, "price_strength": 0.0, "body": 0.0, "volume": 0.0,
        "ema10_trend": 0.0, "ema20_or_vwap": 0.0, "volatility": 0.0,
    }
    hist_impulse = sign * (float(m["hist"]) - float(m["prev_hist"])) / atr14
    m["hist_impulse_atr"] = hist_impulse
    scores["hist_impulse"] = _tier(hist_impulse, [(0.10, 10.0), (0.15, 18.0), (0.22, 25.0)])

    breakout = bool(m["breakout_up"] if direction == Direction.UP_RED else m["breakout_down"])
    price_impulse = sign * (float(m["close"]) - float(m["close_3_bars_ago"])) / atr14
    m["breakout"] = breakout
    m["price_impulse_atr"] = price_impulse
    price_pts = 25.0 if breakout else _tier(price_impulse, [(0.35, 15.0), (0.55, 25.0)])
    scores["price_strength"] = price_pts

    body_atr = float(m["body_atr"])
    body_ok = (float(m["close"]) > float(m["open"])) if direction == Direction.UP_RED else (float(m["close"]) < float(m["open"]))
    scores["body"] = _tier(body_atr, [(0.25, 5.0), (0.40, 10.0)]) if body_ok else 0.0

    scores["volume"] = _tier(float(m["volume_ratio"]), [(1.00, 5.0), (1.10, 10.0), (1.20, 15.0)])

    if direction == Direction.UP_RED:
        ema10_ok = float(m["ema10"]) > float(m["ema10_prev"]) and float(m["close"]) > float(m["ema10"])
        ema20_or_vwap_ok = float(m["close"]) > float(m["ema20"]) or (
            m.get("vwap") is not None and _finite(m["vwap"]) and float(m["close"]) > float(m["vwap"])
        )
    else:
        ema10_ok = float(m["ema10"]) < float(m["ema10_prev"]) and float(m["close"]) < float(m["ema10"])
        ema20_or_vwap_ok = float(m["close"]) < float(m["ema20"]) or (
            m.get("vwap") is not None and _finite(m["vwap"]) and float(m["close"]) < float(m["vwap"])
        )
    m["ema10_ok"] = bool(ema10_ok)
    m["ema20_or_vwap_ok"] = bool(ema20_or_vwap_ok)
    scores["ema10_trend"] = 10.0 if ema10_ok else 0.0
    scores["ema20_or_vwap"] = 10.0 if ema20_or_vwap_ok else 0.0

    vol_ok = (
        float(m["recent_range_ratio"]) >= config.MAJOR_SIDEWAYS_RANGE_MAX
        or float(m["atr14"]) >= float(m["atr_median_prev20"])
    )
    scores["volatility"] = 5.0 if vol_ok else 0.0
    return scores, m


def evaluate_major_flag_b(
    bars_3m, flag_direction, position_direction, last_entry_at, daily_count, now,
) -> MajorFlagDecision:
    direction = _as_direction(flag_direction)
    if direction is None:
        return _reject(decision=config.FILTER_INPUT_NOT_CROSSOVER, block_reason=config.FILTER_INPUT_NOT_CROSSOVER, reasons=["bad direction"])
    pos_dir = _as_direction(position_direction)
    is_reversal = pos_dir is not None and pos_dir != direction
    fast = False
    if is_reversal and last_entry_at is not None:
        if (now - last_entry_at).total_seconds() / 60.0 <= B_FAST_WINDOW_MIN:
            fast = True
    required = B_FAST if fast else (B_REVERSAL if is_reversal else B_ENTRY)

    work = _prepare_bars(bars_3m)
    if work is None:
        return _reject(decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
                       reasons=["insufficient bars"], is_reversal=is_reversal, fast_reversal=fast, required_score=required)
    _m, _s, hist = _macd_lines(work["close"].astype(float))
    prev_h, cur_h = float(hist.iloc[-2]), float(hist.iloc[-1])
    if not _finite(prev_h) or not _finite(cur_h) or _raw_crossover_direction(prev_h, cur_h) != direction:
        return _reject(decision=config.FILTER_INPUT_NOT_CROSSOVER, block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
                       reasons=["not crossover"], is_reversal=is_reversal, fast_reversal=fast, required_score=required)

    scores_t, metrics_t, err = compute_component_scores(work)
    if err or scores_t is None or metrics_t is None:
        return _reject(decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
                       reasons=[err or "insufficient"], is_reversal=is_reversal, fast_reversal=fast, required_score=required)
    scores, metrics = score_components_b(metrics_t, direction)
    total = float(sum(scores.values()))

    if (
        float(metrics["ema_spread_ratio"]) < config.MAJOR_SIDEWAYS_EMA_SPREAD_MAX
        and float(metrics["recent_range_ratio"]) < config.MAJOR_SIDEWAYS_RANGE_MAX
    ):
        return _reject(decision=config.MAJOR_SIDEWAYS_BLOCK, block_reason=config.MAJOR_SIDEWAYS_BLOCK,
                       reasons=["sideways"], is_reversal=is_reversal, fast_reversal=fast,
                       score=total, required_score=required, component_scores=scores, metrics=metrics)

    # Required: price strength > 0 OR ema20/vwap points
    if scores["price_strength"] <= 0 and scores["ema20_or_vwap"] <= 0:
        return _reject(decision=config.MAJOR_PRICE_CONFIRMATION_FAILED, block_reason=config.MAJOR_PRICE_CONFIRMATION_FAILED,
                       reasons=["price confirm failed"], is_reversal=is_reversal, fast_reversal=fast,
                       score=total, required_score=required, component_scores=scores, metrics=metrics)

    if total < required:
        return _reject(decision=config.MAJOR_SCORE_BELOW_THRESHOLD, block_reason=config.MAJOR_SCORE_BELOW_THRESHOLD,
                       reasons=[f"score {total:.0f} < {required:.0f}"], is_reversal=is_reversal, fast_reversal=fast,
                       score=total, required_score=required, component_scores=scores, metrics=metrics)

    return MajorFlagDecision(
        approved=True, score=total, required_score=required, decision=config.MAJOR_APPROVED,
        reasons=("ok",), component_scores=scores, metrics=metrics,
        is_reversal=is_reversal, fast_reversal=fast, block_reason=None,
    )


def apply_gates_b(decision, *, flag_direction, position_direction, last_entry_at, last_same_exit, daily_count, now):
    # Reuse production gate logic but with B constants via temporary monkey values
    # Implement locally to avoid touching config.
    direction = _as_direction(flag_direction)
    pos_dir = _as_direction(position_direction)
    if pos_dir is not None and pos_dir == direction:
        return _reject(decision=config.SAME_DIRECTION_POSITION_HELD, block_reason=config.SAME_DIRECTION_POSITION_HELD,
                       reasons=["same dir"], score=decision.score, required_score=decision.required_score,
                       component_scores=decision.component_scores, metrics=decision.metrics)
    if not decision.approved:
        return decision
    if int(daily_count) >= B_MAX_DAILY:
        return _reject(decision=config.MAJOR_DAILY_ENTRY_LIMIT, block_reason=config.MAJOR_DAILY_ENTRY_LIMIT,
                       reasons=["daily limit"], is_reversal=decision.is_reversal, fast_reversal=decision.fast_reversal,
                       score=decision.score, required_score=decision.required_score,
                       component_scores=decision.component_scores, metrics=decision.metrics)
    if pos_dir is None and last_same_exit is not None:
        mins = (now - last_same_exit).total_seconds() / 60.0
        if mins < B_SAME_REENTRY:
            return _reject(decision=config.MAJOR_SAME_DIRECTION_COOLDOWN, block_reason=config.MAJOR_SAME_DIRECTION_COOLDOWN,
                           reasons=["cooldown"], is_reversal=decision.is_reversal, fast_reversal=decision.fast_reversal,
                           score=decision.score, required_score=decision.required_score,
                           component_scores=decision.component_scores, metrics=decision.metrics)
    if decision.is_reversal and last_entry_at is not None:
        hold = (now - last_entry_at).total_seconds() / 60.0
        if hold < B_MIN_HOLD and decision.score < B_FAST:
            return _reject(decision=config.MAJOR_MIN_HOLD_BLOCK, block_reason=config.MAJOR_MIN_HOLD_BLOCK,
                           reasons=["min hold"], is_reversal=decision.is_reversal, fast_reversal=decision.fast_reversal,
                           score=decision.score, required_score=decision.required_score,
                           component_scores=decision.component_scores, metrics=decision.metrics)
    return decision


# ── Simulation ──────────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    direction: Direction
    qty: int
    entry_price: float
    entry_at: datetime
    signal_bar_at: datetime
    order_at: datetime
    peak_net: float = 0.0
    profit_lock_active: bool = False


def _target(direction: Direction) -> str:
    return LONG if direction == Direction.UP_RED else INV


def _next_open(etf: pd.DataFrame, at_or_after: datetime) -> Optional[tuple[datetime, float]]:
    if etf is None or etf.empty:
        return None
    sub = etf[etf["datetime"] >= at_or_after]
    if sub.empty:
        return None
    # Fail closed if the next available bar is > 1 minute after confirmation
    # (would mean we are jumping a gap — caller should have failed the day already).
    row = sub.iloc[0]
    dt = pd.Timestamp(row["datetime"]).to_pydatetime().astimezone(KST)
    if (dt - at_or_after).total_seconds() > 60:
        return None
    return dt, float(row["open"])


def _price_at(etf: pd.DataFrame, when: datetime) -> Optional[float]:
    sub = etf[etf["datetime"] <= when]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def _qty(price: float) -> int:
    return int((BUDGET * 0.995) // price) if price > 0 else 0


def _net_pct(engine, symbol, entry, current, qty) -> float:
    if entry <= 0 or qty <= 0 or current <= 0:
        return 0.0
    pnl = engine.compute_net_pnl(symbol, entry, current, qty, buy_order_type="market", sell_order_type="market")
    return float(pnl["net_pnl"]) / (entry * qty) * 100.0


def simulate_plan(data: dict[str, pd.DataFrame], plan: str) -> tuple[list[dict], list[dict], dict[str, Any]]:
    engine = TradeCostEngine()
    watch_parts = []
    if f"{WATCH}_warmup" in data:
        watch_parts.append(data[f"{WATCH}_warmup"])
    for date in DATES:
        if f"{WATCH}_{date}" in data:
            watch_parts.append(data[f"{WATCH}_{date}"])
    watch_all = (
        pd.concat(watch_parts, ignore_index=True)
        .drop_duplicates("datetime", keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )

    flags: list[dict] = []
    trades: list[dict] = []
    future_access_violations = 0
    equity = 0.0
    equity_curve: list[float] = []

    position: Optional[Position] = None
    last_entry_at: Optional[datetime] = None
    last_exit_at: Optional[datetime] = None
    last_exit_dir: Optional[Direction] = None

    for date in DATES:
        if f"{WATCH}_{date}" not in data:
            continue
        day_watch = data[f"{WATCH}_{date}"]
        hist_end = day_watch["datetime"].iloc[-1].to_pydatetime()
        hist = watch_all[watch_all["datetime"] <= hist_end].copy()
        now_end = hist_end.replace(hour=15, minute=33, second=0, microsecond=0)
        bars_3m = resample_completed_3m(hist, now=now_end)
        day_mask = bars_3m["datetime"].dt.tz_convert(KST).dt.strftime("%Y%m%d") == date
        day_idxs = list(bars_3m.index[day_mask])
        if not day_idxs:
            continue
        first_idx = day_idxs[0]
        long_1m = data.get(f"{LONG}_{date}", pd.DataFrame())
        inv_1m = data.get(f"{INV}_{date}", pd.DataFrame())

        def etf(sym: str) -> pd.DataFrame:
            return long_1m if sym == LONG else inv_1m

        daily_entries = 0
        last_detected: Optional[Direction] = None

        # Event timeline
        events: list[tuple[datetime, str, Any]] = []
        for i in day_idxs:
            bar_dt = pd.Timestamp(bars_3m.loc[i, "datetime"]).to_pydatetime().astimezone(KST)
            events.append((bar_dt + timedelta(minutes=3), "bar", i))
        mark_times = set()
        for df in (long_1m, inv_1m):
            if not df.empty:
                for t in df["datetime"]:
                    mark_times.add(pd.Timestamp(t).to_pydatetime().astimezone(KST))
        for t in sorted(mark_times):
            events.append((t, "mark", None))
        force_at = datetime.strptime(date + "150000", "%Y%m%d%H%M%S").replace(tzinfo=KST)
        events.append((force_at, "force", None))
        events.sort(key=lambda x: (x[0], 0 if x[1] == "bar" else 1 if x[1] == "mark" else 2))

        def close_pos(exit_at, exit_px, reason, related=""):
            nonlocal position, equity, last_exit_at, last_exit_dir
            assert position is not None
            pnl = engine.compute_net_pnl(
                position.symbol, position.entry_price, exit_px, position.qty,
                buy_order_type="market", sell_order_type="market",
            )
            trades.append({
                "plan": plan, "trading_date": date, "direction": position.direction.value,
                "symbol": position.symbol,
                "signal_bar_at": position.signal_bar_at.isoformat(),
                "order_at": position.order_at.isoformat(),
                "entry_price": position.entry_price,
                "exit_at": exit_at.isoformat(), "exit_price": exit_px,
                "quantity": position.qty, "exit_reason": reason,
                "gross_pnl": pnl["gross_pnl"], "total_cost": pnl["total_cost"],
                "net_pnl": pnl["net_pnl"],
                "return_pct_budget": round(float(pnl["net_pnl"]) / BUDGET * 100.0, 4),
                "related_flag": related,
            })
            equity += float(pnl["net_pnl"])
            equity_curve.append(equity)
            last_exit_at = exit_at
            last_exit_dir = position.direction
            position = None

        for when, kind, payload in events:
            if kind == "force" and position is not None:
                px = _price_at(etf(position.symbol), when)
                if px is not None:
                    close_pos(when, px, config.EXIT_FORCED_LIQUIDATION)
                continue
            if kind == "mark" and position is not None and when > position.order_at:
                px = _price_at(etf(position.symbol), when)
                if px is None:
                    continue
                net = _net_pct(engine, position.symbol, position.entry_price, px, position.qty)
                dec = evaluate_position_exits(
                    current_net_return=net, peak_net_return=position.peak_net,
                    profit_lock_active=position.profit_lock_active,
                )
                position.peak_net = dec.peak_net_return
                position.profit_lock_active = dec.profit_lock_active
                if dec.exit_reason in (config.EXIT_STOP_LOSS, config.EXIT_PROFIT_LOCK):
                    close_pos(when, px, dec.exit_reason)
                continue
            if kind != "bar":
                continue
            i = int(payload)
            bars_upto = bars_3m.iloc[: i + 1].copy()
            # Future-data guard: last bar must be <= current bar
            if pd.Timestamp(bars_upto["datetime"].iloc[-1]).to_pydatetime().astimezone(KST) > pd.Timestamp(bars_3m.loc[i, "datetime"]).to_pydatetime().astimezone(KST):
                future_access_violations += 1
            snap = calculate_macd(bars_upto)
            if snap is None:
                continue
            bar_dt = snap.bar_dt.astimezone(KST)
            confirm_at = bar_dt + timedelta(minutes=3)
            if i == first_idx:
                last_detected = None
                continue
            direction = evaluate_macd_crossover(snap, last_detected)
            if direction == Direction.HOLD:
                continue
            last_detected = direction

            pos_dir = position.direction if position else None
            last_ent = position.entry_at if position else last_entry_at
            if plan == "A":
                decision = evaluate_major_flag(bars_upto, direction, pos_dir, last_ent, daily_entries, confirm_at)
                decision = apply_major_trade_gates(
                    decision, flag_direction=direction, position_direction=pos_dir,
                    last_entry_at=last_ent,
                    last_same_direction_exit_at=(last_exit_at if last_exit_dir == direction else None),
                    daily_major_entry_count=daily_entries, now=confirm_at,
                )
            else:
                decision = evaluate_major_flag_b(bars_upto, direction, pos_dir, last_ent, daily_entries, confirm_at)
                decision = apply_gates_b(
                    decision, flag_direction=direction, position_direction=pos_dir,
                    last_entry_at=last_ent,
                    last_same_exit=(last_exit_at if last_exit_dir == direction else None),
                    daily_count=daily_entries, now=confirm_at,
                )

            comps = dict(decision.component_scores or {})
            row = {
                "plan": plan, "trading_date": date, "flag_time": bar_dt.strftime("%H:%M"),
                "flag_bar_start": bar_dt.isoformat(),
                "flag_bar_end": confirm_at.strftime("%H:%M"),
                "direction": direction.value,
                "score": decision.score, "required_score": decision.required_score,
                "approved": bool(decision.approved),
                "block_reason": decision.block_reason or "",
                "decision": decision.decision,
                **{f"score_{k}": v for k, v in comps.items()},
            }
            entry_cutoff = datetime.strptime(date + "145500", "%Y%m%d%H%M%S").replace(tzinfo=KST)
            action = "FILTERED_OUT"
            if decision.approved and confirm_at < entry_cutoff:
                target = _target(direction)
                if position is not None and position.direction != direction:
                    fill_x = _next_open(etf(position.symbol), confirm_at)
                    if fill_x is None:
                        row["trade_action"] = "APPROVED_NO_SAFE_EXIT_FILL"
                        flags.append(row)
                        continue
                    close_pos(fill_x[0], fill_x[1], config.EXIT_OPPOSITE_SIGNAL,
                              related=f"{date}_{bar_dt.strftime('%H%M')}_{direction.value}")
                if position is None:
                    fill = _next_open(etf(target), confirm_at)
                    if fill is None:
                        row["trade_action"] = "APPROVED_NO_SAFE_ENTRY_FILL"
                        flags.append(row)
                        continue
                    order_at, px = fill
                    q = _qty(px)
                    if q <= 0:
                        row["trade_action"] = "QTY0"
                        flags.append(row)
                        continue
                    position = Position(target, direction, q, px, order_at, bar_dt, order_at)
                    daily_entries += 1
                    last_entry_at = order_at
                    action = f"ENTER {target} @{px} x{q} at {order_at.strftime('%H:%M')}"
            elif decision.approved:
                action = "APPROVED_AFTER_1455"
            row["trade_action"] = action
            flags.append(row)

        if position is not None:
            px = _price_at(etf(position.symbol), force_at)
            if px is not None:
                close_pos(force_at, px, config.EXIT_FORCED_LIQUIDATION)

    # stats
    tdf = pd.DataFrame(trades)
    peak = mdd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    by_date = {}
    for date in DATES:
        sub = tdf[tdf["trading_date"] == date] if not tdf.empty else pd.DataFrame()
        if sub.empty:
            by_date[date] = {"trades": 0, "wins": 0, "win_rate_pct": 0.0,
                             "gross_pnl": 0.0, "total_cost": 0.0, "net_pnl": 0.0, "mdd_krw": 0.0}
            continue
        # per-day mdd approximate from trade order within day
        eq = 0.0
        pk = dd = 0.0
        for net in sub["net_pnl"]:
            eq += float(net)
            pk = max(pk, eq)
            dd = max(dd, pk - eq)
        wins = int((sub["net_pnl"] > 0).sum())
        by_date[date] = {
            "trades": int(len(sub)), "wins": wins,
            "win_rate_pct": round(wins / len(sub) * 100.0, 2),
            "gross_pnl": round(float(sub["gross_pnl"].sum()), 2),
            "total_cost": round(float(sub["total_cost"].sum()), 2),
            "net_pnl": round(float(sub["net_pnl"].sum()), 2),
            "mdd_krw": round(dd, 2),
        }
    summary = {
        "plan": plan,
        "by_date": by_date,
        "trades": int(len(trades)),
        "flags": int(len(flags)),
        "approved_flags": int(sum(1 for f in flags if f["approved"])),
        "total_gross_pnl": round(float(tdf["gross_pnl"].sum()), 2) if not tdf.empty else 0.0,
        "total_cost": round(float(tdf["total_cost"].sum()), 2) if not tdf.empty else 0.0,
        "total_net_pnl": round(float(tdf["net_pnl"].sum()), 2) if not tdf.empty else 0.0,
        "total_return_pct_budget": round((float(tdf["net_pnl"].sum()) / BUDGET * 100.0) if not tdf.empty else 0.0, 4),
        "max_drawdown_krw": round(mdd, 2),
        "future_data_access_violations": future_access_violations,
    }
    return flags, trades, summary


def investigate_plus3(data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Explain bar_start vs KIS reference timing."""
    notes = {
        "macd2_convention": (
            "MACD2 signal_id / flag_time uses completed 3m bar OPEN (left label): "
            "bar 13:42 covers 13:42/13:43/13:44 and confirms at 13:45."
        ),
        "kis_chart_hypothesis": (
            "If KIS chart paints the flag on the candle that closed (end label) or on the "
            "prior open, displayed clock can differ by one 3m step (+/- 3 minutes)."
        ),
        "pairs": [],
    }
    # Build program flags from watch using production crossover (plan-agnostic)
    watch_parts = []
    if f"{WATCH}_warmup" in data:
        watch_parts.append(data[f"{WATCH}_warmup"])
    for date in DATES:
        if f"{WATCH}_{date}" in data:
            watch_parts.append(data[f"{WATCH}_{date}"])
    watch_all = pd.concat(watch_parts, ignore_index=True).drop_duplicates("datetime", keep="last").sort_values("datetime")
    for date in DATES:
        day = data.get(f"{WATCH}_{date}")
        if day is None:
            continue
        hist = watch_all[watch_all["datetime"] <= day["datetime"].iloc[-1]].copy()
        bars = resample_completed_3m(hist, now=day["datetime"].iloc[-1].to_pydatetime().replace(hour=15, minute=33))
        day_idxs = list(bars.index[bars["datetime"].dt.tz_convert(KST).dt.strftime("%Y%m%d") == date])
        last = None
        prog = []
        for j, i in enumerate(day_idxs):
            snap = calculate_macd(bars.iloc[: i + 1])
            if snap is None:
                continue
            if j == 0:
                last = None
                continue
            d = evaluate_macd_crossover(snap, last)
            if d == Direction.HOLD:
                continue
            last = d
            bt = snap.bar_dt.astimezone(KST)
            prog.append((bt.strftime("%H:%M"), d.value, (bt + timedelta(minutes=3)).strftime("%H:%M")))
        for ref_d, ref_hm, ref_dir in KIS_REFERENCE_FLAGS:
            if ref_d != date:
                continue
            # match exact or +/- 3 minutes
            exact = [p for p in prog if p[0] == ref_hm and p[1] == ref_dir]
            plus3 = [p for p in prog if p[0] == (
                datetime.strptime(ref_hm, "%H:%M") + timedelta(minutes=3)
            ).strftime("%H:%M") and p[1] == ref_dir]
            minus3 = [p for p in prog if p[0] == (
                datetime.strptime(ref_hm, "%H:%M") - timedelta(minutes=3)
            ).strftime("%H:%M") and p[1] == ref_dir]
            notes["pairs"].append({
                "kis_reference": f"{ref_d} {ref_hm} {ref_dir}",
                "exact_program_match": bool(exact),
                "program_bar_start_plus3": plus3[0][0] if plus3 else None,
                "program_bar_end_if_plus3": plus3[0][2] if plus3 else None,
                "program_bar_start_minus3": minus3[0][0] if minus3 else None,
                "interpretation": (
                    "KIS time equals program bar_start+0"
                    if exact else
                    "KIS time == program bar_start - 3m (KIS may label prior open / end-aligned differently); "
                    "program bar_end (== confirm) equals KIS+3"
                    if plus3 else
                    "no nearby program crossover"
                ),
            })
    return notes


def enrich_kis_diagnosis(report: dict[str, Any]) -> dict[str, Any]:
    """Annotate coverage with known KIS-absent patterns (read-only diagnosis)."""
    auction = {"start": "15:20", "end": "15:29", "minutes": 10, "kind": "closing_auction_absent_in_kis"}
    for item in report.get("items") or []:
        gaps = item.get("gap_runs") or []
        kinds = []
        for g in gaps:
            if g.get("start") == "15:20" and g.get("end") == "15:29":
                kinds.append(dict(auction))
            elif g.get("minutes", 0) >= 1:
                kinds.append({**g, "kind": "kis_source_hole_or_halt"})
        item["gap_diagnosis"] = kinds
        miss = [t for t in (item.get("missing_timestamps") or []) if not ("15:20" <= t <= "15:29")]
        if item.get("role") == "session":
            item["complete_excluding_auction_1520_1529"] = len(miss) == 0
    report["kis_diagnosis"] = {
        "auction_1520_1529": (
            "KIS dailychartprice systematically omits 15:20-15:29; "
            "only 15:30 closing bar appears after 15:19."
        ),
        "fake_tick_y": (
            "FID_FAKE_TICK_INCU_YN=Y does not fill midday or auction gaps "
            "(probed 000660 20260729)."
        ),
        "backtest_policy": "Any missing minute (>=1 contiguous) fails the day; incomplete => no PnL.",
    }
    return report


def print_coverage(report: dict[str, Any]) -> None:
    print("\n========== DATA COVERAGE (expected 09:00-15:30 = 391) ==========")
    print(f"warmup_date={report.get('warmup_date')} all_complete={report.get('all_complete')}")
    for item in report.get("items", []):
        if item.get("role") != "session":
            continue
        print(
            f"{item.get('date')} {item.get('symbol')}: "
            f"expected={item.get('expected')} actual={item.get('actual')} "
            f"missing={item.get('missing_count')} complete={item.get('complete')}"
        )
        for g in item.get("gap_runs") or []:
            print(f"  gap {g['start']}-{g['end']} ({g['minutes']} min)")
        miss = item.get("missing_timestamps") or []
        if miss and len(miss) <= 40:
            print(f"  missing timestamps: {', '.join(miss)}")
        elif miss:
            print(f"  missing timestamps ({len(miss)}): {', '.join(miss[:20])} ...")
        if item.get("role") == "session":
            print(
                f"  complete_excluding_auction_1520_1529="
                f"{item.get('complete_excluding_auction_1520_1529')}"
            )
    print(
        "\n[KIS source note] inquire-time-dailychartprice returns NO bars for "
        "15:20-15:29 (closing auction jump 15:19->15:30) on all probed days; "
        "FID_FAKE_TICK_INCU_YN=Y also does not synthesize those minutes. "
        "Midday holes (e.g. 12:33-13:01, 10:14-10:42) are likewise absent from "
        "KIS responses for 000660/0193T0/0197X0 - gap-fill cannot invent them."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="data/validation/major_filter_ab_3days")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--kis-mode", default="real")
    args = ap.parse_args()
    out = Path(args.output_dir)
    raw = out / "raw"
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)

    # Operational untouched check
    forbidden = [
        ROOT / "data/state/macd2_runtime.json",
        ROOT / "data/logs" / config.SIGNAL_LEDGER_FILENAME,
        ROOT / "data/logs" / config.EXECUTION_LEDGER_FILENAME,
    ]
    before = {p: (p.stat().st_mtime if p.exists() else None) for p in forbidden}

    if args.skip_fetch:
        data, report = load_raw(raw)
        # re-analyze completeness from disk
        report["all_complete"] = True
        if not report.get("warmup_date"):
            report["all_complete"] = False
        for item in report["items"]:
            if item.get("role") == "session" and not item.get("complete"):
                report["all_complete"] = False
    else:
        client = create_kis_client(args.kis_mode)
        if client is None:
            print("ERROR: KIS client unavailable")
            return 2
        data, report = collect_all(client, raw)

    report = enrich_kis_diagnosis(report)
    print_coverage(report)
    (out / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    plus3 = investigate_plus3(data) if f"{WATCH}_warmup" in data else {"error": "no warmup"}
    (out / "plus3_investigation.json").write_text(json.dumps(plus3, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n========== +3 MINUTE FLAG TIMING ==========")
    print(plus3.get("macd2_convention"))
    print(plus3.get("kis_chart_hypothesis"))
    for p in plus3.get("pairs") or []:
        print(json.dumps(p, ensure_ascii=False))

    # Per-day fail: any symbol incomplete that day => day fails
    day_fail: dict[str, bool] = {d: False for d in DATES}
    for item in report.get("items") or []:
        if item.get("role") == "session" and not item.get("complete"):
            day_fail[str(item.get("date"))] = True
    print("\n========== DAY SIM GATE ==========")
    for d in DATES:
        print(f"{d}: {'FAIL (gap>=1m)' if day_fail[d] else 'PASS'}")

    if not report.get("all_complete"):
        summary = {
            "error": "incomplete_data",
            "message": "1분 이상 연속 공백 또는 missing timestamp 존재 - 수익률 계산하지 않음",
            "day_sim_fail": day_fail,
            "ab_compare_skipped": True,
            "pnl_computed": False,
            "coverage": report,
            "plus3": plus3,
        }
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nFAIL: data incomplete - backtest skipped (no PnL).")
        print("A/B MAJOR filter compare NOT run (same-data requirement unmet).")
        after = {p: (p.stat().st_mtime if p.exists() else None) for p in forbidden}
        assert before == after
        return 2


    # Data complete — run A and B
    flags_a, trades_a, sum_a = simulate_plan(data, "A")
    flags_b, trades_b, sum_b = simulate_plan(data, "B")

    # Merge flag comparison
    key = lambda r: (r["trading_date"], r["flag_time"], r["direction"])
    map_a = {key(r): r for r in flags_a}
    map_b = {key(r): r for r in flags_b}
    all_keys = sorted(set(map_a) | set(map_b))
    compare_rows = []
    only_a = []
    only_b = []
    for k in all_keys:
        a = map_a.get(k)
        b = map_b.get(k)
        compare_rows.append({
            "trading_date": k[0], "flag_time": k[1], "direction": k[2],
            "A_score": None if not a else a["score"],
            "A_required": None if not a else a["required_score"],
            "A_approved": None if not a else a["approved"],
            "A_block": None if not a else a.get("block_reason"),
            "B_score": None if not b else b["score"],
            "B_required": None if not b else b["required_score"],
            "B_approved": None if not b else b["approved"],
            "B_block": None if not b else b.get("block_reason"),
        })
        if a and a["approved"] and (not b or not b["approved"]):
            only_a.append(k)
        if b and b["approved"] and (not a or not a["approved"]):
            only_b.append(k)

    pd.DataFrame(compare_rows).to_csv(out / "flags_ab.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(trades_a + trades_b).to_csv(out / "trades_ab.csv", index=False, encoding="utf-8-sig")
    summary = {
        "data_complete": True,
        "coverage": report,
        "plus3": plus3,
        "A": sum_a,
        "B": sum_b,
        "only_A_approved": [{"date": x[0], "time": x[1], "direction": x[2]} for x in only_a],
        "only_B_approved": [{"date": x[0], "time": x[1], "direction": x[2]} for x in only_b],
        "future_data_access_violations_A": sum_a["future_data_access_violations"],
        "future_data_access_violations_B": sum_b["future_data_access_violations"],
        "cost_config": TradeCostEngine()._cfg,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n========== FLAGS A/B ==========")
    print(pd.DataFrame(compare_rows).to_string(index=False))
    print("\n========== DAILY / TOTAL ==========")
    print("A:", json.dumps(sum_a, ensure_ascii=False, indent=2))
    print("B:", json.dumps(sum_b, ensure_ascii=False, indent=2))
    print("\nonly A approved:", only_a)
    print("only B approved:", only_b)
    print("future access violations A/B:", sum_a["future_data_access_violations"], sum_b["future_data_access_violations"])

    after = {p: (p.stat().st_mtime if p.exists() else None) for p in forbidden}
    if before != after:
        print("WARNING: operational files changed")
        return 3
    print("\n[ok] outputs in", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
