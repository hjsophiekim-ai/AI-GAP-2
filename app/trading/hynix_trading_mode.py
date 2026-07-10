"""hynix_trading_mode.py — 거래 모드(SAFE/BALANCED/ACTIVE/AGGRESSIVE) 설정.

모드별로 신규진입 확률 임계값, 조기 시험진입 비중 사다리, 최대 총투자비중,
HOLD 완화 하한, 하루 최대 왕복거래 횟수를 정의한다. 기본값은 ACTIVE.

이 모듈은 순수 설정/계산 함수만 담고 있다 — 실제 주문 실행과는 무관하다
(app/trading/hynix_active_strategy_engine.py가 이 설정을 사용해 주문을 결정한다).
"""

from __future__ import annotations

from typing import Optional

MODE_SAFE = "SAFE"
MODE_BALANCED = "BALANCED"
MODE_ACTIVE = "ACTIVE"
MODE_AGGRESSIVE = "AGGRESSIVE"

DEFAULT_MODE = MODE_ACTIVE
ALL_MODES = (MODE_SAFE, MODE_BALANCED, MODE_ACTIVE, MODE_AGGRESSIVE)

# 섹션 1 — 모드별 초기 진입 확률 임계값
_INITIAL_THRESHOLD = {MODE_SAFE: 70.0, MODE_BALANCED: 64.0, MODE_ACTIVE: 58.0, MODE_AGGRESSIVE: 54.0}

# 섹션 5 — HOLD 완화 최저 임계값(이 아래로는 절대 낮추지 않음)
_MIN_THRESHOLD_FLOOR = {MODE_SAFE: 65.0, MODE_BALANCED: 60.0, MODE_ACTIVE: 55.0, MODE_AGGRESSIVE: 52.0}

# 섹션 9 — 하루 최대 왕복거래
_MAX_ROUND_TRIPS = {MODE_SAFE: 2, MODE_BALANCED: 3, MODE_ACTIVE: 4, MODE_AGGRESSIVE: 5}

# 섹션 2 — 조기 시험진입 비중 사다리: (확률 하한, 확률 상한, 비중%). ACTIVE/AGGRESSIVE만
# 54~59% 구간 시험진입을 허용한다(SAFE/BALANCED는 그 구간에서 진입하지 않음).
_EARLY_ENTRY_LADDER_ACTIVE_ONLY = (54.0, 60.0, 15.0)
_EARLY_ENTRY_LADDER = (
    (60.0, 65.0, 25.0),
    (65.0, 73.0, 40.0),
    (73.0, 83.0, 60.0),
    (83.0, 200.0, 80.0),
)
MAX_TOTAL_POSITION_PCT = 80.0
MAX_TOTAL_POSITION_PCT_STRETCH = 90.0  # 확률>=90 & confidence>=85일 때만

# 섹션 5 — HOLD 5연속/8연속 완화 폭(포인트)
HOLD_RELIEF_AT_5 = 3.0
HOLD_RELIEF_AT_8 = 5.0  # 누적(3+2)
MIN_EXPECTED_MOVE_FOR_RELIEF_PCT = 0.15


def mode_initial_threshold(mode: str) -> float:
    return _INITIAL_THRESHOLD.get(mode, _INITIAL_THRESHOLD[DEFAULT_MODE])


def mode_min_threshold_floor(mode: str) -> float:
    return _MIN_THRESHOLD_FLOOR.get(mode, _MIN_THRESHOLD_FLOOR[DEFAULT_MODE])


def mode_max_round_trips(mode: str) -> int:
    return _MAX_ROUND_TRIPS.get(mode, _MAX_ROUND_TRIPS[DEFAULT_MODE])


def calculate_entry_position_pct(probability: float, mode: str) -> float:
    """섹션 2 — 확률 구간별 조기 시험진입 비중(%). 매칭 구간이 없으면 0.0."""
    if mode in (MODE_ACTIVE, MODE_AGGRESSIVE) and _EARLY_ENTRY_LADDER_ACTIVE_ONLY[0] <= probability < _EARLY_ENTRY_LADDER_ACTIVE_ONLY[1]:
        return _EARLY_ENTRY_LADDER_ACTIVE_ONLY[2]
    for low, high, pct in _EARLY_ENTRY_LADDER:
        if low <= probability < high:
            return pct
    return 0.0


def calculate_scale_up_target_pct(probability: float) -> float:
    """섹션 3 — 시험진입 후 확률 상승에 따른 목표 총비중(%)."""
    if probability >= 90.0:
        return 90.0
    if probability >= 80.0:
        return 80.0
    if probability >= 72.0:
        return 60.0
    if probability >= 65.0:
        return 40.0
    return 0.0


def max_total_position_pct(probability: float, confidence: float) -> float:
    if probability >= 90.0 and confidence >= 85.0:
        return MAX_TOTAL_POSITION_PCT_STRETCH
    return MAX_TOTAL_POSITION_PCT


def daily_pnl_position_scale(daily_return_pct: Optional[float]) -> dict:
    """섹션 10 — 일 실현수익률/손실률에 따른 포지션 상한·신규진입 정책.

    Returns dict: {max_position_pct, threshold_add, entries_allowed, force_liquidate}
    """
    if daily_return_pct is None:
        return {"max_position_pct": MAX_TOTAL_POSITION_PCT, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}

    if daily_return_pct <= -2.5:
        return {"max_position_pct": 0.0, "threshold_add": 0.0, "entries_allowed": False, "force_liquidate": True}
    if daily_return_pct <= -1.8:
        return {"max_position_pct": 40.0, "threshold_add": 0.0, "entries_allowed": False, "force_liquidate": False}
    if daily_return_pct <= -1.2:
        return {"max_position_pct": 40.0, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}
    if daily_return_pct <= -0.8:
        return {"max_position_pct": 70.0, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}
    if daily_return_pct >= 3.0:
        return {"max_position_pct": MAX_TOTAL_POSITION_PCT, "threshold_add": 0.0, "entries_allowed": False, "force_liquidate": False}
    if daily_return_pct >= 2.0:
        return {"max_position_pct": 50.0, "threshold_add": 3.0, "entries_allowed": True, "force_liquidate": False}
    if daily_return_pct >= 1.0:
        return {"max_position_pct": 70.0, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}
    return {"max_position_pct": MAX_TOTAL_POSITION_PCT, "threshold_add": 0.0, "entries_allowed": True, "force_liquidate": False}
