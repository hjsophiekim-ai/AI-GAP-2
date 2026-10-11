"""Trend Regime Hold (C) — 연구 전용 순수 모듈, 2026-09-17. READ-ONLY.

production 에 들어가지 않는다. `app/trading/macd2/small_whipsaw_hold.py` 와 같은
계약(순수 함수, 새 지표식 없음)으로 쓰되, 지표는 전부 production 헬퍼
(`major_flag_filter._ema` / `_prepare_bars`)를 그대로 재사용한다.

상위추세 조건 (LONG = UP_RED 보유 기준, SHORT 는 완전 대칭)
    1) close > EMA50
    2) EMA20 > EMA50
    3) EMA50 slope > 0        (= EMA50[-1] > EMA50[-2], 1봉 기울기)

HOLD 해제 변형
    C1  close 가 EMA50 을 완성봉 **1봉** 이탈
    C2  close 가 EMA50 을 완성봉 **2봉 연속** 이탈
    C3  EMA20/EMA50 구조가 반전
    C4  아래 3조건 중 **2개 이상**
          - close < EMA20
          - EMA20 slope < 0
          - close < EMA50
    C5  EMA50 이탈 전까지 HOLD (= C1 과 같은 해제규칙, 기존 래더 우선은 엔진이 보장)

새 임계값은 하나도 만들지 않는다. EMA 스팬은 H50 이 쓰는 config 값
(H50_TREND_EMA_FAST=20 / H50_TREND_EMA_SLOW=50)을 그대로 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import _ema, _prepare_bars
from app.trading.macd2.models import Direction

RELEASES = ("C1", "C2", "C3", "C4", "C5")
SCOPES = ("R1", "R2")


@dataclass(frozen=True)
class RegimeSnapshot:
    ok: bool                      # 3조건 전부 만족(보유방향 기준)
    close: Optional[float]
    ema_fast: Optional[float]
    ema_slow: Optional[float]
    ema_slow_slope: Optional[float]
    ema_fast_slope: Optional[float]
    insufficient: bool


def _span_fast() -> int:
    return int(config.H50_TREND_EMA_FAST)


def _span_slow() -> int:
    return int(config.H50_TREND_EMA_SLOW)


def snapshot(bars_3m, held_direction) -> RegimeSnapshot:
    """보유방향 기준 상위추세 스냅샷. 판정시점 이전 완성봉만 들어온다."""
    work = _prepare_bars(bars_3m)
    slow = _span_slow()
    if work is None or len(work) < slow + 1:
        return RegimeSnapshot(False, None, None, None, None, None, True)
    close = float(work["close"].iloc[-1])
    ef = _ema(work["close"], _span_fast())
    es = _ema(work["close"], slow)
    ema_fast = float(ef.iloc[-1])
    ema_slow = float(es.iloc[-1])
    slow_slope = float(es.iloc[-1] - es.iloc[-2])
    fast_slope = float(ef.iloc[-1] - ef.iloc[-2])
    if held_direction == Direction.UP_RED:
        ok = (close > ema_slow) and (ema_fast > ema_slow) and (slow_slope > 0)
    else:
        ok = (close < ema_slow) and (ema_fast < ema_slow) and (slow_slope < 0)
    return RegimeSnapshot(bool(ok), close, ema_fast, ema_slow, slow_slope, fast_slope, False)


def should_release(bars_3m, held_direction, variant: str, break_count: int) -> tuple[bool, int, str]:
    """(해제할까, 갱신된 이탈봉 카운트, 사유). 순수 함수."""
    snap = snapshot(bars_3m, held_direction)
    if snap.insufficient:
        return False, break_count, "INSUFFICIENT_BARS"
    long_side = held_direction == Direction.UP_RED

    below_slow = (snap.close < snap.ema_slow) if long_side else (snap.close > snap.ema_slow)
    below_fast = (snap.close < snap.ema_fast) if long_side else (snap.close > snap.ema_fast)
    fast_slope_bad = (snap.ema_fast_slope < 0) if long_side else (snap.ema_fast_slope > 0)
    structure_flipped = (snap.ema_fast < snap.ema_slow) if long_side else (snap.ema_fast > snap.ema_slow)

    count = break_count + 1 if below_slow else 0

    if variant in ("C1", "C5"):
        return (below_slow, count, "EMA50_BREAK" if below_slow else "HOLDING")
    if variant == "C2":
        return (count >= 2, count, "EMA50_BREAK_2BAR" if count >= 2 else "HOLDING")
    if variant == "C3":
        return (structure_flipped, count,
                "EMA_STRUCTURE_FLIP" if structure_flipped else "HOLDING")
    if variant == "C4":
        hits = int(below_fast) + int(fast_slope_bad) + int(below_slow)
        return (hits >= 2, count, f"TWO_OF_THREE({hits})" if hits >= 2 else "HOLDING")
    raise ValueError(f"unknown variant {variant}")


# ── 장 regime 일별 분류 (보고 전용, 전략 판정에는 쓰이지 않는다) ─────────────
MORNING_END = pd.Timestamp("11:30").time()
AFTERNOON_START = pd.Timestamp("12:30").time()


def classify_days(bars_3m, dates) -> dict:
    """Trend / Neutral(chop) / Reversal 일 분류.

    **이 분류는 전략이 읽지 않는다** — 결과를 regime 별로 쪼개 보기 위한
    사후 라벨이다. 그래서 여기 쓰인 0.7 / 4회 같은 숫자는 전략 파라미터가
    아니며 어떤 임계값 최적화에도 참여하지 않는다.
    """
    work = _prepare_bars(bars_3m)
    ef = _ema(work["close"], _span_fast())
    es = _ema(work["close"], _span_slow())
    df = pd.DataFrame({
        "datetime": work["datetime"],
        "up": (ef > es).astype(int),
        "slope_up": (es.diff() > 0).astype(int),
    })
    df["date"] = pd.DatetimeIndex(df["datetime"]).tz_convert(config.KST).strftime("%Y%m%d")
    df["t"] = pd.DatetimeIndex(df["datetime"]).tz_convert(config.KST).time

    out = {}
    for d in dates:
        day = df[df["date"] == d]
        if day.empty:
            out[d] = "UNKNOWN"
            continue
        morning = day[day["t"] < MORNING_END]
        afternoon = day[day["t"] >= AFTERNOON_START]
        if morning.empty or afternoon.empty:
            out[d] = "UNKNOWN"
            continue
        m_up = float(morning["up"].mean())
        a_up = float(afternoon["up"].mean())
        m_dir = "UP" if m_up >= 0.5 else "DOWN"
        a_dir = "UP" if a_up >= 0.5 else "DOWN"
        crossings = int((day["up"].diff().abs() > 0).sum())
        trend_frac = float((morning["up"] & morning["slope_up"]).mean())
        down_frac = float(((1 - morning["up"]) & (1 - morning["slope_up"])).mean())
        if m_dir != a_dir:
            out[d] = "REVERSAL"
        elif max(trend_frac, down_frac) >= 0.7:
            out[d] = "TREND"
        elif crossings >= 4:
            out[d] = "CHOP"
        else:
            out[d] = "NEUTRAL"
    return out
