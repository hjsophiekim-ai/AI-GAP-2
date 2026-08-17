"""Optional "시간대별 최적거래 필터" (Time-Window Optimal Trading Filter) —
pure functions only, entry-gate half (see time_window_position_manager.py
for the paired exit-ladder half).

2026-08-15 사용자 요청. Scope and invariants (docs/MACD2_LOGIC.md's own rules
for every other optional filter apply here unchanged):

- Never creates or suppresses a confirmed MACD crossover itself —
  signal_engine.evaluate_macd_crossover / _advance_confirmed_primary in
  worker.py remain the single source of "was there a flag". This module only
  decides whether an ALREADY-confirmed flag, once it has cleared its own
  extra 3-minute re-confirmation (see below), gets order authority.
- Reuses major_flag_filter's EMA10/EMA20/ATR/volume/_prepare_bars machinery
  and signal_engine.calculate_macd_series — no duplicated indicator math.
- Every input is a completed-bar DataFrame truncated at or before the
  decision bar; no forming bar, no future bar, no live-quote injection
  anywhere in this module (look-ahead bias is structurally impossible as
  long as callers only ever pass bars up to "now").

Two-bar confirmation model (spec §1, distinct from — and layered on top of
— the plain completed-bar confirmation every other MACD2 filter uses):
a flag confirmed on completed bar T does NOT get order authority immediately.
The caller (worker.py's ``_advance_time_window_candidate`` / the backtest
driver) must wait for the NEXT completed bar (T+3) and re-check that the
MACD/Signal relationship is still in the flag's favor AND that the
MACD-Signal gap has widened versus its value at T. ``evaluate_time_window_entry``
below is the single pure decision function both call once that next bar is in
hand — it takes bars_3m truncated through T+3 and the ORIGINAL flag bar's own
timestamp, and never accepts data past the T+3 confirmation bar.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Any, Optional, Union

import pandas as pd

from app.trading.macd2 import config, signal_engine
from app.trading.macd2.major_flag_filter import (
    _as_direction,
    _direction_sign,
    _prepare_bars,
    compute_component_scores,
)
from app.trading.macd2.models import Direction, MajorFlagDecision

# ── window classification ──────────────────────────────────────────────────
WINDOW_MORNING_1 = "W1_MORNING_AGGRESSIVE"
WINDOW_MORNING_2 = "W2_MORNING_SECOND"
WINDOW_MORNING_3 = "W3_MORNING_THIRD_STRICT"
WINDOW_NO_NEW_ENTRY = "W4_NO_NEW_ENTRY"
WINDOW_AFTERNOON_1 = "W5_EARLY_AFTERNOON_A_GRADE"
WINDOW_AFTERNOON_2 = "W6_LATE_AFTERNOON_MAIN"

# WINDOW_NO_NEW_ENTRY counts as a "morning" session for entry-cap/position-
# management purposes ONLY IF config.TW_ALLOW_ENTRY_1050_1300 relaxes it
# into a tradeable window at all (§7 default keeps it closed entirely, so
# this classification is otherwise unreachable).
_MORNING_WINDOWS = (WINDOW_MORNING_1, WINDOW_MORNING_2, WINDOW_MORNING_3, WINDOW_NO_NEW_ENTRY)
_AFTERNOON_WINDOWS = (WINDOW_AFTERNOON_1, WINDOW_AFTERNOON_2)


def classify_window(moment: dtime) -> Optional[str]:
    """§4-9 time-window classification. ``None`` outside the trading day's
    entry-eligible span (before 09:00 or at/after 15:00)."""
    if config.TW_WINDOW1_START <= moment < config.TW_WINDOW1_END:
        return WINDOW_MORNING_1
    if config.TW_WINDOW2_START <= moment < config.TW_WINDOW2_END:
        return WINDOW_MORNING_2
    if config.TW_WINDOW3_START <= moment < config.TW_WINDOW3_END:
        return WINDOW_MORNING_3
    if config.TW_NO_NEW_ENTRY_START <= moment < config.TW_NO_NEW_ENTRY_END:
        return WINDOW_NO_NEW_ENTRY
    if config.TW_WINDOW5_START <= moment < config.TW_WINDOW5_END:
        return WINDOW_AFTERNOON_1
    if config.TW_WINDOW6_START <= moment < config.TW_WINDOW6_END:
        return WINDOW_AFTERNOON_2
    return None


def session_for_window(window: Optional[str]) -> Optional[str]:
    if window in _MORNING_WINDOWS:
        return "MORNING"
    if window in _AFTERNOON_WINDOWS:
        return "AFTERNOON"
    return None


def _reject(
    *, decision: str, block_reason: str, reasons: list[str],
    score: float = 0.0, required_score: float = 0.0,
    component_scores: Optional[dict] = None, metrics: Optional[dict] = None,
) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=False, score=score, required_score=required_score, decision=decision,
        reasons=tuple(reasons), component_scores=dict(component_scores or {}),
        metrics=dict(metrics or {}), is_reversal=False, fast_reversal=False,
        block_reason=block_reason,
    )


# ── shared gap-series / flag-history helpers ───────────────────────────────
def _gap_series(work: pd.DataFrame) -> Optional[pd.DataFrame]:
    """MACD-Signal per-bar series (unsigned) for the whole frame — reuses
    signal_engine.calculate_macd_series (same EMA formula as calculate_macd,
    no duplicated computation)."""
    series = signal_engine.calculate_macd_series(work)
    if series is None:
        return None
    series = series.copy()
    series["gap"] = series["macd"] - series["signal"]
    return series


def _confirmed_flag_indices(series: pd.DataFrame) -> list[tuple[int, Direction]]:
    """Replicates signal_engine.evaluate_macd_crossover's onset rule
    (previous_diff<=0 & current_diff>0 -> UP_RED, mirrored for DOWN_BLUE,
    same-direction repeats suppressed) walked across the whole gap series,
    so callers can locate "the previous confirmed flag" without re-deriving
    MACD from scratch bar-by-bar."""
    flags: list[tuple[int, Direction]] = []
    last_direction: Optional[Direction] = None
    gap = series["gap"]
    for i in range(1, len(series)):
        prev_diff = float(gap.iloc[i - 1])
        curr_diff = float(gap.iloc[i])
        if prev_diff <= 0 and curr_diff > 0:
            direction: Optional[Direction] = Direction.UP_RED
        elif prev_diff >= 0 and curr_diff < 0:
            direction = Direction.DOWN_BLUE
        else:
            direction = None
        if direction is not None and direction != last_direction:
            flags.append((i, direction))
            last_direction = direction
    return flags


def _find_previous_opposite_flag(
    series: pd.DataFrame, flag_idx: int, direction: Direction,
) -> Optional[int]:
    opposite = Direction.DOWN_BLUE if direction == Direction.UP_RED else Direction.UP_RED
    candidates = [i for i, d in _confirmed_flag_indices(series) if i < flag_idx and d == opposite]
    return candidates[-1] if candidates else None


# ── §3 is_valid_reset() ─────────────────────────────────────────────────────
def is_valid_reset(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    flag_bar_dt: datetime,
) -> tuple[bool, dict[str, Any]]:
    """§3: whether a new same-side entry may follow a too-recent opposite
    flag. Vacuously True when no prior opposite-direction confirmed flag
    exists yet in the supplied history (e.g. the day's first flag). Uses
    ONLY bars up to and including ``flag_bar_dt`` — never looks past the
    flag bar itself.

    True when ANY of:
      1. the opposite MACD state was held for >= config.TW_RESET_MIN_
         OPPOSITE_BARS completed bars before this flag.
      2. the MACD-Signal gap contracted to <= config.TW_RESET_GAP_
         CONTRACTION_RATIO of its value at the prior opposite flag, then
         re-expanded by this flag.
      3. price pulled back through EMA10 or EMA20 at some point since the
         prior opposite flag, then this flag's close resumed beyond it in
         the flag's own direction.
    """
    direction = _as_direction(flag_direction)
    if direction is None:
        return False, {"error": "invalid_direction"}

    work = _prepare_bars(bars_3m)
    if work is None:
        return True, {"note": "insufficient_history_for_reset_check_defaults_to_allow"}
    work = work[work["datetime"] <= flag_bar_dt].reset_index(drop=True)
    if work.empty:
        return True, {"note": "no_bars_at_or_before_flag"}

    series = _gap_series(work)
    if series is None:
        return True, {"note": "insufficient_history_for_reset_check_defaults_to_allow"}

    flag_rows = series.index[series["datetime"] == flag_bar_dt]
    if len(flag_rows) == 0:
        return True, {"note": "flag_bar_not_in_series"}
    flag_idx = int(flag_rows[-1])

    prev_idx = _find_previous_opposite_flag(series, flag_idx, direction)
    if prev_idx is None:
        return True, {"reason": "no_prior_opposite_flag"}

    sign = _direction_sign(direction)
    gap = series["gap"] * sign  # positive == supports `direction`
    close = work["close"].reset_index(drop=True)
    ema10 = close.ewm(span=config.MAJOR_EMA_FAST, adjust=False).mean()
    ema20 = close.ewm(span=config.MAJOR_EMA_SLOW, adjust=False).mean()

    condition1 = (flag_idx - prev_idx) >= config.TW_RESET_MIN_OPPOSITE_BARS

    gap_prev_flag = float(gap.iloc[prev_idx])
    gap_flag = float(gap.iloc[flag_idx])
    between = gap.iloc[prev_idx + 1: flag_idx]
    min_between = float(between.abs().min()) if not between.empty else abs(gap_flag)
    condition2 = (
        gap_prev_flag > 0
        and min_between <= config.TW_RESET_GAP_CONTRACTION_RATIO * abs(gap_prev_flag)
        and abs(gap_flag) > min_between
    )

    def _healthy(i: int) -> bool:
        if direction == Direction.UP_RED:
            return float(close.iloc[i]) > float(ema10.iloc[i]) or float(close.iloc[i]) > float(ema20.iloc[i])
        return float(close.iloc[i]) < float(ema10.iloc[i]) or float(close.iloc[i]) < float(ema20.iloc[i])

    span = range(prev_idx, flag_idx + 1)
    touched_pullback = any(not _healthy(i) for i in span)
    resumed = _healthy(flag_idx)
    condition3 = touched_pullback and resumed

    valid = bool(condition1 or condition2 or condition3)
    return valid, {
        "prev_opposite_flag_idx": prev_idx,
        "bars_since_prev_opposite_flag": flag_idx - prev_idx,
        "condition1_opposite_state_held": condition1,
        "condition2_gap_contract_then_expand": condition2,
        "condition3_ema_pullback_then_resume": condition3,
        "gap_prev_flag": gap_prev_flag,
        "gap_flag": gap_flag,
        "min_gap_between": min_between,
    }


# ── §4-9 calculate_flag_quality_score() ─────────────────────────────────────
def calculate_flag_quality_score(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    *,
    flag_gap: Optional[float] = None,
    price_ema_ref: str = "ema10",
) -> tuple[int, dict[str, Any]]:
    """0-5 point quality score (§6/§8). Components:

      1. 3-minute confirmation held (structural precondition of being called
         at all post-confirmation — always counted True here).
      2. MACD-Signal gap expanded vs the flag bar's own gap (``flag_gap``).
      3. price vs EMA10 (W3) or EMA20 (W5/observability elsewhere) direction
         agreement.
      4. EMA10 vs EMA20 trend-stack direction agreement.
      5. confirmation-bar volume >= the preceding 5 completed bars' average
         (current bar excluded from its own average, §6).
    """
    direction = _as_direction(flag_direction)
    if direction is None:
        return 0, {"error": config.FILTER_INPUT_NOT_CROSSOVER}

    work = _prepare_bars(bars_3m)
    if work is None:
        return 0, {"error": config.FILTER_DATA_INSUFFICIENT}

    scores_t, metrics_t, err = compute_component_scores(work)
    if err or scores_t is None or metrics_t is None:
        return 0, {"error": err or config.FILTER_DATA_INSUFFICIENT}

    sign = _direction_sign(direction)
    macd_now = float(metrics_t["macd"]) if "macd" in metrics_t else None
    close = float(metrics_t["close"])
    ema10 = float(metrics_t["ema10"])
    ema20 = float(metrics_t["ema20"])

    c1_confirmed = True

    series = _gap_series(work)
    gap_now = None
    if series is not None and len(series) > 0:
        gap_now = float(series["gap"].iloc[-1]) * sign
    c2_gap_expanding = bool(gap_now is not None and flag_gap is not None and gap_now > flag_gap)

    ema_ref_value = ema10 if price_ema_ref == "ema10" else ema20
    c3_price_vs_ema = bool(close > ema_ref_value) if direction == Direction.UP_RED else bool(close < ema_ref_value)

    c4_ema_stack = bool(ema10 > ema20) if direction == Direction.UP_RED else bool(ema10 < ema20)

    lookback = config.TW_QUALITY_VOLUME_LOOKBACK_BARS
    volumes = pd.to_numeric(work["volume"], errors="coerce")
    if len(volumes) > lookback:
        recent_avg = float(volumes.iloc[-(lookback + 1):-1].mean())
        current_volume = float(volumes.iloc[-1])
        c5_volume_ok = bool(current_volume >= recent_avg)
    else:
        recent_avg = None
        current_volume = float(volumes.iloc[-1]) if len(volumes) else 0.0
        c5_volume_ok = False

    components = {
        "confirmed_3min": c1_confirmed,
        "gap_expanding": c2_gap_expanding,
        "price_vs_ema": c3_price_vs_ema,
        "ema_stack_aligned": c4_ema_stack,
        "volume_vs_5bar_avg": c5_volume_ok,
    }
    score = sum(1 for v in components.values() if v)
    detail = dict(components)
    detail.update({
        "gap_now": gap_now, "flag_gap": flag_gap, "close": close,
        "ema10": ema10, "ema20": ema20, "price_ema_ref": price_ema_ref,
        "current_volume": current_volume, "recent_5bar_avg_volume": recent_avg,
        "macd_now": macd_now,
    })
    return int(score), detail


# ── main entry gate ─────────────────────────────────────────────────────────
def evaluate_time_window_entry(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    flag_bar_dt: datetime,
    decision_at: datetime,
    *,
    position_direction: Optional[Direction] = None,
    morning_entry_count: int = 0,
    afternoon_entry_count: int = 0,
) -> MajorFlagDecision:
    """Single order-authority decision for the "시간대별 최적거래 필터"
    (§1-10, §15). ``bars_3m`` must be truncated at/through the T+3
    confirmation bar (its LAST row); ``flag_bar_dt`` identifies bar T within
    it. Pure — same inputs always produce the same output; never mutates
    ``bars_3m``. Both worker.py's live candidate tracker and the backtest
    driver call this exact function (no duplicated entry-condition logic).
    """
    direction = _as_direction(flag_direction)
    if direction is None:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER, block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=["flag_direction must be UP_RED or DOWN_BLUE"],
        )

    work = _prepare_bars(bars_3m)
    if work is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["insufficient or invalid completed 3m bars"],
        )

    series = _gap_series(work)
    if series is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["insufficient bars for MACD series"],
        )

    flag_rows = series.index[series["datetime"] == flag_bar_dt]
    if len(flag_rows) == 0 or int(flag_rows[-1]) != len(series) - 2:
        return _reject(
            decision=config.TW_REJECT_NOT_CONFIRMED, block_reason=config.TW_REJECT_NOT_CONFIRMED,
            reasons=["bars_3m must end exactly one bar after flag_bar_dt (the T+3 confirmation bar)"],
        )
    flag_idx = int(flag_rows[-1])
    confirm_idx = len(series) - 1

    sign = _direction_sign(direction)
    gap = series["gap"] * sign
    gap_flag = float(gap.iloc[flag_idx])
    gap_now = float(gap.iloc[confirm_idx])
    macd_now = float(series["macd"].iloc[confirm_idx])
    macd_prev = float(series["macd"].iloc[confirm_idx - 1])
    macd_rising_ok = bool((macd_now > macd_prev) if direction == Direction.UP_RED else (macd_now < macd_prev))

    base_metrics: dict[str, Any] = {
        "gap_flag": gap_flag, "gap_now": gap_now,
        "macd_now": macd_now, "macd_prev": macd_prev, "macd_rising_ok": macd_rising_ok,
        "flag_bar_at": flag_bar_dt.isoformat(),
        "confirm_bar_at": series["datetime"].iloc[confirm_idx].isoformat(),
    }

    if gap_now <= 0:
        return _reject(
            decision=config.TW_REJECT_NOT_CONFIRMED, block_reason=config.TW_REJECT_NOT_CONFIRMED,
            reasons=["MACD/Signal relationship did not hold 3 minutes after the flag"],
            metrics=base_metrics,
        )
    if not (gap_now > gap_flag):
        return _reject(
            decision=config.TW_REJECT_MACD_GAP_NOT_EXPANDING, block_reason=config.TW_REJECT_MACD_GAP_NOT_EXPANDING,
            reasons=[f"gap_now {gap_now:.4f} did not exceed gap_flag {gap_flag:.4f}"],
            metrics=base_metrics,
        )

    prev_opposite_idx = _find_previous_opposite_flag(series, flag_idx, direction)
    interval_minutes: Optional[float] = None
    if prev_opposite_idx is not None:
        interval_minutes = (
            series["datetime"].iloc[flag_idx] - series["datetime"].iloc[prev_opposite_idx]
        ).total_seconds() / 60.0
    base_metrics["interval_minutes_since_prior_opposite_flag"] = interval_minutes

    if interval_minutes is not None and interval_minutes < config.MIN_FLAG_INTERVAL_MINUTES:
        reset_ok, reset_detail = is_valid_reset(bars_3m, direction, flag_bar_dt)
        base_metrics["reset_detail"] = reset_detail
        if not reset_ok:
            return _reject(
                decision=config.TW_REJECT_SHORT_FLAG_INTERVAL, block_reason=config.TW_REJECT_SHORT_FLAG_INTERVAL,
                reasons=[f"interval {interval_minutes:.1f}min < {config.MIN_FLAG_INTERVAL_MINUTES}min and no valid reset"],
                metrics=base_metrics,
            )

    window = classify_window(decision_at.astimezone(config.KST).time())
    base_metrics["window"] = window
    session = session_for_window(window)
    base_metrics["session"] = session

    no_entry_window_blocked = window == WINDOW_NO_NEW_ENTRY and not config.TW_ALLOW_ENTRY_1050_1300
    afternoon_blocked = window in _AFTERNOON_WINDOWS and config.TW_MORNING_ONLY
    if window is None or no_entry_window_blocked or afternoon_blocked:
        return _reject(
            decision=config.TW_REJECT_TIME_WINDOW, block_reason=config.TW_REJECT_TIME_WINDOW,
            reasons=[f"no new entries in this time window (decision_at={decision_at.isoformat()})"],
            metrics=base_metrics,
        )

    if window == WINDOW_AFTERNOON_2 and decision_at.astimezone(config.KST).time() >= config.TW_AFTERNOON_ENTRY_HARD_CUTOFF:
        return _reject(
            decision=config.TW_REJECT_TIME_WINDOW, block_reason=config.TW_REJECT_TIME_WINDOW,
            reasons=["past 14:57 -- a new flag cannot complete 3-min confirmation before 15:00"],
            metrics=base_metrics,
        )

    quality_score, quality_detail = calculate_flag_quality_score(
        bars_3m, direction, flag_gap=gap_flag,
        price_ema_ref="ema20" if window in (WINDOW_AFTERNOON_1, WINDOW_AFTERNOON_2, WINDOW_NO_NEW_ENTRY) else "ema10",
    )
    base_metrics["quality_score"] = quality_score
    base_metrics["quality_detail"] = quality_detail

    # 2026-08-18 사용자 확정 지시: "게이트 전체 완화" baseline은 모든 창(W1-W6)에
    # quality_score>=QUALITY_SCORE_THRESHOLD를 동일하게 적용해 백테스트됐다 --
    # 이전의 창별 특례(W1 면제/W2 reset-only/W6 EMA-only)는 백테스트에 없던
    # 조건이라 실전과 백테스트가 어긋나는 원인이었다. 백테스트와 실전이 반드시
    # 같은 판단을 하도록 창 구분 없이 단일 규칙으로 통일한다.
    required_score = float(config.QUALITY_SCORE_THRESHOLD)
    if quality_score < required_score:
        return _reject(
            decision=config.TW_REJECT_LOW_QUALITY_SCORE, block_reason=config.TW_REJECT_LOW_QUALITY_SCORE,
            reasons=[f"quality_score {quality_score} < {required_score:.0f}"],
            score=quality_score, required_score=required_score, metrics=base_metrics,
        )

    if window in _MORNING_WINDOWS and morning_entry_count >= config.MAX_MORNING_ENTRIES:
        return _reject(
            decision=config.TW_REJECT_MAX_ENTRY_COUNT, block_reason=config.TW_REJECT_MAX_ENTRY_COUNT,
            reasons=[f"morning entry count {morning_entry_count} >= {config.MAX_MORNING_ENTRIES}"],
            score=quality_score, metrics=base_metrics,
        )
    if window in _AFTERNOON_WINDOWS and afternoon_entry_count >= config.MAX_AFTERNOON_ENTRIES:
        return _reject(
            decision=config.TW_REJECT_MAX_ENTRY_COUNT, block_reason=config.TW_REJECT_MAX_ENTRY_COUNT,
            reasons=[f"afternoon entry count {afternoon_entry_count} >= {config.MAX_AFTERNOON_ENTRIES}"],
            score=quality_score, metrics=base_metrics,
        )
    if (morning_entry_count + afternoon_entry_count) >= config.MAX_DAILY_ENTRIES:
        return _reject(
            decision=config.TW_REJECT_MAX_ENTRY_COUNT, block_reason=config.TW_REJECT_MAX_ENTRY_COUNT,
            reasons=["daily entry count >= MAX_DAILY_ENTRIES"],
            score=quality_score, metrics=base_metrics,
        )

    if position_direction == direction and not config.ALLOW_PYRAMIDING:
        return _reject(
            decision=config.TW_REJECT_DUPLICATE_POSITION, block_reason=config.TW_REJECT_DUPLICATE_POSITION,
            reasons=["already holding this direction and ALLOW_PYRAMIDING is False"],
            score=quality_score, metrics=base_metrics,
        )

    return MajorFlagDecision(
        approved=True, score=float(quality_score), required_score=required_score,
        decision=config.TW_APPROVED,
        reasons=(f"{window} approved: gap expanding, quality_score={quality_score}",),
        component_scores={k: (1.0 if v else 0.0) for k, v in quality_detail.items() if isinstance(v, bool)},
        metrics=base_metrics, is_reversal=False, fast_reversal=False, block_reason=None,
    )
