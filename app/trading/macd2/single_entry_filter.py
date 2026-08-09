"""Optional Daily Single-Entry filter — pure functions only, order
authority gate only (never creates/suppresses a confirmed MACD crossover;
worker.py's signal_engine crossover detection is completely untouched).

Unlike major_flag_filter/sideways_filter/trend_persistence_filter, this
gate needs no bars/score at all: it blocks every confirmed crossover before
config.SINGLE_ENTRY_CUTOFF_TIME (11:00), then approves exactly the first
one after that cutoff each day and rejects every one after (daily_entry_
count already tracks "how many fills today", the same counter the other
three optional filters use for their own daily caps).

2026-08-08: new gate, OFF by default (config.SINGLE_ENTRY_FILTER_DEFAULT).
Mutually exclusive with sideways_filter_enabled/major_filter_enabled/
trend_persistence_filter_enabled (see worker._judge_entry_gate priority
chain) — never touches STOP_LOSS/PROFIT_LOCK/order-fill/ledger logic, and
gates a NEW BUY only — the caller still liquidates a held position on a
rejected reversal (sell-only/no-re-entry, exactly like the other three
optional filters).
"""
from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Optional, Union

from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import _as_direction
from app.trading.macd2.models import MajorFlagDecision


def _decision(*, approved: bool, decision: str, block_reason: Optional[str], reasons: list[str]) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=approved, score=0.0, required_score=0.0, decision=decision,
        reasons=tuple(reasons), component_scores={}, metrics={},
        is_reversal=False, fast_reversal=False, block_reason=block_reason,
    )


def evaluate_single_entry(
    flag_direction: Union["Direction", str],  # noqa: F821 - typing only, avoids a hard Direction import cycle
    now: datetime,
    daily_entry_count: int,
    *,
    cutoff_time: Optional[dt_time] = None,
) -> MajorFlagDecision:
    """Approve exactly the first confirmed crossover of the day at/after
    ``cutoff_time`` (defaults to config.SINGLE_ENTRY_CUTOFF_TIME); reject
    everything before that cutoff and every later one the same day. Pure:
    same inputs -> same output. Never called when ``state.single_entry_
    filter_enabled`` is False.
    """
    if _as_direction(flag_direction) is None:
        return _decision(
            approved=False, decision=config.FILTER_INPUT_NOT_CROSSOVER,
            block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=["flag_direction must be UP_RED or DOWN_BLUE"],
        )

    effective_cutoff = cutoff_time if cutoff_time is not None else config.SINGLE_ENTRY_CUTOFF_TIME
    if now.time() < effective_cutoff:
        return _decision(
            approved=False, decision=config.SINGLE_ENTRY_BEFORE_CUTOFF,
            block_reason=config.SINGLE_ENTRY_BEFORE_CUTOFF,
            reasons=[f"now {now.time()} < cutoff {effective_cutoff}"],
        )
    if int(daily_entry_count or 0) >= 1:
        return _decision(
            approved=False, decision=config.SINGLE_ENTRY_ALREADY_USED_TODAY,
            block_reason=config.SINGLE_ENTRY_ALREADY_USED_TODAY,
            reasons=[f"daily_entry_count {daily_entry_count} >= 1"],
        )
    return _decision(
        approved=True, decision=config.SINGLE_ENTRY_APPROVED, block_reason=None,
        reasons=[f"first confirmed crossover at/after {effective_cutoff}"],
    )
