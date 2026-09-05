"""TW2 3-SLOT ("3슬롯 유연배분") — 2026-09-01 사용자 요청.

Pure functions only, entry-slot-orchestration half of a NEW, separately
selectable time-window mode. This module NEVER duplicates or modifies any
existing TW2/TEG decision logic — it is a thin orchestration layer on top of
the SAME, completely unmodified functions TW2 already uses:

  - ``time_window_filter.evaluate_time_window_entry`` (T+3 re-confirmation,
    per-window quality-score gate, interval/reset checks) — called by
    worker.py with entry-count params forced to 0/None so its OWN
    3/2/5 (MAX_MORNING/AFTERNOON/DAILY_ENTRIES) caps never fire; THIS
    module's ``resolve_slot`` is the only cap actually enforced for this mode.
  - ``time_window_filter.evaluate_tw2_extra_vetoes`` (VWAP-adverse veto +
    recent-cross veto) — reused unchanged.
  - ``app.trading.macd2.teg_gate.evaluate_teg`` ("TEGv2") — reused unchanged,
    called directly as a mandatory AND-gate for every afternoon candidate
    (NOT via production's once-daily count-cap-bypass mechanism — a
    deliberately different, new use of an existing, untouched function).
  - ``time_window_position_manager`` ladder (TP1/TP2 raised to
    ``config.TW2_MORNING_TP2``/trailing/SL) and the whipsaw-tolerant T+3
    OPPOSITE_SIGNAL reversal-exit classification
    (``config.TW_WHIPSAW_REJECT_REASONS``) — reused unchanged by worker.py's
    ``_resolve_tw2_3slot_candidate``, exactly as TW2 does.

Only genuinely NEW logic lives here:

1. ``evaluate_trend_quality`` — the 5-condition "Trend Quality" score gate
   for a morning 3rd-slot candidate (spec, ported faithfully from the
   validated backtest ``scripts/tw2_3slot_flex_backtest.py``):
     a. price vs EMA10 direction match (UP_RED: close>EMA10, DOWN_BLUE:
        close<EMA10) at the confirmation bar.
     b. EMA10-EMA20 SIGNED spread expansion in the signal direction over a
        2-completed-bar net-change window (not simple full alignment —
        catches a sharp early reversal a static EMA10>EMA20 check would
        miss, same spirit as teg_gate.py's ``_signed_net_change_condition``
        but no floor threshold, sign-of-change only).
     c. MACD-Signal gap (``time_window_filter._gap_series``) expanding in
        the signal direction over the same 2-bar window.
     d. EMA20 slope in the signal direction over the last 2 completed bars.
     e. VWAP direction match (reuses ``major_flag_filter._session_vwap``,
        the SAME VWAP TW2's own extra-veto already uses).
   Approved iff >= ``config.TW2_3SLOT_MORNING_3RD_QUALITY_MIN`` (default 3) of 5
   pass. Backtest-validated: TRAIN-selected 3/5 beat a stricter 4/5
   candidate on every TRAIN metric (data/validation/tw2_3slot_flex/).

2. ``resolve_slot`` — the 3-total-daily-slot budget + per-session gate
   requirement (pure function, no state/IO):
     - Session is determined by wall-clock time actually reached
       (``config.TW2_3SLOT_MORNING_WINDOW_END``=11:00,
       ``config.TW2_3SLOT_AFTERNOON_WINDOW_END``=14:50), not a fixed
       "slot index script" — a candidate's gate requirement follows whatever
       session it actually lands in.
     - Daily cap (default 3) checked first, always.
     - Morning: 1st/2nd candidate (morning_count < 2) = plain TW2 approval,
       no extra gate. 3rd (morning_count == 2, i.e. 2 slots already used
       today and still morning) = requires the Trend Quality gate above.
     - Afternoon: always requires TEG (mandatory AND-gate, not a bypass).
       A 2nd afternoon candidate (afternoon_count >= 1) additionally
       requires the account to be flat (the 1st afternoon position already
       fully closed) AND the new direction to be OPPOSITE the closed
       position's direction — a same-direction re-entry is rejected
       regardless of TW2/TEG approval. A live opposite-direction SWITCH of a
       still-held afternoon position is not "the 2nd afternoon candidate" in
       this sense (the sell leg itself closes the prior trade first, so the
       "already closed" requirement is trivially satisfied) and is not
       subject to this direction check — the caller only applies it to the
       genuinely flat case.

Both functions are pure (same inputs -> same outputs, no state mutation, no
I/O, no look-ahead beyond the data given) and independently unit-tested in
tests/macd2/test_time_window_3slot.py, mirroring every other MACD2
filter module's convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Optional, Union

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2 import time_window_filter as twf
from app.trading.macd2.major_flag_filter import _as_direction, _prepare_bars, _session_vwap
from app.trading.macd2.models import Direction

# ── Trend Quality (morning 3rd-slot gate) ───────────────────────────────────
QUALITY_COND_PRICE_EMA10 = "price_ema10_direction"
QUALITY_COND_EMA_SPREAD = "ema10_ema20_signed_spread_expanding"
QUALITY_COND_MACD_GAP = "macd_gap_signed_expanding"
QUALITY_COND_EMA20_SLOPE = "ema20_slope_direction"
QUALITY_COND_VWAP = "vwap_direction"

ALL_QUALITY_CONDITIONS = (
    QUALITY_COND_PRICE_EMA10, QUALITY_COND_EMA_SPREAD, QUALITY_COND_MACD_GAP,
    QUALITY_COND_EMA20_SLOPE, QUALITY_COND_VWAP,
)


@dataclass(frozen=True)
class TrendQualityDecision:
    approved: bool
    passed_count: int
    required: int
    conditions: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    reject_reasons: tuple = ()


def _insufficient_quality(reason: str, required: int) -> TrendQualityDecision:
    return TrendQualityDecision(
        approved=False, passed_count=0, required=required,
        conditions={}, metrics={}, reject_reasons=(reason,),
    )


def evaluate_trend_quality(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    *,
    required: Optional[int] = None,
) -> TrendQualityDecision:
    """``bars_3m`` truncated through the T+3 confirmation bar (its LAST row)
    — the same frame the caller already passed to
    ``time_window_filter.evaluate_time_window_entry``. Pure function, no
    look-ahead beyond that bar."""
    need = int(required if required is not None else config.TW2_3SLOT_MORNING_3RD_QUALITY_MIN)
    direction = _as_direction(flag_direction)
    if direction is None:
        return _insufficient_quality("invalid_direction", need)

    work = _prepare_bars(bars_3m)
    n_back = config.TW2_3SLOT_QUALITY_NET_CHANGE_BARS
    if work is None or len(work) < max(config.MAJOR_EMA_SLOW, n_back + 1) + 1:
        return _insufficient_quality("insufficient_bars", need)

    sign = 1 if direction == Direction.UP_RED else -1
    idx = len(work) - 1
    close = work["close"].astype(float).reset_index(drop=True)
    ema10 = close.ewm(span=config.MAJOR_EMA_FAST, adjust=False).mean()
    ema20 = close.ewm(span=config.MAJOR_EMA_SLOW, adjust=False).mean()

    conditions: dict[str, bool] = {}
    metrics: dict[str, Any] = {}
    reasons: list[str] = []

    # a) price vs EMA10 direction
    close_now = float(close.iloc[idx])
    ema10_now = float(ema10.iloc[idx])
    metrics["close"] = close_now
    metrics["ema10"] = ema10_now
    cond_a = (close_now > ema10_now) if direction == Direction.UP_RED else (close_now < ema10_now)
    conditions[QUALITY_COND_PRICE_EMA10] = bool(cond_a)
    if not cond_a:
        reasons.append(QUALITY_COND_PRICE_EMA10)

    # b) EMA10-EMA20 signed spread net-change expansion (n_back-bar window)
    back_idx = idx - n_back
    if back_idx >= 0:
        signed_spread = (ema10 - ema20) * sign
        net_change_b = float(signed_spread.iloc[idx] - signed_spread.iloc[back_idx])
        metrics["ema_spread_net_change"] = net_change_b
        cond_b = net_change_b > 0
    else:
        metrics["ema_spread_net_change"] = None
        cond_b = False
    conditions[QUALITY_COND_EMA_SPREAD] = bool(cond_b)
    if not cond_b:
        reasons.append(QUALITY_COND_EMA_SPREAD)

    # c) MACD-Signal gap signed net-change expansion (same window)
    series = twf._gap_series(work)
    if series is not None and len(series) > n_back and back_idx >= 0:
        signed_gap = series["gap"].reset_index(drop=True) * sign
        gap_idx = len(signed_gap) - 1
        gap_back_idx = gap_idx - n_back
        if gap_back_idx >= 0:
            net_change_c = float(signed_gap.iloc[gap_idx] - signed_gap.iloc[gap_back_idx])
            metrics["macd_gap_net_change"] = net_change_c
            cond_c = net_change_c > 0
        else:
            metrics["macd_gap_net_change"] = None
            cond_c = False
    else:
        metrics["macd_gap_net_change"] = None
        cond_c = False
    conditions[QUALITY_COND_MACD_GAP] = bool(cond_c)
    if not cond_c:
        reasons.append(QUALITY_COND_MACD_GAP)

    # d) EMA20 slope in signal direction over the last N bars
    slope_back = idx - config.TW2_3SLOT_EMA20_SLOPE_BARS
    if slope_back >= 0:
        ema20_now = float(ema20.iloc[idx])
        ema20_back = float(ema20.iloc[slope_back])
        metrics["ema20_now"] = ema20_now
        metrics["ema20_slope_back"] = ema20_back
        cond_d = (ema20_now > ema20_back) if direction == Direction.UP_RED else (ema20_now < ema20_back)
    else:
        cond_d = False
    conditions[QUALITY_COND_EMA20_SLOPE] = bool(cond_d)
    if not cond_d:
        reasons.append(QUALITY_COND_EMA20_SLOPE)

    # e) VWAP direction match
    vwap_series = _session_vwap(work)
    vwap_now = float(vwap_series.iloc[idx]) if idx < len(vwap_series) else float("nan")
    metrics["vwap"] = vwap_now if pd.notna(vwap_now) else None
    if pd.isna(vwap_now) or vwap_now <= 0:
        cond_e = False
    elif direction == Direction.UP_RED:
        cond_e = close_now > vwap_now
    else:
        cond_e = close_now < vwap_now
    conditions[QUALITY_COND_VWAP] = bool(cond_e)
    if not cond_e:
        reasons.append(QUALITY_COND_VWAP)

    passed = sum(1 for c in ALL_QUALITY_CONDITIONS if conditions.get(c, False))
    approved = passed >= need
    return TrendQualityDecision(
        approved=approved, passed_count=passed, required=need,
        conditions=conditions, metrics=metrics, reject_reasons=tuple(reasons),
    )


# ── Slot orchestration (3-total-daily-slot budget) ──────────────────────────
SESSION_MORNING = "MORNING"
SESSION_AFTERNOON = "AFTERNOON"

REJECT_OUTSIDE_WINDOW = config.TW2_3SLOT_REJECT_OUTSIDE_WINDOW
REJECT_SLOT_CAP = config.TW2_3SLOT_REJECT_SLOT_CAP
REJECT_SAME_DIRECTION_AFTERNOON = config.TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND


@dataclass(frozen=True)
class SlotDecision:
    slot_allowed: bool
    slot_number: Optional[int]
    session: Optional[str]
    requires_quality_gate: bool
    requires_teg_gate: bool
    reject_reason: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)


def resolve_slot(
    *,
    now: datetime,
    slots_used_today: int,
    morning_count: int,
    afternoon_count: int,
    direction: Union[Direction, str],
    is_flat: bool,
    last_afternoon_direction: Optional[str] = None,
) -> SlotDecision:
    """Pure decision: does a candidate arriving right now get a shot at a
    slot, and if so, which extra gate (quality / TEG) must it also clear?
    Never itself calls evaluate_time_window_entry/evaluate_tw2_extra_vetoes/
    evaluate_teg/evaluate_trend_quality — the caller (worker.py) composes
    those separately, using this function only to decide WHICH extra checks
    apply and whether the daily budget has room at all."""
    moment = now.astimezone(config.KST).time() if now.tzinfo else now.time()
    direction_obj = _as_direction(direction)

    if moment < config.SESSION_OPEN or moment >= config.TW2_3SLOT_AFTERNOON_WINDOW_END:
        return SlotDecision(
            slot_allowed=False, slot_number=None, session=None,
            requires_quality_gate=False, requires_teg_gate=False,
            reject_reason=REJECT_OUTSIDE_WINDOW,
        )

    session = SESSION_MORNING if moment < config.TW2_3SLOT_MORNING_WINDOW_END else SESSION_AFTERNOON

    if slots_used_today >= config.TW2_3SLOT_DAILY_CAP:
        return SlotDecision(
            slot_allowed=False, slot_number=None, session=session,
            requires_quality_gate=False, requires_teg_gate=False,
            reject_reason=REJECT_SLOT_CAP,
        )

    slot_number = slots_used_today + 1

    if session == SESSION_MORNING:
        requires_quality = morning_count >= 2
        return SlotDecision(
            slot_allowed=True, slot_number=slot_number, session=session,
            requires_quality_gate=requires_quality, requires_teg_gate=False,
        )

    # AFTERNOON
    if (
        afternoon_count >= 1
        and is_flat
        and direction_obj is not None
        and last_afternoon_direction is not None
        and direction_obj.value == last_afternoon_direction
    ):
        return SlotDecision(
            slot_allowed=False, slot_number=None, session=session,
            requires_quality_gate=False, requires_teg_gate=False,
            reject_reason=REJECT_SAME_DIRECTION_AFTERNOON,
        )

    return SlotDecision(
        slot_allowed=True, slot_number=slot_number, session=session,
        requires_quality_gate=False, requires_teg_gate=True,
    )


# ── Slot1 CHOP veto (2026-09-04) ────────────────────────────────────────────
SLOT1_CHOP_VETO_SLOT_NUMBER = 1


@dataclass(frozen=True)
class Slot1ChopVetoDecision:
    """``vetoed=True`` 이면 **그 신규진입만** 막는다. 슬롯 소비/청산/보유
    포지션에 대한 의미는 전혀 없다(호출자가 approved=False 로만 쓴다)."""
    vetoed: bool
    applicable: bool
    is_chop: bool
    score: int
    conditions: dict[str, bool] = field(default_factory=dict)
    reason: Optional[str] = None


def evaluate_slot1_chop_veto(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    decision_at: datetime,
    *,
    slot_number: Optional[int],
    enabled: Optional[bool] = None,
) -> Slot1ChopVetoDecision:
    """그날 첫 신규진입(Slot1) 후보가 진입시점 CHOP 이면 그 진입만 거절.

    새 점수식/임계값을 만들지 않는다 — 판정은 전적으로 production
    ``early_take_profit.evaluate_entry_chop`` 의 반환값(``is_chop``)이다.
    (그 함수는 이미 조기익절 필터가 매 체결마다 호출하는 것과 동일하며,
    ``bars_3m`` 은 T+3 확정봉까지 truncate 된 프레임, 즉 호출자가 이미
    ``evaluate_time_window_entry`` / ``resolve_slot`` 에 넘긴 그 프레임이다.)

    Slot2/Slot3 에는 절대 적용되지 않고(``applicable=False``),
    데이터 부족이면 veto 하지 않는다(기존 동작 유지 = 안전한 기본값).
    순수 함수: 상태 변경/IO 없음, 주어진 데이터 이후를 보지 않음.
    """
    # 지연 import: early_take_profit 은 이 모듈을 import 하지 않으므로 순환은
    # 없지만, 슬롯 오케스트레이션이 청산 모듈에 module-load 시점 의존성을
    # 갖지 않도록 호출 시점에만 가져온다.
    from app.trading.macd2 import early_take_profit

    active = config.TW2_3SLOT_SLOT1_CHOP_VETO if enabled is None else bool(enabled)
    if not active:
        return Slot1ChopVetoDecision(
            vetoed=False, applicable=False, is_chop=False, score=0, reason="disabled")
    if slot_number != SLOT1_CHOP_VETO_SLOT_NUMBER:
        return Slot1ChopVetoDecision(
            vetoed=False, applicable=False, is_chop=False, score=0, reason="not_slot1")

    chop = early_take_profit.evaluate_entry_chop(bars_3m, flag_direction, decision_at)
    if chop.insufficient_data:
        return Slot1ChopVetoDecision(
            vetoed=False, applicable=True, is_chop=False, score=int(chop.score),
            conditions=dict(chop.conditions), reason="insufficient_data")
    return Slot1ChopVetoDecision(
        vetoed=bool(chop.is_chop), applicable=True, is_chop=bool(chop.is_chop),
        score=int(chop.score), conditions=dict(chop.conditions),
        reason=(config.TW2_3SLOT_REJECT_SLOT1_ENTRY_CHOP if chop.is_chop else None),
    )
