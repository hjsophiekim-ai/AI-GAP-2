"""hynix_big_trend_engine.py — "장중 큰 추세 추종 + 수익 보호 + 반전 확인 청산" 엔진.

기존 DynamicExitEngine의 고정 tp_pct(예: NORMAL 3% 전량익절)는 강한 추세장에서
이익을 조기에 끊어버리는 문제가 있었다(2026-07-13 사용자 관찰 — 강한 하락장에서도
인버스를 짧게 사고팔아 큰 흐름을 놓침). 이 엔진은 시장을 7가지 Trend Regime으로
분류하고, regime별로 다른 부분익절/트레일링/손절 정책을 적용한다. 작은 반대 신호
하나만으로는 청산하지 않고(Hysteresis), 실제 추세 반전은 9개 조건 중 최소 3개
이상이 동시에 확인되어야 인정한다.

이 모듈 자체는 주문을 실행하지 않는다 — decide_trend_hold_action()의 반환값은
"권장" 행동이며, 실제 브로커 호출은 호출부(app/trading/dynamic_exit_watcher.py)가
mock 모드에서만, 명시적 opt-in 토글(state["big_trend_holding_enabled"])이 켜졌을
때만 수행한다. 초기 손절 안전장치(effective_sl_pct)는 이 엔진이 켜져 있어도 항상
동작한다 — 절대 생략되지 않는다.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.utils.data_paths import LOGS_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_PATH = LOGS_DIR / "hynix_big_trend_log.csv"

# =============================================================================
# 상수
# =============================================================================

DIRECTION_HYNIX = "HYNIX"
DIRECTION_INVERSE = "INVERSE"
DIRECTION_NEUTRAL = "NEUTRAL"

REGIME_STRONG_TREND = "STRONG_TREND"
REGIME_NORMAL_TREND = "NORMAL_TREND"
REGIME_REVERSAL_RISK = "REVERSAL_RISK"
REGIME_RANGE = "RANGE"
REGIME_WHIPSAW = "WHIPSAW"
REGIME_PANIC = "PANIC"
REGIME_RECOVERY = "RECOVERY"

ACTION_HOLD_FULL = "HOLD_FULL"
ACTION_HOLD_REDUCED = "HOLD_REDUCED"
ACTION_TAKE_PROFIT_25 = "TAKE_PROFIT_25"
ACTION_TAKE_PROFIT_50 = "TAKE_PROFIT_50"
ACTION_EXIT_ALL = "EXIT_ALL"
ACTION_SWITCH_TO_HYNIX = "SWITCH_TO_HYNIX"
ACTION_SWITCH_TO_INVERSE = "SWITCH_TO_INVERSE"

LOG_COLUMNS = [
    "timestamp", "symbol", "entry_price", "current_price", "net_return_pct", "peak_net_return_pct",
    "profit_giveback_pct", "dominant_direction", "trend_regime", "trend_strength_score",
    "trend_persistence_score", "reversal_probability_3m", "reversal_probability_5m",
    "reversal_probability_15m", "hold_confidence", "exit_confidence", "profit_lock_floor_pct",
    "trailing_pct", "position_pct", "recommended_action", "executed_action",
    "reason_top1", "reason_top2", "reason_top3",
]


# =============================================================================
# 섹션 2 — 큰 흐름 판단(방향/강도)
# =============================================================================

def _bearish_condition_flags(f: dict) -> dict:
    """강한 하락(INVERSE 우세) 조건 8개. 데이터가 없는 조건은 딕셔너리에서 제외한다
    (해당 없음으로 카운트하지 않음 — 없는 데이터를 있다고 가정하지 않는다)."""
    flags = {}
    if f.get("minutes_below_vwap") is not None:
        flags["vwap_below_10min"] = f["minutes_below_vwap"] >= 10
    if f.get("ema5") is not None and f.get("ema10") is not None and f.get("ema20") is not None:
        flags["ema_bearish_stack"] = f["ema5"] < f["ema10"] < f["ema20"]
    if f.get("lower_high_count_last3") is not None:
        flags["lower_high_2of3"] = f["lower_high_count_last3"] >= 2
    if f.get("lower_low_count_last3") is not None:
        flags["lower_low_2of3"] = f["lower_low_count_last3"] >= 2
    if f.get("macd_histogram") is not None and f.get("macd_histogram_prev") is not None:
        flags["macd_negative_worsening"] = f["macd_histogram"] < 0 and f["macd_histogram"] < f["macd_histogram_prev"]
    if f.get("rsi_14") is not None:
        flags["rsi_below_50"] = f["rsi_14"] < 50
    if f.get("down_volume_increasing") is not None:
        flags["down_volume_rising"] = bool(f["down_volume_increasing"])
    if f.get("inverse_probability") is not None:
        flags["inverse_probability_high"] = f["inverse_probability"] >= 65
    return flags


def _bullish_condition_flags(f: dict) -> dict:
    """강한 상승(HYNIX 우세) 조건 — 하락 조건을 대칭적으로 뒤집는다."""
    flags = {}
    if f.get("minutes_above_vwap") is not None:
        flags["vwap_above_10min"] = f["minutes_above_vwap"] >= 10
    if f.get("ema5") is not None and f.get("ema10") is not None and f.get("ema20") is not None:
        flags["ema_bullish_stack"] = f["ema5"] > f["ema10"] > f["ema20"]
    if f.get("higher_high_count_last3") is not None:
        flags["higher_high_2of3"] = f["higher_high_count_last3"] >= 2
    if f.get("higher_low_count_last3") is not None:
        flags["higher_low_2of3"] = f["higher_low_count_last3"] >= 2
    if f.get("macd_histogram") is not None and f.get("macd_histogram_prev") is not None:
        flags["macd_positive_improving"] = f["macd_histogram"] > 0 and f["macd_histogram"] > f["macd_histogram_prev"]
    if f.get("rsi_14") is not None:
        flags["rsi_above_50"] = f["rsi_14"] > 50
    if f.get("up_volume_increasing") is not None:
        flags["up_volume_rising"] = bool(f["up_volume_increasing"])
    if f.get("hynix_probability") is not None:
        flags["hynix_probability_high"] = f["hynix_probability"] >= 65
    return flags


def compute_trend_strength_score(features: dict) -> dict:
    """8개 조건 중 충족 비율(조건점수) + 확률(Prediction AI V2)을 6:4로 블렌딩해
    0~100 trend_strength_score와 dominant_direction을 계산한다."""
    bearish = _bearish_condition_flags(features)
    bullish = _bullish_condition_flags(features)
    bearish_matched = sum(1 for v in bearish.values() if v)
    bullish_matched = sum(1 for v in bullish.values() if v)
    bearish_total = max(1, len(bearish))
    bullish_total = max(1, len(bullish))

    bearish_condition_score = bearish_matched / bearish_total * 100.0
    bullish_condition_score = bullish_matched / bullish_total * 100.0

    inverse_p = features.get("inverse_probability", 50.0) or 50.0
    hynix_p = features.get("hynix_probability", 50.0) or 50.0

    bearish_score = round(0.6 * bearish_condition_score + 0.4 * inverse_p, 2)
    bullish_score = round(0.6 * bullish_condition_score + 0.4 * hynix_p, 2)

    if bearish_score >= bullish_score and bearish_score >= 55.0:
        direction = DIRECTION_INVERSE
        strength = bearish_score
    elif bullish_score > bearish_score and bullish_score >= 55.0:
        direction = DIRECTION_HYNIX
        strength = bullish_score
    else:
        direction = DIRECTION_NEUTRAL
        strength = max(bearish_score, bullish_score)

    return {
        "dominant_direction": direction, "trend_strength_score": round(min(100.0, strength), 2),
        "bearish_score": bearish_score, "bullish_score": bullish_score,
        "bearish_matched": bearish_matched, "bearish_total": bearish_total,
        "bullish_matched": bullish_matched, "bullish_total": bullish_total,
    }


def compute_trend_persistence_score(features: dict, direction: str) -> float:
    """추세가 얼마나 "오래/일관되게" 유지되고 있는지(0~100). VWAP 체류시간 +
    EMA 배열 지속 + HH/HL 또는 LH/LL 구조 일관성을 종합한다."""
    if direction == DIRECTION_NEUTRAL:
        return 30.0

    components = []
    if direction == DIRECTION_INVERSE:
        minutes_key, ema_ok = "minutes_below_vwap", (
            features.get("ema5") is not None and features.get("ema5") < features.get("ema10", 1e18) < features.get("ema20", 1e18)
            if features.get("ema10") is not None and features.get("ema20") is not None else None
        )
        structure_matched = (features.get("lower_high_count_last3") or 0) + (features.get("lower_low_count_last3") or 0)
    else:
        minutes_key, ema_ok = "minutes_above_vwap", (
            features.get("ema5") is not None and features.get("ema5") > features.get("ema10", -1e18) > features.get("ema20", -1e18)
            if features.get("ema10") is not None and features.get("ema20") is not None else None
        )
        structure_matched = (features.get("higher_high_count_last3") or 0) + (features.get("higher_low_count_last3") or 0)

    minutes = features.get(minutes_key)
    if minutes is not None:
        components.append(min(100.0, minutes / 20.0 * 100.0))
    if ema_ok is not None:
        components.append(100.0 if ema_ok else 20.0)
    components.append(min(100.0, structure_matched / 6.0 * 100.0))

    if not components:
        return 50.0
    return round(sum(components) / len(components), 2)


def classify_trend_regime(
    trend_strength_score: float, trend_persistence_score: float, reversal_probability_5m: Optional[float],
    relative_volume: Optional[float], atr_pct: Optional[float], recent_direction_flip_count: int = 0,
    is_panic_signal: bool = False, is_recovery_signal: bool = False,
) -> str:
    """7가지 Trend Regime 중 하나로 분류한다."""
    if is_panic_signal:
        return REGIME_PANIC
    if is_recovery_signal:
        return REGIME_RECOVERY
    if recent_direction_flip_count >= 2:
        return REGIME_WHIPSAW
    if reversal_probability_5m is not None and reversal_probability_5m >= 55.0 and trend_persistence_score < 55.0:
        return REGIME_REVERSAL_RISK
    if trend_strength_score >= 75.0 and trend_persistence_score >= 65.0:
        return REGIME_STRONG_TREND
    if trend_strength_score >= 55.0:
        return REGIME_NORMAL_TREND
    return REGIME_RANGE


# =============================================================================
# 섹션 21 — Regime 안정성(한 번의 봉 변화로 전환하지 않음) + 섹션 20 전환 핸들러
# =============================================================================

_REGIME_CONFIRM_CONFIDENCE = 80.0
_REGIME_CONFIRM_BARS_1MIN = 2
_WHIPSAW_REPEAT_WINDOW_SECONDS = 120


# 요구사항(2026-07-16) — Big Trend Holding은 더 이상 자체적으로 장세를 재분류하지
# 않는다. 이미 2연속 사이클로 확정된 공용 Adaptive Regime(app.trading.
# adaptive_market_regime) 결과를 이 표로 매핑만 해서 실행한다.
_ADAPTIVE_REGIME_TO_BIG_TREND_REGIME = {
    "STRONG_UP": REGIME_STRONG_TREND, "STRONG_DOWN": REGIME_STRONG_TREND,
    "RANGE": REGIME_RANGE, "VOLATILE_RANGE": REGIME_WHIPSAW,
    "HIGH_VOLATILITY": REGIME_WHIPSAW, "PANIC": REGIME_PANIC,
    "REVERSAL_CANDIDATE_UP": REGIME_REVERSAL_RISK, "REVERSAL_CANDIDATE_DOWN": REGIME_REVERSAL_RISK,
    "DATA_INSUFFICIENT": REGIME_RANGE,
}


def map_adaptive_regime_to_big_trend_regime(adaptive_regime: Optional[str]) -> str:
    """공용 Adaptive Regime 결과를 Big Trend Holding의 정책 실행에 필요한 자체
    regime 이름으로 매핑만 한다(재분류 없음). 모르는/빈 값은 RANGE(가장 보수적인
    기본값)로 폴백한다."""
    return _ADAPTIVE_REGIME_TO_BIG_TREND_REGIME.get(adaptive_regime, REGIME_RANGE)


def default_regime_state() -> dict:
    return {
        "confirmed_regime": None, "candidate_regime": None, "candidate_bar_count": 0,
        "regime_started_at": None, "transition_count": 0, "transition_history": [],
    }


def _within_seconds(iso_ts: Optional[str], now: datetime, seconds: float) -> bool:
    if not iso_ts:
        return False
    try:
        return (now - datetime.fromisoformat(iso_ts)).total_seconds() <= seconds
    except Exception:
        return False


def update_regime_state(
    regime_state: Optional[dict], candidate_regime: str, confidence: float, now: datetime,
    bar_completed_3min: bool = False,
) -> dict:
    """섹션 21 — Regime 전환은 (2개 연속 1분봉) 또는 (1개 완성된 3분봉) 또는
    (confidence>=80) 중 하나를 만족해야 확정된다. 확정된 전환이 최근 2분 이내에
    이미 한 번 이상 있었다면(반복 전환) WHIPSAW로 강제 분류한다."""
    state = dict(regime_state) if regime_state else default_regime_state()
    confirmed = state.get("confirmed_regime")

    if confirmed is None:
        state["confirmed_regime"] = candidate_regime
        state["regime_started_at"] = now.isoformat()
        state["candidate_regime"], state["candidate_bar_count"] = None, 0
        return state

    if candidate_regime == confirmed:
        state["candidate_regime"], state["candidate_bar_count"] = None, 0
        return state

    if state.get("candidate_regime") != candidate_regime:
        state["candidate_regime"] = candidate_regime
        state["candidate_bar_count"] = 1
    else:
        state["candidate_bar_count"] = state.get("candidate_bar_count", 0) + 1

    should_confirm = (
        confidence >= _REGIME_CONFIRM_CONFIDENCE
        or state["candidate_bar_count"] >= _REGIME_CONFIRM_BARS_1MIN
        or bar_completed_3min
    )
    if not should_confirm:
        return state

    history = [h for h in state.get("transition_history", []) if _within_seconds(h["at"], now, _WHIPSAW_REPEAT_WINDOW_SECONDS)]
    recent_count_before = len(history)
    history.append({"at": now.isoformat(), "from": confirmed, "to": candidate_regime})
    state["transition_history"] = history[-20:]
    state["transition_count"] = state.get("transition_count", 0) + 1

    final_regime = REGIME_WHIPSAW if recent_count_before >= 1 else candidate_regime
    state["confirmed_regime"] = final_regime
    state["regime_started_at"] = now.isoformat()
    state["candidate_regime"], state["candidate_bar_count"] = None, 0
    return state


def regime_duration_seconds(regime_state: dict, now: datetime) -> Optional[float]:
    started = regime_state.get("regime_started_at") if regime_state else None
    if not started:
        return None
    try:
        return (now - datetime.fromisoformat(started)).total_seconds()
    except Exception:
        return None


# 섹션 20 — Regime 전환 시 즉시 대응(명세 예시를 그대로 반영, 나머지는 일반 규칙으로 대체).
_REGIME_TRANSITION_ACTIONS = {
    (REGIME_STRONG_TREND, REGIME_RANGE): {
        "action": "REDUCE_POSITION", "reduce_ratio": 0.4, "tighten_trailing": True, "tighten_take_profit": True,
    },
    (REGIME_RANGE, REGIME_STRONG_TREND): {
        "action": "HOLD_AND_RELAX", "reduce_ratio": 0.0, "allow_pullback_scale_in": True, "remove_fixed_tp": True,
    },
    (REGIME_NORMAL_TREND, REGIME_WHIPSAW): {
        "action": "REDUCE_POSITION", "reduce_ratio": 0.5, "tighten_reentry_cooldown": True,
    },
}


def compute_regime_transition_action(old_regime: Optional[str], new_regime: str) -> dict:
    """섹션 20 — regime이 바뀌면 즉시 보유전략도 전환한다. 명시된 3개 조합은 정확한
    수치를 쓰고, 그 외 조합은 "추세→횡보/휩쏘 계열"이면 축소, "횡보/휩쏘→추세"면
    유지라는 일반 규칙으로 근사한다."""
    if old_regime is None or old_regime == new_regime:
        return {"action": "NONE", "reduce_ratio": 0.0}
    specific = _REGIME_TRANSITION_ACTIONS.get((old_regime, new_regime))
    if specific:
        return specific
    slow_regimes = (REGIME_RANGE, REGIME_WHIPSAW)
    trend_regimes = (REGIME_STRONG_TREND, REGIME_NORMAL_TREND, REGIME_PANIC, REGIME_RECOVERY)
    if new_regime in slow_regimes and old_regime in trend_regimes:
        return {"action": "REDUCE_POSITION", "reduce_ratio": 0.3, "tighten_trailing": True}
    if new_regime in trend_regimes and old_regime in slow_regimes:
        return {"action": "HOLD_AND_RELAX", "reduce_ratio": 0.0}
    return {"action": "NONE", "reduce_ratio": 0.0}


# =============================================================================
# 섹션 3/4 — 추세유지 vs 반전 분리 + Hysteresis
# =============================================================================

_ENTRY_THRESHOLDS = {"probability": 62.0, "trend_strength": 65.0}
_HOLD_MIN_PROBABILITY = 48.0
_HOLD_MIN_PERSISTENCE = 60.0
_EXIT_OPPOSITE_PROBABILITY = 65.0
_EXIT_REVERSAL_5M = 68.0
_EXIT_CONFIDENCE_THRESHOLD = 72.0


def entry_gate_ok(probability_for_direction: float, trend_strength_score: float) -> bool:
    return probability_for_direction >= _ENTRY_THRESHOLDS["probability"] and trend_strength_score >= _ENTRY_THRESHOLDS["trend_strength"]


def hold_gate_ok(probability_for_direction: float, trend_persistence_score: float) -> bool:
    """진입 후 확률이 소폭 떨어져도(예: 62→55) 바로 팔지 않고 유지할 수 있는지."""
    return probability_for_direction >= _HOLD_MIN_PROBABILITY or trend_persistence_score >= _HOLD_MIN_PERSISTENCE


def exit_gate_triggered(opposite_probability: float, reversal_probability_5m: Optional[float], exit_confidence: Optional[float]) -> bool:
    if opposite_probability >= _EXIT_OPPOSITE_PROBABILITY:
        return True
    if reversal_probability_5m is not None and reversal_probability_5m >= _EXIT_REVERSAL_5M:
        return True
    if exit_confidence is not None and exit_confidence >= _EXIT_CONFIDENCE_THRESHOLD:
        return True
    return False


# =============================================================================
# 섹션 3 — 실제 추세 반전 확인(9개 중 3개 이상)
# =============================================================================

_REVERSAL_CONFIRMATION_MIN = 3


def count_reversal_confirmations(signals: dict) -> dict:
    """signals에 있는(None이 아닌) 조건만 평가한다 — 데이터 없는 조건은 "미확인"으로
    치고 카운트에서 제외한다(있다고 가정하지 않음)."""
    checks = {
        "opposite_probability_high": signals.get("opposite_probability_high"),
        "vwap_opposite_break_confirmed": signals.get("vwap_opposite_break_confirmed"),
        "structure_broken": signals.get("structure_broken"),
        "macd_flip_3_consecutive": signals.get("macd_flip_3_consecutive"),
        "rsi_cross_50_opposite": signals.get("rsi_cross_50_opposite"),
        "order_flow_reversed": signals.get("order_flow_reversed"),
        "tape_speed_changed": signals.get("tape_speed_changed"),
        "volume_confirmed_opposite_break": signals.get("volume_confirmed_opposite_break"),
        "prediction_and_cycle_both_opposite": signals.get("prediction_and_cycle_both_opposite"),
    }
    evaluated = {k: v for k, v in checks.items() if v is not None}
    matched = sum(1 for v in evaluated.values() if v)
    return {"matched": matched, "evaluated_count": len(evaluated), "confirmed": matched >= _REVERSAL_CONFIRMATION_MIN, "checks": evaluated}


# =============================================================================
# 섹션 5 — 장세별 청산 정책(고정 3% 전량익절 폐지)
# =============================================================================

REGIME_EXIT_POLICY = {
    # 섹션 19 — regime별 목표 보유시간(분)/부분익절/손절/트레일링/최대비중/재진입 대기를
    # 명시적으로 분리한다. STRONG_TREND/PANIC은 "단기 신호 1회로 청산 금지 + 반전조건
    # 3개 이상 동시 충족 시에만 축소"가 동일하게 적용되는 그룹이다(SHORT_SQUEEZE는
    # 별도 regime 상수가 없어 PANIC과 같은 정책을 공유한다).
    REGIME_STRONG_TREND: {
        "first_tp_return_pct": 3.0, "first_tp_ratio": 0.25, "relax_time_stop": True,
        "min_hold_minutes": (30, 120), "single_signal_exit_blocked": True, "reversal_conditions_required": 3,
        "trailing_pct_range": (1.8, 3.0), "persistence_hold_threshold": 70.0,
        "sl_pct": None, "max_position_pct": None, "reentry_cooldown_minutes": None,
    },
    REGIME_NORMAL_TREND: {
        "first_tp_return_pct": 3.0, "first_tp_ratio": 0.45, "relax_time_stop": False,
        "min_hold_minutes": (15, 45), "single_signal_exit_blocked": False, "reversal_conditions_required": None,
        "trailing_pct_range": (1.2, 1.5), "opposite_probability_reduce_threshold": 65.0,
        "sl_pct": None, "max_position_pct": None, "reentry_cooldown_minutes": None,
    },
    REGIME_RANGE: {
        "first_tp_return_pct": 2.0, "first_tp_ratio": 0.85, "relax_time_stop": False,
        "min_hold_minutes": (3, 15), "single_signal_exit_blocked": False, "reversal_conditions_required": None,
        "trailing_pct_range": None, "sl_pct": -1.0, "max_position_pct": 35.0, "reentry_cooldown_minutes": 5,
    },
    REGIME_WHIPSAW: {
        "first_tp_return_pct": 1.25, "first_tp_ratio": 1.0, "relax_time_stop": False,
        "min_hold_minutes": (1, 10), "single_signal_exit_blocked": False, "reversal_conditions_required": None,
        "trailing_pct_range": None, "sl_pct": -0.8, "max_position_pct": 25.0, "reentry_cooldown_minutes": None,
        "direction_flip_entry_pause_minutes": 15, "require_2_confirmations_to_enter": True,
    },
    REGIME_PANIC: {
        "first_tp_return_pct": None, "first_tp_ratio": None, "relax_time_stop": True,
        "min_hold_minutes": (30, 120), "single_signal_exit_blocked": True, "reversal_conditions_required": 3,
        "trailing_pct_range": (1.8, 3.0), "persistence_hold_threshold": 70.0,
        "sl_pct": None, "max_position_pct": None, "reentry_cooldown_minutes": None,
    },
    REGIME_REVERSAL_RISK: {
        "first_tp_return_pct": 2.0, "first_tp_ratio": 0.60, "relax_time_stop": False,
        "min_hold_minutes": (5, 20), "single_signal_exit_blocked": False, "reversal_conditions_required": None,
        "trailing_pct_range": (1.0, 1.3), "sl_pct": None, "max_position_pct": None, "reentry_cooldown_minutes": None,
    },
    REGIME_RECOVERY: {
        "first_tp_return_pct": 3.0, "first_tp_ratio": 0.45, "relax_time_stop": False,
        "min_hold_minutes": (15, 45), "single_signal_exit_blocked": False, "reversal_conditions_required": None,
        "trailing_pct_range": (1.2, 1.5), "sl_pct": None, "max_position_pct": None, "reentry_cooldown_minutes": None,
    },
}


def get_exit_policy(regime: str) -> dict:
    return REGIME_EXIT_POLICY.get(regime, REGIME_EXIT_POLICY[REGIME_NORMAL_TREND])


get_regime_hold_policy = get_exit_policy  # 섹션 19 표현("보유정책")에 맞춘 별칭


# =============================================================================
# 섹션 6 — Profit Lock(Net Return 기준)
# =============================================================================

PROFIT_LOCK_LADDER = [
    (15.0, 12.0), (12.0, 9.0), (8.0, 6.0), (5.0, 3.5), (3.0, 1.8), (2.0, 0.8), (1.2, 0.0), (0.7, -0.3),
]
PROFIT_LOCK_3PCT_PARTIAL_RATIO = 0.25


def compute_profit_lock_floor_pct(net_return_pct: float) -> Optional[float]:
    """net_return_pct(수수료·세금·슬리피지 차감 후) 기준 최소 보장수익 floor."""
    for milestone, floor in PROFIT_LOCK_LADDER:
        if net_return_pct >= milestone:
            return floor
    return None


# =============================================================================
# 섹션 7 — Adaptive Trailing Stop(장세별 + ATR 기반 중 큰 값)
# =============================================================================

_REGIME_TRAILING_PCT = {
    REGIME_RANGE: 0.85, REGIME_WHIPSAW: 0.85, REGIME_NORMAL_TREND: 1.35, REGIME_RECOVERY: 1.35,
    REGIME_REVERSAL_RISK: 1.0, REGIME_STRONG_TREND: 2.0, REGIME_PANIC: 2.6,
}
_ATR_TRAILING_MULTIPLIER = 1.5


def regime_trailing_pct(regime: str) -> float:
    return _REGIME_TRAILING_PCT.get(regime, 1.35)


def atr_based_trailing_pct(atr_pct: Optional[float]) -> float:
    if atr_pct is None:
        return 0.0
    return round(atr_pct * _ATR_TRAILING_MULTIPLIER, 4)


def compute_effective_trailing_pct(regime: str, atr_pct: Optional[float], profit_lock_floor_pct: Optional[float], net_return_pct: float) -> float:
    """regime 기준과 ATR 기준 중 큰 값을 쓰되, Profit Lock 최소수익보다 아래로
    내려갈 수 있는 폭은 허용하지 않는다(trailing으로 인한 매도가가 floor 아래로
    가지 않도록 상한을 둔다)."""
    base = max(regime_trailing_pct(regime), atr_based_trailing_pct(atr_pct))
    if profit_lock_floor_pct is not None:
        max_allowed_pullback = max(0.0, net_return_pct - profit_lock_floor_pct)
        base = min(base, max_allowed_pullback) if max_allowed_pullback > 0 else 0.0
    return round(base, 4)


# =============================================================================
# 섹션 8 — 손절 규칙(Net PnL 기준 단일 effective_sl_pct) + 선제청산
# =============================================================================

_SL_LADDER = {"LOW_VOL": -1.0, "NORMAL": -1.5, "HIGH_VOL": -2.0, "STRONG_TREND_INITIAL": -1.8}


def effective_sl_pct(volatility_class: str, is_strong_trend_initial_phase: bool = False) -> float:
    if is_strong_trend_initial_phase:
        return _SL_LADDER["STRONG_TREND_INITIAL"]
    return _SL_LADDER.get(volatility_class, _SL_LADDER["NORMAL"])


def count_early_reversal_warnings(
    opposite_probability_delta: Optional[float], trend_strength_drop: Optional[float],
    vwap_broken_opposite: Optional[bool], order_flow_reversed: Optional[bool],
) -> int:
    count = 0
    if opposite_probability_delta is not None and opposite_probability_delta >= 20.0:
        count += 1
    if trend_strength_drop is not None and trend_strength_drop >= 20.0:
        count += 1
    if vwap_broken_opposite:
        count += 1
    if order_flow_reversed:
        count += 1
    return count


def should_preemptive_reduce(held_minutes: Optional[float], warning_count: int) -> bool:
    """진입 후 3분 이내 + 4개 중 3개 이상 경고 → 50% 선제 축소."""
    if held_minutes is None or held_minutes > 3.0:
        return False
    return warning_count >= 3


# =============================================================================
# 섹션 9 — 수익 반납 제한(Profit Giveback)
# =============================================================================

_GIVEBACK_LADDER = [
    (0.0, 3.0, 1.0), (3.0, 5.0, 1.5), (5.0, 10.0, 2.0), (10.0, 15.0, 2.5), (15.0, float("inf"), 3.0),
]


def max_giveback_pct(peak_net_return_pct: float) -> float:
    for lo, hi, allowed in _GIVEBACK_LADDER:
        if lo <= peak_net_return_pct < hi:
            return allowed
    return 3.0


def compute_profit_giveback_pct(peak_net_return_pct: float, current_net_return_pct: float) -> float:
    return round(max(0.0, peak_net_return_pct - current_net_return_pct), 4)


def giveback_forced_exit_ratio(peak_net_return_pct: float, current_net_return_pct: float) -> float:
    """수익 반납 한도 초과 시 강제 청산 비율(0이면 위반 아님). 명세 9절 예시(peak+10%→
    7.5%↓시 최소50%청산, peak+15%→12%↓시 최소70%청산)를 일반화한 규칙."""
    giveback = compute_profit_giveback_pct(peak_net_return_pct, current_net_return_pct)
    allowed = max_giveback_pct(peak_net_return_pct)
    if giveback <= allowed:
        return 0.0
    if peak_net_return_pct >= 15.0:
        return 0.70
    if peak_net_return_pct >= 10.0:
        return 0.50
    if peak_net_return_pct >= 5.0:
        return 0.40
    return 0.30


# =============================================================================
# 섹션 12 — 추세강도 기반 포지션 사이징
# =============================================================================

_POSITION_SIZING_LADDER = [(93.0, 90.0), (85.0, 80.0), (75.0, 60.0), (65.0, 40.0), (55.0, 20.0)]


def position_pct_from_trend_strength(trend_strength_score: float) -> float:
    for floor, pct in _POSITION_SIZING_LADDER:
        if trend_strength_score >= floor:
            return pct
    return 0.0


def scale_in_allowed(
    currently_profitable: bool, trend_strengthening: bool, pullback_then_rebreak: bool,
    seconds_since_last_entry: Optional[float],
) -> bool:
    if not currently_profitable:
        return False  # 손실 중 추가매수 금지
    if seconds_since_last_entry is None or seconds_since_last_entry < 180.0:
        return False
    return bool(trend_strengthening or pullback_then_rebreak)


# =============================================================================
# 섹션 11 — 반대 방향 전환(4/6 조건, 단계적 전환)
# =============================================================================

def count_switch_confirmations(signals: dict) -> dict:
    checks = {
        "target_probability_high": signals.get("target_probability_high"),
        "reversal_probability_5m_high": signals.get("reversal_probability_5m_high"),
        "vwap_recovered_2bars": signals.get("vwap_recovered_2bars"),
        "structure_confirmed": signals.get("structure_confirmed"),
        "order_flow_target_high": signals.get("order_flow_target_high"),
        "cycle_phase_confirmed": signals.get("cycle_phase_confirmed"),
    }
    evaluated = {k: v for k, v in checks.items() if v is not None}
    matched = sum(1 for v in evaluated.values() if v)
    return {"matched": matched, "confirmed": matched >= 4, "checks": evaluated}


def default_switch_state() -> dict:
    return {"awaiting_final_confirmation": False, "first_confirmed_at": None, "target_direction": None}


def evaluate_staged_switch(switch_state: dict, signals: dict, target_direction: str, now: datetime) -> dict:
    """1차: 4/6 조건 충족 시 50% 청산 + 다음 1분봉 확인 대기. 2차(1분 후에도 유지):
    전량청산 + 반대방향 20~30% 시험진입 허용."""
    state = dict(switch_state) if switch_state else default_switch_state()
    confirmation = count_switch_confirmations(signals)

    if not state.get("awaiting_final_confirmation"):
        if confirmation["confirmed"]:
            state["awaiting_final_confirmation"] = True
            state["first_confirmed_at"] = now.isoformat()
            state["target_direction"] = target_direction
            return {"action": "PARTIAL_CLOSE_50", "state": state, "confirmation": confirmation}
        return {"action": "NONE", "state": state, "confirmation": confirmation}

    # 2차 확인 대기 중
    if state.get("target_direction") != target_direction:
        # 방향이 바뀌었으면 확인 절차를 리셋한다.
        state = default_switch_state()
        return {"action": "NONE", "state": state, "confirmation": confirmation}

    first_confirmed_at = state.get("first_confirmed_at")
    elapsed = None
    if first_confirmed_at:
        try:
            elapsed = (now - datetime.fromisoformat(first_confirmed_at)).total_seconds()
        except Exception:
            elapsed = None
    if elapsed is not None and elapsed < 45.0:
        return {"action": "NONE", "state": state, "confirmation": confirmation}  # 다음 1분봉 형성 대기

    if confirmation["confirmed"]:
        result_state = default_switch_state()
        return {"action": "FULL_CLOSE_AND_TRIAL_ENTRY", "state": result_state, "confirmation": confirmation}

    state = default_switch_state()
    return {"action": "NONE", "state": state, "confirmation": confirmation}


# =============================================================================
# 섹션 10 — Trend Hold Decision(최종 결정, 우선순위대로 평가)
# =============================================================================

def decide_trend_hold_action(
    *, held_symbol: str, net_return_pct: float, peak_net_return_pct: float,
    regime: str, trend_strength_score: float, trend_persistence_score: float,
    probability_for_direction: float, opposite_probability: float,
    reversal_probability_5m: Optional[float], exit_confidence: Optional[float],
    profit_lock_floor_pct: Optional[float], hard_stop_triggered: bool,
    reversal_confirmed: bool, first_tp_taken: bool,
) -> dict:
    """섹션 10 우선순위: ①강제손절/청산 ②Profit Lock 위반 ③실제 추세반전 ④부분익절
    ⑤추세지속 HOLD ⑥방향전환(스위칭은 evaluate_staged_switch가 별도로 담당하므로
    여기서는 EXIT_ALL만 반환하고 실제 전환 판단은 호출부가 잇는다)."""
    reasons: list[str] = []

    # ① 강제손절
    if hard_stop_triggered:
        return {"action": ACTION_EXIT_ALL, "reasons": ["강제손절 트리거"]}

    # ② Profit Lock 위반
    if profit_lock_floor_pct is not None and net_return_pct < profit_lock_floor_pct:
        reasons.append(f"Profit Lock 위반(net {net_return_pct:.2f}% < floor {profit_lock_floor_pct:.2f}%)")
        return {"action": ACTION_EXIT_ALL, "reasons": reasons}

    giveback_ratio = giveback_forced_exit_ratio(peak_net_return_pct, net_return_pct)
    if giveback_ratio > 0:
        reasons.append(f"수익반납 한도 초과(peak {peak_net_return_pct:.2f}% → 현재 {net_return_pct:.2f}%)")
        action = ACTION_EXIT_ALL if giveback_ratio >= 0.70 else ACTION_TAKE_PROFIT_50
        return {"action": action, "reasons": reasons, "forced_exit_ratio": giveback_ratio}

    # ③ 실제 추세 반전 확인
    if reversal_confirmed:
        reasons.append("실제 추세 반전 확인(9개 중 3개 이상)")
        return {"action": ACTION_EXIT_ALL, "reasons": reasons}
    if exit_gate_triggered(opposite_probability, reversal_probability_5m, exit_confidence):
        reasons.append(f"청산 게이트 충족(반대확률 {opposite_probability:.0f}%)")
        return {"action": ACTION_HOLD_REDUCED, "reasons": reasons}

    # ④ 부분익절(regime별 정책)
    policy = get_exit_policy(regime)
    if not first_tp_taken and policy["first_tp_return_pct"] is not None and net_return_pct >= policy["first_tp_return_pct"]:
        ratio = policy["first_tp_ratio"]
        reasons.append(f"{regime} 1차 부분익절(+{policy['first_tp_return_pct']:.1f}%, 비중 {ratio*100:.0f}%)")
        action = ACTION_TAKE_PROFIT_25 if ratio <= 0.30 else ACTION_TAKE_PROFIT_50
        return {"action": action, "reasons": reasons, "tp_ratio": ratio}

    # ⑤ 추세지속 HOLD
    if hold_gate_ok(probability_for_direction, trend_persistence_score):
        reasons.append(f"추세 지속(persistence {trend_persistence_score:.0f}, probability {probability_for_direction:.0f})")
        return {"action": ACTION_HOLD_FULL, "reasons": reasons}

    reasons.append("추세유지 조건 미충족 — 비중 축소 대기")
    return {"action": ACTION_HOLD_REDUCED, "reasons": reasons}


# =============================================================================
# 로깅
# =============================================================================

def log_big_trend_decision(row: dict) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _LOG_PATH.exists()
        with _LOG_PATH.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=LOG_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow({c: row.get(c, "") for c in LOG_COLUMNS})
    except Exception as exc:
        logger.debug("[BigTrendEngine] 로그 기록 실패: %s", exc)


# =============================================================================
# 오케스트레이션 클래스
# =============================================================================

class HynixBigTrendEngine:
    """섹션 1 출력 스키마를 계산하는 진입점. 이 클래스는 주문을 실행하지 않는다."""

    def compute(
        self, *, features: dict, held_symbol: Optional[str], entry_price: Optional[float],
        current_price: Optional[float], net_return_pct: float, peak_net_return_pct: float,
        reversal_probability_3m: Optional[float], reversal_probability_5m: Optional[float],
        reversal_probability_15m: Optional[float], reversal_signals: dict,
        recent_direction_flip_count: int = 0, hard_stop_triggered: bool = False,
        first_tp_taken: bool = False, volatility_class: str = "NORMAL",
        is_strong_trend_initial_phase: bool = False,
        regime_state: Optional[dict] = None, now: Optional[datetime] = None, bar_completed_3min: bool = False,
        adaptive_regime: Optional[str] = None,
    ) -> dict:
        trend = compute_trend_strength_score(features)
        direction = trend["dominant_direction"]
        persistence = compute_trend_persistence_score(features, direction)

        is_panic = bool(features.get("is_panic_signal"))
        is_recovery = bool(features.get("is_recovery_signal"))

        if adaptive_regime is not None:
            # 요구사항(2026-07-16) — 별도로 재분류하지 않는다. 이미 2연속 사이클로
            # 확정된 공용 Adaptive Regime 결과를 그대로 매핑해서 실행만 한다.
            # regime_state는 "전환이 방금 일어났는지"(포지션 축소 트리거용)만
            # 계속 추적한다 — classify_trend_regime()/update_regime_state()의
            # 자체 확인 절차(1분봉 2연속/3분봉 완성/confidence>=80)는 더 이상
            # 거치지 않는다(공용 엔진이 이미 그 역할을 함).
            raw_regime = map_adaptive_regime_to_big_trend_regime(adaptive_regime)
            updated_regime_state = None
            transition_action = {"action": "NONE", "reduce_ratio": 0.0}
            if regime_state is not None and now is not None:
                previous_confirmed = regime_state.get("confirmed_regime")
                updated_regime_state = dict(regime_state)
                if previous_confirmed != raw_regime:
                    updated_regime_state["confirmed_regime"] = raw_regime
                    updated_regime_state["regime_started_at"] = now.isoformat()
                    updated_regime_state["transition_count"] = regime_state.get("transition_count", 0) + 1
                    if previous_confirmed is not None:
                        transition_action = compute_regime_transition_action(previous_confirmed, raw_regime)
                regime = updated_regime_state["confirmed_regime"]
            else:
                regime = raw_regime
        else:
            raw_regime = classify_trend_regime(
                trend["trend_strength_score"], persistence, reversal_probability_5m,
                features.get("relative_volume"), features.get("atr_pct"), recent_direction_flip_count,
                is_panic_signal=is_panic, is_recovery_signal=is_recovery,
            )

            # 섹션 21 — regime_state가 주어지면(실시간 연동) 한 틱의 변화로 바로 전환하지
            # 않고 confirm된 regime만 실제 정책에 사용한다. 주어지지 않으면(단위 테스트 등)
            # 기존처럼 즉시 분류 결과를 그대로 쓴다(하위호환).
            updated_regime_state = None
            transition_action = {"action": "NONE", "reduce_ratio": 0.0}
            if regime_state is not None and now is not None:
                previous_confirmed = regime_state.get("confirmed_regime")
                updated_regime_state = update_regime_state(regime_state, raw_regime, trend["trend_strength_score"], now, bar_completed_3min)
                regime = updated_regime_state["confirmed_regime"]
                if previous_confirmed is not None and previous_confirmed != regime:
                    transition_action = compute_regime_transition_action(previous_confirmed, regime)
            else:
                regime = raw_regime

        reversal = count_reversal_confirmations(reversal_signals)

        probability_for_direction = features.get("inverse_probability" if direction == DIRECTION_INVERSE else "hynix_probability", 50.0) or 50.0
        opposite_probability = features.get("hynix_probability" if direction == DIRECTION_INVERSE else "inverse_probability", 50.0) or 50.0
        exit_confidence = features.get("exit_confidence")
        hold_confidence = round(max(0.0, min(100.0, persistence * 0.6 + probability_for_direction * 0.4)), 2)

        profit_lock_floor = compute_profit_lock_floor_pct(net_return_pct) if held_symbol else None
        trailing_pct = compute_effective_trailing_pct(regime, features.get("atr_pct"), profit_lock_floor, net_return_pct) if held_symbol else None
        position_pct = position_pct_from_trend_strength(trend["trend_strength_score"])

        decision = None
        if held_symbol:
            decision = decide_trend_hold_action(
                held_symbol=held_symbol, net_return_pct=net_return_pct, peak_net_return_pct=peak_net_return_pct,
                regime=regime, trend_strength_score=trend["trend_strength_score"], trend_persistence_score=persistence,
                probability_for_direction=probability_for_direction, opposite_probability=opposite_probability,
                reversal_probability_5m=reversal_probability_5m, exit_confidence=exit_confidence,
                profit_lock_floor_pct=profit_lock_floor, hard_stop_triggered=hard_stop_triggered,
                reversal_confirmed=reversal["confirmed"], first_tp_taken=first_tp_taken,
            )

        hold_policy = get_regime_hold_policy(regime)
        return {
            "dominant_direction": direction, "trend_regime": regime, "raw_trend_regime": raw_regime,
            "trend_strength_score": trend["trend_strength_score"], "trend_persistence_score": persistence,
            "reversal_probability_3m": reversal_probability_3m, "reversal_probability_5m": reversal_probability_5m,
            "reversal_probability_15m": reversal_probability_15m,
            "hold_confidence": hold_confidence, "exit_confidence": exit_confidence,
            "recommended_hold_minutes": 60.0 if regime == REGIME_STRONG_TREND else (30.0 if regime == REGIME_NORMAL_TREND else 15.0),
            "min_hold_minutes": hold_policy.get("min_hold_minutes"),
            "max_position_pct": position_pct, "current_profit_lock_pct": profit_lock_floor,
            "trailing_pct": trailing_pct, "reversal_confirmation": reversal,
            "final_hold_action": decision["action"] if decision else None,
            "reasons": decision["reasons"] if decision else [],
            "tp_ratio": decision.get("tp_ratio") if decision else None,
            "effective_sl_pct": effective_sl_pct(volatility_class, is_strong_trend_initial_phase),
            "regime_state": updated_regime_state, "regime_transition_action": transition_action,
        }


# =============================================================================
# 실데이터 → features 변환 (실시간 연동용). 없는 데이터(주문흐름/체결강도/호가불균형/
# 외국인·프로그램 수급)는 None으로 두어 count_reversal_confirmations 등이 "미확인"
# 으로 정직하게 처리하도록 한다(있다고 가정하지 않음).
# =============================================================================

def _ema(series, span: int):
    return series.ewm(span=span, adjust=False).mean()


def _consecutive_side_of_vwap(df_1min) -> dict:
    """누적(그 시점까지) VWAP 대비 종가가 최근 몇 개 연속 bar 동안 위/아래였는지."""
    if df_1min is None or df_1min.empty or len(df_1min) < 2:
        return {"minutes_above_vwap": None, "minutes_below_vwap": None}
    work = df_1min.sort_values("datetime").copy()
    typical = (work["high"] + work["low"] + work["close"]) / 3.0
    cum_vol = work["volume"].cumsum()
    cum_pv = (typical * work["volume"]).cumsum()
    running_vwap = cum_pv / cum_vol.replace(0, float("nan"))
    above = (work["close"] > running_vwap).tolist()

    above_count = 0
    for v in reversed(above):
        if v is True:
            above_count += 1
        else:
            break
    below_count = 0
    for v in reversed(above):
        if v is False:
            below_count += 1
        else:
            break
    return {"minutes_above_vwap": above_count, "minutes_below_vwap": below_count}


def _hh_hl_lh_ll_counts(df_3min) -> dict:
    """최근 3개 bar의 고점/저점 구조(Higher High/Low, Lower High/Low) 카운트."""
    empty = {
        "higher_high_count_last3": None, "higher_low_count_last3": None,
        "lower_high_count_last3": None, "lower_low_count_last3": None,
    }
    if df_3min is None or len(df_3min) < 4:
        return empty
    work = df_3min.sort_values("datetime").tail(4)
    highs = work["high"].tolist()
    lows = work["low"].tolist()
    hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i - 1])
    hl = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
    ll = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i - 1])
    return {
        "higher_high_count_last3": hh, "higher_low_count_last3": hl,
        "lower_high_count_last3": lh, "lower_low_count_last3": ll,
    }


def _volume_trend(df_1min) -> dict:
    if df_1min is None or len(df_1min) < 3:
        return {"up_volume_increasing": None, "down_volume_increasing": None}
    work = df_1min.sort_values("datetime").tail(3)
    closes = work["close"].tolist()
    vols = work["volume"].tolist()
    price_down = closes[-1] < closes[-2]
    price_up = closes[-1] > closes[-2]
    vol_rising = vols[-1] > vols[-2]
    return {
        "up_volume_increasing": bool(price_up and vol_rising) if price_up or price_down else None,
        "down_volume_increasing": bool(price_down and vol_rising) if price_up or price_down else None,
    }


def build_big_trend_features(
    df_1min, snapshot: dict, inverse_probability: Optional[float], hynix_probability: Optional[float],
    is_panic_signal: bool = False, is_recovery_signal: bool = False,
) -> dict:
    """DynamicExitEngine.build_snapshot() 결과(atr/macd/rsi/relative_volume 재사용) +
    df_1min에서 새로 계산한 EMA/VWAP체류시간/HH·HL·LH·LL/거래량추세를 합쳐
    compute_trend_strength_score()/compute_trend_persistence_score()에 바로 넣을 수
    있는 features dict를 만든다."""
    features: dict = {
        "macd_histogram": snapshot.get("macd_histogram"), "macd_histogram_prev": snapshot.get("macd_histogram_prev"),
        "rsi_14": snapshot.get("rsi_14"), "relative_volume": snapshot.get("relative_volume"),
        "atr_pct": snapshot.get("atr_14_pct"), "inverse_probability": inverse_probability,
        "hynix_probability": hynix_probability, "is_panic_signal": is_panic_signal, "is_recovery_signal": is_recovery_signal,
    }

    if df_1min is not None and not df_1min.empty:
        work = df_1min.sort_values("datetime")
        closes = work["close"]
        if len(closes) >= 20:
            features["ema5"] = round(float(_ema(closes, 5).iloc[-1]), 4)
            features["ema10"] = round(float(_ema(closes, 10).iloc[-1]), 4)
            features["ema20"] = round(float(_ema(closes, 20).iloc[-1]), 4)
        features.update(_consecutive_side_of_vwap(work))
        features.update(_volume_trend(work))
        try:
            from app.data_sources.auto_market_collector import _resample_minutes

            df_3min = _resample_minutes(work, 3)
            features.update(_hh_hl_lh_ll_counts(df_3min))
        except Exception:
            pass

    return features


def build_reversal_signals(
    features: dict, direction: str, prediction_v2_action: Optional[str], cycle_phase: Optional[str],
) -> dict:
    """섹션 3 반전확인 9개 중 실데이터로 계산 가능한 6개만 채운다. order_flow_reversed/
    tape_speed_changed/volume_confirmed_opposite_break는 이 코드베이스에 실시간
    체결강도·호가·수급 데이터 소스가 없어 None(미확인)으로 둔다 — 있다고 가정하지 않는다."""
    opposite_probability = features.get("hynix_probability" if direction == DIRECTION_INVERSE else "inverse_probability")
    signals: dict = {
        "opposite_probability_high": (opposite_probability >= 65.0) if opposite_probability is not None else None,
        "order_flow_reversed": None, "tape_speed_changed": None, "volume_confirmed_opposite_break": None,
    }

    minutes_key = "minutes_above_vwap" if direction == DIRECTION_INVERSE else "minutes_below_vwap"
    minutes_opposite = features.get(minutes_key)
    signals["vwap_opposite_break_confirmed"] = (minutes_opposite >= 2) if minutes_opposite is not None else None

    if direction == DIRECTION_INVERSE:
        hh = features.get("higher_high_count_last3")
        hl = features.get("higher_low_count_last3")
        signals["structure_broken"] = (hh is not None and hl is not None and (hh + hl) >= 2)
    else:
        lh = features.get("lower_high_count_last3")
        ll = features.get("lower_low_count_last3")
        signals["structure_broken"] = (lh is not None and ll is not None and (lh + ll) >= 2)

    hist, hist_prev = features.get("macd_histogram"), features.get("macd_histogram_prev")
    if hist is not None and hist_prev is not None:
        signals["macd_flip_3_consecutive"] = (hist > 0 > hist_prev) or (hist < 0 < hist_prev)
    else:
        signals["macd_flip_3_consecutive"] = None

    rsi = features.get("rsi_14")
    if rsi is not None:
        signals["rsi_cross_50_opposite"] = (rsi > 50.0) if direction == DIRECTION_INVERSE else (rsi < 50.0)
    else:
        signals["rsi_cross_50_opposite"] = None

    if prediction_v2_action is not None and cycle_phase is not None:
        target_opposite_action = "INVERSE" if direction == DIRECTION_HYNIX else "BUY"
        pred_opposite = prediction_v2_action == target_opposite_action
        cycle_opposite_phases = ("BREAKDOWN", "GAP_FAILURE", "PANIC_SELL") if direction == DIRECTION_HYNIX else ("TREND_UP", "REVERSAL_CONFIRMED_UP")
        cycle_opposite = cycle_phase in cycle_opposite_phases
        signals["prediction_and_cycle_both_opposite"] = bool(pred_opposite and cycle_opposite)
    else:
        signals["prediction_and_cycle_both_opposite"] = None

    return signals
