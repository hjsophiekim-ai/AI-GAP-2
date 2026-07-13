"""hynix_active_strategy_engine.py — 거래 모드 기반 조기진입/Scale-in/빠른전환/
선제매도/재진입 통합 엔진 (사용자 명세: 거래 빈도·기대수익률 개선).

파이프라인(명세 11절):
  Prediction(Cycle Detector + Decision V2) → PositionSizingAI → Risk Approval
  → (호출부가 OrderCoordinator/Broker 실행) → Ledger/State/UI

이 모듈 자체는 주문을 실행하지 않는다 — decide_active_strategy_action()은
"권장" 행동과 blocking_reason만 반환한다. 실제 브로커 호출은 호출부
(hynix_switch_engine.py)가 mock 모드에서만, 명시적 opt-in 토글이 켜졌을 때만 수행한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.trading.hynix_trading_mode import (
    DEFAULT_MODE, calculate_scale_up_target_pct,
    daily_pnl_position_scale, max_total_position_pct, mode_initial_threshold,
    mode_max_round_trips, mode_min_threshold_floor, HOLD_RELIEF_AT_5, HOLD_RELIEF_AT_8,
    MIN_EXPECTED_MOVE_FOR_RELIEF_PCT,
)
from app.models.hynix_position_sizing_ai import PositionSizingAI

ACTION_HOLD = "HOLD"
ACTION_ENTER_HYNIX = "ENTER_HYNIX"
ACTION_ENTER_INVERSE = "ENTER_INVERSE"
ACTION_SCALE_IN = "SCALE_IN"
ACTION_SCALE_OUT_PARTIAL = "SCALE_OUT_PARTIAL"
ACTION_EXIT_ALL = "EXIT_ALL"
ACTION_SWITCH = "SWITCH"

_MIN_SCALE_IN_GAP_SECONDS = 90
_MIN_SWITCH_INTERVAL_SECONDS = 3 * 60
_WHIPSAW_WINDOW_SECONDS = 10 * 60
_WHIPSAW_FLIP_LIMIT = 2
_WHIPSAW_DAMPEN_SECONDS = 15 * 60
_MAX_SCALE_INS = 3
_ADVERSE_MOVE_LIMIT_PCT = 0.8
_REENTRY_COOLDOWN_SECONDS = 5 * 60
_REENTRY_COOLDOWN_AFTER_SL_SECONDS = 15 * 60


def default_active_strategy_state(mode: str = DEFAULT_MODE) -> dict:
    return {
        "_state_date": None, "mode": mode,
        "position_symbol": None, "position_entry_price": None, "position_entry_time": None,
        "position_entry_probability": None, "position_current_pct": 0.0, "scale_in_count": 0,
        "last_exit_time": None, "last_exit_was_stop_loss": False, "last_exit_symbol": None,
        "last_switch_time": None, "switch_history": [],
        "whipsaw_dampened_until": None,
        "round_trip_count_today": 0, "hold_streak_no_position": 0, "hold_streak_started_at": None,
        "consecutive_stop_losses": 0,
    }


def _reset_if_new_day(state: dict, now: datetime) -> dict:
    today = now.strftime("%Y%m%d")
    if state.get("_state_date") != today:
        fresh = default_active_strategy_state(state.get("mode", DEFAULT_MODE))
        fresh["_state_date"] = today
        return fresh
    return state


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def update_hold_streak(state: dict, has_position: bool, action: str, now: datetime) -> dict:
    """명세 5절 — 최근 30분 무포지션 + HOLD 5/8회 이상 시 threshold 완화용 카운터."""
    state = dict(state)
    if has_position:
        state["hold_streak_no_position"] = 0
        state["hold_streak_started_at"] = None
        return state
    if action == ACTION_HOLD:
        if state.get("hold_streak_started_at") is None:
            state["hold_streak_started_at"] = now.isoformat()
        started = _parse_iso(state.get("hold_streak_started_at")) or now
        if (now - started).total_seconds() > 30 * 60:
            # 30분 창을 벗어나면 새 창으로 리셋(카운트는 1부터 다시)
            state["hold_streak_started_at"] = now.isoformat()
            state["hold_streak_no_position"] = 1
        else:
            state["hold_streak_no_position"] = state.get("hold_streak_no_position", 0) + 1
    else:
        state["hold_streak_no_position"] = 0
        state["hold_streak_started_at"] = None
    return state


def calculate_effective_threshold(
    mode: str, hold_streak: int, expected_move_pct: Optional[float], cycle_phase: Optional[str],
    daily_return_pct: Optional[float],
) -> dict:
    """명세 1·5·10절을 합쳐 이 사이클의 실제 진입 임계값을 계산한다."""
    base = mode_initial_threshold(mode)
    floor = mode_min_threshold_floor(mode)
    relief = 0.0

    relief_allowed = (
        cycle_phase != "NO_TRADE"
        and (expected_move_pct is None or expected_move_pct >= MIN_EXPECTED_MOVE_FOR_RELIEF_PCT)
    )
    if relief_allowed:
        if hold_streak >= 8:
            relief = HOLD_RELIEF_AT_8
        elif hold_streak >= 5:
            relief = HOLD_RELIEF_AT_5

    daily = daily_pnl_position_scale(daily_return_pct)
    threshold = max(floor, base - relief + daily["threshold_add"])
    return {
        "threshold": round(threshold, 2), "base_threshold": base, "relief_applied": relief,
        "daily_threshold_add": daily["threshold_add"], "daily_max_position_pct": daily["max_position_pct"],
        "entries_allowed": daily["entries_allowed"], "force_liquidate": daily["force_liquidate"],
    }


def _is_whipsaw_dampened(state: dict, now: datetime) -> bool:
    until = _parse_iso(state.get("whipsaw_dampened_until"))
    return bool(until and now < until)


def _register_switch(state: dict, now: datetime) -> dict:
    state = dict(state)
    history = [h for h in state.get("switch_history", []) if _parse_iso(h) and (now - _parse_iso(h)).total_seconds() <= _WHIPSAW_WINDOW_SECONDS]
    history.append(now.isoformat())
    state["switch_history"] = history
    state["last_switch_time"] = now.isoformat()
    if len(history) > _WHIPSAW_FLIP_LIMIT:
        state["whipsaw_dampened_until"] = (now + timedelta(seconds=_WHIPSAW_DAMPEN_SECONDS)).isoformat()
    return state


def decide_active_strategy_action(
    mode: str, now: datetime,
    buy_probability: float, inverse_probability: float, hold_probability: float,
    model_confidence: float, expected_move_pct: float,
    down_turn_probability_3m: Optional[float], up_turn_probability_3m: Optional[float],
    momentum_inflection_or_acceleration: Optional[float],
    cycle_phase: Optional[str], order_flow_confidence: Optional[float],
    atr_pct: Optional[float], consecutive_stop_losses: int, recent_pnl_pct: Optional[float],
    daily_return_pct: Optional[float], position_state: dict, strategy_state: dict,
    data_ok: bool = True, position_conflict: bool = False,
    enhanced_ai_score: Optional[float] = None, micron_ai_score: Optional[float] = None,
) -> dict:
    """전체 판단(진입/Scale-in/방향전환/선제매도/재진입)을 1회 실행한다.

    신규 진입 판단은 fusion_score(app.models.hynix_decision_v2.calculate_fusion_score)
    기반이다 — Cycle Phase는 Entry Gate가 아니라 fusion_score의 작은 보조 feature일
    뿐이며, enhanced_ai_score/micron_ai_score를 넘기지 않으면 중립값(50)으로 대체된다.

    position_state: {"symbol": str|None, "quantity": int, "entry_price": float|None}
    strategy_state: default_active_strategy_state() 스키마(호출부가 이어서 유지).
    """
    state = _reset_if_new_day(dict(strategy_state), now)
    state["mode"] = mode
    reasons: list = []
    blocking_reason = None
    action = ACTION_HOLD
    recommended_symbol = None
    recommended_position_pct = 0.0

    held_symbol = position_state.get("symbol")
    held_qty = position_state.get("quantity") or 0
    has_position = bool(held_symbol) and held_qty > 0

    now_hm = now.strftime("%H:%M")
    eff = calculate_effective_threshold(mode, state.get("hold_streak_no_position", 0), expected_move_pct, cycle_phase, daily_return_pct)
    threshold = eff["threshold"]

    # ── 전역 차단 조건(명세 1절) ─────────────────────────────────────────────
    if not data_ok:
        blocking_reason = "데이터 오류/stale — 신규 진입 금지"
    elif position_conflict:
        blocking_reason = "Broker/Position 불일치 — 신규 진입 금지"
    # 주의: Cycle Phase NO_TRADE는 더 이상 단독 Entry Gate가 아니다 — fusion_score의
    # 작은 감점(cycle_bonus)으로만 반영되며, PredictionAI/EnhancedAI가 충분히 높으면
    # decide_fusion_based_action()의 NO_TRADE override로 오히려 시험진입이 허용된다.
    elif now_hm >= "15:00":
        blocking_reason = "15:00 이후 — 신규 진입 금지"
    elif eff["force_liquidate"]:
        blocking_reason = f"일 손실한도 도달({daily_return_pct:+.2f}%) — 전량청산 및 자동매매 중단 대상"
        action = ACTION_EXIT_ALL if has_position else ACTION_HOLD
    elif not eff["entries_allowed"] and not has_position:
        blocking_reason = f"일 손익 기준({daily_return_pct:+.2f}%) — 신규진입 중단, 보유 포지션만 관리"

    if blocking_reason and action != ACTION_EXIT_ALL:
        state = update_hold_streak(state, has_position, ACTION_HOLD, now)
        return {
            "action": ACTION_HOLD, "recommended_symbol": None, "recommended_position_pct": held_qty and 100.0 or 0.0,
            "blocking_reason": blocking_reason, "reasons": [blocking_reason], "effective_threshold": eff,
            "state": state,
        }

    # ── 보유 중: 선제매도(명세 8절) + 방향전환(명세 4절) ─────────────────────
    if has_position:
        opposite_prob = inverse_probability if held_symbol != "0197X0" else buy_probability
        exit_prob = opposite_prob  # 반대 방향 확률을 청산 확률의 근사로 사용
        entry_prob = state.get("position_entry_probability")
        prob_drop = (entry_prob - (buy_probability if held_symbol != "0197X0" else inverse_probability)) if entry_prob is not None else None

        # 빠른 방향 전환(4절): 반대 확률 + 전환확률 + inflection이 모두 강하면 스위칭
        can_switch = (
            not _is_whipsaw_dampened(state, now)
            and (not state.get("last_switch_time") or (now - _parse_iso(state["last_switch_time"])).total_seconds() >= _MIN_SWITCH_INTERVAL_SECONDS)
        )
        target_symbol = "0197X0" if held_symbol != "0197X0" else "000660"
        turn_prob_for_switch = down_turn_probability_3m if held_symbol != "0197X0" else up_turn_probability_3m
        strong_switch = (
            opposite_prob >= 76.0
            or (opposite_prob >= 68.0 and (turn_prob_for_switch or 0) >= 65.0 and (momentum_inflection_or_acceleration or 0) >= 60.0)
        )
        if can_switch and strong_switch:
            partial = opposite_prob < 76.0
            action = ACTION_SCALE_OUT_PARTIAL if partial else ACTION_SWITCH
            recommended_symbol = held_symbol
            recommended_position_pct = 50.0 if partial else 0.0
            reasons.append(f"반대방향 확률 {opposite_prob:.0f}% — {'50% 우선청산' if partial else '전량청산 후 전환'}")
            if not partial:
                state = _register_switch(state, now)

        # 선제매도(8절) — 스위칭이 아직 트리거되지 않았을 때만 별도로 평가
        elif exit_prob >= 78.0:
            action, recommended_symbol, recommended_position_pct = ACTION_EXIT_ALL, held_symbol, 0.0
            reasons.append(f"exit_probability {exit_prob:.0f}% >= 78 — 전량청산")
        elif exit_prob >= 70.0 or (turn_prob_for_switch or 0) >= 70.0:
            action, recommended_symbol, recommended_position_pct = ACTION_SCALE_OUT_PARTIAL, held_symbol, 50.0
            reasons.append("하락(반대)전환 확률 70 이상 — 50% 축소")
        elif prob_drop is not None and prob_drop >= 15.0 and (turn_prob_for_switch or 0) >= 62.0:
            # recommended_position_pct는 이 액션 전반에서 "매도할 비중(%)"으로 통일한다(70% 유지 = 30% 매도).
            action, recommended_symbol, recommended_position_pct = ACTION_SCALE_OUT_PARTIAL, held_symbol, 30.0
            reasons.append(f"진입 대비 확률 {prob_drop:.0f}%p 하락 — 30% 축소(70% 유지)")

        if action == ACTION_HOLD:
            state = update_hold_streak(state, has_position, action, now)
            reasons.append(f"{held_symbol} 보유 유지 — 전환/청산 조건 미충족")
        return {
            "action": action, "recommended_symbol": recommended_symbol or held_symbol,
            "recommended_position_pct": recommended_position_pct, "blocking_reason": None,
            "reasons": reasons, "effective_threshold": eff, "state": state,
        }

    # ── 무포지션: 재진입 쿨다운(9절) ─────────────────────────────────────────
    last_exit_time = _parse_iso(state.get("last_exit_time"))
    if last_exit_time:
        cooldown = _REENTRY_COOLDOWN_AFTER_SL_SECONDS if state.get("last_exit_was_stop_loss") else _REENTRY_COOLDOWN_SECONDS
        elapsed = (now - last_exit_time).total_seconds()
        if elapsed < cooldown:
            state = update_hold_streak(state, False, ACTION_HOLD, now)
            reason = f"직전 청산 후 재진입 쿨다운 중({int(cooldown/60)}분, {int(elapsed)}초 경과)"
            return {
                "action": ACTION_HOLD, "recommended_symbol": None, "recommended_position_pct": 0.0,
                "blocking_reason": reason, "reasons": [reason], "effective_threshold": eff, "state": state,
            }

    if state.get("round_trip_count_today", 0) >= mode_max_round_trips(mode):
        reason = f"{mode} 모드 하루 최대 왕복거래({mode_max_round_trips(mode)}회) 도달"
        state = update_hold_streak(state, False, ACTION_HOLD, now)
        return {
            "action": ACTION_HOLD, "recommended_symbol": None, "recommended_position_pct": 0.0,
            "blocking_reason": reason, "reasons": [reason], "effective_threshold": eff, "state": state,
        }

    # ── 무포지션: 신규/조기 진입 판단 — fusion_score 기반(Cycle Phase는 보조 feature) ──
    from app.models.hynix_decision_v2 import (
        calculate_fusion_score, calculate_prediction_ai_directional_score, decide_fusion_based_action,
        ACTION_BUY as _FUSION_BUY, ACTION_HOLD as _FUSION_HOLD,
    )

    prediction_ai_score = calculate_prediction_ai_directional_score(buy_probability, inverse_probability)
    momentum_ai_score = momentum_inflection_or_acceleration if momentum_inflection_or_acceleration is not None else 50.0
    fusion_result = calculate_fusion_score(
        prediction_ai_score=prediction_ai_score,
        enhanced_ai_score=enhanced_ai_score if enhanced_ai_score is not None else 50.0,
        momentum_ai_score=momentum_ai_score,
        micron_ai_score=micron_ai_score if micron_ai_score is not None else 50.0,
        cycle_phase=cycle_phase,
    )
    fusion_decision = decide_fusion_based_action(fusion_result, cycle_phase)
    reasons.append(
        f"fusion_score={fusion_result['fusion_score']:.1f} "
        f"(Prediction={prediction_ai_score:.0f} Enhanced={fusion_result['enhanced_ai_score']:.0f} "
        f"Momentum={momentum_ai_score:.0f} Micron={fusion_result['micron_ai_score']:.0f} "
        f"CycleBonus={fusion_result['cycle_bonus']:+.0f}[{cycle_phase}]) — {fusion_decision['reason']}"
    )

    symbol = "000660" if fusion_decision["action"] == _FUSION_BUY else "0197X0"
    entry_pct = fusion_decision["position_pct"]

    if fusion_decision["action"] == _FUSION_HOLD or entry_pct <= 0:
        action = ACTION_HOLD
        state = update_hold_streak(state, False, action, now)
        return {
            "action": action, "recommended_symbol": None, "recommended_position_pct": 0.0,
            "blocking_reason": None, "reasons": reasons, "effective_threshold": eff,
            "fusion_result": fusion_result, "state": state,
        }

    cap = min(eff["daily_max_position_pct"], max_total_position_pct(prediction_ai_score, model_confidence))
    entry_pct = min(entry_pct, cap)

    action = ACTION_ENTER_HYNIX if symbol == "000660" else ACTION_ENTER_INVERSE
    recommended_symbol = symbol
    recommended_position_pct = entry_pct
    state["position_entry_probability"] = prediction_ai_score
    state = update_hold_streak(state, False, action, now)

    return {
        "action": action, "recommended_symbol": recommended_symbol, "recommended_position_pct": recommended_position_pct,
        "blocking_reason": None, "reasons": reasons, "effective_threshold": eff,
        "fusion_result": fusion_result, "state": state,
    }


def evaluate_scale_in(
    now: datetime, position_state: dict, strategy_state: dict, current_probability: float,
    opposite_probability: float, momentum_continuing: bool, current_price: float,
) -> dict:
    """명세 3절 — 시험진입 후 추가매수(Scale-In) 평가. 최대 3회, 90초 최소 간격."""
    state = dict(strategy_state)
    entry_time = _parse_iso(state.get("position_entry_time"))
    entry_price = state.get("position_entry_price")
    scale_in_count = state.get("scale_in_count", 0)

    if scale_in_count >= _MAX_SCALE_INS:
        return {"approved": False, "reason": f"Scale-in 최대 {_MAX_SCALE_INS}회 도달", "target_pct": None, "state": state}
    if not entry_time or (now - entry_time).total_seconds() < _MIN_SCALE_IN_GAP_SECONDS:
        return {"approved": False, "reason": "최초/직전 진입 후 90초 미경과", "target_pct": None, "state": state}
    if entry_price and current_price:
        adverse_pct = (current_price / entry_price - 1.0) * 100.0
        symbol = position_state.get("symbol")
        # 인버스는 가격 하락이 유리하므로 부호를 뒤집어 "불리한 방향" 여부를 판정한다.
        if symbol == "0197X0":
            adverse_pct = -adverse_pct
        if adverse_pct <= -_ADVERSE_MOVE_LIMIT_PCT:
            return {"approved": False, "reason": f"진입가 대비 불리하게 {adverse_pct:.2f}% 이동 — 추가매수 금지", "target_pct": None, "state": state}
    if not momentum_continuing:
        return {"approved": False, "reason": "동일 방향 모멘텀 미유지", "target_pct": None, "state": state}
    if opposite_probability >= 40.0:
        return {"approved": False, "reason": f"반대 방향 확률 {opposite_probability:.0f}% >= 40 — 추가매수 금지", "target_pct": None, "state": state}

    target_pct = calculate_scale_up_target_pct(current_probability)
    if target_pct <= state.get("position_current_pct", 0.0):
        return {"approved": False, "reason": "목표 비중이 현재 비중 이하 — 확대 불필요", "target_pct": None, "state": state}

    state["scale_in_count"] = scale_in_count + 1
    return {"approved": True, "reason": f"확률 {current_probability:.0f}% — 총 {target_pct:.0f}%까지 확대", "target_pct": target_pct, "state": state}


def register_position_opened(state: dict, symbol: str, entry_price: float, position_pct: float, now: datetime) -> dict:
    state = dict(state)
    state["position_symbol"] = symbol
    state["position_entry_price"] = entry_price
    state["position_entry_time"] = now.isoformat()
    state["position_current_pct"] = position_pct
    state["scale_in_count"] = 0
    return state


def register_position_closed(state: dict, was_stop_loss: bool, now: datetime) -> dict:
    state = dict(state)
    state["last_exit_time"] = now.isoformat()
    state["last_exit_was_stop_loss"] = bool(was_stop_loss)
    state["last_exit_symbol"] = state.get("position_symbol")
    state["position_symbol"] = None
    state["position_entry_price"] = None
    state["position_entry_time"] = None
    state["position_entry_probability"] = None
    state["position_current_pct"] = 0.0
    state["scale_in_count"] = 0
    state["round_trip_count_today"] = state.get("round_trip_count_today", 0) + 1
    state["consecutive_stop_losses"] = (state.get("consecutive_stop_losses", 0) + 1) if was_stop_loss else 0
    return state
