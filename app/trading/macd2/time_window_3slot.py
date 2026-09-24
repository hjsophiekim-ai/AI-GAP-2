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


# ── AR1 — 오후 동일방향 재진입 예외 (2026-09-23 연구, 2026-09-24 내장) ───────
#: AR1 로 재진입이 열렸을 때 slot_metrics 에 남기는 사유 문자열.
AR1_ALLOW_REASON = "AR1_STACK_EXEMPT"
AR1_REJECT_OUT_OF_SCOPE = "AR1_OUT_OF_SCOPE"
AR1_REJECT_NO_CONDITIONS = "AR1_NO_TEG_CONDITIONS"
AR1_REJECT_MULTI_FAIL = "AR1_MULTI_CONDITION_FAIL"
AR1_REJECT_NO_MOMENTUM = "AR1_MOMENTUM_NOT_CONFIRMED"
AR1_ALLOW_TEG_APPROVED = "AR1_TEG_ALREADY_APPROVED"


@dataclass(frozen=True)
class AR1Decision:
    """``REJECT_SAME_DIRECTION_AFTERNOON`` 을 되돌릴지에 대한 답 하나뿐이다."""
    allowed: bool = False
    stack_exempt: bool = False
    reason: str = ""
    failing_conditions: tuple = ()
    metrics: dict[str, Any] = field(default_factory=dict)


def evaluate_afternoon_reentry(
    teg_decision: Any,
    *,
    base_reject_reason: Optional[str],
) -> AR1Decision:
    """AR1 — **narrow exception only.** 새 numerical threshold 를 만들지 않는다.

    적용대상은 ``REJECT_SAME_DIRECTION_AFTERNOON`` 으로 거절된 후보뿐이다
    (호출부가 그 사유를 넘길 때만 동작한다 — 오후 TEG 전체를 완화할 수 없다).
    TEG 는 그대로 요구하되, **탈락 조건이 ``price_ema_stack_aligned`` 하나뿐이고**
    동시에 ``macd_gap_signed_net_expanding`` / ``ema_spread_signed_net_expanding``
    / ``vwap_favorable_side`` 가 전부 참이면 stack 하나만 면제한다.

    통과는 "즉시 주문"이 아니라 **동일방향 거절의 해제**일 뿐이다 — 이후 기존
    진입 파이프라인(CHOP TEG / 예산 / SMART sizing / order path)을 그대로 탄다.

    검증: research_20260923c_x1_80d (80영업일 20260527~20260922) — BASE 162거래
    /복리 422.9169 대비 AR1 171거래/457.9459, LOO 80/80 양수, MDD 불변.
    2026-09-23 FLIP EXIT 제외 재검증에서도 같은 숫자를 재현했다. 순수함수다.
    """
    from app.trading.macd2 import teg_gate as _teg

    if base_reject_reason != REJECT_SAME_DIRECTION_AFTERNOON:
        return AR1Decision(reason=AR1_REJECT_OUT_OF_SCOPE)
    conditions = dict(getattr(teg_decision, "conditions", None) or {})
    if not conditions:
        return AR1Decision(reason=AR1_REJECT_NO_CONDITIONS)
    metrics = dict(getattr(teg_decision, "metrics", None) or {})
    if bool(getattr(teg_decision, "approved", False)):
        return AR1Decision(allowed=True, stack_exempt=False,
                           reason=AR1_ALLOW_TEG_APPROVED, metrics=metrics)
    failing = tuple(c for c in _teg.ALL_CONDITIONS if not conditions.get(c, False))
    if failing != (_teg.COND_EMA_STACK,):
        return AR1Decision(reason=AR1_REJECT_MULTI_FAIL,
                           failing_conditions=failing, metrics=metrics)
    if not (conditions.get(_teg.COND_MACD_GAP_EXPANDING)
            and conditions.get(_teg.COND_EMA_SPREAD_EXPANDING)
            and conditions.get(_teg.COND_VWAP)):
        return AR1Decision(reason=AR1_REJECT_NO_MOMENTUM,
                           failing_conditions=failing, metrics=metrics)
    return AR1Decision(allowed=True, stack_exempt=True, reason=AR1_ALLOW_REASON,
                       failing_conditions=failing, metrics=metrics)


# ── TWF 3-SLOT — 진입은 TW2 3-SLOT 과 동일, 청산 3개만 다름 (2026-09-07) ────
MODE_TW2_3SLOT = "TW2_3SLOT"
MODE_TWF_3SLOT = "TWF_3SLOT"
#: X2-lite (2026-09-12) — 진입은 TW TEG 3-SLOT(MODE_TWF_3SLOT) 과 100% 동일하고
#: 청산 파라미터만 다르다. config.py 의 X2LITE_* 블록 참조.
MODE_X2LITE_3SLOT = "X2LITE_3SLOT"
#: H50 (2026-09-15) — X2-lite 와 **진입·청산 파라미터가 100% 동일**하고,
#: 반대신호 청산을 조건부로 보류하는 것(small_whipsaw_hold)만 다르다.
#: 그래서 아래 모드 분기들은 전부 X2-lite 와 같은 값을 돌려준다.
MODE_X2LITE_H50_3SLOT = "X2LITE_H50_3SLOT"
#: "X2-lite 파라미터를 쓰는 모드" 집합. 새 모드가 늘어도 여기만 보면 된다.
MODES_X2LITE_FAMILY = (MODE_X2LITE_3SLOT, MODE_X2LITE_H50_3SLOT)
#: N1 (2026-09-20) — 진입은 H50 과 동일(quality 임계값만 3), 청산은 상위추세로
#: TP1/TP1비중/TP2 가 봉마다 전환된다. **MODES_X2LITE_FAMILY 에 넣지 않는다** —
#: 그 집합은 exit_overrides / morning_tp2_pct_override / early_take_profit.
#: thresholds 가 X2-lite 값을 돌려주는 기준이고, N1 은 그 셋이 전부 다르다.
#: X2-lite/H50 의 반환값을 한 값도 바꾸지 않기 위해 별도 집합으로 둔다.
MODE_N1_3SLOT = "N1_3SLOT"
MODES_N1_FAMILY = (MODE_N1_3SLOT,)
#: W1a 사이징 / CHOP->TEG 게이트 / small whipsaw HOLD 를 **공유**하는 집합.
#: N1 은 이 셋을 X2-lite 계열과 100% 같이 쓴다(연구사양).
MODES_W1A_FAMILY = MODES_X2LITE_FAMILY + MODES_N1_FAMILY
#: ``state.time_window_active_mode`` 가 이 셋 중 하나면 "3-SLOT 계열"이다.
#: 진입 경로(worker._judge_tw2_3slot_flag / _resolve_tw2_3slot_candidate),
#: 슬롯 카운터(state.tw2_3slot_*), 원장 컬럼, signal_type 은 세 모드가 전부
#: 공유한다 — 갈라지는 것은 ``exit_overrides`` / ``morning_tp2_pct_override``
#: 가 돌려주는 청산 임계값뿐이다.
MODES_3SLOT = (MODE_TW2_3SLOT, MODE_TWF_3SLOT, MODE_X2LITE_3SLOT,
               MODE_X2LITE_H50_3SLOT, MODE_N1_3SLOT)

#: 2026-09-08 이 모드는 "TW TEG 3-SLOT" 으로 정리됐다. 디스크에 이미 저장된
#: 상태/원장 값과의 호환을 위해 wire value 는 "TWF_3SLOT" 그대로 두고 이름만
#: 별칭으로 붙인다(config.TW_TEG_3SLOT_STRATEGY_NAME 주석 참고).
MODE_TW_TEG_3SLOT = MODE_TWF_3SLOT


def quality_score_threshold(mode: Optional[str]) -> int:
    """이 모드의 Trend Quality 통과 기준 개수.

    N1 만 3 이고(연구사양 q3), 나머지 전 모드는
    ``config.QUALITY_SCORE_THRESHOLD``(4) 그대로다 — 기존 동작 불변.
    """
    if mode in MODES_N1_FAMILY:
        return int(config.N1_QUALITY_SCORE_THRESHOLD)
    return int(config.QUALITY_SCORE_THRESHOLD)


def requires_chop_teg_gate(mode: Optional[str]) -> bool:
    """CHOP 진입후보에 TEGv2 를 추가로 요구하는 모드인가.

    TW TEG 3-SLOT 과 X2-lite 에서 True — X2-lite 의 진입은 TW TEG 3-SLOT 과
    100% 동일해야 하므로 이 규칙도 그대로 공유한다(2026-09-12). TW2 3-SLOT /
    TW2 / TEGv2 / MU_MACD 는 전부 False 라 기존 동작은 조금도 바뀌지 않는다.
    """
    return mode in (MODE_TW_TEG_3SLOT,) + MODES_W1A_FAMILY

#: 토글이 동시에 켜지는 일은 service 의 상호배제가 막지만, 만에 하나
#: 그런 상태가 들어와도 결정론적으로 TW2 3-SLOT 이 이긴다(기존 동작 보존).
#: X2-lite 는 가장 마지막 — 기존 두 모드의 우선순위를 바꾸지 않는다.
_MODE_BY_FLAG = (
    ("time_window_3slot_filter_enabled", MODE_TW2_3SLOT),
    ("time_window_twf_filter_enabled", MODE_TWF_3SLOT),
    ("time_window_x2lite_filter_enabled", MODE_X2LITE_3SLOT),
    ("time_window_h50_filter_enabled", MODE_X2LITE_H50_3SLOT),
    # N1 은 가장 마지막 — 기존 네 모드의 우선순위를 한 칸도 바꾸지 않는다.
    ("time_window_n1_filter_enabled", MODE_N1_3SLOT),
)


def active_3slot_mode(state) -> Optional[str]:
    """켜져 있는 3-SLOT 계열 모드 이름, 없으면 None."""
    for flag, mode in _MODE_BY_FLAG:
        if bool(getattr(state, flag, False)):
            return mode
    return None


def is_3slot_enabled(state) -> bool:
    """TW2 3-SLOT 또는 TWF 3-SLOT 중 하나라도 켜져 있는가."""
    return active_3slot_mode(state) is not None


def afternoon_reentry_exception_enabled(state) -> bool:
    """AR1 을 이 상태에서 평가해도 되는가 — **N1 경로 전용**이다.

    AR1 의 80영업일 검증 BASE 는 N1 + C1 (+ SMART 민감도) 하나뿐이다. 오후
    슬롯 코드(resolve_slot / _resolve_tw2_3slot_candidate_body)는 3-SLOT 계열
    다섯 모드가 통째로 공유하므로, 게이트가 없으면 X2-lite W1 / H50 /
    TW2 3-SLOT / TW TEG 3-SLOT 까지 AR1 이 함께 발동한다. 그 조합은 검증된
    적이 없다 — 여기서 N1 계열로 잘라 연구 scope 와 실엔진 scope 를 맞춘다.

    별도 토글은 두지 않는다(2026-09-24 사용자 확정): N1 이 켜지면 AR1 도 N1
    내부 규칙으로 자동으로 켜지고, N1 이 아니면 평가 자체를 하지 않는다.
    """
    return active_3slot_mode(state) in MODES_N1_FAMILY


def scheduled_entry_supported(state) -> bool:
    """09:03 예약매수(2026-08-06)를 이 모드에서 쓸 수 있는가.

    3-SLOT 계열(TW2 3-SLOT / TW TEG 3-SLOT / X2-lite / H50)에서는 **쓰지 않는다**.
    이 계열은 하루 슬롯 예산과 T+3 재확인으로 진입을 통제하는데, 예약매수는 그
    통제를 통째로 우회해 09:03에 방향만 보고 전량매수한다. 2026-09-16 실거래
    사고(H50 운영 중 08:00 BLUE 기준 09:03 인버스 매수)가 정확히 그 경로였다.

    자매 기능인 프리마켓 승계는 처음부터 TW2/TEGv2 가 아니면 발동하지 않았고
    (2026-09-01 에 TW2_3SLOT 도 명시적으로 제외), 예약매수에만 그 게이트가
    없었다. 여기서 대칭을 맞춘다 -- TW/TW2/TEGv2/무필터 등 다른 전략의 예약매수
    동작은 이 함수로 조금도 바뀌지 않는다.
    """
    if bool(getattr(config, "SCHEDULED_ENTRY_ALLOW_IN_3SLOT", False)):
        # 2026-09-16 사용자 결정: 하드 차단이 아니라 환경변수 스위치로 둔다.
        # MACD2_SCHEDULED_ENTRY_ALLOW_IN_3SLOT=1 이면 3-SLOT 계열에서도 예전처럼
        # 쓸 수 있다(기본 0=차단). 이 함수가 제어하는 것은 **1차 방어(arm 금지)
        # 뿐**이고, 주문 직전 MACD 재확인 / 복원 무효화 / arm 만료·소진은
        # 이 값과 무관하게 항상 동작한다.
        return True
    return not is_3slot_enabled(state)


def exit_overrides(mode: Optional[str]) -> dict:
    """``time_window_position_manager`` 에 넘길 override 묶음.

    TWF 3-SLOT / X2-lite 가 아니면 전부 ``None`` 이라 기존 모듈 상수가 그대로
    쓰인다 — TW2 3-SLOT / TW2 / TEGv2 / MU_MACD 동작은 이 함수로 조금도 바뀌지
    않는다. 모듈 상수를 직접 갈아끼우지 않는 이유는 그 상수를 MU_MACD 가
    같은 모듈에서 import 해 쓰기 때문이다(config.py MORNING_STOP_LOSS 주석).

    2026-09-12: X2-lite 추가로 ``tp1_sell_ratio_override`` /
    ``trailing_stop_pct_override`` 두 키가 늘었다. TWF 3-SLOT 을 포함한 기존
    모드에서는 두 키가 항상 ``None`` 이므로 기존 동작은 불변이다."""
    if mode == MODE_TWF_3SLOT:
        return {
            "stop_loss_pct_override": float(config.TWF_MORNING_STOP_LOSS) * 100.0,
            "after_tp1_stop_pct_override": float(config.TWF_MORNING_AFTER_TP1_STOP) * 100.0,
            "afternoon_tp_pct_override": float(config.TWF_AFTERNOON_TP) * 100.0,
            "tp1_sell_ratio_override": None,
            "trailing_stop_pct_override": None,
        }
    if mode in MODES_N1_FAMILY:
        # N1 (2026-09-20): X2-lite 값과 **두 개만** 다르다 — trailing stop
        # 2.80 -> 1.50, 오후TP 3.00 -> 4.00 (N1_SPEC.md 3-1).
        return {
            "stop_loss_pct_override": float(config.X2LITE_MORNING_STOP_LOSS) * 100.0,
            "after_tp1_stop_pct_override": float(config.X2LITE_MORNING_AFTER_TP1_STOP) * 100.0,
            "afternoon_tp_pct_override": float(config.N1_AFTERNOON_TP) * 100.0,
            "tp1_sell_ratio_override": float(config.X2LITE_MORNING_TP1_SELL_RATIO),
            "trailing_stop_pct_override": float(config.N1_MORNING_TRAILING_STOP) * 100.0,
        }
    if mode in MODES_X2LITE_FAMILY:
        return {
            "stop_loss_pct_override": float(config.X2LITE_MORNING_STOP_LOSS) * 100.0,
            "after_tp1_stop_pct_override": float(config.X2LITE_MORNING_AFTER_TP1_STOP) * 100.0,
            "afternoon_tp_pct_override": float(config.X2LITE_AFTERNOON_TP) * 100.0,
            "tp1_sell_ratio_override": float(config.X2LITE_MORNING_TP1_SELL_RATIO),
            "trailing_stop_pct_override": float(config.X2LITE_MORNING_TRAILING_STOP) * 100.0,
        }
    return {
        "stop_loss_pct_override": None,
        "after_tp1_stop_pct_override": None,
        "afternoon_tp_pct_override": None,
        "tp1_sell_ratio_override": None,
        "trailing_stop_pct_override": None,
    }


#: TP2 override 를 쓰는 모드 집합. 2026-08-21 부터 worker 가
#: ``("TW2", "TEG", "TEGv2") + MODES_3SLOT`` 조건으로 TW2_MORNING_TP2(6.0%) 를
#: 넘겨 왔는데, X2-lite 는 같은 3-SLOT 계열이면서 TP2 가 5.0% 라서 그 조건식을
#: 그대로 둘 수 없다. 아래 헬퍼로 분기를 옮기되 **기존 5개 모드의 반환값은
#: 한 값도 바뀌지 않는다**(회귀 테스트로 잠금).
_TP2_TW2_MODES = ("TW2", "TEG", "TEGv2", MODE_TW2_3SLOT, MODE_TWF_3SLOT)


def morning_tp2_pct_override(mode: Optional[str]) -> Optional[float]:
    """오전 TP2 override(%), 해당 없으면 ``None``.

    TW2/TEG/TEGv2/TW2 3-SLOT/TW TEG 3-SLOT -> ``config.TW2_MORNING_TP2 * 100``
    (2026-08-21 이후 기존 동작 그대로), X2-lite -> ``X2LITE_MORNING_TP2 * 100``
    (5.0%), 그 외(MU_MACD/무필터/입양 포지션) -> ``None`` 이라 모듈 기본
    MORNING_TP2 가 쓰인다."""
    if mode in MODES_N1_FAMILY:
        # N1 의 **추세구간** TP2. 비추세 4.0 은 worker 가 n1_adaptive 판정으로
        # tp2_pct_override 를 직접 덮어쓴다(이 함수는 모드만 보므로 봉 상태를
        # 알 수 없다). 즉 이 값은 "adaptive 판정이 없을 때의 기준값" 이다.
        return float(config.N1_TREND_TP2) * 100.0
    if mode in MODES_X2LITE_FAMILY:
        return float(config.X2LITE_MORNING_TP2) * 100.0
    if mode in _TP2_TW2_MODES:
        return float(config.TW2_MORNING_TP2) * 100.0
    return None


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
