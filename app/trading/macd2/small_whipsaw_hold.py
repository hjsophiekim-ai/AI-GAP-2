"""H50 — 작은 휩쏘 HOLD, X2-lite 전용 독립 필터 (2026-09-15).

이 모듈이 절대 하지 않는 것
---------------------------
진입 판정을 하지 않는다. MACD 플래그 생성, T+3 confirmation, TW TEG,
3-SLOT 슬롯 배분, quality gate, CHOP 판정, TEGv2, 신규진입 cutoff,
하루 3회 cap, W1a sizing 은 이 파일에서 import 조차 하지 않는 영역이고
한 줄도 수정되지 않았다. **H50 은 진입집합을 바꾸지 않는다.**

이 모듈이 하는 것 — 단 하나
---------------------------
보유 중 **반대 플래그가 정상 확정**됐을 때, 아래 두 조건을 모두 만족하면
그 반대신호 청산을 **보류(HOLD)** 한다.

    (a) 보유 방향이 구조적 상위추세와 같다
            LONG  보유 -> EMA20 > EMA50
            SHORT 보유 -> EMA20 < EMA50
    (b) 최근 60분(완성 3분봉 20개) high-low range <= 2.35%

HOLD 중에도 아래는 **전부 기존 그대로** 작동한다 (이 모듈은 관여하지 않는다):
    hard stop-loss -1.30% / TP1 / TP2 / trailing / breakeven / profit-lock /
    조기익절(ETP) / 강제청산 / 기존 risk management

HOLD 종료 조건 (아래 중 하나)
    1) 구조적 상위추세가 반대로 **2개 완성 3분봉 연속** 전환
    2) HOLD 시작 후 **60분** 경과
    3) 위 래더(하드스톱/TP/트레일링/ETP/강제청산)가 먼저 발동
종료 후에는 기존 X2-lite + W1a 로직으로 그대로 복귀한다.

HOLD 중 **반대방향 신규진입은 하지 않는다** (청산을 보류한 것이므로 포지션이
그대로 남아 있고, 슬롯도 소비되지 않는다).

순수 함수만 있다(``risk_exit.py`` / ``position_sizing.py`` 와 같은 계약):
네트워크/브로커 접근 없음. ``note_*`` / ``clear`` 만 state 를 갱신한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2 import time_window_3slot
from app.trading.macd2.major_flag_filter import _ema, _prepare_bars
from app.trading.macd2.models import Direction

#: 이 모듈이 내는 유일한 청산 사유.
EXIT_SMALL_WHIPSAW_HOLD = "SMALL_WHIPSAW_HOLD_EXIT"

TREND_UP = "UP"
TREND_DOWN = "DOWN"
TREND_FLAT = "FLAT"


@dataclass(frozen=True)
class HoldDecision:
    """반대 플래그를 HOLD 할 것인가."""
    should_hold: bool
    trend: str                  # UP / DOWN / FLAT
    range_pct: Optional[float]  # 최근 60분 high-low range (%)
    trend_ok: bool
    range_ok: bool
    insufficient_data: bool
    reason: str


@dataclass(frozen=True)
class ReleaseDecision:
    """HOLD 를 끝낼 것인가."""
    should_release: bool
    trend: str
    trend_break_count: int
    elapsed_min: Optional[float]
    reason: str


NO_HOLD = HoldDecision(
    should_hold=False, trend=TREND_FLAT, range_pct=None, trend_ok=False,
    range_ok=False, insufficient_data=True, reason="NOT_ACTIVE",
)


def is_active(state) -> bool:
    """H50 이 적용되는 상태인가 — H50 모드일 때만 True.

    다른 모든 전략(X2-lite / TW TEG 3-SLOT / TW2 3-SLOT / TW2 / TEGv2 /
    무필터 / MU_MACD)에서는 False 이므로 동작이 조금도 바뀌지 않는다."""
    if not bool(getattr(config, "H50_ENABLED", True)):
        return False
    return (time_window_3slot.active_3slot_mode(state)
            == time_window_3slot.MODE_X2LITE_H50_3SLOT)


# ── 지표 (production 함수 재사용, 새 지표식 없음) ──────────────────────────
def _structural_trend(work: pd.DataFrame) -> str:
    """EMA20 vs EMA50 배열. 완성 3분봉만 들어온다."""
    span_fast = int(config.H50_TREND_EMA_FAST)
    span_slow = int(config.H50_TREND_EMA_SLOW)
    if work is None or len(work) < span_slow:
        return TREND_FLAT
    fast = float(_ema(work["close"], span_fast).iloc[-1])
    slow = float(_ema(work["close"], span_slow).iloc[-1])
    if fast > slow:
        return TREND_UP
    if fast < slow:
        return TREND_DOWN
    return TREND_FLAT


def _range_pct(work: pd.DataFrame) -> Optional[float]:
    """최근 N봉(기본 20 = 60분) high-low 폭을 현재가 대비 %로."""
    bars = int(config.H50_RANGE_BARS)
    if work is None or len(work) < bars:
        return None
    win = work.iloc[-bars:]
    hi = float(win["high"].max())
    lo = float(win["low"].min())
    close = float(work["close"].iloc[-1])
    if close <= 0:
        return None
    return (hi - lo) / close * 100.0


def _wanted_trend(held_direction) -> str:
    """보유 방향이 '순방향'이려면 어떤 추세여야 하는가."""
    return TREND_UP if held_direction == Direction.UP_RED else TREND_DOWN


def evaluate_hold(bars_3m, held_direction, now: Optional[datetime] = None) -> HoldDecision:
    """반대 플래그 확정 시점에 HOLD 할지 판정. 순수 함수."""
    work = _prepare_bars(bars_3m)
    if work is None:
        return HoldDecision(False, TREND_FLAT, None, False, False, True,
                            "INSUFFICIENT_BARS")
    trend = _structural_trend(work)
    rng = _range_pct(work)
    if trend == TREND_FLAT or rng is None:
        return HoldDecision(False, trend, rng, False, False, True, "INSUFFICIENT_BARS")
    trend_ok = trend == _wanted_trend(held_direction)
    range_ok = rng <= float(config.H50_RANGE_MAX_PCT)
    hold = bool(trend_ok and range_ok)
    if hold:
        reason = "SMALL_WHIPSAW_HOLD"
    elif not trend_ok:
        reason = "TREND_NOT_ALIGNED"
    else:
        reason = "RANGE_TOO_WIDE"
    return HoldDecision(hold, trend, rng, trend_ok, range_ok, False, reason)


def evaluate_release(bars_3m, held_direction, started_at: Optional[datetime],
                     now: datetime, trend_break_count: int = 0) -> ReleaseDecision:
    """완성봉마다 호출 — HOLD 를 끝낼지 판정. 순수 함수(카운터는 반환만 한다).

    1) 구조적 추세가 보유 반대방향으로 **2봉 연속** -> 해제
    2) HOLD 시작 후 ``H50_MAX_HOLD_MIN`` 분 경과 -> 해제
    """
    work = _prepare_bars(bars_3m)
    if work is None:
        return ReleaseDecision(False, TREND_FLAT, int(trend_break_count), None,
                               "INSUFFICIENT_BARS")
    trend = _structural_trend(work)
    want = _wanted_trend(held_direction)
    broke = trend != want and trend != TREND_FLAT
    count = int(trend_break_count) + 1 if broke else 0
    elapsed = None
    if started_at is not None:
        elapsed = (now - started_at).total_seconds() / 60.0
    if count >= int(config.H50_TREND_BREAK_BARS):
        return ReleaseDecision(True, trend, count, elapsed, "TREND_BREAK")
    if elapsed is not None and elapsed >= float(config.H50_MAX_HOLD_MIN):
        return ReleaseDecision(True, trend, count, elapsed, "MAX_HOLD")
    return ReleaseDecision(False, trend, count, elapsed, "HOLDING")


# ── state 헬퍼 (이 모듈 전용 필드만 만진다) ────────────────────────────────
def note_hold_start(state, *, held_direction, now: datetime, decision: HoldDecision) -> None:
    state.h50_hold_active = True
    state.h50_original_direction = getattr(held_direction, "value", str(held_direction))
    state.h50_hold_started_at = now.isoformat()
    state.h50_trend_break_count = 0
    state.h50_last_checked_bar_ts = None
    state.h50_last_hold_range_pct = (
        None if decision.range_pct is None else round(float(decision.range_pct), 4)
    )


def note_trend_break_count(state, count: int) -> None:
    state.h50_trend_break_count = int(count)


def note_checked_bar(state, bar_ts) -> None:
    state.h50_last_checked_bar_ts = (
        bar_ts.isoformat() if hasattr(bar_ts, "isoformat") else (bar_ts or None)
    )


def clear(state) -> None:
    """HOLD 종료(해제/청산/일자변경/재시작 정리) 공통 경로."""
    state.h50_hold_active = False
    state.h50_hold_started_at = None
    state.h50_original_direction = None
    state.h50_trend_break_count = 0
    state.h50_last_checked_bar_ts = None
    state.h50_last_hold_range_pct = None


def is_holding(state) -> bool:
    return bool(getattr(state, "h50_hold_active", False))


def held_direction(state) -> Optional[Direction]:
    raw = getattr(state, "h50_original_direction", None)
    if not raw:
        return None
    try:
        return Direction(raw)
    except ValueError:
        return None
