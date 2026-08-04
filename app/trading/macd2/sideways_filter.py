"""Optional 추세전환장(sideways/whipsaw) entry filter — pure functions only.

2026-08-04: user-specified criterion derived from a single day's
(2026-08-03) 14-flag Quick-Profit backtest run in conversation — on a
choppy/trend-reversal day, MAJOR_FLAG's own strength score alone did not
cleanly separate winning flags from losing ones (two of that day's losses
scored just as "strong" as the real winners). Requiring the confirmation
candle's body size AND volume to also be elevated, on top of the score,
did separate them well while keeping to ~3-4 entries/day.

Deliberately reuses major_flag_filter.compute_component_scores/
score_for_direction/_as_direction/_prepare_bars (docs §17: no duplicated
MACD/EMA/ATR/volume computation) — this module only adds a NEW, simpler
threshold combination on top of the SAME metrics MAJOR_FLAG already
computes. Never creates or suppresses a confirmed flag itself (worker.py's
signal_engine crossover detection is untouched); order-gate only, exactly
like major_flag_filter, and evaluated on completed 3m bars up to the flag
bar only (no future bars, no forming bar, no live quote injection).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Union

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import (
    _as_direction,
    _prepare_bars,
    compute_component_scores,
    score_for_direction,
)
from app.trading.macd2.models import Direction, MajorFlagDecision


def _reject(*, decision: str, block_reason: str, reasons: list[str],
            score: float = 0.0, required_score: float = 0.0,
            component_scores: Optional[dict] = None, metrics: Optional[dict] = None) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=False, score=score, required_score=required_score, decision=decision,
        reasons=tuple(reasons), component_scores=dict(component_scores or {}), metrics=dict(metrics or {}),
        is_reversal=False, fast_reversal=False, block_reason=block_reason,
    )


def evaluate_sideways_flag(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    now: datetime,
) -> MajorFlagDecision:
    """Score + gate an ALREADY-confirmed crossover for the 추세전환장 mode.

    Approval requires ALL three:
      - MAJOR_FLAG's own component score >= SIDEWAYS_ENTRY_SCORE_MIN
      - confirmation candle body >= SIDEWAYS_BODY_ATR_MIN * ATR14
      - confirmation candle volume >= SIDEWAYS_VOLUME_RATIO_MIN * 20-bar median volume

    Pure: same inputs -> same output. Never called when
    ``state.sideways_filter_enabled`` is False.
    """
    del now  # not used by this simpler gate (no reversal-cooldown/time-of-day logic)
    required_score = float(config.SIDEWAYS_ENTRY_SCORE_MIN)

    direction = _as_direction(flag_direction)
    if direction is None:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER, block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=["flag_direction must be UP_RED or DOWN_BLUE"], required_score=required_score,
        )

    work = _prepare_bars(bars_3m)
    if work is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["insufficient or invalid completed 3m bars"], required_score=required_score,
        )

    scores_t, metrics_t, err = compute_component_scores(work)
    if err or scores_t is None or metrics_t is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=[err or config.FILTER_DATA_INSUFFICIENT], required_score=required_score,
        )

    scores, metrics = score_for_direction(scores_t, metrics_t, direction)
    total = float(sum(scores.values()))
    body_atr = float(metrics.get("body_atr") or 0.0)
    volume_ratio = float(metrics.get("volume_ratio") or 0.0)

    if total < required_score:
        return _reject(
            decision=config.SIDEWAYS_SCORE_BELOW_THRESHOLD, block_reason=config.SIDEWAYS_SCORE_BELOW_THRESHOLD,
            reasons=[f"score {total:.0f} < required {required_score:.0f}"],
            score=total, required_score=required_score, component_scores=scores, metrics=metrics,
        )
    if body_atr < float(config.SIDEWAYS_BODY_ATR_MIN):
        return _reject(
            decision=config.SIDEWAYS_BODY_BELOW_THRESHOLD, block_reason=config.SIDEWAYS_BODY_BELOW_THRESHOLD,
            reasons=[f"body_atr {body_atr:.3f} < required {config.SIDEWAYS_BODY_ATR_MIN}"],
            score=total, required_score=required_score, component_scores=scores, metrics=metrics,
        )
    if volume_ratio < float(config.SIDEWAYS_VOLUME_RATIO_MIN):
        return _reject(
            decision=config.SIDEWAYS_VOLUME_BELOW_THRESHOLD, block_reason=config.SIDEWAYS_VOLUME_BELOW_THRESHOLD,
            reasons=[f"volume_ratio {volume_ratio:.3f} < required {config.SIDEWAYS_VOLUME_RATIO_MIN}"],
            score=total, required_score=required_score, component_scores=scores, metrics=metrics,
        )

    return MajorFlagDecision(
        approved=True, score=total, required_score=required_score, decision=config.SIDEWAYS_APPROVED,
        reasons=("score/body/volume all above sideways-mode thresholds",),
        component_scores=scores, metrics=metrics, is_reversal=False, fast_reversal=False, block_reason=None,
    )
