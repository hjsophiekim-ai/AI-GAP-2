"""
hynix_switch_engine.py — 하이닉스⇄인버스 Enhanced 자동매매 오케스트레이터.

3분마다(또는 UI 자동새로고침 주기마다) 아래 순서를 반복한다:
① kospilab 갱신 ② 마이크론 실시간 갱신 ③~⑥ 점수/판단 계산 ⑦ 보유종목 확인
⑧ 강제청산/TP·SL/스위칭 실행 ⑨ 로그 기록 ⑩ 결과 반환(UI 렌더링용).

각 단계는 개별 try/except로 감싸 부분 실패해도 나머지는 계속 진행한다.
"""

from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

from app.logger import logger
from app.utils.time_utils import kst_now
from app.trading.hynix_symbols import SIGNAL_SYMBOL, LONG_SYMBOL as HYNIX_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL
from app.services.hynix_switch_state import load_state, save_state_atomic, set_active_mode, reset_mock_state
from app.services.hynix_switch_logger import log_enhanced_prediction, log_trade
from app.trading.hynix_switch_risk_gate import (
    is_new_entry_allowed, describe_new_entry_window, get_liquidation_phase,
    should_force_trade, _parse_hm,
)
from app.trading.hynix_switch_position_manager import (
    run_liquidation_if_needed, run_tp_sl_if_needed, run_switch_or_entry, _current_price, _ACTION_TO_SYMBOL,
    apply_position_manager_to_state, run_reversal_switch_if_needed,
)
from app.trading.hynix_position_common import HynixPositionManager
from app.trading.hynix_pullback_entry import detect_pullback
from app.trading.hynix_trend_switch_accelerator import (
    default_confirm_state,
    default_frequency_state as default_trend_frequency_state,
    plan_entry as plan_trend_switch_entry,
    signal_direction,
    update_confirm_tracker,
)
from app.trading.hynix_fast_trend import compute_fast_trend_signal
from app.trading.hynix_primary_trend import (
    PRIMARY_TREND_RANGE, PRIMARY_TREND_UP, PRIMARY_TREND_DOWN,
    classify_short_term_move, compute_primary_trend, default_reversal_confirmation_state,
    inverse_block_vote_count, new_hynix_entry_blocked, new_inverse_entry_blocked, update_reversal_confirmation,
)
from app.trading.adaptive_market_regime import adaptive_regime_to_primary_trend_result

_PULLBACK_MORNING_WINDOW_END = "10:00"
_PULLBACK_PATIENCE_MINUTES = 2

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


_EARLY_ORDER_LOCK = threading.Lock()


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
        # execution_stage: run_switch_or_entry()가 실제로 보고한 단계("entry"=주문이
        # 필요 없었음/진입 자체를 시도 안 함, "state_sync"=POSITION_SYNC_PENDING으로
        # 차단, "order_sent"=주문을 실제로 시도함). order_sent=False가 "주문 전송
        # 실패"인지 "애초에 주문이 필요 없었음"인지 구분하는 데 쓴다.
        "execution_stage": None,
        # 요구사항(2026-07-16) — run_switch_or_entry()의 실제 결과는 entry_approved_reason
        # (진입 "승인" 사유)과 완전히 분리된 필드에 저장한다. blocking_reason이 "왜
        # 주문이 안 나갔는지"를 설명할 때 이 필드들만 사용하고, 승인 문구를 실패
        # 사유로 재사용하지 않는다.
        "execution_message": None, "order_failure_code": None, "broker_error": None,
        "requested_symbol": None, "requested_qty": None, "order_price": None,
        "buyable_cash": None, "sized_cash": None, "cooldown_remaining": None, "pending_order": False,
        # 과거(사이클 시작 전) 스냅샷 vs 이번 사이클에 새로 계산한 live 상태를 UI에서
        # 완전히 분리해 보여주기 위한 필드(요구사항1) — 눌림목 대기 같은 과거
        # 스냅샷이 남아 있어도 이번 사이클의 실제 진입 판단(live)과 혼동되지 않게 한다.
        "snapshot_pullback_status": None, "snapshot_confirmation_count": None,
        "live_entry_gate_status": None, "live_confirmation_count": None,
        "broker_executed": False,
        "position_confirmed": None,
        "ui_synced": None,
        "trade_counter": 0,
        "stopped_stage": None,
        "blocking_reason": None,
        "enhanced_direction_approval": None,
        "enhanced_direct_order_blocked": False,
        "early_decision": None,
        "early_order_result": None,
        "signal_summary": None,
    }


def _raw_score_leader(decision: dict) -> str:
    hynix_score = decision.get("enhanced_score")
    inverse_score = decision.get("inverse_pressure_score")
    try:
        hynix_score_f = float(hynix_score)
        inverse_score_f = float(inverse_score)
    except Exception:
        return "NEUTRAL"
    if hynix_score_f > inverse_score_f:
        return "HYNIX"
    if inverse_score_f > hynix_score_f:
        return "INVERSE"
    return "NEUTRAL"


def _live_trade_direction_label(state: dict) -> str:
    live = state.get("live_trade_direction") or {}
    direction = live.get("direction")
    if direction not in ("UP", "DOWN"):
        return "NONE"
    if live.get("status") == "REVERSAL_CANDIDATE":
        return f"REVERSAL_CANDIDATE_{direction}"
    return direction


def _is_hynix_live_uptrend_block(final_action: str, reason: str, state: dict) -> bool:
    if final_action not in ("INVERSE_BUY", "INVERSE_STRONG_BUY"):
        return False
    text = str(reason or "").upper()
    live_direction = (state.get("live_trade_direction") or {}).get("direction")
    live_trend = state.get("last_live_hynix_trend") or {}
    live_returns = live_trend.get("returns") or {}
    primary = state.get("last_primary_trend") or {}
    live_short_up = (
        live_trend.get("above_vwap") is True
        and (live_returns.get("3m") or 0.0) > 0.0
        and (live_returns.get("5m") or 0.0) > 0.0
        and (live_trend.get("ema_slope_pct") or 0.0) > 0.0
    )
    short_up = (
        live_direction == "UP"
        or ("PRIMARY_TREND=UP" in text)
        or live_short_up
        or (
            primary.get("above_vwap") is True
            and primary.get("above_ema20") is True
            and str(primary.get("trend_5m") or "").upper() == "UP"
        )
    )
    return bool(short_up)


def _build_signal_summary(
    *, decision: dict, trace: dict, state: dict, now: datetime, new_entry_allowed_now: bool,
    new_entry_window: Optional[dict] = None,
) -> dict:
    raw_leader = _raw_score_leader(decision)
    prediction_signal = trace.get("prediction_signal") or _map_prediction_signal(decision.get("final_action", "HOLD"))
    decision_action = decision.get("final_action", "HOLD")
    entry_reason = trace.get("entry_approved_reason") or ""
    block_reason = None
    if not new_entry_allowed_now:
        block_reason = "NEW_ENTRY_TIME_CLOSED"
    elif trace.get("entry_approved") is False:
        block_reason = "LIVE_HYNIX_UPTREND" if _is_hynix_live_uptrend_block(decision_action, entry_reason, state) else (
            trace.get("order_failure_code") or trace.get("stopped_stage") or "ENTRY_BLOCKED"
        )
    actionable_signal = prediction_signal
    if decision_action == "HOLD" or block_reason or trace.get("entry_approved") is False:
        actionable_signal = "HOLD"
    if trace.get("order_sent"):
        final_action = "BUY" if prediction_signal in ("BUY", "INVERSE") else prediction_signal
    else:
        final_action = "HOLD"
    if block_reason == "LIVE_HYNIX_UPTREND":
        live_label = _live_trade_direction_label(state)
        if live_label == "NONE":
            live_label = "UP"
    else:
        live_label = _live_trade_direction_label(state)

    if not new_entry_allowed_now:
        conclusion = "14:50 이후 신호와 무관하게 신규진입 금지 → HOLD"
    elif block_reason == "LIVE_HYNIX_UPTREND" and raw_leader == "INVERSE":
        conclusion = "원점수는 INVERSE 우세이나 하이닉스 단기상승 확인으로 인버스 진입 차단 → HOLD"
    elif actionable_signal == "HOLD":
        conclusion = f"원점수는 {raw_leader} 우세이나 실행 가능한 신규진입 신호 없음 → HOLD"
    else:
        conclusion = f"원점수 {raw_leader}, 실행신호 {actionable_signal} → {final_action}"

    return {
        "computed_at": now.isoformat(),
        "raw_score_leader": raw_leader,
        "hynix_score": decision.get("enhanced_score"),
        "inverse_score": decision.get("inverse_pressure_score"),
        "live_trade_direction": live_label,
        "actionable_signal": actionable_signal,
        "final_action": final_action,
        "decision_final_action": decision_action,
        "prediction_signal": prediction_signal,
        "block_reason": block_reason,
        "entry_approved_reason": entry_reason,
        "new_entry_allowed": bool(new_entry_allowed_now),
        "new_entry_rule": (new_entry_window or {}).get("rule"),
        "conclusion": conclusion,
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
    if trace.get("enhanced_direct_order_blocked"):
        early_decision = trace.get("early_decision") or {}
        early_order = trace.get("early_order_result") or {}
        if early_order.get("broker_executed"):
            return None
        if early_decision.get("reason_code") == "TARGET_ALREADY_FILLED":
            return None
        return "early_order"
    if not trace["order_sent"]:
        # order_sent=False만으로는 "주문 전송 실패"로 단정하지 않는다 — 이미 목표
        # 종목을 보유해서 추가 진입이 필요 없었거나(entry), POSITION_SYNC_PENDING으로
        # 차단됐을 수 있다(state_sync). 실행 계층(run_switch_or_entry)이 보고한
        # execution_stage를 우선 신뢰하고, 없을 때만(레거시 경로 등) order_sent로 본다.
        exec_stage = trace.get("execution_stage")
        if exec_stage in ("entry", "state_sync"):
            return exec_stage
        return "order_sent"
    if not trace["broker_executed"]:
        return "broker_executed"
    if trace["position_confirmed"] is False:
        return "position_confirmed"
    if trace["ui_synced"] is False:
        return "ui_synced"
    return None


_ORDER_FAILURE_CODE_TEXT = {
    "ORDER_QTY_ZERO": "계산된 매수 수량이 0주(투입 현금이 1주 가격보다 작음)",
    "BUYABLE_CASH_ZERO": "매수가능금액(buyable cash) 조회 결과 0원",
    "PRICE_UNAVAILABLE": "현재가 조회 실패로 가격을 확인할 수 없음",
    "COOLDOWN_ACTIVE": "직전 매수 후 쿨다운 시간 이내 — 신규 매수 대기",
    "ORDER_IN_FLIGHT": "이미 처리 중인 주문이 있어 중복 전송 방지",
    "BROKER_REJECTED": "브로커가 주문을 거부함",
    "EXECUTION_EXCEPTION": "주문 실행 중 예외 발생",
}


def _build_blocking_reason(trace: dict) -> Optional[str]:
    """stopped_stage를 사람이 읽을 수 있는 한 줄 사유로 변환 (UI의 blocking_reason 필드).

    요구사항(2026-07-16) — entry/state_sync/order_sent 단계는 절대 entry_approved_reason
    (진입 "승인" 문구)을 실패 사유로 재사용하지 않는다. run_switch_or_entry()가 실제로
    보고한 execution_message/order_failure_code/broker_error만 사용하고, 실패코드가
    있으면 사람이 읽을 한 줄 설명(+ 원본 코드)을 함께 보여준다."""
    stage = trace.get("stopped_stage")
    if not stage:
        return None

    def _execution_reason(fallback: str) -> str:
        code = trace.get("order_failure_code")
        msg = trace.get("execution_message")
        broker_error = trace.get("broker_error")
        if code:
            text = _ORDER_FAILURE_CODE_TEXT.get(code, code)
            detail = f" — {broker_error}" if broker_error else (f" ({msg})" if msg else "")
            return f"{code}: {text}{detail}"
        if msg:
            return msg
        return fallback

    reason_map = {
        "entry_approved": trace.get("entry_approved_reason"),
        "risk_manager": trace.get("risk_manager_reason"),
        "entry": _execution_reason("이미 목표 종목 보유 중이거나 추가 진입이 필요 없어 주문을 시도하지 않음"),
        "state_sync": _execution_reason("POSITION_SYNC_PENDING — 브로커 잔고 확인 전이라 주문 차단"),
        "order_sent": _execution_reason("주문이 브로커로 전송되지 않음(가격 조회 실패/쿨다운/허용 시간대 아님 등)"),
        "broker_executed": "주문은 전송됐으나 브로커 체결 실패",
        "position_confirmed": "체결 후 재조회한 포지션이 기대와 불일치",
        "ui_synced": "상태 저장(디스크 반영) 실패 — 다음 사이클에서 재시도됨",
        "early_order": (
            f"{((trace.get('early_decision') or {}).get('reason_code') or 'NO_EARLY_SIGNAL')}: "
            f"{((trace.get('early_decision') or {}).get('reason') or 'Early Detector 주문 미실행')}"
        ),
    }
    return f"[{stage}] {reason_map.get(stage) or '알 수 없음'}"


def evaluate_pullback_gate(
    state: dict, desired_symbol: str, final_action: str, now: datetime, forced_info: dict, hynix_df_1min, mode: str,
    primary_trend_result: Optional[dict] = None,
) -> dict:
    """General BUY pullback gate with a two-minute maximum wait."""
    held_symbol = (state.get("position") or {}).get("symbol")
    ptrend = primary_trend_result or {}
    primary_trend = ptrend.get("primary_trend", PRIMARY_TREND_RANGE)
    live_trade = state.get("live_trade_direction") or {}
    live_direction_for_desired = "UP" if desired_symbol == HYNIX_SYMBOL else "DOWN" if desired_symbol == INVERSE_SYMBOL else None
    live_reversal_allows_direction = (
        live_trade.get("status") == "REVERSAL_CANDIDATE"
        and live_trade.get("direction") == live_direction_for_desired
    )
    trend_blocked_reason = None
    if not live_reversal_allows_direction and final_action in ("INVERSE_BUY", "INVERSE_STRONG_BUY") and new_inverse_entry_blocked(
        primary_trend, ptrend.get("above_vwap"), ptrend.get("above_ema20"), ptrend,
    ):
        votes, vote_reasons = inverse_block_vote_count(ptrend)
        trend_blocked_reason = (
            f"PRIMARY_TREND=UP with {votes} uptrend confirmations({', '.join(vote_reasons)}) - new INVERSE entry blocked "
            f"(short-term move classified as {classify_short_term_move(primary_trend, None)}"
            "; requires 2x-confirmed VWAP/15m-trend/swing-low breakdown to flip)"
        )
    elif not live_reversal_allows_direction and final_action in ("HYNIX_BUY", "HYNIX_STRONG_BUY") and new_hynix_entry_blocked(
        primary_trend, ptrend.get("above_vwap"), ptrend.get("above_ema20"),
    ):
        trend_blocked_reason = (
            "PRIMARY_TREND=DOWN and price below VWAP/EMA20 - new HYNIX entry blocked "
            "(requires 2x-confirmed VWAP/15m-trend/swing-high breakout to flip)"
        )
    if trend_blocked_reason:
        state["last_trend_switch_plan"] = {
            "proceed": False, "dominant_direction": signal_direction(final_action), "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": None, "block_reason": trend_blocked_reason,
            "primary_trend": primary_trend,
        }
        return {
            "proceed": False, "deadline_expired": False, "pullback_wait_remaining_seconds": None,
            "message": trend_blocked_reason,
        }
    confirm_tracker = update_confirm_tracker(
        state.get("trend_switch_confirm_tracker") or default_confirm_state(),
        final_action, held_symbol, desired_symbol, now,
    )
    frequency_state = state.get("trend_switch_frequency_state") or default_trend_frequency_state()
    state["trend_switch_confirm_tracker"] = confirm_tracker
    has_unconfirmed_order = bool(
        state.get("order_in_flight")
        or state.get("pending_order")
        or state.get("trend_switch_unconfirmed_order")
    )
    pre_plan = plan_trend_switch_entry(
        final_action=final_action,
        held_symbol=held_symbol,
        desired_symbol=desired_symbol,
        confirm_tracker=confirm_tracker,
        frequency_state=frequency_state,
        pullback_result=None,
        now=now,
        data_ok=bool(desired_symbol),
        has_unconfirmed_order=has_unconfirmed_order,
        daily_return_pct=state.get("realized_pnl_today_pct"),
        atr_pct=None,
        override_daily_loss_block=bool(state.get("daily_loss_block_override")),
    )
    state["last_trend_switch_plan"] = {
        **pre_plan,
        "dominant_direction": signal_direction(final_action),
        "desired_symbol": desired_symbol,
        "pullback_wait_remaining_seconds": None,
    }
    if pre_plan.get("proceed"):
        return {
            "proceed": True,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": 0,
            "message": (
                f"TrendSwitchAccel 즉시 진입: {pre_plan.get('entry_type')} "
                f"{(pre_plan.get('position_pct') or 0) * 100:.0f}%"
            ),
        }
    pre_block = str(pre_plan.get("block_reason") or "")
    if pre_block and "눌림목" not in pre_block:
        return {
            "proceed": False,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": None,
            "message": pre_block,
        }

    pending = state.get("pending_entry")
    if not pending or pending.get("action") != final_action or pending.get("symbol") != desired_symbol:
        pending = {"action": final_action, "symbol": desired_symbol, "since": now.isoformat()}
        state["pending_entry"] = pending

    try:
        since = datetime.fromisoformat(pending["since"])
    except Exception:
        since = now

    deadline = since + timedelta(minutes=_PULLBACK_PATIENCE_MINUTES)
    window = forced_info.get("window")
    if window:
        try:
            _, end_str = window.split("-")
            deadline = min(deadline, datetime.combine(now.date(), _parse_hm(end_str)))
        except Exception:
            pass

    remaining_seconds = max(0, int((deadline - now).total_seconds()))
    if now >= deadline:
        deadline_plan = plan_trend_switch_entry(
            final_action=final_action,
            held_symbol=held_symbol,
            desired_symbol=desired_symbol,
            confirm_tracker=confirm_tracker,
            frequency_state=frequency_state,
            pullback_result={"proceed": True, "message": "pullback wait expired with current signal revalidated"},
            now=now,
            data_ok=bool(desired_symbol),
            has_unconfirmed_order=has_unconfirmed_order,
            daily_return_pct=state.get("realized_pnl_today_pct"),
            atr_pct=None,
            override_daily_loss_block=bool(state.get("daily_loss_block_override")),
        )
        state["last_trend_switch_plan"] = {
            **deadline_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": 0,
        }
        return {
            "proceed": bool(deadline_plan.get("proceed")),
            "deadline_expired": True,
            "pullback_wait_remaining_seconds": 0,
            "message": (
                f"눌림목 대기 데드라인 2분 만료({deadline.strftime('%H:%M')}) - 현재 신호 재검증 후 진입 허용"
                if deadline_plan.get("proceed") else deadline_plan.get("block_reason")
            ),
        }

    df_for_check = hynix_df_1min if desired_symbol == HYNIX_SYMBOL else _load_inverse_1min_for_pullback(mode)
    pullback = detect_pullback(df_for_check)
    try:
        pullback_pct = float(pullback.get("pullback_pct") or pullback.get("drop_pct") or pullback.get("distance_pct") or 0.0)
    except Exception:
        pullback_pct = 0.0
    if pullback_pct >= 4.0:
        message = f"deep pullback {pullback_pct:.2f}% - re-evaluate current trend instead of waiting on old high"
        wait_plan = {**pre_plan, "block_reason": message}
        state["last_trend_switch_plan"] = {
            **wait_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": 0,
            "dynamic_pullback_basis": pullback,
        }
        return {
            "proceed": False,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": 0,
            "message": message,
        }
    if pullback.get("is_pullback"):
        pullback_plan = plan_trend_switch_entry(
            final_action=final_action,
            held_symbol=held_symbol,
            desired_symbol=desired_symbol,
            confirm_tracker=confirm_tracker,
            frequency_state=frequency_state,
            pullback_result={"proceed": True, "message": pullback.get("reason")},
            now=now,
            data_ok=bool(desired_symbol),
            has_unconfirmed_order=has_unconfirmed_order,
            daily_return_pct=state.get("realized_pnl_today_pct"),
            atr_pct=None,
            override_daily_loss_block=bool(state.get("daily_loss_block_override")),
        )
        state["last_trend_switch_plan"] = {
            **pullback_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": remaining_seconds,
        }
        return {
            "proceed": bool(pullback_plan.get("proceed")),
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": remaining_seconds,
            "message": (
                f"눌림목 진입 조건 충족: {pullback.get('reason')}"
                if pullback_plan.get("proceed") else pullback_plan.get("block_reason")
            ),
        }
    wait_plan = {
        **pre_plan,
        "block_reason": f"눌림목 대기 중({pullback.get('reason')}) - {deadline.strftime('%H:%M')}까지 대기",
    }
    state["last_trend_switch_plan"] = {
        **wait_plan,
        "dominant_direction": signal_direction(final_action),
        "desired_symbol": desired_symbol,
        "pullback_wait_remaining_seconds": remaining_seconds,
    }
    return {
        "proceed": False,
        "deadline_expired": False,
        "pullback_wait_remaining_seconds": remaining_seconds,
        "message": wait_plan["block_reason"],
    }


def _load_inverse_1min_for_pullback(mode: str):
    try:
        from app.data_sources.hynix_inverse_collector import collect_inverse_minute

        result = collect_inverse_minute(mode=mode)
        return result.get("df_1min")
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 인버스 1분봉 조회 실패(눌림목 판단용): %s", exc)
        return None


def _create_strategy_broker(cfg, mode: str):
    """Create the account adapter for the selected mode while keeping strategy logic common.

    mock/real must share signals, gates and sizing. The only branch here is the
    broker adapter and real-order safety confirmation required before a real KIS
    order-capable broker can be constructed.
    """
    from app.trading.broker_factory import create_broker

    if mode == "real":
        gate_status = (
            cfg.enhanced_real_gate_status(current_mode=mode)
            if hasattr(cfg, "enhanced_real_gate_status")
            else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": [], "checks": {}}
        )
        if not bool(gate_status.get("ready")):
            raise RuntimeError(
                "REAL gate blocked: "
                + ", ".join(gate_status.get("blocking_reasons") or ["UNKNOWN"])
            )
        return create_broker(
            cfg, mode="real", confirm_text=cfg.full_auto_real_confirm_text(),
            runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
        )
    return create_broker(cfg, mode=mode)


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


DAILY_RETURN_UNKNOWN = "DAILY_RETURN_UNKNOWN"
ACCOUNT_EQUITY_MISMATCH = "ACCOUNT_EQUITY_MISMATCH"
_EQUITY_MISMATCH_TOLERANCE_PCT = 0.5
_EQUITY_MISMATCH_RETRY_ATTEMPTS = 3
_EQUITY_MISMATCH_RETRY_DELAY_SECONDS = 3
# 요구사항(2026-07-16 실측) — 60초는 3분 주기 사이클 + KIS 레이트리밋(EGW00201)
# 백오프/재시도 지연을 감안하면 너무 짧다. 실제 매수 직후 브로커 잔고에 그
# 매수가 반영되기까지(레이트리밋으로 재시도가 몇 번 겹치면) 60초를 쉽게 넘길 수
# 있고, 그 사이에 유예가 끝나버리면 방금 막 체결된 정상 거래를
# ACCOUNT_EQUITY_MISMATCH로 오판해 신규주문을 막아버린다(2026-07-16 실측: 500만원
# 매수 직후 인버스 신호가 이 사유로 차단됨). 사이클 1~2회분 여유를 두도록 5분으로 늘린다.
_ACCOUNT_SETTLEMENT_GRACE_SECONDS = 300
_ACCOUNT_SNAPSHOT_FALLBACK_SECONDS = 90


def _position_market_value(position) -> float:
    if isinstance(position, dict):
        qty = float(position.get("quantity", position.get("hldg_qty", 0)) or 0)
        price = float(position.get("current_price", position.get("prpr", 0)) or 0)
        market_value = position.get("market_value")
    else:
        qty = float(getattr(position, "quantity", 0) or 0)
        price = float(getattr(position, "current_price", 0) or 0)
        market_value = getattr(position, "market_value", None)
    if market_value not in (None, ""):
        try:
            return float(market_value)
        except Exception:
            pass
    return qty * price


def _account_settlement_grace_active(state: dict, now: datetime) -> bool:
    candidates = [
        state.get("last_trade_time"),
        state.get("last_order_time"),
        (state.get("position") or {}).get("entry_time"),
    ]
    for raw in candidates:
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except Exception:
            continue
        if 0 <= (now - ts).total_seconds() <= _ACCOUNT_SETTLEMENT_GRACE_SECONDS:
            return True
    return False


def _read_account_equity_snapshot(broker, now: datetime) -> dict:
    """Read one account snapshot and calculate equity from that same response."""
    snapshot = {
        "ok": False, "cash": None, "holdings_market_value": None, "current_equity": None,
        "positions": [], "error": None, "source": None, "as_of": now.isoformat(timespec="seconds"),
    }
    try:
        if hasattr(broker, "kis") and hasattr(broker.kis, "get_balance"):
            bal = broker.kis.get_balance()
            if bal.get("error"):
                snapshot["error"] = bal.get("error")
                snapshot["rt_cd"] = bal.get("rt_cd")
                snapshot["msg_cd"] = bal.get("msg_cd")
                snapshot["msg1"] = bal.get("msg1")
                snapshot["response_field_names"] = bal.get("response_field_names")
                snapshot["output1_field_names"] = bal.get("output1_field_names")
                snapshot["output2_field_names"] = bal.get("output2_field_names")
                return snapshot
            positions = bal.get("positions") or []
            cash = bal.get("cash", None)
            if cash is None:
                snapshot["error"] = "cash field missing"
                return snapshot
            holdings = sum(_position_market_value(p) for p in positions)
            snapshot.update({
                "ok": True, "cash": float(cash), "holdings_market_value": holdings,
                "current_equity": float(cash) + holdings, "positions": positions,
                "source": "kis.get_balance",
                "response_field_names": bal.get("response_field_names"),
                "output1_field_names": bal.get("output1_field_names"),
                "output2_field_names": bal.get("output2_field_names"),
            })
            return snapshot

        positions = broker.get_positions()
        cash = broker.get_balance() if hasattr(broker, "get_balance") else None
        if cash is None:
            snapshot["error"] = "broker cash field missing"
            return snapshot
        holdings = sum(_position_market_value(p) for p in positions)
        snapshot.update({
            "ok": True, "cash": float(cash), "holdings_market_value": holdings,
            "current_equity": float(cash) + holdings, "positions": positions,
            "source": "broker.get_balance+get_positions",
        })
        return snapshot
    except Exception as exc:
        snapshot["error"] = str(exc)
        return snapshot


def _recent_valid_account_snapshot(state: dict, now: datetime) -> Optional[dict]:
    """Return a recent successful account snapshot when KIS is temporarily rate-limited.

    This is only a short-lived read-through fallback for EGW00201/tokenP throttling.
    It must not turn a missing or failed account response into 0 won.
    """
    snapshot = state.get("last_account_equity_snapshot") or {}
    if not snapshot.get("ok"):
        return None
    if snapshot.get("cash") is None or snapshot.get("current_equity") is None:
        return None
    raw_as_of = snapshot.get("as_of")
    if not raw_as_of:
        return None
    try:
        as_of = datetime.fromisoformat(str(raw_as_of))
    except Exception:
        return None
    try:
        age_seconds = (now - as_of).total_seconds()
    except Exception:
        return None
    if age_seconds < 0 or age_seconds > _ACCOUNT_SNAPSHOT_FALLBACK_SECONDS:
        return None
    cached = dict(snapshot)
    cached["ok"] = True
    cached["cached_fallback"] = True
    cached["cached_age_seconds"] = int(age_seconds)
    cached["source"] = f"{cached.get('source') or 'account_snapshot'}+recent_cache"
    return cached


def compute_net_daily_return(
    state: dict, position: Optional[dict], hynix_price: Optional[float], inverse_price: Optional[float],
    cash: Optional[float], positions_from_broker: Optional[list], cash_fetch_ok: bool,
    settlement_grace_active: bool = False,
) -> dict:
    """risk_manager와 UI가 공유하는 단일 일일손익 계산(요구사항 1/5절).

    net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity.
    실현손익은 원장(state["realized_pnl_today_krw"])을, 미실현손익은 현재 보유
    포지션+현재가로 계산한다 — 둘 다 "지금 이 순간의 계좌 잔고 조회"에 의존하지
    않으므로, KIS API 오류(레이트리밋 등)로 잔고조회가 0을 반환해도 이 계산 자체는
    영향받지 않는다(2026-07-14 실측 버그: total_equity=0 → 일손실 -100%로 오판).

    cash/positions_from_broker는 오직 교차검증(current_equity 대조)에만 쓰이며,
    주 계산식에는 들어가지 않는다. cash_fetch_ok=False(조회 실패/빈 응답/필드
    누락)면 DAILY_RETURN_UNKNOWN으로 신규주문만 보류하고 손실로 기록하지 않는다.
    """
    net_realized_pnl = state.get("realized_pnl_today_krw", 0.0)
    result = {
        "starting_equity": state.get("daily_pnl_baseline_equity"),
        "net_realized_pnl": net_realized_pnl, "net_unrealized_pnl": 0.0,
        "net_daily_return": None, "current_equity": None,
        "calculation_source": "ledger_unified", "blocked_reason": None,
        "equity_tolerance_pct": _EQUITY_MISMATCH_TOLERANCE_PCT,
        "settlement_grace_active": settlement_grace_active,
    }

    has_position = bool(
        position and position.get("symbol") and (position.get("quantity") or 0) > 0 and position.get("entry_price"),
    )

    def _local_unrealized_pnl() -> float:
        if not has_position:
            return 0.0
        cur = hynix_price if position["symbol"] == HYNIX_SYMBOL else inverse_price
        if cur is None:
            return 0.0
        try:
            from app.trading.trading_cost_engine import TradeCostEngine

            cost_result = TradeCostEngine().compute_unrealized_net_pnl(
                position["symbol"], entry_price=position["entry_price"], current_price=cur,
                quantity=position["quantity"],
            )
            return float(cost_result["net_unrealized_pnl"])
        except Exception:
            return float((cur - position["entry_price"]) * position["quantity"])

    # 요구사항(2026-07-16 실측) — 방금 체결된 실매수 직후 KIS 잔고조회(output1)가
    # 아직 새 보유종목을 반영하지 못해 positions_from_broker가 비어있거나 보유 중인
    # 심볼을 포함하지 않는 짧은 지연이 실제로 발생한다(모의/실전 공통). 이 상태로
    # holdings_value를 계산하면 current_equity가 방금 매수한 금액만큼 실제보다 낮게
    # 잡혀 큰 괴리가 생기고, 이를 ACCOUNT_EQUITY_MISMATCH(계좌 데이터 이상)로 오판해
    # 신규주문이 계속 차단됐다(BUY 신호가 risk_manager에서 반복 차단됨) — 브로커
    # 포지션 동기화 지연은 계좌 이상이 아니므로, cash_fetch_ok=False와 동일하게
    # 원장/entry_price 기준(신뢰 가능) 미실현손익 계산으로 즉시 대체한다.
    if has_position and cash_fetch_ok and positions_from_broker is not None:
        broker_has_held_symbol = any(
            (p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)) == position.get("symbol")
            for p in positions_from_broker
        )
        if not broker_has_held_symbol:
            cash_fetch_ok = False
            result["calculation_warning"] = "BROKER_POSITION_SYNC_LAG_LEDGER_FALLBACK"

    if not cash_fetch_ok:
        starting_equity = result["starting_equity"]
        if starting_equity and starting_equity > 0:
            net_unrealized_pnl = _local_unrealized_pnl()
            result["net_unrealized_pnl"] = net_unrealized_pnl
            result["net_daily_return"] = round((net_realized_pnl + net_unrealized_pnl) / starting_equity * 100.0, 4)
            result["calculation_warning"] = result.get("calculation_warning") or "ACCOUNT_SNAPSHOT_UNAVAILABLE_LEDGER_FALLBACK"
            return result
        result["blocked_reason"] = DAILY_RETURN_UNKNOWN
        return result

    holdings_value = sum(
        (getattr(p, "market_value", None) if not isinstance(p, dict) else p.get("market_value")) or 0.0
        for p in (positions_from_broker or [])
    )
    current_equity = (float(cash) + holdings_value) if cash is not None else None
    result["current_equity"] = current_equity

    starting_equity = result["starting_equity"]
    if starting_equity is None:
        # 요구사항(2026-07-20 실측) — 당일 "첫 유효 조회"가 반드시 장 시작 직후(오늘
        # 거래가 전혀 없던 시점)에 일어난다는 보장이 없다. KIS 토큰 발급 지연/
        # 레이트리밋(EGW00201/EGW00123 — 이 파일에 이미 여러 번 실측된 문제)이나
        # 앱 재시작으로 첫 성공 조회가 오늘 이미 실현손익이 쌓였거나 포지션을 보유해
        # 미실현손익이 있는 "이후" 시점에 일어나면, 그 순간의 current_equity를 그대로
        # 기준자산으로 저장했다 — 그 결과 이후 매 사이클 원장 기준(net_daily_return)이
        # 이미 기준자산에 반영된 그 손익을 다시 더해 이중계산하면서, 원장 기준과
        # 현재자산 기준 수익률 사이에 "기준자산을 캡처한 시점까지의 손익"만큼 고정된
        # 괴리가 생겼다. 이 괴리는 일시적 API 글리치가 아니라 구조적인 오차라서
        # 재시도(3회)/정산지연 설명/5분 유예 중 어느 것으로도 해소되지 않고, 그날
        # 남은 사이클 내내 ACCOUNT_EQUITY_MISMATCH로 신규주문이 계속 차단됐다.
        # 수정: 기준자산을 "지금 시점의 current_equity"가 아니라 오늘 이미 발생한
        # 실현+미실현 손익을 역산해서 뺀 값(=진짜 하루 시작 시점 자산)으로 설정한다.
        if current_equity is not None and current_equity > 0:
            net_unrealized_pnl = _local_unrealized_pnl()
            adjusted_baseline = current_equity - net_realized_pnl - net_unrealized_pnl
            if adjusted_baseline > 0:
                state["daily_pnl_baseline_equity"] = adjusted_baseline
                result["starting_equity"] = adjusted_baseline
                result["net_unrealized_pnl"] = net_unrealized_pnl
                result["net_daily_return"] = round((net_realized_pnl + net_unrealized_pnl) / adjusted_baseline * 100.0, 4)
                # 요구사항 — 방금 막 확정한 기준자산에서 도출된 수익률 하나만으로
                # 곧바로 일일손실 강제중단(-2.5%)까지 실행하지는 않는다(호출부가
                # 이 플래그를 보고 스킵). 첫 스냅샷은 아직 가격/잔고 데이터가 안정된
                # 것으로 확인되지 않았고, 예전 코드가 net_daily_return=0.0으로
                # 고정했던 것도 바로 이 "첫 표본의 노이즈로 즉시 조치하지 않는다"는
                # 취지였다 — 다음 사이클에도 같은(이미 확정된) 기준자산으로 재계산해
                # 여전히 -2.5% 이하면 그때는 정상적으로 차단된다.
                result["baseline_just_established"] = True
            else:
                # 오늘 이미 발생한 손실이 현재자산 이상으로 극단적인 경우(이론상
                # 방어) — 역산 기준자산이 0 이하로 나오면 대신 현재자산을 그대로
                # 기준으로 삼는다(0/음수 기준자산으로 나눗셈하지 않도록).
                state["daily_pnl_baseline_equity"] = current_equity
                result["starting_equity"] = current_equity
                result["net_daily_return"] = 0.0
                result["baseline_just_established"] = True
        else:
            result["blocked_reason"] = DAILY_RETURN_UNKNOWN
        return result

    if starting_equity <= 0:
        # 이전 버전의 버그(또는 수동 조작)로 이미 저장된 기준자산이 0/음수라면,
        # 무한 차단 대신 "기준자산 없음"과 동일하게 취급해 위와 같은 방식으로
        # 즉시 재계산(self-heal)한다 — 하루 종일 차단된 채로 남지 않는다.
        if current_equity is not None and current_equity > 0:
            net_unrealized_pnl = _local_unrealized_pnl()
            adjusted_baseline = current_equity - net_realized_pnl - net_unrealized_pnl
            if adjusted_baseline > 0:
                state["daily_pnl_baseline_equity"] = adjusted_baseline
                result["starting_equity"] = adjusted_baseline
                result["net_unrealized_pnl"] = net_unrealized_pnl
                result["net_daily_return"] = round((net_realized_pnl + net_unrealized_pnl) / adjusted_baseline * 100.0, 4)
                result["baseline_rebased"] = True
                result["baseline_just_established"] = True
                return result
        result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH
        return result

    # 요구사항 6절 — current_equity<=0인데 현금/원장잔고가 실제로 존재해야 하는 상황
    if current_equity is not None and current_equity <= 0 and (net_realized_pnl != 0 or starting_equity > 0):
        result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH if has_position else DAILY_RETURN_UNKNOWN
        return result

    # ── 미실현손익(요구사항 2절 — 보유 없으면 0) ─────────────────────────────
    net_unrealized_pnl = _local_unrealized_pnl()
    result["net_unrealized_pnl"] = net_unrealized_pnl

    net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity * 100.0

    # 요구사항 6절 — 원장기준 수익률과 현재자산기준 수익률 차이가 0.1%p 초과하면
    # 계좌 데이터 불일치로 간주하고 -100% 같은 값으로 기록하지 않는다.
    if current_equity is not None and current_equity > 0:
        equity_ratio_return = (current_equity / starting_equity - 1.0) * 100.0
        if abs(equity_ratio_return - net_daily_return) > _EQUITY_MISMATCH_TOLERANCE_PCT:
            if not has_position and net_realized_pnl == 0 and net_unrealized_pnl == 0:
                state["daily_pnl_baseline_equity"] = current_equity
                result["starting_equity"] = current_equity
                result["net_daily_return"] = 0.0
                result["equity_ratio_return"] = 0.0
                result["baseline_rebased"] = True
                return result
            # KRX는 매도대금을 T+2에 정산한다 — 오늘 청산한 왕복거래의 실현손익은
            # 원장(net_realized_pnl)에는 즉시 반영되지만, 브로커의 실제 현금(cash)에는
            # 아직 반영되지 않은 게 정상이다. 이 "정산 지연"만으로 설명되는 차이라면
            # (미실현손익 기준 기대 수익률과는 실제로 일치) 계좌 이상이 아니므로 차단하지
            # 않는다 — 이전에는 60초 유예만으로는 턱없이 부족해 실현손익이 있는 날은
            # 이후 신규주문이 하루 종일 계속 차단되는 문제가 있었다.
            unsettled_adjusted_return = (net_unrealized_pnl / starting_equity) * 100.0
            settlement_adjusted_mismatch = abs(equity_ratio_return - unsettled_adjusted_return)
            if settlement_adjusted_mismatch <= _EQUITY_MISMATCH_TOLERANCE_PCT:
                result["net_daily_return"] = round(net_daily_return, 4)
                result["equity_ratio_return"] = round(equity_ratio_return, 4)
                result["mismatch_explained_by_unsettled_realized_pnl"] = True
                result["unsettled_realized_pnl_krw"] = net_realized_pnl
                return result
            if settlement_grace_active:
                result["net_daily_return"] = round(net_daily_return, 4)
                result["equity_ratio_return"] = round(equity_ratio_return, 4)
                result["mismatch_deferred"] = True
                return result
            result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH
            result["equity_ratio_return"] = round(equity_ratio_return, 4)
            return result

    result["net_daily_return"] = round(net_daily_return, 4)
    return result


def _compute_net_daily_return_with_retries(
    state: dict, broker, position: Optional[dict], hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, *, attempts: int = _EQUITY_MISMATCH_RETRY_ATTEMPTS,
    delay_seconds: int = _EQUITY_MISMATCH_RETRY_DELAY_SECONDS,
) -> dict:
    attempts = max(1, int(attempts or 1))
    grace = _account_settlement_grace_active(state, now)
    history = []
    last_result = None
    last_snapshot = None
    for idx in range(attempts):
        snapshot = _read_account_equity_snapshot(broker, now)
        if not snapshot.get("ok"):
            cached_snapshot = _recent_valid_account_snapshot(state, now)
            if cached_snapshot is not None:
                cached_snapshot["live_error"] = snapshot.get("error")
                cached_snapshot["live_msg_cd"] = snapshot.get("msg_cd")
                cached_snapshot["live_msg1"] = snapshot.get("msg1")
                snapshot = cached_snapshot
        last_snapshot = snapshot
        result = compute_net_daily_return(
            state, position, hynix_price, inverse_price,
            cash=snapshot.get("cash"), positions_from_broker=snapshot.get("positions"),
            cash_fetch_ok=bool(snapshot.get("ok")), settlement_grace_active=grace,
        )
        result["account_snapshot"] = {
            "as_of": snapshot.get("as_of"), "source": snapshot.get("source"),
            "cash": snapshot.get("cash"), "holdings_market_value": snapshot.get("holdings_market_value"),
            "current_equity": snapshot.get("current_equity"), "ok": snapshot.get("ok"),
            "error": snapshot.get("error"),
            "positions": snapshot.get("positions") or [],
            "rt_cd": snapshot.get("rt_cd"), "msg_cd": snapshot.get("msg_cd"), "msg1": snapshot.get("msg1"),
            "response_field_names": snapshot.get("response_field_names"),
            "output1_field_names": snapshot.get("output1_field_names"),
            "output2_field_names": snapshot.get("output2_field_names"),
        }
        for key in ("cached_fallback", "cached_age_seconds", "live_error", "live_msg_cd", "live_msg1"):
            if snapshot.get(key) is not None:
                result["account_snapshot"][key] = snapshot.get(key)
        result["mismatch_retry_index"] = idx + 1
        history.append({
            "attempt": idx + 1,
            "blocked_reason": result.get("blocked_reason"),
            "current_equity": result.get("current_equity"),
            "net_daily_return": result.get("net_daily_return"),
            "equity_ratio_return": result.get("equity_ratio_return"),
            "snapshot": result["account_snapshot"],
        })
        last_result = result
        if result.get("blocked_reason") != ACCOUNT_EQUITY_MISMATCH:
            break
        if idx < attempts - 1:
            time.sleep(delay_seconds)
    last_result = last_result or {"blocked_reason": DAILY_RETURN_UNKNOWN}
    last_result["equity_check_history"] = history
    last_result["equity_check_attempts"] = len(history)
    last_result["settlement_grace_active"] = grace
    if last_snapshot:
        state["last_account_equity_snapshot"] = last_result.get("account_snapshot")
    return last_result


def check_real_mode_gates(state: dict, cfg=None) -> dict:
    """명세 5절 — real 실제 주문 전 필수 게이트 체크리스트(읽기 전용 진단 함수).

    이 함수는 어떤 게이트도 새로 활성화하지 않는다 — 기존 config.yaml/.env/
    trading_policy.yaml 값을 그대로 조회만 한다. Active Strategy는 여전히 mock
    전용이며, 이 체크리스트는 향후 real 연결 승인 여부를 판단하기 위한 참고용이다.
    """
    from app.config import get_config

    cfg = cfg or get_config()
    real_gate_status = (
        cfg.enhanced_real_gate_status(current_mode=state.get("mode", "mock"))
        if hasattr(cfg, "enhanced_real_gate_status")
        else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": []}
    )
    checks = {
        "ui_mode_real": state.get("mode") == "real",
        "real_master_switch": cfg.real_trading_enabled(),
        "auto_trade_enabled": bool(state.get("auto_trade_on")),
        "real_auto_order_enabled": bool(real_gate_status.get("ready")),
        "no_position_conflict": not bool(state.get("position_conflict")),
    }
    all_pass = all(checks.values())
    failed = [name for name, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "all_pass": all_pass,
        "failed_gates": failed,
        "real_gate_status": real_gate_status,
    }


def _run_active_strategy_entry(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, orders_this_cycle: list, enhanced_ai_score: Optional[float] = None,
    position_manager=None,
) -> dict:
    """ACTIVE STRATEGY diagnostics for the common mock/real strategy profile.

    This path is evaluated identically in mock and real when the common toggle is
    enabled. Actual order ownership remains with the common execution engines
    below, so broker/account differences do not alter the decision surface.
    """
    final_decision = {"executable": False, "order_sent": False, "signal_source": "SHADOW_ONLY"}
    state["last_final_execution_decision"] = final_decision
    return {
        "acted": False,
        "message": "ACTIVE_STRATEGY shadow-only: actual broker orders are owned by ENHANCED_REGIME_SWITCH",
        "action": "SHADOW_ONLY",
        "decision": {},
        "final_decision": final_decision,
        "failure_reason": "SHADOW_ONLY",
    }

    from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio
    from app.trading.hynix_active_strategy_engine import (
        decide_active_strategy_action, default_active_strategy_state,
        register_position_opened, register_position_closed,
        to_final_execution_decision, generate_idempotency_key, is_duplicate_signal, register_idempotency_key,
        REASON_INSUFFICIENT_CASH, REASON_DUPLICATE_SIGNAL, REASON_STALE_PRICE, REASON_ORDER_EXCEPTION,
        REASON_RISK_LIMIT, REASON_COOLDOWN,
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
        enhanced_ai_score=enhanced_ai_score, micron_ai_score=shadow.get("effective_micron_score"),
    )
    state["active_strategy_state"] = decision_result["state"]
    action = decision_result["action"]

    # ── FinalExecutionDecision: 이번 사이클의 실행 신호를 단일 객체로 통일한다.
    # 주문 실행은 이 객체의 executable/blocking_reason만 근거로 삼는다.
    final_decision = to_final_execution_decision(decision_result, held_symbol=position_state.get("symbol"))
    state["last_final_execution_decision"] = final_decision

    acted = False
    order_id = executed_price = executed_qty = None
    failure_reason = None
    message = final_decision.get("blocking_reason") or "; ".join(final_decision.get("reasons", [])) or "HOLD"

    if not final_decision["executable"]:
        final_decision.update(order_sent=False, order_id=None, executed_price=None, executed_qty=None, failure_reason=None)
        return {
            "acted": False, "message": message, "action": action,
            "decision": decision_result, "final_decision": final_decision, "failure_reason": None,
        }

    # ── 중복 신호 방지(명세 8절): 같은 분 단위 cycle_id 안에서 동일 action+symbol 재실행 금지 ──
    cycle_id = now.strftime("%Y%m%d%H%M")
    idem_key = generate_idempotency_key(now, mode_name, cycle_id, final_decision["action"], final_decision["symbol"] or "")
    if is_duplicate_signal(state["active_strategy_state"], idem_key):
        failure_reason = REASON_DUPLICATE_SIGNAL
        message = f"중복 신호 차단(idempotency_key={idem_key})"
        final_decision.update(order_sent=False, order_id=None, executed_price=None, executed_qty=None, failure_reason=failure_reason)
        return {
            "acted": False, "message": message, "action": action,
            "decision": decision_result, "final_decision": final_decision, "failure_reason": failure_reason,
        }

    if action in (ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE):
        symbol = decision_result["recommended_symbol"]
        price = _current_price(symbol, hynix_price, inverse_price)
        pct = decision_result["recommended_position_pct"]
        if not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 주문 미실행"
        elif pct <= 0:
            failure_reason, message = REASON_RISK_LIMIT, "권장 비중 0% — 주문 미실행"
        else:
            try:
                full_cash = float(broker.get_buyable_cash())
            except Exception as exc:
                full_cash, failure_reason, message = 0.0, REASON_ORDER_EXCEPTION, f"매수가능금액 조회 실패: {exc}"
            if full_cash > 0:
                cash_amount = full_cash * (pct / 100.0)
                if cash_amount < price:
                    failure_reason = REASON_INSUFFICIENT_CASH
                    message = f"매수가능금액 부족(필요 {price:,.0f}원, 가용 {cash_amount:,.0f}원)"
                else:
                    try:
                        buy_result = _buy_new(
                            broker, symbol, price, cash_amount, f"Active Strategy({mode_name}) 진입 {pct:.0f}%",
                            orders_this_cycle, mode="mock", signal_source="ACTIVE_ONLY",
                            position_manager=position_manager,
                        )
                    except Exception as exc:
                        buy_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                    if buy_result.get("success"):
                        if buy_result.get("position_sync_status") == "POSITION_SYNC_PENDING":
                            failure_reason = REASON_ORDER_EXCEPTION
                            message = buy_result.get("message", "POSITION_SYNC_PENDING")
                            state["position_sync_block_new_orders"] = True
                            state["position_sync_status"] = "POSITION_SYNC_PENDING"
                            state["critical_alert"] = message
                            return {
                                "acted": True, "message": message, "action": action,
                                "decision": decision_result, "final_decision": {}, "failure_reason": failure_reason,
                            }
                        acted = True
                        qty = buy_result.get("bought_quantity", 0)
                        order_id, executed_price, executed_qty = buy_result.get("order_id"), price, qty
                        state["position"] = {
                            "symbol": symbol, "name": symbol, "quantity": qty, "avg_price": price,
                            "entry_price": price, "entry_time": now.isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
                        }
                        state["active_strategy_state"] = register_position_opened(state["active_strategy_state"], symbol, price, pct, now)
                        message = f"Active Strategy 진입: {symbol} {pct:.0f}%({qty}주)"
                    elif not failure_reason:
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = buy_result.get("message", "매수 실패")

    elif action in (ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH) and position_state.get("symbol"):
        symbol = position_state["symbol"]
        price = _current_price(symbol, hynix_price, inverse_price)
        ratio = 1.0 if action in (ACTION_EXIT_ALL, ACTION_SWITCH) else max(0.01, min(1.0, decision_result["recommended_position_pct"] / 100.0))
        if not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 매도 미실행"
        else:
            try:
                sell_result = _sell_all_or_ratio(
                    broker, position, price, ratio, f"Active Strategy({mode_name}) {action}", orders_this_cycle,
                    mode="mock", exit_reason_type="active_strategy", signal_source="ACTIVE_ONLY",
                    position_manager=position_manager,
                )
            except Exception as exc:
                sell_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
            if sell_result.get("success"):
                acted = True
                order_id, executed_price = sell_result.get("order_id"), price
                executed_qty = sell_result.get("sold_quantity")
                remaining = sell_result.get("remaining_quantity")
                if sell_result.get("position_sync_status") == "POSITION_SYNC_PENDING" or remaining is None:
                    state["position_sync_block_new_orders"] = True
                    state["position_sync_status"] = "POSITION_SYNC_PENDING"
                    state["critical_alert"] = "POSITION_SYNC_PENDING - broker balance confirmation failed after sell"
                    failure_reason = REASON_ORDER_EXCEPTION
                    message = state["critical_alert"]
                    return {
                        "acted": True, "message": message, "action": action,
                        "decision": decision_result, "final_decision": {}, "failure_reason": failure_reason,
                    }
                if remaining <= 0:
                    state["position"] = {
                        "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
                        "entry_time": None, "name": None, "partial_tp1_done": False, "partial_sl1_done": False,
                    }
                    state["active_strategy_state"] = register_position_closed(state["active_strategy_state"], was_stop_loss=False, now=now)
                else:
                    state["position"]["quantity"] = remaining
                message = f"Active Strategy 청산/축소: {symbol} {action} (비중 {ratio*100:.0f}%)"
            elif not failure_reason:
                failure_reason = REASON_COOLDOWN if sell_result.get("blocked_by_coordinator") else REASON_ORDER_EXCEPTION
                message = sell_result.get("message", "매도 실패")

    if acted:
        state["active_strategy_state"] = register_idempotency_key(state["active_strategy_state"], idem_key)

    final_decision.update(
        order_sent=acted, order_id=order_id, executed_price=executed_price, executed_qty=executed_qty,
        failure_reason=(failure_reason if not acted else None),
    )
    state["last_final_execution_decision"] = final_decision

    return {
        "acted": acted, "message": message, "action": action,
        "decision": decision_result, "final_decision": final_decision, "failure_reason": failure_reason,
    }


def _run_adaptive_fusion_entry(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, orders_this_cycle: list, enhanced_ai_score: Optional[float] = None,
    hynix_df_1min=None, position_manager=None,
) -> dict:
    """ADAPTIVE FUSION diagnostics for the common mock/real strategy profile.

    2026-07-13 사용자 검증: 이전에는 Prediction V2가 buy_probability/sell_probability
    "값"만 _run_active_strategy_entry의 입력으로 흘러들어갔을 뿐, V2 자신의 독자 판단
    (decide_final_action_v2)이나 Cycle AI/Micron Proxy의 독자 판단은 실제 체결
    signal_source에 전혀 반영되지 않았다(항상 ACTIVE_STRATEGY_MOCK/DYNAMIC_EXIT만
    기록됨). 이 함수는 5개 모델(ACTIVE_FUSION/PREDICTION_V2/CYCLE_AI/EARLY_PREDICTION/
    MICRON_PROXY)의 독자 확률을 실제로 융합해 신규 진입을 결정하고, 그 기여도를
    ledger(active_probability/prediction_v2_probability/.../dominant_model/
    prediction_v2_weight)에 그대로 남긴다. 보유 포지션의 청산/전환 관리는 이미 검증된
    decide_active_strategy_action()의 판단을 그대로 재사용한다(이번 턴에서 선제청산
    로직까지 전부 새로 검증하기보다, 신규진입 판단에 V2를 반영하는 핵심 문제부터
    안전하게 해결하기 위함 — 확장 여지는 남겨둔다).
    """
    fusion_decision = {"executable": False, "signal_source": "SHADOW_ONLY", "weights": {}, "dominant_model": "SHADOW_ONLY"}
    final_decision = {"executable": False, "order_sent": False, "signal_source": "SHADOW_ONLY"}
    state["last_fusion_decision"] = fusion_decision
    state["last_final_execution_decision"] = final_decision
    return {
        "acted": False,
        "message": "ADAPTIVE_FUSION shadow-only: actual broker orders are owned by ENHANCED_REGIME_SWITCH",
        "action": "SHADOW_ONLY",
        "decision": {},
        "fusion_decision": fusion_decision,
        "final_decision": final_decision,
        "failure_reason": "SHADOW_ONLY",
    }

    from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio
    from app.trading.hynix_active_strategy_engine import (
        decide_active_strategy_action, default_active_strategy_state,
        register_position_opened, register_position_closed,
        generate_idempotency_key, is_duplicate_signal, register_idempotency_key,
        REASON_INSUFFICIENT_CASH, REASON_DUPLICATE_SIGNAL, REASON_STALE_PRICE, REASON_ORDER_EXCEPTION,
        REASON_RISK_LIMIT, REASON_COOLDOWN,
        ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE, ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH,
    )
    from app.trading.hynix_trading_mode import DEFAULT_MODE
    from app.trading.hynix_adaptive_fusion_engine import (
        HynixAdaptiveFusionEngine, evaluate_prediction_v2_performance,
        default_hold_tracker, update_hold_tracker, default_whipsaw_state, register_direction_flip,
        default_frequency_state, register_frequency_entry, register_frequency_round_trip_closed,
        compute_live_hynix_trend,
        MODEL_PREDICTION_V2, MODEL_STATUS_ADVISORY, MODEL_STATUS_LIVE_VALIDATED,
        ACTION_HYNIX as FUSION_HYNIX, ACTION_INVERSE as FUSION_INVERSE, ACTION_HOLD as FUSION_HOLD,
    )
    from app.services.hynix_execution_ledger import (
        SIGNAL_SOURCE_ACTIVE_ONLY, SIGNAL_SOURCE_ADAPTIVE_FUSION, SIGNAL_SOURCE_PREDICTION_V2_ASSISTED,
    )

    shadow = state.get("last_cycle_ai_result") or {}
    cyc = shadow.get("cycle") or {}
    prob = shadow.get("probability") or {}
    decision_v2 = shadow.get("decision_v2") or {"final_action_v2": "HOLD"}
    turning_point = cyc.get("turning_point") or {}
    momentum = cyc.get("momentum") or {}

    mode_name = state.get("trading_mode", DEFAULT_MODE)
    strategy_state = state.get("active_strategy_state") or default_active_strategy_state(mode_name)

    position = state.get("position") or {}
    position_state = {
        "symbol": position.get("symbol"), "quantity": position.get("quantity") or 0,
        "entry_price": position.get("entry_price"),
    }
    has_position = bool(position_state["symbol"]) and position_state["quantity"] > 0

    raw_vel = momentum.get("raw_velocity_3")
    expected_move_pct = round(abs(raw_vel) * 2.0, 3) if raw_vel is not None else 0.25
    data_ok = bool(hynix_price and inverse_price)

    # ── Model A: ACTIVE_FUSION 자체 판단(이미 검증된 로직 — 보유 포지션 관리는 이 결과를 그대로 쓴다) ──
    active_decision_result = decide_active_strategy_action(
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
        data_ok=data_ok, position_conflict=bool(state.get("position_conflict")),
        enhanced_ai_score=enhanced_ai_score, micron_ai_score=shadow.get("effective_micron_score"),
    )
    state["active_strategy_state"] = active_decision_result["state"]

    # ── Model B~E 입력 준비 ──────────────────────────────────────────────────
    prediction_v2_performance = evaluate_prediction_v2_performance(now)
    micron_snapshot = state.get("last_micron_proxy_snapshot") or {}
    micron_proxy = None
    if micron_snapshot.get("effective_micron_score") is not None:
        micron_proxy = {
            "effective_micron_score": micron_snapshot.get("effective_micron_score"),
            "micron_data_confidence": micron_snapshot.get("confidence"),
            "micron_score_source": micron_snapshot.get("score_source"),
            "calculated_at": micron_snapshot.get("calculated_at"),
        }
        try:
            if micron_snapshot.get("calculated_at"):
                age = (now - datetime.fromisoformat(str(micron_snapshot.get("calculated_at")))).total_seconds() / 60.0
                if age < 0:
                    # 캔들/스냅샷 시각이 현재(KST) 기준 미래로 보임 — 시계/타임존
                    # 불일치(DATA_TIME_ERROR)이지 "매우 신선한 데이터"가 아니다.
                    # 절대 0으로 뭉개서 fresh처럼 취급하면 안 된다(2026-07-16 실측
                    # 버그: age=-538분이 is_stale=False로 통과됨).
                    micron_proxy["age_minutes"] = round(age, 2)
                    micron_proxy["is_stale"] = True
                    micron_proxy["data_time_error"] = True
                    logger.error(
                        "[HynixSwitchEngine] Micron proxy DATA_TIME_ERROR: age=%.1f분(음수) — "
                        "시계/타임존 불일치 의심, stale로 처리",
                        age,
                    )
                else:
                    micron_proxy["age_minutes"] = round(age, 2)
                    micron_proxy["is_stale"] = age > 15.0
                    micron_proxy["data_time_error"] = False
        except Exception:
            pass

    hold_tracker = state.get("adaptive_fusion_hold_tracker") or default_hold_tracker()
    whipsaw_state = state.get("adaptive_fusion_whipsaw_state") or default_whipsaw_state()
    frequency_state = state.get("adaptive_fusion_frequency_state") or default_frequency_state()
    live_hynix_trend = compute_live_hynix_trend(hynix_df_1min, now)
    prior_live = state.get("live_hynix_trend_state") or {}
    if live_hynix_trend.get("hynix_uptrend_confirmed"):
        live_hynix_trend["hynix_uptrend_streak"] = int(prior_live.get("hynix_uptrend_streak", 0) or 0) + 1
        live_hynix_trend["hynix_downtrend_streak"] = 0
    elif live_hynix_trend.get("hynix_downtrend_confirmed"):
        live_hynix_trend["hynix_downtrend_streak"] = int(prior_live.get("hynix_downtrend_streak", 0) or 0) + 1
        live_hynix_trend["hynix_uptrend_streak"] = 0
    else:
        live_hynix_trend["hynix_uptrend_streak"] = 0
        live_hynix_trend["hynix_downtrend_streak"] = 0
    state["live_hynix_trend_state"] = {
        "hynix_uptrend_streak": live_hynix_trend.get("hynix_uptrend_streak", 0),
        "hynix_downtrend_streak": live_hynix_trend.get("hynix_downtrend_streak", 0),
        "last_direction": live_hynix_trend.get("direction"),
        "as_of": live_hynix_trend.get("as_of"),
    }
    state["last_live_hynix_trend"] = live_hynix_trend

    engine = HynixAdaptiveFusionEngine()
    fusion_decision = engine.decide(
        now=now, active_decision_result=active_decision_result,
        prediction_v2_probability=prob, prediction_v2_decision=decision_v2,
        prediction_v2_performance=prediction_v2_performance, cycle_result=cyc,
        cycle_ai_validated=bool(state.get("cycle_ai_validated", False)), micron_proxy=micron_proxy,
        held_symbol=position_state["symbol"], position_conflict=bool(state.get("position_conflict")),
        data_ok=data_ok, price_is_stale=not data_ok,
        daily_return_pct=state.get("realized_pnl_today_pct"), orders_today_count=state.get("daily_trade_count", 0),
        hold_tracker=hold_tracker, whipsaw_state=whipsaw_state, frequency_state=frequency_state,
        consecutive_stop_losses=strategy_state.get("consecutive_stop_losses", 0),
        live_hynix_trend=live_hynix_trend,
    )

    fused_action = fusion_decision["final_action"]
    hold_tracker = update_hold_tracker(hold_tracker, has_position, fused_action, now)
    state["adaptive_fusion_hold_tracker"] = hold_tracker
    last_direction = state.get("adaptive_fusion_last_direction")
    if fused_action in (FUSION_HYNIX, FUSION_INVERSE):
        if last_direction and last_direction != fused_action:
            whipsaw_state = register_direction_flip(whipsaw_state, now)
        state["adaptive_fusion_last_direction"] = fused_action
    state["adaptive_fusion_whipsaw_state"] = whipsaw_state
    state["last_fusion_decision"] = fusion_decision

    # ── signal_source: Prediction V2가 실제로 (SHADOW를 벗어나) 기여했을 때만 그렇다고 표기한다 ──
    pv2_weight = fusion_decision["weights"].get(MODEL_PREDICTION_V2, 0.0)
    pv2_status = prediction_v2_performance.get("model_status")
    pv2_applied = pv2_status in (MODEL_STATUS_ADVISORY, MODEL_STATUS_LIVE_VALIDATED) and pv2_weight > 0
    if not pv2_applied:
        signal_source = SIGNAL_SOURCE_ACTIVE_ONLY
    elif fusion_decision["dominant_model"] == MODEL_PREDICTION_V2:
        signal_source = SIGNAL_SOURCE_PREDICTION_V2_ASSISTED
    else:
        signal_source = SIGNAL_SOURCE_ADAPTIVE_FUSION

    fusion_metadata = {
        "active_probability": fusion_decision["fused_hynix_probability"],  # 참고용(대표값) — 상세는 아래 개별 확률 사용
        "prediction_v2_probability": prob.get("buy_probability"),
        "cycle_probability": turning_point.get("up_turn_probability_3m"),
        "fused_probability": max(fusion_decision["fused_hynix_probability"], fusion_decision["fused_inverse_probability"]),
        "prediction_v2_weight": pv2_weight if pv2_applied else 0.0,
        "dominant_model": fusion_decision["dominant_model"],
        "model_agreement": fusion_decision["model_agreement"],
        "expected_value": fusion_decision["expected_value"],
        "target_position_pct": fusion_decision["target_position_pct"],
    }

    action = None
    acted = False
    order_id = executed_price = executed_qty = None
    failure_reason = None
    message = fusion_decision.get("blocking_reason") or "; ".join(fusion_decision.get("reasons", [])[:1]) or "HOLD"

    if has_position:
        # 보유 포지션 관리(청산/전환/Scale)는 이미 검증된 ACTIVE_FUSION 판단을 그대로 쓴다.
        action = active_decision_result["action"]
        if action in (ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH):
            symbol = position_state["symbol"]
            price = _current_price(symbol, hynix_price, inverse_price)
            ratio = 1.0 if action in (ACTION_EXIT_ALL, ACTION_SWITCH) else max(0.01, min(1.0, active_decision_result["recommended_position_pct"] / 100.0))
            if not price:
                failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 매도 미실행"
            else:
                try:
                    sell_result = _sell_all_or_ratio(
                        broker, position, price, ratio, f"Adaptive Fusion({mode_name}) {action}", orders_this_cycle,
                        mode="mock", exit_reason_type="active_strategy", signal_source=signal_source,
                        fusion_metadata=fusion_metadata, position_manager=position_manager,
                    )
                except Exception as exc:
                    sell_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                if sell_result.get("success"):
                    acted = True
                    order_id, executed_price = sell_result.get("order_id"), price
                    executed_qty = sell_result.get("sold_quantity")
                    remaining = sell_result.get("remaining_quantity")
                    if sell_result.get("position_sync_status") == "POSITION_SYNC_PENDING" or remaining is None:
                        state["position_sync_block_new_orders"] = True
                        state["position_sync_status"] = "POSITION_SYNC_PENDING"
                        state["critical_alert"] = "POSITION_SYNC_PENDING - broker balance confirmation failed after sell"
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = state["critical_alert"]
                        return {
                            "acted": True, "message": message, "action": action,
                            "decision": fusion_decision, "final_decision": {}, "failure_reason": failure_reason,
                        }
                    if remaining <= 0:
                        state["position"] = {
                            "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
                            "entry_time": None, "name": None, "partial_tp1_done": False, "partial_sl1_done": False,
                        }
                        state["active_strategy_state"] = register_position_closed(state["active_strategy_state"], was_stop_loss=False, now=now)
                        state["adaptive_fusion_frequency_state"] = register_frequency_round_trip_closed(frequency_state, now)
                    else:
                        state["position"]["quantity"] = remaining
                    message = f"Adaptive Fusion 청산/축소: {symbol} {action} (비중 {ratio*100:.0f}%)"
                elif not failure_reason:
                    failure_reason = REASON_COOLDOWN if sell_result.get("blocked_by_coordinator") else REASON_ORDER_EXCEPTION
                    message = sell_result.get("message", "매도 실패")
    elif fusion_decision["executable"] and fused_action in (FUSION_HYNIX, FUSION_INVERSE) and fusion_decision.get("symbol"):
        symbol = fusion_decision["symbol"]
        pct = fusion_decision["target_position_pct"]
        price = _current_price(symbol, hynix_price, inverse_price)
        cycle_id = now.strftime("%Y%m%d%H%M")
        idem_key = generate_idempotency_key(now, mode_name, cycle_id, fused_action, symbol)
        if is_duplicate_signal(state["active_strategy_state"], idem_key):
            failure_reason, message = REASON_DUPLICATE_SIGNAL, f"중복 신호 차단(idempotency_key={idem_key})"
        elif not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 주문 미실행"
        elif pct <= 0:
            failure_reason, message = REASON_RISK_LIMIT, "권장 비중 0% — 주문 미실행"
        else:
            try:
                full_cash = float(broker.get_buyable_cash())
            except Exception as exc:
                full_cash, failure_reason, message = 0.0, REASON_ORDER_EXCEPTION, f"매수가능금액 조회 실패: {exc}"
            if full_cash > 0:
                cash_amount = full_cash * (pct / 100.0)
                if cash_amount < (price or 0):
                    failure_reason = REASON_INSUFFICIENT_CASH
                    message = f"매수가능금액 부족(필요 {price:,.0f}원, 가용 {cash_amount:,.0f}원)"
                else:
                    try:
                        buy_result = _buy_new(
                            broker, symbol, price, cash_amount, f"Adaptive Fusion({mode_name}) 진입 {pct:.0f}%",
                            orders_this_cycle, mode="mock", signal_source=signal_source, fusion_metadata=fusion_metadata,
                            position_manager=position_manager,
                        )
                    except Exception as exc:
                        buy_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                    if buy_result.get("success"):
                        if buy_result.get("position_sync_status") == "POSITION_SYNC_PENDING":
                            failure_reason = REASON_ORDER_EXCEPTION
                            message = buy_result.get("message", "POSITION_SYNC_PENDING")
                            state["position_sync_block_new_orders"] = True
                            state["position_sync_status"] = "POSITION_SYNC_PENDING"
                            state["critical_alert"] = message
                            return {
                                "acted": True, "message": message, "action": fused_action,
                                "decision": fusion_decision, "final_decision": {}, "failure_reason": failure_reason,
                            }
                        acted = True
                        action = ACTION_ENTER_HYNIX if symbol == "000660" else ACTION_ENTER_INVERSE
                        qty = buy_result.get("bought_quantity", 0)
                        order_id, executed_price, executed_qty = buy_result.get("order_id"), price, qty
                        state["position"] = {
                            "symbol": symbol, "name": symbol, "quantity": qty, "avg_price": price,
                            "entry_price": price, "entry_time": now.isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
                            "entry_type": fusion_decision.get("entry_type") or "NORMAL",
                        }
                        state["active_strategy_state"] = register_position_opened(state["active_strategy_state"], symbol, price, pct, now)
                        state["active_strategy_state"] = register_idempotency_key(state["active_strategy_state"], idem_key)
                        state["adaptive_fusion_frequency_state"] = register_frequency_entry(frequency_state, fused_action, now)
                        message = f"Adaptive Fusion 진입: {symbol} {pct:.0f}%({qty}주, {fusion_decision.get('entry_type') or 'NORMAL'}) — {signal_source}"
                    elif not failure_reason:
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = buy_result.get("message", "매수 실패")

    from app.trading.hynix_active_strategy_engine import build_final_execution_decision
    final_decision = build_final_execution_decision(
        action=(fused_action if not has_position else (active_decision_result.get("action") or "HOLD")),
        symbol=fusion_decision.get("symbol") or position_state.get("symbol"),
        target_position_pct=fusion_decision.get("target_position_pct", 0.0),
        confidence=fusion_decision.get("fused_confidence", 50.0), signal_source=signal_source,
        reasons=fusion_decision.get("reasons", []), executable=bool(acted),
        blocking_reason=(failure_reason if not acted else None),
    )
    final_decision.update(
        order_sent=acted, order_id=order_id, executed_price=executed_price, executed_qty=executed_qty,
        failure_reason=(failure_reason if not acted else None),
    )
    state["last_final_execution_decision"] = final_decision

    return {
        "acted": acted, "message": message, "action": action,
        "decision": fusion_decision, "final_decision": final_decision, "failure_reason": failure_reason,
    }


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

        # 매 사이클 Micron Proxy 결과를 state에 스냅샷으로 저장한다 — 이번 사이클에서
        # 원본 데이터가 없어 계산이 실패/생략되더라도 이 필드는 마지막 성공 값을 그대로
        # 유지하므로, UI는 "데이터 없음"으로 빈 화면을 보이는 대신 마지막 성공 계산
        # 결과 + 경과시간을 표시할 수 있다.
        state["last_micron_proxy_snapshot"] = {
            "real_micron_score": micron_proxy.get("real_micron_score"),
            "synthetic_micron_score": micron_proxy.get("synthetic_micron_score"),
            "effective_micron_score": effective_micron_score,
            "score_source": micron_proxy.get("micron_score_source"),
            "confidence": micron_proxy.get("micron_data_confidence"),
            "calculated_at": now.isoformat(timespec="seconds"),
        }

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


def compute_eod_regime_only(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """요구사항(2026-07-16) — 장 마감 후(is_within_operating_window()==False)에도
    오늘 저장된(또는 방금 조회 가능하면 최신) 1분봉으로 EOD(End-Of-Day) regime
    분석을 수행해 오늘 최종 장세를 표시한다. 이 함수는 시세 조회 + 장세분류 +
    state 저장만 한다 — run_switch_or_entry/run_tp_sl_if_needed/
    run_liquidation_if_needed 등 주문 실행 함수는 절대 호출하지 않는다.
    """
    from app.services.hynix_switch_state import with_state_lock
    from app.trading.adaptive_market_regime import compute_and_confirm_regime
    from app.data_sources.auto_market_collector import collect_hynix_minute, collect_hynix_daily

    now = now or kst_now()
    resolved_mode = mode or load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        state = load_state(mode=resolved_mode)
        try:
            minute_result = collect_hynix_minute(mode=resolved_mode)
            daily_result = collect_hynix_daily(mode=resolved_mode)
            eod_result = compute_and_confirm_regime(
                minute_result.get("df_1min"), prev_close=daily_result.get("prev_close"),
                confirmation_state=state.get("adaptive_regime_eod_confirmation"), now=now,
            )
            state["adaptive_regime_eod_confirmation"] = eod_result["confirmation_state"]
            state["adaptive_regime_eod"] = {k: v for k, v in eod_result.items() if k != "confirmation_state"}
            save_state_atomic(state)
            return state["adaptive_regime_eod"]
        except Exception as exc:
            logger.error("[HynixSwitchEngine] EOD regime 분석 실패: %s", exc)
            return {"error": str(exc)}


def _early_signal_symbol_agreement(etd_state: dict, fast_signal: dict, hynix_price: Optional[float], inverse_price: Optional[float]) -> Optional[bool]:
    """요구사항1 — "000660과 실제 ETF 방향 일치도". fast_signal 자체는 000660(SIGNAL_
    SYMBOL) 1분봉 기준이므로, 실제 거래종목(0193T0/0197X0) 현재가가 직전 틱 대비
    같은 방향으로 움직였는지를 가벼운 대리지표로 비교한다(별도 분봉 캐시를 새로
    수집하지 않는다). 비교 대상이 없는 최초 틱은 None(불확실 — 감점하지 않음)."""
    direction = fast_signal.get("direction")
    if direction not in ("UP", "DOWN"):
        return None
    prev = etd_state.get("prev_etf_prices") or {}
    prev_hynix, prev_inverse = prev.get(HYNIX_SYMBOL), prev.get(INVERSE_SYMBOL)
    etd_state["prev_etf_prices"] = {HYNIX_SYMBOL: hynix_price, INVERSE_SYMBOL: inverse_price}
    if direction == "UP":
        return (hynix_price >= prev_hynix) if (prev_hynix and hynix_price) else None
    return (inverse_price >= prev_inverse) if (prev_inverse and inverse_price) else None


def _load_etf_own_minute_cache(symbol: str):
    """desired_symbol(0193T0/0197X0) 자신의 1분봉을 로컬 캐시에서만 읽는다(새
    KIS 호출 없음) — 5초 주기 Early Detector 피드가 VWAP/구조돌파/거래량 급증을
    판단할 때 매 틱 새로 분봉 API를 부르지 않기 위함(요구사항1, KIS 호출량 안전)."""
    try:
        if symbol == HYNIX_SYMBOL:
            from app.data_sources.hynix_long_collector import _load_long_minute_cache

            return _load_long_minute_cache()
        if symbol == INVERSE_SYMBOL:
            from app.data_sources.hynix_inverse_collector import _load_inverse_minute_cache

            return _load_inverse_minute_cache()
    except Exception:
        return None
    return None


def _quote_timestamp(quote: dict, fallback: datetime) -> Optional[datetime]:
    if not isinstance(quote, dict):
        return None
    for key in ("timestamp", "collected_at", "updated_at", "as_of", "last_update_time"):
        raw = quote.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
    if quote.get("stale") or quote.get("status") == "stale_cache" or quote.get("source") == "cache":
        return None
    return fallback


def _data_time_mismatch_status(quotes: dict[str, dict], fetched_at: dict[str, datetime], *, now: datetime) -> dict:
    entries = {}
    timestamps = []
    for symbol, quote in quotes.items():
        ts = _quote_timestamp(quote, fetched_at.get(symbol, now))
        age = (now - ts).total_seconds() if ts is not None else None
        entries[symbol] = {
            "timestamp": ts.isoformat() if ts is not None else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "source": (quote or {}).get("source"),
            "status": (quote or {}).get("status"),
            "stale": bool((quote or {}).get("stale")),
        }
        if ts is not None:
            timestamps.append(ts)
    max_delta = None
    if len(timestamps) == len(quotes) and timestamps:
        max_delta = (max(timestamps) - min(timestamps)).total_seconds()
    blocked = len(timestamps) != len(quotes) or (max_delta is not None and max_delta > 5.0)
    return {
        "blocked": blocked,
        "reason_code": "DATA_TIME_MISMATCH" if blocked else None,
        "max_delta_seconds": round(max_delta, 3) if max_delta is not None else None,
        "symbols": entries,
    }


def _early_reason_code(reason: Optional[str]) -> str:
    text = str(reason or "")
    upper = text.upper()
    if "SIGNAL_EXPIRED" in upper:
        return "SIGNAL_EXPIRED"
    if "DATA_TIME_MISMATCH" in upper:
        return "DATA_TIME_MISMATCH"
    if "MICRO_CHOP" in upper:
        return "MICRO_CHOP"
    if "COOLDOWN" in upper:
        return "REENTRY_COOLDOWN"
    if "BUYABLE_CASH" in upper or "ORDER_QTY_ZERO" in upper or "PRICE_UNAVAILABLE" in upper:
        return "ETF_DATA_INSUFFICIENT"
    if "CHASE_BLOCK" in upper:
        return "CHASE_BLOCK"
    if "COST_EDGE" in upper:
        return "COST_EDGE_BLOCK"
    if "TARGET_ALREADY_FILLED" in upper:
        return "TARGET_ALREADY_FILLED"
    if "시간" in text or "09:" in text:
        return "TIME_GATE_BLOCK"
    if "CHASE_BLOCK" in text or "추격" in text or "고점" in text or "저점" in text:
        return "CHASE_BLOCK"
    if "거래비용" in text or "예상순이익" in text:
        return "COST_EDGE_BLOCK"
    if "쿨다운" in text or "재진입" in text:
        return "REENTRY_COOLDOWN"
    if "현재가" in text or "ETF" in text and "실패" in text:
        return "ETF_DATA_INSUFFICIENT"
    if "방향" in text and "불일치" in text:
        return "ETF_DIRECTION_MISMATCH"
    if "이미 목표" in text or "추가 진입이 필요" in text or "단계 유지" in text:
        return "TARGET_ALREADY_FILLED"
    return "NO_EARLY_SIGNAL"


def _augment_fast_signal_with_enhanced_approval(fast_signal: dict, final_action: str, decision: dict) -> dict:
    """요구사항(2026-07-21 방향편향 수정) — Enhanced(raw_score_leader/final_action)는
    더 이상 Early Detector의 실제 진입 방향(actionable_direction)을 덮어쓰지 않는다.

    과거에는 여기서 final_action을 근거로 fast_signal의 direction/up_votes/down_votes/
    returns를 강제로 갈아끼웠다 — 이는 "raw_score_leader는 참고 표시로만 사용하고
    실제 주문 방향은 actionable_direction만 사용한다"는 원칙을 어기고, 원점수(raw
    score)가 실시간 방향(actionable_direction)을 역전시킬 수 있는 방향편향의 원인이었다.

    이제는 final_action을 참고용 표시 필드로만 첨부하고, fast_signal 본체(이번 틱에
    실제 계산된 5/10/20/30초 기울기·투표 기반 direction/up_votes/down_votes/returns)는
    전혀 건드리지 않는다 — 실제 진입 방향은 항상 Early Detector가 이번 틱에 직접
    계산한 live fast_signal.direction(actionable_direction)만 따른다.
    """
    result = dict(fast_signal or {})
    result["raw_score_leader_final_action"] = final_action
    result["raw_score_leader_reference_only"] = True
    return result


def _record_early_result_on_trace(trace: dict, early_result: Optional[dict]) -> None:
    if early_result is None:
        trace["early_decision"] = {
            "attempted": False, "skipped": True, "reason_code": "NO_EARLY_SIGNAL",
            "reason": "Early Detector did not run for this cycle",
        }
        trace["early_order_result"] = None
        return

    switch = early_result.get("switch") or {}
    orders = [o for o in (switch.get("orders") or []) if isinstance(o, dict)]
    sent_orders = [o for o in orders if o.get("action") in ("BUY", "SELL")]
    succeeded_orders = [o for o in sent_orders if o.get("success")]
    reason_code = early_result.get("reason_code") or _early_reason_code(early_result.get("reason") or switch.get("message"))
    trace["early_decision"] = {
        "attempted": True,
        "skipped": bool(early_result.get("skipped")),
        "reason_code": reason_code,
        "reason": early_result.get("reason") or switch.get("message"),
        "stage": early_result.get("stage"),
        "target_pct": early_result.get("target_pct") or early_result.get("expanded_to") or early_result.get("staged_to"),
        "signal_direction": (early_result.get("signal") or {}).get("direction"),
        "signal_score": (early_result.get("signal") or {}).get("score"),
    }
    trace["early_order_result"] = {
        "order_sent": bool(sent_orders),
        "broker_executed": bool(succeeded_orders),
        "execution_stage": switch.get("stage"),
        "execution_message": switch.get("message"),
        "order_failure_code": switch.get("failure_code"),
        "broker_error": switch.get("broker_error"),
        "requested_symbol": switch.get("requested_symbol"),
        "requested_qty": switch.get("requested_qty"),
        "order_price": switch.get("order_price"),
        "buyable_cash": switch.get("buyable_cash"),
        "sized_cash": switch.get("sized_cash"),
        "orders": orders,
    }


def _run_early_trend_detector_tick(
    *, state: dict, mode: str, now: datetime, fast_signal: dict, df_1min,
    confirmed_regime: Optional[str], broker, position_manager,
    hynix_price: Optional[float], inverse_price: Optional[float],
    live_slopes: Optional[dict] = None,
) -> Optional[dict]:
    """Early Trend Detector — Adaptive Market Regime 하위의 제한적 탐색진입 엔진.

    최종 주문권한/최대비중은 항상 Adaptive Regime(confirmed_regime)이 결정한다 —
    이 함수는 (a) 무포지션일 때 최초 탐색진입(10~15%→30%→50%)만 시작하거나,
    (b) 이미 이 엔진이 만든 EARLY_PROBE 포지션을 들고 있을 때 단계 진행 또는
    STRONG_UP/DOWN 확정 후 확대(50%)만 판단한다. 조기진입의 철수(고정 -0.4%
    또는 VOLATILE_RANGE TP1/TP2/SL/신호약화/30초 미확인)는 1초 주기 Dynamic
    Exit Watcher가 담당한다(이 함수는 5초 주기 피드에서 호출돼도 철수는 항상
    가장 빠른 워처가 전담한다).

    live_slopes: app.trading.early_trend_live_feed로 5초 주기로 쌓은
    {symbol: {"direction": ..., "slopes": {...}}} — 1분봉 vote보다 먼저 반전을
    잡기 위한 입력(요구사항1).

    반환값이 None이면 "이 틱에서 관여하지 않음"을 뜻하며, 호출부는 기존 레거시
    로직(PRIMARY_TREND 기반 Fast Watcher)을 그대로 이어서 실행해야 한다.
    """
    from app.trading import early_trend_detector as etd
    from app.trading.etf_entry_confirmation import compute_etf_breakouts, compute_etf_volume_surge

    live_slopes = live_slopes or {}
    position = state.get("position") or {}
    held_symbol = position.get("symbol")
    today = now.strftime("%Y%m%d")
    etd_state = dict(state.get("early_trend_detector") or {})
    freq = etd.reset_frequency_state_if_new_day(etd_state.get("frequency"), today)
    etd_state["frequency"] = freq
    live = bool(state.get("early_trend_detector_live"))
    etd_state["engine_mode"] = "LIVE" if live else "SHADOW"
    etd_state["order_worker_name"] = etd_state.get("order_worker_name") or "MAIN_CYCLE"

    if held_symbol and position.get("entry_type") != etd.ENTRY_TYPE_EARLY_PROBE:
        # 이미 이 엔진이 아닌 다른 경로로 만들어진 일반 포지션을 보유 중이다 —
        # Early Detector는 관여하지 않고 기존 로직에 맡긴다.
        state["early_trend_detector"] = etd_state
        return None

    if held_symbol is None:
        # ── 최초 탐색진입 판단 ──────────────────────────────────────────────
        halted, remaining = etd.is_halted(freq, now)
        if halted:
            etd_state["last_block_reason"] = f"가짜신호 서킷브레이커 — {remaining / 60.0:.0f}분 후 재개"
            etd_state["candidate"] = {}
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "REENTRY_COOLDOWN"}

        data_time_status = etd_state.get("data_time_status") or {}
        if data_time_status.get("blocked"):
            etd_state["last_block_reason"] = "DATA_TIME_MISMATCH"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "DATA_TIME_MISMATCH"}

        signal_symbol_agreement = _early_signal_symbol_agreement(etd_state, fast_signal, hynix_price, inverse_price)

        # 요구사항1 — live_slopes는 종목별(HYNIX_SYMBOL/INVERSE_SYMBOL) 자기 자신의
        # 가격 기울기다. INVERSE_SYMBOL은 기초자산과 반대로 움직이므로, "기초자산
        # 방향" 기준 단일 판단으로 정규화한다(레버리지 상승=기초자산 UP, 인버스
        # 상승=기초자산 DOWN). 두 심볼의 함의가 서로 다르면(둘 다 있는데 불일치)
        # 아직 불확실로 본다 — 한쪽만 있으면 그 값을 그대로 쓴다.
        _prev_signal_direction = (etd_state.get("last_signal") or {}).get("direction")
        _prev_signal_score = (etd_state.get("last_signal") or {}).get("score")
        _live_hynix_dir = (live_slopes.get(HYNIX_SYMBOL) or {}).get("direction")
        _live_inverse_dir = (live_slopes.get(INVERSE_SYMBOL) or {}).get("direction")
        _live_inverse_implied = {"UP": "DOWN", "DOWN": "UP"}.get(_live_inverse_dir)
        _live_direction_candidates = [d for d in (_live_hynix_dir, _live_inverse_implied) if d]
        _live_direction_for_data = (
            _live_direction_candidates[0]
            if _live_direction_candidates and len(set(_live_direction_candidates)) == 1
            else None
        )

        # 두 후보 방향(UP=HYNIX_SYMBOL, DOWN=INVERSE_SYMBOL) 중 지금 판단 대상이
        # 되는 쪽의 ETF 자체 VWAP 이탈/1분봉 구조돌파/거래량 급증을 미리 계산한다
        # — 1분봉 vote가 아직 확정하지 못했어도 실시간 기울기가 먼저 확정되면
        # 그 방향으로 조기신호를 만들 수 있게 한다(요구사항1).
        _vote_direction = fast_signal.get("direction") if fast_signal.get("direction") in ("UP", "DOWN") else None
        _candidate_direction_for_data = _vote_direction or _live_direction_for_data or _prev_signal_direction
        _probe_symbol = HYNIX_SYMBOL if _candidate_direction_for_data == "UP" else INVERSE_SYMBOL if _candidate_direction_for_data == "DOWN" else None
        _etf_df = _load_etf_own_minute_cache(_probe_symbol) if _probe_symbol else None
        if _etf_df is not None and "datetime" in _etf_df.columns:
            try:
                _etf_df = _etf_df[_etf_df["datetime"] <= now].copy()
                if _etf_df.empty:
                    _etf_df = None
            except Exception:
                pass
        _etf_price_for_data = hynix_price if _probe_symbol == HYNIX_SYMBOL else inverse_price if _probe_symbol == INVERSE_SYMBOL else None
        _breakouts = compute_etf_breakouts(_etf_df, _etf_price_for_data, _candidate_direction_for_data) if _probe_symbol else {}
        _volume_surge = compute_etf_volume_surge(_etf_df) if _etf_df is not None else None
        _micro_chop = etd.update_micro_chop_state(
            etd_state.get("micro_chop"),
            direction=_live_direction_for_data,
            vwap_crossed=bool(_breakouts.get("vwap_breakout")),
            reversal_exit=False,
            move_efficiency=None,
            now=now,
        )
        etd_state["micro_chop"] = _micro_chop

        early_signal = etd.compute_composite_early_signal(
            fast_signal=fast_signal, signal_symbol_agreement=signal_symbol_agreement,
            live_direction=_live_direction_for_data, etf_vwap_breakout=_breakouts.get("vwap_breakout"),
            etf_structure_breakout=_breakouts.get("structure_breakout"), etf_volume_surge=_volume_surge,
        )
        direction = early_signal.get("direction")
        etd_state["last_signal"] = early_signal

        # 요구사항4 — 이전 후보 방향과 반대로 확정되면(1분봉 vote 또는 실시간
        # 기울기 어느 쪽이든) 즉시 기존 방향 점수를 70% 감쇠하고 그 방향 재진입을
        # 지금부터 다시 쿨다운 처리한다 — 확인 스트릭은 아래에서 candidate가 새로
        # 만들어지며 자연히 리셋된다.
        _prev_candidate = etd_state.get("candidate") or {}
        _prev_candidate_direction = _prev_candidate.get("direction")
        if direction in ("UP", "DOWN") and _prev_candidate_direction and direction != _prev_candidate_direction:
            freq = etd.apply_opposite_change_point_reaction(freq, _prev_candidate_direction, _prev_signal_score, now)
            etd_state["frequency"] = freq

        if direction not in ("UP", "DOWN") or early_signal["score"] < 50.0:
            etd_state["candidate"] = {}
            etd_state["last_block_reason"] = "조기신호 없음/약함"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "NO_EARLY_SIGNAL", "signal": early_signal}

        desired_symbol = HYNIX_SYMBOL if direction == "UP" else INVERSE_SYMBOL
        current_etf_price = hynix_price if direction == "UP" else inverse_price
        if not current_etf_price:
            etd_state["last_block_reason"] = "ETF 현재가 조회 실패"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "ETF_DATA_INSUFFICIENT", "signal": early_signal}
        if signal_symbol_agreement is False:
            etd_state["last_block_reason"] = "ETF_DIRECTION_MISMATCH — 기초자산 신호와 실제 ETF 방향 불일치"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "ETF_DIRECTION_MISMATCH", "signal": early_signal}
        if _micro_chop.get("active") and not (_breakouts.get("vwap_breakout") or _breakouts.get("structure_breakout")):
            etd_state["last_block_reason"] = "MICRO_CHOP: VWAP/swing 돌파 없는 박스권 신규진입 차단"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "MICRO_CHOP", "signal": early_signal}

        candidate = dict(etd_state.get("candidate") or {})
        if candidate.get("direction") != direction:
            candidate = {
                "direction": direction, "first_detected_at": now.isoformat(), "reference_price": current_etf_price,
                "change_point_detected_at": now.isoformat(), "direction_confirmed_at": now.isoformat(),
            }
        try:
            _candidate_detected_at = datetime.fromisoformat(candidate["first_detected_at"])
        except Exception:
            _candidate_detected_at = now
            candidate["first_detected_at"] = now.isoformat()
        signal_id = candidate.get("signal_id") or etd.make_signal_id(direction, _candidate_detected_at)
        episode_id = candidate.get("episode_id") or etd.make_episode_id(direction, _candidate_detected_at)
        candidate["signal_id"] = signal_id
        candidate["episode_id"] = episode_id
        candidate["signal_age_seconds"] = round(max(0.0, (now - _candidate_detected_at).total_seconds()), 3)
        etd_state["candidate"] = candidate

        try:
            first_detected_at = datetime.fromisoformat(candidate["first_detected_at"])
        except Exception:
            first_detected_at = now
        elapsed = max(0.0, (now - first_detected_at).total_seconds())
        latency = dict(etd_state.get("latency") or etd.default_latency_trace(
            signal_id=signal_id, worker_name=etd_state.get("order_worker_name") or "MAIN_CYCLE"
        ))
        latency["signal_id"] = signal_id
        latency["worker_name"] = etd_state.get("order_worker_name") or latency.get("worker_name") or "MAIN_CYCLE"
        latency["detected_at"] = candidate.get("first_detected_at")
        latency["direction_confirmed_at"] = candidate.get("direction_confirmed_at") or now.isoformat()
        latency["main_cycle_waiting"] = latency.get("worker_name") != "EARLY_FAST_WORKER"
        latency = etd.mark_latency(latency, "gates_started_at", now)
        etd_state["latency"] = latency
        etd_state["signal_id"] = signal_id
        etd_state["episode_id"] = episode_id
        etd_state["signal_age_seconds"] = candidate["signal_age_seconds"]

        validity = etd.signal_validity(candidate.get("first_detected_at"), now)
        etd_state["signal_validity"] = validity
        if validity == "EXPIRED":
            etd_state["last_block_reason"] = etd.SIGNAL_EXPIRED_CODE
            etd_state["signal_expired"] = True
            etd_state["candidate"] = {}
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": etd.SIGNAL_EXPIRED_CODE, "signal": early_signal, "signal_id": signal_id, "episode_id": episode_id}
        if validity == "REVALIDATE" and _live_direction_for_data != direction:
            etd_state["last_block_reason"] = "SIGNAL_EXPIRED: 30~60초 신호 즉시 재검증 실패"
            etd_state["signal_expired"] = True
            etd_state["candidate"] = {}
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": etd.SIGNAL_EXPIRED_CODE, "signal": early_signal, "signal_id": signal_id, "episode_id": episode_id}

        if etd.episode_first_entry_done(etd_state, episode_id):
            etd_state["last_block_reason"] = "TARGET_ALREADY_FILLED: trend_episode 최초 진입 이미 실행"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "TARGET_ALREADY_FILLED", "signal": early_signal, "signal_id": signal_id, "episode_id": episode_id}

        if etd.is_same_direction_cooldown_active(freq, direction, now):
            etd_state["last_block_reason"] = "동일 방향 재진입 쿨다운(3분)"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "REENTRY_COOLDOWN", "signal": early_signal}

        chase = etd.evaluate_chase_block(
            signal_reference_price=candidate.get("reference_price"), current_price=current_etf_price,
            confirmed_regime=confirmed_regime, df_1min=_etf_df, direction="UP",
        )
        etd_state["chase"] = chase
        if chase["blocked"]:
            _chase_reason = "; ".join(chase["reasons"]) or "CHASE_BLOCK"
            try:
                if _etf_df is not None and current_etf_price:
                    _recent = _etf_df.sort_values("datetime").iloc[-1:]
                    _recent_high = float(_recent["high"].max())
                    _recent_low = float(_recent["low"].min())
                    _distance_to_high_pct = round((_recent_high - float(current_etf_price)) / _recent_high * 100.0, 4) if _recent_high else None
                    _chase_reason = (
                        f"{_chase_reason} | ETF={desired_symbol} price={current_etf_price} "
                        f"recent_1m_high={_recent_high} recent_1m_low={_recent_low} "
                        f"distance_to_high_pct={_distance_to_high_pct}"
                    )
                    chase.update({
                        "symbol": desired_symbol,
                        "recent_1m_high": _recent_high,
                        "recent_1m_low": _recent_low,
                        "distance_to_high_pct": _distance_to_high_pct,
                    })
            except Exception:
                pass
            etd_state["last_block_reason"] = _chase_reason
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "CHASE_BLOCK", "signal": early_signal}

        _returns = fast_signal.get("returns") or {}
        expected_move_pct = max(abs(float(_returns.get(k) or 0.0)) for k in ("1m", "3m", "5m"))
        cost_gate = etd.evaluate_cost_gate(desired_symbol, expected_move_pct)
        etd_state["cost_gate"] = cost_gate
        if cost_gate["blocked"]:
            def _fmt_pct(value) -> str:
                try:
                    return f"{float(value):.2f}%"
                except (TypeError, ValueError):
                    return "n/a"

            def _fmt_ratio(value) -> str:
                try:
                    return f"{float(value):.2f}x"
                except (TypeError, ValueError):
                    return "n/a"

            etd_state["last_block_reason"] = (
                f"거래비용 게이트 — 예상Gross {_fmt_pct(cost_gate.get('expected_gross_edge_pct'))}, "
                f"비용 {_fmt_pct(cost_gate.get('cost_pct'))}, 예상순이익 {_fmt_pct(cost_gate.get('net_edge_pct'))}, "
                f"Gross/비용 {_fmt_ratio(cost_gate.get('gross_to_cost_ratio'))}"
            )
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "COST_EDGE_BLOCK", "signal": early_signal}

        # 요구사항2 — 50% 단계는 30초 유지 "그리고" ETF/기초자산 방향 일치일 때만.
        direction_aligned = bool(signal_symbol_agreement)
        stage, target_pct = etd.compute_target_probe_pct(confirmed_regime, elapsed, direction_aligned=direction_aligned)
        etd_state["stage"], etd_state["target_pct"] = stage, target_pct
        if target_pct <= 0.0:
            etd_state["last_block_reason"] = f"{confirmed_regime} 장세는 조기진입 금지"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "NO_EARLY_SIGNAL", "signal": early_signal}

        if not current_etf_price:
            etd_state["last_block_reason"] = "ETF 현재가 조회 실패"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "ETF_DATA_INSUFFICIENT"}

        etd_state["signal_price"] = candidate.get("reference_price")
        etd_state["current_price"] = current_etf_price
        etd_state["last_block_reason"] = None
        moved_pct_since_signal = None
        if candidate.get("reference_price") and current_etf_price:
            moved_pct_since_signal = round(abs(current_etf_price / candidate["reference_price"] - 1.0) * 100.0, 4)
        etd_state["etf_move_pct_since_signal"] = moved_pct_since_signal
        latency = etd.mark_latency(etd_state.get("latency"), "gates_completed_at", now)
        etd_state["latency"] = latency

        if not live:
            state["early_trend_detector"] = etd_state
            return {
                "skipped": True, "reason": "SHADOW 모드 — 계산만 하고 실제 진입은 하지 않음",
                "reason_code": "NO_EARLY_SIGNAL", "signal": early_signal, "stage": stage, "target_pct": target_pct,
                "signal_id": signal_id, "episode_id": episode_id, "latency": latency,
            }

        final_action = "HYNIX_BUY" if direction == "UP" else "INVERSE_BUY"
        latency = etd.mark_latency(etd_state.get("latency"), "order_requested_at", now)
        etd_state["latency"] = latency
        # 요구사항7(2026-07-21) — detected→order_requested 등 latency를 실운영
        # 기준으로 집계(median/p95/60초 이상 신호 건수)하려면 신호별 trace가
        # 하루 단위로 남아 있어야 한다. 성공/실패와 무관하게 "주문을 실제로
        # 시도한 시점"의 latency는 항상 기록한다.
        etd.log_latency_trace(latency, now)
        switch = run_switch_or_entry(
            state, broker, final_action, hynix_price, inverse_price, now=now, forced=True,
            reason=f"EARLY_TREND_DETECTOR 조기진입({stage})", position_manager=position_manager,
            target_position_pct=target_pct, entry_type=etd.ENTRY_TYPE_EARLY_PROBE, stop_loss_pct=etd.FIXED_EARLY_STOP_PCT,
            signal_source=etd.signal_source_for_stage(stage),
        )
        if switch.get("acted") and any(bool(o.get("success")) for o in (switch.get("orders") or []) if isinstance(o, dict)):
            latency = etd.mark_latency(latency, "broker_accepted_at", now)
            latency = etd.mark_latency(latency, "fill_confirmed_at", now)
            probe = etd.default_probe_state()
            probe.update({
                "active": True, "direction": direction, "detected_at": candidate["first_detected_at"],
                "signal_reference_price": candidate.get("reference_price"), "stage": stage, "position_pct": target_pct,
                "last_reconfirmed_at": now.isoformat(),
                "change_point_detected_at": candidate.get("change_point_detected_at", candidate["first_detected_at"]),
                "initial_probe_entered_at": now.isoformat(),
                "signal_to_fill_latency_seconds": round((now - first_detected_at).total_seconds(), 2),
                "signal_id": signal_id, "episode_id": episode_id,
            })
            etd_state["probe"] = probe
            etd_state["frequency"] = etd.register_probe_entry(freq, direction, now)
            etd_state = etd.register_episode_first_entry(etd_state, episode_id, signal_id, now)
            etd_state["candidate"] = {}
            state["actual_order_driver"] = "EARLY_TREND_DETECTOR"
            position_manager.sync(force=True)
            latency = etd.mark_latency(latency, "position_synced_at", kst_now())
            etd_state["latency"] = latency
            apply_position_manager_to_state(state, position_manager)
            state["early_trend_detector"] = etd_state
            return {"skipped": False, "signal": early_signal, "switch": switch, "stage": stage, "target_pct": target_pct, "reason_code": None, "signal_id": signal_id, "episode_id": episode_id, "latency": latency}
        etd_state["last_block_reason"] = switch.get("message") or "TARGET_ALREADY_FILLED"
        state["early_trend_detector"] = etd_state
        return {
            "skipped": True, "reason": etd_state["last_block_reason"],
            "reason_code": _early_reason_code(switch.get("failure_code") or etd_state["last_block_reason"]),
            "signal": early_signal, "switch": switch, "stage": stage, "target_pct": target_pct,
            "signal_id": signal_id, "episode_id": episode_id, "latency": etd_state.get("latency"),
        }

    # ── 이미 EARLY_PROBE 보유 중 — 단계 진행/확대만 판단(철수는 Dynamic Exit Watcher 담당) ──
    probe = dict(etd_state.get("probe") or etd.default_probe_state())
    direction = probe.get("direction")
    try:
        detected_at = datetime.fromisoformat(probe["detected_at"]) if probe.get("detected_at") else now
    except Exception:
        detected_at = now
    elapsed = max(0.0, (now - detected_at).total_seconds())

    holding_inverse = held_symbol == INVERSE_SYMBOL
    current_etf_price = inverse_price if holding_inverse else hynix_price
    etd_state["signal_price"] = probe.get("signal_reference_price")
    etd_state["current_price"] = current_etf_price
    etd_state["stage"] = probe.get("stage")
    etd_state["target_pct"] = probe.get("position_pct")
    if probe.get("signal_reference_price") and current_etf_price:
        etd_state["etf_move_pct_since_signal"] = round(abs(current_etf_price / probe["signal_reference_price"] - 1.0) * 100.0, 4)

    if not live:
        state["early_trend_detector"] = etd_state
        return None  # SHADOW 모드에서는 확대/단계진행도 실행하지 않고 기존 로직에 맡긴다.

    def _order_succeeded(switch_result: dict) -> bool:
        # 요구사항 — run_switch_or_entry()는 주문이 브로커에 거부돼도(예: 당일
        # 중복매수 등) "acted": True를 반환할 수 있다("시도했다"는 뜻이지 "성공했다"는
        # 뜻이 아니다) — 실제 체결 성공 여부는 orders[].success로만 판단한다.
        return any(bool(o.get("success")) for o in (switch_result.get("orders") or []) if isinstance(o, dict))

    expansion_pct = etd.expansion_target_pct(confirmed_regime, direction, holding_inverse)
    if expansion_pct and expansion_pct > (probe.get("position_pct") or 0.0):
        final_action = "HYNIX_BUY" if direction == "UP" else "INVERSE_BUY"
        switch = run_switch_or_entry(
            state, broker, final_action, hynix_price, inverse_price, now=now, forced=True,
            reason=f"EARLY_TREND_DETECTOR 확대(confirmed {confirmed_regime})", position_manager=position_manager,
            target_position_pct=expansion_pct, entry_type="CONFIRMED",
            signal_source=etd.signal_source_for_stage(etd.STAGE_CONFIRMED_EXPANDED),
        )
        if _order_succeeded(switch):
            probe.update({"stage": etd.STAGE_CONFIRMED_EXPANDED, "position_pct": expansion_pct, "expanded": True})
            etd_state["probe"] = probe
            state["early_trend_detector"] = etd_state
            state["actual_order_driver"] = "EARLY_TREND_DETECTOR"
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            return {"skipped": False, "switch": switch, "expanded_to": expansion_pct, "reason_code": None}

    # 요구사항2 — 50% 단계는 30초 유지 "그리고" 방향 일치일 때만(held_symbol이
    # 이미 그 방향으로 진입해 보유 중이므로, 실시간 기울기가 같은 방향을
    # 가리키는지로 "지금도 정렬돼 있는지"를 재확인한다). INVERSE_SYMBOL은
    # 기초자산과 반대로 움직이므로 "기초자산 방향" 기준으로 정규화해서 비교한다.
    _held_live_raw_direction = (live_slopes.get(held_symbol) or {}).get("direction")
    _held_live_direction = (
        {"UP": "DOWN", "DOWN": "UP"}.get(_held_live_raw_direction) if holding_inverse else _held_live_raw_direction
    )
    direction_aligned = (
        _held_live_direction == direction if _held_live_direction else fast_signal.get("direction") == direction
    )
    new_stage, new_target_pct = etd.compute_target_probe_pct(confirmed_regime, elapsed, direction_aligned=direction_aligned)
    if new_target_pct > (probe.get("position_pct") or 0.0):
        final_action = "HYNIX_BUY" if direction == "UP" else "INVERSE_BUY"
        switch = run_switch_or_entry(
            state, broker, final_action, hynix_price, inverse_price, now=now, forced=True,
            reason=f"EARLY_TREND_DETECTOR 단계진행({new_stage})", position_manager=position_manager,
            target_position_pct=new_target_pct, entry_type=etd.ENTRY_TYPE_EARLY_PROBE, stop_loss_pct=etd.FIXED_EARLY_STOP_PCT,
            signal_source=etd.signal_source_for_stage(new_stage),
        )
        if _order_succeeded(switch):
            probe.update({"stage": new_stage, "position_pct": new_target_pct, "last_reconfirmed_at": now.isoformat()})
            etd_state["probe"] = probe
            state["early_trend_detector"] = etd_state
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            return {"skipped": False, "switch": switch, "staged_to": new_target_pct, "reason_code": None}

    state["early_trend_detector"] = etd_state
    return {"skipped": True, "reason": "단계 유지", "reason_code": "TARGET_ALREADY_FILLED"}


def run_early_trend_fast_feed_tick(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """요구사항(2026-07-20 최종) — Early Trend Detector 전용 5초 주기 경량 틱.

    calculate_enhanced_hynix_prediction_score() 등 무거운 전체 재계산(30초
    주기 run_fast_trend_watcher_tick이 계속 담당)은 다시 하지 않는다 — KIS
    분봉/계좌 API를 새로 늘리지 않기 위해서다. 이 함수는 오직:
      (a) 가벼운 현재가 조회 2건(collect_long_current/collect_inverse_current)만으로
          진짜 5/10/20/30초 기울기(app.trading.early_trend_live_feed)를 쌓고,
      (b) 최근(최대 30초 전) 계산된 fast_signal/confirmed_regime을 그대로 재사용해
          Early Trend Detector의 신규진입/단계진행/확대 판단만 더 빠른 주기로 수행한다.
    반대 change-point 발생 시의 즉시 철수는 1초 주기 Dynamic Exit Watcher가 이
    live_slopes를 읽어 담당한다(app.trading.dynamic_exit_watcher._tick_locked)."""
    from app.services.hynix_switch_state import with_state_lock
    from app.trading import early_trend_live_feed as feed
    from app.trading import early_trend_detector as etd

    now = now or kst_now()
    resolved_mode = mode or load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        state = load_state(mode=resolved_mode)
        state["mode"] = resolved_mode
        if not state.get("auto_trade_on") or state.get("stopped"):
            return {"skipped": True, "reason": "auto off or stopped"}
        if not state.get("early_trend_detector_enabled"):
            return {"skipped": True, "reason": "early trend detector disabled"}

        try:
            from app.data_sources.hynix_long_collector import collect_long_current
            from app.data_sources.hynix_inverse_collector import collect_inverse_current
            from app.data_sources.auto_market_collector import _fetch_hynix_current_from_kis

            def _fetch_signal_quote():
                fetched_at = kst_now()
                price = _fetch_hynix_current_from_kis(resolved_mode)
                return fetched_at, {
                    "symbol": SIGNAL_SYMBOL,
                    "current_price": price,
                    "source": "kis_direct",
                    "status": "success" if price is not None else "unavailable",
                    "timestamp": fetched_at.isoformat(),
                    "stale": price is None,
                }

            def _fetch_long_quote():
                quote = collect_long_current(mode=resolved_mode)
                return kst_now(), quote

            def _fetch_inverse_quote():
                quote = collect_inverse_current(mode=resolved_mode)
                return kst_now(), quote

            with ThreadPoolExecutor(max_workers=3) as pool:
                fs = pool.submit(_fetch_signal_quote)
                fl = pool.submit(_fetch_long_quote)
                fi = pool.submit(_fetch_inverse_quote)
                signal_fetched_at, signal_quote = fs.result()
                long_fetched_at, long_quote = fl.result()
                inverse_fetched_at, inverse_quote = fi.result()
            signal_quote = {
                **signal_quote,
                "timestamp": signal_quote.get("timestamp") or signal_fetched_at.isoformat(),
            }
        except Exception as exc:
            logger.debug("[EarlyTrendFastFeed] 현재가 조회 실패: %s", exc)
            return {"skipped": True, "reason": f"price fetch failed: {exc}"}

        long_price = long_quote.get("current_price")
        inverse_price = inverse_quote.get("current_price")
        signal_price = signal_quote.get("current_price")

        etd_state = dict(state.get("early_trend_detector") or {})
        data_time_status = _data_time_mismatch_status(
            {
                SIGNAL_SYMBOL: signal_quote,
                HYNIX_SYMBOL: long_quote,
                INVERSE_SYMBOL: inverse_quote,
            },
            {
                SIGNAL_SYMBOL: signal_fetched_at,
                HYNIX_SYMBOL: long_fetched_at,
                INVERSE_SYMBOL: inverse_fetched_at,
            },
            now=now,
        )
        etd_state["data_time_status"] = data_time_status
        history = etd_state.get("price_history") or {}
        history = feed.record_price_sample(history, SIGNAL_SYMBOL, signal_price, now)
        history = feed.record_price_sample(history, HYNIX_SYMBOL, long_price, now)
        history = feed.record_price_sample(history, INVERSE_SYMBOL, inverse_price, now)
        etd_state["price_history"] = history
        etd_state["live_slopes"] = {
            SIGNAL_SYMBOL: feed.compute_live_direction(history, SIGNAL_SYMBOL, now),
            HYNIX_SYMBOL: feed.compute_live_direction(history, HYNIX_SYMBOL, now),
            INVERSE_SYMBOL: feed.compute_live_direction(history, INVERSE_SYMBOL, now),
        }
        live_trade = feed.compute_live_trade_direction(
            history, now, signal_symbol=SIGNAL_SYMBOL, long_symbol=HYNIX_SYMBOL, inverse_symbol=INVERSE_SYMBOL,
        )
        previous_live_direction = (state.get("live_trade_direction") or {}).get("direction")
        cached_fast_signal = (state.get("fast_trend_watcher") or {}).get("last_signal") or {}
        if live_trade.get("direction") in ("UP", "DOWN"):
            live_direction = live_trade["direction"]
            cached_fast_signal = {
                **cached_fast_signal,
                "direction": live_direction,
                "signal_id": f"FAST_LIVE:{live_direction}:{now.strftime('%Y%m%d%H%M%S')}",
                "up_votes": max(int(cached_fast_signal.get("up_votes") or 0), 6 if live_direction == "UP" else 0),
                "down_votes": max(int(cached_fast_signal.get("down_votes") or 0), 6 if live_direction == "DOWN" else 0),
                "returns": {"3m": max(abs((cached_fast_signal.get("returns") or {}).get("3m") or 0.0), 1.0)},
                "top_factors": list(cached_fast_signal.get("top_factors") or []) + ["EARLY_FAST_WORKER actionable live direction"],
            }
        factors = {
            "signal_slope_reversal": bool(
                previous_live_direction
                and (etd_state["live_slopes"].get(SIGNAL_SYMBOL) or {}).get("direction")
                and (etd_state["live_slopes"].get(SIGNAL_SYMBOL) or {}).get("direction") != previous_live_direction
            ),
            "etf_pair_direction_confirmed": bool(live_trade.get("direction")),
            "volume_increase": float(cached_fast_signal.get("volume_ratio") or 0.0) >= 1.2,
            "macd_short_reversal": (cached_fast_signal.get("direction") in ("UP", "DOWN") and cached_fast_signal.get("direction") == live_trade.get("direction")),
            "higher_low_lower_high": bool(live_trade.get("direction") and max(live_trade.get("up_votes", 0), live_trade.get("down_votes", 0)) >= 2),
            "vwap_reclaim_break": bool((state.get("last_primary_trend") or {}).get("above_vwap") is not None and live_trade.get("direction")),
        }
        reversal_candidate = feed.update_reversal_candidate_state(
            etd_state.get("reversal_candidate"),
            live_direction=live_trade.get("direction"),
            previous_direction=previous_live_direction,
            factors=factors,
            now=now,
        )
        if reversal_candidate.get("status") == "REVERSAL_CANDIDATE":
            freq = etd.reset_frequency_state_if_new_day(etd_state.get("frequency"), now.strftime("%Y%m%d"))
            prev_signal = (etd_state.get("last_signal") or {})
            etd_state["frequency"] = etd.apply_live_reversal_candidate_reaction(
                freq, previous_live_direction, prev_signal.get("score"), now,
            )
            state["live_trade_direction"] = {
                **live_trade,
                "direction": reversal_candidate.get("candidate_direction") or live_trade.get("direction"),
                "status": "REVERSAL_CANDIDATE",
                "structural_trend": (state.get("last_primary_trend") or {}).get("primary_trend"),
                "existing_direction_blocked": True,
                "first_detected_at": reversal_candidate.get("first_detected_at"),
                "confirmed_at": reversal_candidate.get("confirmed_at"),
                "detection_to_confirmation_delay_seconds": reversal_candidate.get("detection_to_confirmation_delay_seconds"),
            }
            cached_fast_signal = {
                **cached_fast_signal,
                "direction": state["live_trade_direction"]["direction"],
                "up_votes": 6 if state["live_trade_direction"]["direction"] == "UP" else 0,
                "down_votes": 6 if state["live_trade_direction"]["direction"] == "DOWN" else 0,
                "returns": {"3m": 0.9},
                "top_factors": reversal_candidate.get("active_factors") or [],
            }
            cached_confirmed_regime = etd.REGIME_FAST_REVERSAL_RANGE
        else:
            state["live_trade_direction"] = {
                **live_trade,
                "status": reversal_candidate.get("status"),
                "structural_trend": (state.get("last_primary_trend") or {}).get("primary_trend"),
                "existing_direction_blocked": bool(reversal_candidate.get("existing_direction_blocked")),
                "first_detected_at": reversal_candidate.get("first_detected_at"),
                "confirmed_at": reversal_candidate.get("confirmed_at"),
                "detection_to_confirmation_delay_seconds": reversal_candidate.get("detection_to_confirmation_delay_seconds"),
            }
            cached_confirmed_regime = (state.get("adaptive_regime") or {}).get("confirmed_regime")
        etd_state["reversal_candidate"] = reversal_candidate
        state["early_trend_detector"] = etd_state

        live = bool(state.get("early_trend_detector_live"))
        if not live:
            save_state_atomic(state)
            return {"skipped": True, "reason": "SHADOW/실전 — 가격 샘플만 갱신", "live_slopes": etd_state["live_slopes"]}

        if not is_new_entry_allowed(now):
            position = state.get("position") or {}
            if position.get("entry_type") != etd.ENTRY_TYPE_EARLY_PROBE:
                save_state_atomic(state)
                return {"skipped": True, "reason": "new entry window closed", "live_slopes": etd_state["live_slopes"]}

        if not _EARLY_ORDER_LOCK.acquire(blocking=False):
            etd_state["last_block_reason"] = "DUPLICATE_ORDER_LOCK: Early order worker already running"
            state["early_trend_detector"] = etd_state
            save_state_atomic(state)
            return {"skipped": True, "reason": etd_state["last_block_reason"], "live_slopes": etd_state["live_slopes"]}

        try:
            from app.config import get_config
            from app.trading.hynix_position_common import HynixPositionManager

            cfg = get_config()
            broker = _create_strategy_broker(cfg, resolved_mode)
            position_manager = HynixPositionManager(broker, mode=resolved_mode)
            etd_state["order_worker_name"] = "EARLY_FAST_WORKER"
            etd_state["latency"] = etd.mark_latency(
                etd.default_latency_trace(worker_name="EARLY_FAST_WORKER"), "account_query_started_at", now
            )
            state["early_trend_detector"] = etd_state
            position_manager.sync()
            etd_state = dict(state.get("early_trend_detector") or etd_state)
            etd_state["latency"] = etd.mark_latency(etd_state.get("latency"), "account_query_completed_at", kst_now())
            state["early_trend_detector"] = etd_state
            apply_position_manager_to_state(state, position_manager)

            early_result = _run_early_trend_detector_tick(
                state=state, mode=resolved_mode, now=now, fast_signal=cached_fast_signal, df_1min=None,
                confirmed_regime=cached_confirmed_regime, broker=broker, position_manager=position_manager,
                hynix_price=long_price, inverse_price=inverse_price, live_slopes=etd_state["live_slopes"],
            )
        except Exception as exc:
            logger.error("[EarlyTrendFastFeed] tick 실패: %s", exc)
            save_state_atomic(state)
            return {"skipped": True, "reason": f"tick failed: {exc}", "live_slopes": etd_state["live_slopes"]}
        finally:
            _EARLY_ORDER_LOCK.release()

        save_state_atomic(state)
        return {"skipped": False, "early_result": early_result, "live_slopes": etd_state["live_slopes"]}


def run_fast_trend_watcher_tick(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """30s fast live-trend watcher entry point.

    It only uses live Hynix minute trend for direction flips. The 3-minute AI
    cycle remains the broader confirmation/diagnostic path.
    """
    from app.models.hynix_enhanced_score import calculate_enhanced_hynix_prediction_score
    from app.services.hynix_switch_state import with_state_lock

    now = now or kst_now()
    resolved_mode = mode or load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        state = load_state(mode=resolved_mode)
        state["mode"] = resolved_mode
        if not state.get("auto_trade_on") or state.get("stopped"):
            return {"skipped": True, "reason": "auto off or stopped"}
        if not is_new_entry_allowed(now):
            _window_reason = describe_new_entry_window(now)["rule"]
            status = state.get("fast_trend_watcher") or {}
            status.update({"last_checked_at": now.isoformat(), "blocked_reason": _window_reason})
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": _window_reason, "state": state}

        enhanced_result = calculate_enhanced_hynix_prediction_score(mode=resolved_mode)
        df_1min = (enhanced_result.get("market_data") or {}).get("hynix_minute", {}).get("df_1min")
        fast_signal = compute_fast_trend_signal(df_1min, now=now)
        primary_trend_result = compute_primary_trend(
            df_1min, prev_close=enhanced_result.get("hynix_prev_close"), now=now,
        )
        primary_trend = primary_trend_result.get("primary_trend")
        state["last_primary_trend"] = primary_trend_result
        status = dict(state.get("fast_trend_watcher") or {})

        # 요구사항: fast confirmed direction(candidate_direction/confirmation_count)은
        # 날짜(위 auto off/rollover 분기에서 이미 처리) 뿐 아니라 계좌/보유수량이 바뀌면도
        # 초기화해야 한다 — 이전 사이클과 보유 상태가 달라졌다면 그 사이의 연속 확인
        # 횟수는 지금의 포지션에 대해 더 이상 유효하지 않다.
        position_now = state.get("position") or {}
        position_signature = f"{resolved_mode}:{position_now.get('symbol')}:{position_now.get('quantity') or 0}"
        if status.get("position_signature") not in (None, position_signature):
            status["candidate_direction"] = None
            status["confirmation_count"] = 0
        status["position_signature"] = position_signature

        direction = fast_signal.get("direction")
        if direction in ("UP", "DOWN"):
            if status.get("candidate_direction") == direction:
                status["confirmation_count"] = int(status.get("confirmation_count", 0)) + 1
            else:
                status["candidate_direction"] = direction
                status["confirmation_count"] = 1
        else:
            status["confirmation_count"] = 0
        status.update({
            "direction": direction,
            "last_signal": fast_signal,
            "last_checked_at": now.isoformat(timespec="seconds"),
            "actual_order_driver": "ENHANCED_REGIME_SWITCH",
            "primary_trend": primary_trend,
            "blocked_reason": None,
        })
        state["fast_trend_watcher"] = status

        # ── Early Trend Detector(요구사항, 토글 기본 OFF) ───────────────────
        # confirmed_regime 기반으로 RANGE 외 장세에서도 제한적 탐색진입을
        # 판단해야 하므로, 아래의 "PRIMARY_TREND==RANGE만 허용" 레거시 게이트보다
        # 먼저 평가한다. 실패해도(예: 브로커 오류) 레거시 로직은 그대로 이어진다.
        if state.get("early_trend_detector_enabled"):
            try:
                from app.trading.adaptive_market_regime import compute_and_confirm_regime

                _etd_adaptive = compute_and_confirm_regime(
                    df_1min, prev_close=enhanced_result.get("hynix_prev_close"),
                    confirmation_state=state.get("adaptive_regime_confirmation"), now=now,
                )
                state["adaptive_regime_confirmation"] = _etd_adaptive["confirmation_state"]
                state["adaptive_regime"] = {k: v for k, v in _etd_adaptive.items() if k != "confirmation_state"}
                _etd_confirmed_regime = _etd_adaptive.get("confirmed_regime")

                if state.get("early_trend_detector_live"):
                    _etd_state = dict(state.get("early_trend_detector") or {})
                    _etd_state["order_worker_name"] = "EARLY_FAST_WORKER"
                    _etd_state["main_cycle_waiting"] = False
                    _etd_state["last_main_cycle_note"] = "Early LIVE 신규진입은 5초 Fast Worker가 직접 실행"
                    state["early_trend_detector"] = _etd_state
                    status["actual_order_driver"] = "EARLY_FAST_WORKER"
                    status["blocked_reason"] = "Early LIVE entries handled by 5s Fast Worker"
                    state["fast_trend_watcher"] = status
                    save_state_atomic(state)
                    return {
                        "skipped": True,
                        "reason": status["blocked_reason"],
                        "fast_signal": fast_signal,
                        "state": state,
                    }

                from app.config import get_config
                from app.trading.broker_factory import create_broker

                cfg = get_config()
                _etd_broker = _create_strategy_broker(cfg, resolved_mode)
                _etd_position_manager = HynixPositionManager(_etd_broker, mode=resolved_mode)
                _etd_position_manager.sync(force=True)
                apply_position_manager_to_state(state, _etd_position_manager)

                from app.data_sources.hynix_long_collector import collect_long_current
                from app.data_sources.hynix_inverse_collector import collect_inverse_current

                _etd_long_quote = collect_long_current(mode=resolved_mode)
                _etd_inverse_quote = collect_inverse_current(mode=resolved_mode)

                early_result = _run_early_trend_detector_tick(
                    state=state, mode=resolved_mode, now=now, fast_signal=fast_signal, df_1min=df_1min,
                    confirmed_regime=_etd_confirmed_regime, broker=_etd_broker, position_manager=_etd_position_manager,
                    hynix_price=_etd_long_quote.get("current_price"), inverse_price=_etd_inverse_quote.get("current_price"),
                    live_slopes=(state.get("early_trend_detector") or {}).get("live_slopes"),
                )
                if early_result is not None:
                    save_state_atomic(state)
                    return early_result
            except Exception as exc:
                logger.debug("[EarlyTrendDetector] tick failed (harmless — legacy fast watcher continues): %s", exc)

        # 요구사항: Fast Watcher의 빠른 스위칭은 PRIMARY_TREND가 RANGE일 때만 허용한다.
        # UP/DOWN 추세 중에는 1·3·5분 신호가 그 추세에 대한 PULLBACK일 수 있으므로,
        # 이 30초 워처가 단독으로 전환하지 않는다 — 실제 추세반전은 update_reversal_confirmation
        # (VWAP 이탈 + 15분 추세 + 주요 저점/고점 붕괴, 2회 연속 확인)이 별도로 담당한다.
        if primary_trend != PRIMARY_TREND_RANGE:
            move_kind = classify_short_term_move(primary_trend, direction)
            status["blocked_reason"] = (
                f"PRIMARY_TREND={primary_trend} - fast rapid switching only runs in RANGE "
                f"(this move classified as {move_kind})"
            )
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "primary_trend": primary_trend_result, "state": state}

        if direction not in ("UP", "DOWN") or int(status.get("confirmation_count", 0)) < 2:
            save_state_atomic(state)
            return {"skipped": True, "reason": "fast trend not confirmed", "fast_signal": fast_signal, "state": state}

        final_action = "HYNIX_BUY" if direction == "UP" else "INVERSE_BUY"
        desired_symbol = _ACTION_TO_SYMBOL.get(final_action)
        held_symbol = (state.get("position") or {}).get("symbol")
        if desired_symbol == held_symbol:
            status["blocked_reason"] = "already holding confirmed fast direction"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}

        idempotency_key = f"FAST:{direction}:{now.strftime('%Y%m%d%H%M')}"
        previous_fast_execution = ((state.get("fast_trend_watcher") or {}).get("last_execution") or {})
        previous_orders = previous_fast_execution.get("orders") or []
        previous_had_success_order = any(bool(o.get("success")) for o in previous_orders if isinstance(o, dict))
        if state.get("last_execution_idempotency_key") == idempotency_key and previous_had_success_order:
            status["blocked_reason"] = "duplicate fast watcher idempotency key"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}
        _is_reversal_vs_holding = bool(held_symbol and desired_symbol != held_symbol)
        # 요구사항8 — same_direction_streak(연속 같은 방향 확인 횟수)과 reversal_streak
        # (현재 보유와 반대 방향 전환이 확인된 횟수)은 서로 다른 개념이다. Fast Watcher의
        # confirmation_count는 "같은 방향이 몇 틱 연속 확인됐는지"만 측정하므로,
        # 실제로 보유 종목과 반대 방향으로 전환하는 상황(_is_reversal_vs_holding)이
        # 아니면 reversal_streak은 0이어야 한다 — 과거에는 두 필드에 항상 같은 값을
        # 넣어 "그냥 같은 방향 유지"도 "반전 확인"처럼 보이게 했다.
        _confirmation_count = int(status.get("confirmation_count", 0))
        state["last_trend_switch_plan"] = {
            "proceed": True,
            "position_pct": 0.20,
            "entry_type": "EXPLORATORY",
            "immediate_switch": _is_reversal_vs_holding,
            "dominant_direction": "HYNIX" if direction == "UP" else "INVERSE",
            "desired_symbol": desired_symbol,
            "same_direction_streak": _confirmation_count,
            "reversal_streak": _confirmation_count if _is_reversal_vs_holding else 0,
            "pullback_wait_remaining_seconds": 0,
            "block_reason": None,
            "source": "FAST_TREND_WATCHER",
        }

        try:
            from app.config import get_config

            cfg = get_config()
            broker = _create_strategy_broker(cfg, resolved_mode)
            position_manager = HynixPositionManager(broker, mode=resolved_mode)
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            # 요구사항(2026-07-15) — 실행가는 000660이 아니라 LONG_SYMBOL(0193T0)의
            # 실제 현재가여야 한다(3분 사이클과 동일 원칙).
            from app.data_sources.hynix_long_collector import collect_long_current

            _fast_long_quote = collect_long_current(mode=resolved_mode)
            _fast_long_price = _fast_long_quote.get("current_price")
            if not _fast_long_price:
                status["blocked_reason"] = f"LONG_SYMBOL(0193T0) 현재가 조회 실패: {_fast_long_quote.get('error')}"
                state["fast_trend_watcher"] = status
                save_state_atomic(state)
                return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}
            if _is_reversal_vs_holding:
                # 요구사항7 — Fast Watcher는 REVERSAL_CANDIDATE만 만들고 단독으로 반대
                # ETF 전액 주문을 넣지 못한다. 최종 주문권한은 공용 반전 스위칭
                # 상태머신(run_reversal_switch_if_needed)만 갖는다 — run_switch_or_entry
                # (즉시 전량 스위칭)는 호출하지 않는다.
                #
                # 요구사항1 — 장세를 하루 고정값으로 쓰지 않고 20~30초마다 재평가한다.
                # 메인 3분 사이클과 동일한 confirmation_state 키(state["adaptive_regime_
                # confirmation"])를 공유해(별도 재분류가 아니라 같은 단일 소스를 더 빠른
                # 주기로 갱신) Fast Watcher의 30초 틱마다 함께 갱신한다.
                from app.trading.adaptive_market_regime import compute_and_confirm_regime

                _fast_adaptive_result = compute_and_confirm_regime(
                    df_1min, prev_close=enhanced_result.get("hynix_prev_close"),
                    confirmation_state=state.get("adaptive_regime_confirmation"), now=now,
                )
                state["adaptive_regime_confirmation"] = _fast_adaptive_result["confirmation_state"]
                state["adaptive_regime"] = {k: v for k, v in _fast_adaptive_result.items() if k != "confirmation_state"}
                _previous_confirmed_regime = _fast_adaptive_result.get("previous_regime")
                _current_confirmed_regime = _fast_adaptive_result.get("confirmed_regime")
                switch = run_reversal_switch_if_needed(
                    state, broker, _fast_long_price, enhanced_result.get("inverse_current_price"),
                    now=now, position_manager=position_manager,
                    hard_stop_triggered=(_current_confirmed_regime == "PANIC"),
                    regime_downgraded_to_range=(
                        _previous_confirmed_regime in ("STRONG_UP", "STRONG_DOWN")
                        and _current_confirmed_regime == "RANGE"
                    ),
                    snapshot=_fast_adaptive_result.get("snapshot"),
                    previous_regime=_previous_confirmed_regime, current_regime=_current_confirmed_regime,
                    allow_final_actions=False,
                )
            else:
                switch = run_switch_or_entry(
                    state, broker, final_action,
                    _fast_long_price, enhanced_result.get("inverse_current_price"),
                    now=now, forced=True, reason="FAST_TREND_WATCHER 2x confirmed",
                    position_manager=position_manager, target_position_pct=0.20, entry_type="EXPLORATORY",
                )
            status["last_execution"] = {
                "idempotency_key": idempotency_key,
                "final_action": final_action,
                "result_message": switch.get("message"),
                "orders": switch.get("orders", []),
            }
            if any(bool(o.get("success")) for o in (switch.get("orders") or []) if isinstance(o, dict)):
                state["last_execution_idempotency_key"] = idempotency_key
            elif state.get("last_execution_idempotency_key") == idempotency_key:
                state["last_execution_idempotency_key"] = None
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": False, "fast_signal": fast_signal, "switch": switch, "state": state}
        except Exception as exc:
            status["blocked_reason"] = f"fast watcher execution failed: {exc}"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            logger.error("[FastTrendWatcher] execution failed: %s", exc)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}


def _update_hynix_auto_trade_loop_locked(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """1회 실행 사이클의 실제 구현(반드시 with_state_lock(mode) 안에서만 호출).

    `now`는 테스트에서 시각을 주입하기 위한 선택 인자이며, 운영 시에는 항상 현재시각이 쓰인다.
    """
    warnings: list[str] = []
    now = now or kst_now()
    state = load_state(mode=mode)
    mode = mode or state.get("mode", "mock")
    state["mode"] = mode
    state["actual_order_driver"] = "ENHANCED_REGIME_SWITCH"

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

    hynix_signal_price = enhanced_result.get("hynix_current_price")
    inverse_price = enhanced_result.get("inverse_current_price")
    df_1min = (enhanced_result.get("market_data") or {}).get("hynix_minute", {}).get("df_1min")

    # 요구사항(2026-07-16) — ADAPTIVE_MARKET_REGIME을 신규진입/스위칭/손절/익절/
    # 보유시간이 전부 공유하는 단일 계산 지점으로 만든다. Enhanced 자동매매가
    # ON이면(auto_trade_on) 별도 수동 체크박스 없이 이 결과가 곧바로 LIVE로
    # 적용되고, OFF면 SHADOW(계산·표시만, 주문에 미반영)로 남는다.
    try:
        from app.trading.adaptive_market_regime import compute_and_confirm_regime

        adaptive_regime_result = compute_and_confirm_regime(
            df_1min, prev_close=enhanced_result.get("hynix_prev_close"),
            confirmation_state=state.get("adaptive_regime_confirmation"), now=now,
        )
        state["adaptive_regime_confirmation"] = adaptive_regime_result["confirmation_state"]
        state["adaptive_regime"] = {k: v for k, v in adaptive_regime_result.items() if k != "confirmation_state"}
    except Exception as exc:
        logger.error("[HynixSwitchEngine] adaptive_regime 계산 실패: %s", exc)
        warnings.append(f"adaptive_regime 계산 실패: {exc}")
    state["adaptive_regime_enabled"] = bool(state.get("auto_trade_on"))
    state["adaptive_regime_mode"] = "LIVE" if state["adaptive_regime_enabled"] else "SHADOW"

    # ── SHADOW MODE: Cycle Detector AI + Prediction AI V2(BUY/SELL/HOLD 확률) ──
    # 아래 호출은 `decision`/실제 주문에 절대 영향을 주지 않는다 — 계산·로그·state 저장만
    # 수행하며, 예외가 나도 무해하게 삼켜진다. 실제 주문 연결은 별도 승인 후 진행한다.
    # 신호 계산 참고용이므로 여기서는 000660 가격(hynix_signal_price)을 그대로 쓴다.
    _run_shadow_cycle_ai_and_decision_v2(state, enhanced_result, decision, df_1min, hynix_signal_price, inverse_price, now)

    # 요구사항(2026-07-15) — 실제 매매 종목은 000660이 아니라 LONG_SYMBOL(0193T0)이다.
    # 이 지점부터 `hynix_price`는 000660이 아니라 0193T0의 실제 현재가여야 한다 —
    # run_switch_or_entry/run_tp_sl_if_needed/run_liquidation_if_needed/evaluate_pullback_gate
    # 등 실행 계층에 넘기는 값이 그대로 주문가격·손익 계산 기준이 되기 때문이다.
    try:
        from app.data_sources.hynix_long_collector import collect_long_current

        _long_quote = collect_long_current(mode=mode)
        hynix_price = _long_quote.get("current_price")
        if _long_quote.get("stale"):
            warnings.append(f"LONG_SYMBOL(0193T0) 현재가가 최신이 아닐 수 있음: {_long_quote.get('error')}")
    except Exception as exc:
        logger.error("[HynixSwitchEngine] LONG_SYMBOL(0193T0) 현재가 조회 실패: %s", exc)
        warnings.append(f"LONG_SYMBOL(0193T0) 현재가 조회 실패: {exc}")
        hynix_price = None

    signal_data_ok = bool((enhanced_result.get("data_valid") or {}).get("hynix_signal_price", True))
    price_data_ok = hynix_price is not None and signal_data_ok
    order_api_ok = True
    broker = None
    real_gate_ok = True
    real_gate_status = None

    auto_trade_on = bool(state.get("auto_trade_on"))
    position_manager = None
    if auto_trade_on:
        try:
            from app.config import get_config

            cfg = get_config()
            if mode == "real":
                real_gate_status = (
                    cfg.enhanced_real_gate_status(current_mode=mode)
                    if hasattr(cfg, "enhanced_real_gate_status")
                    else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": [], "checks": {}}
                )
                real_gate_ok = bool(real_gate_status.get("ready"))
                trace["real_gate_status"] = real_gate_status
                if not real_gate_ok:
                    warnings.append(
                        "REAL 완전자동 게이트 미충족: "
                        + ", ".join(real_gate_status.get("blocking_reasons") or ["UNKNOWN"])
                        + " — 주문 실행 생략"
                    )
            if real_gate_ok:
                broker = _create_strategy_broker(cfg, mode)

            if broker is not None:
                # Broker가 유일한 Source of Truth — position_manager.sync()로 실제 포지션을
                # 먼저 확정하고, state는 그 결과를 담는 캐시로만 갱신한다.
                position_manager = HynixPositionManager(broker, mode=mode)
                position_manager.sync(force=True)
                apply_position_manager_to_state(state, position_manager)
                if state.get("position_conflict"):
                    warnings.append(state.get("critical_alert") or "0193T0/0197X0 동시 보유 감지 — 신규매수 금지")
        except Exception as exc:
            order_api_ok = False
            warnings.append(f"브로커 초기화 실패: {exc}")
            logger.error("[HynixSwitchEngine] 브로커 초기화 실패: %s", exc)

    total_equity = None
    daily_pnl_pct = None
    daily_return_blocked_this_cycle = False
    net_return_result = None
    if broker is not None:
        try:
            net_return_result = _compute_net_daily_return_with_retries(
                state, broker, state.get("position"), hynix_price, inverse_price, now,
            )
            snapshot = net_return_result.get("account_snapshot") or {}
            positions = snapshot.get("positions") or []
            cash = snapshot.get("cash")
            total_equity = snapshot.get("current_equity")
            is_mock_override = mode == "mock" and state.get("allow_mock_loss_override")

            # 요구사항 1/5절 — risk_manager와 UI가 동일한 값(원장 실현손익 + 미실현손익
            # / 시작자산)을 쓰도록 통합한다. get_buyable_cash()는 KIS API 오류(예: "1분당
            # 1회" 토큰발급 레이트리밋)가 나도 예외 없이 0.0을 반환하는 하위호환 계약이
            # 있어(KisMockBroker.get_orderable_cash), 그 값을 곧바로 "계좌가 0원이 됐다"로
            # 써버리면 일일 손실이 -100%로 오판된다(2026-07-14 실측). 새 계산식은 실현손익을
            # 원장(state)에서, 미실현손익을 보유 포지션+현재가에서 가져오므로 이 계좌조회
            # 글리치의 영향을 받지 않는다 — cash/positions는 교차검증에만 쓰인다.
            state["daily_return_calculation"] = {k: v for k, v in net_return_result.items()}
            daily_pnl_pct = net_return_result["net_daily_return"]

            if net_return_result["blocked_reason"]:
                daily_return_blocked_this_cycle = True
                warnings.append(
                    f"일일손익 판정 보류({net_return_result['blocked_reason']}) — "
                    f"신규주문만 일시 보류(기존 정지상태는 변경하지 않음)"
                )
                logger.warning(
                    "[HynixSwitchEngine] %s — 손익 판정 보류, -100%% 등으로 기록하지 않음",
                    net_return_result["blocked_reason"],
                )
            else:
                state["total_equity"] = total_equity
                # 요구사항(2026-07-20) — 이번 사이클에 기준자산이 방금 (재)확정됐다면
                # (baseline_just_established), 그 즉시 계산된 수익률만으로 자동매매를
                # 강제 중단하지 않는다 — 아직 가격/잔고 데이터가 안정적인지 한 번도
                # 확인되지 않은 첫 표본이다. 다음 사이클부터는 이미 확정된(같은)
                # 기준자산으로 다시 계산되므로, 진짜 -2.5% 이하 손실이면 그때 정상
                # 차단된다.
                if net_return_result.get("baseline_just_established"):
                    pass
                elif daily_pnl_pct is not None and daily_pnl_pct <= -2.5 and mode == "real":
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
        override_daily_loss_block=bool(state.get("daily_loss_block_override")),
    )

    liquidation_phase_now = get_liquidation_phase(now)

    orders_this_cycle: list = []
    attempted_entry = False
    # 요구사항(2026-07-20) — 신규진입 시간창(is_new_entry_allowed)은 더 이상 이
    # 플래그에 포함하지 않는다. trading_allowed는 이제 "기존 포지션 손절/익절/
    # 반전청산/15:15 강제청산을 이번 사이클에 시도해도 되는가"만 뜻하며, 신규
    # 진입 가능 여부는 아래 new_entry_allowed_now로 별도 판단한다 — 그래야
    # 09:15~09:30(신규진입 금지 구간)에도 보유 포지션 청산은 정상 실행된다.
    trading_allowed = (
        auto_trade_on and real_gate_ok and not state.get("stopped")
        and not daily_return_blocked_this_cycle and broker is not None
        and not state.get("position_sync_block_new_orders")
    )
    new_entry_window = describe_new_entry_window(now)
    new_entry_allowed_now = new_entry_window["allowed"]

    if not trading_allowed:
        trace["risk_manager_ok"] = False
        if state.get("stopped"):
            trace["risk_manager_reason"] = state.get("stopped_reason") or "자동매매 중단 상태"
        elif daily_return_blocked_this_cycle:
            blocked_reason = (net_return_result or {}).get("blocked_reason") or DAILY_RETURN_UNKNOWN
            trace["risk_manager_reason"] = f"{blocked_reason} — 계좌 데이터 이상으로 이번 사이클 신규주문 일시 보류"
        elif state.get("position_sync_block_new_orders"):
            trace["risk_manager_reason"] = state.get("critical_alert") or "POSITION_SYNC_PENDING"
        elif not auto_trade_on:
            trace["risk_manager_reason"] = "자동매매 OFF"
        elif not real_gate_ok:
            if real_gate_status:
                trace["risk_manager_reason"] = (
                    "REAL_GATE_NOT_READY: "
                    + ", ".join(real_gate_status.get("blocking_reasons") or ["UNKNOWN"])
                )
            else:
                trace["risk_manager_reason"] = "REAL_GATE_NOT_READY"
        elif broker is None:
            trace["risk_manager_reason"] = "브로커 초기화 실패"
    elif state.get("position_conflict"):
        trace["risk_manager_ok"] = False
        trace["risk_manager_reason"] = state.get("critical_alert") or "0193T0/0197X0 동시 보유 — 포지션 동기화 필요"

    if not new_entry_allowed_now:
        warnings.append(f"신규진입 시간창: {new_entry_window['rule']}")

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

            # 요구사항(2026-07-16) — 장중 다중 추세전환 상태머신: STRONG_TREND 보유 중
            # 반전 신호가 쌓이면 TP/SL(가격 기준)과는 별개로 선제 축소·전량청산·
            # (브로커 확인 후) 반대 ETF 탐색진입·확대를 단계적으로 실행한다. 이번
            # 사이클에 이미 한 번만 계산된 공용 state["adaptive_regime"] 결과(snapshot/
            # confirmed_regime/previous_regime)만 사용한다 — 별도 재분류하지 않는다.
            try:
                _ar = state.get("adaptive_regime") or {}
                _current_confirmed_regime = _ar.get("confirmed_regime")
                _previous_confirmed_regime = _ar.get("previous_regime")
                _regime_downgraded_to_range = (
                    _previous_confirmed_regime in ("STRONG_UP", "STRONG_DOWN")
                    and _current_confirmed_regime == "RANGE"
                )
                reversal_switch_result = run_reversal_switch_if_needed(
                    state, broker, hynix_price, inverse_price, now=now, position_manager=position_manager,
                    hard_stop_triggered=(_current_confirmed_regime == "PANIC"),
                    regime_downgraded_to_range=_regime_downgraded_to_range,
                    snapshot=_ar.get("snapshot"),
                    previous_regime=_previous_confirmed_regime, current_regime=_current_confirmed_regime,
                )
                orders_this_cycle.extend(reversal_switch_result.get("orders", []))
            except Exception as exc:
                logger.error("[HynixSwitchEngine] 반전 스위칭 처리 실패: %s", exc)
                warnings.append(f"반전 스위칭 처리 실패: {exc}")

            if not signal_data_ok:
                trace["entry_approved"] = False
                trace["entry_approved_reason"] = "DATA_UNIT_MISMATCH/DATA_ERROR — 000660 신호가격 검증 실패로 신규 진입 차단"
                warnings.append(trace["entry_approved_reason"])
            elif not tp_sl.get("triggered"):
                # 요구사항(2026-07-16 실측) — active_strategy_enabled가 켜져 있으면 이
                # 자리가 예전엔 `elif`로 아래 ENHANCED_REGIME_SWITCH 실제 판단과 배타적
                # 이었다. _run_active_strategy_entry/_run_adaptive_fusion_entry는 맨
                # 위에서 무조건 "acted": False(SHADOW_ONLY)만 반환하도록 이미 비활성화돼
                # 있는데(그 아래 실제 매수/매도 코드는 도달 불가능한 죽은 코드), 이
                # 토글이 켜져 있으면 그 사이클에 실제 엔진(ENHANCED_REGIME_SWITCH)이
                # 아예 실행되지 않아 BUY/INVERSE 신호가 나도 주문이 전혀 들어가지
                # 않았다 — "ACTIVE_STRATEGY shadow-only: actual broker orders are owned
                # by ENHANCED_REGIME_SWITCH"라는, 마치 다른 엔진이 대신 처리한 것처럼
                # 보이는 메시지만 남고 사실은 아무도 처리하지 않았다. 진단용 shadow
                # 계산(state["last_final_execution_decision"] 등 UI 표시용)은 그대로
                # 유지하되, 더 이상 아래 실제 진입 로직을 막지 않는다 — 항상 실행한다.
                if state.get("active_strategy_enabled"):
                    try:
                        # 요구사항(2026-07-21) — inverse_pressure_score로 enhanced_ai_score를
                        # 방향성 있게 보정하던 _boost_enhanced_score_with_inverse_pressure()는
                        # 이미 항등함수(return enhanced_score)로 무력화돼 있었고, 그 비대칭
                        # 의도가 담긴 죽은 코드였으므로 제거했다. enhanced_score를 그대로 쓴다.
                        _enhanced_score_for_entry = decision.get("enhanced_score")
                        if state.get("adaptive_fusion_enabled"):
                            _run_adaptive_fusion_entry(
                                state, broker, hynix_price, inverse_price, now, [],
                                enhanced_ai_score=_enhanced_score_for_entry, hynix_df_1min=df_1min,
                                position_manager=position_manager,
                            )
                        else:
                            _run_active_strategy_entry(
                                state, broker, hynix_price, inverse_price, now, [],
                                enhanced_ai_score=_enhanced_score_for_entry, position_manager=position_manager,
                            )
                    except Exception as exc:
                        logger.error("[HynixSwitchEngine] Active Strategy/Adaptive Fusion 진단 계산 실패(무해): %s", exc)
                        warnings.append(f"Active Strategy/Adaptive Fusion 진단 계산 실패: {exc}")

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
                    trace["enhanced_direction_approval"] = {
                        "approved": bool(is_new_entry and desired_symbol),
                        "final_action": final_action,
                        "desired_symbol": desired_symbol,
                        "direction": "UP" if desired_symbol == HYNIX_SYMBOL else "DOWN" if desired_symbol == INVERSE_SYMBOL else None,
                        "reason": reason,
                    }

                    # 요구사항(2026-07-16) — 이번 사이클이 갱신하기 전, "과거(직전 사이클까지)"
                    # 스냅샷을 먼저 떼어 UI에 별도로 보여준다. 예: 직전 사이클엔 눌림목
                    # 대기 중이었는데 이번 사이클엔 이미 확인횟수가 쌓여 즉시 진입이
                    # 승인된 경우, 과거 스냅샷("눌림목 대기")과 이번 사이클의 live 승인
                    # 상태가 뒤섞여 보이지 않게 한다.
                    _snapshot_trend_plan = dict(state.get("last_trend_switch_plan") or {})
                    _snapshot_confirm_tracker = dict(state.get("trend_switch_confirm_tracker") or {})
                    trace["snapshot_confirmation_count"] = _snapshot_confirm_tracker.get("same_direction_streak")
                    if _snapshot_trend_plan.get("block_reason"):
                        trace["snapshot_pullback_status"] = _snapshot_trend_plan["block_reason"]
                    elif _snapshot_trend_plan.get("proceed") is True:
                        trace["snapshot_pullback_status"] = "PROCEED(직전 사이클 승인)"
                    elif _snapshot_trend_plan:
                        trace["snapshot_pullback_status"] = "눌림목 대기 중(직전 사이클)"
                    else:
                        trace["snapshot_pullback_status"] = None

                    proceed = True
                    _early_detector_live_exclusive = bool(
                        state.get("early_trend_detector_enabled")
                        and state.get("early_trend_detector_live")
                    )
                    if not is_new_entry:
                        trace["entry_approved"] = True
                        trace["entry_approved_reason"] = "이미 목표 종목 보유 중 — 추가 진입 불필요"
                    elif _early_detector_live_exclusive:
                        # 요구사항(2026-07-20 최종) — Early Trend Detector가 LIVE인 동안
                        # ENHANCED_REGIME_SWITCH는 신규매수를 직접 실행하지 않는다.
                        # 방향(confirmed_regime) 승인과 50% 확대 승인만 계속 제공하고
                        # (Early Detector의 expansion_target_pct가 이 confirmed_regime을
                        # 그대로 참조한다), 실제 주문 실행은 Early Detector 전담이다.
                        proceed = False
                        trace["entry_approved"] = True
                        trace["entry_approved_reason"] = (
                            "Early Trend Detector LIVE — ENHANCED_REGIME_SWITCH는 신규매수 직접 실행 금지"
                            "(방향/50% 확대 승인만 담당, 실제 주문은 Early Detector 전담)"
                        )
                        trace["enhanced_direct_order_blocked"] = True
                        if not new_entry_allowed_now:
                            early_result = {
                                "skipped": True,
                                "reason": new_entry_window["rule"],
                                "reason_code": "TIME_GATE_BLOCK",
                            }
                        else:
                            try:
                                _early_base_fast_signal = compute_fast_trend_signal(df_1min, now=now)
                            except Exception:
                                _early_base_fast_signal = {}
                            early_fast_signal = _augment_fast_signal_with_enhanced_approval(_early_base_fast_signal, final_action, decision)
                            early_result = _run_early_trend_detector_tick(
                                state=state, mode=mode, now=now, fast_signal=early_fast_signal, df_1min=df_1min,
                                confirmed_regime=(state.get("adaptive_regime") or {}).get("confirmed_regime"),
                                broker=broker, position_manager=position_manager,
                                hynix_price=hynix_price, inverse_price=inverse_price,
                                live_slopes=(state.get("early_trend_detector") or {}).get("live_slopes"),
                            )
                            if early_result and early_result.get("switch"):
                                orders_this_cycle.extend((early_result.get("switch") or {}).get("orders", []))
                        _record_early_result_on_trace(trace, early_result)
                    elif state.get("position_conflict"):
                        proceed = False
                        warnings.append("포지션 동기화 필요(0193T0/0197X0 동시 보유) — 신규매수 금지")
                        trace["entry_approved"] = False
                        trace["entry_approved_reason"] = "포지션 동기화 필요(동시 보유) — 신규매수 금지"
                    elif (
                        (desired_symbol == INVERSE_SYMBOL and (state.get("adaptive_regime") or {}).get("confirmed_regime") == "STRONG_UP")
                        or (desired_symbol == HYNIX_SYMBOL and (state.get("adaptive_regime") or {}).get("confirmed_regime") == "STRONG_DOWN")
                    ) and not (
                        (state.get("live_trade_direction") or {}).get("status") == "REVERSAL_CANDIDATE"
                        and (state.get("live_trade_direction") or {}).get("direction") == ("UP" if desired_symbol == HYNIX_SYMBOL else "DOWN")
                    ):
                        # 요구사항(2026-07-16, 큰 추세 수익 극대화판) — STRONG_UP 확정 중에는
                        # 0197X0(인버스) 신규매수를, STRONG_DOWN 확정 중에는 0193T0(레버리지)
                        # 신규매수를 금지한다. Adaptive Regime(2연속 사이클로 이미 확정된
                        # confirmed_regime — 매 사이클 재분류하지 않고 공용 결과만 참조)만
                        # 사용한다.
                        proceed = False
                        _ar_confirmed_for_block = (state.get("adaptive_regime") or {}).get("confirmed_regime")
                        trace["entry_approved"] = False
                        trace["entry_approved_reason"] = (
                            f"Adaptive Regime={_ar_confirmed_for_block} 확정 중 — 반대방향 신규진입 금지"
                        )
                        warnings.append(trace["entry_approved_reason"])
                    else:
                        if str(final_action).endswith("_STRONG_BUY"):
                            confirm_tracker = update_confirm_tracker(
                                state.get("trend_switch_confirm_tracker") or default_confirm_state(),
                                final_action, held_symbol, desired_symbol, now,
                            )
                            state["trend_switch_confirm_tracker"] = confirm_tracker
                            trend_plan = plan_trend_switch_entry(
                                final_action=final_action,
                                held_symbol=held_symbol,
                                desired_symbol=desired_symbol,
                                confirm_tracker=confirm_tracker,
                                frequency_state=state.get("trend_switch_frequency_state") or default_trend_frequency_state(),
                                pullback_result=None,
                                now=now,
                                data_ok=bool(hynix_price and inverse_price),
                                has_unconfirmed_order=bool(state.get("order_in_flight") or state.get("pending_order")),
                                daily_return_pct=state.get("realized_pnl_today_pct"),
                                atr_pct=None,
                                override_daily_loss_block=bool(state.get("daily_loss_block_override")),
                            )
                            proceed = bool(trend_plan.get("proceed"))
                            # 강한 신호(STRONG_BUY)도 PRIMARY_TREND 차단은 무시할 수 없다 — "강한
                            # 신호니까 눌림목 대기 생략"이 단기 조정을 실제 추세전환으로 오판하게
                            # 두지 않는다.
                            # 요구사항(2026-07-16, 남은 통합 작업1) — 별도로 compute_primary_trend()를
                            # 다시 호출해 재분류하지 않는다. 이번 사이클에 이미 한 번만 계산된 공용
                            # state["adaptive_regime"] 결과에서만 파생시킨다.
                            strong_primary_trend_result = adaptive_regime_to_primary_trend_result(state.get("adaptive_regime"))
                            state["last_primary_trend"] = strong_primary_trend_result
                            strong_ptrend = strong_primary_trend_result.get("primary_trend", PRIMARY_TREND_RANGE)
                            trend_block_reason = None
                            if final_action == "INVERSE_STRONG_BUY" and new_inverse_entry_blocked(
                                strong_ptrend, strong_primary_trend_result.get("above_vwap"),
                                strong_primary_trend_result.get("above_ema20"), strong_primary_trend_result,
                            ):
                                votes, vote_reasons = inverse_block_vote_count(strong_primary_trend_result)
                                trend_block_reason = (
                                    f"PRIMARY_TREND=UP with {votes} uptrend confirmations({', '.join(vote_reasons)}) - even a strong INVERSE "
                                    "signal is blocked without 2x-confirmed VWAP/15m-trend/swing-low breakdown"
                                )
                            elif final_action == "HYNIX_STRONG_BUY" and new_hynix_entry_blocked(
                                strong_ptrend, strong_primary_trend_result.get("above_vwap"), strong_primary_trend_result.get("above_ema20"),
                            ):
                                trend_block_reason = (
                                    "PRIMARY_TREND=DOWN and price below VWAP/EMA20 - even a strong HYNIX "
                                    "signal is blocked without 2x-confirmed VWAP/15m-trend/swing-high breakout"
                                )
                            if trend_block_reason:
                                proceed = False
                            trace["entry_approved"] = proceed
                            trace["entry_approved_reason"] = (
                                f"{final_action} 강한 신호 — 눌림목 대기 생략"
                                if proceed else (trend_block_reason or trend_plan.get("block_reason") or "강한 신호 차단")
                            )
                            if not proceed:
                                warnings.append(trace["entry_approved_reason"])
                            state["last_trend_switch_plan"] = {
                                **trend_plan,
                                "dominant_direction": "HYNIX" if desired_symbol == HYNIX_SYMBOL else "INVERSE",
                                "desired_symbol": desired_symbol,
                                "pullback_wait_remaining_seconds": 0,
                                "primary_trend": strong_ptrend,
                                **({"block_reason": trend_block_reason} if trend_block_reason else {}),
                            }
                        else:
                            try:
                                # 요구사항(2026-07-16, 남은 통합 작업1) — 여기서도 재분류하지 않고
                                # 이번 사이클의 공용 adaptive_regime 결과에서만 파생시킨다.
                                primary_trend_result = adaptive_regime_to_primary_trend_result(state.get("adaptive_regime"))
                                state["last_primary_trend"] = primary_trend_result
                                gate = evaluate_pullback_gate(
                                    state, desired_symbol, final_action, now, forced_info, df_1min, mode,
                                    primary_trend_result=primary_trend_result,
                                )
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

                    # 요구사항(2026-07-16) — 이번 사이클에 새로 계산된 live 상태(위
                    # snapshot_*와 명확히 구분됨). same_direction_streak(연속 확인
                    # 횟수) 규칙: 1회=일반적으로 대기, 2회=탐색진입(20~30%), 3회
                    # 이상=확대(50%) — 단, STRONG 신호는 1회 확인만으로도 즉시
                    # 탐색진입을 허용한다(plan_entry의 "STRONG_BUY 첫 신호는 계속
                    # 소액 탐색 진입을 허용한다" 분기, entry_approved_reason에
                    # "강한 신호 — 눌림목 대기 생략"으로 명시됨).
                    trace["live_confirmation_count"] = (state.get("trend_switch_confirm_tracker") or {}).get("same_direction_streak")
                    trace["live_entry_gate_status"] = trace.get("entry_approved_reason")

                    if proceed:
                        attempted_entry = True
                        state.pop("pending_entry", None)
                        try:
                            switch = run_switch_or_entry(
                                state, broker, final_action, hynix_price, inverse_price,
                                now=now, forced=forced, reason=reason, position_manager=position_manager,
                            )
                            orders_this_cycle.extend(switch.get("orders", []))
                            trace["execution_stage"] = switch.get("stage")
                            # 요구사항(2026-07-16) — run_switch_or_entry()의 실제 결과는
                            # entry_approved_reason(진입 "승인" 사유 필드)을 덮어쓰지 않고
                            # 별도 필드에 저장한다. 과거에는 실제로 브로커가 주문을 거부
                            # (action=BUY, success=False)했을 때도 "_switch_sent_orders"가
                            # 비어있지 않다고 판정돼 entry_approved_reason이 그대로 남아,
                            # blocking_reason에 "EXPLORATORY 30% 진입 승인" 같은 승인 문구가
                            # 실패 사유인 것처럼 표시되는 사고가 있었다(2026-07-16 사용자
                            # 리포트: Entry Approved=YES, Order Sent=NO인데도 blocking_reason
                            # 이 승인 문구를 그대로 보여줌).
                            trace["execution_message"] = switch.get("message")
                            trace["order_failure_code"] = switch.get("failure_code")
                            trace["broker_error"] = switch.get("broker_error")
                            trace["requested_symbol"] = switch.get("requested_symbol")
                            trace["requested_qty"] = switch.get("requested_qty")
                            trace["order_price"] = switch.get("order_price")
                            trace["buyable_cash"] = switch.get("buyable_cash")
                            trace["sized_cash"] = switch.get("sized_cash")
                            trace["cooldown_remaining"] = switch.get("cooldown_remaining")
                            trace["pending_order"] = switch.get("pending_order", False)
                            _switch_sent_orders = [
                                o for o in (switch.get("orders") or [])
                                if o.get("action") in ("BUY", "SELL") and o.get("success")
                            ]
                            if not _switch_sent_orders:
                                # entry_approved_reason 자체는 "진입이 승인됐는지/왜"만
                                # 담는다 — 주문 실행 결과는 위 execution_message/
                                # order_failure_code로만 노출한다(같은 필드를 재사용하지
                                # 않음, 요구사항2).
                                pass
                        except Exception as exc:
                            logger.error("[HynixSwitchEngine] 스위칭/진입 처리 실패: %s", exc)
                            warnings.append(f"스위칭/진입 처리 실패: {exc}")
                            trace["execution_stage"] = "order_sent"
                            trace["execution_message"] = f"스위칭/진입 처리 예외: {exc}"
                            trace["order_failure_code"] = "EXECUTION_EXCEPTION"
                            trace["broker_error"] = str(exc)
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
    if trace["broker_executed"]:
        state["last_trade_time"] = now.isoformat()
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
    # unrealized_pnl은 GrossPnL이 아니라 NetPnL이다 — "지금 판다면" 발생할 매도수수료/
    # 거래세/슬리피지를 선차감해 표시한다(docs/requirements.md 섹션 2.10). 일손익
    # 리스크 게이트(daily_return_pct 기반 신규진입 중단/강제청산)도 이 값을 그대로
    # 쓰므로, Gross보다 보수적인(더 이르게 위험을 인식하는) 방향으로 안전하게 작동한다.
    position = state.get("position") or {}
    unrealized_pnl = 0.0
    gross_unrealized_pnl = 0.0
    if position.get("symbol") and (position.get("quantity") or 0) > 0 and position.get("entry_price"):
        cur = _current_price(position["symbol"], hynix_price, inverse_price)
        if cur is not None:
            try:
                from app.trading.trading_cost_engine import TradeCostEngine

                cost_result = TradeCostEngine().compute_unrealized_net_pnl(
                    position["symbol"], entry_price=position["entry_price"], current_price=cur,
                    quantity=position["quantity"],
                )
                unrealized_pnl = cost_result["net_unrealized_pnl"]
                gross_unrealized_pnl = cost_result["gross_unrealized_pnl"]
                state["unrealized_pnl_cost_breakdown"] = cost_result
            except Exception:
                unrealized_pnl = gross_unrealized_pnl = (cur - position["entry_price"]) * position["quantity"]
    state["unrealized_pnl"] = unrealized_pnl
    state["gross_unrealized_pnl"] = gross_unrealized_pnl

    # net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity(당일 시작
    # 자산) — 반드시 당일 시작 시점 자산(daily_pnl_baseline_equity)을 분모로 써야 한다.
    # total_equity(지금 이 순간의 계좌평가액)를 분모로 쓰지 않는다 — 그 값 자체가
    # 이미 오늘 손익을 반영해 시시각각 변하고, 계좌조회 실패 시 0을 반환할 수 있어
    # (2026-07-14 실측: 이 경로 때문에 일손실 -100%로 오판돼 자동매매가 잘못
    # 정지됨) 분모로 쓰면 위험하다. risk_manager 게이트(compute_net_daily_return, 위
    # 참조)와 반드시 같은 분모/공식을 써야 하므로(요구사항 1/5절) baseline이 아직
    # 확정되지 않았으면(당일 첫 유효 조회가 아직 없었으면) 갱신을 건너뛰고 이전 값을
    # 유지한다 — 0이나 total_equity로 대체하지 않는다.
    starting_equity = state.get("daily_pnl_baseline_equity")
    if starting_equity and starting_equity > 0:
        state["realized_pnl_today_pct"] = round(
            (state.get("realized_pnl_today_krw", 0.0) + unrealized_pnl) / starting_equity * 100.0, 4,
        )
        state["gross_realized_pnl_today_pct"] = round(
            (state.get("gross_realized_pnl_today_krw", 0.0) + gross_unrealized_pnl) / starting_equity * 100.0, 4,
        )
        # risk_manager와 UI가 같은 값을 쓰도록(요구사항 5절) 이번 사이클(주문 반영 후)
        # 최신 미실현손익 기준으로 daily_return_calculation도 함께 갱신한다.
        dr = state.get("daily_return_calculation") or {}
        dr.update({
            "starting_equity": starting_equity,
            "net_realized_pnl": state.get("realized_pnl_today_krw", 0.0),
            "net_unrealized_pnl": unrealized_pnl,
            "net_daily_return": state["realized_pnl_today_pct"],
            "calculation_source": "ledger_unified",
        })
        state["daily_return_calculation"] = dr

    trace["trade_counter"] = state.get("daily_trade_count", 0)
    trace["ui_synced"] = save_state_atomic(state)
    trace["stopped_stage"] = _first_blocked_stage(trace)
    # 사용자 요청 필드명(risk_approved/blocking_reason)도 함께 노출 — risk_manager_ok/
    # stopped_stage와 같은 값을 가리키는 별칭이다.
    trace["risk_approved"] = trace["risk_manager_ok"]
    trace["blocking_reason"] = _build_blocking_reason(trace)
    trace["signal_summary"] = _build_signal_summary(
        decision=decision,
        trace=trace,
        state=state,
        now=now,
        new_entry_allowed_now=new_entry_allowed_now,
        new_entry_window=new_entry_window,
    )

    # 백그라운드 스레드에서만 사이클이 돌아도(=Streamlit 세션에 아무도 접속하지 않아도)
    # UI가 "사이클 미실행"을 보여주지 않도록, 이번 사이클 결과(최종 trace 포함)를
    # state에도 남겨 한 번 더 저장한다.
    state["last_pipeline_trace"] = trace
    state["last_cycle_computed_at"] = now.isoformat()
    state["last_hynix_price"] = hynix_price  # legacy alias: LONG_SYMBOL(0193T0) execution price
    state["last_long_price"] = hynix_price  # LONG_SYMBOL(0193T0) — 실제 매매/손익 기준
    state["last_hynix_signal_price"] = hynix_signal_price  # 000660 — 감시 기초자산(신호 계산 참고용)
    state["last_inverse_price"] = inverse_price
    state["last_enhanced_result"] = enhanced_result
    state["last_decision"] = decision
    state["last_signal_summary"] = trace["signal_summary"]
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
        "new_entry_allowed": is_new_entry_allowed(now),
        "new_entry_window_rule": describe_new_entry_window(now)["rule"],
        "liquidation_phase": liquidation_phase,
        "hynix_current_price": hynix_price,
        "hynix_signal_price": hynix_signal_price,
        "long_current_price": hynix_price,
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
