"""'2% 3회진입' filter — Optional Daily Single-Entry filter, pure functions
only, order authority gate only (never creates/suppresses a confirmed
MACD crossover; worker.py's signal_engine crossover detection is
completely untouched).

2026-08-10 v3 (사용자 요청 — 하루 전체 확정 플래그를 계속 평가, 4번째 이후
자동차단 폐지): every confirmed flag of the day is scored, not just the
first few. The daily fill cap (config.SINGLE_ENTRY_MAX_DAILY_ENTRIES) still
blocks a NEW BUY once that many entries have already filled today, but a
low-scoring 1st/2nd/3rd flag can be skipped and a high-scoring 4th+ flag
can still enter:

    score = major_flag_filter's existing 0-100 component score
          + sequence bonus (1st/2nd/3rd get a bonus, 4th+ get 0 -- NOT a
            block; see config.SINGLE_ENTRY_SEQ_BONUS_1/_2/_3)
          + gap-expansion / EMA10-slope / 15m-price-slope bonuses (each
            direction-aligned, computed AT the confirming bar only --
            no future bars, no 1-2 bar confirmation delay)
          - overheat penalty when price_impulse_atr in the flag's own
            direction is >= config.SINGLE_ENTRY_OVERHEAT_THRESHOLD
    approved = (today's fill count < config.SINGLE_ENTRY_MAX_DAILY_ENTRIES)
               and (score >= config.SINGLE_ENTRY_SCORE_MIN)

near-zero BLUE (abs(macd) < config.SINGLE_ENTRY_NEAR_ZERO_MACD_THRESHOLD)
is recorded as a diagnostic only (metrics["near_zero_blue"]) and never
added to the score -- see config.py for why (a 20-25 day sweep found the
unconditional near-zero cohort has a LOWER, not higher, +2% hit rate).

OFF by default (config.SINGLE_ENTRY_FILTER_DEFAULT). Mutually exclusive
with sideways_filter_enabled/major_filter_enabled/trend_persistence_
filter_enabled (see worker._judge_entry_gate priority chain) — never
touches STOP_LOSS/PROFIT_LOCK/order-fill/ledger logic, and gates a NEW BUY
only — the caller still liquidates a held position on a rejected reversal
(sell-only/no-re-entry, exactly like the other three optional filters).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Union

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import _as_direction, _prepare_bars, compute_component_scores, score_for_direction
from app.trading.macd2.models import Direction, MajorFlagDecision

_SEQ_BONUS = {1: config.SINGLE_ENTRY_SEQ_BONUS_1, 2: config.SINGLE_ENTRY_SEQ_BONUS_2, 3: config.SINGLE_ENTRY_SEQ_BONUS_3}


def _decision(
    *, approved: bool, decision: str, block_reason: Optional[str], reasons: list[str],
    score: float = 0.0, required_score: float = 0.0,
    component_scores: Optional[dict[str, float]] = None, metrics: Optional[dict] = None,
) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=approved, score=float(score), required_score=float(required_score), decision=decision,
        reasons=tuple(reasons), component_scores=dict(component_scores or {}), metrics=dict(metrics or {}),
        is_reversal=False, fast_reversal=False, block_reason=block_reason,
    )


def _price_slope_pct(df_1m: Optional[pd.DataFrame], now: datetime, minutes: int) -> float:
    """Causal % slope of WATCH_SYMBOL close price over the trailing
    ``minutes`` window ending at ``now`` (inclusive) -- only bars with
    datetime <= now are ever consulted, exactly like every other feature
    here; no future bars."""
    if df_1m is None or df_1m.empty or "datetime" not in df_1m.columns:
        return 0.0
    dt_col = pd.to_datetime(df_1m["datetime"])
    now_ts = pd.Timestamp(now)
    if dt_col.dt.tz is not None and now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize(dt_col.dt.tz)
    elif dt_col.dt.tz is not None:
        now_ts = now_ts.tz_convert(dt_col.dt.tz)
    mask = (dt_col <= now_ts) & (dt_col > now_ts - pd.Timedelta(minutes=minutes))
    window = df_1m.loc[mask.to_numpy()]
    if len(window) < 2:
        return 0.0
    first = float(window["close"].iloc[0])
    last = float(window["close"].iloc[-1])
    if first <= 0:
        return 0.0
    return (last / first - 1.0) * 100.0


def evaluate_single_entry(
    bars_3m,
    df_1m,
    flag_direction: Union[Direction, str],
    now: datetime,
    flag_seq: int,
    daily_entry_count: int,
    *,
    score_min: Optional[float] = None,
    max_daily_entries: Optional[int] = None,
) -> MajorFlagDecision:
    """Score + gate an ALREADY-confirmed crossover. Pure: same inputs ->
    same output. Never called when ``state.single_entry_filter_enabled``
    is False."""
    direction = _as_direction(flag_direction)
    if direction is None:
        return _decision(
            approved=False, decision=config.FILTER_INPUT_NOT_CROSSOVER,
            block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=["flag_direction must be UP_RED or DOWN_BLUE"],
        )

    limit = int(max_daily_entries if max_daily_entries is not None else config.SINGLE_ENTRY_MAX_DAILY_ENTRIES)
    count = int(daily_entry_count or 0)
    if count >= limit:
        return _decision(
            approved=False, decision=config.SINGLE_ENTRY_DAILY_LIMIT_REACHED,
            block_reason=config.SINGLE_ENTRY_DAILY_LIMIT_REACHED,
            reasons=[f"daily_entry_count {count} >= {limit}"],
        )

    work = _prepare_bars(bars_3m)
    if work is None:
        return _decision(
            approved=False, decision=config.FILTER_DATA_INSUFFICIENT,
            block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["insufficient or invalid completed 3m bars"],
        )

    scores_t, metrics_t, err = compute_component_scores(work)
    if err or scores_t is None or metrics_t is None:
        return _decision(
            approved=False, decision=config.FILTER_DATA_INSUFFICIENT,
            block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=[err or config.FILTER_DATA_INSUFFICIENT],
        )

    scores, metrics = score_for_direction(scores_t, metrics_t, direction)
    major_score = float(sum(scores.values()))

    seq = int(flag_seq)
    seq_bonus = float(_SEQ_BONUS.get(seq, 0.0))

    sign = 1.0 if direction == Direction.UP_RED else -1.0
    gap_expansion = float(metrics["hist"]) - float(metrics["prev_hist"])
    gap_bonus = config.SINGLE_ENTRY_GAP_EXPANSION_BONUS if sign * gap_expansion > 0 else 0.0

    ema10_cur, ema10_prev = float(metrics["ema10"]), float(metrics["ema10_prev"])
    ema10_slope_pct = (ema10_cur / ema10_prev - 1.0) * 100.0 if ema10_prev else 0.0
    ema10_bonus = config.SINGLE_ENTRY_EMA10_SLOPE_BONUS if sign * ema10_slope_pct > 0 else 0.0

    price_slope_15m = _price_slope_pct(df_1m, now, 15)
    slope_bonus = config.SINGLE_ENTRY_PRICE_SLOPE_15M_BONUS if sign * price_slope_15m > 0 else 0.0

    price_impulse_atr = float(metrics.get("price_impulse_atr") or 0.0)
    overheat = abs(price_impulse_atr) >= config.SINGLE_ENTRY_OVERHEAT_THRESHOLD
    overheat_penalty = config.SINGLE_ENTRY_OVERHEAT_PENALTY if overheat else 0.0

    total_score = major_score + seq_bonus + gap_bonus + ema10_bonus + slope_bonus - overheat_penalty

    macd_val = float(metrics.get("macd") or 0.0)
    near_zero_blue = bool(direction == Direction.DOWN_BLUE and abs(macd_val) < config.SINGLE_ENTRY_NEAR_ZERO_MACD_THRESHOLD)

    required = float(score_min if score_min is not None else config.SINGLE_ENTRY_SCORE_MIN)
    approved = total_score >= required
    decision_str = config.SINGLE_ENTRY_APPROVED if approved else config.SINGLE_ENTRY_SCORE_BELOW_THRESHOLD

    component_scores = {
        **scores, "seq_bonus": seq_bonus, "gap_expansion_bonus": gap_bonus,
        "ema10_slope_bonus": ema10_bonus, "price_slope_15m_bonus": slope_bonus,
        "overheat_penalty": -overheat_penalty,
    }
    out_metrics = {
        **metrics, "major_score": major_score, "total_score": total_score, "flag_seq": seq,
        "gap_expansion": gap_expansion, "ema10_slope_pct": ema10_slope_pct,
        "price_slope_15m_pct": price_slope_15m, "overheat": overheat, "near_zero_blue": near_zero_blue,
    }
    return _decision(
        approved=approved, decision=decision_str, block_reason=None if approved else decision_str,
        reasons=[f"score {total_score:.1f} {'>=' if approved else '<'} {required:.1f} (seq={seq})"],
        score=total_score, required_score=required, component_scores=component_scores, metrics=out_metrics,
    )
