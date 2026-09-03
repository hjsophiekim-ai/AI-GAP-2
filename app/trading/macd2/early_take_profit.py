"""조기익절 필터 (Early Take-Profit filter) — TW2 3-SLOT 전용, 따로 켜고 끄는
risk-management 단계 서브필터. 2026-09-03 사용자 요청.

이름 주의: MACD2에는 이미 완전히 무관한 옛 기능 "PROFIT_LOCK"이 있다
(``config.PROFIT_LOCK_*``, ``worker._advance_profit_lock``,
``config.EXIT_PROFIT_LOCK``, ``state.profit_lock_*``). 이 모듈은 그것과
상수/상태필드/코드경로를 하나도 공유하지 않으며, 전부 ``EARLY_TP_*`` 접두사를
쓴다. 자세한 배경/검증수치는 ``config.py``의 EARLY_TP_* 블록 참고.

이 모듈이 절대 하지 않는 것
---------------------------
진입 판단을 전혀 하지 않는다. TW2 3-SLOT의 MACD zero-cross 검출, T+3 재확인,
TW2 veto, 슬롯 배정, Trend Quality, TEGv2 는 이 파일에서 import조차 하지 않는
영역이고 한 줄도 수정되지 않았다. 여기 있는 함수는 (1) 이미 체결된 진입의
확정봉이 CHOP이었는지 분류하고, (2) 이미 보유 중인 포지션에 대해 "지금 잔량을
전량 매도할지"만 답한다. 신규 진입/추가매수/슬롯 소비를 유발할 수 있는 반환값이
아예 존재하지 않는다.

기존 청산 우선순위
------------------
호출자(``worker._advance_held_position_risk_management``)는 production 래더
(``time_window_position_manager.evaluate_take_profit_immediate`` 로 TP1/TP2/
오후TP, 그 다음 ``evaluate_position`` 으로 손절/after-TP1-stop/trailing)를 먼저
전부 평가하고, **그 어느 것도 발동하지 않은 경우에만** 이 모듈을 호출한다.
따라서 실효 스탑은 ``max(production 활성 스탑, EARLY_TP_FLOOR_PCT)`` 이 되고
TP1/TP2/trailing 은 그대로 살아 있다.

순수 함수만 있다(``risk_exit.py``/``time_window_position_manager.py``와 동일한
계약): 네트워크/상태파일/브로커 접근 없음, 입력 프레임 변경 없음, 주어진
데이터 이후를 내다보지 않음.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Union

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2 import time_window_filter as twf
from app.trading.macd2.major_flag_filter import _as_direction, _prepare_bars, _session_vwap
from app.trading.macd2.models import Direction

# ── CHOP 판정 4개 조건 (2026-09-03 TRAIN 확정, 재튜닝 금지) ─────────────────
CHOP_COND_RECENT_CROSSES = "recent_confirmed_crosses"
CHOP_COND_SPREAD_NOT_EXPANDING = "ema10_ema20_spread_not_expanding"
CHOP_COND_EMA20_SLOPE_NOT_ALIGNED = "ema20_slope_not_aligned"
CHOP_COND_VWAP_REPEAT = "vwap_repeat"

ALL_CHOP_CONDITIONS = (
    CHOP_COND_RECENT_CROSSES,
    CHOP_COND_SPREAD_NOT_EXPANDING,
    CHOP_COND_EMA20_SLOPE_NOT_ALIGNED,
    CHOP_COND_VWAP_REPEAT,
)

LABEL_NOT_ENTRY_CHOP = "NOT_ENTRY_CHOP"
LABEL_NOT_ARMED = "NOT_ARMED"
LABEL_ARMED_HOLD = "ARMED_HOLD"
LABEL_FIRED = "EARLY_TP_FIRED"


@dataclass(frozen=True)
class EntryChopDecision:
    """진입 확정봉의 CHOP/TREND 판정. ``is_chop``만 포지션에 저장되고, 나머지는
    UI/ledger 진단용이다."""
    is_chop: bool
    score: int
    required: int
    conditions: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    insufficient_data: bool = False


@dataclass(frozen=True)
class EarlyTakeProfitDecision:
    """``exit_reason`` 이 None이면 HOLD. ``sell_fraction`` 은 현재 보유 잔량 대비
    비율이며 이 필터는 항상 1.0(전량) 또는 0.0이다 — 부분매도는 production
    TP1만 하고, 이 필터가 부분매도를 만들어내지 않는다."""
    armed: bool
    exit_reason: Optional[str]
    sell_fraction: float
    label: str


def _insufficient(required: int) -> EntryChopDecision:
    return EntryChopDecision(
        is_chop=False, score=0, required=required,
        conditions={}, metrics={}, insufficient_data=True,
    )


def _first_session_bar_index(work: pd.DataFrame, last_idx: int) -> Optional[int]:
    """``last_idx`` 봉이 속한 **같은 날** 정규장(09:00 이후) 첫 봉의 인덱스.

    30분 창이 전일이나 장전(08:00-09:00 NXT 프리마켓)으로 새는 것을 막는다 —
    ``twf._count_recent_confirmed_crossovers`` 자신이 2026-08-25 실제사고 때문에
    장전 봉을 제외하는 것과 같은 이유이고, ``_session_vwap`` 도 일자별
    cumsum이라 같은 경계를 쓴다."""
    kst = work["datetime"].dt.tz_convert(config.KST)
    day = kst.dt.strftime("%Y%m%d")
    target_day = day.iloc[last_idx]
    first = None
    for i in range(last_idx + 1):
        if day.iloc[i] != target_day:
            continue
        if kst.iloc[i].time() < config.SESSION_OPEN:
            continue
        first = i
        break
    return first


def evaluate_entry_chop(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    decision_at: datetime,
) -> EntryChopDecision:
    """``bars_3m`` 는 T+3 확정봉까지 잘린 프레임(마지막 행이 확정봉) —
    ``time_window_filter.evaluate_time_window_entry`` / ``time_window_3slot.
    evaluate_trend_quality`` 에 넘기는 것과 완전히 같은 프레임이다. 순수 함수이며
    그 봉 이후를 내다보지 않는다.

    4개 조건 중 ``config.EARLY_TP_SCORE_MIN`` 개 이상이면 CHOP:

      1. 최근 30분(``EARLY_TP_LOOKBACK_MINUTES``) 확정 zero-cross 횟수가
         ``EARLY_TP_RECENT_CROSS_MIN`` 이상 — production
         ``twf._count_recent_confirmed_crossovers`` 를 그대로 호출한다(자체
         크로스오버 재구현 없음, 장전 제외도 그 함수가 이미 한다).
         후보 자신의 플래그 봉을 제외하지 **않는다**(TW2 veto는 제외하지만,
         여기서 재는 것은 "최근 30분에 교차가 몇 번 있었나"이므로 자기 교차도
         포함하는 것이 정의다 — 그래서 기본 임계값이 1이다).
      2. EMA10-EMA20 spread 의 진입방향 부호 순변화 <= 0 (30분 창) —
         즉 추세가 벌어지지 **못하고** 있음. EMA span/공식은
         ``config.MAJOR_EMA_FAST/SLOW`` 로 Trend Quality 조건 b, teg_gate 조건 4,
         ``evaluate_whipsaw_watch`` 가 쓰는 것과 동일하다(새 지표 없음).
      3. EMA20 의 진입방향 부호 순변화 <= 0 (같은 창) — slope 가 진입방향이
         아님.
      4. 최근 30분 종가-세션VWAP 부호 교차 횟수가 ``EARLY_TP_VWAP_FLIP_MIN``
         이상 — VWAP 위아래를 반복. VWAP 은 ``major_flag_filter._session_vwap``
         (TW2 veto/Trend Quality 조건 e 가 쓰는 바로 그 VWAP).

    30분 창에 정규장 봉이 ``EARLY_TP_MIN_BARS`` 개보다 적으면
    ``insufficient_data=True``, ``is_chop=False`` — 09:15 이전 진입은 구조적으로
    CHOP 판정이 불가능하고, 그런 경우는 TREND(=필터 미적용)로 취급한다.
    """
    required = int(config.EARLY_TP_SCORE_MIN)
    direction = _as_direction(flag_direction)
    if direction is None:
        return _insufficient(required)

    work = _prepare_bars(bars_3m)
    if work is None or len(work) < config.MAJOR_EMA_SLOW + 1:
        return _insufficient(required)

    idx = len(work) - 1
    kst_last = pd.Timestamp(work["datetime"].iloc[idx]).astimezone(config.KST)
    if kst_last.time() < config.SESSION_OPEN:
        return _insufficient(required)

    lookback_bars = max(1, int(config.EARLY_TP_LOOKBACK_MINUTES) // 3)
    first_session = _first_session_bar_index(work, idx)
    if first_session is None:
        return _insufficient(required)
    anchor = max(idx - lookback_bars, first_session)
    if idx - anchor < int(config.EARLY_TP_MIN_BARS):
        return _insufficient(required)

    sign = 1 if direction == Direction.UP_RED else -1
    close = work["close"].astype(float).reset_index(drop=True)
    ema10 = close.ewm(span=config.MAJOR_EMA_FAST, adjust=False).mean()
    ema20 = close.ewm(span=config.MAJOR_EMA_SLOW, adjust=False).mean()
    spread = ema10 - ema20

    conditions: dict[str, bool] = {}
    metrics: dict[str, Any] = {}

    recent_crosses = twf._count_recent_confirmed_crossovers(
        work, decision_at, int(config.EARLY_TP_LOOKBACK_MINUTES),
    )
    metrics["recent_confirmed_crosses"] = int(recent_crosses)
    conditions[CHOP_COND_RECENT_CROSSES] = bool(
        recent_crosses >= int(config.EARLY_TP_RECENT_CROSS_MIN)
    )

    spread_change = float(spread.iloc[idx] - spread.iloc[anchor]) * sign
    metrics["ema_spread_signed_change"] = spread_change
    conditions[CHOP_COND_SPREAD_NOT_EXPANDING] = bool(spread_change <= 0)

    ema20_change = float(ema20.iloc[idx] - ema20.iloc[anchor]) * sign
    metrics["ema20_signed_change"] = ema20_change
    conditions[CHOP_COND_EMA20_SLOPE_NOT_ALIGNED] = bool(ema20_change <= 0)

    vwap = _session_vwap(work).reset_index(drop=True)
    flips = 0
    prev_sign = 0
    for j in range(anchor, idx + 1):
        v = float(vwap.iloc[j])
        if not pd.notna(v) or v <= 0:
            continue
        c = float(close.iloc[j])
        s = 1 if c > v else (-1 if c < v else 0)
        if s == 0:
            continue
        if prev_sign != 0 and s != prev_sign:
            flips += 1
        prev_sign = s
    metrics["vwap_flip_count"] = int(flips)
    metrics["lookback_bars_used"] = int(idx - anchor)
    conditions[CHOP_COND_VWAP_REPEAT] = bool(flips >= int(config.EARLY_TP_VWAP_FLIP_MIN))

    score = sum(1 for c in ALL_CHOP_CONDITIONS if conditions.get(c, False))
    return EntryChopDecision(
        is_chop=bool(score >= required), score=int(score), required=required,
        conditions=conditions, metrics=metrics, insufficient_data=False,
    )


def is_enabled(state) -> bool:
    """토글 자체 + TW2 3-SLOT 동시활성 여부. TW2 3-SLOT이 OFF면 이 필터는
    자동으로 비활성이다(사용자 요청). 진입 시점의 CHOP 판정을 저장할지 말지를
    결정하는 데도 이 함수를 쓴다 — 필터가 OFF일 때는 판정 자체를 계산하지
    않아야 동작이 완전히 불변이기 때문이다."""
    return bool(
        getattr(state, "early_tp_filter_enabled", False)
        and getattr(state, "time_window_3slot_filter_enabled", False)
    )


def is_active(state) -> bool:
    """청산 판단에 참여할 수 있는지. ``is_enabled`` 에 더해 **현재 보유 포지션이
    TW2 3-SLOT 이 관리하는 포지션**이어야 한다 — TW2/TEGv2 가 열었거나
    브로커에서 발견돼 입양된 포지션에는 절대 적용되지 않는다."""
    return bool(
        is_enabled(state)
        and getattr(state, "time_window_position_active", False)
        and getattr(state, "time_window_active_mode", None) == "TW2_3SLOT"
    )


def evaluate(
    *,
    entry_chop: bool,
    peak_net_return_pct: float,
    net_return_pct: float,
) -> EarlyTakeProfitDecision:
    """``peak_net_return_pct`` = 진입 후 MFE(틱 관측 최고 순수익률, %),
    ``net_return_pct`` = 판정 대상 **완성봉 종가** 기준 순수익률(%).

    호출자는 production 래더가 아무 청산도 내지 않았을 때만 이 함수를 부른다 —
    즉 여기서 나오는 exit_reason 은 절대 TP1/TP2/trailing/손절을 앞지르지
    않는다."""
    if not entry_chop:
        return EarlyTakeProfitDecision(False, None, 0.0, LABEL_NOT_ENTRY_CHOP)
    armed = float(peak_net_return_pct) >= float(config.EARLY_TP_TRIGGER_PCT)
    if not armed:
        return EarlyTakeProfitDecision(False, None, 0.0, LABEL_NOT_ARMED)
    if float(net_return_pct) <= float(config.EARLY_TP_FLOOR_PCT):
        return EarlyTakeProfitDecision(True, config.EXIT_EARLY_TAKE_PROFIT, 1.0, LABEL_FIRED)
    return EarlyTakeProfitDecision(True, None, 0.0, LABEL_ARMED_HOLD)
