#!/usr/bin/env python
"""2026-08-17 사용자 요청: "시간대별 최적거래 필터" + "게이트 전체 완화"를
baseline으로, 하루 2~3회 거래를 유지하면서 누적수익 70%+를 목표로 하는 추가
파라미터 탐색. 진입조건 먼저 최적화 -> 고정 -> 청산(TP/SL) 최적화 순서로
진행하고, TRAIN(60%)/VALIDATION(20%)/FINAL OOS(20%, 단 1회) 분할로 과최적화를
점검한다.

Strictly read-only research: never touches app/trading/macd2 production code
or the real runtime state/ledger. Reuses REAL decision functions (signal_engine
.calculate_macd/evaluate_macd_crossover, time_window_filter.calculate_flag_
quality_score/is_valid_reset/classify_window/session_for_window, time_window_
position_manager.evaluate_morning_position/evaluate_afternoon_position,
TradeCostEngine) -- only the entry-gate ORCHESTRATION is reimplemented here in
a parametrized form (evaluate_relaxed_entry), since production code has no
knobs for "require_gap_expansion=False" / "blocked_windows" / per-direction
quality bonus / arbitrary flag-interval -- these are exactly what this task
needs to sweep. TP1/TP2/stop-loss values are swept by temporarily overriding
time_window_position_manager's own module-level *_PCT constants (same
technique scripts/backtest_time_window_filter.py's own run_sweep() already
uses), never by duplicating the ladder math.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.trading.macd2 import config, time_window_filter as twf, time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.major_flag_filter import _as_direction, _direction_sign  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m  # noqa: E402
from app.trading.macd2.worker import _net_return_pct  # noqa: E402
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402

KST = config.KST
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw_gate_relaxed_optimization"
SESSION_END = dtime(15, 30)
COST_ENGINE = TradeCostEngine()

FULL_DATES = [
    "20260527", "20260528", "20260529", "20260601", "20260602", "20260604", "20260605",
    "20260608", "20260609", "20260610", "20260611", "20260612", "20260615", "20260616",
    "20260617", "20260618", "20260619", "20260622", "20260623", "20260624", "20260625",
    "20260626", "20260629", "20260630", "20260701", "20260702", "20260703", "20260706",
    "20260707", "20260708", "20260709", "20260710", "20260713", "20260714", "20260715",
    "20260716", "20260720", "20260721", "20260722", "20260723", "20260724", "20260727",
    "20260728", "20260729", "20260730", "20260731", "20260803", "20260804", "20260805",
    "20260806", "20260807", "20260810", "20260811", "20260812", "20260813", "20260814",
]
TRAIN_DATES = FULL_DATES[:34]   # 20260527~20260714 (60%)
VAL_DATES = FULL_DATES[34:45]   # 20260715~20260730 (20%)
OOS_DATES = FULL_DATES[45:]     # 20260731~20260814 (20%, run ONCE)
assert len(TRAIN_DATES) == 34 and len(VAL_DATES) == 11 and len(OOS_DATES) == 11


# ── data loading (mirrors scripts/backtest_time_window_filter.py exactly) ──
def _all_cached_hynix_dates() -> list[str]:
    dates = set()
    for path in CACHE_DIR.glob("replay_*_hynix_1m.csv"):
        parts = path.stem.split("_")
        if len(parts) >= 3 and parts[1].isdigit() and len(parts[1]) == 8:
            dates.add(parts[1])
    return sorted(dates)


_ALL_HYNIX_DATES = _all_cached_hynix_dates()


def _prior_trading_date(date: str) -> Optional[str]:
    earlier = [d for d in _ALL_HYNIX_DATES if d < date]
    return earlier[-1] if earlier else None


def _load_1m(date: str, tag: str) -> Optional[pd.DataFrame]:
    path = CACHE_DIR / f"replay_{date}_{tag}_1m.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize(KST)
    return df.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)


def _load_hynix_with_warmup(date: str) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    current = _load_1m(date, "hynix")
    if current is None:
        return current, None
    prior_date = _prior_trading_date(date)
    if prior_date is None:
        return current, None
    prior = _load_1m(prior_date, "hynix")
    if prior is None:
        return current, None
    combined = pd.concat([prior, current], ignore_index=True)
    combined = combined.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)
    return combined, prior_date


def _session_end_dt(date: str) -> datetime:
    day = datetime.strptime(date, "%Y%m%d").replace(tzinfo=KST)
    return day.replace(hour=SESSION_END.hour, minute=SESSION_END.minute)


def detect_confirmed_flags(bars_3m: pd.DataFrame, current_date: str) -> list[tuple[int, Direction]]:
    """Mirrors app/trading/macd2/worker.py's REAL day-boundary behavior
    exactly (2026-08-18 second fix -- see worker.py's _advance_confirmed_
    primary docstring for the full writeup).

    Old (pre-2026-08-18) behavior forced HOLD on the first completed bar of
    every new calendar date, on the theory that a zero-crossing spanning
    yesterday's last bar into today's first is always an overnight-gap
    artifact rather than a genuine reversal. Real KIS has no "trading day"
    concept at all -- it is one continuous EMA/MACD line -- so a large
    genuine overnight-gap crossing DOES show up as a real KIS flag (verified
    2026-08-18: a +5.53% gap produced a real 09:00 UP_RED KIS flag this used
    to silently swallow). The gate is now removed entirely: every completed
    bar, including the day's first, is evaluated the same way via
    evaluate_macd_crossover. ``previous_direction`` is still reset to None
    on the day-boundary branch (mirrors worker.py's separate day-rollover
    reset of ``state.last_detected_direction``), so the first crossover of a
    new day is never suppressed as a stale repeat of yesterday's last
    direction -- it just now actually dispatches instead of being forced to
    HOLD.
    """
    flags: list[tuple[int, Direction]] = []
    previous_direction: Optional[Direction] = None
    last_bar_date: Optional[str] = None
    for i in range(len(bars_3m)):
        snap = calculate_macd(bars_3m.iloc[: i + 1])
        if snap is None:
            continue
        bar_date = pd.Timestamp(bars_3m["datetime"].iloc[i]).astimezone(KST).strftime("%Y%m%d")
        is_first_of_day = last_bar_date is None or bar_date != last_bar_date
        last_bar_date = bar_date
        if is_first_of_day:
            previous_direction = None  # mirrors worker.py's day-rollover state reset
        direction = evaluate_macd_crossover(snap, previous_direction)
        if direction in (Direction.UP_RED, Direction.DOWN_BLUE):
            previous_direction = direction
            if bar_date == current_date:
                flags.append((i, direction))
    return flags


def _target_symbol(direction: Direction) -> str:
    return config.LONG_SYMBOL if direction == Direction.UP_RED else config.INVERSE_SYMBOL


def _direction_for_symbol(symbol: str) -> Optional[Direction]:
    if symbol == config.LONG_SYMBOL:
        return Direction.UP_RED
    if symbol == config.INVERSE_SYMBOL:
        return Direction.DOWN_BLUE
    return None


def _etf_close_lookup(etf_bars_3m: pd.DataFrame) -> dict:
    return dict(zip(etf_bars_3m["datetime"], etf_bars_3m["close"]))


def _net_pct(symbol: str, entry_price: float, exit_price: float) -> float:
    return _net_return_pct(symbol, entry_price, exit_price, 1)


def _prepare_day_cache(dates: list[str]) -> tuple[list[dict], list[str]]:
    cache = []
    notes = []
    for date in dates:
        hynix_1m_warm, prior_date = _load_hynix_with_warmup(date)
        long_1m = _load_1m(date, "long")
        inverse_1m = _load_1m(date, "inverse")
        if hynix_1m_warm is None or long_1m is None or inverse_1m is None:
            notes.append(f"{date}: missing 1m data -- skipped")
            continue
        end = _session_end_dt(date)
        hynix_bars_3m = resample_completed_3m(hynix_1m_warm, now=end)
        long_bars_3m = resample_completed_3m(long_1m, now=end)
        inverse_bars_3m = resample_completed_3m(inverse_1m, now=end)
        etf_close = {
            config.LONG_SYMBOL: _etf_close_lookup(long_bars_3m),
            config.INVERSE_SYMBOL: _etf_close_lookup(inverse_bars_3m),
        }
        current_day_mask = hynix_bars_3m["datetime"].dt.strftime("%Y%m%d") == date
        if not current_day_mask.any():
            notes.append(f"{date}: no completed 3m bars for this date -- skipped")
            continue
        start_idx = int(current_day_mask.to_numpy().nonzero()[0][0])
        flags = detect_confirmed_flags(hynix_bars_3m, date)
        cache.append({
            "date": date, "hynix_bars_3m": hynix_bars_3m, "flags": flags,
            "etf_close": etf_close, "start_idx": start_idx,
        })
    return cache, notes


# ── parametrized entry gate (generalizes the "게이트 전체 완화" baseline) ──
@dataclass
class EntryParams:
    quality_threshold: int = 2
    require_gap_expansion: bool = True
    min_flag_interval_minutes: int = 9
    max_morning_entries: int = 3
    max_afternoon_entries: int = 2
    max_daily_entries: int = 5
    blocked_windows: tuple = ()          # window label strings to exclude entirely
    direction_quality_bonus: dict = field(default_factory=dict)  # {Direction: +N required-score adjustment}
    excluded_quality_scores: tuple = ()  # exact quality_score values to reject even if >= threshold
    blocked_window_directions: tuple = ()  # (window, Direction) pairs to exclude entirely


def evaluate_relaxed_entry(
    bars_3m, flag_direction, flag_bar_dt, decision_at, *, params: EntryParams,
    position_direction=None, morning_entry_count=0, afternoon_entry_count=0,
):
    direction = _as_direction(flag_direction)
    if direction is None:
        return None, {"reject": "bad_direction"}

    work = twf._prepare_bars(bars_3m)
    if work is None:
        return None, {"reject": "insufficient_bars"}
    series = twf._gap_series(work)
    if series is None:
        return None, {"reject": "insufficient_series"}

    flag_rows = series.index[series["datetime"] == flag_bar_dt]
    if len(flag_rows) == 0 or int(flag_rows[-1]) != len(series) - 2:
        return None, {"reject": "not_confirmed_bar_alignment"}
    flag_idx = int(flag_rows[-1])
    confirm_idx = len(series) - 1

    sign = _direction_sign(direction)
    gap = series["gap"] * sign
    gap_flag = float(gap.iloc[flag_idx])
    gap_now = float(gap.iloc[confirm_idx])

    if gap_now <= 0:
        return None, {"reject": "macd_signal_not_held"}
    if params.require_gap_expansion and not (gap_now > gap_flag):
        return None, {"reject": "gap_not_expanding"}

    prev_opposite_idx = twf._find_previous_opposite_flag(series, flag_idx, direction)
    interval_minutes = None
    if prev_opposite_idx is not None:
        interval_minutes = (series["datetime"].iloc[flag_idx] - series["datetime"].iloc[prev_opposite_idx]).total_seconds() / 60.0
    if interval_minutes is not None and interval_minutes < params.min_flag_interval_minutes:
        reset_ok, _ = twf.is_valid_reset(bars_3m, direction, flag_bar_dt)
        if not reset_ok:
            return None, {"reject": "short_flag_interval_no_reset"}

    window = twf.classify_window(decision_at.astimezone(KST).time())
    if window is None or window in params.blocked_windows:
        return None, {"reject": "time_window_blocked", "window": window}

    if window == twf.WINDOW_AFTERNOON_2 and decision_at.astimezone(KST).time() >= config.TW_AFTERNOON_ENTRY_HARD_CUTOFF:
        return None, {"reject": "past_afternoon_hard_cutoff", "window": window}

    session = twf.session_for_window(window)
    price_ema_ref = "ema20" if window in (twf.WINDOW_AFTERNOON_1, twf.WINDOW_AFTERNOON_2, twf.WINDOW_NO_NEW_ENTRY) else "ema10"
    quality_score, quality_detail = twf.calculate_flag_quality_score(bars_3m, direction, flag_gap=gap_flag, price_ema_ref=price_ema_ref)

    required = params.quality_threshold + params.direction_quality_bonus.get(direction, 0)
    if quality_score < required:
        return None, {"reject": "low_quality_score", "window": window, "quality_score": quality_score, "required": required}
    if quality_score in params.excluded_quality_scores:
        return None, {"reject": "excluded_quality_score", "window": window, "quality_score": quality_score}
    if (window, direction) in params.blocked_window_directions:
        return None, {"reject": "blocked_window_direction", "window": window}

    if session == "MORNING" and morning_entry_count >= params.max_morning_entries:
        return None, {"reject": "max_morning_entries", "window": window}
    if session == "AFTERNOON" and afternoon_entry_count >= params.max_afternoon_entries:
        return None, {"reject": "max_afternoon_entries", "window": window}
    if (morning_entry_count + afternoon_entry_count) >= params.max_daily_entries:
        return None, {"reject": "max_daily_entries", "window": window}

    if position_direction == direction and not config.ALLOW_PYRAMIDING:
        return None, {"reject": "duplicate_position", "window": window}

    return True, {
        "window": window, "session": session, "quality_score": quality_score,
        "gap_flag": gap_flag, "gap_now": gap_now,
    }


# ── trade record ────────────────────────────────────────────────────────────
@dataclass
class Trade:
    trading_date: str
    direction: str
    flag_time: str
    entry_time: str
    entry_symbol: str
    entry_price: float
    window: str
    quality_score: int
    flag_seq_of_day: int   # 1-based sequence number of this ENTRY among the day's entries
    tp1_hit: bool = False
    tp2_hit: bool = False
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    net_return_pct: Optional[float] = None  # qty-weighted blend across ALL sold legs (TP1 partial + final)
    legs: list = field(default_factory=list)  # [(qty_fraction, price, leg_reason), ...] for inspection/reporting


@dataclass
class OpenPosition:
    symbol: str
    entry_idx: int
    entry_price: float
    entry_time: datetime
    session: str
    tp1_done: bool = False
    peak_net_return: float = 0.0
    remaining_fraction: float = 1.0  # shrinks on each partial (TP1) sell
    trade: Trade = None


def _record_partial_leg(position: "OpenPosition", *, qty_fraction: float, price: float, reason: str) -> None:
    """TP1 sells ``qty_fraction`` of whatever is CURRENTLY held (matches
    production's own sell_fraction semantics) -- converted to a fraction of
    the ORIGINAL entry quantity so the final blended return correctly
    weights every leg by its true share of the original position."""
    absolute_fraction = position.remaining_fraction * qty_fraction
    position.trade.legs.append((absolute_fraction, price, reason))
    position.remaining_fraction -= absolute_fraction


def _close_trade(trade: Trade, *, exit_time, exit_price, reason, entry_price, symbol, remaining_fraction: float) -> None:
    """Final leg closes whatever fraction of the original quantity is still
    held. ``net_return_pct`` is the QUANTITY-WEIGHTED blend across every leg
    (TP1 partial(s) + this final exit) -- this is what actually changes when
    MORNING_TP1_SELL_RATIO is swept; the naive single entry->final-price %
    (what the original backtest_time_window_filter.py script uses) is blind
    to the sell ratio entirely, since it never depends on it."""
    trade.exit_time = exit_time.isoformat()
    trade.exit_price = exit_price
    trade.exit_reason = reason
    if remaining_fraction > 1e-9:
        trade.legs.append((remaining_fraction, exit_price, reason))
    trade.net_return_pct = sum(
        frac * _net_pct(symbol, entry_price, price) for frac, price, _r in trade.legs
    )


def simulate(date: str, hynix_bars_3m, flags, etf_close, *, entry_params: EntryParams, start_idx: int = 0) -> list[Trade]:
    """``hynix_bars_3m`` is the FULL frame (prior-day warmup bars + today's),
    never sliced -- ``start_idx`` is where TODAY's own bars begin within it,
    so every ``hynix_bars_3m.iloc[:idx+1]`` passed to a decision function
    still carries its full EMA/MACD warm-up prefix (mirrors scripts/
    backtest_time_window_filter.py's own convention exactly)."""
    trades: list[Trade] = []
    position: Optional[OpenPosition] = None
    flags_by_idx = dict(flags)
    pending = None
    morning_count = 0
    afternoon_count = 0
    daily_entry_seq = 0

    def position_direction():
        return _direction_for_symbol(position.symbol) if position is not None else None

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()
        bar_ts = hynix_bars_3m["datetime"].iloc[idx]

        # 1) position management ladder (real production decision functions)
        if position is not None and idx > position.entry_idx:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                net = _net_pct(position.symbol, position.entry_price, close)
                if position.session == "MORNING":
                    pm = twpm.evaluate_morning_position(net_return_pct=net, tp1_done=position.tp1_done, peak_net_return=position.peak_net_return)
                else:
                    pm = twpm.evaluate_afternoon_position(net_return_pct=net, peak_net_return=position.peak_net_return)
                position.peak_net_return = pm.peak_net_return
                position.tp1_done = pm.tp1_done
                if pm.exit_reason is not None:
                    if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                        position.trade.tp1_hit = True
                        _record_partial_leg(position, qty_fraction=pm.sell_fraction, price=close, reason=pm.exit_reason)
                    else:
                        if pm.exit_reason == config.EXIT_TW_TP2_FULL:
                            position.trade.tp2_hit = True
                        _close_trade(position.trade, exit_time=bar_dt, exit_price=close, reason=pm.exit_reason, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                        trades.append(position.trade)
                        position = None

        # 2) forced liquidation at/after 15:00
        if position is not None and bar_dt.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position.symbol].get(bar_ts)
            if close is not None:
                _close_trade(position.trade, exit_time=bar_dt, exit_price=close, reason=config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                trades.append(position.trade)
                position = None
            pending = None

        # 3) fresh confirmed crossover -> pending T+3 candidate
        if idx in flags_by_idx:
            pending = (flags_by_idx[idx], idx, bar_ts)

        # 4) resolve pending candidate exactly one bar after its flag bar
        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                decision_at = bar_dt + timedelta(minutes=3)
                approved, info = evaluate_relaxed_entry(
                    hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at, params=entry_params,
                    position_direction=position_direction(), morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                if approved:
                    target = _target_symbol(p_direction)
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position.symbol != target:
                            close_now = etf_close[position.symbol].get(bar_ts, position.entry_price)
                            _close_trade(position.trade, exit_time=bar_dt, exit_price=close_now, reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                            trades.append(position.trade)
                            position = None
                        if position is None:
                            session = info["session"]
                            if session == "MORNING":
                                morning_count += 1
                            else:
                                afternoon_count += 1
                            daily_entry_seq += 1
                            new_trade = Trade(
                                trading_date=date, direction=p_direction.value, flag_time=flag_bar_dt.isoformat(),
                                entry_time=bar_dt.isoformat(), entry_symbol=target, entry_price=fill,
                                window=info["window"], quality_score=info["quality_score"], flag_seq_of_day=daily_entry_seq,
                            )
                            position = OpenPosition(symbol=target, entry_idx=idx, entry_price=fill, entry_time=bar_dt, session=session, trade=new_trade)

    if position is not None:
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime()
        close = etf_close[position.symbol].get(hynix_bars_3m["datetime"].iloc[-1], position.entry_price)
        _close_trade(position.trade, exit_time=last_dt, exit_price=close, reason="END_OF_DATA", entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
        trades.append(position.trade)
    return trades


def run_over_cache(cache: list[dict], entry_params: EntryParams) -> list[Trade]:
    all_trades = []
    for day in cache:
        trades = simulate(
            day["date"], day["hynix_bars_3m"], day["flags"], day["etf_close"],
            entry_params=entry_params, start_idx=day["start_idx"],
        )
        all_trades.extend(trades)
    return all_trades


def metrics(trades: list[Trade], trading_days: int) -> dict[str, Any]:
    closed = [t for t in trades if t.net_return_pct is not None]
    wins = [t for t in closed if t.net_return_pct > 0]
    losses = [t for t in closed if t.net_return_pct <= 0]
    morning = [t for t in closed if t.window in twf._MORNING_WINDOWS]
    afternoon = [t for t in closed if t.window in twf._AFTERNOON_WINDOWS]
    total_simple = sum(t.net_return_pct for t in closed)
    compounded = 1.0
    for t in closed:
        compounded *= (1.0 + t.net_return_pct / 100.0)
    compounded_pct = (compounded - 1.0) * 100.0
    gross_win = sum(t.net_return_pct for t in wins)
    gross_loss = abs(sum(t.net_return_pct for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))

    equity = peak = max_dd = 0.0
    max_consec_losses = consec = 0
    for t in closed:
        equity += t.net_return_pct
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if t.net_return_pct <= 0:
            consec += 1
            max_consec_losses = max(max_consec_losses, consec)
        else:
            consec = 0

    return {
        "trading_days": trading_days,
        "total_entries": len(closed),
        "avg_entries_per_day": round(len(closed) / trading_days, 3) if trading_days else 0.0,
        "morning_entries": len(morning),
        "afternoon_entries": len(afternoon),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2) if closed else 0.0,
        "avg_daily_return_pct": round(total_simple / trading_days, 4) if trading_days else 0.0,
        "total_simple_cumulative_return_pct": round(total_simple, 4),
        "compounded_cumulative_return_pct": round(compounded_pct, 4),
        "profit_factor": (round(profit_factor, 4) if isinstance(profit_factor, float) and profit_factor != float("inf") else profit_factor),
        "max_drawdown_pct": round(max_dd, 4),
        "max_consecutive_losses": max_consec_losses,
    }


if __name__ == "__main__":
    print("Loading TRAIN/VAL/OOS day caches...")
    train_cache, train_notes = _prepare_day_cache(TRAIN_DATES)
    val_cache, val_notes = _prepare_day_cache(VAL_DATES)
    oos_cache, oos_notes = _prepare_day_cache(OOS_DATES)
    print(f"TRAIN={len(train_cache)} days, VAL={len(val_cache)} days, OOS={len(oos_cache)} days")
    for n in train_notes + val_notes + oos_notes:
        print("  note:", n)
