"""Optional Trend Persistence entry filter — pure functions only, order
authority gate only (never creates/suppresses a confirmed MACD crossover;
worker.py's signal_engine crossover detection is completely untouched).

Reuses the existing compute_trend_persistence_score(features, direction)
formula (app/trading/hynix_big_trend_engine.py) — a 0-100 blend of
VWAP-dwell-time + EMA5/10/20 stack ordering + HH/HL (or LH/LL) structure,
already used elsewhere in this repo with the same 55/60/65 threshold family
— instead of inventing a new score. The features it needs are computed
locally here (running VWAP dwell minutes off macd2's own df_1m, and last-3
completed-3m-bar HH/HL/LH/LL off macd2's own bars_3m) rather than importing
hynix_big_trend_engine.build_big_trend_features, which pulls in a different
engine's AI-probability snapshot this module has no business depending on.

2026-08-07: new gate, OFF by default (config.TREND_PERSISTENCE_FILTER_
DEFAULT). Mutually exclusive with sideways_filter_enabled/major_filter_
enabled (see worker._judge_entry_gate priority chain) — never touches
STOP_LOSS/PROFIT_LOCK/order-fill/ledger logic, and gates a NEW BUY only —
the caller still liquidates a held position on a rejected reversal
(sell-only/no-re-entry, exactly like the other two optional filters).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Union

import pandas as pd

from app.trading.hynix_big_trend_engine import (
    DIRECTION_HYNIX,
    DIRECTION_INVERSE,
    compute_trend_persistence_score,
)
from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import _as_direction, _prepare_bars
from app.trading.macd2.models import Direction, MajorFlagDecision

_MIN_1M_BARS_FOR_EMA20 = 20


def _reject(*, decision: str, block_reason: str, reasons: list[str],
            score: float = 0.0, required_score: float = 0.0,
            component_scores: Optional[dict] = None, metrics: Optional[dict] = None) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=False, score=score, required_score=required_score, decision=decision,
        reasons=tuple(reasons), component_scores=dict(component_scores or {}), metrics=dict(metrics or {}),
        is_reversal=False, fast_reversal=False, block_reason=block_reason,
    )


def _prepare_1m_bars(df_1m: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df_1m is None or df_1m.empty:
        return None
    missing = [c for c in ("datetime", "high", "low", "close", "volume") if c not in df_1m.columns]
    if missing:
        return None
    work = df_1m.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    for col in ("high", "low", "close", "volume"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["datetime", "high", "low", "close"]).sort_values("datetime")
    work = work.drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)
    if len(work) < _MIN_1M_BARS_FOR_EMA20:
        return None
    return work


def _vwap_dwell_minutes(df_1m: pd.DataFrame) -> dict:
    """Same running-VWAP dwell-time calculation as
    hynix_big_trend_engine._consecutive_side_of_vwap, computed locally
    against macd2's own WATCH_SYMBOL 1-minute bars instead of importing
    that (private) helper from a sibling engine module."""
    typical = (df_1m["high"] + df_1m["low"] + df_1m["close"]) / 3.0
    cum_vol = df_1m["volume"].cumsum()
    cum_pv = (typical * df_1m["volume"]).cumsum()
    running_vwap = cum_pv / cum_vol.replace(0, float("nan"))
    above = (df_1m["close"] > running_vwap).tolist()

    above_count = 0
    for v in reversed(above):
        if v is True:
            above_count += 1
        else:
            break
    below_count = 0
    for v in reversed(above):
        if v is False:
            below_count += 1
        else:
            break
    return {"minutes_above_vwap": above_count, "minutes_below_vwap": below_count}


def _structure_counts_last3(bars_3m: pd.DataFrame) -> dict:
    """Same HH/HL/LH/LL-over-last-3-completed-bars calculation as
    hynix_big_trend_engine._hh_hl_lh_ll_counts, computed against macd2's
    own completed 3-minute bars (signal_engine.resample_completed_3m) — no
    need to re-resample from 1-minute bars the way that helper does."""
    empty = {
        "higher_high_count_last3": None, "higher_low_count_last3": None,
        "lower_high_count_last3": None, "lower_low_count_last3": None,
    }
    if len(bars_3m) < 4:
        return empty
    work = bars_3m.tail(4)
    highs = work["high"].tolist()
    lows = work["low"].tolist()
    hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i - 1])
    hl = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
    ll = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i - 1])
    return {
        "higher_high_count_last3": hh, "higher_low_count_last3": hl,
        "lower_high_count_last3": lh, "lower_low_count_last3": ll,
    }


def _ema_stack(df_1m: pd.DataFrame) -> dict:
    closes = df_1m["close"]
    return {
        "ema5": round(float(closes.ewm(span=5, adjust=False).mean().iloc[-1]), 4),
        "ema10": round(float(closes.ewm(span=10, adjust=False).mean().iloc[-1]), 4),
        "ema20": round(float(closes.ewm(span=20, adjust=False).mean().iloc[-1]), 4),
    }


def _direction_to_engine(direction: Direction) -> str:
    return DIRECTION_HYNIX if direction == Direction.UP_RED else DIRECTION_INVERSE


def evaluate_trend_persistence(
    bars_3m: Optional[pd.DataFrame],
    df_1m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    now: datetime,
    *,
    score_min: Optional[float] = None,
) -> MajorFlagDecision:
    """Score an ALREADY-confirmed crossover with compute_trend_persistence_
    score and approve only if it clears ``score_min`` (defaults to
    config.TREND_PERSISTENCE_SCORE_MIN). Pure: same inputs -> same output.
    Never called when ``state.trend_persistence_filter_enabled`` is False.
    """
    del now  # no time-of-day branching in this gate (unlike sideways v5)
    required_score = float(score_min if score_min is not None else config.TREND_PERSISTENCE_SCORE_MIN)

    direction = _as_direction(flag_direction)
    if direction is None:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER, block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=["flag_direction must be UP_RED or DOWN_BLUE"], required_score=required_score,
        )

    bars_3m_ready = _prepare_bars(bars_3m)
    df_1m_ready = _prepare_1m_bars(df_1m)
    if bars_3m_ready is None or df_1m_ready is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["insufficient or invalid completed bars"], required_score=required_score,
        )

    features: dict = {}
    features.update(_ema_stack(df_1m_ready))
    features.update(_vwap_dwell_minutes(df_1m_ready))
    features.update(_structure_counts_last3(bars_3m_ready))

    score = float(compute_trend_persistence_score(features, _direction_to_engine(direction)))

    if score >= required_score:
        return MajorFlagDecision(
            approved=True, score=score, required_score=required_score,
            decision=config.TREND_PERSISTENCE_APPROVED,
            reasons=(f"trend_persistence_score {score:.1f} >= {required_score:.1f}",),
            component_scores={"trend_persistence_score": score}, metrics=features,
            is_reversal=False, fast_reversal=False, block_reason=None,
        )
    return _reject(
        decision=config.TREND_PERSISTENCE_BELOW_THRESHOLD, block_reason=config.TREND_PERSISTENCE_BELOW_THRESHOLD,
        reasons=[f"trend_persistence_score {score:.1f} < {required_score:.1f}"],
        score=score, required_score=required_score,
        component_scores={"trend_persistence_score": score}, metrics=features,
    )
