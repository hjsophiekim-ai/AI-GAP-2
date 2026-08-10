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

2026-08-07 v3 (time-aware): re-validated on an expanded 10-day 추세전환장
set (added 06/24, 08/04, 08/05) by bucketing every confirmed flag's outcome
into 09:00-11:00 / 11:00-14:00 / 14:00-15:30 KST. The score<max-and-not-
breakout gate below still wins net P&L INSIDE 11:00-14:00 (mean score of
winners 44.2 vs losers 58.8 there — the inverted relationship above is
tightest in this window), but a full tick-by-tick replay of all 10 days
showed dropping the gate entirely OUTSIDE that window (every confirmed
flag enters in 09:00-11:00 and 14:00-15:30) beats both the gate applied
all day (avg net/day +291,071) and a "require a HIGH score outside
11:00-14:00" variant (+85,348 — tested and rejected: the low-score-wins
relationship is not actually 11:00-14:00-specific, so requiring a high
score outside it just selects worse trades). The no-gate-outside-window
form nets +317,978/day at ~4 trades/day vs ~2/day for the other two.
SIDEWAYS_TIME_GATE_START/_END (11:00/14:00) bound the still-gated window;
outside it every already-confirmed crossover is approved unconditionally
(breakout included) instead of being scored against the threshold.

2026-08-07 v5 (사용자 요청 — 시간대별 로직 재설계): the v3/v4 "unconditional
outside 11:00-14:00" design above was replayed tick-by-tick through the REAL
worker.run_once() over the most recent real trading week (08/03-08/07 Mon-Fri,
Fri partial to ~14:58) alongside 3 alternatives: (A) no filter at all,
(C) 09:00-11:00 PRIMARY_TREND-pullback-only + the SAME score<45-and-not-
breakout gate extended from 11:00 through end of day (no unconditional
window left at all), (D) same as C but even stricter after 14:00
(score<30). Results: A=+2.95% cum (36% win rate, 89 trades), v3/v4-as-B=
+12.59% (57%, 29 trades), C=+13.87% (67%, 24 trades, zero days left an open
position at the data cutoff), D=+12.61% (64%, 22 trades) — D's extra
afternoon strictness bought nothing over C. (C) wins and is now shipped:
PRIMARY_TREND pullback moved from "checked every tick all day" (v3/v4) to
"checked ONLY inside 09:00-11:00" (evaluate_sideways_flag now owns that
call directly instead of worker.py calling it separately beforehand), and
the 11:00-14:00 score+breakout gate now simply has no upper time bound —
14:00-15:30 gets the identical treatment 11:00-14:00 already had, so
SIDEWAYS_TIME_GATE_END no longer exists. Sample caveat: only 5 real trading
days (~48 confirmed flags total) backed this comparison; re-validate after
a few more weeks of live data.

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

from app.trading import hynix_primary_trend
from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import (
    _as_direction,
    _prepare_bars,
    compute_component_scores,
    score_for_direction,
)
from app.trading.macd2.models import Direction, MajorFlagDecision


def evaluate_primary_trend_pullback(
    df_1m: Optional[pd.DataFrame], flag_direction: Union[Direction, str], now: datetime,
) -> Optional[MajorFlagDecision]:
    """2026-08-07 (사용자 요청): reject a confirmed flag that runs AGAINST
    today's dominant trend as a brief PULLBACK, not a real reversal — the
    held position still gets liquidated by the caller (worker.py reuses the
    existing sell-only/no-re-entry path, same as a MAJOR/추세전환장-filtered
    reversal), it just doesn't flip into the counter-trend ETF. Returns
    ``None`` (never rejects) when the flag direction AGREES with today's
    trend, or when PRIMARY_TREND itself is still RANGE (not enough votes yet
    to call a real trend — the existing score-gated logic in
    evaluate_sideways_flag decides those cases, unchanged).

    PRIMARY_TREND is recomputed fresh from the day's 1-minute bars on every
    call (never cached/frozen from earlier in the session) — reusing
    ``hynix_primary_trend.compute_primary_trend``'s own vote-based
    classification means a genuine mid-day trend change (VWAP/EMA slope/
    swing structure all shifting together) naturally flips PRIMARY_TREND on
    its own, and a flag matching the NEW direction is then ALIGNED (not a
    pullback) and passes straight through — no separate reversal-
    confirmation delay for now (see module docstring for the stricter
    2-consecutive-check variant this project's sibling hynix_switch_
    position_manager uses; not ported here to start simple)."""
    if df_1m is None or df_1m.empty or "datetime" not in df_1m.columns:
        return None
    now_kst = now.astimezone(config.KST)
    dt_col = pd.to_datetime(df_1m["datetime"])
    dt_col = dt_col.dt.tz_convert(config.KST) if dt_col.dt.tz is not None else dt_col.dt.tz_localize(config.KST)
    # 2026-08-10 fix: bound by <= now_kst, not just by calendar date -- a
    # caller holding history PAST `now` (a replay/backtest driving many
    # ticks off one static df_1m, or a live restart's catch-up replay) must
    # never leak later-today bars into "today's dominant trend" for an
    # earlier tick. This module's own docstring already promises "no future
    # bars" — the date-only filter silently broke that promise whenever
    # df_1m outran `now`.
    today_mask = (dt_col.dt.date == now_kst.date()) & (dt_col <= now_kst)
    today_df = df_1m[today_mask.to_numpy()]
    prior_df = df_1m[(dt_col.dt.date != now_kst.date()).to_numpy()]
    if today_df.empty:
        return None
    prev_close = float(prior_df["close"].iloc[-1]) if not prior_df.empty else None

    trend_result = hynix_primary_trend.compute_primary_trend(today_df, prev_close=prev_close, now=now_kst)
    primary_trend = trend_result.get("primary_trend")
    if primary_trend == hynix_primary_trend.PRIMARY_TREND_RANGE:
        return None

    direction = _as_direction(flag_direction)
    short_term_direction = "UP" if direction == Direction.UP_RED else "DOWN"
    move = hynix_primary_trend.classify_short_term_move(primary_trend, short_term_direction)
    if move != hynix_primary_trend.MOVE_PULLBACK:
        return None

    return _reject(
        decision=config.SIDEWAYS_PRIMARY_TREND_PULLBACK_BLOCKED,
        block_reason=config.SIDEWAYS_PRIMARY_TREND_PULLBACK_BLOCKED,
        reasons=[f"primary_trend={primary_trend} vs flag={short_term_direction} — pullback, sell-only/no-re-entry"],
        metrics=trend_result,
    )


def _is_morning_window(now: datetime) -> bool:
    """True strictly before SIDEWAYS_TIME_GATE_START (09:00-11:00 KST) --
    the PRIMARY_TREND-pullback-only window. At/after it (11:00 through end
    of day), the score+breakout gate is the sole authority — see
    evaluate_sideways_flag's docstring (2026-08-07 v5)."""
    return now.astimezone(config.KST).time() < config.SIDEWAYS_TIME_GATE_START


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
    df_1m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    now: datetime,
) -> MajorFlagDecision:
    """Gate an ALREADY-confirmed crossover for the 추세전환장 mode — two
    time windows, no more unconditional-approval branch (2026-08-07 v5; see
    module docstring for the 4-candidate week-long replay that picked this
    combination over the v3/v4 "unconditional outside 11:00-14:00" design):

    - 09:00-11:00 (before SIDEWAYS_TIME_GATE_START): PRIMARY_TREND-pullback
      check ONLY (evaluate_primary_trend_pullback) — a flag running against
      today's dominant trend is rejected as a pullback (sell-only/no-re-
      entry for a held position); a flag AGREEING with the trend, or any
      flag while PRIMARY_TREND is still RANGE, is approved regardless of
      score/breakout (SIDEWAYS_MORNING_TREND_APPROVED).
    - 11:00 onward (through end of day — NEW_ENTRY_CUTOFF already caps real
      entries at 14:55): the score+breakout gate, UNCHANGED from v2/v3/v4 —
      approval requires BOTH MAJOR_FLAG's own component score <
      SIDEWAYS_ENTRY_SCORE_MAX (a LOW score, not a high one — see module
      docstring for why this is inverted from a naive "strong flag" filter)
      AND no 4-bar breakout.

    Score/breakout are still computed and returned for observability
    (state.last_sideways_score etc.) even in the morning branch, which does
    not gate on them.

    Pure: same inputs -> same output. Never called when
    ``state.sideways_filter_enabled`` is False.
    """
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

    if _is_morning_window(now):
        pullback = evaluate_primary_trend_pullback(df_1m, direction, now)
        if pullback is not None:
            return pullback
        return MajorFlagDecision(
            approved=True, score=total, required_score=0.0,
            decision=config.SIDEWAYS_MORNING_TREND_APPROVED,
            reasons=("09:00-11:00 trend-aligned (or PRIMARY_TREND still RANGE) — no score gate here",),
            component_scores=scores, metrics=metrics, is_reversal=False, fast_reversal=False, block_reason=None,
        )

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
