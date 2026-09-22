"""SMART sizing — P2 슬롯 배분 + toxic confirmation 감액 (2026-09-22).

이 모듈이 절대 하지 않는 것
---------------------------
진입/청산 판정을 하지 않는다. 플래그 생성, T+3 confirmation, TW/TEG, 3-SLOT
슬롯 배분, quality gate, CHOP 판정, TP/SL/ETP/trailing/반대신호/강제청산,
브로커/주문 의미는 이 파일이 import 조차 하지 않는 영역이다. 여기 있는 함수는
**이미 승인된 진입의 주문수량 배수**만 답한다. 거래를 추가하거나 삭제할 수 있는
반환값이 존재하지 않는다.

SMART 정의 (research_20260922d / w2_full.py, 전략 E "TOXIC-OVERRIDE")
--------------------------------------------------------------------
    if toxic:  multiplier = TOXIC_MULT (0.25)          <- 슬롯과 무관한 override
    else:      multiplier = P2_multiplier(slot, session)

**곱하지 않는다.** 오전 slot3 toxic 에서 0.25 x 0.25 = 0.0625 같은 이중감액이
생기지 않도록 override 로 고정했다. 78일 연구에서 오전 slot3 toxic 은 0건이었지만
(quality gate 4점을 통과한 진입이라 구조적으로 드물다) 언젠가 나올 수 있고,
그때 0.0625 는 방어 불가능한 배수다.

toxic 정의
----------
    confirmation_weak  AND  ema20_50_directional_pct < TOXIC_EMA20_50_MAX_PCT

  * confirmation_weak = 플래그봉 시작 ~ 진입 직전 구간에서 **보유할 ETF** 의
    수익률 <= 0%  ("확인 구간 동안 ETF 가 신호 방향으로 전혀 안 따라왔다")
  * ema20_50_directional_pct = 하이닉스 EMA20-EMA50 을 보유 방향 기준으로
    부호 정규화하고 종가로 나눈 값(%)  ("역추세 진입 정도")

두 임계(0% / -0.20)는 연구값 그대로다. 새 임계를 만들지 않는다.

미래정보
--------
두 입력 모두 **진입 승인 직전까지**의 데이터만 쓴다.
  * ema 는 호출부가 넘기는 `bars_3m` 으로 계산하는데, worker 는 이미
    `filter_complete_3m_bars` 를 통과한 **완성봉만** 넘긴다.
  * confirmation 수익률은 `end_before`(= 진입 시각) **미만** 샘플만 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

import pandas as pd

from app.trading.macd2 import config


@dataclass(frozen=True)
class ToxicAssessment:
    """toxic 판정 결과 + 로그에 그대로 실을 진단값."""
    toxic: bool
    confirmation_return_pct: Optional[float]
    confirmation_weak: Optional[bool]
    ema20_50_directional_pct: Optional[float]
    reason: str
    samples_used: int = 0


UNKNOWN = ToxicAssessment(
    toxic=False, confirmation_return_pct=None, confirmation_weak=None,
    ema20_50_directional_pct=None, reason="NO_DATA", samples_used=0,
)


# ── 하이닉스 EMA20-50 방향정규화 ────────────────────────────────────────
def ema20_50_directional_pct(bars_3m, direction) -> Optional[float]:
    """(EMA_fast - EMA_slow) / close * 100, 보유방향 기준 부호.

    EMA 상수는 새로 만들지 않는다 — H50 상위추세 판정과 **같은**
    ``H50_TREND_EMA_FAST/SLOW`` (20/50) 를 쓴다. 음수일수록 "보유 방향과
    반대로 추세가 벌어져 있다"(= 역추세 진입).

    ``bars_3m`` 은 호출부가 넘기는 완성봉 프레임 그대로다 — 이 함수는 절대
    마지막 봉을 잘라내거나 더하지 않는다(미래정보 없음).
    """
    if bars_3m is None or len(bars_3m) == 0:
        return None
    if "close" not in getattr(bars_3m, "columns", []):
        return None
    closes = pd.to_numeric(bars_3m["close"], errors="coerce").dropna()
    slow = int(config.H50_TREND_EMA_SLOW)
    if len(closes) < slow:
        return None
    fast_ema = closes.ewm(span=int(config.H50_TREND_EMA_FAST), adjust=False).mean()
    slow_ema = closes.ewm(span=slow, adjust=False).mean()
    last_close = float(closes.iloc[-1])
    if last_close <= 0:
        return None
    gap = float(fast_ema.iloc[-1]) - float(slow_ema.iloc[-1])
    sign = 1.0 if _is_up(direction) else -1.0
    return sign * gap / last_close * 100.0


def _is_up(direction) -> bool:
    value = getattr(direction, "value", direction)
    return str(value).upper().endswith("UP_RED") or str(value).upper() == "UP_RED"


# ── confirmation 구간 ETF 수익률 ────────────────────────────────────────
def confirmation_etf_return_pct(
    samples: Sequence[tuple[Any, float]], *,
    start_at: datetime, end_before: datetime,
) -> tuple[Optional[float], int]:
    """``[start_at, end_before)`` 구간 ETF 수익률 % 와 사용 샘플 수.

    samples 는 ``(timestamp, price)`` 의 시간순 리스트다(worker 가 매 tick
    적재한 ETF 호가 trail). 시작가는 구간 내 **첫** 샘플, 종가는 **마지막**
    샘플이다. 최소 ``TOXIC_MIN_CONFIRM_SAMPLES`` 개가 없으면 판정 불가로
    ``(None, n)`` 을 돌려준다 — 판정 불가는 toxic 이 **아니다**(fail-open).
    """
    picked: list[float] = []
    for raw_ts, price in samples or ():
        ts = _parse_ts(raw_ts)
        if ts is None:
            continue
        try:
            px = float(price)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        if ts < start_at or ts >= end_before:
            continue
        picked.append(px)
    if len(picked) < int(config.TOXIC_MIN_CONFIRM_SAMPLES):
        return None, len(picked)
    first, last = picked[0], picked[-1]
    if first <= 0:
        return None, len(picked)
    return (last - first) / first * 100.0, len(picked)


def _parse_ts(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


# ── toxic 판정 ──────────────────────────────────────────────────────────
def assess(
    *, bars_3m, direction,
    samples: Sequence[tuple[Any, float]],
    confirm_start_at: Optional[datetime],
    entry_at: Optional[datetime],
) -> ToxicAssessment:
    """toxic 판정. 입력이 모자라면 **toxic=False**(감액하지 않음)로 fail-open."""
    ema = ema20_50_directional_pct(bars_3m, direction)
    if confirm_start_at is None or entry_at is None:
        return ToxicAssessment(False, None, None, ema, "NO_WINDOW", 0)
    ret, used = confirmation_etf_return_pct(
        samples, start_at=confirm_start_at, end_before=entry_at)
    if ret is None:
        return ToxicAssessment(False, None, None, ema, "NO_CONFIRM_SAMPLES", used)
    weak = bool(ret <= float(config.TOXIC_CONFIRM_RETURN_MAX_PCT))
    if ema is None:
        return ToxicAssessment(False, ret, weak, None, "NO_EMA", used)
    toxic = bool(weak and ema < float(config.TOXIC_EMA20_50_MAX_PCT))
    return ToxicAssessment(
        toxic=toxic, confirmation_return_pct=ret, confirmation_weak=weak,
        ema20_50_directional_pct=ema,
        reason="TOXIC" if toxic else ("WEAK_ONLY" if weak else "NORMAL"),
        samples_used=used,
    )


def smart_multiplier(p2_mult: float, *, toxic: bool) -> float:
    """SMART 최종 배수. toxic 이면 슬롯과 무관하게 override (곱하지 않는다)."""
    if toxic:
        return float(config.SMART_TOXIC_MULT)
    return float(p2_mult)


# ── ETF 호가 trail (worker 가 매 tick 적재) ─────────────────────────────
def append_quote_sample(state, symbol: str, price: float, at: datetime) -> None:
    """ETF 호가를 state 의 trail 에 적재. 최근 ``TOXIC_QUOTE_TRAIL_MAX`` 개만 유지.

    confirmation 구간(플래그봉 시작 ~ 진입, 약 6분)을 덮으려면 5초 tick 기준
    ~72 개면 충분하다. 넉넉히 잡아도 state 크기 영향이 미미하다.
    """
    try:
        px = float(price)
    except (TypeError, ValueError):
        return
    if px <= 0 or at is None or at.tzinfo is None:
        return
    trail = dict(getattr(state, "etf_quote_trail", None) or {})
    items = list(trail.get(symbol) or ())
    stamp = at.isoformat()
    if items and items[-1][0] == stamp:
        items[-1] = [stamp, px]
    else:
        items.append([stamp, px])
    limit = int(config.TOXIC_QUOTE_TRAIL_MAX)
    if len(items) > limit:
        items = items[-limit:]
    trail[symbol] = items
    state.etf_quote_trail = trail


def trail_for(state, symbol: str) -> list[tuple[Any, float]]:
    trail = getattr(state, "etf_quote_trail", None) or {}
    return [(row[0], row[1]) for row in (trail.get(symbol) or ()) if len(row) >= 2]


def clear_trail(state) -> None:
    state.etf_quote_trail = {}
