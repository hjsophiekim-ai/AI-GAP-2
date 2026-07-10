"""
hynix_switch_engine.py — 하이닉스⇄인버스 Enhanced 자동매매 오케스트레이터.

3분마다(또는 UI 자동새로고침 주기마다) 아래 순서를 반복한다:
① kospilab 갱신 ② 마이크론 실시간 갱신 ③~⑥ 점수/판단 계산 ⑦ 보유종목 확인
⑧ 강제청산/TP·SL/스위칭 실행 ⑨ 로그 기록 ⑩ 결과 반환(UI 렌더링용).

각 단계는 개별 try/except로 감싸 부분 실패해도 나머지는 계속 진행한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.logger import logger
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
from app.services.hynix_switch_state import load_state, save_state_atomic, set_active_mode, reset_mock_state
from app.services.hynix_switch_logger import log_enhanced_prediction, log_trade
from app.trading.hynix_switch_risk_gate import (
    is_watch_only, is_new_entry_allowed, get_liquidation_phase,
    should_force_trade, _parse_hm,
)
from app.trading.hynix_switch_position_manager import (
    run_liquidation_if_needed, run_tp_sl_if_needed, run_switch_or_entry, _current_price, _ACTION_TO_SYMBOL,
    apply_position_manager_to_state,
)
from app.trading.hynix_position_common import HynixPositionManager
from app.trading.hynix_pullback_entry import detect_pullback

_PULLBACK_MORNING_WINDOW_END = "10:00"
_PULLBACK_PATIENCE_MINUTES = 15

_SIGNAL_DISPLAY_MAP = {
    "HYNIX_BUY": "BUY", "HYNIX_STRONG_BUY": "BUY",
    "INVERSE_BUY": "INVERSE", "INVERSE_STRONG_BUY": "INVERSE",
    "HOLD": "HOLD",
}

# 파이프라인 트레이스 단계 순서 — UI가 "어디서 멈췄는지"를 이 순서로 판정한다.
_PIPELINE_STAGES = [
    "prediction_signal", "entry_approved", "risk_manager", "order_sent",
    "broker_executed", "position_confirmed", "ui_synced",
]


def _map_prediction_signal(final_action: str) -> str:
    return _SIGNAL_DISPLAY_MAP.get(final_action, "HOLD")


def _blank_pipeline_trace() -> dict:
    """Signal 생성과 실제 체결을 분리해서 보여주기 위한 단계별 추적 정보의 기본값.

    각 단계는 True(성공/승인)/False(실패/차단)/None(해당 없음, 예: HOLD라 진입 자체를
    시도하지 않음) 중 하나이며, `stopped_stage`는 그 중 실제로 막힌 첫 단계 이름이다
    (UI가 여기서 빨간색으로 표시한다).
    """
    return {
        "prediction_signal": "HOLD",
        "entry_approved": None, "entry_approved_reason": "",
        "risk_manager_ok": True, "risk_manager_reason": "정상",
        "risk_approved": True,
        "order_sent": False,
        "broker_executed": False,
        "position_confirmed": None,
        "ui_synced": None,
        "trade_counter": 0,
        "stopped_stage": None,
        "blocking_reason": None,
    }


def _first_blocked_stage(trace: dict) -> Optional[str]:
    """Signal이 BUY/SELL/INVERSE인데 실제로 어느 단계에서 멈췄는지 첫 번째로 찾는다.

    HOLD는 애초에 아무것도 시도하지 않는 게 정상이므로 항상 None(정상)이다.
    """
    if trace["prediction_signal"] == "HOLD":
        return None
    if trace["entry_approved"] is False:
        return "entry_approved"
    if not trace["risk_manager_ok"]:
        return "risk_manager"
    if not trace["order_sent"]:
        return "order_sent"
    if not trace["broker_executed"]:
        return "broker_executed"
    if trace["position_confirmed"] is False:
        return "position_confirmed"
    if trace["ui_synced"] is False:
        return "ui_synced"
    return None


def _build_blocking_reason(trace: dict) -> Optional[str]:
    """stopped_stage를 사람이 읽을 수 있는 한 줄 사유로 변환 (UI의 blocking_reason 필드)."""
    stage = trace.get("stopped_stage")
    if not stage:
        return None
    reason_map = {
        "entry_approved": trace.get("entry_approved_reason"),
        "risk_manager": trace.get("risk_manager_reason"),
        "order_sent": "주문이 브로커로 전송되지 않음(가격 조회 실패/쿨다운/허용 시간대 아님 등)",
        "broker_executed": "주문은 전송됐으나 브로커 체결 실패",
        "position_confirmed": "체결 후 재조회한 포지션이 기대와 불일치",
        "ui_synced": "상태 저장(디스크 반영) 실패 — 다음 사이클에서 재시도됨",
    }
    return f"[{stage}] {reason_map.get(stage) or '알 수 없음'}"


def evaluate_pullback_gate(state: dict, desired_symbol: str, final_action: str, now: datetime, forced_info: dict, hynix_df_1min, mode: str) -> dict:
    """신규 진입(매수) 전 눌림목 부근인지 확인한다.

    09:10~10:00 구간은 그 창이 끝날 때까지, 그 외 시간대는 신호 발생 후 최대
    `_PULLBACK_PATIENCE_MINUTES`분까지 눌림목을 기다린다. 강제거래창이 먼저
    끝나면 그 마감시각을 데드라인으로 우선한다. 데드라인 도달 시 무조건 진입(진행)한다.
    """
    pending = state.get("pending_entry")
    if not pending or pending.get("action") != final_action or pending.get("symbol") != desired_symbol:
        pending = {"action": final_action, "symbol": desired_symbol, "since": now.isoformat()}
        state["pending_entry"] = pending

    try:
        since = datetime.fromisoformat(pending["since"])
    except Exception:
        since = now

    signal_started_in_morning_window = _parse_hm("09:10") <= since.time() < _parse_hm(_PULLBACK_MORNING_WINDOW_END)
    if signal_started_in_morning_window:
        deadline = datetime.combine(since.date(), _parse_hm(_PULLBACK_MORNING_WINDOW_END))
    else:
        deadline = since + timedelta(minutes=_PULLBACK_PATIENCE_MINUTES)

    window = forced_info.get("window")
    if window:
        try:
            _, end_str = window.split("-")
            window_deadline = datetime.combine(now.date(), _parse_hm(end_str))
            deadline = min(deadline, window_deadline)
        except Exception:
            pass

    if now >= deadline:
        return {"proceed": True, "message": f"눌림목 대기 데드라인({deadline.strftime('%H:%M')}) 도달 — 강제 진입"}

    if desired_symbol == HYNIX_SYMBOL:
        df_for_check = hynix_df_1min
    else:
        df_for_check = _load_inverse_1min_for_pullback(mode)

    pullback = detect_pullback(df_for_check)
    if pullback.get("is_pullback"):
        return {"proceed": True, "message": f"눌림목 진입 조건 충족: {pullback.get('reason')}"}
    return {
        "proceed": False,
        "message": f"눌림목 대기 중({pullback.get('reason')}) — 데드라인 {deadline.strftime('%H:%M')}까지 대기",
    }


def _load_inverse_1min_for_pullback(mode: str):
    try:
        from app.data_sources.hynix_inverse_collector import collect_inverse_minute

        result = collect_inverse_minute(mode=mode)
        return result.get("df_1min")
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 인버스 1분봉 조회 실패(눌림목 판단용): %s", exc)
        return None


def set_control(
    auto_trade_on: Optional[bool] = None, mode: Optional[str] = None,
    allow_mock_loss_override: Optional[bool] = None, mock_budget_krw: Optional[float] = None,
) -> dict:
    """UI에서 자동매매 ON/OFF, mock/real 모드, mock 예산을 설정할 때 사용."""
    if mode is not None:
        set_active_mode(mode)
    state = load_state(mode=mode)
    if auto_trade_on is not None:
        state["auto_trade_on"] = bool(auto_trade_on)
    if mode is not None:
        state["mode"] = mode
    if allow_mock_loss_override is not None:
        state["allow_mock_loss_override"] = bool(allow_mock_loss_override)
    if mock_budget_krw is not None:
        state["mock_budget_krw"] = float(mock_budget_krw)
        if state.get("mode") == "mock" and not (state.get("position") or {}).get("symbol"):
            state["cash"] = float(mock_budget_krw)
    save_state_atomic(state)
    return state


def reset_mock_account(budget_krw: Optional[float] = None) -> dict:
    """UI의 'mock 계좌 초기화' 버튼 — 포지션/거래횟수/현금을 오늘자 기준으로 완전히 새로 시작."""
    return reset_mock_state(budget_krw=budget_krw)


def _daily_pnl_pct(state: dict, total_equity: Optional[float]) -> Optional[float]:
    if total_equity is None:
        return None
    baseline = state.get("daily_pnl_baseline_equity")
    if not baseline:
        state["daily_pnl_baseline_equity"] = total_equity
        return 0.0
    if baseline <= 0:
        return 0.0
    return (total_equity / baseline - 1.0) * 100.0


def _run_active_strategy_entry(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, orders_this_cycle: list,
) -> dict:
    """ACTIVE STRATEGY(거래모드 기반 조기진입/Scale-in/빠른전환) — mock 전용 opt-in.

    state["active_strategy_enabled"]가 True이고 mode=="mock"일 때만 호출부에서
    호출된다(real 모드에서는 절대 호출되지 않음 — 호출부에서 이미 mode=="mock"을
    확인). 기존 ENHANCED_LEGACY 진입 로직(run_switch_or_entry)을 이번 사이클만
    대체하며, 같은 브로커/포지션 파이프라인(_buy_new/_sell_all_or_ratio → 실행
    원장)을 그대로 사용하되 signal_source="ACTIVE_STRATEGY_MOCK"으로 구분 기록한다.
    """
    from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio
    from app.trading.hynix_active_strategy_engine import (
        decide_active_strategy_action, default_active_strategy_state,
        register_position_opened, register_position_closed,
        ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE, ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH,
    )
    from app.trading.hynix_trading_mode import DEFAULT_MODE

    shadow = state.get("last_cycle_ai_result") or {}
    cyc = shadow.get("cycle") or {}
    prob = shadow.get("probability") or {}
    turning_point = cyc.get("turning_point") or {}
    momentum = cyc.get("momentum") or {}

    mode_name = state.get("trading_mode", DEFAULT_MODE)
    strategy_state = state.get("active_strategy_state") or default_active_strategy_state(mode_name)

    position = state.get("position") or {}
    position_state = {
        "symbol": position.get("symbol"), "quantity": position.get("quantity") or 0,
        "entry_price": position.get("entry_price"),
    }

    # expected_move_pct — 전용 예측 필드가 아직 없어 momentum의 최근 3분 속도를 근사치로
    # 사용한다(정밀한 값이 아니라 "과도한 진입/완화를 막는 최소 안전장치" 목적).
    raw_vel = momentum.get("raw_velocity_3")
    expected_move_pct = round(abs(raw_vel) * 2.0, 3) if raw_vel is not None else 0.25

    decision_result = decide_active_strategy_action(
        mode=mode_name, now=now,
        buy_probability=prob.get("buy_probability", 0.0), inverse_probability=prob.get("sell_probability", 0.0),
        hold_probability=prob.get("hold_probability", 100.0),
        model_confidence=turning_point.get("confidence", 50.0), expected_move_pct=expected_move_pct,
        down_turn_probability_3m=turning_point.get("down_turn_probability_3m"),
        up_turn_probability_3m=turning_point.get("up_turn_probability_3m"),
        momentum_inflection_or_acceleration=momentum.get("momentum_acceleration_up"),
        cycle_phase=cyc.get("cycle_phase"), order_flow_confidence=None,
        atr_pct=None, consecutive_stop_losses=strategy_state.get("consecutive_stop_losses", 0),
        recent_pnl_pct=state.get("realized_pnl_today_pct"), daily_return_pct=state.get("realized_pnl_today_pct"),
        position_state=position_state, strategy_state=strategy_state,
        data_ok=bool(hynix_price and inverse_price), position_conflict=bool(state.get("position_conflict")),
    )
    state["active_strategy_state"] = decision_result["state"]
    action = decision_result["action"]
    acted = False
    message = decision_result.get("blocking_reason") or "; ".join(decision_result.get("reasons", [])) or "HOLD"

    if action in (ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE):
        symbol = decision_result["recommended_symbol"]
        price = _current_price(symbol, hynix_price, inverse_price)
        pct = decision_result["recommended_position_pct"]
        if price and pct > 0:
            try:
                full_cash = float(broker.get_buyable_cash())
            except Exception:
                full_cash = 0.0
            cash_amount = full_cash * (pct / 100.0)
            buy_result = _buy_new(
                broker, symbol, price, cash_amount, f"Active Strategy({mode_name}) 진입 {pct:.0f}%",
                orders_this_cycle, mode="mock", signal_source="ACTIVE_STRATEGY_MOCK",
            )
            if buy_result.get("success"):
                acted = True
                qty = buy_result.get("bought_quantity", 0)
                state["position"] = {
                    "symbol": symbol, "name": symbol, "quantity": qty, "avg_price": price,
                    "entry_price": price, "entry_time": now.isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
                }
                state["active_strategy_state"] = register_position_opened(state["active_strategy_state"], symbol, price, pct, now)
                message = f"Active Strategy 진입: {symbol} {pct:.0f}%({qty}주)"

    elif action in (ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH) and position_state.get("symbol"):
        symbol = position_state["symbol"]
        price = _current_price(symbol, hynix_price, inverse_price)
        ratio = 1.0 if action in (ACTION_EXIT_ALL, ACTION_SWITCH) else max(0.01, min(1.0, decision_result["recommended_position_pct"] / 100.0))
        if price:
            sell_result = _sell_all_or_ratio(
                broker, position, price, ratio, f"Active Strategy({mode_name}) {action}", orders_this_cycle,
                mode="mock", exit_reason_type="active_strategy", signal_source="ACTIVE_STRATEGY_MOCK",
            )
            if sell_result.get("success"):
                acted = True
                remaining = sell_result.get("remaining_quantity", 0)
                if remaining <= 0:
                    state["position"] = {
                        "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
                        "entry_time": None, "name": None, "partial_tp1_done": False, "partial_sl1_done": False,
                    }
                    state["active_strategy_state"] = register_position_closed(state["active_strategy_state"], was_stop_loss=False, now=now)
                else:
                    state["position"]["quantity"] = remaining
                message = f"Active Strategy 청산/축소: {symbol} {action} (비중 {ratio*100:.0f}%)"

    return {"acted": acted, "message": message, "action": action, "decision": decision_result}


def _run_shadow_cycle_ai_and_decision_v2(
    state: dict, enhanced_result: dict, decision: dict, df_1min, hynix_price, inverse_price, now: datetime,
) -> Optional[dict]:
    """SHADOW MODE — Cycle Detector AI + Prediction AI V2(BUY/SELL/HOLD 확률+Adaptive
    Threshold)를 기존 enhanced_score 기반 실제 주문 흐름과 나란히 계산·기록만 한다.

    이 함수의 결과는 어떤 이유로도 `decision`/실제 주문 실행에 영향을 주지 않는다 —
    명세(Cycle Detector 17절)가 요구하는 최소 5거래일 Shadow Mode 검증을 위한 것이며,
    실제 주문 연결은 별도의 명시적 승인 이후에만 이루어진다. 실패해도 예외를 삼키고
    None을 반환한다(호출부 로직에 영향 없음).
    """
    try:
        from app.trading.hynix_cycle_detector import (
            HynixCycleDetector, default_cycle_state, log_cycle_ai_prediction,
        )
        from app.models.hynix_decision_v2 import (
            compute_buy_sell_hold_probability, decide_final_action_v2, adaptive_threshold_update,
            default_threshold_state,
        )
        from app.models.micron_proxy_prediction import compute_effective_micron_score_from_market_data

        market_data = enhanced_result.get("market_data") or {}
        micron_proxy = compute_effective_micron_score_from_market_data(market_data, now=now)
        effective_micron_score = micron_proxy.get("effective_micron_score")
        korea_score = micron_proxy.get("korea_semiconductor_confirmation_score")

        gap_pct = None
        session_high = session_low = None
        prior_close = enhanced_result.get("hynix_prev_close")
        if df_1min is not None and not df_1min.empty:
            session_high = float(df_1min["high"].max())
            session_low = float(df_1min["low"].min())
            if prior_close:
                gap_pct = (float(df_1min["open"].iloc[0]) / prior_close - 1.0) * 100.0

        position = state.get("position") or {}
        position_state = {
            "symbol": position.get("symbol"),
            "position_pct": 100.0 if (position.get("quantity") or 0) > 0 else 0.0,
        }

        cycle_state = state.get("cycle_ai_state") or default_cycle_state()
        cycle_result = HynixCycleDetector().run(
            df_1min, now, position_state=position_state, state=cycle_state,
            gap_pct=gap_pct, session_high=session_high, session_low=session_low, prior_close=prior_close,
            inverse_pressure_score=decision.get("inverse_pressure_score"),
            korea_semiconductor_confirmation_score=korea_score, effective_micron_score=effective_micron_score,
        )
        state["cycle_ai_state"] = cycle_result["state"]

        threshold_state = state.get("decision_v2_threshold_state") or default_threshold_state()
        probability = compute_buy_sell_hold_probability(
            cycle_result["turning_point"], enhanced_score=decision.get("enhanced_score"),
            effective_micron_score=effective_micron_score,
        )
        decision_v2 = decide_final_action_v2(probability, threshold_state)
        state["decision_v2_threshold_state"] = adaptive_threshold_update(
            threshold_state, decision_v2["final_action_v2"], now,
        )

        # 우선순위 스택(Cycle Phase → Turning Point → Momentum → Entry Timing → Enhanced →
        # Effective Micron): Cycle Detector가 HOLD가 아닌 액션을 냈으면 그것을 combined 액션으로,
        # 아니면 확률 기반 decision_v2의 액션을 combined 액션으로 삼는다(로그/UI 비교용일 뿐,
        # 실제 주문에는 반영되지 않음).
        combined_action = cycle_result["action"] if cycle_result["action"] != "HOLD" else decision_v2["final_action_v2"]

        shadow_result = {
            "cycle": cycle_result, "probability": probability, "decision_v2": decision_v2,
            "combined_shadow_action": combined_action, "effective_micron_score": effective_micron_score,
            "korea_semiconductor_confirmation_score": korea_score,
        }
        state["last_cycle_ai_result"] = shadow_result

        reasons = (cycle_result.get("reasons") or [])[:3]
        log_cycle_ai_prediction({
            "timestamp": now.isoformat(timespec="seconds"), "hynix_price": hynix_price, "inverse_price": inverse_price,
            "cycle_phase": cycle_result.get("cycle_phase"), "previous_cycle_phase": cycle_result.get("previous_cycle_phase"),
            "phase_duration_seconds": cycle_result.get("phase_duration_seconds"),
            "momentum_velocity": (cycle_result.get("momentum") or {}).get("raw_velocity_3"),
            "momentum_acceleration_up": (cycle_result.get("momentum") or {}).get("momentum_acceleration_up"),
            "momentum_acceleration_down": (cycle_result.get("momentum") or {}).get("momentum_acceleration_down"),
            "early_reversal_score": (cycle_result.get("state", {}) or {}).get("early_reversal_score"),
            "up_turn_3m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_3m"),
            "up_turn_5m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_5m"),
            "up_turn_10m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_10m"),
            "down_turn_3m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_3m"),
            "down_turn_5m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_5m"),
            "down_turn_10m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_10m"),
            "cycle_confidence": cycle_result.get("cycle_confidence"),
            "cycle_entry_score": max((cycle_result.get("entry_scores") or {}).values(), default=None),
            "enhanced_score": decision.get("enhanced_score"), "effective_micron_score": effective_micron_score,
            "recommended_symbol": cycle_result.get("recommended_symbol"),
            "recommended_position_pct": cycle_result.get("recommended_position_pct"),
            "final_action": combined_action, "order_sent": False, "order_executed": False,
            "reason_top1": reasons[0] if len(reasons) > 0 else "",
            "reason_top2": reasons[1] if len(reasons) > 1 else "",
            "reason_top3": reasons[2] if len(reasons) > 2 else "",
        })
        return shadow_result
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] Shadow Cycle AI/Decision V2 계산 실패(무해, 실거래에 영향 없음): %s", exc)
        return None


def update_hynix_auto_trade_loop(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """1회 실행 사이클의 공개 진입점 — mode별 state 락으로 감싼 얇은 wrapper.

    백그라운드 3분 사이클 스레드, Dynamic Exit Watcher(1초 주기), Streamlit 수동 실행이
    모두 같은 mode의 state를 동시에 load→수정→save할 수 있어(lost update 위험 — 실제로
    2026-07-10 부분손절 손익 1건이 이렇게 누락된 사고가 있었다), 이 함수 호출 전체를
    mode별 락(app.services.hynix_switch_state.with_state_lock)으로 직렬화한다.
    """
    from app.services.hynix_switch_state import with_state_lock

    resolved_mode = mode
    if resolved_mode is None:
        resolved_mode = load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        return _update_hynix_auto_trade_loop_locked(mode=mode, now=now)


def _update_hynix_auto_trade_loop_locked(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """1회 실행 사이클의 실제 구현(반드시 with_state_lock(mode) 안에서만 호출).

    `now`는 테스트에서 시각을 주입하기 위한 선택 인자이며, 운영 시에는 항상 현재시각이 쓰인다.
    """
    warnings: list[str] = []
    now = now or datetime.now()
    state = load_state(mode=mode)
    mode = mode or state.get("mode", "mock")
    state["mode"] = mode

    if state.get("stopped"):
        trace = _blank_pipeline_trace()
        trace["risk_manager_ok"] = False
        trace["risk_manager_reason"] = state.get("stopped_reason") or "자동매매 정지 상태"
        trace["risk_approved"] = False
        trace["stopped_stage"] = "risk_manager"
        trace["blocking_reason"] = f"[risk_manager] {trace['risk_manager_reason']}"
        return {
            "skipped": True, "reason": state.get("stopped_reason") or "자동매매 정지 상태", "state": state,
            "pipeline_trace": trace,
        }

    trace = _blank_pipeline_trace()

    # ── ①~⑥ 점수/판단 계산 (기존 데이터 흐름 재사용) ────────────────────────
    try:
        from app.models.hynix_enhanced_score import calculate_enhanced_hynix_prediction_score

        enhanced_result = calculate_enhanced_hynix_prediction_score(mode=mode)
    except Exception as exc:
        logger.error("[HynixSwitchEngine] enhanced_score 계산 실패: %s", exc)
        warnings.append(f"enhanced_score 계산 실패: {exc}")
        enhanced_result = {
            "base_prediction_score": 50.0, "existing_micron_score": 50.0,
            "hynix_technical_score": 50.0, "intraday_momentum_score": 50.0,
            "inverse_pressure_score": 50.0, "enhanced_score": 50.0,
            "reason_top5": [], "data_valid": {"base_prediction": False, "hynix_technical": False},
            "hynix_current_price": None, "inverse_current_price": None, "warnings": [str(exc)],
        }

    try:
        from app.models.hynix_action_decider import decide_hynix_or_inverse_action

        decision = decide_hynix_or_inverse_action(enhanced_result, current_position=state.get("position"))
    except Exception as exc:
        logger.error("[HynixSwitchEngine] action_decider 실패: %s", exc)
        warnings.append(f"action_decider 실패: {exc}")
        decision = {"final_action": "HOLD", "enhanced_score": enhanced_result.get("enhanced_score", 50.0),
                    "inverse_pressure_score": enhanced_result.get("inverse_pressure_score", 50.0),
                    "score_gap": 0.0, "score_gap_below_forced_trade_threshold": True, "reasons": [str(exc)]}

    trace["prediction_signal"] = _map_prediction_signal(decision.get("final_action", "HOLD"))

    hynix_price = enhanced_result.get("hynix_current_price")
    inverse_price = enhanced_result.get("inverse_current_price")
    df_1min = (enhanced_result.get("market_data") or {}).get("hynix_minute", {}).get("df_1min")

    # ── SHADOW MODE: Cycle Detector AI + Prediction AI V2(BUY/SELL/HOLD 확률) ──
    # 아래 호출은 `decision`/실제 주문에 절대 영향을 주지 않는다 — 계산·로그·state 저장만
    # 수행하며, 예외가 나도 무해하게 삼켜진다. 실제 주문 연결은 별도 승인 후 진행한다.
    _run_shadow_cycle_ai_and_decision_v2(state, enhanced_result, decision, df_1min, hynix_price, inverse_price, now)

    price_data_ok = hynix_price is not None
    order_api_ok = True
    broker = None
    real_gate_ok = True

    auto_trade_on = bool(state.get("auto_trade_on"))
    position_manager = None
    if auto_trade_on:
        try:
            if mode == "real":
                from app.config import get_config
                from app.trading.broker_factory import create_broker

                cfg = get_config()
                real_gate_ok = cfg.full_auto_real_confirm_ok()
                if not real_gate_ok:
                    warnings.append("REAL 완전자동 게이트 미충족(safety.enable_real_trading / FULL_AUTO_REAL_CONFIRM_TEXT) — 주문 실행 생략")
                if real_gate_ok:
                    broker = create_broker(
                        cfg, mode="real",
                        runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
                    )
            else:
                # mock은 KIS 모의투자 서버(계좌 권한/외부 상태에 의존)를 거치지 않고,
                # 사용자가 설정한 예산으로 완전히 로컬에서 동작하는 DryRunBroker를 사용한다.
                # → KIS 모의계좌 승인/장시간 이슈와 무관하게 항상 자동매매가 동작한다.
                from app.trading.dry_run_broker import DryRunBroker

                broker = DryRunBroker(initial_balance=float(state.get("mock_budget_krw", 10_000_000.0)))

            if broker is not None:
                # Broker가 유일한 Source of Truth — position_manager.sync()로 실제 포지션을
                # 먼저 확정하고, state는 그 결과를 담는 캐시로만 갱신한다.
                position_manager = HynixPositionManager(broker, mode=mode)
                position_manager.sync(force=True)
                apply_position_manager_to_state(state, position_manager)
                if state.get("position_conflict"):
                    warnings.append(state.get("critical_alert") or "000660/0197X0 동시 보유 감지 — 신규매수 금지")
        except Exception as exc:
            order_api_ok = False
            warnings.append(f"브로커 초기화 실패: {exc}")
            logger.error("[HynixSwitchEngine] 브로커 초기화 실패: %s", exc)

    total_equity = None
    daily_pnl_pct = None
    if broker is not None:
        try:
            positions = broker.get_positions()
            cash = broker.get_buyable_cash()
            total_equity = float(cash) + sum(p.market_value for p in positions)
            is_mock_override = mode == "mock" and state.get("allow_mock_loss_override")
            daily_pnl_pct = _daily_pnl_pct(state, total_equity)
            if daily_pnl_pct is not None and daily_pnl_pct <= -2.5 and mode == "real":
                state["stopped"] = True
                state["stopped_reason"] = f"일 누적 손실 {daily_pnl_pct:.2f}% ≤ -2.5% — REAL 자동매매 강제 중단"
                logger.error(state["stopped_reason"])
            elif daily_pnl_pct is not None and daily_pnl_pct <= -2.5 and mode == "mock" and not is_mock_override:
                state["stopped"] = True
                state["stopped_reason"] = f"일 누적 손실 {daily_pnl_pct:.2f}% ≤ -2.5% — MOCK 자동매매 중단(설정에서 계속 테스트 가능)"
        except Exception as exc:
            order_api_ok = False
            warnings.append(f"계좌 조회 실패: {exc}")

    fired_windows = state.get("fired_windows", [])
    forced_info = should_force_trade(
        decision, fired_windows, price_data_ok, order_api_ok, df_1min, daily_pnl_pct, now=now,
    )

    liquidation_phase_now = get_liquidation_phase(now)

    orders_this_cycle: list = []
    attempted_entry = False
    trading_allowed = auto_trade_on and real_gate_ok and not state.get("stopped") and not is_watch_only(now) and broker is not None

    if not trading_allowed:
        trace["risk_manager_ok"] = False
        if state.get("stopped"):
            trace["risk_manager_reason"] = state.get("stopped_reason") or "자동매매 중단 상태"
        elif not auto_trade_on:
            trace["risk_manager_reason"] = "자동매매 OFF"
        elif not real_gate_ok:
            trace["risk_manager_reason"] = "REAL 완전자동 게이트 미충족"
        elif is_watch_only(now):
            trace["risk_manager_reason"] = "관찰 전용 시간대(watch-only)"
        elif broker is None:
            trace["risk_manager_reason"] = "브로커 초기화 실패"
    elif state.get("position_conflict"):
        trace["risk_manager_ok"] = False
        trace["risk_manager_reason"] = state.get("critical_alert") or "000660/0197X0 동시 보유 — 포지션 동기화 필요"

    if trading_allowed:
        try:
            liq = run_liquidation_if_needed(now, state, broker, hynix_price, inverse_price, position_manager=position_manager)
            orders_this_cycle.extend(liq.get("orders", []))
        except Exception as exc:
            logger.error("[HynixSwitchEngine] 강제청산 처리 실패: %s", exc)
            warnings.append(f"강제청산 처리 실패: {exc}")
            liq = {"liquidated": False}

        if liquidation_phase_now == "closed" and not liq.get("liquidated"):
            warnings.append("15:20 이후 — 신규 주문 판단 없이 상태 정리만 수행")
        elif not liq.get("liquidated"):
            try:
                tp_sl = run_tp_sl_if_needed(state, broker, hynix_price, inverse_price, position_manager=position_manager, now=now)
                orders_this_cycle.extend(tp_sl.get("orders", []))
            except Exception as exc:
                logger.error("[HynixSwitchEngine] TP/SL 처리 실패: %s", exc)
                warnings.append(f"TP/SL 처리 실패: {exc}")
                tp_sl = {"triggered": False}

            if not tp_sl.get("triggered") and mode == "mock" and state.get("active_strategy_enabled"):
                # ── ACTIVE STRATEGY(거래모드 기반) — mock 전용 opt-in, 이번 사이클의
                # 신규진입/전환 판단을 기존 ENHANCED_LEGACY 대신 이 엔진이 담당한다.
                # 강제청산/레거시 TP·SL은 위에서 이미 항상 우선 실행되었으므로 안전망은 유지된다.
                try:
                    active_result = _run_active_strategy_entry(state, broker, hynix_price, inverse_price, now, orders_this_cycle)
                    trace["entry_approved"] = active_result.get("acted", False)
                    trace["entry_approved_reason"] = f"[ACTIVE_STRATEGY] {active_result.get('message', '')}"
                    if active_result.get("acted"):
                        attempted_entry = True
                        state.pop("pending_entry", None)
                except Exception as exc:
                    logger.error("[HynixSwitchEngine] Active Strategy 진입 처리 실패: %s", exc)
                    warnings.append(f"Active Strategy 진입 처리 실패: {exc}")
            elif not tp_sl.get("triggered"):
                final_action = decision.get("final_action", "HOLD")
                forced = False
                reason = "; ".join(decision.get("reasons", []))
                if final_action == "HOLD" and forced_info.get("should_force"):
                    final_action = forced_info.get("forced_direction") or "HOLD"
                    forced = True
                    reason = f"강제거래창({forced_info.get('window')}) — {reason}"

                if final_action != "HOLD":
                    held_symbol = (state.get("position") or {}).get("symbol")
                    desired_symbol = _ACTION_TO_SYMBOL.get(final_action)
                    is_new_entry = desired_symbol is not None and held_symbol != desired_symbol

                    proceed = True
                    if not is_new_entry:
                        trace["entry_approved"] = True
                        trace["entry_approved_reason"] = "이미 목표 종목 보유 중 — 추가 진입 불필요"
                    elif state.get("position_conflict"):
                        proceed = False
                        warnings.append("포지션 동기화 필요(000660/0197X0 동시 보유) — 신규매수 금지")
                        trace["entry_approved"] = False
                        trace["entry_approved_reason"] = "포지션 동기화 필요(동시 보유) — 신규매수 금지"
                    else:
                        try:
                            gate = evaluate_pullback_gate(state, desired_symbol, final_action, now, forced_info, df_1min, mode)
                            proceed = gate["proceed"]
                            trace["entry_approved"] = proceed
                            trace["entry_approved_reason"] = gate["message"]
                            if not proceed:
                                warnings.append(gate["message"])
                        except Exception as exc:
                            logger.error("[HynixSwitchEngine] 눌림목 게이트 판단 실패, 즉시 진입으로 폴백: %s", exc)
                            proceed = True
                            trace["entry_approved"] = True
                            trace["entry_approved_reason"] = f"눌림목 게이트 오류로 즉시 진입 폴백: {exc}"

                    if proceed:
                        attempted_entry = True
                        state.pop("pending_entry", None)
                        try:
                            switch = run_switch_or_entry(
                                state, broker, final_action, hynix_price, inverse_price,
                                now=now, forced=forced, reason=reason,
                            )
                            orders_this_cycle.extend(switch.get("orders", []))
                        except Exception as exc:
                            logger.error("[HynixSwitchEngine] 스위칭/진입 처리 실패: %s", exc)
                            warnings.append(f"스위칭/진입 처리 실패: {exc}")
                else:
                    state.pop("pending_entry", None)
                    trace["entry_approved_reason"] = "HOLD — 신규 진입 신호 없음"

        if forced_info.get("should_force") and forced_info.get("window") and attempted_entry:
            if forced_info["window"] not in fired_windows:
                fired_windows.append(forced_info["window"])
                state["fired_windows"] = fired_windows

        # 이번 사이클에 주문을 실행했다면, "확정된 것으로 추정한 상태"가 아니라 브로커에
        # 실제로 무엇이 체결됐는지 다시 확인하고 그 결과로 state(캐시)를 갱신한다.
        # (buy()/sell() → broker.positions 갱신 → get_positions() → position_manager.sync() → state 캐시)
        if orders_this_cycle and position_manager is not None:
            try:
                position_manager.sync(force=True)
                apply_position_manager_to_state(state, position_manager)
            except Exception as exc:
                logger.error("[HynixSwitchEngine] 주문 후 포지션 재확인 실패: %s", exc)
                warnings.append(f"주문 후 포지션 재확인 실패: {exc}")

    # ── Order Sent / Broker Executed / Position Confirmed 판정 ──────────────
    sent_orders = [o for o in orders_this_cycle if o.get("action") in ("BUY", "SELL")]
    trace["order_sent"] = bool(sent_orders)
    trace["broker_executed"] = any(o.get("success") for o in sent_orders)
    if sent_orders:
        last_order = sent_orders[-1]
        if not last_order.get("success"):
            trace["position_confirmed"] = False
        elif last_order.get("action") == "BUY":
            pos_now = state.get("position") or {}
            trace["position_confirmed"] = (
                pos_now.get("symbol") == last_order.get("symbol") and (pos_now.get("quantity") or 0) > 0
            )
        else:  # SELL
            # 부분매도(expected_remaining_qty>0)는 같은 심볼이 남은 수량과 정확히
            # 일치해야 confirmed다 — "심볼이 사라졌는지"만 보면 부분매도는 항상
            # False로 오판된다. 전량매도(expected_remaining_qty==0)만 심볼 소거를 기대한다.
            pos_now = state.get("position") or {}
            expected_remaining = last_order.get("expected_remaining_qty")
            actual_qty = pos_now.get("quantity") or 0
            if expected_remaining == 0:
                trace["position_confirmed"] = pos_now.get("symbol") != last_order.get("symbol") or actual_qty == 0
            else:
                trace["position_confirmed"] = (
                    pos_now.get("symbol") == last_order.get("symbol") and actual_qty == expected_remaining
                )
            trace["prediction_signal"] = "SELL"  # TP/SL/강제청산/스위칭 매도 — 예측신호와 별개로 실제 실행된 것

    # ── 미실현손익/당일수익률 갱신 ────────────────────────────────────────────
    position = state.get("position") or {}
    unrealized_pnl = 0.0
    if position.get("symbol") and (position.get("quantity") or 0) > 0 and position.get("entry_price"):
        cur = _current_price(position["symbol"], hynix_price, inverse_price)
        if cur is not None:
            unrealized_pnl = (cur - position["entry_price"]) * position["quantity"]
    state["unrealized_pnl"] = unrealized_pnl

    if total_equity:
        state["realized_pnl_today_pct"] = round(
            (state.get("realized_pnl_today_krw", 0.0) + unrealized_pnl) / total_equity * 100.0, 4,
        )

    trace["trade_counter"] = state.get("daily_trade_count", 0)
    trace["ui_synced"] = save_state_atomic(state)
    trace["stopped_stage"] = _first_blocked_stage(trace)
    # 사용자 요청 필드명(risk_approved/blocking_reason)도 함께 노출 — risk_manager_ok/
    # stopped_stage와 같은 값을 가리키는 별칭이다.
    trace["risk_approved"] = trace["risk_manager_ok"]
    trace["blocking_reason"] = _build_blocking_reason(trace)

    # 백그라운드 스레드에서만 사이클이 돌아도(=Streamlit 세션에 아무도 접속하지 않아도)
    # UI가 "사이클 미실행"을 보여주지 않도록, 이번 사이클 결과(최종 trace 포함)를
    # state에도 남겨 한 번 더 저장한다.
    state["last_pipeline_trace"] = trace
    state["last_cycle_computed_at"] = now.isoformat()
    state["last_hynix_price"] = hynix_price
    state["last_inverse_price"] = inverse_price
    state["last_enhanced_result"] = enhanced_result
    state["last_decision"] = decision
    save_state_atomic(state)

    # ── 로그 기록 ────────────────────────────────────────────────────────────
    try:
        log_enhanced_prediction({
            "hynix_price": hynix_price, "inverse_price": inverse_price,
            "base_prediction_score": enhanced_result.get("base_prediction_score"),
            "existing_micron_score": enhanced_result.get("existing_micron_score"),
            "hynix_technical_score": enhanced_result.get("hynix_technical_score"),
            "intraday_momentum_score": enhanced_result.get("intraday_momentum_score"),
            "inverse_pressure_score": enhanced_result.get("inverse_pressure_score"),
            "enhanced_score": enhanced_result.get("enhanced_score"),
            "final_action": decision.get("final_action"),
            "reason_top5": enhanced_result.get("reason_top5"),
        })
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 예측 로그 기록 실패: %s", exc)

    failed_orders = [o for o in orders_this_cycle if not o.get("success")]
    if failed_orders:
        for o in failed_orders:
            warnings.append(f"주문 실패/스킵: [{o.get('action')}] {o.get('symbol')} — {o.get('message')}")

    for order in orders_this_cycle:
        try:
            log_trade({
                **order, "mode": mode,
                "base_prediction_score": enhanced_result.get("base_prediction_score"),
                "existing_micron_score": enhanced_result.get("existing_micron_score"),
                "hynix_technical_score": enhanced_result.get("hynix_technical_score"),
                "inverse_pressure_score": enhanced_result.get("inverse_pressure_score"),
                "enhanced_score": enhanced_result.get("enhanced_score"),
                "realized_pnl": state.get("realized_pnl_today_krw"),
                "unrealized_pnl": unrealized_pnl,
                "daily_return": state.get("realized_pnl_today_pct"),
            })
        except Exception as exc:
            logger.debug("[HynixSwitchEngine] 거래 로그 기록 실패: %s", exc)

    # ── 판단 로그 + 예측/실제 결과 추적 (실제 주문 여부와 무관하게 항상 수행) ──
    try:
        from app.services.hynix_prediction_tracker import log_trade_decision, check_and_resolve_pending_outcomes

        log_trade_decision(
            now, hynix_price, inverse_price, enhanced_result, decision,
            actual_trade_executed=any(o.get("success") for o in orders_this_cycle),
            position_symbol=(state.get("position") or {}).get("symbol"),
        )
        check_and_resolve_pending_outcomes(now, hynix_price, inverse_price)
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 판단/결과 추적 로그 실패: %s", exc)

    liquidation_phase = liquidation_phase_now

    # ── 장 종료 후 1일 1회: 종가 outcome 확정 + 일별 리포트 + 가중치 추천 ──────
    today_str = now.strftime("%Y%m%d")
    if liquidation_phase == "closed" and state.get("daily_report_generated_date") != today_str and hynix_price:
        try:
            from app.services.hynix_prediction_tracker import resolve_close_outcomes
            from app.services.hynix_prediction_report import generate_daily_prediction_report
            from app.services.hynix_weight_recommender import recommend_weight_adjustment
            from app.services.hynix_weight_manager import maybe_auto_apply_in_mock

            resolve_close_outcomes(
                date_str=today_str, hynix_close_price=hynix_price, inverse_close_price=inverse_price,
                realized_pnl_today_krw=state.get("realized_pnl_today_krw", 0.0),
            )
            generate_daily_prediction_report(date_str=today_str)
            recommend_weight_adjustment()
            maybe_auto_apply_in_mock(mode, bool(state.get("weight_auto_apply_enabled")))

            from app.services.hynix_exit_recommender import recommend_exit_parameters, generate_daily_exit_learning

            recommend_exit_parameters()
            generate_daily_exit_learning(date_str=today_str)

            state["daily_report_generated_date"] = today_str
            save_state_atomic(state)
        except Exception as exc:
            logger.error("[HynixSwitchEngine] 장종료 리포트/추천 생성 실패: %s", exc)
            warnings.append(f"장종료 리포트/추천 생성 실패: {exc}")

    return {
        "skipped": False,
        "computed_at": now.isoformat(),
        "mode": mode,
        "auto_trade_on": auto_trade_on,
        "watch_only": is_watch_only(now),
        "new_entry_allowed": is_new_entry_allowed(now),
        "liquidation_phase": liquidation_phase,
        "hynix_current_price": hynix_price,
        "inverse_current_price": inverse_price,
        "enhanced_result": enhanced_result,
        "decision": decision,
        "forced_info": forced_info,
        "orders_this_cycle": orders_this_cycle,
        "state": state,
        # UI/Dynamic Exit AI는 이 필드(브로커 sync 직후 결과)를 읽어야 한다.
        # state["position"]/state["daily_trade_count"]는 이 값을 그대로 옮겨 담은 캐시일 뿐이다.
        "position_manager": position_manager.to_cache_dict() if position_manager is not None else None,
        "warnings": warnings + (enhanced_result.get("warnings") or []),
        "pipeline_trace": trace,
        # SHADOW MODE 전용 — 실제 주문에 영향 없음(비교/검증 목적).
        "cycle_ai_shadow_result": state.get("last_cycle_ai_result"),
    }


def execute_hynix_auto_trade(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """update_hynix_auto_trade_loop()의 공개 래퍼."""
    return update_hynix_auto_trade_loop(mode=mode, now=now)
