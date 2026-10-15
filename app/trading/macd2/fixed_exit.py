"""고정 ±1.5% 청산 토글 (2026-10-15, 사용자 요청).

전략모드(N1 / P3 / E / CHOP)는 **진입·사이징·반대 플래그 처리**를 그대로 하고,
이 토글이 켜져 있으면 **청산만** 아래 두 줄로 바뀐다::

    TP  틱 순수익률 >= +1.5%             -> 즉시 전량익절   FIXED_TP15
    SL  완성 3분봉 종가 순수익률 <= -1.5% -> 전량손절       FIXED_SL15

꺼지는 것(토글 ON 동안 호출조차 되지 않는다):
    · 시간대 래더 — TP1 부분익절 / TP2 / 오후TP / 손절 / after-TP1 stop / trailing
      (N1 래더 override 와 SLOW-TP2 포함)
    · 조기익절(ETP)
    · P3 스택 — B3 TP/SL, P3 rescue 부분익절, B3 max-hold, Y3, H30, CHOP 구조손절 유예
    · C1 peak protection

그대로인 것:
    · 15:00 강제청산 (래더보다 앞에서 먼저 판정된다)
    · 반대 플래그 T+3 switch / sell-only, whipsaw-watch, H50 HOLD 와 그 해제
      — 전부 반대 플래그에서 시작되는 처리라 손대지 않는다
    · 계좌/브로커 safety, SAFE EXIT, 사용자 일괄매도

OFF 일 때는 이 모듈의 판정 함수가 **한 번도 호출되지 않는다** — worker 의 분기는
:func:`is_active` 하나뿐이고, 그것이 False 면 기존 코드가 한 줄도 다르지 않게 돈다.
"""
from __future__ import annotations

from typing import Optional

from app.trading.macd2 import config, strategy_mode

TP_PCT = float(config.FIXED_EXIT_TP_PCT)
SL_PCT = float(config.FIXED_EXIT_SL_PCT)
EXIT_TP = config.EXIT_FIXED_TP15
EXIT_SL = config.EXIT_FIXED_SL15


def is_enabled(state) -> bool:
    """사용자 토글 값 그대로 (UI 표시용)."""
    return bool(getattr(state, "fixed_exit_15_enabled", False))


def is_active(state) -> bool:
    """지금 청산을 이 규칙으로 바꾸는가.

    토글이 켜져 있고 **전략모드가 선택돼 있을 때만** True 다. 전략모드가 비어 있는
    state(레거시 TW2 / X2-lite / H50 등을 쓰던 state — strategy_mode.migrate 가
    일부러 비워 둔다)는 이 기능의 대상이 아니므로 기존 청산 그대로 둔다.
    """
    if not is_enabled(state):
        return False
    stored = getattr(state, "strategy_mode", None)
    return bool(stored) and strategy_mode.normalize(stored) in strategy_mode.ALL_MODES


def evaluate_tick(net_return_pct: float) -> Optional[str]:
    """틱(현재가) 순수익률로 익절만 본다. 손절은 틱으로 보지 않는다."""
    return EXIT_TP if float(net_return_pct) >= TP_PCT else None


def evaluate_bar(bar_net_return_pct: float) -> Optional[str]:
    """완성 3분봉 종가 순수익률로 손절만 본다."""
    return EXIT_SL if float(bar_net_return_pct) <= SL_PCT else None


def describe(state) -> str:
    """UI 한 줄 요약."""
    if not is_enabled(state):
        return "OFF (전략 기본 청산)"
    if not is_active(state):
        return "ON (전략모드 미선택 — 적용 안 됨)"
    return f"ON (+{TP_PCT:.1f}% 즉시 전량익절 / 3분봉 종가 {SL_PCT:.1f}% 전량손절)"
