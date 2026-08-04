"""Optional 추세전환장(sideways/whipsaw) entry filter — pure functions only.

2026-08-04 v2 (tight): re-derived from the last 20 real trading days
(2026-07 ~ 2026-08-03), restricted to the 7 days classified as genuine
"추세전환장" (>=5 confirmed flags/day — a natural gap separated these from
the other 13 "확실한 추세" days at <=3 flags/day). Pooling all 55 real
trades from just those 7 days (every confirmed flag entering, plus the
Quick-Profit +1.5% take-profit exit) showed the INVERSE of the original
(v1) relationship: on these choppy days, a LOW major_flag_filter score
predicted the winners, not a high one — e.g. score 30-45 netted +1.08M
across 11 trades while score 60-90 netted -850K across 26 trades.
Requiring breakout==False on top removed one more clean outlier loss for
free (cost zero winners). The old v1 body/volume-floor conditions did NOT
hold up on this larger sample (winner/loser ranges overlapped too much)
and are dropped entirely.

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

    Approval requires BOTH:
      - MAJOR_FLAG's own component score < SIDEWAYS_ENTRY_SCORE_MAX (a LOW
        score, not a high one — see module docstring for why this is
        inverted from a naive "strong flag" filter)
      - confirmation candle did NOT 4-bar breakout (breakout == False)

    Pure: same inputs -> same output. Never called when
    ``state.sideways_filter_enabled`` is False.
    """
    del now  # not used by this simpler gate (no reversal-cooldown/time-of-day logic)
    required_score = float(config.SIDEWAYS_ENTRY_SCORE_MAX)

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
    breakout = bool(metrics.get("breakout"))

    if total >= required_score:
        return _reject(
            decision=config.SIDEWAYS_SCORE_ABOVE_THRESHOLD, block_reason=config.SIDEWAYS_SCORE_ABOVE_THRESHOLD,
            reasons=[f"score {total:.0f} >= max {required_score:.0f} (약한 플래그만 진입)"],
            score=total, required_score=required_score, component_scores=scores, metrics=metrics,
        )
    if breakout:
        return _reject(
            decision=config.SIDEWAYS_BREAKOUT_BLOCKED, block_reason=config.SIDEWAYS_BREAKOUT_BLOCKED,
            reasons=["4-bar breakout confirmed (돌파 플래그는 이 모드에서 제외)"],
            score=total, required_score=required_score, component_scores=scores, metrics=metrics,
        )

    return MajorFlagDecision(
        approved=True, score=total, required_score=required_score, decision=config.SIDEWAYS_APPROVED,
        reasons=("score below max threshold and no breakout — 추세전환장 모드 승인",),
        component_scores=scores, metrics=metrics, is_reversal=False, fast_reversal=False, block_reason=None,
    )
