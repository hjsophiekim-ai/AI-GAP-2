"""N1 adaptive 청산 래더 — N1 전용 독립 판정 모듈 (2026-09-20).

이 모듈이 절대 하지 않는 것
---------------------------
진입 판정을 하지 않는다. MACD 플래그 생성, T+3 confirmation, 슬롯 배분,
quality gate, TEG, CHOP 판정, W1a sizing, 주문 수량 계산은 이 파일에서
import 조차 하지 않는 영역이다. **N1 adaptive 는 진입집합을 바꾸지 않는다.**

손절 / after-TP1 스탑 / trailing trigger / trailing stop / 오후 래더 /
강제청산 / 조기익절도 건드리지 않는다 — 그 값들은
``time_window_3slot.exit_overrides(MODE_N1_3SLOT)`` 가 한 번만 돌려주는
**고정값**이다.

이 모듈이 하는 것 — 단 하나
---------------------------
**상위추세 여부**를 보고 오전 래더 3개 값을 고른다.

    추세 ok   : TP1 3.5 / TP1 매도비중 0.0 / TP2 8.0
    추세 아님 : TP1 3.0 / TP1 매도비중 0.2 / TP2 4.0   (= X2-lite 기본값 + off TP2)

상위추세 판정 (보유방향 기준, 연구사양 `tregime.snapshot` 과 동치)

    LONG(UP_RED)     : close > EMA50  AND  EMA20 > EMA50  AND  EMA50 기울기 > 0
    SHORT(DOWN_BLUE) : close < EMA50  AND  EMA20 < EMA50  AND  EMA50 기울기 < 0

    EMA span  = config.H50_TREND_EMA_FAST(20) / H50_TREND_EMA_SLOW(50)
                — **H50 과 같은 config 상수**. 새 span 을 만들지 않는다.
    기울기    = EMA50[-1] - EMA50[-2]  (완성봉 1봉)
    프레임    = 하이닉스 3분봉, 판정시점 이전 **완성봉만**
    봉 부족   = 완성봉이 slow+1(51) 개 미만이면 insufficient -> ok=False
                (= 비추세로 취급. 연구엔진과 같은 fallback)

지표는 전부 production 헬퍼를 그대로 재사용한다
(``major_flag_filter._ema`` / ``_prepare_bars``) — **새 EMA 계산식 없음**.

왜 "봉마다" 인가
----------------
연구엔진은 매 완성봉에서 이 판정을 다시 하고, 같은 봉의 틱 판단에도 그 봉의
판정을 재사용한다(`trend_ok_at` 캐시, 키 = (봉 인덱스, 방향)). worker 도 같은
계약을 따른다 — 완성봉이 바뀔 때만 재판정하고, 그 사이 틱은 마지막 판정을 쓴다.

순수 함수만 있다(``risk_exit.py`` / ``small_whipsaw_hold.py`` 와 같은 계약):
네트워크/브로커 접근 없음. ``note_*`` 만 state 를 갱신한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2 import time_window_3slot
from app.trading.macd2.major_flag_filter import _ema, _prepare_bars
from app.trading.macd2.models import Direction

REGIME_TREND = "TREND"
REGIME_OFF_TREND = "OFF_TREND"
REGIME_INSUFFICIENT = "INSUFFICIENT_BARS"


@dataclass(frozen=True)
class RegimeSnapshot:
    """보유방향 기준 상위추세 스냅샷. 판정시점 이전 완성봉만 들어온다."""

    ok: bool
    close: Optional[float]
    ema_fast: Optional[float]
    ema_slow: Optional[float]
    ema_slow_slope: Optional[float]
    insufficient: bool
    reason: str


@dataclass(frozen=True)
class LadderDecision:
    """이 봉에서 오전 래더에 넘길 3개 값 + 진단."""

    tp1_pct: float
    tp1_sell_ratio: float
    tp2_pct: float
    regime_ok: bool
    regime_reason: str


def is_active(state) -> bool:
    """N1 adaptive 가 이 tick 에서 실제로 적용되는가.

    N1 모드일 때만 True. 다른 모든 전략(X2-lite / H50 / TW TEG 3-SLOT /
    TW2 3-SLOT / TW2 / TEGv2 / 무필터 / MU_MACD)에서는 False 이므로 동작이
    조금도 바뀌지 않는다."""
    if not bool(getattr(config, "N1_ENABLED", True)):
        return False
    return (time_window_3slot.active_3slot_mode(state)
            in time_window_3slot.MODES_N1_FAMILY)


def _span_fast() -> int:
    return int(config.H50_TREND_EMA_FAST)


def _span_slow() -> int:
    return int(config.H50_TREND_EMA_SLOW)


def snapshot(bars_3m: Optional[pd.DataFrame],
             held_direction: Optional[Direction]) -> RegimeSnapshot:
    """상위추세 스냅샷. 연구사양 `tregime.snapshot` 과 값이 동치다."""
    work = _prepare_bars(bars_3m)
    slow = _span_slow()
    if work is None or len(work) < slow + 1 or held_direction is None:
        return RegimeSnapshot(False, None, None, None, None, True, REGIME_INSUFFICIENT)
    close = float(work["close"].iloc[-1])
    ef = _ema(work["close"], _span_fast())
    es = _ema(work["close"], slow)
    ema_fast = float(ef.iloc[-1])
    ema_slow = float(es.iloc[-1])
    slow_slope = float(es.iloc[-1] - es.iloc[-2])
    if held_direction == Direction.UP_RED:
        ok = (close > ema_slow) and (ema_fast > ema_slow) and (slow_slope > 0)
    elif held_direction == Direction.DOWN_BLUE:
        ok = (close < ema_slow) and (ema_fast < ema_slow) and (slow_slope < 0)
    else:
        ok = False
    return RegimeSnapshot(
        bool(ok), close, ema_fast, ema_slow, slow_slope, False,
        REGIME_TREND if ok else REGIME_OFF_TREND,
    )


def trend_ladder() -> tuple[float, float, float]:
    """(TP1%, TP1 매도비중, TP2%) — 추세구간."""
    return (float(config.N1_TREND_TP1) * 100.0,
            float(config.N1_TREND_TP1_SELL_RATIO),
            float(config.N1_TREND_TP2) * 100.0)


def off_trend_ladder() -> tuple[float, float, float]:
    """(TP1%, TP1 매도비중, TP2%) — 비추세구간.

    TP1 과 매도비중은 X2-lite 기본값으로 **되돌아간다**(연구 `eff_cfg` 가
    ``off_`` 접두 키만 적용하므로 ``tp1``/``tp1_ratio`` 는 빠진다). TP2 만
    N1 전용 4.0 이다.
    """
    return (float(config.MORNING_TP1) * 100.0,
            float(config.X2LITE_MORNING_TP1_SELL_RATIO),
            float(config.N1_OFF_TREND_TP2) * 100.0)


def resolve_ladder(bars_3m: Optional[pd.DataFrame],
                   held_direction: Optional[Direction]) -> LadderDecision:
    """이 완성봉에서 오전 래더에 넘길 값을 고른다."""
    snap = snapshot(bars_3m, held_direction)
    tp1, ratio, tp2 = trend_ladder() if snap.ok else off_trend_ladder()
    return LadderDecision(tp1, ratio, tp2, snap.ok, snap.reason)


def thresholds_etp() -> tuple[float, float]:
    """N1 의 조기익절 (trigger%, floor%). X2-lite(1.5/1.0)와 다르다."""
    return (float(config.N1_EARLY_TP_TRIGGER_PCT),
            float(config.N1_EARLY_TP_FLOOR_PCT))


# ── state 헬퍼 (독립 namespace) ───────────────────────────────────────────
def note_eval(state, bar_ts, decision: LadderDecision) -> None:
    """이 완성봉의 판정을 state 에 캐시한다. 같은 봉에서는 재계산하지 않고,
    그 사이 틱 판단도 이 값을 쓴다(연구엔진 trend_ok_at 캐시와 같은 계약)."""
    state.n1_last_eval_bar_ts = (
        bar_ts.isoformat() if hasattr(bar_ts, "isoformat") else (bar_ts or None)
    )
    state.n1_regime_state = decision.regime_reason
    state.n1_effective_tp2 = float(decision.tp2_pct)
    state.n1_effective_tp1 = float(decision.tp1_pct)
    state.n1_effective_tp1_ratio = float(decision.tp1_sell_ratio)


def cached_ladder(state) -> Optional[LadderDecision]:
    """state 에 캐시된 판정. 아직 없으면 None."""
    if not getattr(state, "n1_last_eval_bar_ts", None):
        return None
    tp2 = getattr(state, "n1_effective_tp2", None)
    if tp2 is None:
        return None
    return LadderDecision(
        float(getattr(state, "n1_effective_tp1", 0.0) or 0.0),
        float(getattr(state, "n1_effective_tp1_ratio", 0.0) or 0.0),
        float(tp2),
        str(getattr(state, "n1_regime_state", "") or "") == REGIME_TREND,
        str(getattr(state, "n1_regime_state", "") or REGIME_INSUFFICIENT),
    )


def clear(state) -> None:
    """포지션 종료 / 일자변경 / 재시작 정리 공통 경로.

    토글(``time_window_n1_filter_enabled``)은 건드리지 않는다 — 그것은 전략
    선택이고, 이 함수는 **보유기간 캐시**만 되돌린다."""
    state.n1_last_eval_bar_ts = None
    state.n1_regime_state = None
    state.n1_effective_tp2 = None
    state.n1_effective_tp1 = None
    state.n1_effective_tp1_ratio = None
