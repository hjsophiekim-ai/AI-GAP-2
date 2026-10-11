"""V4 Adaptive Auto-Select — 연구 전용, 2026-09-17. READ-ONLY.

진입 확정 시점(= flat 상태에서 새 포지션을 여는 순간)의 **그 시점까지의 완성봉만**
보고 V1/V2/V3 중 하나를 고른다. 고른 전략은 그 포지션이 닫힐 때까지 고정된다
(장중 전환 금지, 사용자 조건).

  V1 = X2-lite + W1a          (H50 없음)
  V2 = H50                     (X2-lite + W1a + 작은 휩쏘 HOLD)
  V3 = H50 + D3a               (추세일 때 TP2 5.0 -> 6.0)

새 threshold 를 만들지 않는 것이 이 모듈의 설계 제약이다. 쓰는 값은 전부
production config 이거나 이미 production 에서 쓰이는 판정기다:

  CHOP 판정      `early_take_profit.evaluate_entry_chop(...).is_chop`
                 (W1a 사이징과 Slot1 CHOP veto 가 이미 쓰는 바로 그 함수)
  rng60 임계     `config.H50_RANGE_MAX_PCT`        = 2.35
  rng 창         `config.H50_RANGE_BARS`           = 20봉(60분)
  EMA 스팬       `config.H50_TREND_EMA_FAST/SLOW`  = 20 / 50
  "유지" 봉 수   `config.H50_TREND_BREAK_BARS`     = 2

판정 순서 (사용자 명세 그대로, 위에서부터)
  1) CHOP / 빠른 방향전환  -> V1
  2) 강한 추세 지속        -> V3
  3) 안정 추세             -> V2
  4) 그 외                 -> 기본값 (V4a=V1 / V4b=V2)

2)를 3)보다 먼저 보는 이유: 3)은 2)의 상위집합(2 조건 + close 유지)이라
순서를 바꾸면 3)에 절대 도달하지 못한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.trading.macd2 import config
from app.trading.macd2 import early_take_profit as etp
from app.trading.macd2.major_flag_filter import _ema, _prepare_bars
from app.trading.macd2.models import Direction

MODES = ("V1", "V2", "V3")


@dataclass(frozen=True)
class Pick:
    mode: str
    reason: str
    is_chop: bool
    aligned_bars: int          # 정렬+slope 가 연속 유지된 봉 수(최대 확인분)
    close_side_bars: int       # close 가 EMA50 같은 쪽에 있은 연속 봉 수
    range_pct: Optional[float]


def _range_pct(work) -> Optional[float]:
    """H50 과 **같은 계산** — 최근 H50_RANGE_BARS 봉 high-low 를 현재가 대비 %."""
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


def pick_mode(bars_3m, direction, now=None, *, default: str = "V2") -> Pick:
    """진입 확정 시점의 regime 판정. 순수 함수, 미래정보 없음."""
    work = _prepare_bars(bars_3m)
    slow = int(config.H50_TREND_EMA_SLOW)
    if work is None or len(work) < slow + int(config.H50_RANGE_BARS):
        return Pick(default, "INSUFFICIENT_BARS", False, 0, 0, None)

    # 1) CHOP -- production 판정기 그대로 재사용
    cd = etp.evaluate_entry_chop(work, direction, now)
    is_chop = bool(cd.is_chop) and not cd.insufficient_data
    if is_chop:
        return Pick("V1", "CHOP", True, 0, 0, _range_pct(work))

    ef = _ema(work["close"], int(config.H50_TREND_EMA_FAST))
    es = _ema(work["close"], slow)
    close = work["close"].astype(float)
    long_side = direction == Direction.UP_RED

    # 정렬 + slope 가 연속으로 유지된 봉 수
    keep = int(config.H50_TREND_BREAK_BARS)
    aligned = 0
    for k in range(1, keep + 1):
        i = -k
        al = (ef.iloc[i] > es.iloc[i]) if long_side else (ef.iloc[i] < es.iloc[i])
        sl = (es.iloc[i] - es.iloc[i - 1])
        sl_ok = (sl > 0) if long_side else (sl < 0)
        if al and sl_ok:
            aligned += 1
        else:
            break

    # close 가 EMA50 같은 쪽에 있은 연속 봉 수 (최대 H50_RANGE_BARS 까지만 확인)
    span = int(config.H50_RANGE_BARS)
    side = 0
    for k in range(1, span + 1):
        i = -k
        ok = (close.iloc[i] > es.iloc[i]) if long_side else (close.iloc[i] < es.iloc[i])
        if ok:
            side += 1
        else:
            break

    rng = _range_pct(work)
    rng_ok = rng is not None and rng <= float(config.H50_RANGE_MAX_PCT)
    trend_ok = aligned >= keep

    # 2) 강한 추세 지속 -- 정렬/slope 유지 + close 가 EMA50 한쪽을 계속 지킴
    if trend_ok and side >= span:
        return Pick("V3", "STRONG_TREND", False, aligned, side, rng)
    # 3) 안정 추세 -- 정렬/slope 유지 + rng60 이 H50 조건 안
    if trend_ok and rng_ok:
        return Pick("V2", "STABLE_TREND", False, aligned, side, rng)
    return Pick(default, "DEFAULT", False, aligned, side, rng)
