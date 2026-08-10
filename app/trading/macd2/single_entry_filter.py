"""Optional Daily Single-Entry filter — pure functions only, order
authority gate only (never creates/suppresses a confirmed MACD crossover;
worker.py's signal_engine crossover detection is completely untouched).

Unlike major_flag_filter/sideways_filter/trend_persistence_filter, this
gate needs no bars/score at all: it approves a confirmed crossover purely
by ITS OWN SEQUENCE NUMBER within the trading day — the 1st, 2nd, ... up to
``config.SINGLE_ENTRY_MAX_DAILY_ENTRIES``-th confirmed flag of the day is
approved, every later one is rejected (daily_entry_count already tracks
"how many fills today", the same counter the other three optional filters
use for their own daily caps).

2026-08-10 redesign (see config.py for the full 15-trading-day sequence
analysis this replaced the old 11:00-cutoff/one-shot design with): OFF by
default (config.SINGLE_ENTRY_FILTER_DEFAULT). Mutually exclusive with
sideways_filter_enabled/major_filter_enabled/trend_persistence_filter_enabled
(see worker._judge_entry_gate priority chain) — never touches STOP_LOSS/
PROFIT_LOCK/order-fill/ledger logic, and gates a NEW BUY only — the caller
still liquidates a held position on a rejected reversal (sell-only/no-re-
entry, exactly like the other three optional filters).
"""
from __future__ import annotations

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
    daily_entry_count: int,
    *,
    max_daily_entries: Optional[int] = None,
) -> MajorFlagDecision:
    """Approve a confirmed crossover if today's fill count is still below
    ``max_daily_entries`` (defaults to config.SINGLE_ENTRY_MAX_DAILY_ENTRIES);
    reject once the cap is reached. Pure: same inputs -> same output. Never
    called when ``state.single_entry_filter_enabled`` is False.
    """
    if _as_direction(flag_direction) is None:
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
    return _decision(
        approved=True, decision=config.SINGLE_ENTRY_APPROVED, block_reason=None,
        reasons=[f"entry #{count + 1} of {limit} today"],
    )
